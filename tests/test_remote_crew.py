"""remote_crew.py: the bots living on a fleet machine, found with one SSH call, cached off-thread."""
import base64
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from flota_plugin import remote_crew
from flota_plugin.machines import Machine

M = Machine(name="studio", profile="flota-studio", host="10.0.0.9", user="alice")
PNG = base64.b64encode(b"\x89PNG tiny").decode()


def _out(bots, hostname="desk-pc"):
    return json.dumps({"hostname": hostname, "bots": bots})


def _run(stdout="", returncode=0, calls=None):
    def run(argv, **kw):
        if calls is not None:
            calls.append((argv, kw))
        return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)
    return run


def test_scan_is_one_batchmode_ssh_call_with_the_script_on_stdin():
    calls = []
    bots = remote_crew.scan(M, run=_run(_out([{"id": "aide", "name": "Hermes", "color": "hsl(270 68% 58%)",
                                                  "avatar_b64": PNG, "mime": "image/png", "gateway_alive": True,
                                                  "active_agents": 2}]), calls=calls))
    assert len(calls) == 1
    argv, kw = calls[0]
    assert argv[:3] == ["ssh", "-o", "BatchMode=yes"] and argv[-2:] == ["alice@10.0.0.9", "sh -s"]
    assert kw["input"] == remote_crew.SCRIPT and kw["timeout"] == 8
    assert bots == [{"id": "aide", "name": "Hermes", "color": "hsl(270 68% 58%)", "avatar_b64": PNG,
                     "mime": "image/png", "gateway_alive": True, "active_agents": 2}]


@pytest.mark.parametrize("stdout", ["", "garbage", "{\"bots\": 3}", "[1, 2]", "null", "{\"hostname\": 5}",
                                    "Welcome!\n{not json"])
def test_garbage_output_is_no_bots(stdout):
    assert remote_crew.scan(M, run=_run(stdout)) == []


def test_ssh_failure_and_timeout_are_no_bots():
    assert remote_crew.scan(M, run=_run(_out([{"id": "a", "name": "A"}]), returncode=255)) == []

    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])
    assert remote_crew.probe(M, run=slow) == {"hostname": None, "bots": []}

    def missing(argv, **kw):
        raise FileNotFoundError("ssh")
    assert remote_crew.scan(M, run=missing) == []


def test_parse_drops_bad_fields_big_avatars_and_plumbing():
    big = base64.b64encode(b"x" * (remote_crew.AVATAR_MAX + 1)).decode()
    data = remote_crew.parse(_out([
        {"id": "default", "name": None, "gateway_alive": "yes", "active_agents": True},
        {"id": "big", "name": "Big<script>", "avatar_b64": big, "mime": "image/png", "color": "red;}x{"},
        {"id": "weird", "avatar_b64": "!!!", "mime": "image/png"},
        {"id": "svg", "avatar_b64": PNG, "mime": "image/svg+xml"},
        {"id": "flota-dev", "name": ""},  # plumbing
        {"id": "flota-lab", "name": "Lab"},  # titled: a bot
        {"id": "../etc", "name": "x"}, {"id": "default", "name": "dup"}, "nope"]))
    assert data["hostname"] == "desk-pc"
    by = {b["id"]: b for b in data["bots"]}
    assert list(by) == ["default", "big", "weird", "svg", "flota-lab"]
    assert by["default"] == {"id": "default", "name": "Hermes", "color": None, "avatar_b64": None, "mime": None,
                             "gateway_alive": False, "active_agents": 0}
    assert by["big"]["name"] == "Bigscript" and by["big"]["avatar_b64"] is None and by["big"]["color"] is None
    assert by["weird"]["avatar_b64"] is None and by["svg"]["avatar_b64"] is None


def test_parse_caps_the_bots_and_rejects_odd_hostnames():
    data = remote_crew.parse(_out([{"id": f"b{i}", "name": f"B{i}"} for i in range(50)], hostname="a b;rm"))
    assert len(data["bots"]) == remote_crew.MAX_BOTS and data["hostname"] is None


