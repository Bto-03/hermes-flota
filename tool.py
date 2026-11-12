"""The ``flota`` agent tool: see which of the user's machines are on, wake one, or put it to sleep/off.

By default the tool only offers ``status`` and ``wake``. ``sleep`` and ``off`` stop a real computer
that a person may be using, so they are opt-in (the panel's ``agent_power_actions``) and, even then,
every call goes through Hermes' human-approval gate: the hook from ``make_approval_hook`` is a
``pre_tool_call`` hook that returns ``{"action": "approve", ...}`` for them, with one ``rule_key`` per
machine and action.
"""
import json
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor

from . import settings as settings_mod
from .machines import load_machines

TOOL_NAME = "flota"
SAFE_ACTIONS = ("status", "wake")
POWER_ACTIONS = ("sleep", "off")
ACTIONS = SAFE_ACTIONS + POWER_ACTIONS
POWER_ACTIONS_OFF = ("The agent may not suspend or power off machines: this is off by default. The user can "
                     "turn on \"Agent power actions\" (agent_power_actions) in Plugins > Flota and start a new "
                     "session; each request then still asks the user for approval. The user can always run "
                     "`hermes flota sleep|off <machine>` or use `/flota` or the Flota page.")


def schema(power_actions: bool = False) -> dict:
    """The tool schema; ``sleep`` / ``off`` are offered only when ``power_actions`` is on."""
    actions = ACTIONS if power_actions else SAFE_ACTIONS
    if power_actions:
        what = ("to wake one ahead of work, or to put one to sleep or power it off when the user asks "
                "(only machines with a wake method can be powered off; the others can only sleep). "
                "Sleep and off always ask the user for approval first, because whoever is using that "
                "machine loses their session. A machine with running subagents is never put to sleep or "
                "powered off from here. ")
        action_help = ("status: list machines, whether each is up and its running subagents. "
                       "wake / sleep / off: act on `machine`.")
    else:
        what = ("or to wake one ahead of work. Putting a machine to sleep or powering it off is left to "
                "the user (`hermes flota sleep|off <machine>`, `/flota`, or the Flota page). ")
        action_help = ("status: list machines, whether each is up and its running subagents. "
                       "wake: wake `machine`.")
    return {
        "name": TOOL_NAME,
        "description": (
            "The user's own machines that subagents can run on. Use it to check which machines are on, "
            + what + "Delegating a task with `machine` already wakes that machine automatically, so you "
            "do not need to wake it first."),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(actions), "description": action_help},
                "machine": {"type": "string", "description": "Machine name, required for "
                            + ("wake, sleep and off." if power_actions else "wake.")},
            },
            "required": ["action"],
        },
    }


SCHEMA = schema(False)  # the default: what a fresh install offers the model


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
        allowed = ACTIONS if settings.get("agent_power_actions") is True else SAFE_ACTIONS
        if action in POWER_ACTIONS and action not in allowed:
            return _json(error=POWER_ACTIONS_OFF)
        if action not in allowed:
            return _json(error=f"Unknown action {action!r}; use one of: {', '.join(allowed)}.")
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
                waited = round(ensure(machine), 1)
            except Exception as exc:  # WakeError, or a wake command that blew up
                return _json(machine=machine.name, error=str(exc))
            if sleeper is not None:  # woken with no subagent on it: it still gets its idle timer
                sleeper.watch(machine)
            return _json(machine=machine.name, up=True, waited_s=waited)
        if action == "off" and settings.get("allow_power_off") is False:
            return _json(machine=machine.name, error="Powering off is disabled; the user can enable "
                         "allow_power_off in the Flota plugin settings. Use action 'sleep' instead.")
        if action == "off" and not settings_mod.can_power_off(machine, settings):
            return _json(machine=machine.name, error=settings_mod.no_wake_message(machine.name))
        # No `force` here: a model (or text injected into it) never stops a working machine.
        busy = (running or {}).get(machine.name, 0)
        if busy:
            return _json(machine=machine.name, error=f"{machine.name} is busy with {busy} running "
                         "subagent(s), so it was left on. Only the user can stop it anyway: "
                         f"`hermes flota {action} {machine.name} --force` in a terminal, or the Flota page.")
        if sleeper is not None:  # the sleeper also counts routes not started yet
            return _json(machine=machine.name, result=sleeper.sleep_now(machine)
                         if action == "sleep" else sleeper.sleep_now(machine, "poweroff"))
        return _json(machine=machine.name,
                     result=power(machine, "poweroff" if action == "off" else _sleep_action(machine)))
    except Exception as exc:  # a tool answers with an error, never a traceback
        return _json(error=f"flota failed: {exc}")


def _load_config() -> Mapping:
    from hermes_cli.config import load_config_readonly
    from .home import owner_scope
    with owner_scope():  # a flota-<machine> profile holds a stale copy of the fleet
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


def power_actions_enabled(load_config=_load_config) -> bool:
    """The panel's ``agent_power_actions``; False when the config cannot be read."""
    try:
        return _effective(load_config() or {})["agent_power_actions"] is True
    except Exception:
        return False


def _verb(action: str) -> str:
    return "put to sleep" if action == "sleep" else "power off"


def make_approval_hook(load_config=_load_config):
    """``pre_tool_call`` hook: every ``flota`` call with ``action`` sleep or off goes to the user.

    Returns ``{"action": "approve", "message", "rule_key"}`` (core asks the user and fails closed when
    no user is present), or ``{"action": "block", ...}`` while ``agent_power_actions`` is off (the
    handler refuses those calls too, so asking the user would be pointless). Anything else: None.
    ``rule_key`` is per machine and action, so an "always" answer covers only that pair."""
    def approve_power_action(tool_name=None, args=None, **_):
        try:
            if tool_name != TOOL_NAME or not isinstance(args, Mapping):
                return None
            action = args.get("action")
            if action not in POWER_ACTIONS:
                return None
            if not power_actions_enabled(load_config):  # core wraps a block message as {"error": ...}
                return {"action": "block", "message": POWER_ACTIONS_OFF}
            raw = args.get("machine")
            name = raw.strip()[:64] if isinstance(raw, str) and raw.strip() else "(no machine given)"
            return {"action": "approve",
                    "message": (f"Flota: the agent wants to {_verb(action)} the machine '{name}'. Anyone "
                                f"using {name} right now will lose their session (open programs and "
                                "unsaved work)."),
                    "rule_key": f"flota:{action}:{name}"}
        except Exception:  # never let a hook error turn into an unguarded power action
            return {"action": "block", "message": "flota: the approval check failed, so nothing was done."}
    return approve_power_action
