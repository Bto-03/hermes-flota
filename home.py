"""Which Hermes home Flota's own state belongs to.

Flota keeps its settings, invites and busy counts in the active profile (``<home>/config.yaml``,
``<home>/flota/``). A ``flota-<machine>`` profile is only SSH plumbing: ``hermes flota setup`` clones
it from the profile that owns the fleet, so its config enables Flota too, and a request scoped to it
(the desktop app scopes plugin calls to the profile you last chatted with, and clicking a ship's bot
opens a chat as ``flota-<machine>``) would otherwise read and write a stale copy of the fleet there.
Such a home maps to its owner: the profile named in ``<plumbing>/flota/owner`` (written at setup),
else the root Hermes home. Every other home is its own owner.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

PROFILE_PREFIX = "flota-"
OWNER_FILE = Path("flota") / "owner"
ADOPTED_FILE = Path("flota") / "adopted"  # the profile existed before setup: never Flota's to delete


def active_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def is_plumbing(home: Path) -> bool:
    """True for ``<root>/profiles/flota-<machine>``."""
    home = Path(home)
    return home.parent.name == "profiles" and home.name.startswith(PROFILE_PREFIX)


def owner_of(home: Path) -> Path:
    home = Path(home)
    if not is_plumbing(home):
        return home
    try:
        owner = Path((home / OWNER_FILE).read_text().strip())
        if owner.is_absolute() and owner.is_dir() and not is_plumbing(owner):
            return owner
    except OSError:
        pass
    return home.parent.parent


def flota_home() -> Path:
    """The home Flota's state lives in for this request / command."""
    return owner_of(active_home())


def mark_owner(profile_dir: Path, owner: Path) -> None:
    """Record which profile a plumbing profile belongs to (best effort: the fallback is the root)."""
    path = Path(profile_dir) / OWNER_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{Path(owner)}\n")
    except OSError as exc:
        logger.warning("flota: could not write %s: %s", path, exc)


ROSTER_KEY = "hermes-bots"  # the Desktop Bots roster's namespace in profile.yaml ui_meta


def _load_yaml(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    try:
        import hermes_yaml as yaml
    except ImportError:
        import yaml
    data = yaml.safe_load(text) or {}
    return data if isinstance(data, dict) else {}


def _write_yaml(path: Path, data: dict) -> None:
    try:
        from utils import atomic_yaml_write  # core's atomic writer
    except ImportError:
        import yaml
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(yaml.safe_dump(data, sort_keys=False))
        tmp.replace(path)
        return
    atomic_yaml_write(path, data, sort_keys=False)


def hide_from_roster(profile_dir: Path, machine: str) -> bool:
    """Hide a plumbing profile from the Desktop Bots roster, the way its "Hide Bot" does:
    ``ui_meta['hermes-bots'].hidden: true`` in ``profile.yaml``, bumping that key's
    ``_ui_meta_revisions`` so every Desktop's compare-and-set sees the change. No title is set
    (a titled ``flota-*`` profile counts as a bot on the Flota map). A missing description gets
    one that says what the profile is. True when the file changed; never raises."""
    path = Path(profile_dir) / "profile.yaml"
    try:
        if not Path(profile_dir).is_dir():
            return False
        data = _load_yaml(path)
        ui = data.get("ui_meta") if isinstance(data.get("ui_meta"), dict) else {}
        bots = ui.get(ROSTER_KEY) if isinstance(ui.get(ROSTER_KEY), dict) else {}
        described = bool(str(data.get("description") or "").strip())
        if bots.get("hidden") is True and described:
            return False
        if not described:
            data["description"] = (f"Flota plumbing: the SSH terminal on {machine}. Not a bot; Flota uses it "
                                   "to run subagents there.")
        if bots.get("hidden") is not True:
            ui[ROSTER_KEY] = {**bots, "hidden": True}
            data["ui_meta"] = ui
            revisions = data.get("_ui_meta_revisions") if isinstance(data.get("_ui_meta_revisions"), dict) else {}
            current = revisions.get(ROSTER_KEY)
            current = current if isinstance(current, int) and not isinstance(current, bool) and current > 0 else 0
            data["_ui_meta_revisions"] = {**revisions, ROSTER_KEY: current + 1}
        _write_yaml(path, data)
        return True
    except Exception as exc:  # best effort: a roster cosmetic must never break setup
        logger.warning("flota: could not hide %s from the bots roster: %s", profile_dir, exc)
        return False


@contextmanager
def owner_scope() -> Iterator[Path]:
    """Run core calls that read ``get_hermes_home()`` (config, cron, profile clone) in the owner's
    home when the active one is a plumbing profile; a no-op otherwise. Context-local, never env."""
    home = active_home()
    owner = owner_of(home)
    if owner == home:
        yield owner
        return
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(str(owner))
    try:
        yield owner
    finally:
        reset_hermes_home_override(token)
