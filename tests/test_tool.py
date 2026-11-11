import json
from types import SimpleNamespace

from flota_plugin import tool
from flota_plugin.wake import WakeError

SETTINGS = {"machines": {"pc": {"profile": "pc", "host": "10.0.0.2", "wake": "none"},
                         "lap": {"profile": "lap", "host": "10.0.0.3", "port": 2222},
                         "tower": {"profile": "tower", "host": "10.0.0.4", "wake": "wol",
                                   "mac": "aa:bb:cc:dd:ee:ff"}}}


class Sleeper:
    def __init__(self, running=None):
        self.running, self.slept, self.marked = running or {}, [], []

    def active(self):
        return dict(self.running)

    def sleep_now(self, machine, action=None):
        self.slept.append(machine.name if action is None else (machine.name, action))
        return f"{machine.name}: slept"

    def mark_down(self, machine):
        self.marked.append(machine.name)

    def watch(self, machine):
        self.watched = getattr(self, "watched", []) + [machine.name]


def run(args, settings=SETTINGS, sleeper=None, probe=None, ensure=None):
    calls = []

    def power(machine, action):
        calls.append((machine.name, action))
        return f"{machine.name}: {action} sent"

    out = tool.handle(args, settings=settings, probe=probe or (lambda host, port: host == "10.0.0.2"),
                      ensure=ensure or (lambda m: 4.2), power=power, sleeper=sleeper)
    return json.loads(out), calls


def test_schema():
    assert tool.SCHEMA["name"] == "flota"
    props = tool.SCHEMA["parameters"]["properties"]
    assert props["action"]["enum"] == ["status", "wake", "sleep", "off"]
    assert set(props) == {"action", "machine"}  # no force: only a human stops a busy machine
    assert tool.SCHEMA["parameters"]["required"] == ["action"]


def test_status_lists_every_machine():
    out, _ = run({"action": "status"}, sleeper=Sleeper({"pc": 2}))
    assert out == {"machines": [
        {"name": "pc", "up": True, "wake": "none", "sleep": "suspend", "can_power_off": False, "running": 2},
        {"name": "lap", "up": False, "wake": "none", "sleep": "suspend", "can_power_off": False, "running": 0},
        {"name": "tower", "up": False, "wake": "wol", "sleep": "suspend", "can_power_off": True, "running": 0}]}


def test_status_without_sleeper_reports_unknown_running():
    out, _ = run({"action": "status"})
    assert out["machines"][0]["running"] is None


def test_wake_reports_wait_and_failure():
    out, _ = run({"action": "wake", "machine": "lap"})
    assert out == {"machine": "lap", "up": True, "waited_s": 4.2}

    def fail(m):
        raise WakeError("no answer after 90s")
    out, _ = run({"action": "wake", "machine": "lap"}, ensure=fail)
    assert out == {"machine": "lap", "error": "no answer after 90s"}


def test_sleep_goes_through_the_sleeper():
    sleeper = Sleeper()
    out, calls = run({"action": "sleep", "machine": "pc"}, sleeper=sleeper)
    assert out == {"machine": "pc", "result": "pc: slept"} and sleeper.slept == ["pc"] and not calls


def test_sleep_without_sleeper_uses_power_with_machine_sleep(monkeypatch):
    out, calls = run({"action": "sleep", "machine": "pc"})
    assert calls == [("pc", "suspend")] and out["result"] == "pc: suspend sent"
    machine = SimpleNamespace(name="pc", host="h", port=22, wake="none", sleep="hibernate")
    monkeypatch.setattr(tool, "load_machines", lambda s: ({"pc": machine}, []))
    _, calls = run({"action": "sleep", "machine": "pc"})
    assert calls == [("pc", "hibernate")]


def test_busy_machine_is_refused_even_with_force():
    for action, name in (("sleep", "pc"), ("off", "tower")):
        sleeper = Sleeper({name: 1})
        for args in ({}, {"force": True}, {"force": "true"}):
            out, calls = run({"action": action, "machine": name, **args}, sleeper=sleeper)
            assert "busy" in out["error"] and f"hermes flota {action} {name} --force" in out["error"]
            assert not calls and not sleeper.slept and not sleeper.marked


