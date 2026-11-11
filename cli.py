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
import shlex
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
from . import machines as machines_mod
from .machines import OS_NAMES, SLEEP_METHODS, WAKE_METHODS, Machine, load_machines, parse_duration
from .settings import PLUGIN_ID, effective, plugin_settings_from_config

_USAGE = ("usage: hermes flota {status | dry-run | wake | sleep | off <machine> | "
          "setup USER@HOST | setup <name> --host HOST --user USER [options] | remove <machine> | "
          "doctor [machine] | invite | join <invite> | join-check | add-ship <ship code> | "
          "leave [flagship] | role [flagship|ship|none] [--force] | uninstall}")
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


def _mark_adopted(profile_dir: Path) -> None:
    path = Path(profile_dir) / _homes.ADOPTED_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("set up by Flota on an existing profile\n")
    except OSError as exc:
        print(f"flota: could not write {path}: {exc}", file=sys.stderr)


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


def _key_is_file(path: str) -> bool:
    return Path(_os.path.expanduser(path)).is_file()


def _flota_made(profile: str) -> bool:
    """Flota created this profile: a ``flota-*`` name carrying Flota's owner marker, not adopted."""
    return (profile.startswith(_homes.PROFILE_PREFIX) and _profile_exists(profile)
            and (_profile_dir(profile) / _homes.OWNER_FILE).is_file()
            and not (_profile_dir(profile) / _homes.ADOPTED_FILE).is_file())


def _delete_profile(profile: str) -> None:
    from hermes_cli.profiles import delete_profile
    delete_profile(profile, yes=True)


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
    add = sub.add_parser
    # --help of a subcommand shows its one-line help as the description too.
    sub.add_parser = lambda name, **kw: add(name, **{"description": kw.get("help"), **kw})
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
    setup.add_argument("name", help="A name for the machine, or USER@HOST (the name is then taken from the host)")
    setup.add_argument("--host", help="Address the machine answers SSH on (USER@HOST also works)")
    setup.add_argument("--user", help="SSH user")
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
    remove = sub.add_parser("remove", help="Remove a machine from the fleet, and its flota-* profile if "
                                           "Flota created it")
    remove.add_argument("machine")
    remove.add_argument("--force", action="store_true", help="Even if the gateway reports subagents running on it")
    remove.add_argument("--keep-profile", action="store_true", help="Leave its Hermes profile in place")
    uninstall = sub.add_parser("uninstall", help="Undo what Flota did on this machine (shows the plan; "
                                                 "--yes does it), before `hermes plugins remove flota`")
    uninstall.add_argument("--yes", action="store_true", help="Do it instead of listing it")
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
    add_ship.add_argument("--replace", action="store_true",
                          help="Let the code re-point a machine already in the fleet to a new address or user")
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
    elif command in ("remove", "uninstall"):
        try:
            return _remove(args) if command == "remove" else _uninstall(args.yes)
        except (ValueError, RuntimeError, OSError) as exc:
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
        print(_USAGE + "\n")
        try:
            print("\n".join(_guide()))
        except Exception as exc:  # an unreadable config must not hide the usage line
            print(f"✗ {exc}", file=sys.stderr)
    return 0


def _backend_on(config: Mapping) -> bool:
    delegation = config.get("delegation")
    return isinstance(delegation, Mapping) and delegation.get("backend") == PLUGIN_ID


def _guide() -> list[str]:
    """Where this Hermes stands and the one command to run next (bare ``hermes flota``)."""
    config = _read_config(_active_config_path())
    panel = plugin_settings_from_config(config)
    joined, names = fleet(panel), list(_ships(config))
    stray = [] if joined else pairing.flagships(_home())
    lines = []
    if stray:
        lines.append(f"⚠ No fleet is recorded, but {', '.join(stray)} still has a login key in this account's "
                     "SSH authorized_keys; `hermes flota leave` removes it.")
    if joined and not names:
        return lines + [f"This Hermes is a ship in the fleet of {joined['flagship']}; machines are managed on "
                        "the flagship.",
                        "Next: nothing to do here. `hermes flota join-check` checks the flagship can reach it; "
                        "`hermes flota leave` leaves the fleet."]
    if not names:
        return lines + ["No machines yet. Add the first one, either way:",
                        "  hermes flota setup USER@HOST    (you can already `ssh USER@HOST` from here)",
                        "  hermes flota invite             (then paste the line it prints on the other machine)",
                        "Next: hermes flota setup USER@HOST"]
    lines.append(f"{len(names)} machine(s): {', '.join(names)}. Delegation backend: "
                 f"{'flota (on)' if _backend_on(config) else 'not flota yet'}.")
    if not _backend_on(config):
        return lines + ["Next: hermes config set delegation.backend flota   (then: hermes gateway restart)"]
    return lines + ["Next: hermes flota doctor   (checks every machine); `hermes flota status` shows which are up"]


