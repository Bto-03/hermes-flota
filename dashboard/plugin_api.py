"""Flota dashboard tab backend, mounted at ``/api/plugins/flota/``.

Thin wrappers around the plugin's own modules (the same code ``/flota`` and ``hermes flota`` run):
``slash.load_settings`` for the merged settings, ``machines.load_machines`` to parse and validate,
``wake`` to probe and wake, ``power`` to sleep / power off, ``cli`` to write the config.

The dashboard imports this file on its own (not as part of the plugin package), so the package is
looked up in ``sys.modules`` (Hermes may already have loaded it) or imported here from the parent
directory. The dashboard is a different process from the gateway, so it reads the busy counts the
gateway publishes (``idle.read_busy``); ``busy_known`` is false when no live gateway wrote them.
Sleep / power off answer 409 for a busy machine unless called with ``?force=1``.

``GET /status`` is polled every few seconds and must always answer fast: TCP probes run in
background threads (one in flight per machine, ``PROBE_TIMEOUT`` each) and the request waits at most
``STATUS_PROBE_WAIT`` for them, reporting the last known state (``probing: true``) for the rest; the
SSH crew scan is cached the same way (``remote_crew.RemoteCrews``). It never answers 500: a failure
comes back as ``{"machines": [], "error": ...}``. Every other route answers JSON errors too
(``_JSONErrors``), so the page never gets an HTML 500 it cannot read.
"""
from __future__ import annotations

import contextvars
import importlib
import logging
import importlib.util
import platform
import re
import socket
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class _JSONErrors(APIRoute):
    """An unexpected exception in a handler answers ``500 {"detail": ...}`` (JSON) instead of the
    server's plain-text 500, and is logged. HTTP errors and validation errors pass through."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def run(request):
            try:
                return await handler(request)
            except (StarletteHTTPException, RequestValidationError):
                raise
            except Exception as exc:  # any bug or core failure: still JSON the page can show
                logger.exception("flota: %s %s failed", request.method, request.url.path)
                return JSONResponse({"detail": f"Flota backend error: {exc}", "error": str(exc)},
                                    status_code=500)

        return run


router = APIRouter(route_class=_JSONErrors)

_ROOT = Path(__file__).resolve().parents[1]
_FALLBACK_PACKAGE = "flota_plugin"
PROBE_TIMEOUT = 1.5
STATUS_PROBE_WAIT = 1.0  # the longest /status waits for this round's TCP probes
SETUP_TIMEOUT = 60.0
# Keys the form may set. Shell-bearing keys (sleep_command, wake_command, power_prefix) and the
# profile are never accepted from the browser; existing values are kept on update.
_EDITABLE = ("host", "user", "key", "port", "description", "wake", "mac", "broadcast", "via",
             "sleep", "sleep_after", "wake_timeout", "fallback", "os")
_FORBIDDEN = ("sleep_command", "wake_command", "power_prefix", "command", "profile")
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,57}")  # flota-<name> must be a valid profile id (64 chars)

# name -> {"thread", "error", "started", "waited"}: wakes started from this tab.
_wakes: dict[str, dict[str, Any]] = {}
_wakes_lock = threading.Lock()


def _package():
    init = _ROOT / "__init__.py"
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if path and getattr(module, "__path__", None) is not None:
            try:
                if Path(path).resolve() == init:
                    return module
            except OSError:
                continue
    spec = importlib.util.spec_from_file_location(_FALLBACK_PACKAGE, init,
                                                  submodule_search_locations=[str(_ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[_FALLBACK_PACKAGE] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_FALLBACK_PACKAGE, None)
        raise
    return module


_PACKAGE = _package().__name__


def _mod(name: str):
    """A plugin submodule, looked up per call so tests (and reloads) can swap its functions."""
    return importlib.import_module(f"{_PACKAGE}.{name}")


def _settings() -> Mapping:
    try:
        return _mod("slash").load_settings()
    except Exception as exc:
        raise HTTPException(500, f"could not read the Hermes config: {exc}") from exc


def _fleet() -> tuple[Mapping, dict, list[str]]:
    settings = _settings()
    machines, problems = _mod("machines").load_machines(settings)
    return settings, machines, problems


def _machine(name: str):
    settings, machines, _ = _fleet()
    machine = machines.get(name)
    if machine is None:
        raise HTTPException(404, f"no machine named {name!r}; configured: {', '.join(machines) or 'none'}")
    return settings, machine


def _profile_exists(profile: str) -> bool:
    try:
        return bool(_mod("slash")._profile_exists(profile))
    except Exception:
        return False


def _busy() -> dict | None:
    try:
        return _mod("idle").read_busy()
    except Exception:
        return None


def _setup_command(name: str, host: str, user: str, key: str) -> str:
    parts = ["hermes flota setup", name, "--host", host or "HOST", "--user", user or "USER"]
    if key:
        parts += ["--key", key]
    return " ".join(parts)


def _wake_state(name: str) -> tuple[bool, str | None]:
    with _wakes_lock:
        state = _wakes.get(name)
    if not state:
        return False, None
    return state["thread"].is_alive(), state["error"]


# --- read ----------------------------------------------------------------------------------------

class _Probes:
    """TCP reachability per (host, port), probed off the request thread: at most one probe in flight
    per address (a dead host is not probed again until its probe times out); the last result is
    kept for the next poll."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._results: dict[tuple, bool] = {}
        self._running: dict[tuple, threading.Event] = {}

    def start(self, keys) -> list[threading.Event]:
        events = []
        for key in dict.fromkeys(keys):
            with self._lock:
                event = self._running.get(key)
                fresh = event is None
                if fresh:
                    event = self._running[key] = threading.Event()
            events.append(event)
            if fresh:
                threading.Thread(target=self._probe, args=(key, event), name=f"flota-probe-{key[0]}",
                                 daemon=True).start()
        return events

    def _probe(self, key: tuple, event: threading.Event) -> None:
        try:
            up = bool(_mod("wake").is_up(key[0], key[1], timeout=PROBE_TIMEOUT))
        except Exception:
            up = False
        with self._lock:
            self._results[key] = up
            self._running.pop(key, None)
        event.set()

    def read(self, key: tuple) -> tuple[bool, bool]:
        """``(up, probing)``: the last result (False before the first) and whether one is in flight."""
        with self._lock:
            return self._results.get(key, False), key in self._running

    def running(self) -> bool:
        with self._lock:
            return bool(self._running)


