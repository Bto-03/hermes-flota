"""The Flota dashboard tab: ``dashboard/plugin_api.py`` (FastAPI router), its manifest and its bundle.

The API module is imported the way the dashboard imports it (a single file under a made-up module
name), so it has to find the plugin package by itself. The core seams (config read/write, TCP probe,
wake, power, profile lookup) are replaced with in-memory fakes, as in test_cli.py.
"""
import copy
import importlib.util
import json
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from flota_plugin import cli, idle, pairing, power, slash, wake

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
ACTIVE = Path("/fake-home/config.yaml")
MACHINES = {
    "torre": {"profile": "flota-torre", "host": "10.0.0.5", "user": "carol", "description": "GPU tower",
              "wake": "wol", "mac": "aa:bb:cc:dd:ee:ff", "sleep": "suspend", "sleep_after": "10m"},
    "mini": {"profile": "flota-mini", "host": "mini.lan", "wake": "none", "sleep": "none"},
}


NO_WAKE_MINI = "mini has no wake method, so Flota would not be able to turn it back on; it can only be suspended."


def _load_api():
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_flota_test",
                                                  DASHBOARD / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def env(monkeypatch):
    api = _load_api()
    store = {ACTIVE: {"plugins": {"entries": {"flota": {"settings": {"machines": copy.deepcopy(MACHINES)}}}}}}
    up = {"10.0.0.5": False, "mini.lan": True}
    calls = []

    def update(path, mutate):
        data = copy.deepcopy(store[path])
        mutate(data)
        store[path] = data

    monkeypatch.setattr(cli, "_active_config_path", lambda: ACTIVE)
    monkeypatch.setattr(cli, "_read_config", lambda path: copy.deepcopy(store[path]))
    monkeypatch.setattr(cli, "_update_config", update)
    monkeypatch.setattr(slash, "_load_config", lambda: copy.deepcopy(store[ACTIVE]))
    monkeypatch.setattr(slash, "_profile_exists", lambda name: name == "flota-torre")

    def probe(host, port=22, timeout=2.0):
        calls.append(("probe", host, port, timeout))
        return up.get(host, False)

    def fake_power(machine, action):
        calls.append(("power", machine.name, action))
        return f"{machine.name}: {action} sent"

    monkeypatch.setattr(wake, "is_up", probe)
    monkeypatch.setattr(power, "power", fake_power)
    busy = {"counts": None}  # what the gateway's state file says; None: no live gateway
    monkeypatch.setattr(idle, "read_busy", lambda *a, **k: busy["counts"])
    monkeypatch.setattr(api, "_profile_terminals", lambda: {})  # never the real profiles
    monkeypatch.setattr(api, "_crew_profiles", lambda: [])  # never the real bots
    monkeypatch.setattr(api, "_remote_scan", lambda machine, up: None)  # never a real SSH scan
    monkeypatch.setattr(api, "_cron_jobs", lambda include_disabled=False: [])  # never the real cron store
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/flota")

    e = SimpleNamespace(api=api, store=store, up=up, calls=calls, client=TestClient(app), busy=busy,
                        panel=lambda: store[ACTIVE]["plugins"]["entries"]["flota"]["settings"])
    yield e
    for state in list(api._wakes.values()):
        state["thread"].join(5)


def _by_name(payload):
    return {m["name"]: m for m in payload["machines"]}


# --- status --------------------------------------------------------------------------------------

def test_status_lists_machines_with_a_fast_probe(env):
    data = env.client.get("/api/plugins/flota/status").json()
    machines = _by_name(data)
    assert list(machines) == ["torre", "mini"]
    torre = machines["torre"]
    assert torre["up"] is False and torre["waking"] is False and torre["wake_error"] is None
    assert torre["host"] == "10.0.0.5" and torre["port"] == 22 and torre["user"] == "carol"
    assert torre["description"] == "GPU tower" and torre["os"] == "linux"
    assert torre["wake"] == "wol" and torre["sleep"] == "suspend" and torre["sleep_after"] == 600
    assert torre["profile"] == "flota-torre" and torre["profile_exists"] is True
    assert machines["mini"]["up"] is True and machines["mini"]["profile_exists"] is False
    assert machines["mini"]["setup_command"].startswith("hermes flota setup mini --host mini.lan")
    assert {c[3] for c in env.calls if c[0] == "probe"} == {1.5}
    assert data["allow_power_off"] is True and data["problems"] == []
    assert torre["can_power_off"] is True and torre["power_off_reason"] is None
    assert machines["mini"]["can_power_off"] is False
    assert machines["mini"]["power_off_reason"] == NO_WAKE_MINI
    assert data["busy_known"] is False  # no live gateway state file
    assert torre["busy"] is None


def test_status_shows_busy_counts_from_the_gateway(env):
    env.busy["counts"] = {"torre": 2}
    data = env.client.get("/api/plugins/flota/status").json()
    assert data["busy_known"] is True
    assert _by_name(data)["torre"]["busy"] == 2 and _by_name(data)["mini"]["busy"] == 0


@pytest.mark.parametrize("path, action", [("sleep", "suspend"), ("off", "poweroff")])
def test_a_busy_machine_is_refused_unless_forced(env, path, action):
    env.busy["counts"] = {"torre": 2}
    response = env.client.post(f"/api/plugins/flota/{path}/torre", json={})
    assert response.status_code == 409
    assert response.json()["detail"].startswith("torre is busy with 2 subagent(s)")
    assert not [c for c in env.calls if c[0] == "power"]
    response = env.client.post(f"/api/plugins/flota/{path}/torre?force=1", json={})
    assert response.status_code == 200 and ("power", "torre", action) in env.calls


