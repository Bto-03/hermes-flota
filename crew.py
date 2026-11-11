"""Crew: the Hermes bots shown on the Flota map, each on the machine it lives on.

A bot is a Hermes profile with an identity (the Bots roster of the Desktop app). Its name, colour
and avatar are where core keeps them: ``profile.yaml`` ``ui_meta['hermes-bots'].title`` / ``.color``
(``hermes_cli/profiles.py`` ``read_profile_meta``), else ``display_name``; the picture is
``<profile dir>/assets/avatar.{png,jpg,webp}`` (``tui_gateway/methods_profiles.py``
``_profile_ui_meta_fields``). Flota's own ``flota-<machine>`` profiles are the ship hands of their
machine, not bots of this host.

Hub bots are 'awake' while a gateway serves them (the pid in ``gateway.lock`` / ``gateway.pid`` of
their profile dir is alive) and 'working' while that gateway reports ``active_agents`` > 0.
Machine bots follow the machine: 'asleep' when it is down, 'awake' when up, and the first N are
'working' while N subagents run on it.
"""
import base64
import importlib
import json
import os
import re
import threading
import time
from collections.abc import Mapping
from pathlib import Path

AVATAR_CAP = 64 * 1024
AVATAR_TTL = 60.0
_MIME = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
_FLOTA_PREFIX = "flota-"
_avatars: dict[str, tuple[float, str | None]] = {}
_avatars_lock = threading.Lock()


def load_yaml(path: Path) -> dict:
    """A YAML mapping, {} when missing / unreadable / not a mapping. Never raises."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    try:
        try:
            yaml = importlib.import_module("hermes_yaml")
        except ImportError:
            yaml = importlib.import_module("yaml")
        data = yaml.safe_load(text) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def avatar_data_url(profile_dir: Path, *, clock=time.monotonic) -> str | None:
    """``assets/avatar.<ext>`` as a data URL; None when absent or over ``AVATAR_CAP`` bytes.
    Cached ``AVATAR_TTL`` s per profile dir: /status is polled every few seconds."""
    key = str(profile_dir)
    now = clock()
    with _avatars_lock:
        hit = _avatars.get(key)
        if hit and now - hit[0] < AVATAR_TTL:
            return hit[1]
    value = None
    for ext, mime in _MIME.items():
        path = Path(profile_dir) / "assets" / f"avatar.{ext}"
        try:
            if not path.is_file() or path.stat().st_size > AVATAR_CAP:
                continue
            value = f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()
            break
        except OSError:
            continue
    with _avatars_lock:
        _avatars[key] = (now, value)
    return value


def identity(profile_id: str, profile_dir: Path, *, load=load_yaml) -> dict:
    """``{"id", "name", "color", "avatar", "named"}``; ``named`` is true when the profile was given
    a bot title or display name."""
    meta = load(Path(profile_dir) / "profile.yaml")
    ui = meta.get("ui_meta") if isinstance(meta.get("ui_meta"), Mapping) else {}
    bots = ui.get("hermes-bots") if isinstance(ui.get("hermes-bots"), Mapping) else {}
    title = str(bots.get("title") or "").strip() or str(meta.get("display_name") or "").strip()
    color = bots.get("color")
    fallback = "Hermes" if profile_id == "default" else profile_id
    return {"id": profile_id, "name": title or fallback,
            "color": color.strip() if isinstance(color, str) and color.strip() else None,
            "avatar": avatar_data_url(Path(profile_dir)), "named": bool(title)}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _json(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def gateway_state(profile_dir: Path, *, alive=_pid_alive) -> str:
    """'working' | 'awake' | 'asleep' for the gateway serving this profile."""
    profile_dir = Path(profile_dir)
    for name in ("gateway.lock", "gateway.pid"):
        pid = _json(profile_dir / name).get("pid")
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and alive(pid):
            state = _json(profile_dir / "gateway_state.json")
            active = state.get("active_agents")
            if (state.get("pid") == pid and isinstance(active, int) and not isinstance(active, bool)
                    and active > 0):
                return "working"
            return "awake"
    return "asleep"


def has_avatar(profile_dir: Path) -> bool:
    return any((Path(profile_dir) / "assets" / f"avatar.{ext}").is_file() for ext in _MIME)


def is_bot(profile_id: str, profile_dir: Path, ident: Mapping, *, ship_hands=()) -> bool:
    """default, or a profile with a bot title, display name, SOUL.md or avatar. ``flota-*``
    profiles are Flota plumbing (Flota gives them a SOUL.md and an avatar): a bot only when someone
    titled it, and never when it is the ship hand of a registered machine (shown on that ship)."""
    if profile_id.startswith(_FLOTA_PREFIX):
        return bool(ident.get("named")) and profile_id not in set(ship_hands)
    return (profile_id == "default" or bool(ident.get("named")) or (Path(profile_dir) / "SOUL.md").is_file()
            or has_avatar(profile_dir))


def hub_report(profiles, *, ship_hands=(), load=load_yaml) -> list[tuple[str, bool, str]]:
    """``[(profile id, included, why), ...]``: why each local profile is (not) on the hub crew."""
    out = []
    for profile_id, profile_dir in profiles:
        ident = identity(profile_id, profile_dir, load=load)
        reasons = [r for r, ok in (("default", profile_id == "default"), ("titled", ident.get("named")),
                                   ("SOUL.md", (Path(profile_dir) / "SOUL.md").is_file()),
                                   ("avatar", has_avatar(profile_dir))) if ok]
        included = is_bot(profile_id, profile_dir, ident, ship_hands=ship_hands)
        if profile_id.startswith(_FLOTA_PREFIX) and not included:
            why = "ship hand of a registered machine" if ident.get("named") else "flota plumbing, no bot title"
        else:
            why = ", ".join(reasons) or "no title, SOUL.md or avatar"
        out.append((profile_id, included, why))
    return out


def _bot(ident: Mapping, state: str, **extra) -> dict:
    return {"id": ident["id"], "name": ident["name"], "avatar": ident["avatar"], "color": ident["color"],
            "state": state, **extra}


def hub_crew(profiles, *, load=load_yaml, alive=_pid_alive, ship_hands=()) -> list[dict]:
    """The bots living on the Hermes host. ``profiles``: ``[(profile id, profile dir), ...]``;
    ``ship_hands``: the ``flota-<machine>`` profiles of registered machines (never hub bots)."""
    crew = []
    for profile_id, profile_dir in profiles:
        ident = identity(profile_id, profile_dir, load=load)
        if is_bot(profile_id, profile_dir, ident, ship_hands=ship_hands):
            crew.append(_bot(ident, gateway_state(profile_dir, alive=alive)))
    return crew


def hub_crew_names(profiles, *, load=load_yaml) -> list[str]:
    """Display names of this host's bots (what a ship reports to its flagship)."""
    return [bot["name"] for bot in hub_crew(profiles, load=load, alive=lambda pid: False)]


