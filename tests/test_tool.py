import json
from types import SimpleNamespace

from flota_plugin import tool
from flota_plugin.wake import WakeError

SETTINGS = {"machines": {"pc": {"profile": "pc", "host": "10.0.0.2", "wake": "none"},
                         "lap": {"profile": "lap", "host": "10.0.0.3", "port": 2222},
                         "tower": {"profile": "tower", "host": "10.0.0.4", "wake": "wol",
                                   "mac": "aa:bb:cc:dd:ee:ff"}}}
# Most tests below exercise sleep/off, so they run with the panel's opt-in on; the default (off) has
# its own tests at the end.
POWER = {**SETTINGS, "agent_power_actions": True}


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


def run(args, settings=POWER, sleeper=None, probe=None, ensure=None):
    calls = []

    def power(machine, action):
        calls.append((machine.name, action))
        return f"{machine.name}: {action} sent"

    out = tool.handle(args, settings=settings, probe=probe or (lambda host, port: host == "10.0.0.2"),
                      ensure=ensure or (lambda m: 4.2), power=power, sleeper=sleeper)
    return json.loads(out), calls


def test_schema():
    assert tool.SCHEMA == tool.schema(False)  # the default offers no power actions
    for power_actions, actions in ((False, ["status", "wake"]), (True, ["status", "wake", "sleep", "off"])):
        schema = tool.schema(power_actions)
        assert schema["name"] == "flota"
        props = schema["parameters"]["properties"]
        assert props["action"]["enum"] == actions
        assert set(props) == {"action", "machine"}  # no force: only a human stops a busy machine
        assert schema["parameters"]["required"] == ["action"]
    assert "sleep" not in tool.schema(False)["parameters"]["properties"]["machine"]["description"]
    assert "approval" in tool.schema(True)["description"]


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
    out, calls = run({"action": "off", "machine": "tower"}, settings={**POWER, "allow_power_off": False})
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
    config = {"plugins": {"entries": {"flota": {"settings": {"agent_power_actions": True,
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


# --- agent_power_actions off (the default) -------------------------------------------------------

def test_by_default_the_handler_refuses_sleep_and_off_and_says_how_to_enable_them():
    for settings in (SETTINGS, {**SETTINGS, "agent_power_actions": False},
                     {**SETTINGS, "agent_power_actions": "yes"}):  # only an explicit True opts in
        for action, name in (("sleep", "pc"), ("off", "tower")):
            sleeper = Sleeper()
            out, calls = run({"action": action, "machine": name}, settings=settings, sleeper=sleeper)
            assert out == {"error": tool.POWER_ACTIONS_OFF} and not calls and not sleeper.slept
    assert "agent_power_actions" in tool.POWER_ACTIONS_OFF and "Plugins > Flota" in tool.POWER_ACTIONS_OFF


def test_by_default_status_and_wake_still_work():
    out, _ = run({"action": "wake", "machine": "lap"}, settings=SETTINGS)
    assert out == {"machine": "lap", "up": True, "waited_s": 4.2}
    out, _ = run({"action": "status"}, settings=SETTINGS)
    assert [m["name"] for m in out["machines"]] == ["pc", "lap", "tower"]
    out, _ = run({"action": "reboot", "machine": "pc"}, settings=SETTINGS)
    assert out["error"] == "Unknown action 'reboot'; use one of: status, wake."


def test_the_panel_default_is_off():
    handler = tool.make_handler(probe=lambda h, p: True, ensure=lambda m: 0.0,
                                power=lambda m, a: "should not run", load_config=lambda: {
                                    "plugins": {"entries": {"flota": {"settings": SETTINGS}}}})
    assert json.loads(handler({"action": "sleep", "machine": "pc"})) == {"error": tool.POWER_ACTIONS_OFF}
    assert tool.power_actions_enabled(load_config=lambda: {}) is False

    def broken():
        raise OSError("unreadable")
    assert tool.power_actions_enabled(load_config=broken) is False


# --- pre_tool_call approval hook ----------------------------------------------------------------

def _hook(enabled=True):
    config = {"plugins": {"entries": {"flota": {"settings": {**SETTINGS, "agent_power_actions": enabled}}}}}
    return tool.make_approval_hook(load_config=lambda: config)


def test_hook_asks_for_approval_naming_the_machine_and_the_action():
    hook = _hook()
    verdict = hook(tool_name="flota", args={"action": "sleep", "machine": "desk"}, task_id="t", session_id="s")
    assert verdict["action"] == "approve"
    assert "put to sleep the machine 'desk'" in verdict["message"]
    assert "Anyone using desk right now will lose their session" in verdict["message"]
    assert verdict["rule_key"] == "flota:sleep:desk"
    verdict = hook(tool_name="flota", args={"action": "off", "machine": "gpu-box"})
    assert "power off the machine 'gpu-box'" in verdict["message"] and verdict["rule_key"] == "flota:off:gpu-box"


def test_hook_rule_keys_differ_per_machine_and_action():
    hook = _hook()
    keys = {hook(tool_name="flota", args={"action": a, "machine": m})["rule_key"]
            for a in ("sleep", "off") for m in ("desk", "gpu-box")}
    assert len(keys) == 4  # an "always" for one pair never covers another


def test_hook_ignores_other_tools_and_safe_actions():
    hook = _hook()
    assert hook(tool_name="terminal", args={"action": "sleep", "machine": "desk"}) is None
    for args in ({"action": "status"}, {"action": "wake", "machine": "desk"}, {}, None, "sleep"):
        assert hook(tool_name="flota", args=args) is None


def test_hook_blocks_power_actions_while_the_option_is_off():
    verdict = _hook(enabled=False)(tool_name="flota", args={"action": "off", "machine": "desk"})
    # Plain text: core itself wraps a block message as the tool result {"error": message}.
    assert verdict == {"action": "block", "message": tool.POWER_ACTIONS_OFF}


def test_hook_without_a_machine_still_asks_and_a_broken_config_blocks():
    verdict = _hook()(tool_name="flota", args={"action": "sleep"})
    assert verdict["action"] == "approve" and verdict["rule_key"] == "flota:sleep:(no machine given)"

    def broken():
        raise OSError("unreadable")
    verdict = tool.make_approval_hook(load_config=broken)(tool_name="flota", args={"action": "sleep", "machine": "x"})
    assert verdict["action"] == "block"  # unreadable config: option counts as off, nothing runs


def test_hook_directive_is_read_by_core_as_approve(monkeypatch):
    # Core's own parser (the installed Hermes) turns our dict into an approve directive.
    import hermes_cli.lifecycle as lifecycle
    from hermes_cli import plugins as core_plugins
    hook = _hook()
    monkeypatch.setattr(lifecycle, "invoke_hook",
                        lambda name, **kw: [hook(**kw)] if name == "pre_tool_call" else [])
    details = core_plugins._get_pre_tool_call_directive_details("flota", {"action": "off", "machine": "gpu-box"})
    assert (details.action, details.rule_key) == ("approve", "flota:off:gpu-box")
    assert "gpu-box" in (details.message or "")