def test_a_woken_machine_gets_its_idle_timer():
    sleeper = Sleeper()
    out, _ = run({"action": "wake", "machine": "pc"}, sleeper=sleeper)
    assert out["up"] is True and sleeper.watched == ["pc"]

    def boom(m):
        raise WakeError("down")
    sleeper = Sleeper()
    run({"action": "wake", "machine": "pc"}, sleeper=sleeper, ensure=boom)
    assert not hasattr(sleeper, "watched")


def test_off_needs_a_wake_method_and_the_master_switch():
    out, calls = run({"action": "off", "machine": "pc"})
    assert out["error"] == ("pc has no wake method, so Flota would not be able to turn it back on; "
                            "it can only be suspended.") and not calls
    out, calls = run({"action": "off", "machine": "tower"}, settings={**SETTINGS, "allow_power_off": False})
    assert "allow_power_off" in out["error"] and not calls
    out, calls = run({"action": "off", "machine": "tower"})
    assert calls == [("tower", "poweroff")] and out == {"machine": "tower", "result": "tower: poweroff sent"}
    out, calls = run({"action": "off", "machine": "tower"}, sleeper=Sleeper({"tower": 1}))
    assert "busy" in out["error"] and not calls


def test_status_says_whether_each_machine_can_be_powered_off():
    out, _ = run({"action": "status"})
    rows = {m["name"]: m for m in out["machines"]}
    assert rows["tower"]["can_power_off"] is True and rows["pc"]["can_power_off"] is False


def test_unknown_machine_and_action_list_valid_ones():
    out, _ = run({"action": "wake", "machine": "nas"})
    assert "pc, lap" in out["error"]
    out, _ = run({"action": "wake"})
    assert "pc, lap" in out["error"]
    out, _ = run({"action": "reboot", "machine": "pc"})
    assert "status, wake, sleep, off" in out["error"]


def test_never_raises():
    def boom(*a):
        raise RuntimeError("probe broke")
    out, _ = run({"action": "status"}, probe=boom)
    assert "probe broke" in out["error"]
    out, _ = run(None)
    assert "error" in out


def test_make_handler_reads_config_at_call_time():
    config = {"plugins": {"entries": {"flota": {"settings": {
        "machines": {"pc": {"profile": "pc", "host": "h", "wake": "command", "wake_command": "on"}}}}}}}
    calls = []
    handler = tool.make_handler(probe=lambda h, p: True, ensure=lambda m: 0.0,
                                power=lambda m, a: calls.append((m.name, a)) or "ok",
                                load_config=lambda: config)
    assert json.loads(handler({"action": "off", "machine": "pc"}, task_id="t")) == {
        "machine": "pc", "result": "ok"}
    config["plugins"]["entries"]["flota"]["settings"]["allow_power_off"] = False
    assert "error" in json.loads(handler({"action": "off", "machine": "pc"}))


def test_make_handler_reports_unreadable_config():
    def broken():
        raise OSError("config.yaml unreadable")
    handler = tool.make_handler(probe=lambda h, p: True, ensure=lambda m: 0.0,
                                power=lambda m, a: "ok", load_config=broken)
    out = json.loads(handler({"action": "status"}))
    assert "unreadable" in out["error"]


def test_available_follows_agent_tool():
    assert tool.available(load_config=lambda: {}) is True
    off = {"plugins": {"entries": {"flota": {"settings": {"agent_tool": False}}}}}
    assert tool.available(load_config=lambda: off) is False


def test_off_always_goes_through_the_sleeper():
    sleeper = Sleeper()
    for args in ({}, {"force": True}):  # the sleeper also refuses routes not started yet
        out, calls = run({"action": "off", "machine": "tower", **args}, sleeper=sleeper)
        assert not calls and not sleeper.marked
    assert sleeper.slept == [("tower", "poweroff")] * 2
