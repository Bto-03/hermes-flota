"""Audit fixes in the sleeper: thread-aware matching, the idle watch, retry, profile context."""
import contextvars
import json
import threading
from types import SimpleNamespace

import pytest

from flota_plugin.idle import IdleSleeper
from flota_plugin.machines import Machine


def machine(name="desk", sleep_after=300.0, sleep_command="ssh desk systemctl suspend"):
    return Machine(name=name, profile=name, host=name, sleep_after=sleep_after,
                   sleep_command=sleep_command)


class FakeTimer:
    def __init__(self, interval, function):
        self.interval, self.function, self.started, self.cancelled = interval, function, False, False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.function()


class Watched:
    def __init__(self, **kw):
        self.timers, self.runs, self.now = [], [], 0.0
        self.result = SimpleNamespace(returncode=0, stderr="")
        self.up, self.probes = True, []
        self.sleeper = IdleSleeper(run=self.run, timer=self.timer, clock=lambda: self.now,
                                   probe=self.probe, **kw)

    def run(self, command, **kwargs):
        self.runs.append((command, kwargs))
        return self.result

    def timer(self, interval, function):
        self.timers.append(FakeTimer(interval, function))
        return self.timers[-1]

    def child(self, m, goal, session):
        self.sleeper.routed(m, {"goal": goal})
        self.sleeper.on_start(child_goal=goal, child_session_id=session)

    def stop(self, session):
        self.sleeper.on_stop(child_session_id=session)

    def probe(self, host, port):
        self.probes.append(host)
        return self.up


@pytest.fixture
def w():
    return Watched()


def in_thread(fn):
    t = threading.Thread(target=fn)
    t.start()
    t.join()


# --- finding 2: subagent -> machine matching --------------------------------------------------

def test_same_goal_from_two_threads_lands_on_each_threads_machine(w):
    a, b = machine("a"), machine("b")
    in_thread(lambda: w.sleeper.routed(a, {"goal": "g", "index": 0}))  # another session, routed first
    w.sleeper.routed(b, {"goal": "g", "index": 0})
    w.sleeper.on_start(child_goal="g", child_session_id="mine")
    assert w.sleeper.active() == {"b": 1}


def test_a_refused_task_resent_to_another_machine_counts_there(w):
    a, b = machine("a"), machine("b")
    w.sleeper.routed(a, {"goal": "g", "index": 0})  # core refused it afterwards: no child
    w.sleeper.routed(b, {"goal": "g", "index": 0})  # the next delegate_task call of this thread
    w.sleeper.on_start(child_goal="g", child_session_id="s")
    assert w.sleeper.active() == {"b": 1}
    assert w.sleeper._busy("a") == 0
    assert [t.interval for t in w.timers] == [300.0]  # a, left awake by the refused task, is watched


def test_one_batch_keeps_its_earlier_tasks(w):
    a, b = machine("a"), machine("b")
    w.sleeper.routed(a, {"goal": "g", "index": 0})
    w.sleeper.routed(b, {"goal": "h", "index": 1})
    assert w.sleeper._busy("a") == 1 and w.sleeper._busy("b") == 1


def test_goal_alone_still_matches_from_another_thread(w):
    in_thread(lambda: w.sleeper.routed(machine("a"), {"goal": "g", "index": 0}))
    w.sleeper.on_start(child_goal="g", child_session_id="s")
    assert w.sleeper.active() == {"a": 1}


def test_a_stale_expectation_gets_the_idle_timer_when_pruned(w):
    a, b = machine("a"), machine("b")
    in_thread(lambda: w.sleeper.routed(a, {"goal": "g"}))
    w.now = 601.0
    w.sleeper.routed(b, {"goal": "h"})
    assert [t.interval for t in w.timers] == [300.0]
    w.timers[0].fire()
    assert len(w.runs) == 1


# --- finding 3: the idle watch ----------------------------------------------------------------

def test_watch_gives_an_idle_machine_its_full_sleep_after(w):
    w.sleeper.watch(machine())
    assert [t.interval for t in w.timers] == [300.0] and w.timers[0].started and w.runs == []
    w.timers[0].fire()
    assert len(w.runs) == 1


