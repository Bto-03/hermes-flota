"""Puts a machine back to sleep once it has had no subagents for ``sleep_after`` seconds.

It also publishes how busy each machine is to ``<hermes home>/flota/state.json`` so that
``hermes flota`` and the dashboard, which run in other processes, can refuse to sleep a busy machine.
"""
import contextvars
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from . import power

logger = logging.getLogger(__name__)

_STALE_AFTER = 600.0
_GOING_DOWN_FOR = 60.0  # a sleep sent this recently may not have taken the machine down yet
_STATE_VALID_FOR = 86400.0  # a state file older than this is not trusted, even if its pid is alive
_RECHECK = 60.0  # after a failed auto-sleep: look again this much later, once


def state_path() -> Path:
    from .home import flota_home
    return flota_home() / "flota" / "state.json"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # it exists, owned by someone else
    except OSError:
        return False
    return True


def read_busy(path: Path | None = None, *, now=time.time, alive=pid_alive) -> dict[str, int] | None:
    """Busy counts per machine published by a running gateway, or None when unknown."""
    try:
        data = json.loads((path or state_path()).read_text())
        pid, updated, busy = data["pid"], data["updated"], data["busy"]
        if (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
                or isinstance(updated, bool) or not isinstance(updated, (int, float))
                or not isinstance(busy, dict)):
            return None
        if now() - updated > _STATE_VALID_FOR or not alive(pid):
            return None
        return {str(name): count for name, count in busy.items()
                if isinstance(count, int) and not isinstance(count, bool) and count > 0}
    except Exception:  # missing, unreadable or not JSON: nothing is known
        return None


def auto_sleeps(machine) -> bool:
    return machine.sleep_after > 0 and (machine.sleep != "none" or bool(machine.sleep_command))


def _is_up(host, port) -> bool:
    from .wake import is_up
    return is_up(host, port)


def _armed_at(name):
    from .waketimer import armed_at
    return armed_at(name)