_probes = _Probes()


def _reachability(items) -> list[tuple[bool, bool]]:
    keys = [(m.host, m.port) for m in items]
    deadline = time.monotonic() + STATUS_PROBE_WAIT
    for event in _probes.start(keys):
        event.wait(max(0.0, deadline - time.monotonic()))
    return [_probes.read(key) for key in keys]

def _hub() -> dict:
    """The machine Hermes runs on, the centre of the desktop map."""
    try:
        name = socket.gethostname() or "hermes"
    except OSError:
        name = "hermes"
    return {"name": name, "os": (platform.system() or "").lower()}


_TERMINALS: dict[str, Any] = {}  # {"at": monotonic time, "value": {...}}: profile configs, re-read every 30 s


def _profile_terminals() -> dict[str, dict]:
    """``{profile: {"backend", "ssh_host"}}`` from each Hermes profile's ``config.yaml``. A profile
    whose config does not parse is left out. Cached for 30 s: /status is polled every few seconds."""
    cached = _TERMINALS.get("value")
    if cached is not None and time.monotonic() - _TERMINALS["at"] < 30:
        return cached
    try:
        yaml = importlib.import_module("hermes_yaml")  # core's YAML door (ruamel underneath)
    except ImportError:
        import yaml
    profiles = importlib.import_module("hermes_cli.profiles")
    found = {}
    for name in profiles.list_profile_names():
        try:
            text = (Path(profiles.get_profile_dir(name)) / "config.yaml").read_text(encoding="utf-8-sig")
            data = yaml.safe_load(text) or {}
        except Exception:
            continue
        terminal = data.get("terminal") if isinstance(data, Mapping) else None
        terminal = terminal if isinstance(terminal, Mapping) else {}
        found[name] = {"backend": terminal.get("backend"), "ssh_host": terminal.get("ssh_host")}
    _TERMINALS.update(at=time.monotonic(), value=found)
    return found