_SAFE_NAME = re.compile(r"[^\w .'-]+")


def machine_crew(machine, entry: Mapping | None, *, up: bool, busy: int | None, terminals: Mapping,
                 profiles: Mapping, load=load_yaml) -> list[dict]:
    """The bots on ``machine``: its ship hand (the ``flota-<name>`` profile), the other profiles
    whose terminal is SSH to its host, then the bots the machine reported when it joined
    (``crew_names`` of its config entry)."""
    entry = entry if isinstance(entry, Mapping) else {}
    rows = []
    others = sorted(name for name, t in terminals.items()
                    if name != machine.profile and t.get("backend") == "ssh" and t.get("ssh_host") == machine.host)
    for profile_id in [machine.profile, *others]:
        profile_dir = profiles.get(profile_id)
        if profile_dir is not None:
            ident = identity(profile_id, profile_dir, load=load)
        else:
            ident = {"id": profile_id, "name": profile_id, "color": None, "avatar": None, "named": False}
        if profile_id == machine.profile and not ident["named"]:
            ident = {**ident, "name": machine.name}
        rows.append((ident, profile_id == machine.profile))
    reported = entry.get("crew_names")
    seen = {ident["name"] for ident, _ in rows}
    for raw in reported if isinstance(reported, list) else []:
        name = _SAFE_NAME.sub("", str(raw)).strip()[:64] if isinstance(raw, str) else ""
        if name and name not in seen:
            seen.add(name)
            rows.append(({"id": f"{machine.name}/{name}", "name": name, "color": None, "avatar": None}, False))
    working = busy or 0
    crew = []
    for ident, hand in rows:
        if not up:
            state = "asleep"
        elif working > 0:
            state, working = "working", working - 1
        else:
            state = "awake"
        crew.append(_bot(ident, state, ship_hand=hand))
    return crew


def local_profiles() -> list[tuple[str, Path]]:
    """``[(profile id, dir), ...]`` of this Hermes install, from core."""
    profiles = importlib.import_module("hermes_cli.profiles")
    out = []
    for name in profiles.list_profile_names():
        try:
            out.append((name, Path(profiles.get_profile_dir(name))))
        except Exception:
            continue
    return out