def test_watch_restarts_a_pending_timer_so_a_fresh_wake_gets_the_whole_time(w):
    w.child(machine(), "g", "s")
    w.stop("s")
    w.sleeper.watch(machine())  # the user woke it again
    assert w.timers[0].cancelled and not w.timers[1].cancelled
    w.timers[0].fire()
    assert w.runs == []


def test_watch_never_touches_a_busy_machine_or_one_without_auto_sleep(w):
    w.child(machine(), "g", "s")
    w.sleeper.watch(machine())
    w.sleeper.watch(machine("b", sleep_after=0.0))
    w.sleeper.watch(Machine(name="c", profile="c", host="c", sleep_after=300.0))  # sleep: none
    assert w.timers == []


def test_work_cancels_the_watch(w):
    w.sleeper.watch(machine())
    w.sleeper.claim(machine())
    w.timers[0].fire()
    assert w.runs == []


def test_a_delayed_watch_only_starts_the_timer_when_the_machine_answers(w):
    w.sleeper.watch(machine(), delay=90.0)
    assert [t.interval for t in w.timers] == [90.0]
    w.timers[0].fire()
    assert w.probes == ["desk"] and [t.interval for t in w.timers] == [90.0, 300.0] and w.runs == []
    w.timers[1].fire()
    assert len(w.runs) == 1


def test_a_delayed_watch_forgets_a_machine_that_is_down(w):
    """Someone turning it on later is never slept by a timer that started before."""
    w.up = False
    w.sleeper.watch(machine(), delay=90.0)
    w.timers[0].fire()
    assert len(w.timers) == 1 and w.sleeper._timers == {} and w.runs == []


def timer_machine():
    return Machine(name="desk", profile="desk", host="desk", sleep_after=300.0, sleep="suspend",
                   wake="timer", timer_lead=120.0)


def test_a_timer_machine_is_watched_from_its_alarm_on(monkeypatch):
    """Its alarm wakes it for a cron job that may never delegate to it."""
    from flota_plugin import power
    monkeypatch.setattr(power, "power", lambda m, action, run=None: f"{m.name}: suspend sent")
    w = Watched(wall=lambda: 1000.0, armed_at=lambda name: 4000.0)
    assert "sent" in w.sleeper.sleep_now(timer_machine())
    assert [t.interval for t in w.timers] == [3000.0 + 120.0]
    w.timers[0].fire()  # up at job time
    assert w.timers[1].interval == 300.0


def test_no_alarm_no_watch(monkeypatch):
    from flota_plugin import power
    monkeypatch.setattr(power, "power", lambda m, action, run=None: f"{m.name}: suspend sent")
    for armed in (None, 900.0):  # none set, or one already past
        w = Watched(wall=lambda: 1000.0, armed_at=lambda name: armed)
        w.sleeper.sleep_now(timer_machine())
        assert w.timers == []


def test_recover_watches_what_a_dead_gateway_left_busy(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"pid": 4242, "updated": 1000.0, "busy": {"desk": 2, "gone": 1}}))
    w = Watched(state_file=lambda: path, wall=lambda: 1500.0, pid=lambda: 7)
    w.sleeper.recover({"desk": machine()}.get, alive=lambda pid: False)
    assert [t.interval for t in w.timers] == [300.0]
    assert json.loads(path.read_text())["pid"] == 7  # claimed: no second process recovers it


def test_recover_leaves_a_live_gateway_alone(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"pid": 4242, "updated": 1000.0, "busy": {"desk": 2}}))
    w = Watched(state_file=lambda: path, wall=lambda: 1500.0, pid=lambda: 7)
    w.sleeper.recover({"desk": machine()}.get, alive=lambda pid: True)
    assert w.timers == [] and json.loads(path.read_text())["pid"] == 4242


def test_recover_never_raises(tmp_path):
    Watched(state_file=lambda: tmp_path / "missing.json").sleeper.recover(lambda name: 1 / 0)
    Watched().sleeper.recover(lambda name: None)


# --- finding 6: one more try after a failed auto-sleep ----------------------------------------

def test_a_failed_auto_sleep_is_tried_once_more(w):
    w.child(machine(), "g", "s")
    w.stop("s")
    w.result = SimpleNamespace(returncode=1, stderr="boom")
    w.timers[0].fire()
    assert len(w.runs) == 1 and len(w.timers) == 2 and w.timers[1].interval == 60.0
    w.timers[1].fire()  # still up: the idle time again
    assert w.timers[2].interval == 300.0
    w.timers[2].fire()
    assert len(w.runs) == 2 and len(w.timers) == 3  # failed again: no loop