def _status() -> int:
    try:
        config = _read_config(_active_config_path())
        joined = fleet(plugin_settings_from_config(config))
        if joined and not _ships(config):
            print(f"This Hermes is a ship in the fleet of {joined['flagship']}; machines are managed on the "
                  "flagship.")
            return 0
    except Exception:  # the slash text below reports an unreadable config
        pass
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
    loaded = load_machines(settings)[0]
    same = [known for known in loaded if known.lower() == name.lower()]
    if name not in loaded and len(same) == 1:
        name = same[0]
    if loaded and name not in loaded:
        print(f"No machine named '{name}'.\nMachines: {', '.join(loaded)}\n"
              f"Next: hermes flota {command} <machine>, or `hermes flota status`", file=sys.stderr)
        return 2
    text = slash.handle(f"{command} {name}", settings=settings, probe=slash.is_up, ensure=slash.ensure_ready,
                        profile_exists=slash._profile_exists)
    if name not in loaded:
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


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "-", text.lower()).strip("-_")[:58]


def _run_setup(args, say) -> int:
    raw, host, user = (str(getattr(args, key) or "").strip() for key in ("name", "host", "user"))
    auto = not host and "@" in raw
    if auto:  # setup USER@HOST: the name comes from the host
        host = raw
    if "@" in host:
        login, _, host = host.rpartition("@")
        user = user or login
    if auto:
        raw = host if re.fullmatch(r"[0-9.]+|.*:.*", host) else host.split(".")[0]
    args.host, args.user = host, user
    active = _active_config_path()
    config = _read_config(active)
    panel = plugin_settings_from_config(config)
    panel_machines = panel.get("machines") if isinstance(panel.get("machines"), Mapping) else {}
    # effective() ignores the old block once the panel has machines, so the first panel write
    # carries the old machines over. The old block itself is left as it is.
    copied = {} if panel_machines else dict(_legacy_machines(config))
    existing = {**copied, **panel_machines}
    same = [known for known in existing if known.lower() == raw.lower()]
    name = raw if raw in existing else same[0] if len(same) == 1 else _slug(raw)
    usage = "hermes flota setup USER@HOST, or hermes flota setup NAME --host HOST --user USER"
    refusal = (f"'{raw}' is not a usable name: use lowercase letters, digits, '-' or '_'" if not name
               else f"setup needs a host: {usage}" if not host
               else f"setup needs the SSH user: {usage}" if not user
               else f"no such key file: {args.key} (drop --key to use ssh's default keys)"
               if args.key and not _key_is_file(args.key) else "")
    if refusal:
        say(f"✗ {refusal}", True)
        say("Nothing was written.", True)
        return 2
    if name != raw or auto:
        say(f"Name: '{name}' (from the host; `hermes flota setup NAME --host ...` picks another)." if auto
            else f"Name: '{raw}' is saved as '{name}' (lowercase letters, digits, '-' and '_' only).")
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
    answering = _detect(args, name, profile, entry, say)
    unverified = answering is False and not args.reachable  # add-ship has just logged in to it
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
        if not (profile_dir / _homes.OWNER_FILE).is_file():  # not one Flota made: remove keeps it
            _mark_adopted(profile_dir)
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
    if args.sleep != "none" and not unverified:  # a closed port would only cost a second SSH timeout
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
    if not unverified:
        m = loaded[name]  # what will really happen, panel defaults applied
        how = "your sleep_command" if m.sleep_command else m.sleep
        idle_text = f" after {slash._duration(m.sleep_after)} idle" if m.sleep_after else ""
        say(f"Saved machine '{name}' (wake: {m.wake}, sleep: {how}{idle_text}) "
            f"in the Flota panel of {active}.")
    if unverified:
        say(f"⚠ Saved '{name}' but NOT verified: {entry['host']}:{entry['port']} is not answering (it is off, "
            "or the address is wrong).")
        say(f"Next: turn it on and run the same command again; `hermes flota remove {name}` undoes it.")
    elif _backend_on(config):
        say(f"Next: `hermes flota doctor {name}` checks it end to end; `/flota dry-run {name}` in a chat shows "
            "what a delegation would do.")
    else:
        say("Next: `hermes config set delegation.backend flota` to enable it; tune sleep and power in "
            f"Plugins > Flota; `/flota` in a chat (`/flota dry-run {name}` shows what a delegation would do).")
    return 0