def _crew_profiles() -> list:
    """``[(profile id, dir), ...]`` of this Hermes install (tests replace it)."""
    return _mod("crew").local_profiles()


_REMOTE: dict[str, Any] = {}  # {"cache": remote_crew.RemoteCrews}: one per process


def _remote_cache():
    cache = _REMOTE.get("cache")
    if cache is None:
        cache = _REMOTE["cache"] = _mod("remote_crew").RemoteCrews()
    return cache


def _remote_scan(machine, up: bool) -> dict | None:
    """The cached SSH scan of the bots living on ``machine`` (None until the first one finishes).
    Never blocks: a stale or missing result is refreshed in a background thread."""
    try:
        return _remote_cache().get(machine, up)
    except Exception:
        return None


def _crew(machines_rows, settings: Mapping, terminals: Mapping, busy) -> tuple[list, dict, dict]:
    """``(hub_crew, {machine name: crew}, {machine name: remote hostname})``; never raises (the map
    still draws without bots)."""
    crew = _mod("crew")
    try:
        profiles = list(_crew_profiles())
    except Exception:
        profiles = []
    try:
        hub = crew.hub_crew(profiles, ship_hands={m.profile for m, _ in machines_rows})
    except Exception:
        hub = []
    by_id = dict(profiles)
    entries = settings.get("machines") if isinstance(settings.get("machines"), Mapping) else {}
    per = {}
    for machine, is_on in machines_rows:
        try:
            per[machine.name] = crew.machine_crew(
                machine, entries.get(machine.name), up=is_on,
                busy=None if busy is None else busy.get(machine.name, 0), terminals=terminals, profiles=by_id)
        except Exception:
            per[machine.name] = []
    hostnames = {}
    remote = _mod("remote_crew")
    for machine, is_on in machines_rows:
        scan = _remote_scan(machine, is_on)
        try:
            per[machine.name] = remote.merge(per[machine.name], remote.crew_entries(machine, scan, up=is_on))
        except Exception:
            pass
        hostnames[machine.name] = (scan or {}).get("hostname")
    return hub, per, hostnames


def _role() -> str | None:
    try:
        return _mod("cli").role()
    except Exception:
        return None


def _agents(machine, terminals: Mapping) -> list[str]:
    """The Hermes agents that run on ``machine``: its own profile first, then every other profile
    whose terminal is SSH to the same host."""
    others = sorted(name for name, t in terminals.items()
                    if name != machine.profile and t.get("backend") == "ssh" and t.get("ssh_host") == machine.host)
    return [machine.profile, *others]


def _fleet_fields() -> dict:
    """``role``, ``fleet`` and ``ships_count`` for /status; each falls back on its own."""
    cli = _mod("cli")
    out: dict[str, Any] = {"role": _role(), "fleet": None, "ships_count": 0}
    try:
        out["fleet"] = cli.fleet()
    except Exception:
        pass
    try:
        out["ships_count"] = int(cli.ships_count())
    except Exception:
        pass
    return out


@router.get("/status")
def status():
    try:
        return _status()
    except Exception as exc:  # never a 500: the page keeps polling and shows why
        logger.warning("flota: /status failed: %s", exc, exc_info=True)
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        fields = {"role": None, "fleet": None, "ships_count": 0}
        try:
            fields = _fleet_fields()
        except Exception:
            pass
        return {"machines": [], "problems": [], "hub": _hub(), "hub_crew": [], **fields,
                "allow_power_off": True, "default": None, "busy_known": False, "checked_at": time.time(),
                "error": f"Flota could not read the fleet: {detail}"}


