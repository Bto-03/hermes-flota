import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from flota_plugin import idle, power
from flota_plugin.idle import IdleSleeper
from flota_plugin.machines import Machine


def machine(name="desk", sleep_after=300.0, sleep_command="ssh desk systemctl suspend"):
    return Machine(name=name, profile=name, host=name, sleep_after=sleep_after,
                   sleep_command=sleep_command)


def power_machine(sleep="suspend", sleep_after=300.0):
    """A machine slept by Flota itself over ssh, no sleep_command."""
    return Machine(name="desk", profile="desk", host="desk", sleep_after=sleep_after, sleep=sleep)


class FakeTimer:
    def __init__(self, interval, function):
        self.interval, self.function = interval, function
        self.daemon = self.started = self.daemon_at_start = self.cancelled = False

    def start(self):
        self.started, self.daemon_at_start = True, self.daemon

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.function()


class Harness:
    def __init__(self):
        self.timers, self.runs, self.now = [], [], 0.0
        self.result = SimpleNamespace(returncode=0, stderr="")
        self.sleeper = IdleSleeper(run=self.run, timer=self.timer, clock=lambda: self.now)

    def run(self, command, **kwargs):
        self.runs.append((command, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def timer(self, interval, function):
        self.timers.append(FakeTimer(interval, function))
        return self.timers[-1]

    def child(self, m, goal, session):
        """Route a task and start its child."""
        self.sleeper.routed(m, {"goal": goal})
        self.sleeper.on_start(child_goal=goal, child_session_id=session)

    def stop(self, session):
        self.sleeper.on_stop(child_session_id=session)


@pytest.fixture
def h():
    return Harness()


RUN_KWARGS = {"shell": True, "timeout": 30, "capture_output": True, "text": True}
SSH_KWARGS = {"timeout": 30, "capture_output": True, "text": True}


@pytest.mark.parametrize("sleep", ["suspend", "hibernate", "poweroff"])
def test_auto_sleep_runs_the_configured_action_over_ssh(h, sleep):
    h.child(power_machine(sleep), "build", "s1")
    h.stop("s1")
    h.timers[0].fire()
    assert h.runs == [(power.ssh_argv(power_machine(sleep), f"systemctl {sleep}"), SSH_KWARGS)]


def test_auto_sleep_logs_the_sent_line(h, caplog):
    h.child(power_machine(), "build", "s1")
    h.stop("s1")
    with caplog.at_level("INFO"):
        h.timers[0].fire()
    assert "desk: suspend sent" in caplog.text


def test_last_child_stopping_starts_a_daemon_timer_that_runs_the_sleep_command(h):
    h.child(machine(), "build", "s1")
    h.stop("s1")

    [timer] = h.timers
    assert (timer.interval, timer.started, timer.daemon_at_start) == (300.0, True, True)
    assert h.runs == []
    timer.fire()
    assert h.runs == [("ssh desk systemctl suspend", RUN_KWARGS)]


def test_no_timer_while_another_child_is_still_running(h):
    h.child(machine(), "build", "s1")
    h.child(machine(), "test", "s2")
    h.stop("s1")
    assert h.timers == []
    h.stop("s2")
    assert len(h.timers) == 1


def test_no_timer_while_a_routed_child_has_not_started_yet(h):
    h.child(machine(), "build", "s1")
    h.sleeper.routed(machine(), {"goal": "test"})
    h.stop("s1")
    assert h.timers == []


def test_routed_cancels_the_pending_timer(h):
    h.child(machine(), "build", "s1")
    h.stop("s1")
    h.sleeper.routed(machine(), {"goal": "more"})
    assert h.timers[0].cancelled


def test_timer_that_fires_as_a_new_route_arrives_does_not_sleep(h):
    h.child(machine(), "build", "s1")
    h.stop("s1")
    h.sleeper.routed(machine(), {"goal": "more"})
    h.timers[0].fire()  # cancel() lost the race: the function was already running
    assert h.runs == []


def test_superseded_timer_does_not_sleep_after_the_next_child_stopped(h):
    h.child(machine(), "build", "s1")
    h.stop("s1")
    h.child(machine(), "more", "s2")
    h.stop("s2")
    h.timers[0].fire()
    assert h.runs == []
    h.timers[1].fire()
    assert len(h.runs) == 1


@pytest.mark.parametrize("m", [machine(sleep_after=0.0), machine(sleep_command=""),
                               power_machine(sleep="none"), power_machine(sleep_after=0.0)])
def test_machine_without_auto_sleep_never_schedules(h, m):
    h.child(m, "build", "s1")
    h.stop("s1")
    assert h.timers == []
    assert h.sleeper.active() == {}


def test_unknown_goal_and_unknown_session_are_ignored(h):
    h.sleeper.routed(machine(), {"goal": "build"})
    h.sleeper.on_start(child_goal="not routed by flota", child_session_id="x")
    assert h.sleeper.active() == {}
    h.stop("x")
    h.stop("never seen")
    assert h.timers == []


def test_duplicate_goals_map_to_machines_in_routing_order(h):
    desk, attic = machine("desk"), machine("attic")
    h.sleeper.routed(desk, {"goal": "same"})
    h.sleeper.routed(attic, {"goal": "same"})
    h.sleeper.on_start(child_goal="same", child_session_id="s1")
    assert h.sleeper.active() == {"desk": 1}
    h.sleeper.on_start(child_goal="same", child_session_id="s2")
    assert h.sleeper.active() == {"desk": 1, "attic": 1}


def test_two_machines_are_independent(h):
    desk, attic = machine("desk"), machine("attic", sleep_after=60.0, sleep_command="zz")
    h.child(desk, "a", "s1")
    h.child(attic, "b", "s2")
    h.stop("s2")
    [timer] = h.timers
    assert timer.interval == 60.0
    h.sleeper.routed(desk, {"goal": "c"})
    assert not timer.cancelled
    timer.fire()
    assert [command for command, _ in h.runs] == ["zz"]


@pytest.mark.parametrize("result", [
    SimpleNamespace(returncode=255, stderr="no route to host\n"),
    subprocess.TimeoutExpired("zz", 30),
    OSError("no shell"),
])
def test_failed_sleep_command_is_logged_and_swallowed(h, caplog, result):
    h.result = result
    h.child(machine(), "build", "s1")
    h.stop("s1")
    with caplog.at_level("WARNING"):
        h.timers[0].fire()
    assert "desk: suspend failed" in caplog.text
    assert caplog.records[-1].levelname == "WARNING"


def test_hooks_do_not_raise_on_missing_kwargs(h):
    h.sleeper.routed(machine(), {})
    h.sleeper.on_start()
    h.sleeper.on_stop()
    h.sleeper.on_start(child_goal=["unhashable"], child_session_id={})
    h.sleeper.on_stop(child_session_id={})


def test_on_stop_does_not_raise_when_the_timer_cannot_start(h):
    def broken_timer(interval, function):
        raise RuntimeError("can't start new thread")

    h.sleeper = IdleSleeper(run=h.run, timer=broken_timer)
    h.child(machine(), "build", "s1")
    h.stop("s1")


def test_stale_expectation_is_dropped_so_it_cannot_claim_a_later_child(h):
    desk, attic = machine("desk"), machine("attic")
    h.sleeper.routed(desk, {"goal": "same"})  # this child never starts
    h.now = 601.0
    h.child(attic, "same", "s1")
    assert h.sleeper.active() == {"attic": 1}


def test_expectation_within_ten_minutes_is_kept(h):
    desk, attic = machine("desk"), machine("attic")
    h.sleeper.routed(desk, {"goal": "same"})
    h.now = 599.0
    h.child(attic, "same", "s1")
    assert h.sleeper.active() == {"desk": 1}


def test_active_counts_running_children_per_machine(h):
    desk, attic = machine("desk"), machine("attic")
    h.child(desk, "a", "s1")
    h.child(desk, "b", "s2")
    h.child(attic, "c", "s3")
    h.sleeper.routed(attic, {"goal": "not started"})
    assert h.sleeper.active() == {"desk": 2, "attic": 1}
    h.stop("s3")
    assert h.sleeper.active() == {"desk": 2}


def test_sleep_now_sends_the_command(h):
    assert h.sleeper.sleep_now(machine()) == "desk: suspend sent"
    assert h.runs == [("ssh desk systemctl suspend", RUN_KWARGS)]


def test_sleep_now_without_sleep_command_suspends_over_ssh(h):
    assert h.sleeper.sleep_now(machine(sleep_command="")) == "desk: suspend sent"
    assert h.runs == [(power.ssh_argv(machine(), "systemctl suspend"), SSH_KWARGS)]


def test_sleep_now_uses_the_configured_action(h):
    assert h.sleeper.sleep_now(power_machine(sleep="hibernate")) == "desk: hibernate sent"
    assert h.runs[0][0][-1] == "systemctl hibernate"


def test_sleep_now_explicit_action_wins(h):
    assert h.sleeper.sleep_now(power_machine(sleep="hibernate"), "poweroff") == "desk: power off sent"
    assert h.runs[0][0][-1] == "systemctl poweroff"


@pytest.mark.parametrize("result, reason", [
    (SimpleNamespace(returncode=255, stderr="no route to host\n"), "exit 255: no route to host"),
    (subprocess.TimeoutExpired("zz", 30), "timed out after 30s"),
    (OSError("no shell"), "no shell"),
])
def test_sleep_now_reports_failure(h, result, reason):
    h.result = result
    assert h.sleeper.sleep_now(machine()) == f"desk: suspend failed: {reason}"


def test_sleep_now_refuses_a_busy_machine(h):
    h.child(machine(), "a", "s1")
    h.sleeper.routed(machine(), {"goal": "b"})
    assert h.sleeper.sleep_now(machine()) == "desk: busy with 2 subagent(s), not put to sleep"
    assert h.runs == []


def test_sleep_now_cancels_the_pending_timer(h):
    h.child(machine(), "build", "s1")
    h.stop("s1")
    h.sleeper.sleep_now(machine())
    assert h.timers[0].cancelled
    h.timers[0].fire()
    assert len(h.runs) == 1


def test_stale_expectation_does_not_block_a_manual_sleep(h):
    # Core can refuse a task after route_task (e.g. its profile is missing): no child ever starts.
    desk = power_machine()
    h.sleeper.routed(desk, {"goal": "never starts"})
    h.now = 601.0
    assert h.sleeper.sleep_now(desk) == "desk: suspend sent"


# Race with routing (review #3): the provider claims a machine before waking it.

def test_timer_that_fires_after_a_claim_does_not_sleep(h):
    m = machine()
    h.child(m, "build", "s1")
    h.stop("s1")
    assert h.sleeper.claim(m) is False
    h.timers[0].fire()  # fired just as the claim landed: cancel() came too late
    assert h.runs == []


def test_claim_blocks_sleep_until_released(h):
    m = machine()
    h.sleeper.claim(m)
    assert "busy" in h.sleeper.sleep_now(m) and h.runs == []
    h.sleeper.release(m)
    h.sleeper.sleep_now(m)
    assert len(h.runs) == 1


def test_claim_right_after_auto_sleep_reports_going_down_once(h):
    m = machine()
    h.child(m, "build", "s1")
    h.stop("s1")
    h.timers[0].fire()
    h.now += 3
    assert h.sleeper.claim(m) is True
    h.sleeper.release(m)
    assert h.sleeper.claim(m) is False  # the first claimer already waits for it to go down


def test_claim_long_after_a_sleep_is_normal(h):
    m = machine()
    h.sleeper.sleep_now(m)
    h.now += 600
    assert h.sleeper.claim(m) is False


def test_manual_sleep_also_marks_going_down(h):
    m = machine()
    h.sleeper.sleep_now(m)
    assert h.sleeper.claim(m) is True


def test_failed_sleep_is_not_going_down(h):
    h.result = SimpleNamespace(returncode=1, stderr="nope")
    m = machine()
    h.child(m, "build", "s1")
    h.stop("s1")
    h.timers[0].fire()
    assert h.sleeper.claim(m) is False


def test_mark_down_cancels_the_timer_and_marks_going_down(h):
    m = machine()
    h.child(m, "build", "s1")
    h.stop("s1")
    h.sleeper.mark_down(m)
    assert h.timers[0].cancelled
    h.timers[0].fire()
    assert h.runs == [] and h.sleeper.claim(m) is True


# --- busy state file (read by `hermes flota` and the dashboard, other processes) ---------------

@pytest.fixture
def state(tmp_path):
    """A sleeper that publishes its busy counts to ``tmp_path/flota/state.json``."""
    path = tmp_path / "flota" / "state.json"
    s = Harness()
    s.sleeper = IdleSleeper(run=s.run, timer=s.timer, clock=lambda: s.now, state_file=lambda: path,
                            wall=lambda: 1000.0, pid=lambda: 4242)
    s.path = path
    s.read = lambda: json.loads(path.read_text())
    return s



def test_routing_publishes_the_busy_count(state):
    state.sleeper.routed(machine(), {"goal": "build"})
    assert state.read() == {"pid": 4242, "updated": 1000.0, "busy": {"desk": 1}}


def test_finished_work_publishes_zero(state):
    state.child(machine(), "build", "s1")
    state.stop("s1")
    assert state.read()["busy"] == {}


def test_claims_count_as_busy(state):
    m = machine()
    state.sleeper.claim(m)
    state.sleeper.claim(m)
    assert state.read()["busy"] == {"desk": 2}
    state.sleeper.release(m)
    assert state.read()["busy"] == {"desk": 1}


def test_unchanged_counts_are_not_rewritten(state):
    state.sleeper.routed(machine(), {"goal": "build"})
    state.path.unlink()
    state.sleeper.on_start(child_goal="build", child_session_id="s1")  # expected -> running: still 1
    assert not state.path.exists()


def test_a_sleeper_without_a_state_file_writes_nothing(h, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    h.child(machine(), "build", "s1")
    assert list(tmp_path.iterdir()) == []


def test_a_failed_write_is_logged_not_raised(tmp_path, caplog):
    blocker = tmp_path / "flota"
    blocker.write_text("a file where the directory should be")
    sleeper = IdleSleeper(state_file=lambda: blocker / "state.json")
    with caplog.at_level("DEBUG", logger=idle.__name__):
        sleeper.routed(machine(), {"goal": "build"})
    assert "state file" in caplog.text


def _write(path, **payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def test_read_busy_returns_the_counts_of_a_live_gateway(tmp_path):
    path = tmp_path / "state.json"
    _write(path, pid=7, updated=1000.0, busy={"desk": 2})
    assert idle.read_busy(path, now=lambda: 1000.0 + 3600, alive=lambda pid: pid == 7) == {"desk": 2}


@pytest.mark.parametrize("payload, now, alive", [
    ({"pid": 7, "updated": 1000.0, "busy": {"desk": 2}}, 1000.0, False),            # gateway gone
    ({"pid": 7, "updated": 1000.0, "busy": {"desk": 2}}, 1000.0 + 86401, True),     # older than 24 h
    ({"pid": "7", "updated": 1000.0, "busy": {"desk": 2}}, 1000.0, True),           # bad pid
    ({"pid": 7, "updated": 1000.0, "busy": ["desk"]}, 1000.0, True),                # bad busy
    ({"pid": 7, "busy": {}}, 1000.0, True),                                         # no timestamp
])
def test_read_busy_is_unknown_for_a_stale_or_broken_file(tmp_path, payload, now, alive):
    path = tmp_path / "state.json"
    _write(path, **payload)
    assert idle.read_busy(path, now=lambda: now, alive=lambda pid: alive) is None


def test_read_busy_is_unknown_without_a_file_or_with_garbage(tmp_path):
    assert idle.read_busy(tmp_path / "missing.json") is None
    (tmp_path / "bad.json").write_text("{not json")
    assert idle.read_busy(tmp_path / "bad.json") is None


def test_pid_alive_sees_this_process():
    assert idle.pid_alive(os.getpid()) is True
