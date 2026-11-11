"""``hermes flota``: inspect the fleet from a terminal and add a machine to it.

``hermes flota status | dry-run | wake | sleep | off <machine>`` print what ``/flota`` prints in a chat,
exit 1 when the action failed and 2 for an unknown machine. ``sleep`` and ``off`` refuse a machine the
running gateway reports busy (``<hermes home>/flota/state.json``) unless given ``--force``.
``hermes flota setup <name> --host H --user U`` creates the profile ``flota-<name>`` (a clone of the
active profile's config, so it keeps the model and provider), points that profile's terminal at the
machine over SSH, and adds the machine to the Flota panel (``plugins.entries.flota.settings.machines``)
of the active profile. When the machine is on, it reads its OS, MAC, broadcast address and
Wake-on-LAN state over SSH, so those flags are optional. Running it again updates the same entries.
``hermes flota doctor [machine]`` checks each machine and prints a fix per failed line.
"""
import argparse
import getpass
import os as _os
import re
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from collections.abc import Mapping
from pathlib import Path
from typing import Callable

from . import crew, detect, idle, pairing, power, slash, ssh_guard, wake
from . import home as _homes
from .machines import OS_NAMES, SLEEP_METHODS, WAKE_METHODS, Machine, load_machines, parse_duration
from .settings import PLUGIN_ID, effective, plugin_settings_from_config

_USAGE = ("usage: hermes flota {status | dry-run | wake | sleep | off <machine> | "
          "setup <name> --host HOST --user USER [options] | doctor [machine] | "
          "invite | join <invite> | join-check | add-ship <ship code> | leave [flagship] | "
          "role [flagship|ship|none] [--force]}")
ROLES = ("flagship", "ship")
_OPTIONAL = ("key", "port", "description", "wake", "mac", "broadcast", "sleep", "sleep_after", "os")
_MACHINE_COMMANDS = ("dry-run", "wake", "sleep", "off")


# --- core access (tests replace these) ---------------------------------------------------------

def _active_config_path() -> Path:
    """The config of the profile Flota acts for (never a flota-<machine> plumbing profile)."""
    from hermes_cli.config import get_config_path
    with _homes.owner_scope():
        return get_config_path()


def _read_config(path: Path) -> dict:
    from hermes_cli.config import require_readable_config_before_write
    return require_readable_config_before_write(path)