def _drop_machines(names) -> None:
    def mutate(data: dict) -> None:
        old = data.get("delegation") if isinstance(data.get("delegation"), dict) else {}
        old = old.get("backends") if isinstance(old.get("backends"), dict) else {}
        old = old.get(PLUGIN_ID) if isinstance(old.get(PLUGIN_ID), dict) else {}
        panel = data
        for part in ("plugins", "entries", PLUGIN_ID, "settings"):
            panel = panel.get(part) if isinstance(panel.get(part), dict) else {}
        for block in (panel.get("machines"), old.get("machines")):
            for name in names if isinstance(block, dict) else ():
                block.pop(name, None)

    _update_config(_active_config_path(), mutate)


def _drop_profiles(entries: Mapping, kept: Mapping) -> list[str]:
    """Delete the profiles of ``entries`` that Flota created and no machine in ``kept`` uses."""
    used = {e.get("profile") for e in kept.values() if isinstance(e, Mapping)}
    mine = sorted({e.get("profile") for e in entries.values() if isinstance(e, Mapping)
                   and isinstance(e.get("profile"), str) and e["profile"] not in used and _flota_made(e["profile"])})
    for profile in mine:
        _delete_profile(profile)
    return mine


def _remove(args) -> int:
    name = args.machine
    existing = _ships(_read_config(_active_config_path()))
    if name not in existing:
        print(f"✗ no machine named '{name}'. Machines: {', '.join(existing) or 'none'}", file=sys.stderr)
        return 2
    busy = 0 if args.force else (_read_busy() or {}).get(name, 0)
    if busy:
        print(f"✗ {name} is busy with {busy} subagent(s); nothing was removed. Wait for them, or add --force.",
              file=sys.stderr)
        return 1
    entry = existing.pop(name)
    _drop_machines([name])
    print(f"Removed machine '{name}' from the fleet.")
    profile = entry.get("profile") if isinstance(entry, Mapping) else None
    if not args.keep_profile and _drop_profiles({name: entry}, existing):
        print(f"Deleted its profile '{profile}' (Flota created it).")
    elif profile:
        print(f"Kept profile '{profile}'" + ("." if args.keep_profile else " (Flota did not create it, or "
              "another machine uses it).") + f" `hermes profile delete {profile} -y` removes it.")
    print("Next: `hermes flota status` shows what is left; `hermes flota setup USER@HOST` adds a machine.")
    return 0


def _uninstall(yes: bool) -> int:
    config = _read_config(_active_config_path())
    ships, keys = _ships(config), pairing.flagships(_home())
    profiles = sorted({e.get("profile") for e in ships.values() if isinstance(e, Mapping)
                       and isinstance(e.get("profile"), str) and _flota_made(e["profile"])})
    print(f"Machines to forget: {', '.join(ships) or 'none'}\n"
          f"Profiles Flota created, to delete: {', '.join(profiles) or 'none'}\n"
          f"Flagship login keys to take back: {', '.join(keys) or 'none'}")
    if not yes:
        print("Nothing was changed.\nNext: hermes flota uninstall --yes")
        return 0
    busy = sorted(name for name, count in (_read_busy() or {}).items() if count and name in ships)
    if busy:
        print(f"✗ busy with subagents: {', '.join(busy)}; nothing was changed. Run it again when they finish.",
              file=sys.stderr)
        return 1
    removed = sum(pairing.deauthorize(flagship, _home()) for flagship in keys)
    _remove_ships()
    _set_panel("joined_fleets", None)
    _set_panel("role", None)
    _drop_profiles(ships, {})
    print(f"Done: {len(ships)} machine(s) forgotten, {len(profiles)} profile(s) deleted, {removed} key line(s) "
          "removed.")
    print("Left for you (Flota does not delete keys): the key pair flota_ed25519 and .pub in your SSH folder if Flota made "
          "one, the ships' lines in your SSH known_hosts, and on each ship this flagship's line in its "
          "authorized_keys (`hermes flota leave` there).")
    print("Next: " + ("hermes config set delegation.backend default && " if _backend_on(config) else "")
          + "hermes plugins remove flota")
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


