from __future__ import annotations

import importlib.util
import json
import os
import shutil
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from cv_contracts import EdgeDeviceAddress
from cv_contracts.edge import EdgeSettingsCommand
from cv_contracts.edge_environment import DEFAULTS, IP_KEY, revision, validate_values
from edge_agent.settings import queue_settings, settings_busy, settings_status
from edge_agent.state import EdgeAgentConfig

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "key,value",
    [
        (IP_KEY, "192.168.50.0/24"),
        (IP_KEY, "::1"),
        (IP_KEY, "1.2.3.4\nYBB_ALLOW_PLC_WRITES=true"),
        (IP_KEY, "0.0.0.0"),
        (IP_KEY, "255.255.255.255"),
        (IP_KEY, "224.0.0.1"),
        ("YBB_EDGE_TRTEXEC_PATH", "/tmp/evil"),
        ("YBB_ALLOW_PLC_WRITES", "true"),
        ("YBB_EDGE_PLANT_MAX_CONCURRENT_TRIGGERS", "0"),
        ("YBB_EDGE_PREVIEW_JPEG_QUALITY", "96"),
        ("YBB_EDGE_PLANT_RATE_LIMIT_PER_SECOND", "1; reboot"),
    ],
)
def test_rejects_unsafe_or_unbounded_values(key: str, value: str) -> None:
    with pytest.raises(ValueError):
        validate_values({**DEFAULTS, key: value})


def test_canonical_client_list_and_empty_revoke() -> None:
    values = validate_values({**DEFAULTS, IP_KEY: "192.168.50.41, 192.168.50.40,192.168.50.41"})
    assert values[IP_KEY] == "192.168.50.40,192.168.50.41"
    assert validate_values(DEFAULTS)[IP_KEY] == ""