def test_remote_script_reads_no_secrets():
    for script in (remote_crew.SCRIPT, remote_crew._PY, remote_crew._SH_FALLBACK):
        assert ".env" not in script and "config.yaml" not in script and "auth" not in script.lower()


# --- the script itself, run on a fake HERMES_HOME --------------------------------------------------------

def _home(tmp_path):
    root = tmp_path / "hermes"
    (root / "assets").mkdir(parents=True)
    (root / "profile.yaml").write_text("ui_meta:\n  hermes-bots:\n    title: deckhand\n")
    (root / "SOUL.md").write_text("x")
    (root / "assets" / "avatar.png").write_bytes(b"PNG")
    (root / ".env").write_text("SECRET=1")
    (root / "gateway.lock").write_text(json.dumps({"pid": os.getpid()}))
    (root / "gateway_state.json").write_text(json.dumps({"pid": os.getpid(), "active_agents": 3}))
    p = root / "profiles"
    for name, meta in (("aide", "description: x  # c\nui_meta:\n  hermes-bots:\n    color: 'hsl(270 68% 58%)'\n"
                                     "    title: Hermes\n"),
                       ("writer", "display_name: Escritor\n"), ("flota-dev", None), ("flota-lab", "display_name: Lab\n"),
                       ("bare", None), ("big", "display_name: Big\n")):
        (p / name / "assets").mkdir(parents=True)
        if meta:
            (p / name / "profile.yaml").write_text(meta)
    (p / "flota-dev" / "SOUL.md").write_text("x")
    (p / "flota-dev" / "assets" / "avatar.png").write_bytes(b"PNG")
    (p / "aide" / "gateway.lock").write_text(json.dumps({"pid": 999999999}))
    (p / "big" / "assets" / "avatar.webp").write_bytes(b"x" * (remote_crew.AVATAR_MAX + 1))
    return root


@pytest.mark.parametrize("which", ["python", "sh"])
def test_the_remote_script_lists_the_bots_of_a_hermes_home(tmp_path, which):
    root = _home(tmp_path)
    script = remote_crew.SCRIPT if which == "python" else remote_crew._SH_FALLBACK
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "HERMES_HOME": str(root / "profiles" / "aide")}
    done = subprocess.run(["sh", "-s"], input=script, capture_output=True, text=True, timeout=30, env=env)
    assert done.returncode == 0, done.stderr
    data = remote_crew.parse(done.stdout)
    by = {b["id"]: b for b in data["bots"]}
    assert list(by) == ["default", "aide", "big", "flota-lab", "writer"]
    assert by["default"]["name"] == "deckhand" and by["default"]["gateway_alive"] is True
    assert by["default"]["active_agents"] == 3
    assert by["aide"]["name"] == "Hermes" and by["aide"]["color"] == "hsl(270 68% 58%)"
    assert by["aide"]["gateway_alive"] is False
    assert by["writer"]["name"] == "Escritor" and by["big"]["avatar_b64"] is None
    if which == "python":
        assert by["default"]["avatar_b64"] == base64.b64encode(b"PNG").decode() and by["default"]["mime"] == "image/png"
    assert data["hostname"]
    assert "SECRET" not in done.stdout


# --- cache --------------------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _cache(results, clock, spawned=None):
    calls = []

    def probe_fn(machine):
        calls.append(machine.name)
        return results.pop(0)

    def spawn(fn):
        if spawned is None:
            fn()
        else:
            spawned.append(fn)
    return remote_crew.RemoteCrews(probe_fn=probe_fn, clock=clock, spawn=spawn), calls


def test_first_get_returns_nothing_and_refreshes_in_the_background():
    clock, spawned = Clock(), []
    one = {"hostname": "h", "bots": [{"id": "a"}]}
    cache, calls = _cache([one], clock, spawned)
    assert cache.get(M, True) is None and len(spawned) == 1 and calls == []
    assert cache.get(M, True) is None and len(spawned) == 1  # one refresh in flight
    spawned.pop()()
    assert cache.get(M, True) == one and calls == ["studio"]


