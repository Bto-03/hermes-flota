"""Puts a machine back to sleep once it has had no subagents for ``sleep_after`` seconds.

It also publishes how busy each machine is to ``<hermes home>/flota/state.json`` so that
``hermes flota`` and the dashboard, which run in other processes, can refuse to sleep a busy machine.
"""
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


class IdleSleeper:
    def __init__(self, run=subprocess.run, timer=threading.Timer, clock=time.monotonic,
                 state_file=None, wall=time.time, pid=os.getpid):
        self._run, self._timer, self._clock = run, timer, clock
        self._state_file, self._wall, self._pid = state_file, wall, pid  # state_file: () -> Path
        self._published = None  # busy counts last written
        self._lock = threading.Lock()
        self._expected = []  # (goal, machine, routed_at), oldest first
        self._running = {}  # child_session_id -> machine
        self._timers = {}  # machine name -> pending sleep timer
        self._claims = {}  # machine name -> routes being prepared (provider claim, not released)
        self._down = {}  # machine name -> clock() when a sleep was sent

    def routed(self, machine, task) -> None:
        with self._lock:
            now = self._clock()
            # shortcut: an expectation whose child never starts (child build failed) is only
            # cleared here once stale, so its machine is left awake until the next route+stop
            # cycle, upgrade when Hermes passes the route label to subagent_start.
            self._expected = [e for e in self._expected if now - e[2] <= _STALE_AFTER]
            self._cancel_timer(machine.name)
            self._expected.append((task.get("goal"), machine, now))
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
                goal = kw.get("child_goal")
                entry = next((e for e in self._expected if e[0] == goal), None)
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
                if (machine is None or self._busy(machine.name)
                        or not (machine.sleep_after > 0
                                and (machine.sleep != "none" or machine.sleep_command))):
                    return
                timer = self._timer(machine.sleep_after, lambda: self._sleep_if_idle(machine, timer))
                timer.daemon = True
                self._timers[machine.name] = timer
                timer.start()
        except Exception:
            logger.warning("flota: subagent_stop hook failed", exc_info=True)

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
        return line

    def _sleep_if_idle(self, machine, timer) -> None:
        with self._lock:
            # cancel() cannot stop a timer that already fired. Work arrives through claim() or
            # routed(), which drop the timer, so still being registered means still idle. Deciding
            # and marking it going down happen under the same lock as claim().
            if self._timers.get(machine.name) is not timer or self._busy(machine.name):
                return
            del self._timers[machine.name]
            sent = self._down[machine.name] = self._clock()
        line = self._send(machine, power.default_action(machine), sent)
        if " failed: " in line:
            logger.warning("flota: auto-sleep %s", line)
        else:
            logger.info("flota: auto-sleep %s", line)