@pytest.fixture
def setup_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple:
    install = tmp_path / "install"
    install.mkdir()
    shutil.copy(ROOT / "deployment/edge/edge-settings-helper.py", install)
    shutil.copy(
        ROOT / "backend/packages/cv-contracts/src/cv_contracts/edge_environment.py", install
    )
    spec = importlib.util.spec_from_file_location(
        "settings_helper_test", install / "edge-settings-helper.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "WINDOWS", False)
    monkeypatch.setattr(module, "ufw_rules", lambda: "Status: active")
    data = tmp_path / "data"
    data.mkdir()
    environment = {
        **DEFAULTS,
        "YBB_EDGE_MANAGEMENT_IP": "192.168.50.21",
        "YBB_EDGE_PLANT_API_PORT": "9444",
        "YBB_EDGE_CONTROL_PORT": "9443",
        "UNRELATED_SECRET": "do-not-expose",
    }
    (install / "device.env").write_text(
        "# keep this comment\n" + "".join(f"{key}={value}\n" for key, value in environment.items())
    )
    monkeypatch.setenv("YBB_EDGE_SETTINGS_ROOT", str(install))
    state = SimpleNamespace(
        config=EdgeAgentConfig(
            address=EdgeDeviceAddress(management_ip="192.168.50.21"), data_root=data
        ),
        boot_id="boot-before",
    )
    helper = module.SettingsHelper(install, data)
    helper.save()
    firewall_calls, restarts = [], []
    monkeypatch.setattr(module, "firewall", lambda values: firewall_calls.append(dict(values)))
    monkeypatch.setattr(helper, "observe", lambda env: settings_status(state).model_dump())

    def restart() -> None:
        values = module.read_environment(install / "device.env")
        from edge_agent.settings import _FIELDS

        state.config = replace(
            state.config,
            plant_allowed_ipv4s=tuple(filter(None, values[IP_KEY].split(","))),
            **{field: int(values[key]) for key, field in _FIELDS.items()},
        )
        state.boot_id = f"boot-{len(restarts)}"
        restarts.append(state.boot_id)

    monkeypatch.setattr(helper, "restart", restart)
    monkeypatch.setattr(helper, "healthy", lambda environment: True)
    return module, helper, state, firewall_calls, restarts


def command(state: SimpleNamespace, **values: str) -> EdgeSettingsCommand:
    status = settings_status(state)
    return EdgeSettingsCommand(
        request_id="settings-request-1",
        expected_revision=status.revision,
        values={**status.values, **values},
    )


def test_apply_persist_confirm_deduplicate_and_revoke(setup_helper: tuple) -> None:
    module, helper, state, firewall_calls, restarts = setup_helper
    request = command(
        state,
        YBB_EDGE_PLANT_ALLOWED_IPS="192.168.50.41,192.168.50.40",
        YBB_EDGE_PREVIEW_JPEG_QUALITY="80",
    )
    assert queue_settings(state, request).phase == "queued"
    assert settings_status(state).values[IP_KEY] == ""  # Desired != observed before restart.
    helper.tick()
    status = settings_status(state)
    assert status.phase == "succeeded"
    assert status.values[IP_KEY] == "192.168.50.40,192.168.50.41"
    assert status.values["YBB_EDGE_PREVIEW_JPEG_QUALITY"] == "80"
    assert queue_settings(state, request).phase == "succeeded"
    helper.tick()
    assert len(restarts) == 1
    assert "do-not-expose" not in helper.status_path.read_text()
    assert "# keep this comment" in helper.env_path.read_text()
    assert module.read_environment(helper.env_path)["UNRELATED_SECRET"] == "do-not-expose"
    revoke = EdgeSettingsCommand(
        request_id="settings-request-2",
        expected_revision=status.revision,
        values={**status.values, IP_KEY: ""},
    )
    queue_settings(state, revoke)
    helper.tick()
    assert firewall_calls[-1][IP_KEY] == ""
    assert settings_status(state).values[IP_KEY] == ""


def test_missing_helper_stale_revision_busy_and_software_conflicts(setup_helper: tuple) -> None:
    _, helper, state, _, _ = setup_helper
    request = command(state)
    helper.status_path.unlink()
    with pytest.raises(ValueError, match="Einstellungsdienst"):
        queue_settings(state, request)
    helper.save()
    with pytest.raises(ValueError, match="inzwischen"):
        queue_settings(state, request.model_copy(update={"expected_revision": "0" * 64}))
    requests = state.config.data_root / "software/requests"
    requests.mkdir(parents=True)
    (requests / "pending.json").write_text("{}")
    with pytest.raises(ValueError, match="Software-Update"):
        queue_settings(state, request)
    (requests / "pending.json").unlink()
    queue_settings(state, request)
    with pytest.raises(ValueError, match="läuft bereits"):
        queue_settings(state, request.model_copy(update={"request_id": "settings-request-2"}))
    with pytest.raises(ValueError, match="andere Einstellungen"):
        queue_settings(
            state, request.model_copy(update={"values": {**request.values, IP_KEY: "1.2.3.4"}})
        )


@pytest.mark.parametrize("failure", ["firewall", "restart", "confirm"])
def test_failure_restores_values_firewall_and_reports_error(
    setup_helper: tuple, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    module, helper, state, firewall_calls, restarts = setup_helper
    queue_settings(state, command(state, YBB_EDGE_PLANT_ALLOWED_IPS="192.168.50.40"))
    owner, attr = (module, "firewall") if failure == "firewall" else (helper, failure)
    original = getattr(owner, attr)
    attempts = []

    def fail_once(*args: object) -> object:
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("injected apply failure")
        return original(*args)

    monkeypatch.setattr(owner, attr, fail_once)
    helper.tick()
    assert settings_status(state).phase == "failed"
    assert "injected apply failure" in settings_status(state).last_error
    assert settings_status(state).values[IP_KEY] == ""
    assert firewall_calls, settings_status(state).last_error
    assert firewall_calls[-1][IP_KEY] == ""
    assert not helper.journal_path.exists()


def test_interrupted_apply_recovers_from_durable_journal(setup_helper: tuple) -> None:
    _, helper, state, firewall_calls, _ = setup_helper
    helper.journal_path.write_text(json.dumps({"values": DEFAULTS}))
    helper.write_values({**DEFAULTS, IP_KEY: "192.168.50.40"})
    helper.save(request_id="settings-request-1", phase="applying")
    helper.tick()
    assert settings_status(state).phase == "failed"
    assert firewall_calls[-1][IP_KEY] == ""
    assert not helper.journal_path.exists()


def test_ufw_revokes_old_rules_and_inserts_allow_before_deny(
    setup_helper: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, _, _, _, _ = setup_helper
    # Restore the real function from a fresh helper module.
    spec = importlib.util.spec_from_file_location("settings_firewall_test", module.__file__)
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    monkeypatch.setattr(real, "WINDOWS", False)
    calls = []

    def run(args: list[str]) -> str:
        calls.append(args)
        return (
            "Status: active\n[ 1] 9443/tcp ALLOW 192.168.50.10 # control\n"
            "[ 3] 9444/tcp ALLOW 192.168.50.90 # YBB Edge plant trigger client\n"
            "[12] 9444/tcp ALLOW 192.168.50.91 # YBB Edge plant trigger client\n"
        )

    monkeypatch.setattr(real, "run", run)
    real.firewall({"YBB_EDGE_PLANT_API_PORT": "9444", IP_KEY: "192.168.50.40"})
    assert calls[1:3] == [
        ["/usr/sbin/ufw", "--force", "delete", "12"],
        ["/usr/sbin/ufw", "--force", "delete", "3"],
    ]
    assert calls[-1][1:3] == ["prepend", "allow"]
    assert "192.168.50.40" in calls[-1]
    assert all("9443/tcp" not in call for call in calls)
    calls.clear()
    real.firewall({"YBB_EDGE_PLANT_API_PORT": "9444", IP_KEY: ""})
    assert not any("allow" in call for call in calls)


def test_stale_helper_not_writable(setup_helper: tuple) -> None:
    _, helper, state, _, _ = setup_helper
    helper.status_path.write_text(
        json.dumps({"protocol_version": 1, "observed_at": time.time() - 181, "phase": "idle"})
    )
    assert not settings_status(state).writable


def test_agent_and_client_roundtrip_rejects_unknown_keys_and_conflicting_software(
    setup_helper: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cv_contracts.edge import EdgeSoftwareCommand
    from cv_edge import EdgeClientError
    from fastapi.testclient import TestClient
    from tests.integration.test_edge_agent_m0 import _client, _edge_app, _transport

    _, helper, state, _, _ = setup_helper
    monkeypatch.setenv("YBB_EDGE_SOFTWARE_LAUNCHER", "1")
    with TestClient(_edge_app(state.config.data_root)) as http, _client(_transport(http)) as client:
        observed = client.settings_status()
        assert observed.writable
        assert "UNRELATED_SECRET" not in observed.values
        invalid = {
            "request_id": "settings-http-001",
            "expected_revision": observed.revision,
            "values": {**observed.values, "YBB_EDGE_TRTEXEC_PATH": "injected"},
        }
        assert http.put("/edge/v1/settings", json=invalid).status_code == 422
        request = EdgeSettingsCommand(
            request_id="settings-http-001",
            expected_revision=observed.revision,
            values={**observed.values, IP_KEY: "192.168.50.40"},
        )
        assert client.update_settings(request).phase == "queued"
        with pytest.raises(EdgeClientError, match="Einstellungsänderung"):
            client.software_command(
                EdgeSoftwareCommand(request_id="software-http-001", package_sha256="a" * 64)
            )


def test_failed_recovery_blocks_new_changes_and_retries(
    setup_helper: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, helper, state, _, _ = setup_helper
    queue_settings(state, command(state, YBB_EDGE_PLANT_ALLOWED_IPS="192.168.50.40"))
    original = module.firewall

    def fail(_values: dict) -> None:
        raise RuntimeError("firewall unavailable")

    monkeypatch.setattr(module, "firewall", fail)
    helper.tick()
    assert helper.journal_path.exists()
    assert not settings_status(state).writable
    monkeypatch.setattr(module, "firewall", original)
    assert settings_busy(state)
    helper.tick()
    assert not helper.journal_path.exists()
    assert settings_status(state).writable


def test_windows_firewall_and_winsw_configuration(
    setup_helper: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, helper, _, _, _ = setup_helper
    monkeypatch.setattr(module, "WINDOWS", True)
    xml = helper.config_root / "ybb-edge-service.xml"
    xml.write_text(
        "<service><executable>python.exe</executable>"
        '<env name="UNRELATED_SECRET" value="keep"/>'
        '<env name="YBB_EDGE_PLANT_ALLOWED_IPS" value="192.168.50.90"/></service>'
    )
    helper.write_values({**DEFAULTS, IP_KEY: "192.168.50.40"})
    root = module.ElementTree.fromstring(xml.read_text())
    values = {node.get("name"): node.get("value") for node in root.findall("env")}
    assert values[IP_KEY] == "192.168.50.40"
    assert values["UNRELATED_SECRET"] == "keep"
    # Exercise the unmocked platform implementation without running a real firewall command.
    spec = importlib.util.spec_from_file_location("settings_windows_test", module.__file__)
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    monkeypatch.setattr(real, "WINDOWS", True)
    calls = []
    monkeypatch.setattr(real, "powershell", lambda script: calls.append(script))
    real.firewall({IP_KEY: "192.168.50.40", "YBB_EDGE_PLANT_API_PORT": "9444"})
    assert "Remove-NetFirewallRule" in calls[0]
    assert "-RemoteAddress @('192.168.50.40')" in calls[1]
    assert "9443" not in "".join(calls)


def test_plant_acl_uses_applied_ips_and_does_not_trust_forwarded_header(
    setup_helper: tuple,
) -> None:
    from edge_agent.plant import create_plant_app
    from edge_agent.state import EdgeAgentState
    from fastapi.testclient import TestClient

    _, helper, state, _, _ = setup_helper
    queue_settings(state, command(state, YBB_EDGE_PLANT_ALLOWED_IPS="192.168.50.40"))
    helper.tick()
    edge = EdgeAgentState(state.config)
    try:
        with TestClient(create_plant_app(state=edge), client=("192.168.50.40", 1234)) as client:
            assert client.get("/edge-plant/v1/health").status_code == 200
        with TestClient(create_plant_app(state=edge), client=("192.168.50.41", 1234)) as client:
            assert (
                client.get(
                    "/edge-plant/v1/health", headers={"X-Forwarded-For": "192.168.50.40"}
                ).status_code
                == 403
            )
    finally:
        edge.close()


def test_central_forwards_validated_settings_records_audit_and_handles_old_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cv_contracts.edge import EdgeSettingsStatus
    from cv_edge.client import EdgeClientError, EdgeClientErrorCode
    from cv_utils import PlatformError
    from runtime_admin_api.api import edge_routes

    calls = []
    outcome = EdgeSettingsStatus(writable=True, revision=revision(DEFAULTS), values=DEFAULTS)

    class Client:
        def __enter__(self) -> Client:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def settings_status(self) -> EdgeSettingsStatus:
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        def update_settings(self, payload: EdgeSettingsCommand) -> EdgeSettingsStatus:
            calls.append(payload)
            return self.settings_status().model_copy(update={"phase": "queued"})

    monkeypatch.setattr(edge_routes, "_settings_client", lambda *args: Client())
    audits = []
    monkeypatch.setattr(
        edge_routes, "write_runtime_audit_event", lambda **kwargs: audits.append(kwargs)
    )
    payload = EdgeSettingsCommand(
        request_id="central-settings-001", expected_revision=revision(DEFAULTS), values=DEFAULTS
    )
    assert edge_routes.put_edge_settings("edge-001", payload, None, None, None).phase == "queued"
    assert calls == [payload]
    assert audits[0]["event_type"] == "edge.settings.requested.v1"
    assert audits[0]["payload"]["request_id"] == payload.request_id
    outcome = EdgeClientError(
        EdgeClientErrorCode.REMOTE_ERROR, "Not found", retryable=False, status_code=404
    )
    assert not edge_routes.get_edge_settings("edge-001", None, None).supported
    outcome = EdgeClientError(EdgeClientErrorCode.OFFLINE, "Offline", retryable=True)
    with pytest.raises(PlatformError) as exc:
        edge_routes.get_edge_settings("edge-001", None, None)
    assert exc.value.status_code == 503


def test_inactive_firewall_is_rejected_before_mutation(
    setup_helper: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, helper, state, firewall_calls, restarts = setup_helper
    queue_settings(state, command(state, YBB_EDGE_PLANT_ALLOWED_IPS="192.168.50.40"))

    def inactive() -> str:
        raise ValueError("UFW muss aktiv sein")

    monkeypatch.setattr(module, "ufw_rules", inactive)
    helper.tick()
    assert settings_status(state).phase == "failed"
    assert not helper.journal_path.exists()
    assert not firewall_calls and not restarts
    assert settings_status(state).values[IP_KEY] == ""


@pytest.mark.parametrize("request_queued", [False, True])
@pytest.mark.parametrize("firewall_state", ["missing", "inactive"])
def test_firewall_prerequisite_is_visible_and_recovers_without_changing_values(
    setup_helper: tuple,
    monkeypatch: pytest.MonkeyPatch,
    firewall_state: str,
    request_queued: bool,
) -> None:
    module, helper, state, firewall_calls, restarts = setup_helper
    spec = importlib.util.spec_from_file_location("settings_readiness_test", module.__file__)
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    monkeypatch.setattr(module, "ufw_rules", real.ufw_rules)

    def unavailable(args: list[str]) -> str:
        assert args == ["/usr/sbin/ufw", "status", "numbered"]
        if firewall_state == "missing":
            raise FileNotFoundError(2, "No such file or directory", args[0])
        return "Status: inactive\n"

    monkeypatch.setattr(real, "run", unavailable)
    before = helper.env_path.read_bytes()
    request = command(state, YBB_EDGE_PLANT_ALLOWED_IPS="192.168.50.40")
    if request_queued:
        queue_settings(state, request)
    helper.tick()
    status = settings_status(state)
    assert not status.writable
    assert "UFW" in status.message
    if firewall_state == "missing":
        assert "nicht installiert" in status.message
        assert "Ubuntu-Version" in status.message
    if request_queued:
        assert status.phase == "failed"
        assert status.last_error == status.message
        assert not settings_busy(state)
    with pytest.raises(ValueError, match="UFW"):
        queue_settings(state, request.model_copy(update={"request_id": "settings-retry-1"}))
    assert helper.env_path.read_bytes() == before
    assert not helper.journal_path.exists()
    assert not firewall_calls and not restarts
    assert status.values[IP_KEY] == ""

    monkeypatch.setattr(real, "run", lambda args: "Status: active\n")
    helper.tick()
    assert settings_status(state).writable
    assert settings_status(state).message is None
    queue_settings(state, request.model_copy(update={"request_id": "settings-repaired-1"}))
    helper.tick()
    assert settings_status(state).phase == "succeeded"
    assert settings_status(state).values[IP_KEY] == "192.168.50.40"


def test_windows_settings_readiness_does_not_require_ufw(
    setup_helper: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, helper, state, _, _ = setup_helper
    monkeypatch.setattr(module, "WINDOWS", True)

    def unexpected() -> str:
        raise AssertionError("UFW must not be invoked on Windows")

    monkeypatch.setattr(module, "ufw_rules", unexpected)
    helper.tick()
    assert settings_status(state).writable
    assert settings_status(state).message is None


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing behavior")
def test_atomic_write_retries_a_temporary_windows_file_lock(
    setup_helper: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, helper, _, _, _ = setup_helper
    replace_file = module.os.replace
    attempts = []

    def temporarily_locked(source: Path, target: Path) -> None:
        attempts.append(True)
        if len(attempts) == 1:
            raise PermissionError("File temporarily locked")
        replace_file(source, target)

    monkeypatch.setattr(module.os, "replace", temporarily_locked)
    module.atomic_write(helper.config_root / "retry.json", b"{}")
    assert len(attempts) == 2
    assert (helper.config_root / "retry.json").read_bytes() == b"{}"
def test_privileged_system_install_blocks_settings_after_launcher_timeout(setup_helper):
    module, helper, state, _firewall_calls, _restarts = setup_helper
    (helper.config_root / "system-package-status.json").write_text('{"phase":"installing"}')
    assert settings_busy(state)
    with pytest.raises(ValueError, match="Software-Update"):
        queue_settings(state, EdgeSettingsCommand(
            request_id="settings-during-system-update",
            expected_revision=settings_status(state).revision,
            values=DEFAULTS,
        ))
def test_missing_ufw_with_system_helper_points_to_frontend_update(setup_helper, monkeypatch):
    module, _helper, _state, _firewall_calls, _restarts = setup_helper
    spec = importlib.util.spec_from_file_location("settings_system_readiness_test", module.__file__)
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    monkeypatch.setattr(real, "system_packages", SimpleNamespace())

    def unavailable(args: list[str]) -> str:
        raise FileNotFoundError(2, "No such file or directory", args[0])

    monkeypatch.setattr(real, "run", unavailable)
    with pytest.raises(ValueError, match="Runtime aktualisieren"):
        real.ufw_rules()
