"""Machine definitions parsed from the plugin settings. Bad config is reported, never raised."""
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace

# "timer": nothing can wake it on demand (no Wake-on-LAN path); it sets its own wake alarm for its
# next cron job before Flota suspends it (waketimer.py).
WAKE_METHODS = ("none", "wol", "wol_via", "command", "timer")
DEFAULT_TIMER_LEAD = 120.0
SLEEP_METHODS = ("none", "suspend", "hibernate", "poweroff")
OS_NAMES = ("linux", "macos", "windows")

_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*([smh]?)")
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600}
_MAC = re.compile(r"[0-9a-f]{2}(?:([:-]?)[0-9a-f]{2})(?:\1[0-9a-f]{2}){4}", re.IGNORECASE)


@dataclass(frozen=True)
class Machine:
    name: str
    profile: str
    host: str
    port: int = 22
    description: str = ""
    wake: str = "none"
    mac: str = ""
    broadcast: str = "255.255.255.255"
    via: str = ""
    wake_command: str = ""
    wake_timeout: float = 90.0
    fallback: str = ""
    sleep_after: float = 0.0
    sleep_command: str = ""
    user: str = ""
    key: str = ""
    sleep: str = "none"
    power_prefix: str = ""
    os: str = "linux"
    wake_timer: bool = False  # also arm its own wake alarm before sleeping (secondary to wake)
    timer_lead: float = DEFAULT_TIMER_LEAD  # wake this many seconds before its next cron job


def parse_duration(value, default: float) -> float:
    # bool is an int subclass; `sleep_after: true` is a mistake, not one second.
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) and value >= 0 else default
    if isinstance(value, str):
        match = _DURATION.fullmatch(value.strip().lower())
        if match:
            return float(match[1]) * _UNIT_SECONDS[match[2]]
    return default


def _text(entry: Mapping, key: str, default: str = "") -> str:
    value = entry.get(key)
    return value.strip() if isinstance(value, str) else default


def _parse(name: str, entry, problems: list[str]) -> Machine | None:
    def skip(reason: str) -> None:
        problems.append(f"machine '{name}': {reason}; skipped")

    if not isinstance(entry, Mapping):
        return skip("entry must be a mapping")
    profile, host = _text(entry, "profile"), _text(entry, "host")
    if not profile or not host:
        return skip("needs a non-empty 'profile' and 'host'")
    # ssh (Flota's and the profile's terminal) would read a leading "-" as an option (-oProxyCommand=...).
    for key, value in (("host", host), ("user", _text(entry, "user"))):
        if value.startswith("-"):
            return skip(f"'{key}' must not start with '-' (ssh would read it as an option)")

    wake = entry.get("wake")
    wake = "none" if wake is None else wake.strip() if isinstance(wake, str) else wake
    mac, via, wake_command = _text(entry, "mac"), _text(entry, "via"), _text(entry, "wake_command")
    if wake not in WAKE_METHODS:
        return skip(f"wake must be one of {', '.join(WAKE_METHODS)}, got {wake!r}")
    if wake in ("wol", "wol_via") and not _MAC.fullmatch(mac):
        return skip(f"wake '{wake}' needs a valid 'mac' (12 hex digits)")
    if wake == "wol_via" and not via:
        return skip("wake 'wol_via' needs 'via'")
    if wake == "command" and not wake_command:
        return skip("wake 'command' needs 'wake_command'")
    wake_timer = entry.get("wake_timer", False)
    if not isinstance(wake_timer, bool):
        problems.append(f"machine '{name}': wake_timer must be true or false, got {wake_timer!r}; "
                        "using false")
        wake_timer = False
    if wake_timer and wake == "none":
        wake = "timer"  # the timer is its only way back

    port = entry.get("port")
    if port is None:
        port = 22
    elif isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        problems.append(f"machine '{name}': port must be an integer 1-65535, got {port!r}; using 22")
        port = 22

    sleep = entry.get("sleep")
    sleep = "none" if sleep is None else sleep.strip() if isinstance(sleep, str) else sleep
    if sleep not in SLEEP_METHODS:
        problems.append(f"machine '{name}': sleep must be one of {', '.join(SLEEP_METHODS)}, "
                        f"got {sleep!r}; using none")
        sleep = "none"

    os_name = entry.get("os")
    os_name = "linux" if os_name is None else (
        os_name.strip().lower() if isinstance(os_name, str) else os_name)
    if os_name not in OS_NAMES:
        problems.append(f"machine '{name}': os must be one of {', '.join(OS_NAMES)}, "
                        f"got {os_name!r}; using linux")
        os_name = "linux"
    if os_name == "macos" and sleep == "hibernate":
        problems.append(f"machine '{name}': macOS cannot hibernate on demand; using suspend")
        sleep = "suspend"

    sleep_after = parse_duration(entry.get("sleep_after"), 0.0)
    sleep_command = _text(entry, "sleep_command")
    if sleep_after > 0 and sleep == "none" and not sleep_command:
        problems.append(f"machine '{name}': sleep_after needs 'sleep' or 'sleep_command'; "
                        "auto-sleep disabled")
        sleep_after = 0.0

    return Machine(
        name=name, profile=profile, host=host, port=port,
        description=" ".join(_text(entry, "description").split()), wake=wake, mac=mac,
        broadcast=_text(entry, "broadcast") or "255.255.255.255", via=via,
        wake_command=wake_command, wake_timeout=parse_duration(entry.get("wake_timeout"), 90.0),
        fallback=_text(entry, "fallback"), sleep_after=sleep_after, sleep_command=sleep_command,
        user=_text(entry, "user"), key=_text(entry, "key"), sleep=sleep,
        power_prefix=_text(entry, "power_prefix"), os=os_name, wake_timer=wake_timer,
        timer_lead=parse_duration(entry.get("timer_lead"), DEFAULT_TIMER_LEAD))


def load_machines(settings: Mapping) -> tuple[dict[str, Machine], list[str]]:
    raw = settings.get("machines") if isinstance(settings, Mapping) else None
    if raw is None:
        return {}, []
    if not isinstance(raw, Mapping):
        return {}, [f"'machines' must be a mapping of name -> settings, got {type(raw).__name__}"]

    machines: dict[str, Machine] = {}
    problems: list[str] = []
    for key, entry in raw.items():
        name = str(key).strip()
        machine = _parse(name, entry, problems)
        if machine:
            machines[name] = machine

    # Resolved last so a fallback can name a machine defined further down.
    for name, machine in machines.items():
        fallback = machine.fallback
        if fallback and (fallback == name or fallback not in machines):
            reason = "is the machine itself" if fallback == name else "is not a loaded machine"
            problems.append(f"machine '{name}': fallback '{fallback}' {reason}; fallback cleared")
            machines[name] = replace(machine, fallback="")
    return machines, problems