def _status() -> dict:
    settings, machines, problems = _fleet()
    busy = _busy()
    items = list(machines.values())
    reach = _reachability(items)
    up = [is_on for is_on, _ in reach]
    try:
        terminals = _profile_terminals()
    except Exception:  # no profiles module or directory: each machine still has its own profile
        terminals = {}
    hub_crew, crews, hostnames = _crew(list(zip(items, up)), settings, terminals, busy)
    rows = []
    for machine, (is_on, probing) in zip(items, reach):
        waking, error = _wake_state(machine.name)
        exists = _profile_exists(machine.profile)
        refusal = _mod("settings").power_off_refusal(machine, settings)
        rows.append({
            "name": machine.name, "description": machine.description, "host": machine.host,
            "port": machine.port, "user": machine.user, "key": machine.key,
            "os": getattr(machine, "os", "linux") or "linux", "profile": machine.profile,
            "profile_exists": exists,
            "setup_command": None if exists else _setup_command(machine.name, machine.host, machine.user,
                                                                machine.key),
            "wake": machine.wake, "mac": machine.mac, "broadcast": machine.broadcast, "via": machine.via,
            "sleep": machine.sleep, "custom_sleep": bool(machine.sleep_command),
            "sleep_after": machine.sleep_after, "fallback": machine.fallback,
            "up": is_on, "probing": probing, "waking": waking and not is_on,
            "wake_error": None if waking else error,
            "busy": None if busy is None else busy.get(machine.name, 0),
            "agents": _agents(machine, terminals),
            "crew": crews.get(machine.name, []),
            "hostname": hostnames.get(machine.name),
            "can_power_off": refusal is None, "power_off_reason": refusal,
        })
    hub = dict(_hub())
    hub["gateway_up"] = any(b.get("state") in ("awake", "working") for b in hub_crew)  # flagship active = a bot's gateway runs
    return {"machines": rows, "problems": problems, "hub": hub, "hub_crew": hub_crew, **_fleet_fields(),
            "allow_power_off": settings.get("allow_power_off") is not False,
            "default": settings.get("default"), "busy_known": busy is not None, "checked_at": time.time(),
            "error": None}


def _cron_jobs(include_disabled: bool = False) -> list[dict]:
    """This Hermes's scheduled jobs, read from the core cron store (the HERMES_HOME of this process)."""
    with _mod("home").owner_scope():
        return importlib.import_module("cron.jobs").list_jobs(include_disabled=include_disabled)


def _mentions(text: str, names) -> list[str]:
    """Machine names that appear in ``text`` as whole words (case-insensitive), in order of mention.
    'mini-pc' names mini, 'flota-torre' names torre, 'torretas' does not."""
    hits = []
    for name in names:
        match = re.search(rf"(?<![a-z0-9]){re.escape(name.lower())}(?![a-z0-9])", text.lower())
        if match:
            hits.append((match.start(), name))
    return [name for _, name in sorted(hits)]


def _next_run_key(job: Mapping) -> tuple:
    from datetime import datetime, timezone
    raw = job.get("next_run_at")
    try:
        when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.astimezone()  # naive: local time, as the cron store writes it
        return (0, when.astimezone(timezone.utc).timestamp())
    except (TypeError, ValueError):
        return (1, 0.0)  # no next run (paused, finished): last


@router.get("/schedule")
def schedule():
    """The cron jobs of this Hermes, soonest first, each with the machines it names. Read-only: the
    machine is woken by Flota when the job delegates to it, like any other subagent."""
    try:
        _, machines, _ = _fleet()
    except HTTPException:
        machines = {}
    try:
        raw = _cron_jobs(include_disabled=True)
    except Exception as exc:  # cron not importable here, or its store unreadable
        return {"jobs": [], "available": False, "error": f"cron jobs unavailable: {exc}", "checked_at": time.time()}
    jobs = []
    for job in sorted((j for j in raw if isinstance(j, Mapping)), key=_next_run_key):
        prompt = str(job.get("prompt") or "")
        skills = [str(s) for s in (job.get("skills") or ([job["skill"]] if job.get("skill") else []))]
        named = _mentions(" ".join([str(job.get("name") or ""), prompt, *skills]), list(machines))
        explicit = job.get("machine")
        if isinstance(explicit, str) and explicit in machines:
            named = [explicit, *(n for n in named if n != explicit)]
        schedule_text = job.get("schedule_display") or job.get("schedule")
        if isinstance(schedule_text, Mapping):
            schedule_text = schedule_text.get("display") or schedule_text.get("expr") or schedule_text.get("value")
        jobs.append({
            "id": str(job.get("id") or ""), "name": str(job.get("name") or prompt[:50] or "cron job"),
            "schedule": str(schedule_text or "?"), "next_run_at": job.get("next_run_at"),
            "last_run_at": job.get("last_run_at"), "last_status": job.get("last_status"),
            "enabled": job.get("enabled", True) is not False, "state": job.get("state") or "scheduled",
            "skills": skills, "deliver": job.get("deliver"),
            "prompt_preview": prompt[:140] + ("…" if len(prompt) > 140 else ""),
            "machine": named[0] if named else None, "machines": named,
        })
    return {"jobs": jobs, "available": True, "error": None, "checked_at": time.time()}


