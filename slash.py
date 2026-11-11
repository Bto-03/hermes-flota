"""``/flota``: what the fleet looks like right now, and manual wake / sleep.

``/flota`` or ``/flota status``   one line per machine: up/down, wake method, profile, fallback, auto-sleep
``/flota dry-run <machine>``      what delegating to it would do right now, step by step; sends nothing
``/flota wake <machine>``         wake it and wait until its SSH port answers
``/flota sleep <machine>``        put it to sleep now (its sleep action, suspend when none)
``/flota off <machine>``          power it off; only a machine Flota can wake again (wol, wol_via, command),
                                  and not while "Allow power off" is turned off in Plugins > Flota
"""
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from . import power as power_mod
from .machines import Machine, load_machines
from .settings import POWER_OFF_DISABLED, effective, plugin_settings_from_config, power_off_refusal
from .wake import WakeError, ensure_ready, is_up

SUBCOMMANDS = ("status", "dry-run", "wake", "sleep", "off")
_NO_MACHINES = ("No machines configured. Add them in the Flota panel (Plugins > Flota), or run "
                "`hermes flota setup <name> --host HOST --user USER` in a terminal.")
_OFF_DISABLED = POWER_OFF_DISABLED


def _profile_exists(name: str) -> bool:
    from hermes_cli.profiles import profile_exists
    return profile_exists(name)


def _load_config() -> Mapping:
    from hermes_cli.config import load_config_readonly
    from .home import owner_scope
    with owner_scope():
        return load_config_readonly()


def load_settings() -> Mapping:
    """The active profile's Flota settings: the panel merged over ``delegation.backends.flota``."""
    cfg = _load_config()
    cfg = cfg if isinstance(cfg, Mapping) else {}
    return effective(cfg.get("delegation"), plugin_settings_from_config(cfg))


def make_handler(ctx, sleeper=None) -> Callable[..., str]:
    def flota_command(raw_args: str = "", **_) -> str:
        try:
            settings = load_settings()
        except Exception as exc:  # an unreadable config is an answer, not a traceback in the chat
            return f"/flota failed: {exc}"
        return handle(raw_args, settings=settings, probe=is_up, ensure=ensure_ready, sleeper=sleeper,
                      profile_exists=_profile_exists)
    return flota_command


def handle(raw_args: str, *, settings: Mapping, probe=is_up, ensure=ensure_ready, sleeper=None,
           profile_exists=_profile_exists, power=None) -> str:
    try:
        settings = settings if isinstance(settings, Mapping) else {}
        power = power or power_mod.power  # looked up per call so tests can replace it
        machines, problems = load_machines(settings)
        words = (raw_args or "").split()
        command = words[0].lower() if words else "status"
        if command == "status" and len(words) <= 1:
            return _status(machines, problems, probe, sleeper, profile_exists)
        machine = machines.get(words[1]) if len(words) == 2 else None
        if command in SUBCOMMANDS[1:] and len(words) == 2 and machine is None:
            if not machines and not problems:
                return _NO_MACHINES
            return f"No machine named '{words[1]}'.\n" + _usage(machines)
        if command not in SUBCOMMANDS[1:] or machine is None:
            return _usage(machines)
        if command == "dry-run":
            return _dry_run(machine, probe, profile_exists)
        if command == "wake":
            try:
                return f"{machine.name} is up (waited {ensure(machine):.0f}s)"
            except WakeError as exc:
                return str(exc)
        if command == "off":
            refusal = power_off_refusal(machine, settings)
            if refusal:
                return refusal
            # Through the sleeper when there is one, so a machine running subagents is kept on.
            return sleeper.sleep_now(machine, "poweroff") if sleeper else power(machine, "poweroff")
        return sleeper.sleep_now(machine) if sleeper else power(machine, power_mod.default_action(machine))
    except Exception as exc:  # a chat command answers with text, whatever broke
        return f"/flota failed: {exc}"


def _usage(machines: Mapping[str, Machine]) -> str:
    names = ", ".join(machines) or "none configured"
    return ("Usage: /flota [status | dry-run <machine> | wake <machine> | sleep <machine> | off <machine>]"
            f"\nMachines: {names}")