def _detect(args, name: str, profile: str, entry: dict, say=None) -> bool | None:
    """Fill os / mac / broadcast / wake in ``entry`` from the machine itself. Flags always win.
    False when its SSH port did not answer, True when it did, None when it was not probed."""
    say = say or _print
    if args.no_detect:
        return None
    port = entry["port"]
    if (not machines_mod.HOST.fullmatch(entry["host"]) or isinstance(port, bool) or not isinstance(port, int)
            or not 1 <= port <= 65535):
        return None  # validation reports it
    probe = Machine(name=name, profile=profile, host=entry["host"], port=port, user=args.user,
                    key=entry.get("key") or "")
    if not _is_up(probe):
        say(f"Detection skipped: {entry['host']}:{port} is not answering. Saving what was given; run this "
              "again once it is on to fill in the MAC and broadcast address.")
        return False
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
    return True


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
        print("No machines yet.\nNext: hermes flota setup USER@HOST")
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
    setup = "hermes flota setup " + " ".join(shlex.quote(word) for word in (
        m.name, "--host", m.host, *(("--user", m.user) if m.user else ()), *(("--key", m.key) if m.key else ())))
    out = []
    reachable = False
    if m.key and not _key_is_file(m.key):
        out.append(("✗", f"SSH key {m.key} is missing", [f"point it at the right file: `{setup}`"]))
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
    key = _panel().get("ssh_key")
    mine = [Path(_os.path.expanduser(str(key)) + ".pub") if key else _home() / ".ssh" / "id_ed25519.pub",
            _home() / ".ssh" / "id_ed25519.pub", _home() / ".ssh" / "flota_ed25519.pub"]
    if any(p.is_file() and pairing._key_body(p.read_text().strip()) == pairing._key_body(invite["pubkey"])
           for p in mine):
        raise pairing.PairingError("this invite was made on this same machine and account; paste it on the "
                                   "machine that joins (hermes flota join <invite>)")
    ships = ships_count()
    if ships:
        raise pairing.PairingError(f"this Hermes is a flagship: it leads {ships} ship(s). A machine is a flagship "
                                   "or a ship, not both; to make it a ship: hermes flota role ship --force")
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
    if name is not None and not pairing._NAME.fullmatch(name):
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