# --- finding 9: the timer runs in the profile it was started in --------------------------------

def test_the_timer_runs_in_the_context_that_started_it(w):
    var = contextvars.ContextVar("home", default="process")
    seen = []
    w.sleeper._run = lambda *a, **k: seen.append(var.get()) or SimpleNamespace(returncode=0, stderr="")
    var.set("profile-x")
    w.child(machine(), "g", "s")
    w.stop("s")
    var.set("other")
    w.timers[0].fire()
    assert seen == ["profile-x"]


# --- finding 14: settings as they are when the timer fires -------------------------------------

def test_auto_sleep_turned_off_meanwhile_is_respected():
    fresh = {"desk": machine(sleep_after=0.0)}
    w = Watched(reload=fresh.get)
    w.child(machine(), "g", "s")
    w.stop("s")
    w.timers[0].fire()
    assert w.runs == [] and w.sleeper._timers == {}


def test_a_reload_that_fails_keeps_the_routed_settings():
    w = Watched(reload=lambda name: 1 / 0)
    w.child(machine(), "g", "s")
    w.stop("s")
    w.timers[0].fire()
    assert len(w.runs) == 1


def test_the_first_delegation_recovers_once(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"pid": 4242, "updated": 1000.0, "busy": {"old": 1}}))
    old, asked = machine("old"), []
    w = Watched(state_file=lambda: path, wall=lambda: 1500.0, pid=lambda: 7,
                reload=lambda name: asked.append(name) or old)
    w.sleeper.recover = lambda lookup, _real=w.sleeper.recover: _real(lookup, alive=lambda pid: False)
    assert w.timers == []  # nothing at load: a short-lived process must not take the file
    w.sleeper.routed(machine("desk"), {"goal": "g"})
    assert [t.interval for t in w.timers] == [300.0] and asked == ["old"]
    w.sleeper.routed(machine("desk"), {"goal": "h"})
    assert asked == ["old"]


def test_register_gives_the_sleeper_the_live_config(monkeypatch):
    import flota_plugin
    import hermes_cli.config as config_mod
    panel = {"machines": {"pc": {"profile": "pc", "host": "192.0.2.9", "sleep": "none"}}}
    monkeypatch.setattr(config_mod, "load_config_readonly",
                        lambda: {"plugins": {"entries": {"flota": {"settings": panel}}}})
    assert flota_plugin._machine("pc").sleep == "none" and flota_plugin._machine("ghost") is None
    seen = {}
    ctx = SimpleNamespace(register_delegation_provider=lambda p: seen.update(sleeper=p._sleeper),
                          **{f"register_{k}": lambda *a, **kw: None
                             for k in ("hook", "tool", "command", "cli_command")})
    flota_plugin.register(ctx)
    assert seen["sleeper"]._reload is flota_plugin._machine


def _registered(monkeypatch, settings):
    import flota_plugin
    import hermes_cli.config as config_mod
    monkeypatch.setattr(config_mod, "load_config_readonly",
                        lambda: {"plugins": {"entries": {"flota": {"settings": settings}}}})
    hooks, tools = {}, {}
    ctx = SimpleNamespace(register_delegation_provider=lambda p: None,
                          register_hook=lambda name, fn: hooks.setdefault(name, fn),
                          register_tool=lambda **kw: tools.update(kw),
                          register_command=lambda *a, **kw: None, register_cli_command=lambda **kw: None)
    flota_plugin.register(ctx)
    return hooks, tools


def test_register_offers_sleep_and_off_only_when_the_panel_allows_them(monkeypatch):
    hooks, tools = _registered(monkeypatch, {})
    assert set(hooks) == {"subagent_start", "subagent_stop", "pre_tool_call"}
    assert tools["schema"]["parameters"]["properties"]["action"]["enum"] == ["status", "wake"]
    hooks, tools = _registered(monkeypatch, {"agent_power_actions": True})
    assert tools["schema"]["parameters"]["properties"]["action"]["enum"] == ["status", "wake", "sleep", "off"]
    verdict = hooks["pre_tool_call"](tool_name="flota", args={"action": "off", "machine": "gpu-box"})
    assert verdict["action"] == "approve" and verdict["rule_key"] == "flota:off:gpu-box"