def _duration(seconds: float) -> str:
    for unit, size in (("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds / size:g}{unit}"
    return f"{seconds:g}s"


def _status(machines, problems, probe, sleeper, profile_exists) -> str:
    if not machines and not problems:
        return _NO_MACHINES
    # Concurrent: a dead host costs the probe's full timeout, and a sleeping fleet is mostly dead hosts.
    with ThreadPoolExecutor(max_workers=8) as pool:
        up = list(pool.map(lambda m: probe(m.host, m.port), machines.values()))
    running = sleeper.active() if sleeper is not None else None
    lines = []
    for machine, is_on in zip(machines.values(), up):
        missing = "" if profile_exists(machine.profile) else " (profile missing)"
        parts = [f"- {machine.name}: {'up' if is_on else 'down'}", f"wake {machine.wake}",
                 f"profile {machine.profile}{missing}"]
        if machine.fallback:
            parts.append(f"fallback {machine.fallback}")
        if machine.sleep_after:
            action = f"`{machine.sleep_command}`" if machine.sleep_command else machine.sleep
            parts.append(f"sleeps: {action} after {_duration(machine.sleep_after)} idle")
        if running is not None:
            parts.append(f"{running.get(machine.name, 0)} running")
        lines.append(" · ".join(parts))
    return "\n".join(lines + [f"! {problem}" for problem in problems])


def _wake_step(machine: Machine) -> str:
    if machine.wake == "wol":
        return (f"Wake: send a Wake-on-LAN magic packet for {machine.mac} to {machine.broadcast}:9 "
                "(UDP, 3 times) from this computer.")
    if machine.wake == "wol_via":
        return (f"Wake: ssh to the relay {machine.via} and have it broadcast the Wake-on-LAN magic packet "
                f"for {machine.mac} to {machine.broadcast}:9.")
    return f"Wake: run `{machine.wake_command}` on this computer."


def _sleep_step(machine: Machine) -> str:
    if machine.sleep_command:
        return f"run `{machine.sleep_command}` on this computer to put it back to sleep"
    action = power_mod.default_action(machine)
    verb = "power it off" if action == "poweroff" else f"{action} it"
    step = f"{verb} over SSH (`{power_mod.remote_command(machine, action)}`)"
    if machine.power_prefix == power_mod.NIRI_PREFIX:
        step += (", run inside its graphical session (niri) because polkit refuses it from a "
                 "plain SSH session")
    return step


def _dry_run(machine: Machine, probe, profile_exists) -> str:
    address = f"{machine.host}:{machine.port}"
    is_on = probe(machine.host, machine.port)
    fallback = (f"the task goes to '{machine.fallback}' instead" if machine.fallback
                else "the task is refused with that error (no fallback)")
    steps = [f"Probe {address} (SSH): {'up' if is_on else 'down'} right now."]
    if is_on:
        steps.append("No wake needed: the subagent starts immediately.")
    elif machine.wake == "none":
        steps.append(f"It has no wake method (wake: none), so it cannot be turned on from here: {fallback}.")
    else:
        steps.append(_wake_step(machine))
        steps.append(f"Wait: probe {address} every 2s for up to {_duration(machine.wake_timeout)}, "
                     "repeating the wake every 10s in case it was missed.")
    if profile_exists(machine.profile):
        steps.append(f"Run: the subagent runs as Hermes profile '{machine.profile}', whose terminal is "
                     f"SSH on {machine.name}.")
    else:
        steps.append(f"Run: profile '{machine.profile}' does not exist, so Hermes would refuse the task. "
                     f"Create it with `hermes flota setup {machine.name} --host {machine.host} --user USER`.")
    if is_on or machine.wake == "none":
        steps.append(f"Fallback: {'none (no fallback)' if not machine.fallback else machine.fallback}"
                     f"{', not needed while it is up' if is_on else ''}.")
    else:
        steps.append(f"Fallback: if it is still down after {_duration(machine.wake_timeout)}, {fallback}.")
    if machine.sleep_after:
        steps.append(f"Sleep: {_duration(machine.sleep_after)} after its last subagent finishes, if no new "
                     f"one started, {_sleep_step(machine)}.")
    else:
        steps.append("Sleep: never automatically (no sleep_after); it stays on.")
    if machine.sleep == "poweroff":
        steps.append("Note: it sleeps by powering off; waking it from full power-off needs Wake-on-LAN "
                     "from S5 enabled in its BIOS.")
    head = f"Dry run: delegating to '{machine.name}'" + (f" ({machine.description})" if machine.description else "")
    body = "\n".join(f"{i}. {step}" for i, step in enumerate(steps, 1))
    return f"{head}\n{body}\nNothing was sent: this only probed {address}."
