from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cv_contracts import (
    EdgeDeploymentManifest,
    EdgeDeploymentState,
    EdgeMediaLeaseState,
    EdgePlantTriggerResponse,
    EdgeResultAck,
    EdgeResultEnvelope,
    EdgeResultStreamSummary,
    EdgeRuntimeProcess,
    EdgeRuntimeSlot,
)

from edge_agent.result_storage import (
    ACK_CACHE_MAX_ITEMS,
    TRIGGER_CACHE_MAX_BYTES,
    TRIGGER_CACHE_MAX_ITEMS,
    TRIGGER_REPLAY_SECONDS,
)


class TriggerCacheFullError(RuntimeError):
    """A live replay guarantee must not be evicted to accept another trigger."""


class EdgeStateStore:
    """Durable edge-local state. All mutating operations are transactional."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA synchronous = FULL")
        mode = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise RuntimeError(f"Edge state database did not enter WAL mode: {mode}")
        self._migrate()
        self._last_trigger_cleanup = 0.0
        self.prune_trigger_responses()

    @property
    def journal_mode(self) -> str:
        row = self._fetchone("PRAGMA journal_mode")
        if row is None:
            raise RuntimeError("Could not read edge state journal mode")
        return str(row[0]).lower()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _migrate(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deployments (
                    deployment_id TEXT PRIMARY KEY,
                    manifest_json TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    slot TEXT NOT NULL CHECK (slot IN ('A', 'B')),
                    command_keys_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_slots (
                    name TEXT PRIMARY KEY CHECK (name IN ('A', 'B')),
                    state TEXT NOT NULL,
                    deployment_id TEXT,
                    workload_release_id TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_processes (
                    deployment_id TEXT PRIMARY KEY,
                    process_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS results (
                    boot_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    envelope_json TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    PRIMARY KEY (boot_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS result_acks (
                    boot_id TEXT PRIMARY KEY,
                    acknowledged_sequence INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS result_ack_requests (
                    idempotency_key TEXT PRIMARY KEY,
                    ack_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS media_viewer_leases (
                    lease_id TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    lease_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plant_trigger_requests (
                    channel_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (channel_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS ix_results_boot_sequence
                    ON results (boot_id, sequence);
                CREATE INDEX IF NOT EXISTS ix_media_viewer_leases_channel_expiry
                    ON media_viewer_leases (channel_id, expires_at);
                CREATE INDEX IF NOT EXISTS ix_plant_trigger_channel_created
                    ON plant_trigger_requests (channel_id, created_at DESC);
                """
            )
            self._connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES('schema_version', '6')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """
            )
            columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(plant_trigger_requests)")
            }
            if "size_bytes" not in columns:
                self._connection.execute(
                    "ALTER TABLE plant_trigger_requests "
                    "ADD COLUMN size_bytes INTEGER NOT NULL DEFAULT 0"
                )
            self._connection.execute(
                "UPDATE plant_trigger_requests "
                "SET size_bytes = length(CAST(response_json AS BLOB)) "
                "WHERE size_bytes = 0"
            )
            self._recover_reused_sequences()
            self._prune_ack_requests(self._connection)
            for name in ("A", "B"):
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO runtime_slots(
                        name, state, deployment_id, workload_release_id, updated_at
                    ) VALUES(?, 'empty', NULL, NULL, '1970-01-01T00:00:00+00:00')
                    """,
                    (name,),
                )

    def _recover_reused_sequences(self) -> None:
        # Older agents could restart numbering after an ACK drained the queue.
        # Such later rows were never deliverable: give them fresh numbers instead
        # of silently deleting their results. Each update is restart-safe.
        rows = self._connection.execute(
            "SELECT r.boot_id, r.sequence, r.envelope_json FROM results r "
            "JOIN result_acks a ON a.boot_id = r.boot_id "
            "WHERE r.sequence <= a.acknowledged_sequence ORDER BY r.boot_id, r.sequence"
        ).fetchall()
        for row in rows:
            sequence = self.highest_sequence(str(row["boot_id"])) + 1
            payload = json.loads(row["envelope_json"])
            payload["sequence"] = sequence
            serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            self._connection.execute(
                "UPDATE results SET sequence = ?, envelope_json = ?, size_bytes = ? "
                "WHERE boot_id = ? AND sequence = ?",
                (
                    sequence,
                    serialized,
                    len(serialized.encode("utf-8")),
                    row["boot_id"],
                    row["sequence"],
                ),
            )

    def plant_trigger_response(
        self, channel_id: str, idempotency_key: str
    ) -> tuple[str, EdgePlantTriggerResponse] | None:
        self.prune_trigger_responses()
        row = self._fetchone(
            """
            SELECT request_digest, response_json
            FROM plant_trigger_requests
            WHERE channel_id = ? AND idempotency_key = ?
            """,
            (channel_id, idempotency_key),
        )
        if row is None:
            return None
        return (
            str(row["request_digest"]),
            EdgePlantTriggerResponse.model_validate_json(row["response_json"]),
        )

    def latest_plant_trigger_response(self, channel_id: str) -> EdgePlantTriggerResponse | None:
        self.prune_trigger_responses()
        row = self._fetchone(
            """
            SELECT response_json FROM plant_trigger_requests
            WHERE channel_id = ? ORDER BY created_at DESC LIMIT 1
            """,
            (channel_id,),
        )
        return (
            None
            if row is None
            else EdgePlantTriggerResponse.model_validate_json(row["response_json"])
        )

    def save_plant_trigger_response(
        self,
        channel_id: str,
        idempotency_key: str,
        request_digest: str,
        response: EdgePlantTriggerResponse,
        *,
        created_at: str,
    ) -> None:
        serialized = response.model_dump_json()
        size_bytes = len(serialized.encode("utf-8"))
        with self._transaction() as connection:
            if not self.trigger_cache_has_capacity(size_bytes):
                raise TriggerCacheFullError("The bounded trigger replay cache is full.")
            connection.execute(
                """
                INSERT INTO plant_trigger_requests(
                    channel_id, idempotency_key, request_digest,
                    response_json, created_at, size_bytes
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    channel_id,
                    idempotency_key,
                    request_digest,
                    serialized,
                    created_at,
                    size_bytes,
                ),
            )

    def trigger_cache_has_capacity(
        self, additional_bytes: int, *, additional_items: int = 1
    ) -> bool:
        row = self._fetchone(
            "SELECT COUNT(*) AS items, COALESCE(SUM(size_bytes), 0) AS bytes "
            "FROM plant_trigger_requests"
        )
        return bool(
            row is not None
            and int(row["items"]) + additional_items <= TRIGGER_CACHE_MAX_ITEMS
            and int(row["bytes"]) + additional_bytes <= TRIGGER_CACHE_MAX_BYTES
        )

    def prune_trigger_responses(self) -> None:
        cutoff = (datetime.now(UTC) - timedelta(seconds=TRIGGER_REPLAY_SECONDS)).isoformat()
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM plant_trigger_requests WHERE julianday(created_at) <= julianday(?)",
                (cutoff,),
            )
        self._last_trigger_cleanup = time.monotonic()

    @staticmethod
    def _prune_ack_requests(connection: sqlite3.Connection) -> None:
        connection.execute(
            "DELETE FROM result_ack_requests WHERE rowid <= "
            "(SELECT rowid FROM result_ack_requests ORDER BY rowid DESC LIMIT 1 OFFSET ?)",
            (ACK_CACHE_MAX_ITEMS,),
        )

    def metadata(self, key: str) -> str | None:
        row = self._fetchone("SELECT value FROM metadata WHERE key = ?", (key,))
        return None if row is None else str(row["value"])

    def set_metadata(self, key: str, value: str | None) -> None:
        with self._transaction() as connection:
            if value is None:
                connection.execute("DELETE FROM metadata WHERE key = ?", (key,))
            else:
                connection.execute(
                    """
                    INSERT INTO metadata(key, value) VALUES(?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, value),
                )

    def deployment(
        self, deployment_id: str
    ) -> tuple[EdgeDeploymentManifest, EdgeDeploymentState, str, dict[str, str]] | None:
        row = self._fetchone("SELECT * FROM deployments WHERE deployment_id = ?", (deployment_id,))
        return None if row is None else self._deployment_from_row(row)

    def deployments(
        self,
    ) -> list[tuple[EdgeDeploymentManifest, EdgeDeploymentState, str, dict[str, str]]]:
        rows = self._fetchall("SELECT * FROM deployments ORDER BY updated_at, deployment_id")
        return [self._deployment_from_row(row) for row in rows]

    def prepare_deployment(
        self,
        manifest: EdgeDeploymentManifest,
        state: EdgeDeploymentState,
        slot: str,
        *,
        updated_at: str,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO deployments(
                    deployment_id, manifest_json, state_json, slot,
                    command_keys_json, updated_at
                ) VALUES(?, ?, ?, ?, '{}', ?)
                """,
                (
                    manifest.deployment_id,
                    manifest.model_dump_json(),
                    state.model_dump_json(),
                    slot,
                    updated_at,
                ),
            )
            connection.execute(
                """
                UPDATE runtime_slots
                SET state = 'prepared', deployment_id = ?, workload_release_id = ?, updated_at = ?
                WHERE name = ?
                """,
                (manifest.deployment_id, manifest.workload_release_id, updated_at, slot),
            )

    def mark_activation_failed(
        self,
        deployment_id: str,
        state: EdgeDeploymentState,
        command_keys: dict[str, str],
        *,
        updated_at: str,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE deployments SET state_json = ?, command_keys_json = ?, updated_at = ?
                WHERE deployment_id = ?
                """,
                (state.model_dump_json(), json.dumps(command_keys), updated_at, deployment_id),
            )
            connection.execute(
                "UPDATE runtime_slots SET state = 'failed', updated_at = ? WHERE deployment_id = ?",
                (updated_at, deployment_id),
            )

    def save_deployment_state(
        self,
        deployment_id: str,
        state: EdgeDeploymentState,
        *,
        updated_at: str,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE deployments SET state_json = ?, updated_at = ? WHERE deployment_id = ?",
                (state.model_dump_json(), updated_at, deployment_id),
            )

    def activate_deployment(
        self,
        deployment_id: str,
        state: EdgeDeploymentState,
        command_keys: dict[str, str],
        *,
        updated_at: str,
    ) -> EdgeDeploymentState | None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT slot FROM deployments WHERE deployment_id = ?", (deployment_id,)
            ).fetchone()
            if row is None:
                raise KeyError(deployment_id)
            new_slot = str(row["slot"])
            active_slot = self._metadata_in(connection, "active_slot")
            previous_state: EdgeDeploymentState | None = None
            if active_slot and active_slot != new_slot:
                previous_row = connection.execute(
                    "SELECT deployment_id FROM runtime_slots WHERE name = ?", (active_slot,)
                ).fetchone()
                if previous_row is not None and previous_row["deployment_id"]:
                    previous_id = str(previous_row["deployment_id"])
                    deployment_row = connection.execute(
                        "SELECT state_json FROM deployments WHERE deployment_id = ?", (previous_id,)
                    ).fetchone()
                    if deployment_row is not None:
                        previous_state = EdgeDeploymentState.model_validate_json(
                            deployment_row["state_json"]
                        ).model_copy(update={"status": "standby"})
                        connection.execute(
                            """
                            UPDATE deployments SET state_json = ?, updated_at = ?
                            WHERE deployment_id = ?
                            """,
                            (previous_state.model_dump_json(), updated_at, previous_id),
                        )
                connection.execute(
                    "UPDATE runtime_slots SET state = 'standby', updated_at = ? WHERE name = ?",
                    (updated_at, active_slot),
                )
                self._set_metadata_in(connection, "previous_slot", active_slot)
            connection.execute(
                """
                UPDATE deployments SET state_json = ?, command_keys_json = ?, updated_at = ?
                WHERE deployment_id = ?
                """,
                (state.model_dump_json(), json.dumps(command_keys), updated_at, deployment_id),
            )
            connection.execute(
                "UPDATE runtime_slots SET state = 'active', updated_at = ? WHERE name = ?",
                (updated_at, new_slot),
            )
            self._set_metadata_in(connection, "active_slot", new_slot)
            if active_slot == new_slot:
                self._set_metadata_in(connection, "previous_slot", None)
            return previous_state

    def rollback_deployment(
        self,
        deployment_id: str,
        state: EdgeDeploymentState,
        command_keys: dict[str, str],
        *,
        updated_at: str,
    ) -> EdgeDeploymentState | None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT slot FROM deployments WHERE deployment_id = ?", (deployment_id,)
            ).fetchone()
            if row is None:
                raise KeyError(deployment_id)
            current_slot = str(row["slot"])
            connection.execute(
                """
                UPDATE deployments SET state_json = ?, command_keys_json = ?, updated_at = ?
                WHERE deployment_id = ?
                """,
                (state.model_dump_json(), json.dumps(command_keys), updated_at, deployment_id),
            )
            connection.execute(
                "UPDATE runtime_slots SET state = 'failed', updated_at = ? WHERE name = ?",
                (updated_at, current_slot),
            )
            restored: EdgeDeploymentState | None = None
            active_slot = self._metadata_in(connection, "active_slot")
            if active_slot and active_slot != current_slot:
                active = connection.execute(
                    "SELECT deployment_id FROM runtime_slots WHERE name = ?", (active_slot,)
                ).fetchone()
                if active is not None and active["deployment_id"]:
                    active_row = connection.execute(
                        "SELECT state_json FROM deployments WHERE deployment_id = ?",
                        (str(active["deployment_id"]),),
                    ).fetchone()
                    if active_row is not None:
                        return EdgeDeploymentState.model_validate_json(active_row["state_json"])
            previous_slot = self._metadata_in(connection, "previous_slot")
            if previous_slot and previous_slot != current_slot:
                previous = connection.execute(
                    "SELECT deployment_id FROM runtime_slots WHERE name = ?", (previous_slot,)
                ).fetchone()
                if previous is not None and previous["deployment_id"]:
                    previous_id = str(previous["deployment_id"])
                    previous_row = connection.execute(
                        "SELECT state_json FROM deployments WHERE deployment_id = ?", (previous_id,)
                    ).fetchone()
                    if previous_row is not None:
                        restored = EdgeDeploymentState.model_validate_json(
                            previous_row["state_json"]
                        ).model_copy(
                            update={
                                "status": "running",
                                "activated_at": updated_at,
                                "last_error": None,
                            }
                        )
                        connection.execute(
                            """
                            UPDATE deployments SET state_json = ?, updated_at = ?
                            WHERE deployment_id = ?
                            """,
                            (restored.model_dump_json(), updated_at, previous_id),
                        )
                        connection.execute(
                            """
                            UPDATE runtime_slots SET state = 'active', updated_at = ?
                            WHERE name = ?
                            """,
                            (updated_at, previous_slot),
                        )
                        self._set_metadata_in(connection, "active_slot", previous_slot)
                        self._set_metadata_in(connection, "previous_slot", None)
            if restored is None:
                self._set_metadata_in(connection, "active_slot", None)
                self._set_metadata_in(connection, "previous_slot", None)
            return restored

    def stop_deployment(
        self,
        deployment_id: str,
        state: EdgeDeploymentState,
        command_keys: dict[str, str],
        *,
        updated_at: str,
    ) -> None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT slot FROM deployments WHERE deployment_id = ?", (deployment_id,)
            ).fetchone()
            if row is None:
                raise KeyError(deployment_id)
            slot = str(row["slot"])
            connection.execute(
                """
                UPDATE deployments SET state_json = ?, command_keys_json = ?, updated_at = ?
                WHERE deployment_id = ?
                """,
                (state.model_dump_json(), json.dumps(command_keys), updated_at, deployment_id),
            )
            connection.execute(
                """
                UPDATE runtime_slots SET state = 'empty', deployment_id = NULL,
                    workload_release_id = NULL, updated_at = ? WHERE name = ?
                """,
                (updated_at, slot),
            )
            if self._metadata_in(connection, "active_slot") == slot:
                self._set_metadata_in(connection, "active_slot", None)
            if self._metadata_in(connection, "previous_slot") == slot:
                self._set_metadata_in(connection, "previous_slot", None)

    def slots(self) -> list[EdgeRuntimeSlot]:
        rows = self._fetchall("SELECT * FROM runtime_slots ORDER BY name")
        return [
            EdgeRuntimeSlot(
                name=row["name"],
                state=row["state"],
                deployment_id=row["deployment_id"],
                workload_release_id=row["workload_release_id"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def save_process(self, process: EdgeRuntimeProcess, *, updated_at: str) -> None:
        if time.monotonic() - self._last_trigger_cleanup >= 60:
            self.prune_trigger_responses()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO runtime_processes(deployment_id, process_json, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(deployment_id) DO UPDATE SET
                    process_json = excluded.process_json, updated_at = excluded.updated_at
                """,
                (process.deployment_id, process.model_dump_json(), updated_at),
            )

    def processes(self) -> list[EdgeRuntimeProcess]:
        rows = self._fetchall("SELECT process_json FROM runtime_processes ORDER BY deployment_id")
        return [EdgeRuntimeProcess.model_validate_json(row["process_json"]) for row in rows]

    def next_sequence(self, boot_id: str) -> int:
        row = self._fetchone(
            "SELECT COALESCE(MAX(sequence), 0) AS value FROM results WHERE boot_id = ?",
            (boot_id,),
        )
        if row is None:
            raise RuntimeError("Could not determine the next result sequence")
        return max(int(row["value"]), self.acknowledged_sequence(boot_id)) + 1

    def insert_result(self, result: EdgeResultEnvelope, *, size_bytes: int) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO results(boot_id, sequence, envelope_json, size_bytes)
                VALUES(?, ?, ?, ?)
                """,
                (result.boot_id, result.sequence, result.model_dump_json(), size_bytes),
            )

    def results_after(self, boot_id: str, sequence: int) -> list[EdgeResultEnvelope]:
        rows = self._fetchall(
            """
            SELECT envelope_json FROM results
            WHERE boot_id = ? AND sequence > ? ORDER BY sequence
            """,
            (boot_id, sequence),
        )
        return [EdgeResultEnvelope.model_validate_json(row["envelope_json"]) for row in rows]

    def highest_sequence(self, boot_id: str) -> int:
        row = self._fetchone(
            "SELECT COALESCE(MAX(sequence), 0) AS value FROM results WHERE boot_id = ?",
            (boot_id,),
        )
        if row is None:
            raise RuntimeError("Could not determine the highest result sequence")
        return max(int(row["value"]), self.acknowledged_sequence(boot_id))

    def acknowledged_sequence(self, boot_id: str) -> int:
        row = self._fetchone(
            "SELECT acknowledged_sequence FROM result_acks WHERE boot_id = ?", (boot_id,)
        )
        return 0 if row is None else int(row["acknowledged_sequence"])

    def ack_by_key(self, key: str) -> EdgeResultAck | None:
        row = self._fetchone(
            "SELECT ack_json FROM result_ack_requests WHERE idempotency_key = ?", (key,)
        )
        return None if row is None else EdgeResultAck.model_validate_json(row["ack_json"])

    def acknowledge(self, ack: EdgeResultAck) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO result_acks(boot_id, acknowledged_sequence) VALUES(?, ?)
                ON CONFLICT(boot_id) DO UPDATE SET
                    acknowledged_sequence = MAX(
                        acknowledged_sequence, excluded.acknowledged_sequence
                    )
                """,
                (ack.boot_id, ack.acknowledged_through_sequence),
            )
            connection.execute(
                "INSERT INTO result_ack_requests(idempotency_key, ack_json) VALUES(?, ?)",
                (ack.idempotency_key, ack.model_dump_json()),
            )
            connection.execute(
                "DELETE FROM results WHERE boot_id = ? AND sequence <= ?",
                (ack.boot_id, ack.acknowledged_through_sequence),
            )
            self._prune_ack_requests(connection)

    def result_streams(self) -> list[EdgeResultStreamSummary]:
        rows = self._fetchall(
            """
            WITH boot_domains AS (
                SELECT boot_id FROM results
                UNION
                SELECT boot_id FROM result_acks
            )
            SELECT b.boot_id AS boot_id,
                   COALESCE(
                       MIN(
                           CASE
                               WHEN r.sequence > COALESCE(a.acknowledged_sequence, 0)
                               THEN r.sequence
                           END
                       ),
                       0
                   ) AS earliest_sequence,
                   MAX(
                       COALESCE(MAX(r.sequence), 0),
                       COALESCE(a.acknowledged_sequence, 0)
                   ) AS highest_sequence,
                   SUM(
                       CASE
                           WHEN r.sequence > COALESCE(a.acknowledged_sequence, 0) THEN 1
                           ELSE 0
                       END
                   ) AS pending_count,
                   COALESCE(a.acknowledged_sequence, 0) AS acknowledged_sequence
            FROM boot_domains b
            LEFT JOIN result_acks a ON a.boot_id = b.boot_id
            LEFT JOIN results r ON r.boot_id = b.boot_id
            GROUP BY b.boot_id, a.acknowledged_sequence
            ORDER BY b.boot_id
            """
        )
        return [
            EdgeResultStreamSummary(
                boot_id=str(row["boot_id"]),
                earliest_sequence=int(row["earliest_sequence"]),
                highest_sequence=int(row["highest_sequence"]),
                acknowledged_sequence=int(row["acknowledged_sequence"]),
                pending_count=int(row["pending_count"]),
            )
            for row in rows
        ]

    def save_media_lease(self, lease: EdgeMediaLeaseState) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO media_viewer_leases(lease_id, channel_id, lease_json, expires_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(lease_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    lease_json = excluded.lease_json,
                    expires_at = excluded.expires_at
                """,
                (lease.lease_id, lease.channel_id, lease.model_dump_json(), lease.expires_at),
            )

    def media_lease(self, lease_id: str, *, now: str) -> EdgeMediaLeaseState | None:
        self.delete_expired_media_leases(now=now)
        row = self._fetchone(
            "SELECT lease_json FROM media_viewer_leases WHERE lease_id = ?", (lease_id,)
        )
        return None if row is None else EdgeMediaLeaseState.model_validate_json(row["lease_json"])

    def active_media_leases(self, channel_id: str, *, now: str) -> list[EdgeMediaLeaseState]:
        self.delete_expired_media_leases(now=now)
        rows = self._fetchall(
            """
            SELECT lease_json FROM media_viewer_leases
            WHERE channel_id = ? AND expires_at > ? ORDER BY expires_at
            """,
            (channel_id, now),
        )
        return [EdgeMediaLeaseState.model_validate_json(row["lease_json"]) for row in rows]

    def release_media_lease(self, lease_id: str) -> None:
        with self._transaction() as connection:
            connection.execute("DELETE FROM media_viewer_leases WHERE lease_id = ?", (lease_id,))

    def delete_expired_media_leases(self, *, now: str) -> None:
        with self._transaction() as connection:
            connection.execute("DELETE FROM media_viewer_leases WHERE expires_at <= ?", (now,))

    def outbox_usage(self) -> tuple[int, int]:
        row = self._fetchone(
            """
            SELECT COUNT(*) AS items, COALESCE(SUM(r.size_bytes), 0) AS bytes
            FROM results r
            LEFT JOIN result_acks a ON a.boot_id = r.boot_id
            WHERE r.sequence > COALESCE(a.acknowledged_sequence, 0)
            """
        )
        if row is None:
            raise RuntimeError("Could not determine result outbox usage")
        return int(row["items"]), int(row["bytes"])

    def has_contiguous_results(self, boot_id: str, start: int, end: int) -> bool:
        if end < start:
            return True
        row = self._fetchone(
            """
            SELECT COUNT(*) AS count FROM results
            WHERE boot_id = ? AND sequence BETWEEN ? AND ?
            """,
            (boot_id, start, end),
        )
        if row is None:
            return False
        return int(row["count"]) == end - start + 1

    def _fetchone(self, query: str, parameters: tuple[object, ...] = ()) -> sqlite3.Row | None:
        """Serialize reads with writes because this store owns one SQLite connection."""
        with self._lock:
            return self._connection.execute(query, parameters).fetchone()

    def _fetchall(self, query: str, parameters: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        """Materialize rows while holding the shared-connection lock."""
        with self._lock:
            return list(self._connection.execute(query, parameters).fetchall())

    @staticmethod
    def _deployment_from_row(
        row: sqlite3.Row,
    ) -> tuple[EdgeDeploymentManifest, EdgeDeploymentState, str, dict[str, str]]:
        return (
            EdgeDeploymentManifest.model_validate_json(row["manifest_json"]),
            EdgeDeploymentState.model_validate_json(row["state_json"]),
            str(row["slot"]),
            dict(json.loads(row["command_keys_json"])),
        )

    def _transaction(self) -> _Transaction:
        return _Transaction(self._connection, self._lock)

    @staticmethod
    def _metadata_in(connection: sqlite3.Connection, key: str) -> str | None:
        row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    @staticmethod
    def _set_metadata_in(connection: sqlite3.Connection, key: str, value: str | None) -> None:
        if value is None:
            connection.execute("DELETE FROM metadata WHERE key = ?", (key,))
            return
        connection.execute(
            """
            INSERT INTO metadata(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


class _Transaction:
    def __init__(self, connection: sqlite3.Connection, lock: threading.RLock) -> None:
        self.connection = connection
        self.lock = lock

    def __enter__(self) -> sqlite3.Connection:
        self.lock.acquire()
        self.connection.execute("BEGIN IMMEDIATE")
        return self.connection

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            self.connection.execute("COMMIT" if exc_type is None else "ROLLBACK")
        finally:
            self.lock.release()