class IdleSleeper:
    def __init__(self, run=subprocess.run, timer=threading.Timer, clock=time.monotonic,
                 state_file=None, wall=time.time, pid=os.getpid, probe=_is_up, armed_at=_armed_at,
                 reload=None):
        self._run, self._timer, self._clock = run, timer, clock
        self._probe, self._armed_at = probe, armed_at
        self._reload = reload  # name -> the Machine as configured now (None: removed), or None
        self._state_file, self._wall, self._pid = state_file, wall, pid  # state_file: () -> Path
        self._published = None  # busy counts last written
        self._lock = threading.Lock()
        self._expected = []  # (goal, machine, routed_at, thread, task index), oldest first
        self._running = {}  # child_session_id -> machine
        self._timers = {}  # machine name -> pending sleep timer
        self._claims = {}  # machine name -> routes being prepared (provider claim, not released)
        self._recovered = False
        self._down = {}  # machine name -> clock() when a sleep was sent

    def routed(self, machine, task) -> None:
        if self._reload and not self._recovered:
            # First delegation of this process: what a restarted gateway left busy gets its timer.
            # Not at load, where a short-lived `hermes flota ...` would take the file and exit.
            self._recovered = True
            self.recover(self._reload)
        with self._lock:
            now, me, index = self._clock(), threading.get_ident(), task.get("index")

            # An expectation whose child never started (core refused the task after routing it):
            # stale, or left on this thread by an earlier delegate_task call. One call routes its
            # tasks in index order, so an index that does not grow is a new call.
            def dead(e) -> bool:
                return now - e[2] > _STALE_AFTER or (
                    e[3] == me and isinstance(index, int) and isinstance(e[4], int) and e[4] >= index)

            dropped = [e[1] for e in self._expected if dead(e)]
            self._expected = [e for e in self._expected if not dead(e)]
            self._cancel_timer(machine.name)
            self._expected.append((task.get("goal"), machine, now, me, index))
            for left_awake in dropped:
                self._watch(left_awake)
            self._publish()

    def claim(self, machine) -> bool:
        """Called by the provider before waking ``machine``; keeps it awake until ``release``.
        True when a sleep was just sent: the caller must wait for it to go down, then wake it."""
        with self._lock:
            self._cancel_timer(machine.name)
            self._claims[machine.name] = self._claims.get(machine.name, 0) + 1
            sent = self._down.pop(machine.name, None)  # one claimer waits; later ones queue behind it
            self._publish()
            return sent is not None and self._clock() - sent <= _GOING_DOWN_FOR

    def release(self, machine) -> None:
        with self._lock:
            left = self._claims.get(machine.name, 0) - 1
            if left > 0:
                self._claims[machine.name] = left
            else:
                self._claims.pop(machine.name, None)
            self._publish()

    def mark_down(self, machine) -> None:
        """A sleep or power off sent outside the sleeper (forced): drop the timer, warn routes."""
        with self._lock:
            self._cancel_timer(machine.name)
            self._down[machine.name] = self._clock()

    def on_start(self, **kw) -> None:
        try:
            with self._lock:
                goal, me = kw.get("child_goal"), threading.get_ident()
                # core fires this hook on the thread that routed the task; the goal alone is the
                # fallback (two sessions may route the same goal text to different machines).
                same_goal = [e for e in self._expected if e[0] == goal]
                entry = next((e for e in same_goal if e[3] == me), same_goal[0] if same_goal else None)
                if entry:
                    self._expected.remove(entry)
                    self._running[kw.get("child_session_id")] = entry[1]
                    self._publish()
        except Exception:
            logger.warning("flota: subagent_start hook failed", exc_info=True)

    def on_stop(self, **kw) -> None:
        try:
            with self._lock:
                machine = self._running.pop(kw.get("child_session_id"), None)
                self._publish()
                if machine is not None:
                    self._watch(machine)
        except Exception:
            logger.warning("flota: subagent_stop hook failed", exc_info=True)

    def watch(self, machine, delay: float = 0.0, retry: bool = True) -> None:
        """Start the idle timer of a machine that is awake with no subagent on it (woken by hand,
        its task refused, ...). It always gets a whole ``sleep_after``; a busy machine is left
        alone. With ``delay``: look again that much later and only start the timer if the machine
        answers then, so one that someone turns on afterwards is never slept early."""
        with self._lock:
            self._watch(machine, delay, retry)

    def _watch(self, machine, delay: float = 0.0, retry: bool = True) -> None:
        if self._busy(machine.name) or not auto_sleeps(machine):
            return
        self._cancel_timer(machine.name)
        after, then = (delay, self._watch_if_up) if delay > 0 else (machine.sleep_after, self._sleep_if_idle)
        ctx = contextvars.copy_context()  # the timer thread reads this profile's home, not the process's
        timer = self._timer(after, lambda: ctx.run(then, machine, timer, retry))
        timer.daemon = True
        self._timers[machine.name] = timer
        timer.start()

    def _watch_if_up(self, machine, timer, retry) -> None:
        with self._lock:
            if self._timers.get(machine.name) is not timer:
                return
            del self._timers[machine.name]
        if self._probe(machine.host, machine.port):
            self.watch(machine, retry=retry)

    def recover(self, lookup, alive=pid_alive) -> None:
        """At start-up: machines a dead process (a restarted gateway) left busy get their idle timer.
        ``lookup``: name -> Machine or None. Never raises."""
        try:
            if self._state_file is None:
                return
            data = json.loads(Path(self._state_file()).read_text())
            pid, names = data["pid"], list(data["busy"])
            if not names or self._wall() - data["updated"] > _STATE_VALID_FOR or (
                    pid != self._pid() and alive(pid)):
                return
            with self._lock:
                self._publish()  # claim the file, so only one process recovers it
                for name in names:
                    machine = lookup(name)
                    if machine is not None:
                        self._watch(machine)
        except Exception:
            logger.debug("flota: nothing to recover from the busy state file", exc_info=True)

    def active(self) -> dict[str, int]:
        with self._lock:
            counts: dict[str, int] = {}
            for machine in self._running.values():
                counts[machine.name] = counts.get(machine.name, 0) + 1
            return counts

    def sleep_now(self, machine, action=None) -> str:
        with self._lock:
            busy = self._busy(machine.name)
            if busy:
                return f"{machine.name}: busy with {busy} subagent(s), not put to sleep"
            self._cancel_timer(machine.name)
            sent = self._down[machine.name] = self._clock()
        return self._send(machine, action or power.default_action(machine), sent)

    def _busy(self, name: str) -> int:
        now = self._clock()  # a stale expectation is a child that never started, not work
        return (sum(m.name == name for m in self._running.values())
                + sum(e[1].name == name and now - e[2] <= _STALE_AFTER for e in self._expected)
                + self._claims.get(name, 0))

    def _publish(self) -> None:
        """Write the busy counts when they changed. Called under the lock; never raises."""
        if self._state_file is None:
            return
        names = ({m.name for m in self._running.values()} | {e[1].name for e in self._expected}
                 | set(self._claims))
        busy = {name: count for name in sorted(names) if (count := self._busy(name))}
        if busy == self._published:
            return
        try:
            path = Path(self._state_file())
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{self._pid()}.tmp")
            tmp.write_text(json.dumps({"pid": self._pid(), "updated": self._wall(), "busy": busy}))
            os.replace(tmp, path)
            self._published = busy
        except Exception:
            logger.debug("flota: could not write the busy state file", exc_info=True)

    def _cancel_timer(self, name: str) -> None:
        timer = self._timers.pop(name, None)
        if timer:
            timer.cancel()

    def _send(self, machine, action, sent) -> str:
        # Outside the lock: the command may take 30s and must not block the hooks or claims.
        line = power.power(machine, action, run=self._run)
        if " failed: " in line:
            with self._lock:  # it did not go down, so routes need not wait for it
                if self._down.get(machine.name) == sent:
                    del self._down[machine.name]
        elif action != "poweroff":
            self._watch_alarm(machine)
        return line

    def _watch_alarm(self, machine) -> None:
        """A machine that wakes itself for a cron job is watched from that job on: the job may
        never delegate to it. Real work (claim/routed) cancels this like any other timer."""
        try:
            from . import waketimer
            armed = self._armed_at(machine.name) if waketimer.uses_timer(machine) else None
            if armed is not None and armed > self._wall():
                self.watch(machine, delay=armed - self._wall() + machine.timer_lead)
        except Exception:
            logger.debug("flota: could not watch %s's wake timer", machine.name, exc_info=True)

    def _sleep_if_idle(self, machine, timer, retry=True) -> None:
        try:  # the panel may have changed since the timer started
            fresh = self._reload(machine.name) if self._reload else machine
        except Exception:
            fresh = machine
        with self._lock:
            # cancel() cannot stop a timer that already fired. Work arrives through claim() or
            # routed(), which drop the timer, so still being registered means still idle. Deciding
            # and marking it going down happen under the same lock as claim().
            if self._timers.get(machine.name) is not timer or self._busy(machine.name):
                return
            del self._timers[machine.name]
            if fresh is None or not auto_sleeps(fresh):
                return
            machine = fresh
            sent = self._down[machine.name] = self._clock()
        line = self._send(machine, power.default_action(machine), sent)
        if " failed: " in line:
            logger.warning("flota: auto-sleep %s", line)
            if retry:  # once: a hiccup must not leave it on all night
                self.watch(machine, delay=_RECHECK, retry=False)
        else:
            logger.info("flota: auto-sleep %s", line)
