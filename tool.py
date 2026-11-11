"""The ``flota`` agent tool: see which of the user's machines are on, wake one, or put it to sleep/off."""
import json
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor

from . import settings as settings_mod
from .machines import load_machines

ACTIONS = ("status", "wake", "sleep", "off")

SCHEMA = {
    "name": "flota",
    "description": (
        "The user's own machines that subagents can run on. Use it to check which machines are on, "
        "to wake one ahead of work, or to put one to sleep or power it off when the user asks "
        "(only machines with a wake method can be powered off; the others can only sleep). "
        "Delegating a task with `machine` already wakes that machine automatically, so you do not "
        "need to wake it first."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS),
                       "description": "status: list machines, whether each is up and its running "
                                      "subagents. wake / sleep / off: act on `machine`."},
            "machine": {"type": "string", "description": "Machine name, required for wake, sleep and off."},
            "force": {"type": "boolean",
                      "description": "Sleep or power off even though subagents are running on it. "
                                     "Only when the user explicitly asks."},
        },
        "required": ["action"],
    },
}


def _json(**payload) -> str:
    return json.dumps(payload)


def _sleep_action(machine) -> str:
    # `sleep: none` only stops auto-sleep; an explicit request still suspends.
    action = getattr(machine, "sleep", "")
    return action if action in ("suspend", "hibernate", "poweroff") else "suspend"


def handle(args: Mapping, *, settings: Mapping, probe, ensure, power, sleeper=None) -> str:
    try:
        args = args if isinstance(args, Mapping) else {}
        machines, _ = load_machines(settings)
        action = args.get("action")
        if action not in ACTIONS:
            return _json(error=f"Unknown action {action!r}; use one of: {', '.join(ACTIONS)}.")
        running = sleeper.active() if sleeper is not None else None
        if action == "status":
            with ThreadPoolExecutor(max_workers=8) as pool:
                up = list(pool.map(lambda m: probe(m.host, m.port), machines.values()))
            return _json(machines=[
                {"name": m.name, "up": bool(on), "wake": m.wake, "sleep": _sleep_action(m),
                 "can_power_off": settings_mod.can_power_off(m, settings),
                 "running": None if running is None else running.get(m.name, 0)}
                for m, on in zip(machines.values(), up)])

        machine = machines.get(args.get("machine"))
        if machine is None:
            return _json(error=f"Unknown machine {args.get('machine')!r}; configured: "
                               f"{', '.join(machines) or 'none'}.")
        if action == "wake":
            try:
                return _json(machine=machine.name, up=True, waited_s=round(ensure(machine), 1))
            except Exception as exc:  # WakeError, or a wake command that blew up
                return _json(machine=machine.name, error=str(exc))
        if action == "off" and settings.get("allow_power_off") is False:
            return _json(machine=machine.name, error="Powering off is disabled; the user can enable "
                         "allow_power_off in the Flota plugin settings. Use action 'sleep' instead.")
        if action == "off" and not settings_mod.can_power_off(machine, settings):
            return _json(machine=machine.name, error=settings_mod.no_wake_message(machine.name))
        force = args.get("force") is True
        busy = (running or {}).get(machine.name, 0)
        if busy and not force:
            return _json(machine=machine.name, error=f"{machine.name} is busy with {busy} running "
                         "subagent(s); pass force: true only if the user wants it stopped anyway.")
        if sleeper is not None and not force:  # the sleeper also counts routes not started yet
            return _json(machine=machine.name, result=sleeper.sleep_now(machine)
                         if action == "sleep" else sleeper.sleep_now(machine, "poweroff"))
        if sleeper is not None:
            sleeper.mark_down(machine)  # drop its timer; routes wait for it to go down
        return _json(machine=machine.name,
                     result=power(machine, "poweroff" if action == "off" else _sleep_action(machine)))
    except Exception as exc:  # a tool answers with an error, never a traceback
        return _json(error=f"flota failed: {exc}")


def _load_config() -> Mapping:
    from hermes_cli.config import load_config_readonly
    return load_config_readonly()


def _effective(config: Mapping) -> dict:
    return settings_mod.effective(config.get("delegation"), settings_mod.plugin_settings_from_config(config))


def make_handler(sleeper=None, probe=None, ensure=None, power=None, load_config=_load_config):
    """Handler for ``ctx.register_tool``; config is re-read on every call so panel edits apply at once."""
    def flota_tool(args, **_):
        nonlocal probe, ensure, power
        try:
            if probe is None or ensure is None:
                from .wake import ensure_ready, is_up
                probe, ensure = probe or is_up, ensure or ensure_ready
            if power is None:
                from .power import power as power_fn
                power = power_fn
            config = load_config()
            return handle(args, settings=_effective(config if isinstance(config, Mapping) else {}),
                          probe=probe, ensure=ensure, power=power, sleeper=sleeper)
        except Exception as exc:
            return _json(error=f"flota failed: {exc}")
    return flota_tool


def available(load_config=_load_config) -> bool:
    """``check_fn``: the tool is offered only while the panel's ``agent_tool`` is on."""
    try:
        return _effective(load_config() or {})["agent_tool"]
    except Exception:
        return False
