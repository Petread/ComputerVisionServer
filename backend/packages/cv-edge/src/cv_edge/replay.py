from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field

from cv_contracts import EdgeResultAck, EdgeResultEnvelope

from cv_edge.client import EdgeAgentClient

PersistResult = Callable[[EdgeResultEnvelope], None]


@dataclass
class EdgeResultReplayTracker:
    confirmed: dict[tuple[str, str], int] = field(default_factory=dict)
    persisted: set[tuple[str, str, int]] = field(default_factory=set)

    def sync_once(
        self,
        client: EdgeAgentClient,
        persist: PersistResult,
        *,
        boot_id: str,
        limit: int = 100,
        idempotency_key: str,
    ) -> list[EdgeResultEnvelope]:
        device_boot = (str(client.address.management_ip), boot_id)
        after_sequence = self.confirmed.get(device_boot, 0)
        batch = client.results(
            boot_id=boot_id,
            after_sequence=after_sequence,
            limit=limit,
        )
        newly_persisted: list[EdgeResultEnvelope] = []
        for item in batch.items:
            key = (str(item.management_ip), item.boot_id, item.sequence)
            if key not in self.persisted:
                persist(item)
                self.persisted.add(key)
                newly_persisted.append(item)

        highest_contiguous = after_sequence
        available_sequences = {
            sequence
            for management_ip, item_boot_id, sequence in self.persisted
            if management_ip == str(batch.management_ip) and item_boot_id == boot_id
        }
        while highest_contiguous + 1 in available_sequences:
            highest_contiguous += 1

        if highest_contiguous > after_sequence:
            ack = EdgeResultAck(
                management_ip=batch.management_ip,
                boot_id=boot_id,
                acknowledged_through_sequence=highest_contiguous,
                idempotency_key=_ack_idempotency_key(
                    idempotency_key,
                    device_boot=device_boot,
                    sequence=highest_contiguous,
                ),
            )
            client.acknowledge_results(ack)
            self.confirmed[device_boot] = highest_contiguous
        return newly_persisted


def _ack_idempotency_key(
    base_key: str,
    *,
    device_boot: tuple[str, str],
    sequence: int,
) -> str:
    payload = f"{base_key}:{device_boot[0]}:{device_boot[1]}:{sequence}".encode()
    return f"result-ack-{hashlib.sha256(payload).hexdigest()}"