def run_add_ship(code, emit: Callable[[str, bool], None] | None = None, name: str | None = None,
                 replace: bool = False) -> dict:
    """Flagship side: check the ship code, find an address that logs in, then run setup on it. A ship
    already in the fleet (same MAC, or one of its addresses) updates that machine, but only when it
    logs in on an address and user that machine already has: the MAC and addresses in a code are
    self-reported, so anything else is refused (PairingError) unless ``replace``. ``name`` overrides
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
        say("Nothing was saved.", True)
        say("Next: fix the line above on the ship, then run the same `hermes flota add-ship <ship code>` again "
            "(the code stays valid until its invite expires).", True)
        return {"ok": False, "lines": lines, "problems": ["no address of the ship accepted SSH"],
                "machine": target}
    if old is not None and not replace:
        alt = old.get("alt_hosts") if isinstance(old.get("alt_hosts"), list) else []
        known = {str(h).strip().lower() for h in [old.get("host"), *alt] if h}
        was = str(old.get("user") or "").strip()
        port = old.get("port", 22)  # same address, other port: maybe another box behind a NAT
        if host.lower() not in known or (was and was != ship["user"]) or port != ship["port"]:
            raise pairing.PairingError(
                f"this ship code says it is '{target}', but it logs in as {ship['user']}@{host}:{ship['port']} "
                f"and '{target}' is saved as {was + '@' if was else ''}{old.get('host')}:{port}. Nothing was changed. "
                f"If '{target}' really moved there, run it again with --replace "
                f"(hermes flota add-ship <code> --replace); otherwise do not add this code.")
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
                             os=ship["os"], reachable=True, extra={"crew_names": ship["crew_names"],
                                                   "alt_hosts": [h for h in ship["hosts"] if h != host]})
    code_, _ = run_setup(params, emit=say)
    problems = [line[2:] for line in lines if line.startswith("✗ machine")]
    if code_ == 0:
        pairing.spend(ship["nonce"], store)
    return {"ok": code_ == 0, "lines": lines, "problems": problems, "machine": target}


def _pairing_command(command: str, args) -> int:
    if command == "invite":
        invite = run_invite()
        until = time.strftime("%H:%M", time.localtime(time.time() + pairing.INVITE_TTL))
        print(f"Invite from {invite['flagship']}, valid for {pairing.INVITE_TTL // 60} minutes (until {until}), "
              "single use.\n")
        print(invite["code"])
        print("\nNext: on the machine that joins (it needs Hermes with Flota installed), paste this line, or "
              "paste the code in its Flota page. Check the key fingerprint it prints.\n")
        print(f"hermes flota join {invite['code']} --yes")
        return 0
    if command == "join":
        invite = pairing.read_invite(args.code)
        if not args.yes:
            question = (f"Let {invite['flagship']} ({invite['user'] or '?'}) log in to this machine as "
                        f"{_login_user()} with key {pairing.fingerprint(invite['pubkey'])}? [y/N] ")
            if not sys.stdin.isatty():
                print(f"Invite from {invite['flagship']} ({invite['user'] or '?'}), key "
                      f"{pairing.fingerprint(invite['pubkey'])}.\nNot joined. No terminal to confirm on. Check the "
                      "fingerprint and run again with --yes.", file=sys.stderr)
                return 1
            if input(question).strip().lower() not in ("y", "yes"):
                print("Not joined. Nothing was changed.", file=sys.stderr)
                return 1
        result = run_join(args.code)
        print("\n".join(result["lines"]))
        for check in result["checks"]:
            if check["ok"] is not True:
                mark = "✗" if check["ok"] is False else "?"
                print(f"  {mark} {check['label']}" + (f"\n      → {check['fix']}" if check["fix"] else ""))
        print("\nNext: On the flagship, paste this line (or paste the code in Flota > Add a ship):\n")
        print(f"hermes flota add-ship {result['ship_code']}")
        return 0
    if command == "join-check":
        result = join_check()
        marks = {True: "✓", False: "✗", None: "?"}
        for check in result["checks"]:
            print(f"{marks[check['ok']]} {check['label']}" + (f"\n    → {check['fix']}" if check["fix"] else ""))
        return 1 if any(c["ok"] is False for c in result["checks"]) else 0
    if command == "add-ship":
        result = run_add_ship(args.code, emit=_print, name=getattr(args, "name", None),
                              replace=getattr(args, "replace", False))
        return 0 if result["ok"] else 1
    flagship = args.flagship or (fleet() or {}).get("flagship")
    try:
        removed = leave_fleet(flagship)
    except KeyError:
        # No fleet on record (the plugin was removed and installed again): the key lines still say whose they are.
        names = [n for n in pairing.flagships(_home()) if args.flagship in (None, n)]
        if not names:
            print(f"✗ not in a fleet led by {args.flagship!r}" if args.flagship else "✗ not in a fleet",
                  file=sys.stderr)
            return 2
        removed = sum(pairing.deauthorize(n, _home()) for n in names)
        flagship = ", ".join(names)
        print("No fleet is recorded here, but this account's SSH authorized_keys still had a Flota key.")
    print(f"Left the fleet of {flagship}: removed {removed} key line(s) from this account's SSH authorized_keys.")
    print("Next: `hermes flota join <invite>` to join a fleet again, or `hermes flota uninstall` to remove "
          "everything Flota left on this machine.")
    return 0


def _role_command(args) -> int:
    if args.role is None:
        config = _read_config(_active_config_path())
        panel = plugin_settings_from_config(config)
        current, joined, count = role(panel), fleet(panel), ships_count(config)
        chosen = "" if panel.get("role") in ROLES else " (inferred)" if current else ""
        print(f"Role: {current or 'none'}{chosen}")
        print(f"Fleet: {joined['flagship']} (joined {joined.get('joined_at') or '?'})" if joined
              else f"Fleet: leads its own ({count} ship{'' if count == 1 else 's'})" if count else "Fleet: none")
        print(f"Ships: {count}")
        print("Next: " + ("hermes flota join-check   (can the flagship reach this machine?)" if joined
                          else "hermes flota   (shows the next step for this machine)"))
        return 0
    try:
        result = set_role(args.role, force=args.force)
    except RoleConflict as exc:
        hint = ("hermes flota leave" if exc.needs == "leave_fleet"
                else f"hermes flota remove <machine> for each, or hermes flota role {args.role} --force")
        print(f"✗ {exc}\n  → {hint}", file=sys.stderr)
        return 1
    if result["removed_ships"]:
        print(f"Removed {len(result['removed_ships'])} ship(s): {', '.join(result['removed_ships'])} "
              "(their SSH profiles are kept; `hermes profile delete` removes them).")
    if result["left_fleet"]:
        print(f"Left the fleet of {result['left_fleet']} ({result['removed_keys']} key line(s) removed).")
    now = role()
    print(f"Role: {result['role'] or 'none'}" if result["role"] or not now
          else f"Role: {now} (inferred from its {'machines' if now == 'flagship' else 'fleet'}; the choice was cleared)")
    print("Next: `hermes flota join <invite>` (make the invite on the flagship: hermes flota invite)"
          if now == "ship" and not fleet() else "Next: hermes flota   (shows the next step for this machine)")
    return 0