# --- act -----------------------------------------------------------------------------------------

@router.post("/wake/{name}", status_code=202)
def wake_machine(name: str):
    _, machine = _machine(name)
    with _wakes_lock:
        state = _wakes.get(name)
        if state and state["thread"].is_alive():
            return {"ok": True, "state": "waking", "machine": name}
        state = {"error": None, "started": time.time(), "waited": None}

        def run():
            try:
                state["waited"] = _mod("wake").ensure_ready(machine)
            except Exception as exc:  # WakeError, or a wake command that blew up
                state["error"] = str(exc)

        state["thread"] = threading.Thread(target=contextvars.copy_context().run, args=(run,),
                                           name=f"flota-wake-{name}", daemon=True)
        _wakes[name] = state
        state["thread"].start()
    return {"ok": True, "state": "waking", "machine": name}


def _power(machine, action: str, force: bool) -> dict:
    count = 0 if force else (_busy() or {}).get(machine.name, 0)
    if count:
        verb = "power it off" if action == "poweroff" else "sleep it"
        raise HTTPException(409, f"{machine.name} is busy with {count} subagent(s); add --force to {verb} "
                                 "anyway")
    power = _mod("power")
    result = power.power(machine, action)
    if " failed: " in result:
        raise HTTPException(502, result)
    return {"ok": True, "result": result}


@router.post("/sleep/{name}")
def sleep_machine(name: str, force: bool = False):
    _, machine = _machine(name)
    return _power(machine, _mod("power").default_action(machine), force)


@router.post("/off/{name}")
def power_off(name: str, force: bool = False):
    settings, machine = _machine(name)
    refusal = _mod("settings").power_off_refusal(machine, settings)
    if refusal:
        raise HTTPException(403, refusal)
    return _power(machine, "poweroff", force)


# --- edit ----------------------------------------------------------------------------------------

def _panel_machines(config: Mapping) -> tuple[dict, dict]:
    """``(copied, existing)`` exactly as ``hermes flota setup`` merges them: the first panel write
    carries the old ``delegation.backends.flota`` machines over."""
    cli = _mod("cli")
    panel = _mod("settings").plugin_settings_from_config(config)
    panel_machines = panel.get("machines") if isinstance(panel.get("machines"), Mapping) else {}
    copied = {} if panel_machines else dict(cli._legacy_machines(config))
    return copied, {**copied, **panel_machines}


def _write_machines(machines: dict) -> None:
    cli = _mod("cli")
    plugin_id = _mod("settings").PLUGIN_ID

    def mutate(data: dict) -> None:
        block = data
        for key in ("plugins", "entries", plugin_id, "settings"):
            block = cli._child(block, key)
        block["machines"] = machines

    try:
        cli._update_config(cli._active_config_path(), mutate)
    except (RuntimeError, OSError) as exc:  # managed install, unreadable config
        raise HTTPException(409, str(exc)) from exc


