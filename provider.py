"""The ``flota`` delegation provider.

Settings come from the Flota panel (``plugins.entries.flota.settings``, Plugins > Flota), merged
over the older ``delegation.backends.flota`` block of config.yaml (see settings.py)::

    delegation:
      backend: flota
      backends:
        flota:
          machines:                   # see machines.py for every key
            desktop:
              profile: desktop        # Hermes profile whose terminal is SSH to this machine
              host: 192.0.2.20
              description: GPU box; heavy builds.
              wake: wol
              mac: aa:bb:cc:dd:ee:ff
              fallback: laptop
          default: desktop            # optional: machine for tasks that set none
          cache_ttl: 5m               # prompt-cache lifetime quoted in the guidance

``delegate_task`` gains a per-task ``machine`` enum. A task that sets one runs as that machine's
profile once the machine answers (it is woken first); a task without one runs on ``default`` when
set, else on the parent's own route.
"""
import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from agent.delegation_provider import DelegationProvider, DelegationRouteError

from .machines import Machine, load_machines, parse_duration
from .settings import WAKEABLE, effective, plugin_settings_from_config
from .wake import WakeError, ensure_ready, is_up

logger = logging.getLogger(__name__)

# How long a machine that was just told to sleep gets to stop answering. Sending the sleep can take
# 50s (arm its wake timer, then the ssh command), so this matches idle._GOING_DOWN_FOR.
_DOWN_WAIT = 60.0

BACKEND = "flota"
PARAMETER = "machine"
_warned: set[str] = set()


def _warn_once(text: str) -> None:
    # route_task runs for every task; say each problem once.
    if text not in _warned:
        _warned.add(text)
        logger.warning(text)


def _panel() -> Mapping[str, Any]:
    """The Flota panel block of the active config; {} without a Hermes core to read it."""
    try:
        from hermes_cli.config import load_config_readonly
    except ImportError:
        return {}
    from .home import owner_scope
    with owner_scope():  # a flota-<machine> profile holds a stale copy of the fleet
        return plugin_settings_from_config(load_config_readonly() or {})