def test_power_off_switch_is_checked_before_busy(env):
    env.panel()["allow_power_off"] = False
    env.busy["counts"] = {"torre": 1, "mini": 1}
    assert env.client.post("/api/plugins/flota/off/torre", json={}).status_code == 403
    env.panel()["allow_power_off"] = True
    assert env.client.post("/api/plugins/flota/off/mini", json={}).status_code == 403


def test_status_reports_config_problems(env):
    env.panel()["machines"]["broken"] = {"host": "x"}
    data = env.client.get("/api/plugins/flota/status").json()
    assert "broken" not in _by_name(data)
    assert any("machine 'broken'" in p for p in data["problems"])


def test_status_with_no_machines(env):
    env.store[ACTIVE] = {}
    data = env.client.get("/api/plugins/flota/status").json()
    assert data["machines"] == [] and data["problems"] == []


def test_status_names_the_hermes_host(env, monkeypatch):
    import platform
    import socket
    monkeypatch.setattr(socket, "gethostname", lambda: "estudio")
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    data = env.client.get("/api/plugins/flota/status").json()
    assert data["hub"] == {"name": "estudio", "os": "linux", "gateway_up": False}


def test_status_lists_the_agents_whose_terminal_is_each_machine(env, monkeypatch):
    monkeypatch.setattr(env.api, "_profile_terminals", lambda: {
        "flota-torre": {"backend": "ssh", "ssh_host": "10.0.0.5"},
        "gpu-worker": {"backend": "ssh", "ssh_host": "10.0.0.5"},
        "elsewhere": {"backend": "ssh", "ssh_host": "10.0.0.9"},
        "local-one": {"backend": "local"},
    })
    machines = _by_name(env.client.get("/api/plugins/flota/status").json())
    assert machines["torre"]["agents"] == ["flota-torre", "gpu-worker"]
    assert machines["mini"]["agents"] == ["flota-mini"]  # its own profile, even before setup


def test_status_agents_fall_back_to_the_machine_profile(env, monkeypatch):
    def broken():
        raise OSError("no profiles dir")

    monkeypatch.setattr(env.api, "_profile_terminals", broken)
    machines = _by_name(env.client.get("/api/plugins/flota/status").json())
    assert machines["torre"]["agents"] == ["flota-torre"] and machines["mini"]["agents"] == ["flota-mini"]


def test_profile_terminals_reads_each_profile_config(env, monkeypatch, tmp_path):
    import sys
    for name, text in (("flota-torre", "terminal:\n  backend: ssh\n  ssh_host: 10.0.0.5\n"),
                       ("plain", "model: x\n"), ("broken", ":\n  - [")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "config.yaml").write_text(text)
    fake = SimpleNamespace(list_profile_names=lambda: ["flota-torre", "plain", "broken", "gone"],
                           get_profile_dir=lambda name: tmp_path / name)
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", fake)
    found = _load_api()._profile_terminals()  # the real one; the fixture's copy is a fake
    assert found["flota-torre"] == {"backend": "ssh", "ssh_host": "10.0.0.5"}
    assert found["plain"] == {"backend": None, "ssh_host": None}
    assert "broken" not in found and "gone" not in found


# --- schedule ------------------------------------------------------------------------------------

JOBS = [
    {"id": "a1", "name": "Nightly backup", "prompt": "Back up /home on TORRE to the NAS.", "skills": [],
     "schedule_display": "0 2 * * *", "next_run_at": "2026-11-11T02:00:00-05:00", "enabled": True,
     "state": "scheduled", "deliver": "local"},
    {"id": "b2", "name": "Morning digest", "prompt": "Summarise my inbox.", "skills": ["email-inbox-triage"],
     "schedule_display": "every 1d", "next_run_at": "2026-11-10T20:00:00-05:00", "enabled": True,
     "state": "scheduled"},
    {"id": "c3", "name": "Render", "prompt": "Render the video on the mini-pc, not torre", "skills": [],
     "schedule_display": "30 3 * * 1", "next_run_at": None, "enabled": False, "state": "paused"},
    {"id": "d4", "name": "torretas", "prompt": "Count turrets on mini.", "skills": [],
     "schedule_display": "every 6h", "next_run_at": "2026-11-10T19:00:00Z", "enabled": True, "state": "scheduled"},
]


def test_schedule_lists_jobs_by_next_run_with_the_machine_each_one_wakes(env, monkeypatch):
    seen = []

    def fake_jobs(include_disabled=False):
        seen.append(include_disabled)
        return copy.deepcopy(JOBS)

    monkeypatch.setattr(env.api, "_cron_jobs", fake_jobs)
    data = env.client.get("/api/plugins/flota/schedule").json()
    assert seen == [True] and data["available"] is True and data["error"] is None
    jobs = data["jobs"]
    # 19:00Z (14:00-05) < 20:00-05 < 02:00-05 next day; no next run (paused) last.
    assert [j["id"] for j in jobs] == ["d4", "b2", "a1", "c3"]
    by_id = {j["id"]: j for j in jobs}
    assert by_id["a1"]["machine"] == "torre" and by_id["a1"]["machines"] == ["torre"]
    assert by_id["b2"]["machine"] is None and by_id["b2"]["machines"] == []
    assert by_id["c3"]["machines"] == ["mini", "torre"]  # 'mini-pc' names mini; order of mention
    assert by_id["d4"]["machine"] == "mini"  # 'torretas' is not torre
    a1 = by_id["a1"]
    assert a1["name"] == "Nightly backup" and a1["schedule"] == "0 2 * * *" and a1["enabled"] is True
    assert a1["next_run_at"] == "2026-11-11T02:00:00-05:00" and a1["state"] == "scheduled"
    assert by_id["c3"]["enabled"] is False and by_id["c3"]["state"] == "paused"
    assert by_id["b2"]["skills"] == ["email-inbox-triage"]
    assert "prompt" not in a1 and a1["prompt_preview"].startswith("Back up")