def _update_config(path: Path, mutate: Callable[[dict], None]) -> None:
    """Read the whole file, ``mutate`` it, write it back. ``data`` is the complete current state,
    so a key ``mutate`` removed (a machine, the role, a fleet) is a deliberate deletion: that goes
    through core's ``atomic_config_replace``; ``atomic_config_write`` refuses any write that drops a
    key."""
    from hermes_cli import config as core
    if core.is_managed():
        raise RuntimeError(core.format_managed_message("change Flota settings"))
    data = _read_config(path)
    mutate(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    getattr(core, "atomic_config_replace", core.atomic_config_write)(path, data)


def _profile_exists(name: str) -> bool:
    from hermes_cli.profiles import profile_exists
    return profile_exists(name)


def _profile_dir(name: str) -> Path:
    from hermes_cli.profiles import get_profile_dir
    return get_profile_dir(name)


def _mark_owner(profile_dir: Path, owner: Path) -> None:
    _homes.mark_owner(profile_dir, owner)


def _hide_plumbing(profile_dir: Path, machine: str) -> bool:
    return _homes.hide_from_roster(profile_dir, machine)


def _plumbing_profiles() -> list[tuple[str, Path]]:
    """``[(name, dir)]`` of every ``flota-*`` profile next to the active one (its Hermes root's
    ``profiles/``; the root is the active home itself unless that is a named profile)."""
    active = _homes.active_home()
    root = active.parent.parent if active.parent.name == "profiles" else active
    try:
        entries = sorted((root / "profiles").iterdir())
    except OSError:
        return []
    return [(entry.name, entry) for entry in entries
            if entry.name.startswith(_homes.PROFILE_PREFIX) and (entry / "config.yaml").is_file()]


def _wake_timer() -> dict | None:
    """``waketimer.capability()`` for this host, or None when this Flota has no waketimer module."""
    try:
        from . import waketimer
    except ImportError:
        return None
    capability = getattr(waketimer, "capability", None)
    if capability is None:
        return None
    try:
        result = capability()
    except Exception as exc:  # a probe that blew up: the option is shown as not available
        return {"ok": False, "reason": str(exc)}
    return dict(result) if isinstance(result, Mapping) else None


def _create_profile(name: str) -> Path:
    from hermes_cli.profiles import create_profile
    with _homes.owner_scope():  # clone source: the active profile (its owner when that is plumbing)
        return create_profile(name, clone_config=True, no_alias=True)


def _detect_prefix(machine) -> tuple[str | None, str]:
    return power.detect_prefix(machine)


def _is_up(machine) -> bool:
    return wake.is_up(machine.host, machine.port, timeout=3)


def _real_detect_os(machine) -> str:
    detect_os = getattr(power, "detect_os", None)  # may not exist in every version of power.py
    try:
        return (detect_os(machine) if detect_os else "") or ""
    except Exception:  # detection must never break setup
        return ""


_detect_os = _real_detect_os


def _detect_network(machine, os: str) -> dict:
    return detect.network(machine, os=os)


def _ssh_ok(machine) -> tuple[bool, str]:
    try:
        done = subprocess.run(power.ssh_argv(machine, "true"), timeout=15, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return False, "ssh timed out after 15s"
    except (OSError, ValueError) as exc:
        return False, str(exc)
    lines = (done.stderr or "").strip().splitlines()
    return done.returncode == 0, (lines[-1] if lines else f"exit {done.returncode}")


def _file_sync_guarded() -> bool:
    return ssh_guard.installed()


def _local_networks() -> list:
    return detect.local_networks()


def _read_busy() -> dict[str, int] | None:
    return idle.read_busy()


def _hermes_home() -> Path:
    return _homes.flota_home()


def _home() -> Path:
    return Path.home()


def _hostname() -> str:
    try:
        return socket.gethostname() or "hermes"
    except OSError:
        return "hermes"


def _login_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return _os.environ.get("USER") or _os.environ.get("USERNAME") or ""


def _invites_store() -> Path:
    return _hermes_home() / "flota" / "invites.json"


def _flota_key(configured: str | None) -> tuple[str, str]:
    return pairing.flota_key(configured, _home())


def _local_os() -> str:
    return detect.local_os()


def _local_net(os_name: str) -> dict:
    return detect.local_network(os_name)


def _tailscale() -> tuple[str | None, bool]:
    ip = detect.tailscale_ip()
    return ip, bool(ip) or detect.tailscale_installed()


def _local_crew_names() -> list[str]:
    try:
        return crew.hub_crew_names(crew.local_profiles())
    except Exception:
        return []


def _ssh_run(argv, **kw):
    return subprocess.run(argv, **kw)


def _tcp(host: str, port: int = 22) -> bool:
    return pairing.tcp_open(host, port)


# --- command -----------------------------------------------------------------------------------

def setup_parser(parser) -> None:
    sub = parser.add_subparsers(dest="flota_command", metavar="<command>")
    sub.add_parser("status", help="Show every machine: up/down, wake method, profile")
    for name, text in (("dry-run", "Explain what delegating to a machine would do, sending nothing"),
                       ("wake", "Wake a machine and wait until SSH answers"),
                       ("sleep", "Put a machine to sleep now"),
                       ("off", "Power a machine off (only one Flota can wake again: wol, wol_via or command)")):
        command = sub.add_parser(name, help=text)
        command.add_argument("machine")
        if name in ("sleep", "off"):
            command.add_argument("--force", action="store_true",
                                 help="Even if the gateway reports subagents running on it")
    setup = sub.add_parser(
        "setup", help="Create the profile for a machine and add it to the fleet",
        description="Create (or update) the Hermes profile flota-<name>, whose terminal is SSH on the "
                    "machine, and add the machine to the Flota panel (Plugins > Flota).")
    setup.add_argument("name")
    setup.add_argument("--host", required=True, help="Address the machine answers SSH on")
    setup.add_argument("--user", required=True, help="SSH user")
    setup.add_argument("--key", help="Path to the SSH private key")
    setup.add_argument("--port", type=int, help="SSH port (default 22)")
    setup.add_argument("--description", help="One line the agent reads about this machine")
    setup.add_argument("--wake", choices=WAKE_METHODS,
                       help="How to turn it on (default: wol when a MAC is detected, else none)")
    setup.add_argument("--mac", help="MAC address for Wake-on-LAN (default: detected)")
    setup.add_argument("--broadcast", help="Broadcast address for Wake-on-LAN (default: detected, "
                                           "else 255.255.255.255)")
    setup.add_argument("--sleep", choices=SLEEP_METHODS,
                       help="How auto-sleep turns it off (default: the panel's sleep action)")
    setup.add_argument("--sleep-after", type=_duration, metavar="DURATION",
                       help="Idle time before auto-sleep, e.g. 10m (default: the panel's)")
    setup.add_argument("--os", choices=OS_NAMES, help="linux, macos or windows (default: detected)")
    setup.add_argument("--no-detect", action="store_true",
                       help="Do not probe the machine over SSH for its OS, MAC and broadcast address")
    doctor = sub.add_parser("doctor", help="Check each machine and say how to fix what is wrong")
    doctor.add_argument("machine", nargs="?")
    doctor.add_argument("--fix", action="store_true",
                        help="Also apply safe fixes: hide the flota-* SSH profiles from the Bots roster")
    sub.add_parser("invite", help="Print a code another machine pastes to join this fleet (30 min)")
    join = sub.add_parser("join", help="Join a fleet: trust its flagship's SSH key, print the ship code")
    join.add_argument("code")
    join.add_argument("--yes", action="store_true", help="Do not ask before adding the key")
    sub.add_parser("join-check", help="Check this machine can be reached over SSH by a flagship")
    add_ship = sub.add_parser("add-ship", help="Add the machine whose ship code this is (a machine already "
                                               "in the fleet, same MAC or address, is updated)")
    add_ship.add_argument("code")
    add_ship.add_argument("--name", help="Name it this instead of the ship's hostname")
    sub.add_parser("leave", help="Leave the fleet: remove its flagship's key (default: the current fleet)"
                   ).add_argument("flagship", nargs="?")
    role_cmd = sub.add_parser("role", help="Show or choose this machine's role: flagship, ship or none")
    role_cmd.add_argument("role", nargs="?", choices=("flagship", "ship", "none"))
    role_cmd.add_argument("--force", action="store_true",
                          help="flagship->ship: remove its ships (their profiles are kept); "
                               "ship->flagship: leave its fleet")
    parser.set_defaults(func=handle)


def _duration(value: str) -> str:
    if parse_duration(value, -1.0) < 0:
        raise argparse.ArgumentTypeError(f"not a duration: {value!r} (use e.g. 90s, 10m, 1h)")
    return value


def handle(args) -> int:
    command = getattr(args, "flota_command", None)
    if command == "status":
        return _status()
    elif command in _MACHINE_COMMANDS:
        return _machine_command(command, args.machine, getattr(args, "force", False))
    elif command == "setup":
        try:
            return _setup(args)
        except ValueError as exc:  # core refused the profile name
            print(f"✗ {exc}", file=sys.stderr)
            return 2
        except (RuntimeError, OSError) as exc:  # managed install, unreadable config, failed clone
            print(f"✗ {exc}", file=sys.stderr)
            return 1
    elif command == "doctor":
        try:
            if getattr(args, "fix", False):
                _fix_plumbing()
            return _doctor(args.machine)
        except (RuntimeError, OSError) as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 1
    elif command == "role":
        try:
            return _role_command(args)
        except (ValueError, RuntimeError, OSError) as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 1
    elif command in ("invite", "join", "join-check", "add-ship", "leave"):
        try:
            return _pairing_command(command, args)
        except pairing.PairingError as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 2
        except AlreadyInFleet as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 1
        except (ValueError, RuntimeError, OSError) as exc:
            print(f"✗ {exc}", file=sys.stderr)
            return 1
    else:
        print(_USAGE)
    return 0


def _status() -> int:
    text = slash.make_handler(None)("status")
    busy = _read_busy()
    if busy is not None:  # a live gateway published its counts
        text = re.sub(r"^- ([^:\n]+): .*$", lambda m: f"{m.group(0)} · busy {busy.get(m.group(1), 0)}",
                      text, flags=re.M)
    print(text)
    return 1 if text.startswith("/flota failed:") else 0


def _machine_command(command: str, name: str, force: bool) -> int:
    if command in ("sleep", "off") and not force:
        busy = (_read_busy() or {}).get(name, 0)
        if busy:
            verb = "sleep it" if command == "sleep" else "power it off"
            print(f"✗ {name} is busy with {busy} subagent(s); add --force to {verb} anyway", file=sys.stderr)
            return 1
    try:
        settings = slash.load_settings()
    except Exception as exc:
        print(f"/flota failed: {exc}", file=sys.stderr)
        return 1
    text = slash.handle(f"{command} {name}", settings=settings, probe=slash.is_up, ensure=slash.ensure_ready,
                        profile_exists=slash._profile_exists)
    if name not in load_machines(settings)[0]:
        code = 2
    elif text.startswith("/flota failed:"):
        code = 1
    elif command == "wake":
        code = 0 if text.startswith(f"{name} is up") else 1
    elif command in ("sleep", "off"):  # power() answers "<name>: ... sent" or "<name>: ... failed: ..."
        code = 0 if text.startswith(f"{name}: ") and " failed: " not in text else 1
    else:
        code = 0
    print(text, file=sys.stderr if code else sys.stdout)
    return code


def _legacy_machines(config: Mapping) -> Mapping:
    block = config
    for key in ("delegation", "backends", "flota", "machines"):
        block = block.get(key) if isinstance(block, Mapping) else None
    return block if isinstance(block, Mapping) else {}


def _child(parent: dict, key: str) -> dict:
    if not isinstance(parent.get(key), dict):
        parent[key] = {}
    return parent[key]


def _setup(args) -> int:
    code, _ = run_setup(args, emit=_print)
    return code


def _print(text: str, err: bool = False) -> None:
    print(text, file=sys.stderr if err else sys.stdout)


def run_setup(args, emit: Callable[[str, bool], None] | None = None) -> tuple[int, list[str]]:
    """What ``hermes flota setup`` does, for the CLI and the dashboard. ``args`` carries the setup
    flags as attributes (argparse namespace or any object; missing optional ones count as unset).
    Every message goes to ``emit(text, is_error)`` and is returned in ``lines``. Raises what core
    raises (ValueError for a refused profile name, RuntimeError / OSError for a refused write)."""
    lines: list[str] = []

    def say(text: str, err: bool = False) -> None:
        lines.append(text)
        if emit:
            emit(text, err)

    args = _SetupArgs(args)
    code = _run_setup(args, say)
    return code, lines


class _SetupArgs:
    """Optional setup flags default to None (``no_detect`` to False) when the caller left them out."""

    def __init__(self, args) -> None:
        self._args = args

    def __getattr__(self, key):
        return getattr(self._args, key, False if key == "no_detect" else None)


def _run_setup(args, say) -> int:
    name = args.name.strip()
    active = _active_config_path()
    config = _read_config(active)
    panel = plugin_settings_from_config(config)
    panel_machines = panel.get("machines") if isinstance(panel.get("machines"), Mapping) else {}
    # effective() ignores the old block once the panel has machines, so the first panel write
    # carries the old machines over. The old block itself is left as it is.
    copied = {} if panel_machines else dict(_legacy_machines(config))
    existing = {**copied, **panel_machines}
    old = existing.get(name)
    # A machine keeps the profile it has (a hand-edited one included); a new one gets flota-<name>.
    kept = old.get("profile") if isinstance(old, Mapping) else None
    profile = kept.strip() if isinstance(kept, str) and kept.strip() else f"flota-{name}"
    # Keys this command has no flag for (via, wake_command, ...) survive a re-run.
    entry = {**(old if isinstance(old, Mapping) else {}), "profile": profile, "host": args.host.strip(),
             "user": args.user}
    entry.update({key: getattr(args, key) for key in _OPTIONAL if getattr(args, key) is not None})
    entry.update(args.extra or {})  # dashboard / pairing only (e.g. crew_names); no CLI flag
    entry.setdefault("port", 22)
    _detect(args, name, profile, entry, say)
    entry.setdefault("wake", "none")

    loaded, problems = load_machines(effective(None, {**panel, "machines": {**existing, name: entry}}))
    # A `sleep: none` machine gets the panel's sleep_after by default, which load_machines flags;
    # only an explicit sleep_after makes that a mistake.
    problems = [p for p in problems if p.startswith(f"machine '{name}':")
                and ("sleep_after" in entry or "sleep_after needs" not in p)]
    if problems:
        say("\n".join(f"✗ {p}" for p in problems), True)
        if entry["wake"] in ("wol_via", "command"):
            needs = "via" if entry["wake"] == "wol_via" else "wake_command"
            say(f"  `setup` has no flag for '{needs}': add it to machine '{name}' in Plugins > Flota "
                  f"(plugins.entries.flota.settings.machines.{name} in {active}), then run this again.",
                  True)
        say("Nothing was written.", True)
        return 2

    if _profile_exists(profile):
        profile_dir = _profile_dir(profile)
        say(f"Profile '{profile}' already exists; kept it.")
    else:
        profile_dir = _create_profile(profile)
        say(f"Created profile '{profile}' from the active profile's config.")

    def set_terminal(data: dict) -> None:
        terminal = _child(data, "terminal")
        terminal.update(backend="ssh", ssh_host=entry["host"], ssh_user=args.user, ssh_port=entry["port"])
        if args.key is not None:
            terminal["ssh_key"] = args.key

    _update_config(profile_dir / "config.yaml", set_terminal)
    _mark_owner(profile_dir, active.parent)
    if profile.startswith(_homes.PROFILE_PREFIX):  # plumbing, not a bot: keep it out of the Bots roster
        _hide_plumbing(profile_dir, name)
    say(f"Its terminal is SSH {args.user}@{entry['host']}:{entry['port']}"
          f"{f' (key {args.key})' if args.key else ''}.")

    # No --sleep still means auto-sleep (the panel default suspends), so check how it can sleep.
    if args.sleep != "none":
        _check_sleep(loaded[name], entry, say)

    def set_machine(data: dict) -> None:
        block = data
        for key in ("plugins", "entries", PLUGIN_ID, "settings", "machines"):
            block = _child(block, key)
        block.update(copied)
        block[name] = entry

    _update_config(active, set_machine)
    if copied:
        say(f"Copied {len(copied)} machine(s) from delegation.backends.flota.machines into the Flota "
              "panel; the old block is left as it is.")
    say(f"Saved machine '{name}' (wake: {entry['wake']}, sleep: {entry.get('sleep', 'panel default')}) "
          f"in the Flota panel of {active}.")
    say("Next: `hermes config set delegation.backend flota` to enable it; tune sleep and power in "
          f"Plugins > Flota; `/flota` in a chat (`/flota dry-run {name}` shows what a delegation would do).")
    return 0


def _check_sleep(machine, entry: dict, say=None) -> None:
    say = say or _print
    """Find how this machine can be suspended without a password and record it in ``entry``."""
    if entry.get("sleep") == "poweroff":
        say("Note: sleep 'poweroff' only applies to a machine Flota can wake again (Wake-on-LAN or a wake "
              "command) while \"Allow power off\" is on in Plugins > Flota (otherwise it suspends), and "
              "waking from power-off needs Wake-on-LAN from S5 in the BIOS.")
    prefix, explanation = _detect_prefix(machine)
    if prefix == "":
        entry.pop("power_prefix", None)
        say(f"Sleep check: {explanation}.")
    elif prefix is not None:
        entry["power_prefix"] = prefix
        say(f"Sleep check: {explanation}; Flota will run systemctl inside that graphical session.")
    elif explanation.startswith("unreachable"):
        say(f"Sleep check skipped, {machine.name} is {explanation}. Auto-sleep may fail; run this "
              "again once it is on.")
    else:
        say(f"{explanation}\n\nThe machine is saved, but auto-sleep will fail until that rule is installed.")


def _detect(args, name: str, profile: str, entry: dict, say=None) -> None:
    """Fill os / mac / broadcast / wake in ``entry`` from the machine itself. Flags always win."""
    say = say or _print
    if args.no_detect:
        return
    port = entry["port"]
    if not entry["host"] or isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return  # validation reports it
    probe = Machine(name=name, profile=profile, host=entry["host"], port=port, user=args.user,
                    key=entry.get("key") or "")
    if not _is_up(probe):
        say(f"Detection skipped: {entry['host']}:{port} is not answering. Saving what was given; run this "
              "again once it is on to fill in the MAC and broadcast address.")
        return
    os_name = _detect_os(probe)
    if os_name and args.os is None:
        entry["os"] = os_name
    net = _detect_network(probe, os_name or entry.get("os", ""))
    if args.mac is None and net.get("mac"):
        entry["mac"] = net["mac"]
    if args.broadcast is None and net.get("broadcast"):
        entry["broadcast"] = net["broadcast"]
    if args.wake is None and entry.get("wake", "none") == "none" and net.get("mac"):
        entry["wake"] = "wol"
    found = [os_name or "OS unknown", net.get("interface"), net.get("mac") and f"mac {net['mac']}",
             net.get("broadcast") and f"broadcast {net['broadcast']}",
             net.get("interface") and f"wake-on-lan {net.get('wol', 'unknown')}"]
    say("detected: " + " · ".join(part for part in found if part))
    if net.get("reason"):
        say(f"detection: {net['reason']}")
    if net.get("wol") == "disabled":
        say("Wake-on-LAN is off on that interface. To turn it on:")
        say("\n".join(f"  {line}" for line in detect.enable_wol_hint(entry.get("os", ""), net)))


# --- doctor ------------------------------------------------------------------------------------

def _fix_plumbing() -> int:
    """``doctor --fix``: hide every flota-* SSH profile from the Desktop Bots roster."""
    changed = 0
    for name, profile_dir in _plumbing_profiles():
        if _hide_plumbing(profile_dir, name[len(_homes.PROFILE_PREFIX):]):
            changed += 1
            print(f"✓ {name}: hidden from the Bots roster (it is Flota's SSH terminal, not a bot)")
        else:
            print(f"✓ {name}: already hidden from the Bots roster")
    return changed


def _doctor(only: str | None) -> int:
    config = _read_config(_active_config_path())
    settings = effective(config.get("delegation"), plugin_settings_from_config(config))
    loaded, problems = load_machines(settings)
    if only:
        problems = [p for p in problems if p.startswith(f"machine '{only}':")]
        if only not in loaded:
            print("\n".join([f"✗ {p}" for p in problems] or [f"✗ no machine named '{only}'"]), file=sys.stderr)
            return 2
    print("\n".join(f"✗ {p}" for p in problems))
    if not loaded:
        print("No machines yet: hermes flota setup <name> --host HOST --user USER")
        return 1
    failed = bool(problems)
    for name in [only] if only else list(loaded):
        raw = settings["machines"].get(name) or {}
        lines = _checks(loaded[name], str(raw.get("os") or ""))
        print(f"{name}  {_target(loaded[name])}:{loaded[name].port}")
        for mark, text, hints in lines:
            print(f"  {mark} {text}")
            print("".join(f"      → {hint}\n" for hint in hints), end="")
        failed = failed or any(mark == "✗" for mark, _, _ in lines)
    return 1 if failed else 0


def _target(machine) -> str:
    return f"{machine.user}@{machine.host}" if machine.user else machine.host


def _checks(m, os_name: str) -> list[tuple[str, str, list[str]]]:
    setup = f"hermes flota setup {m.name} --host {m.host}" + (f" --user {m.user}" if m.user else "")
    out = []
    reachable = False
    if not _is_up(m):
        out.append(("✗", f"SSH: {m.host}:{m.port} is not answering",
                    [f"is it on? `hermes flota wake {m.name}`" if m.wake != "none" else "turn it on by hand"]))
    else:
        reachable, error = _ssh_ok(m)
        if reachable:
            out.append(("✓", "SSH reachable", []))
        else:
            port = f" -p {m.port}" if m.port != 22 else ""
            out.append(("✗", f"SSH: port open but login failed: {error}", [f"ssh-copy-id{port} {_target(m)}"]))

    if not _profile_exists(m.profile):
        out.append(("✗", f"profile {m.profile} is missing", [setup]))
    else:
        terminal = _read_config(_profile_dir(m.profile) / "config.yaml").get("terminal") or {}
        backend, host = terminal.get("backend"), terminal.get("ssh_host")
        if backend == "ssh" and host == m.host:
            out.append(("✓", f"profile {m.profile} → ssh {_target(m)}", []))
        else:
            out.append(("✗", f"profile {m.profile} terminal is {backend or 'local'} {host or ''}".rstrip()
                        + f", not ssh {m.host}", [setup]))
    if _file_sync_guarded():
        out.append(("✓", "SSH file sync off for flota profiles (the machine's ~/.hermes is never copied)", []))
    else:
        out.append(("✗", "SSH file sync is on: after each subagent Hermes tars the machine's whole ~/.hermes "
                    "back (timeouts, GBs of RAM and disk)",
                    ["update Hermes and Flota; this core's SSHEnvironment has no sync_files parameter"]))

    if m.wake == "none":
        out.append(("✗", "no wake method", [f"while it is on, run `{setup}` to detect the MAC"]))
    else:
        detail = {"wol": f"mac {m.mac}, broadcast {m.broadcast}", "wol_via": f"via {m.via}, mac {m.mac}",
                  "command": m.wake_command}[m.wake]
        out.append(("✓", f"wake: {m.wake} ({detail})", []))

    if m.wake in ("wol", "wol_via"):
        out.append(_wol_check(m, os_name, reachable, setup))
        out.append(("?", "power off: waking from power-off (S5) needs Wake-on-LAN from S5 enabled in the "
                    "BIOS (not checked; test once with `hermes flota off` then `hermes flota wake`)", []))

    if m.sleep_command:
        out.append(("✓", "sleep: uses its sleep_command", []))
    elif m.sleep != "none":
        if not reachable:
            out.append(("?", "sleep: not checked (unreachable)", []))
        else:
            prefix, explanation = _detect_prefix(m)
            if prefix is None and explanation.startswith("unreachable"):
                out.append(("?", f"sleep: not checked ({explanation})", []))
            elif prefix is None:
                out.append(("✗", "sleep: needs a password over SSH",
                            [explanation.splitlines()[0], f"`{setup}` prints the rule to install"]))
            elif prefix != m.power_prefix:
                out.append(("✗", f"sleep: works {explanation}, but the saved power_prefix differs",
                            [f"run `{setup}` again"]))
            else:
                out.append(("✓", f"can sleep without a password ({explanation})", []))

    if m.wake == "wol":
        out.append(_lan_check(m))
    return out


def _wol_check(m, os_name: str, reachable: bool, setup: str) -> tuple[str, str, list[str]]:
    if not reachable:
        return "?", "Wake-on-LAN: not checked (unreachable)", []
    net = _detect_network(m, os_name)
    where = f" on {net['interface']}" if net.get("interface") else ""
    if net.get("mac") and net["mac"] != m.mac.lower().replace("-", ":"):
        return "✗", f"Wake-on-LAN: the LAN MAC is {net['mac']}, the config has {m.mac}", [f"run `{setup}` again"]
    if net.get("wol") == "enabled":
        return "✓", f"Wake-on-LAN enabled{where}", []
    if net.get("wol") == "disabled":
        return "✗", f"Wake-on-LAN disabled{where}", detect.enable_wol_hint(os_name, net)
    return "?", f"Wake-on-LAN: could not read it{where}" + (f" ({net['reason']})" if net.get("reason") else ""), []


def _lan_check(m) -> tuple[str, str, list[str]]:
    if m.broadcast == "255.255.255.255":
        return "?", "LAN: broadcast 255.255.255.255 only reaches this host's own LAN", [
            "set --broadcast (or let setup detect it) to check"]
    nets = _local_networks()
    if not nets:
        return "?", "LAN: could not list this host's networks", []
    lan = detect.same_lan(m.broadcast, nets)
    if lan:
        return "✓", f"this host is on the machine's LAN ({lan})", []
    return "✗", f"this host is not on the LAN of {m.broadcast}", [
        "magic packets do not cross routers or Tailscale: use wake wol_via with 'via' set to an "
        "always-on machine on that LAN (Plugins > Flota)"]


# --- role and pairing (shared by the CLI and the dashboard) ------------------------------------------

def _panel() -> Mapping:
    return plugin_settings_from_config(_read_config(_active_config_path()))


def _set_panel(key: str, value) -> None:
    def mutate(data: dict) -> None:
        block = data
        for part in ("plugins", "entries", PLUGIN_ID, "settings"):
            block = _child(block, part)
        if value is None:
            block.pop(key, None)
        else:
            block[key] = value

    _update_config(_active_config_path(), mutate)


def role(panel: Mapping | None = None) -> str | None:
    """'flagship' | 'ship' | None: the chosen role, else inferred from machines / joined fleets."""
    panel = _panel() if panel is None else panel
    chosen = panel.get("role")
    if chosen in ROLES:
        return chosen
    if isinstance(panel.get("machines"), Mapping) and panel["machines"]:
        return "flagship"
    if isinstance(panel.get("joined_fleets"), list) and panel["joined_fleets"]:
        return "ship"
    return None


class RoleConflict(Exception):
    """A role change that needs something undone first; ``needs`` names it ('remove_ships' or
    'leave_fleet') and ``extra`` carries what is in the way."""

    def __init__(self, message: str, needs: str, **extra) -> None:
        super().__init__(message)
        self.needs, self.extra = needs, extra


class AlreadyInFleet(Exception):
    """This machine already belongs to another flagship's fleet (a ship joins at most one)."""

    def __init__(self, fleet: Mapping) -> None:
        super().__init__(f"already in a fleet: {fleet.get('flagship')} (leave it first: "
                         f"hermes flota leave)")
        self.fleet = dict(fleet)


def fleet(panel: Mapping | None = None) -> dict | None:
    """``{flagship, joined_at, ...}`` of the fleet this machine belongs to, or None. A ship belongs
    to one fleet; an older config listing several reports the last one joined."""
    panel = _panel() if panel is None else panel
    joined = [f for f in (panel.get("joined_fleets") or []) if isinstance(f, Mapping) and f.get("flagship")]
    if not joined:
        return None
    last = dict(joined[-1])
    last.setdefault("joined_at", None)
    return last


def _ships(config: Mapping) -> dict:
    return _panel_machines_of(config)[1]


def ships_count(config: Mapping | None = None) -> int:
    """How many machines (ships) this Hermes leads: the panel's, else the old delegation block's."""
    return len(_ships(_read_config(_active_config_path()) if config is None else config))


def _remove_ships() -> list[str]:
    """Drop every machine from the panel and from the old ``delegation.backends.flota`` block
    (an empty panel would fall back to it). Their profiles are kept."""
    names = sorted(_ships(_read_config(_active_config_path())))

    def mutate(data: dict) -> None:
        block = data
        for part in ("plugins", "entries", PLUGIN_ID, "settings"):
            block = _child(block, part)
        block.pop("machines", None)
        old = data.get("delegation", {}).get("backends", {}).get(PLUGIN_ID) \
            if isinstance(data.get("delegation"), dict) and isinstance(data["delegation"].get("backends"), dict) \
            else None
        if isinstance(old, dict):
            old.pop("machines", None)

    _update_config(_active_config_path(), mutate)
    return names


def set_role(value, force: bool = False) -> dict:
    """Choose this machine's role ('flagship' | 'ship' | None to clear the choice). A flagship
    with ships becomes a ship only with ``force`` (its ships are removed, their profiles kept); a
    ship in a fleet becomes a flagship only with ``force`` (it leaves the fleet). Otherwise
    RoleConflict. Returns ``{role, removed_ships, left_fleet, removed_keys}``."""
    if value == "none":
        value = None
    if value is not None and value not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)} or null")
    result = {"role": value, "removed_ships": [], "left_fleet": None, "removed_keys": 0}
    if value == "ship":
        ships = sorted(_ships(_read_config(_active_config_path())))
        if ships and not force:
            raise RoleConflict(f"this Hermes leads {len(ships)} ship(s) ({', '.join(ships)}); remove them "
                               "first, or force it (their SSH profiles are kept)", "remove_ships", ships=ships)
        if ships:
            result["removed_ships"] = _remove_ships()
    elif value == "flagship":
        current = fleet()
        if current and not force:
            raise RoleConflict(f"this machine is a ship of {current['flagship']}; leave that fleet first, "
                               "or force it", "leave_fleet", fleet=current)
        if current:
            result["removed_keys"] = leave_fleet(current["flagship"])
            result["left_fleet"] = current["flagship"]
    _set_panel("role", value)
    return result