def _own_route(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """The route of a task that goes to no machine: the ``delegation`` section, as the default
    backend answers, so ``delegation.model`` / ``provider`` still apply. Core keeps the route keys."""
    return {k: v for k, v in (config or {}).items() if k != "service_tier"}


def _settings(config: Mapping[str, Any] | None) -> dict:
    return effective(config, _panel())


def fleet(config: Mapping[str, Any] | None) -> tuple[dict[str, Machine], list[str]]:
    """The machines (and config problems) of a ``delegation`` config section plus the panel."""
    return load_machines(_settings(config))


def _duration_text(seconds: float) -> str:
    for size, unit in ((3600, "hour"), (60, "minute")):
        if seconds % size == 0:
            seconds, name = seconds / size, unit
            break
    else:
        name = "second"
    return f"{seconds:g} {name}" + ("" if seconds == 1 else "s")


class FlotaDelegationProvider(DelegationProvider):
    """``delegation.backend: flota``: the model picks one of the user's machines per subagent."""

    def __init__(self, ensure: Callable[[Machine], Any] = ensure_ready,
                 on_routed: Callable[[Machine, Mapping[str, Any]], None] | None = None,
                 sleeper=None, probe=is_up, sleep=time.sleep, clock=time.monotonic) -> None:
        self._ensure = ensure
        self._sleeper = sleeper  # IdleSleeper: claim/release keep auto-sleep off a machine being routed
        self._on_routed = on_routed or (sleeper.routed if sleeper is not None else None)
        self._probe, self._sleep, self._clock = probe, sleep, clock
        # One instance serves every session of the process.
        self._guard = threading.Lock()
        self._wake_locks: dict[str, threading.Lock] = {}

    @property
    def name(self) -> str:
        return BACKEND

    @property
    def display_name(self) -> str:
        return "Flota"

    @property
    def summary(self) -> str:
        return "Runs each subagent on one of your own machines, waking it first."

    def task_parameters(self, config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        machines, _ = fleet(config)
        if not machines:
            return {}
        lines = [
            "Run this subagent on one of these machines: its terminal and files are on that machine. "
            "A machine that is off is woken first, which may take up to a minute or two. "
            "Omit for the default route.",
            *(f"{m.name}: {m.description}" if m.description else m.name for m in machines.values()),
        ]
        return {PARAMETER: {"type": "string", "enum": list(machines), "description": "\n".join(lines)}}

    def description(self, config: Mapping[str, Any]) -> str:
        settings = _settings(config)
        if not load_machines(settings)[0]:
            return ""
        ttl = _duration_text(parse_duration(settings.get("cache_ttl"), 300.0) or 300.0)
        return (
            "Each task may set `machine` to run that subagent on one of the listed machines; tasks for "
            "different machines run in parallel. Do not steer or wait on a subagent that has been "
            f"silent longer than {ttl}: stop it and delegate a fresh task carrying the goal, a short "
            "summary of what is done and the paths of its artifacts, because its prompt cache has "
            "expired and reviving it re-sends the whole transcript.")

    def route_task(self, task: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
        settings = _settings(config)
        machines, _ = load_machines(settings)
        chosen = task.get(PARAMETER)
        if not chosen:
            default = settings.get("default")
            if isinstance(default, str):
                default = default.strip()
            if default in (None, ""):
                return _own_route(config)
            # isinstance first: an unhashable value (a YAML list) cannot be looked up.
            if not isinstance(default, str) or default not in machines:
                _warn_once(f"delegation.backends.{BACKEND}.default {default!r} is not a loaded machine; "
                           "tasks without a machine use the default route.")
                return _own_route(config)
            chosen = default
        machine = machines.get(chosen)
        if machine is None:  # core enforces the enum; this is a schema built from an older config
            raise DelegationRouteError(
                f"Machine '{chosen}' is not configured." + self._advice(machines, {chosen}))

        claimed: list[Machine] = []
        late: list[Machine] = []
        try:
            return self._route(machines, machine, task, claimed, late)
        finally:  # routed() now holds the machine awake (or the route failed)
            for held in claimed:
                self._sleeper.release(held)
            # Woken but not up in time: it may finish booting with no task. Look once more after
            # another wake_timeout and give it its idle timer only if it answers then.
            for woken in late:
                if self._sleeper is not None and woken.wake in WAKEABLE:
                    self._sleeper.watch(woken, delay=max(woken.wake_timeout, 1.0))

    def _route(self, machines, machine, task, claimed, late) -> dict[str, Any]:
        try:
            self._ready(machine, claimed)
            label = f"{BACKEND}:{machine.name}"
        except WakeError as exc:
            late.append(machine)
            failure, tried = f"Machine '{machine.name}' is not available: {exc}.", {machine.name}
            used = machines.get(machine.fallback)
            if used is None:
                raise DelegationRouteError(failure + self._advice(machines, tried)) from exc
            try:  # once: the fallback's own fallback is never followed
                self._ready(used, claimed)
            except WakeError as second:
                late.append(used)
                tried.add(used.name)
                raise DelegationRouteError(
                    f"{failure} Its fallback '{used.name}' is not available either: {second}."
                    + self._advice(machines, tried)) from second
            label = f"{BACKEND}:{used.name} (fallback for {machine.name})"
            machine = used

        if self._on_routed:
            try:
                self._on_routed(machine, task)
            except Exception:
                logger.exception("flota: on_routed failed for machine %s", machine.name)
        return {"profile": machine.profile, "label": label}

    def _ready(self, machine: Machine, claimed: list[Machine]) -> None:
        going_down = False
        if self._sleeper is not None:  # before the probe, so no auto-sleep can start after it
            going_down = self._sleeper.claim(machine)
            claimed.append(machine)
        with self._guard:
            lock = self._wake_locks.setdefault(machine.name, threading.Lock())
        # A second session asking for the same machine waits here, then finds it already up.
        with lock:
            if going_down:
                self._wait_down(machine)
            self._ensure(machine)

    def _wait_down(self, machine: Machine) -> None:
        """A sleep was just sent: wait until sshd stops answering, so ensure wakes it again
        instead of handing the task to a machine that is suspending."""
        deadline = self._clock() + _DOWN_WAIT
        while self._probe(machine.host, machine.port) and self._clock() < deadline:
            self._sleep(1)

    @staticmethod
    def _advice(machines: Mapping[str, Machine], tried: set[str]) -> str:
        others = [name for name in machines if name not in tried]
        other = f"use another machine ({', '.join(others)}) or " if others else ""
        return f" Resend this task: {other}omit `machine` to run it locally."