def _read_config() -> dict:
    cli = _mod("cli")
    try:
        return cli._read_config(cli._active_config_path())
    except (RuntimeError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/machines")
def save_machine(body: dict = Body(...)):
    name = body.get("name")
    name = name.strip() if isinstance(name, str) else ""
    if not _NAME.fullmatch(name):
        raise HTTPException(400, "name must be lowercase letters, digits, '-' or '_' (it names the "
                                 "profile flota-<name>)")
    run_setup = body.get("setup", False)
    if not isinstance(run_setup, bool):
        raise HTTPException(400, "setup must be true or false")
    body = {k: v for k, v in body.items() if k != "setup"}
    forbidden = sorted(set(body) & set(_FORBIDDEN))
    if forbidden:
        raise HTTPException(400, f"{', '.join(forbidden)} cannot be set from the dashboard; edit "
                                 "config.yaml or use `hermes flota setup`")
    unknown = sorted(set(body) - set(_EDITABLE) - {"name"})
    if unknown:
        raise HTTPException(400, f"unknown field(s): {', '.join(unknown)}")
    changes = {}
    for key in _EDITABLE:
        if key not in body:
            continue
        value = body[key]
        if isinstance(value, str):
            value = value.strip()
        elif value is not None and not (key in ("port", "sleep_after", "wake_timeout")
                                        and isinstance(value, (int, float)) and not isinstance(value, bool)):
            raise HTTPException(400, f"{key} must be text")
        changes[key] = value

    config = _read_config()
    copied, existing = _panel_machines(config)
    old = existing.get(name)
    entry = dict(old) if isinstance(old, Mapping) else {}
    for key, value in changes.items():
        if value in ("", None):
            entry.pop(key, None)  # an emptied field goes back to the default
        else:
            entry[key] = value
    entry.setdefault("profile", f"flota-{name}")
    if not entry.get("host"):
        raise HTTPException(400, "host is required")

    settings_mod, machines_mod = _mod("settings"), _mod("machines")
    panel = settings_mod.plugin_settings_from_config(config)
    merged = {**existing, name: entry}
    loaded, problems = machines_mod.load_machines(settings_mod.effective(None, {**panel, "machines": merged}))
    # As in `hermes flota setup`: the panel's default sleep_after on a `sleep: none` machine is not
    # this machine's mistake unless it set sleep_after itself.
    problems = [p for p in problems if p.startswith(f"machine '{name}':")
                and ("sleep_after" in entry or "sleep_after needs" not in p)]
    if problems or name not in loaded:
        raise HTTPException(422, {"message": "Nothing was saved.", "problems": problems})

    _write_machines(merged)
    if run_setup:
        params = {key: entry.get(key) for key in ("host", "user", "key", "port", "description", "wake", "mac",
                                                   "broadcast", "sleep", "sleep_after", "os")}
        params["user"] = params["user"] or ""
        result = _in_worker(lambda: _mod("cli").run_setup(SimpleNamespace(name=name, **params)), name)
        result["copied_legacy"] = sorted(copied)
        return result
    profile = entry["profile"]
    exists = _profile_exists(profile)
    return {"ok": True, "machine": name, "profile": profile, "profile_exists": exists,
            "copied_legacy": sorted(copied),
            "setup_command": None if exists else _setup_command(name, entry["host"], entry.get("user", ""),
                                                                entry.get("key", ""))}


@router.delete("/machines/{name}")
def delete_machine(name: str):
    config = _read_config()
    copied, existing = _panel_machines(config)
    if name not in existing:
        raise HTTPException(404, f"no machine named {name!r} in the Flota panel")
    remaining = {k: v for k, v in existing.items() if k != name}
    legacy = bool(_mod("cli")._legacy_machines(config))
    if not remaining and legacy:
        # An empty panel falls back to delegation.backends.flota, which may bring it right back.
        raise HTTPException(409, "This is the last machine in the Flota panel; removing it would bring back "
                                 "the machines in delegation.backends.flota of config.yaml. Remove it there.")
    _write_machines(remaining)
    with _wakes_lock:
        _wakes.pop(name, None)
    return {"ok": True, "machine": name, "copied_legacy": sorted(set(copied) - {name}),
            "profile_kept": f"The profile of {name} was kept; `hermes profile delete` removes it."}


# --- one-click setup and pairing ----------------------------------------------------------------------

def _in_worker(work, name: str) -> dict:
    """Run a setup (``work() -> (code, lines)`` or a result dict) in a thread for at most
    ``SETUP_TIMEOUT`` s; the reply has ``ok``, ``lines``, ``problems`` and ``machine``."""
    box: dict[str, Any] = {}

    def run():
        try:
            box["value"] = work()
        except Exception as exc:  # core refused the profile or the write, ssh blew up, bad code
            box["error"] = exc

    # A thread starts in an empty context: carry the request's profile scope (core's hermes-home
    # override) into it, or the setup would write the process's launch profile.
    thread = threading.Thread(target=contextvars.copy_context().run, args=(run,), name=f"flota-setup-{name}",
                              daemon=True)
    thread.start()
    thread.join(SETUP_TIMEOUT)
    if thread.is_alive():
        return {"ok": False, "machine": name, "problems": [f"setup still running after {SETUP_TIMEOUT:.0f}s"],
                "lines": [f"Setup of {name} is taking longer than {SETUP_TIMEOUT:.0f}s (an SSH probe hangs?). "
                          "It goes on in the background; refresh in a minute or run `hermes flota doctor`."]}
    if "error" in box:
        exc = box["error"]
        if isinstance(exc, _mod("pairing").PairingError):
            raise HTTPException(400, str(exc))
        return {"ok": False, "machine": name, "problems": [str(exc)], "lines": [f"✗ {exc}"]}
    value = box["value"]
    if isinstance(value, dict):
        return value
    code, lines = value
    problems = [line[2:] for line in lines if line.startswith("✗ ")]
    return {"ok": code == 0, "machine": name, "lines": lines, "problems": problems}


def _code(body: Mapping, key: str) -> str:
    value = body.get(key) if isinstance(body, Mapping) else None
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(400, f"{key} is required")
    if len(value) > _mod("pairing").MAX_CODE + 512:
        raise HTTPException(400, "the code is too long")
    return value


@router.post("/role")
def save_role(body: dict = Body(...)):
    """``{role: 'flagship'|'ship'|null, force?: bool}``. 409 ``{error, needs: 'remove_ships', ships}``
    for a flagship with ships, ``{error, needs: 'leave_fleet', fleet}`` for a ship in a fleet,
    unless ``force``."""
    cli = _mod("cli")
    force = body.get("force", False)
    if not isinstance(force, bool):
        raise HTTPException(400, "force must be true or false")
    try:
        result = cli.set_role(body.get("role"), force=force)
    except cli.RoleConflict as exc:
        return JSONResponse({"error": str(exc), "detail": str(exc), "needs": exc.needs, **exc.extra},
                            status_code=409)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (RuntimeError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, **result}


@router.get("/invite")
def invite():
    try:
        return _mod("cli").run_invite()
    except _mod("pairing").PairingError as exc:
        raise HTTPException(500, str(exc)) from exc
    except (RuntimeError, OSError) as exc:
        raise HTTPException(500, f"could not make an invite: {exc}") from exc


@router.post("/join")
def join(body: dict = Body(...)):
    code = _code(body, "code")
    cli = _mod("cli")
    try:
        return cli.run_join(code)
    except cli.AlreadyInFleet as exc:
        return JSONResponse({"error": "already in a fleet", "fleet": exc.fleet, "detail": str(exc)},
                            status_code=409)
    except _mod("pairing").PairingError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (RuntimeError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/join/check")
def join_check():
    return _mod("cli").join_check()


@router.get("/fleets")
def list_fleets():
    try:
        return {"fleets": _mod("cli").fleets()}
    except (RuntimeError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.delete("/fleets/{flagship}")
def leave_fleet(flagship: str):
    try:
        removed = _mod("cli").leave_fleet(flagship)
    except KeyError:
        raise HTTPException(404, f"not in a fleet led by {flagship!r}") from None
    except (RuntimeError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "flagship": flagship, "removed_keys": removed, "role": None}


@router.post("/ships")
def add_ship(body: dict = Body(...)):
    code = _code(body, "ship_code")
    override = body.get("name")
    if override in ("", None):
        override = None
    elif not isinstance(override, str) or not _NAME.fullmatch(override.strip()):
        raise HTTPException(400, "name must be lowercase letters, digits, '-' or '_' (it names the "
                                 "profile flota-<name>)")
    else:
        override = override.strip()
    pairing = _mod("pairing")
    try:
        name = override or pairing.decode(pairing.SHIP_PREFIX, code).get("name")
    except pairing.PairingError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _in_worker(lambda: _mod("cli").run_add_ship(code, name=override), str(name or "ship"))
