"""Merges the Plugins-hub panel (``plugins.entries.flota.settings``) over the older
``delegation.backends.flota`` block into the one mapping ``load_machines`` and the provider read."""
from collections.abc import Mapping

PLUGIN_ID = "flota"
# Wake methods Flota can use to turn a machine back on; only those machines may be powered off.
WAKEABLE = ("wol", "wol_via", "command")
POWER_OFF_DISABLED = ("Powering off is disabled. Turn on \"Allow power off\" in the Flota panel "
                      "(Plugins > Flota), or use `sleep` instead.")


def no_wake_message(name: str) -> str:
    return (f"{name} has no wake method, so Flota would not be able to turn it back on; "
            "it can only be suspended.")


def _wake_of(machine) -> str:
    wake = machine.get("wake") if isinstance(machine, Mapping) else getattr(machine, "wake", None)
    return wake.strip() if isinstance(wake, str) else ""


def power_off_refusal(machine, settings: Mapping | None, name: str | None = None) -> str | None:
    """Why ``machine`` (a Machine or a raw entry) may not be powered off, or None when it may.
    The panel's ``allow_power_off`` is a master switch: only an explicit False turns it off."""
    if _mapping(settings).get("allow_power_off") is False:
        return POWER_OFF_DISABLED
    if _wake_of(machine) not in WAKEABLE:
        return no_wake_message(name if name is not None else getattr(machine, "name", "This machine"))
    return None


def can_power_off(machine, settings: Mapping | None) -> bool:
    return power_off_refusal(machine, settings) is None


def _mapping(value) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def plugin_settings_from_config(config: Mapping) -> Mapping:
    """``plugins.entries.flota.settings`` of a full config ({} when absent or malformed)."""
    block = config
    for key in ("plugins", "entries", PLUGIN_ID, "settings"):
        block = _mapping(block).get(key)
    return _mapping(block)


def effective(delegation_cfg: Mapping | None, plugin_settings: Mapping | None) -> dict:
    panel = _mapping(plugin_settings)
    old = _mapping(_mapping(_mapping(delegation_cfg).get("backends")).get(PLUGIN_ID))

    def pick(key):
        return panel.get(key) or old.get(key)

    allow_off = panel.get("allow_power_off") is not False
    machines = {}
    for name, entry in _mapping(pick("machines")).items():
        if isinstance(entry, Mapping):  # anything else is left for load_machines to report
            entry = dict(entry)
            entry.setdefault("sleep", panel.get("sleep_action", "suspend"))
            # `sleep: none` means "never auto-sleep"; a default idle time would only raise a config warning.
            if entry["sleep"] != "none" or entry.get("sleep_command"):
                entry.setdefault("sleep_after", panel.get("sleep_after", "10m"))
            if panel.get("auto_sleep", True) is False:
                entry["sleep_after"] = 0
            # load_machines strips the value, so " poweroff " must not slip past the guard. A machine
            # Flota cannot wake again (or any machine, with the master switch off) suspends instead.
            if (isinstance(entry["sleep"], str) and entry["sleep"].strip() == "poweroff"
                    and (not allow_off or _wake_of(entry) not in WAKEABLE)):
                entry["sleep"] = "suspend"
        machines[name] = entry
    return {"machines": machines, "default": pick("default"), "cache_ttl": pick("cache_ttl"),
            "allow_power_off": allow_off, "agent_tool": panel.get("agent_tool", True) is not False}