def run_invite() -> dict:
    pubkey, _ = _flota_key(_panel().get("ssh_key") or None)
    return pairing.make_invite(flagship=_hostname(), user=_login_user(), pubkey=pubkey, store=_invites_store())


def _local_facts() -> dict:
    os_name = _local_os()
    net = _local_net(os_name)
    ts, installed = _tailscale()
    return {"os": os_name, "net": net, "tailscale": ts, "tailscale_installed": installed,
            "lan": net.get("ip") or None, "hostname": _hostname(), "user": _login_user()}


def run_join(code) -> dict:
    """Ship side: trust the invite's key and build the ship code. The caller confirmed first.
    A ship belongs to one fleet: AlreadyInFleet when it is in another flagship's (joining the
    same flagship again refreshes its key)."""
    invite = pairing.read_invite(code)
    current = fleet()
    if current and current.get("flagship") != invite["flagship"]:
        raise AlreadyInFleet(current)
    added, path = pairing.authorize(invite["pubkey"], invite["flagship"], _home())
    fp = pairing.fingerprint(invite["pubkey"])
    lines = [(f"Added the key of {invite['flagship']} ({fp}) to {path}" if added
              else f"The key of {invite['flagship']} ({fp}) was already in {path}"),
             "It is a normal login key (subagents need a shell); `hermes flota leave "
             f"{invite['flagship']}` removes it."]
    facts = _local_facts()
    payload = pairing.ship_payload(invite, facts, _local_crew_names())
    hosts = [payload["host"], *payload["alt_hosts"]]
    lines.append(f"Reachable as {payload['user']}@{' or '.join(hosts)}" +
                 ("" if facts["tailscale"] else " (no Tailscale: only from this LAN)"))
    joined = {"flagship": invite["flagship"], "pubkey_fingerprint": fp,
             "joined_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    _set_panel("joined_fleets", [joined])
    if _panel().get("role") not in ROLES:
        _set_panel("role", "ship")
    lines.append("Paste the ship code on the flagship (Flota > Add a ship).")
    return {"ship_code": pairing.encode(pairing.SHIP_PREFIX, payload), "lines": lines, "fleet": joined,
            "checks": join_check(facts)["checks"]}


def join_check(facts: dict | None = None) -> dict:
    facts = facts or _local_facts()
    checks = pairing.join_checks(os_name=facts["os"], net=facts["net"], tailscale=facts["tailscale"],
                                 tailscale_installed=facts["tailscale_installed"], home=_home(), tcp=_tcp)
    return {"checks": checks, "addresses": {"tailscale": facts["tailscale"], "lan": facts["lan"]},
            "os": facts["os"], "wake_options": wake_options(facts["net"])}


def wake_options(net: Mapping) -> list[dict]:
    """How a flagship could turn this machine on: ``[{id, ok, label}]`` for 'wol', 'wol_via',
    'timer' (it arms its own RTC wake alarm; only when this Flota has waketimer) and 'none'."""
    net = net if isinstance(net, Mapping) else {}
    state = net.get("wol", "unknown")
    if not net.get("mac"):
        ok, why = False, f"no wired LAN interface found ({net.get('reason') or 'unknown'})"
    elif state == "enabled":
        ok, why = True, f"Wake-on-LAN is on ({net.get('interface')})"
    elif state == "disabled":
        ok, why = False, f"Wake-on-LAN is off on {net.get('interface')}"
    else:
        ok, why = None, f"Wake-on-LAN state unknown on {net.get('interface')}"
    options = [
        {"id": "wol", "ok": ok, "label": f"Wake-on-LAN from a flagship on this LAN: {why}"},
        {"id": "wol_via", "ok": ok, "label": "Wake-on-LAN relayed by an always-on machine on this LAN "
                                             f"(for a flagship elsewhere): {why}"},
    ]
    timer = _wake_timer()
    if timer is not None:
        reason = str(timer.get("reason") or "").strip()
        options.append({"id": "timer", "ok": bool(timer.get("ok")),
                        "label": "Wakes itself on a timer (RTC alarm) before scheduled work" +
                                 ("" if timer.get("ok") else f": {reason or 'not available'}")})
    options.append({"id": "none", "ok": True, "label": "Turned on by hand"})
    return options


def fleets() -> list[dict]:
    return [dict(f) for f in (_panel().get("joined_fleets") or []) if isinstance(f, Mapping)]


def leave_fleet(flagship: str | None = None) -> int:
    """Leave ``flagship``'s fleet (default: the current one): remove its authorized_keys line(s),
    forget the fleet and clear the role choice. KeyError when not in that fleet."""
    known = fleets()
    if flagship is None:
        current = fleet()
        if current is None:
            raise KeyError("no fleet")
        flagship = current["flagship"]
    if not any(f.get("flagship") == flagship for f in known):
        raise KeyError(flagship)
    removed = pairing.deauthorize(flagship, _home())
    rest = [f for f in known if f.get("flagship") != flagship]
    _set_panel("joined_fleets", rest or None)
    _set_panel("role", None)
    return removed


def _same_machine(ship: dict, entry) -> bool:
    """The ship code describes this configured machine: same MAC, or one of its addresses."""
    if not isinstance(entry, Mapping):
        return False
    mac = str(entry.get("mac") or "").strip().lower().replace("-", ":")
    if ship["mac"] and mac == ship["mac"]:
        return True
    alt = entry.get("alt_hosts") if isinstance(entry.get("alt_hosts"), list) else []
    known = {str(h).strip().lower() for h in [entry.get("host"), *alt] if h}
    return any(h.lower() in known for h in ship["hosts"])


def _ship_target(ship: dict, name: str | None, say) -> tuple[str, Mapping | None]:
    """``(name, existing entry or None)`` the ship is saved as: the machine already in the fleet with
    its MAC or an address (its name and profile kept), else ``name`` / the ship's suggested name."""
    if name is not None and not pairing._NAME.match(name):
        raise pairing.PairingError("invalid name: lowercase letters, digits, '-' or '_' (it names the "
                                   "profile flota-<name>)")
    _, existing = _panel_machines_of(_read_config(_active_config_path()))
    for known, entry in existing.items():
        if _same_machine(ship, entry):
            say(f"This ship is already in the fleet as '{known}' (same "
                f"{'MAC' if ship['mac'] and str(entry.get('mac') or '').lower() == ship['mac'] else 'address'}): "
                f"updating it" + (f" (--name {name} ignored)" if name and name != known else "") + ".")
            return known, entry
    if name is not None:
        if name in existing:
            raise pairing.PairingError(f"a different machine is already named '{name}'; pick another name")
        return name, None
    chosen, n = ship["name"], 2
    while chosen in existing:
        chosen, n = f"{ship['name'][:54]}-{n}", n + 1
    if chosen != ship["name"]:
        say(f"A different machine is already named '{ship['name']}': saving this one as '{chosen}'.")
    return chosen, None


def _panel_machines_of(config: Mapping) -> tuple[dict, dict]:
    panel = plugin_settings_from_config(config)
    machines = panel.get("machines") if isinstance(panel.get("machines"), Mapping) else {}
    copied = {} if machines else dict(_legacy_machines(config))
    return copied, {**copied, **machines}


def run_add_ship(code, emit: Callable[[str, bool], None] | None = None, name: str | None = None) -> dict:
    """Flagship side: check the ship code, find an address that logs in, then run setup on it. A ship
    already in the fleet (same MAC, or one of its addresses) updates that machine; ``name`` overrides
    the ship's suggested name for a new one."""
    store = _invites_store()
    ship = pairing.read_ship(code, store)
    lines: list[str] = []

    def say(text: str, err: bool = False) -> None:
        lines.append(text)
        if emit:
            emit(text, err)

    target, old = _ship_target(ship, name, say)

    _, key = _flota_key(_panel().get("ssh_key") or None)
    host, tried = pairing.choose_host(ship, key=key, run=_ssh_run, tcp=_tcp)
    for line in tried:
        say(line, line.startswith("✗"))
    if host is None:
        say("Nothing was saved. Fix it on the ship and add the same ship code again (it stays valid "
            "until the invite expires).", True)
        return {"ok": False, "lines": lines, "problems": ["no address of the ship accepted SSH"],
                "machine": target}
    nets = _local_networks()
    if ship["mac"] and ship["broadcast"] and detect.same_lan(ship["broadcast"], nets):
        wake = "wol"
        say(f"Wake-on-LAN: this host is on the ship's LAN ({ship['broadcast']}), magic packets will reach it.")
    else:
        wake = "none"
        say("Wake-on-LAN: this host is not on the ship's LAN, so magic packets cannot reach it; wake is "
            "'none'. To wake it remotely set wake 'wol_via' with 'via' = an always-on machine on its LAN "
            "(Plugins > Flota).")
    if old is not None and old.get("wake") not in (None, "none", "wol"):
        say(f"Keeping its wake method '{old['wake']}'.")
        wake = None  # wol_via / command were chosen by hand; run_setup keeps them
    params = SimpleNamespace(name=target, host=host, user=ship["user"], key=key, port=ship["port"],
                             wake=wake, mac=ship["mac"] or None, broadcast=ship["broadcast"] or None,
                             os=ship["os"], extra={"crew_names": ship["crew_names"],
                                                   "alt_hosts": [h for h in ship["hosts"] if h != host]})
    code_, _ = run_setup(params, emit=say)
    problems = [line[2:] for line in lines if line.startswith("✗ machine")]
    if code_ == 0:
        pairing.spend(ship["nonce"], store)
    return {"ok": code_ == 0, "lines": lines, "problems": problems, "machine": target}


def _pairing_command(command: str, args) -> int:
    if command == "invite":
        invite = run_invite()
        print(f"Invite from {invite['flagship']} (valid until {invite['expires_at']}). On the machine that "
              "joins: hermes flota join <code>, or paste it in its Flota page.\n")
        print(invite["code"])
        return 0
    if command == "join":
        invite = pairing.read_invite(args.code)
        if not args.yes:
            question = (f"Let {invite['flagship']} ({invite['user'] or '?'}) log in to this machine as "
                        f"{_login_user()} with key {pairing.fingerprint(invite['pubkey'])}? [y/N] ")
            if not sys.stdin.isatty() or input(question).strip().lower() not in ("y", "yes"):
                print("Not joined.", file=sys.stderr)
                return 1
        result = run_join(args.code)
        print("\n".join(result["lines"]))
        for check in result["checks"]:
            if check["ok"] is not True:
                mark = "✗" if check["ok"] is False else "?"
                print(f"  {mark} {check['label']}" + (f"\n      → {check['fix']}" if check["fix"] else ""))
        print(f"\n{result['ship_code']}")
        return 0
    if command == "join-check":
        result = join_check()
        marks = {True: "✓", False: "✗", None: "?"}
        for check in result["checks"]:
            print(f"{marks[check['ok']]} {check['label']}" + (f"\n    → {check['fix']}" if check["fix"] else ""))
        return 1 if any(c["ok"] is False for c in result["checks"]) else 0
    if command == "add-ship":
        result = run_add_ship(args.code, emit=_print, name=getattr(args, "name", None))
        return 0 if result["ok"] else 1
    flagship = args.flagship or (fleet() or {}).get("flagship")
    try:
        removed = leave_fleet(flagship)
    except KeyError:
        print(f"✗ not in a fleet led by {args.flagship!r}" if args.flagship else "✗ not in a fleet",
              file=sys.stderr)
        return 2
    print(f"Left the fleet of {flagship}: removed {removed} key line(s) from this account's SSH authorized_keys.")
    return 0


def _role_command(args) -> int:
    if args.role is None:
        config = _read_config(_active_config_path())
        panel = plugin_settings_from_config(config)
        current, joined, count = role(panel), fleet(panel), ships_count(config)
        chosen = "" if panel.get("role") in ROLES else " (inferred)" if current else ""
        print(f"Role: {current or 'none'}{chosen}")
        print(f"Fleet: {joined['flagship']} (joined {joined.get('joined_at') or '?'})" if joined else "Fleet: none")
        print(f"Ships: {count}")
        return 0
    try:
        result = set_role(args.role, force=args.force)
    except RoleConflict as exc:
        hint = ("hermes flota leave" if exc.needs == "leave_fleet"
                else f"hermes flota role {args.role} --force")
        print(f"✗ {exc}\n  → {hint}", file=sys.stderr)
        return 1
    if result["removed_ships"]:
        print(f"Removed {len(result['removed_ships'])} ship(s): {', '.join(result['removed_ships'])} "
              "(their SSH profiles are kept; `hermes profile delete` removes them).")
    if result["left_fleet"]:
        print(f"Left the fleet of {result['left_fleet']} ({result['removed_keys']} key line(s) removed).")
    print(f"Role: {result['role'] or 'none'}")
    return 0