def test_schedule_honours_an_explicit_machine_field(env, monkeypatch):
    monkeypatch.setattr(env.api, "_cron_jobs", lambda include_disabled=False: [
        {"id": "x", "name": "job", "prompt": "on torre", "machine": "mini", "next_run_at": None},
        {"id": "y", "name": "job", "prompt": "on torre", "machine": "nope", "next_run_at": None}])
    jobs = {j["id"]: j for j in env.client.get("/api/plugins/flota/schedule").json()["jobs"]}
    assert jobs["x"]["machine"] == "mini" and jobs["y"]["machine"] == "torre"


def test_schedule_without_the_cron_module_is_empty_not_an_error(env, monkeypatch):
    def missing(include_disabled=False):
        raise ImportError("No module named 'cron'")

    monkeypatch.setattr(env.api, "_cron_jobs", missing)
    response = env.client.get("/api/plugins/flota/schedule")
    assert response.status_code == 200
    data = response.json()
    assert data["jobs"] == [] and data["available"] is False and "cron" in data["error"]


# --- wake / sleep / off --------------------------------------------------------------------------

def test_wake_runs_in_the_background_and_status_shows_it(env, monkeypatch):
    release = threading.Event()
    woken = []

    def ensure(machine):
        woken.append(machine.name)
        release.wait(5)
        return 12.0

    monkeypatch.setattr(wake, "ensure_ready", ensure)
    response = env.client.post("/api/plugins/flota/wake/torre", json={})
    assert response.status_code == 202 and response.json()["state"] == "waking"
    assert _by_name(env.client.get("/api/plugins/flota/status").json())["torre"]["waking"] is True
    # A second click while it is waking does not start another wake.
    assert env.client.post("/api/plugins/flota/wake/torre", json={}).status_code == 202
    release.set()
    env.api._wakes["torre"]["thread"].join(5)
    env.up["10.0.0.5"] = True
    torre = _by_name(env.client.get("/api/plugins/flota/status").json())["torre"]
    assert torre["waking"] is False and torre["up"] is True and torre["wake_error"] is None
    assert woken == ["torre"]


def test_wake_failure_is_shown_in_status(env, monkeypatch):
    def ensure(machine):
        raise wake.WakeError("torre did not come up within 90s of being woken")

    monkeypatch.setattr(wake, "ensure_ready", ensure)
    env.client.post("/api/plugins/flota/wake/torre", json={})
    env.api._wakes["torre"]["thread"].join(5)
    torre = _by_name(env.client.get("/api/plugins/flota/status").json())["torre"]
    assert torre["waking"] is False and "did not come up" in torre["wake_error"]


def test_unknown_machine_is_404(env):
    for path in ("wake", "sleep", "off"):
        assert env.client.post(f"/api/plugins/flota/{path}/nope", json={}).status_code == 404


def test_sleep_uses_the_machine_sleep_action(env):
    response = env.client.post("/api/plugins/flota/sleep/torre", json={})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "result": "torre: suspend sent"}
    assert ("power", "torre", "suspend") in env.calls
    # `sleep: none` only disables auto-sleep; a manual sleep still suspends.
    env.client.post("/api/plugins/flota/sleep/mini", json={})
    assert ("power", "mini", "suspend") in env.calls


def test_sleep_failure_is_not_ok(env, monkeypatch):
    monkeypatch.setattr(power, "power", lambda m, a: f"{m.name}: {a} failed: exit 1: denied")
    response = env.client.post("/api/plugins/flota/sleep/torre", json={})
    assert response.status_code == 502 and "denied" in response.json()["detail"]


def test_power_off_needs_the_panel_switch(env):
    env.panel()["allow_power_off"] = False
    response = env.client.post("/api/plugins/flota/off/torre", json={})
    assert response.status_code == 403 and "Allow power off" in response.json()["detail"]
    assert not [c for c in env.calls if c[0] == "power"]
    data = env.client.get("/api/plugins/flota/status").json()
    assert data["allow_power_off"] is False
    assert _by_name(data)["torre"]["can_power_off"] is False
    assert "Allow power off" in _by_name(data)["torre"]["power_off_reason"]
    env.panel()["allow_power_off"] = True
    assert env.client.get("/api/plugins/flota/status").json()["allow_power_off"] is True
    response = env.client.post("/api/plugins/flota/off/torre", json={})
    assert response.status_code == 200 and ("power", "torre", "poweroff") in env.calls


def test_power_off_is_refused_for_a_machine_without_a_wake_method(env):
    response = env.client.post("/api/plugins/flota/off/mini", json={})
    assert response.status_code == 403 and response.json()["detail"] == NO_WAKE_MINI
    assert not [c for c in env.calls if c[0] == "power"]


# --- add / update / delete -----------------------------------------------------------------------