def test_cache_lasts_120s_then_refreshes():
    clock = Clock()
    one, two = {"hostname": "h", "bots": [{"id": "a"}]}, {"hostname": "h", "bots": [{"id": "b"}]}
    cache, calls = _cache([one, two], clock)
    assert cache.get(M, True) == one
    clock.now += 119
    assert cache.get(M, True) == one and calls == ["studio"]
    clock.now += 2
    assert cache.get(M, True) == two and len(calls) == 2


def test_a_down_machine_is_not_scanned_and_keeps_its_last_bots():
    clock = Clock()
    one = {"hostname": "h", "bots": [{"id": "a"}]}
    cache, calls = _cache([one], clock)
    assert cache.get(M, False) is None and calls == []
    assert cache.get(M, True) == one
    clock.now += 500
    assert cache.get(M, False) == one and len(calls) == 1


def test_a_failed_scan_keeps_the_last_good_bots():
    clock = Clock()
    one = {"hostname": "h", "bots": [{"id": "a"}]}
    cache, calls = _cache([one, {"hostname": None, "bots": []}], clock)
    cache.get(M, True)
    clock.now += 200
    assert cache.get(M, True) == one and len(calls) == 2


def test_windows_machines_are_not_scanned():
    cache, calls = _cache([], Clock())
    win = Machine(name="w", profile="flota-w", host="w", os="windows")
    assert cache.get(win, True) is None and calls == []


# --- crew entries ----------------------------------------------------------------------------------------

BOTS = {"hostname": "desk-pc", "bots": [
    {"id": "aide", "name": "Hermes", "color": "red", "avatar_b64": PNG, "mime": "image/png",
     "gateway_alive": True, "active_agents": 0},
    {"id": "default", "name": "deckhand", "color": None, "avatar_b64": None, "mime": None,
     "gateway_alive": True, "active_agents": 1},
    {"id": "writer", "name": "Escritor", "color": None, "avatar_b64": None, "mime": None,
     "gateway_alive": False, "active_agents": 0}]}


def test_crew_entries_states_and_avatar():
    rows = remote_crew.crew_entries(M, BOTS, up=True)
    assert [(r["id"], r["state"]) for r in rows] == [("studio/aide", "awake"), ("studio/default", "working"),
                                                     ("studio/writer", "asleep")]
    assert rows[0] == {"id": "studio/aide", "profile": "aide", "name": "Hermes",
                       "avatar": f"data:image/png;base64,{PNG}", "color": "red", "state": "awake", "remote": True,
                       "machine": "studio", "hostname": "desk-pc"}
    assert {r["state"] for r in remote_crew.crew_entries(M, BOTS, up=False)} == {"asleep"}
    assert remote_crew.crew_entries(M, None, up=True) == []


def test_merge_keeps_the_ship_hand_and_replaces_matching_placeholders():
    existing = [{"id": "flota-studio", "name": "studio", "ship_hand": True},
                {"id": "studio/Hermes", "name": "Hermes", "ship_hand": False},
                {"id": "studio/Lia", "name": "Lia", "ship_hand": False}]
    merged = remote_crew.merge(existing, remote_crew.crew_entries(M, BOTS, up=True) * 2)
    assert [b["id"] for b in merged] == ["flota-studio", "studio/Lia", "studio/aide",
                                         "studio/default", "studio/writer"]


def test_a_ship_cannot_send_a_colour_that_fetches_a_url():
    ok = remote_crew._bot({"id": "chef", "name": "Chef", "color": "hsl(20 80% 50%)"})
    bad = remote_crew._bot({"id": "chef", "name": "Chef", "color": "url(//evil.example/x)"})
    assert ok["color"] == "hsl(20 80% 50%)" and bad["color"] is None