def test_add_machine_saves_it_in_the_panel(env):
    body = {"name": "portatil", "host": "192.0.2.30", "user": "carol", "key": "~/.ssh/id_ed25519",
            "mac": "11:22:33:44:55:66", "broadcast": "192.0.2.255", "wake": "wol", "sleep": "suspend",
            "sleep_after": "15m", "description": "Laptop", "port": 22, "os": "linux"}
    response = env.client.post("/api/plugins/flota/machines", json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["ok"] is True and data["profile"] == "flota-portatil" and data["profile_exists"] is False
    assert data["setup_command"] == ("hermes flota setup portatil --host 192.0.2.30 --user carol "
                                     "--key ~/.ssh/id_ed25519")
    saved = env.panel()["machines"]["portatil"]
    assert saved["profile"] == "flota-portatil" and saved["host"] == "192.0.2.30"
    assert saved["mac"] == "11:22:33:44:55:66" and saved["sleep_after"] == "15m"
    assert "name" not in saved
    assert set(env.panel()["machines"]) == {"torre", "mini", "portatil"}


def test_update_keeps_keys_the_form_does_not_show(env):
    env.panel()["machines"]["torre"]["power_prefix"] = "sh -c 'x' _"
    response = env.client.post("/api/plugins/flota/machines",
                               json={"name": "torre", "host": "10.0.0.6", "mac": ""})
    # Clearing the MAC of a wol machine is invalid: nothing is written.
    assert response.status_code == 422
    assert any("valid 'mac'" in p for p in response.json()["detail"]["problems"])
    assert env.panel()["machines"]["torre"]["host"] == "10.0.0.5"
    response = env.client.post("/api/plugins/flota/machines", json={"name": "torre", "host": "10.0.0.6"})
    assert response.status_code == 200
    saved = env.panel()["machines"]["torre"]
    assert saved["host"] == "10.0.0.6" and saved["power_prefix"] == "sh -c 'x' _"
    assert saved["mac"] == "aa:bb:cc:dd:ee:ff" and saved["profile"] == "flota-torre"


@pytest.mark.parametrize("key", ["sleep_command", "wake_command", "power_prefix", "command", "profile"])
def test_add_rejects_shell_and_profile_keys(env, key):
    response = env.client.post("/api/plugins/flota/machines",
                               json={"name": "x", "host": "h", key: "rm -rf /"})
    assert response.status_code == 400 and key in response.json()["detail"]
    assert "x" not in env.panel()["machines"]


@pytest.mark.parametrize("body", [{"host": "h"}, {"name": "Bad Name", "host": "h"},
                                  {"name": "-x", "host": "h"}, {"name": "ok", "host": "h", "bogus": 1},
                                  {"name": "ok", "host": ["h"]}])
def test_add_rejects_bad_input(env, body):
    assert env.client.post("/api/plugins/flota/machines", json=body).status_code in (400, 422)
    assert "ok" not in env.panel()["machines"]


def test_add_returns_problems_without_writing(env):
    response = env.client.post("/api/plugins/flota/machines",
                               json={"name": "nuevo", "host": "h", "wake": "wol"})
    assert response.status_code == 422
    assert any("machine 'nuevo'" in p for p in response.json()["detail"]["problems"])
    assert "nuevo" not in env.panel()["machines"]


def test_first_add_carries_legacy_machines_over(env):
    env.store[ACTIVE] = {"delegation": {"backends": {"flota": {"machines": {
        "old": {"profile": "flota-old", "host": "old.lan"}}}}}}
    response = env.client.post("/api/plugins/flota/machines", json={"name": "new", "host": "new.lan"})
    assert response.status_code == 200
    assert set(env.panel()["machines"]) == {"old", "new"}


def test_delete_machine(env):
    assert env.client.delete("/api/plugins/flota/machines/mini").status_code == 200
    assert set(env.panel()["machines"]) == {"torre"}
    assert env.client.delete("/api/plugins/flota/machines/mini").status_code == 404


def test_write_refused_by_core_is_reported(env, monkeypatch):
    def refuse(path, mutate):
        raise RuntimeError("managed install: cannot change Flota settings")

    monkeypatch.setattr(cli, "_update_config", refuse)
    response = env.client.post("/api/plugins/flota/machines", json={"name": "new", "host": "h"})
    assert response.status_code == 409 and "managed" in response.json()["detail"]


# --- manifest and bundle -------------------------------------------------------------------------

def test_manifest_matches_host_discovery(tmp_path):
    data = json.loads((DASHBOARD / "manifest.json").read_text(encoding="utf-8"))
    assert data["name"] == "flota" and data["label"] == "Flota"
    assert data["tab"]["path"] == "/flota" and isinstance(data["tab"]["position"], str)
    for key in ("entry", "css", "api"):
        assert (DASHBOARD / data[key]).is_file(), key
    try:
        from hermes_cli.web_server_dashboard import _discover_dashboard_plugins
    except Exception:  # core's web stack not importable in this environment
        return
    root = tmp_path / "plugins"
    (root / "flota").mkdir(parents=True)
    shutil.copytree(DASHBOARD, root / "flota" / "dashboard")
    [entry] = _discover_dashboard_plugins([(root, "user")])
    assert entry["name"] == "flota" and entry["has_api"] is True and entry["_api_file"] == "plugin_api.py"
    assert entry["tab"] == {"path": "/flota", "position": data["tab"]["position"]}


def test_bundle_registers_and_parses():
    source = (DASHBOARD / "dist" / "index.js").read_text(encoding="utf-8")
    assert 'window.__HERMES_PLUGINS__.register("flota"' in source
    assert "SDK.fetchJSON" in source  # the host's authed fetch, never a hand-rolled token header
    assert "__HERMES_SESSION_TOKEN__" not in source
    assert "Busy — sleep anyway?" in source and "?force=1" in source  # 409 busy: confirm, then force
    assert "busy_known" in source and "cannot see subagents" not in source.replace("\n", " ")
    assert '{ force: true }' in source and '"{}"' in source  # every POST carries a JSON body (the API demands it)
    assert "can_power_off" in source and "document.hidden" in source and "loading.current" in source
    assert "lives in the Flota page of Hermes Desktop" in source
    node = shutil.which("node") or "node"
    if not Path(node).exists():
        pytest.skip("node not installed")
    done = subprocess.run([node, "--check", str(DASHBOARD / "dist" / "index.js")],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


# --- crews, one-click setup, roles and pairing --------------------------------------------------------


def test_status_has_crews_and_role(env, monkeypatch, tmp_path):
    hub = tmp_path / "aide"
    hub.mkdir()
    (hub / "profile.yaml").write_text("ui_meta:\n  hermes-bots:\n    title: Hermes\n    color: red\n")
    flota = tmp_path / "flota-torre"
    flota.mkdir()
    monkeypatch.setattr(env.api, "_crew_profiles", lambda: [("aide", hub), ("flota-torre", flota)])
    env.store[ACTIVE]["plugins"]["entries"]["flota"]["settings"]["machines"]["mini"]["crew_names"] = ["Lia"]
    env.busy["counts"] = {"mini": 1}
    data = env.client.get("/api/plugins/flota/status").json()
    assert data["role"] == "flagship"
    assert data["hub_crew"] == [{"id": "aide", "name": "Hermes", "avatar": None, "color": "red",
                                 "state": "asleep"}]
    rows = {m["name"]: m for m in data["machines"]}
    assert [(b["id"], b["state"]) for b in rows["torre"]["crew"]] == [("flota-torre", "asleep")]
    assert [(b["id"], b["name"], b["state"]) for b in rows["mini"]["crew"]] == [
        ("flota-mini", "mini", "working"), ("mini/Lia", "Lia", "awake")]
    assert rows["mini"]["agents"] == ["flota-mini"]


def test_status_merges_the_bots_scanned_on_each_ship(env, monkeypatch):
    env.store[ACTIVE]["plugins"]["entries"]["flota"]["settings"]["machines"]["mini"]["crew_names"] = ["Hermes", "Lia"]
    scans = {"mini": {"hostname": "desk-pc", "bots": [
        {"id": "aide", "name": "Hermes", "color": "red", "avatar_b64": "UE5H", "mime": "image/png",
         "gateway_alive": True, "active_agents": 0},
        {"id": "default", "name": "deckhand", "color": None, "avatar_b64": None, "mime": None,
         "gateway_alive": False, "active_agents": 0}]},
             "torre": {"hostname": "torre", "bots": [{"id": "x", "name": "X", "color": None, "avatar_b64": None,
                                                     "mime": None, "gateway_alive": True, "active_agents": 1}]}}
    seen = []

    def scan(machine, up):
        seen.append((machine.name, up))
        return scans.get(machine.name)
    monkeypatch.setattr(env.api, "_remote_scan", scan)
    rows = _by_name(env.client.get("/api/plugins/flota/status").json())
    assert sorted(seen) == [("mini", True), ("torre", False)]
    assert rows["mini"]["hostname"] == "desk-pc" and rows["torre"]["hostname"] == "torre"
    assert [(b["id"], b["name"], b["state"]) for b in rows["mini"]["crew"]] == [
        ("flota-mini", "mini", "awake"), ("mini/Lia", "Lia", "awake"), ("mini/aide", "Hermes", "awake"),
        ("mini/default", "deckhand", "asleep")]
    hermes = rows["mini"]["crew"][2]
    assert hermes["remote"] is True and hermes["machine"] == "mini" and hermes["avatar"] == "data:image/png;base64,UE5H"
    assert [(b["id"], b["state"]) for b in rows["torre"]["crew"]] == [("flota-torre", "asleep"), ("torre/x", "asleep")]


def test_status_never_waits_for_ssh(env, monkeypatch):
    """The real cache: the first /status starts a scan in the background and answers at once."""
    import threading as th
    from flota_plugin import remote_crew
    release, started = th.Event(), []

    def slow_probe(machine):
        started.append(machine.name)
        release.wait(5)
        return {"hostname": "h", "bots": [{"id": "a", "name": "A", "color": None, "avatar_b64": None, "mime": None,
                                           "gateway_alive": True, "active_agents": 0}]}
    cache = remote_crew.RemoteCrews(probe_fn=slow_probe)
    monkeypatch.setattr(env.api, "_remote_scan", lambda machine, up: cache.get(machine, up))
    rows = _by_name(env.client.get("/api/plugins/flota/status").json())
    assert [b["id"] for b in rows["mini"]["crew"]] == ["flota-mini"] and rows["mini"]["hostname"] is None
    release.set()
    for _ in range(100):
        if cache.get(env.api._mod("machines").load_machines(env.api._settings())[0]["mini"], False):
            break
        th.Event().wait(0.02)
    rows = _by_name(env.client.get("/api/plugins/flota/status").json())
    assert [b["id"] for b in rows["mini"]["crew"]] == ["flota-mini", "mini/a"] and started == ["mini"]


def test_hub_crew_skips_the_ship_hands_of_registered_machines(env, monkeypatch, tmp_path):
    meta = "ui_meta:\n  hermes-bots:\n    title: Torre\n"
    for name in ("flota-torre", "flota-lab"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "profile.yaml").write_text(meta)
    monkeypatch.setattr(env.api, "_crew_profiles", lambda: [(n, tmp_path / n) for n in ("flota-torre", "flota-lab")])
    data = env.client.get("/api/plugins/flota/status").json()
    assert [b["id"] for b in data["hub_crew"]] == ["flota-lab"]


def test_status_survives_broken_crew_discovery(env, monkeypatch):
    def boom():
        raise RuntimeError("no profiles")
    monkeypatch.setattr(env.api, "_crew_profiles", boom)
    data = env.client.get("/api/plugins/flota/status").json()
    assert data["hub_crew"] == [] and all(m["crew"] for m in data["machines"])


def test_role_endpoint(env):
    env.panel()["machines"] = {}  # no ships: a free change
    r = env.client.post("/api/plugins/flota/role", json={"role": "ship"})
    assert r.status_code == 200 and r.json() == {"ok": True, "role": "ship", "removed_ships": [],
                                                 "left_fleet": None, "removed_keys": 0}
    assert env.panel()["role"] == "ship"
    assert env.client.get("/api/plugins/flota/status").json()["role"] == "ship"
    assert env.client.post("/api/plugins/flota/role", json={"role": "admiral"}).status_code == 400


def test_add_machine_with_setup_runs_the_cli_setup(env, monkeypatch):
    seen = {}

    def fake_setup(params, emit=None):
        seen.update(vars(params))
        return 0, ["Created profile 'flota-nuevo' from the active profile's config.", "Saved machine 'nuevo'"]

    monkeypatch.setattr(cli, "run_setup", fake_setup)
    r = env.client.post("/api/plugins/flota/machines",
                        json={"name": "nuevo", "host": "10.0.0.9", "user": "carol", "setup": True})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["machine"] == "nuevo" and body["problems"] == [] and len(body["lines"]) == 2
    assert seen["name"] == "nuevo" and seen["host"] == "10.0.0.9" and seen["user"] == "carol"
    assert env.panel()["machines"]["nuevo"]["host"] == "10.0.0.9"  # saved before setup runs


def test_add_machine_setup_failure_and_timeout(env, monkeypatch):
    monkeypatch.setattr(cli, "run_setup", lambda params, emit=None: (2, ["✗ machine 'nuevo': bad", "Nothing was written."]))
    body = env.client.post("/api/plugins/flota/machines", json={"name": "nuevo", "host": "h", "setup": True}).json()
    assert body["ok"] is False and body["problems"] == ["machine 'nuevo': bad"]

    def refuse(params, emit=None):
        raise RuntimeError("managed install")
    monkeypatch.setattr(cli, "run_setup", refuse)
    body = env.client.post("/api/plugins/flota/machines", json={"name": "nuevo", "host": "h", "setup": True}).json()
    assert body["ok"] is False and body["problems"] == ["managed install"]

    import threading
    gate = threading.Event()
    monkeypatch.setattr(env.api, "SETUP_TIMEOUT", 0.05)
    monkeypatch.setattr(cli, "run_setup", lambda params, emit=None: gate.wait(5) and (0, []))
    body = env.client.post("/api/plugins/flota/machines", json={"name": "nuevo", "host": "h", "setup": True}).json()
    gate.set()
    assert body["ok"] is False and "still running" in body["problems"][0]
    assert env.client.post("/api/plugins/flota/machines", json={"name": "x", "host": "h", "setup": "yes"}).status_code == 400


def _invite_code(flagship="f"):
    return pairing.encode(pairing.INVITE_PREFIX, {"v": 1, "flagship": flagship, "user": "u", "nonce": "n" * 16,
                                                  "pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGJ1bGs", "exp": 4102444800})


def test_pairing_endpoints(env, monkeypatch):
    minted = []

    def run_invite():
        minted.append(1)
        return {"code": "FLOTA1-INVITE-x", "expires_at": "t", "flagship": "f"}
    monkeypatch.setattr(cli, "run_invite", run_invite)
    monkeypatch.setattr(cli, "_invites_store", lambda: Path("/fake-home/flota/invites.json"))
    # GET never mints: nothing yet, then the invite POST made.
    assert env.client.get("/api/plugins/flota/invite").json()["code"] is None and not minted
    assert env.client.post("/api/plugins/flota/invite", json={}).json()["code"] == "FLOTA1-INVITE-x"
    assert env.client.get("/api/plugins/flota/invite").json()["code"] == "FLOTA1-INVITE-x" and minted == [1]

    good = _invite_code()
    joined = []

    def join(code):
        joined.append(code)
        return {"ship_code": "FLOTA1-SHIP-y", "lines": ["ok"]}
    monkeypatch.setattr(cli, "run_join", join)
    # No confirmation, or the wrong one: nothing is trusted, the reply says what to confirm.
    for body in ({"code": good}, {"code": good, "confirm": "other"}, {"code": good, "confirm": True}):
        r = env.client.post("/api/plugins/flota/join", json=body)
        assert r.status_code == 409 and r.json()["needs"] == "confirm" and r.json()["flagship"] == "f"
        assert r.json()["pubkey_fingerprint"].startswith("SHA256:") and not joined
    fp = r.json()["pubkey_fingerprint"]
    assert env.client.post("/api/plugins/flota/join", json={"code": good, "confirm": "f"}).json()["ship_code"] == "FLOTA1-SHIP-y"
    assert env.client.post("/api/plugins/flota/join", json={"code": good, "confirm": fp}).status_code == 200
    assert joined == [good, good]
    r = env.client.post("/api/plugins/flota/join", json={"code": pairing.encode(pairing.INVITE_PREFIX, {
        "v": 1, "flagship": "f", "nonce": "n" * 16, "pubkey": "ssh-ed25519 AAAA", "exp": 1}), "confirm": "f"})
    assert r.status_code == 400 and "expired" in r.json()["detail"]
    assert env.client.post("/api/plugins/flota/join", json={}).status_code == 400
    assert env.client.post("/api/plugins/flota/join", json={"code": "x" * 9000}).status_code == 400

    monkeypatch.setattr(cli, "join_check", lambda: {"checks": [], "addresses": {"tailscale": None, "lan": "10.0.0.5"},
                                                     "os": "linux"})
    assert env.client.get("/api/plugins/flota/join/check").json()["addresses"]["lan"] == "10.0.0.5"

    monkeypatch.setattr(cli, "fleets", lambda: [{"flagship": "f"}])
    assert env.client.get("/api/plugins/flota/fleets").json() == {"fleets": [{"flagship": "f"}]}

    def leave(name):
        if name != "f":
            raise KeyError(name)
        return 1
    monkeypatch.setattr(cli, "leave_fleet", leave)
    assert env.client.delete("/api/plugins/flota/fleets/f").json()["removed_keys"] == 1
    assert env.client.delete("/api/plugins/flota/fleets/g").status_code == 404


def test_ships_endpoint(env, monkeypatch):
    code = pairing.encode(pairing.SHIP_PREFIX, {"v": 1, "name": "torre"})
    monkeypatch.setattr(cli, "run_add_ship", lambda c, emit=None, name=None: {
        "ok": True, "lines": ["✓"], "problems": [], "machine": "torre"})
    assert env.client.post("/api/plugins/flota/ships", json={"ship_code": code}).json()["machine"] == "torre"
    assert env.client.post("/api/plugins/flota/ships", json={"ship_code": "FLOTA1-INVITE-abc"}).status_code == 400

    def stale(c, emit=None, name=None):
        raise pairing.PairingError("already used")
    monkeypatch.setattr(cli, "run_add_ship", stale)
    r = env.client.post("/api/plugins/flota/ships", json={"ship_code": code})
    assert r.status_code == 400 and "already used" in r.json()["detail"]


def test_ships_endpoint_passes_a_name_override(env, monkeypatch):
    code = pairing.encode(pairing.SHIP_PREFIX, {"v": 1, "name": "desk-pc"})
    seen = []

    def add(c, emit=None, name=None):
        seen.append(name)
        return {"ok": True, "lines": [], "problems": [], "machine": name or "desk-pc"}
    monkeypatch.setattr(cli, "run_add_ship", add)
    assert env.client.post("/api/plugins/flota/ships",
                           json={"ship_code": code, "name": " studio "}).json()["machine"] == "studio"
    assert env.client.post("/api/plugins/flota/ships", json={"ship_code": code, "name": ""}).status_code == 200
    assert seen == ["studio", None]
    for bad in ("Bad Name", "-x", 7):
        assert env.client.post("/api/plugins/flota/ships", json={"ship_code": code, "name": bad}).status_code == 400


# --- /status is fast and never 500s; every route answers JSON ------------------------------------

def test_status_answers_fast_while_a_probe_hangs(env, monkeypatch):
    """A machine whose TCP probe hangs (asleep behind Tailscale) does not hold /status: its last
    known state is reported and the probe goes on in the background, one at a time."""
    import threading as th
    import time as tm
    release, probes = th.Event(), []

    def hanging(host, port=22, timeout=2.0):
        probes.append(host)
        if host == "10.0.0.5":
            release.wait(5)
            return True
        return True

    monkeypatch.setattr(wake, "is_up", hanging)
    monkeypatch.setattr(env.api, "STATUS_PROBE_WAIT", 0.2)
    t0 = tm.monotonic()
    rows = _by_name(env.client.get("/api/plugins/flota/status").json())
    assert tm.monotonic() - t0 < 1.5
    assert rows["torre"]["up"] is False and rows["torre"]["probing"] is True
    assert rows["mini"]["up"] is True and rows["mini"]["probing"] is False
    env.client.get("/api/plugins/flota/status")
    assert probes.count("10.0.0.5") == 1  # the hanging probe is not started twice
    release.set()
    for _ in range(100):
        if not env.api._probes.running():
            break
        th.Event().wait(0.02)
    rows = _by_name(env.client.get("/api/plugins/flota/status").json())
    assert rows["torre"]["up"] is True and rows["torre"]["probing"] is False


def test_status_never_500s(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("config exploded")
    monkeypatch.setattr(slash, "load_settings", boom)
    r = env.client.get("/api/plugins/flota/status")
    assert r.status_code == 200
    body = r.json()
    assert body["machines"] == [] and "config exploded" in body["error"]
    assert body["role"] is None or body["role"] in ("flagship", "ship")
    assert "checked_at" in body


def test_status_survives_a_broken_row(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bad refusal")
    monkeypatch.setattr(env.api._mod("settings"), "power_off_refusal", boom)
    r = env.client.get("/api/plugins/flota/status")
    assert r.status_code == 200 and r.json()["machines"] == [] and "bad refusal" in r.json()["error"]


def test_unexpected_errors_are_json(env, monkeypatch):
    def boom():
        raise ZeroDivisionError("kaboom")  # a bug, not a refusal the route maps to 4xx
    monkeypatch.setattr(cli, "fleets", boom)
    client = TestClient(env.client.app, raise_server_exceptions=False)
    r = client.get("/api/plugins/flota/fleets")
    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/json")
    assert "kaboom" in r.json()["detail"]
    monkeypatch.setattr(cli, "join_check", boom)
    r = client.get("/api/plugins/flota/join/check")
    assert r.status_code == 500 and "kaboom" in r.json()["detail"]


# --- role lifecycle ----------------------------------------------------------------------------------

def test_status_reports_role_fleet_and_ships_count(env, monkeypatch):
    data = env.client.get("/api/plugins/flota/status").json()
    assert data["role"] == "flagship" and data["fleet"] is None and data["ships_count"] == 2
    monkeypatch.setattr(cli, "fleet", lambda panel=None: {"flagship": "servidor", "joined_at": "t"})
    assert env.client.get("/api/plugins/flota/status").json()["fleet"] == {"flagship": "servidor",
                                                                           "joined_at": "t"}


def test_role_flagship_to_ship_conflict_and_force(env):
    r = env.client.post("/api/plugins/flota/role", json={"role": "ship"})
    assert r.status_code == 409
    body = r.json()
    assert body["needs"] == "remove_ships" and body["error"] and sorted(body["ships"]) == ["mini", "torre"]
    assert env.panel()["machines"]  # untouched
    r = env.client.post("/api/plugins/flota/role", json={"role": "ship", "force": True})
    assert r.status_code == 200 and r.json()["role"] == "ship"
    assert sorted(r.json()["removed_ships"]) == ["mini", "torre"]
    data = env.client.get("/api/plugins/flota/status").json()
    assert data["role"] == "ship" and data["ships_count"] == 0 and data["machines"] == []


def test_role_ship_to_flagship_conflict_and_force(env, monkeypatch):
    left = []
    monkeypatch.setattr(cli, "fleet", lambda panel=None: None if left else {"flagship": "servidor", "joined_at": "t"})

    def leave(flagship=None):
        left.append(flagship)
        return 1
    monkeypatch.setattr(cli, "leave_fleet", leave)
    env.panel()["machines"] = {}
    r = env.client.post("/api/plugins/flota/role", json={"role": "flagship"})
    assert r.status_code == 409 and r.json()["needs"] == "leave_fleet"
    assert r.json()["fleet"]["flagship"] == "servidor" and left == []
    r = env.client.post("/api/plugins/flota/role", json={"role": "flagship", "force": True})
    assert r.status_code == 200 and r.json()["role"] == "flagship" and left == ["servidor"]


def test_role_null_clears_the_choice_and_bad_input(env):
    env.panel()["role"] = "flagship"
    r = env.client.post("/api/plugins/flota/role", json={"role": None})
    assert r.status_code == 200 and r.json()["role"] is None and "role" not in env.panel()
    assert env.client.post("/api/plugins/flota/role", json={"role": "admiral"}).status_code == 400
    assert env.client.post("/api/plugins/flota/role", json={"role": "ship", "force": "yes"}).status_code == 400


def test_join_when_already_in_a_fleet_is_409(env, monkeypatch):
    def join(code):
        raise cli.AlreadyInFleet({"flagship": "servidor", "joined_at": "t"})
    monkeypatch.setattr(cli, "run_join", join)
    r = env.client.post("/api/plugins/flota/join", json={"code": _invite_code("otro"), "confirm": "otro"})
    assert r.status_code == 409
    assert r.json() == {"error": "already in a fleet", "fleet": {"flagship": "servidor", "joined_at": "t"},
                        "detail": r.json()["detail"]}


def test_leave_fleet_endpoint_clears_the_role(env, monkeypatch):
    seen = []

    def leave(flagship=None):
        seen.append(flagship)
        return 1
    monkeypatch.setattr(cli, "leave_fleet", leave)
    r = env.client.delete("/api/plugins/flota/fleets/servidor")
    assert r.status_code == 200 and r.json()["role"] is None and seen == ["servidor"]


def test_join_check_passes_wake_options_through(env, monkeypatch):
    monkeypatch.setattr(cli, "join_check", lambda: {"checks": [], "addresses": {}, "os": "linux",
                                                     "wake_options": [{"id": "none", "ok": True, "label": "x"}]})
    assert env.client.get("/api/plugins/flota/join/check").json()["wake_options"][0]["id"] == "none"


# --- hardening: no cross-site "simple" request changes anything ----------------------------------

@pytest.mark.parametrize("path", ["wake/torre", "sleep/torre", "off/torre", "sleep/torre?force=1", "invite", "role",
                                  "machines", "join", "ships"])
def test_posts_need_a_json_content_type(env, path):
    """A form or text/plain POST (what another site can send without a preflight) is refused."""
    url = f"/api/plugins/flota/{path}"
    assert env.client.post(url).status_code == 415
    assert env.client.post(url, content="{}", headers={"content-type": "text/plain"}).status_code == 415
    assert env.client.post(url, data={"force": "1"}).status_code == 415
    assert env.calls == []


def test_force_travels_in_the_json_body(env):
    env.busy["counts"] = {"torre": 1}
    assert env.client.post("/api/plugins/flota/sleep/torre", json={"force": False}).status_code == 409
    assert env.client.post("/api/plugins/flota/sleep/torre", json={"force": True}).status_code == 200
    assert env.client.post("/api/plugins/flota/sleep/torre", json={"force": "yes"}).status_code == 400


def test_status_sanitises_what_ships_say_about_their_bots(env, monkeypatch):
    api = env.api
    bots = [{"id": "torre/evil", "name": "N" * 500, "color": "url(//evil.example/b.png)",
             "avatar": "https://evil.example/a.png", "state": "awake", "remote": True}] + [
        {"id": f"torre/b{i}", "name": f"b{i}", "color": "#ff8800", "avatar": "data:image/png;base64,UE5H",
         "state": "awake", "remote": True} for i in range(200)]
    remote = api._mod("remote_crew")
    monkeypatch.setattr(remote, "crew_entries", lambda machine, scan, up: bots if machine.name == "torre" else [])
    crew = _by_name(env.client.get("/api/plugins/flota/status").json())["torre"]["crew"]
    assert len(crew) <= api._mod("crew").MAX_CREW
    evil = [b for b in crew if b["id"] == "torre/evil"][0]
    assert evil["color"] is None and evil["avatar"] is None and len(evil["name"]) == 64
    ok = [b for b in crew if b["id"] == "torre/b0"][0]
    assert ok["color"] == "#ff8800" and ok["avatar"] == "data:image/png;base64,UE5H"


def test_setup_without_wake_lets_setup_detect_it_and_reports_what_it_found(env, monkeypatch):
    seen = {}

    def fake_setup(params, emit=None):
        seen["wake"] = params.wake
        return 0, ["✓ done"]
    monkeypatch.setattr(cli, "run_setup", fake_setup)
    r = env.client.post("/api/plugins/flota/machines", json={"name": "nueva", "host": "10.0.0.9", "user": "u", "setup": True})
    assert r.status_code == 200 and seen["wake"] is None
    assert r.json()["wake"] == "none" and "mac" in r.json()


def test_ships_endpoint_passes_replace_only_when_true(env, monkeypatch):
    code = pairing.encode(pairing.SHIP_PREFIX, {"v": 1, "name": "torre"})
    seen = []

    def add(c, emit=None, name=None, replace=False):
        seen.append(replace)
        if not replace:
            raise pairing.PairingError("saved as a@b. run it again with --replace (hermes flota add-ship <code> --replace)")
        return {"ok": True, "lines": [], "problems": [], "machine": "torre"}
    monkeypatch.setattr(cli, "run_add_ship", add)
    url = "/api/plugins/flota/ships"
    r = env.client.post(url, json={"ship_code": code})
    assert r.status_code == 400 and "--replace" in r.json()["detail"]
    assert env.client.post(url, json={"ship_code": code, "replace": "yes"}).status_code == 400
    assert env.client.post(url, json={"ship_code": code, "replace": True}).json()["ok"] is True
    assert seen == [False, True]
