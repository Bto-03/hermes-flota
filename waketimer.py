"""Let a ship wake itself: set its own wake alarm right before Flota suspends it.

A suspended machine can only be woken from outside by a Wake-on-LAN packet from its own LAN. A ship
with nothing else on its network (reached only over Tailscale) cannot be woken on demand, but it can
wake itself at a set time. Flota arms that alarm for the next cron job that names the machine,
``timer_lead`` seconds early, and only then suspends it. With no upcoming job nothing is armed and the
machine sleeps until someone turns it on.

Every alarm is set relative to now ("in N seconds"), so the ship's time zone and an RTC kept in local
time (dual boot) do not matter.

What needs root, per OS (checked by ``capability``; ``setup_lines`` prints the one-time commands,
Flota never runs them):

* Linux: the RTC alarm is ``/sys/class/rtc/rtc0/wakealarm``, writable only by root, and ``rtcwake``
  writes the same file. A ``systemd-run --user`` timer with ``WakeSystem=true`` does not help: that
  needs CAP_WAKE_ALARM and only works in the system manager. So it takes one root step, either a udev
  rule that makes wakealarm writable by the user's group (then no sudo at all when arming), or a
  sudoers line for ``rtcwake -m no -s *``.
* Windows: a scheduled task with "Wake the computer to run this task" (``-WakeToRun``) for the
  current user needs no admin. The power plan must allow wake timers (RTCWAKE = 1; 2 means
  "important only", which ignores scheduled tasks).
* macOS: ``pmset relative wake N`` needs root: one sudoers line.
"""
import base64
import json
import logging
import os as _os
import re
import shlex
import subprocess
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from .power import ssh_argv

logger = logging.getLogger(__name__)

DEFAULT_LEAD = 120.0  # wake this long before the job, so it is up when the job routes to it
MIN_AHEAD = 60.0  # an alarm closer than this may fire before the machine is even asleep
TASK_NAME = "FlotaWake"
_TIMEOUT = 20
_WAKEALARM = "/sys/class/rtc/rtc0/wakealarm"


class WakeTimerError(Exception):
    pass


def uses_timer(machine) -> bool:
    return getattr(machine, "wake", "") == "timer" or getattr(machine, "wake_timer", False) is True


# --- when -----------------------------------------------------------------------------------------

def _epoch(raw) -> float | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.astimezone()  # naive: local time, as the cron store writes it
    return when.astimezone(timezone.utc).timestamp()


def _mentions(text: str, name: str) -> bool:
    # Whole word, case-insensitive: 'flota-studio' names studio, 'studios' does not.
    return re.search(rf"(?<![a-z0-9]){re.escape(name.lower())}(?![a-z0-9])", text.lower()) is not None


def targets(job: Mapping, name: str) -> bool:
    """True when the job names the machine: its ``machine``/``machines`` field (as GET /schedule
    returns them) or its name, prompt or skills, matched like the schedule view does."""
    machines = job.get("machines")
    if job.get("machine") == name or (isinstance(machines, list) and name in machines):
        return True
    skills = job.get("skills") or ([job["skill"]] if job.get("skill") else [])
    words = [str(job.get("name") or ""), str(job.get("prompt") or "")]
    words += [str(s) for s in skills] if isinstance(skills, list) else []
    return _mentions(" ".join(words), name)


def next_job_for(machine_name: str, jobs, *, now: float | None = None,
                 others=None) -> tuple[float, Mapping] | None:
    """(run time, job) of the soonest enabled future job that names the machine. ``others``: the
    other machines' names when this one is the fleet's ``default``; it gets every task without a
    ``machine``, so a job that names none of the others counts too."""
    now = time.time() if now is None else now
    best = None
    for job in jobs or ():
        if not isinstance(job, Mapping) or job.get("enabled", True) is False:
            continue
        if job.get("state") in ("paused", "completed", "disabled"):
            continue
        when = _epoch(job.get("next_run_at"))
        if when is None or when <= now:
            continue
        if not targets(job, machine_name) and (
                others is None or any(targets(job, other) for other in others)):
            continue
        if best is None or when < best[0]:
            best = (when, job)
    return best


def next_wake_for(machine_name: str, jobs, *, lead: float = DEFAULT_LEAD,
                  now: float | None = None) -> float | None:
    """Epoch at which the machine should wake for its next job (``lead`` seconds early, never
    sooner than MIN_AHEAD from now), or None when no upcoming job names it.

    ``jobs``: dicts {name, prompt, next_run_at (ISO or epoch), enabled, state?, machine?, machines?,
    skills?}, as the core cron store or GET /schedule return them."""
    now = time.time() if now is None else now
    found = next_job_for(machine_name, jobs, now=now)
    if found is None:
        return None
    return max(found[0] - lead, now + MIN_AHEAD)


def cron_jobs():
    """This Hermes's cron jobs (paused ones included, they are filtered later), or None when the
    core cron store cannot be read."""
    try:
        import importlib
        from .home import owner_scope
        with owner_scope():
            return importlib.import_module("cron.jobs").list_jobs(include_disabled=True)
    except Exception:
        logger.warning("flota: could not read the cron jobs", exc_info=True)
        return None


def others_when_default(machine_name: str):
    """The other machines' names when ``machine_name`` is the configured ``default``, else None."""
    try:
        from hermes_cli.config import load_config_readonly
        from .home import owner_scope
        from .settings import effective, plugin_settings_from_config
        with owner_scope():
            config = load_config_readonly() or {}
        fleet = effective(config.get("delegation"), plugin_settings_from_config(config))
        default = fleet.get("default")
        if not isinstance(default, str) or default.strip() != machine_name:
            return None
        return [str(name).strip() for name in fleet["machines"] if str(name).strip() != machine_name]
    except Exception:
        return None


def format_when(when: float, now: float | None = None) -> str:
    """Local time of the flagship: '02:00', or 'Fri 15 02:00' when more than a day away."""
    now = time.time() if now is None else now
    local = time.localtime(when)
    return time.strftime("%H:%M" if when - now < 86400 else "%a %d %H:%M", local)


# --- state (read by ensure_ready, in any process) -------------------------------------------------

def state_path() -> Path | None:
    try:
        from .home import flota_home
        return flota_home() / "flota" / "waketimers.json"
    except Exception:
        return None


def _read(path) -> dict:
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def armed_at(name: str, path=None) -> float | None:
    path = path if path is not None else state_path()
    value = _read(path).get(name) if path is not None else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _record(path, name: str, when: float | None) -> None:
    """Remember the alarm of ``name``; ``when=None`` forgets it."""
    if path is None:
        return
    try:
        path = Path(path)
        data = _read(path)
        if when is None and name not in data:
            return
        data[name] = when
        if when is None:
            del data[name]
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{_os.getpid()}.tmp")
        tmp.write_text(json.dumps(data))
        _os.replace(tmp, path)
    except OSError:
        logger.debug("flota: could not record the wake timer", exc_info=True)


# --- remote scripts -------------------------------------------------------------------------------
# POSIX scripts run under `sh -c` (the login shell may be fish): no backslash, no single quote.

def _linux_arm(seconds: int) -> str:
    return (f"f={_WAKEALARM}; "
            f"if [ -w $f ]; then echo 0 > $f && echo +{seconds} > $f && echo armed=direct; "
            f"elif sudo -k -n -l rtcwake -m no -s {seconds} >/dev/null 2>&1; then "
            f"sudo -n rtcwake -m no -s {seconds} >/dev/null && echo armed=sudoers; "
            "else echo armed=no; exit 3; fi")


def _macos_arm(seconds: int) -> str:
    return (f"if sudo -k -n -l /usr/bin/pmset relative wake {seconds} >/dev/null 2>&1; then "
            f"sudo -n /usr/bin/pmset relative wake {seconds} && echo armed=sudoers; "
            "else echo armed=no; exit 3; fi")


# Current AC and DC values of "Allow wake timers" (0 off, 1 on, 2 important only). The labels are
# translated, the 0x values are not.
_PS_RTCWAKE = ("$v = powercfg /qh SCHEME_CURRENT SUB_SLEEP RTCWAKE | Select-String '0x[0-9a-fA-F]{8}' "
               "-AllMatches | ForEach-Object { $_.Matches.Value }\n"
               "'rtcwake=' + ($v -join ',')\n")
_PS_TASK = ("$a = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '/c exit 0'\n"
            "$s = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries "
            "-DontStopIfGoingOnBatteries\n")


def _windows_arm(seconds: int) -> str:
    return ("$ErrorActionPreference = 'Stop'\n" + _PS_TASK
            + f"$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds({seconds})\n"
            f"$r = Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t -Settings $s -Force\n"
            "'armed=' + $r.Settings.WakeToRun\n" + _PS_RTCWAKE)


_LINUX_PROBE = (f"f={_WAKEALARM}; [ -e $f ] || {{ echo rtc=missing; exit 0; }}; echo rtc=yes; "
                "[ -w $f ] && echo route=direct; "
                "sudo -k -n -l rtcwake -m no -s 60 >/dev/null 2>&1 && echo route=sudoers; "
                "echo user=$(id -un); echo group=$(id -gn); echo rtcwake=$(command -v rtcwake); true")
_MACOS_PROBE = ("sudo -k -n -l /usr/bin/pmset relative wake 60 >/dev/null 2>&1 && echo route=sudoers; "
                "echo user=$(id -un); true")
_WINDOWS_PROBE = (
    "try {\n" + _PS_TASK
    + "$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddYears(1)\n"
    "$r = Register-ScheduledTask -TaskName 'FlotaWakeProbe' -Action $a -Trigger $t -Settings $s "
    "-Force -ErrorAction Stop\n"
    "'route=' + $r.Settings.WakeToRun\n"
    "Unregister-ScheduledTask -TaskName 'FlotaWakeProbe' -Confirm:$false\n"
    "} catch { 'route=error ' + $_.Exception.Message }\n" + _PS_RTCWAKE + "'user=' + $env:USERNAME\n")


def _encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode()


def _remote(os: str, posix: str, powershell: str) -> str:
    if os == "windows":
        return f"powershell -NoProfile -EncodedCommand {_encoded(powershell)}"
    return "sh -c " + shlex.quote(posix)


def _kv(out: str) -> dict:
    found: dict = {}
    for line in (out or "").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key not in found:
            found[key] = value.strip()
    return found


def _rtcwake_problem(kv: dict) -> str:
    values = [v.lower() for v in kv.get("rtcwake", "").split(",") if v]
    if values and values[0] != "0x00000001":
        return ("the power plan does not allow wake timers (RTCWAKE is "
                f"{'off' if values[0] == '0x00000000' else 'important only'})")
    return ""


# --- arm ------------------------------------------------------------------------------------------

def arm(machine, when_epoch: float, run=subprocess.run, *, now=time.time, state_file=...) -> str:
    """Set the machine's own wake alarm for ``when_epoch`` over SSH. Raises WakeTimerError."""
    seconds = int(round(when_epoch - now()))
    if seconds < 1:
        raise WakeTimerError(f"{machine.name}: wake time is in the past")
    os = getattr(machine, "os", "linux")
    posix = _macos_arm(seconds) if os == "macos" else _linux_arm(seconds)
    try:
        done = run(ssh_argv(machine, _remote(os, posix, _windows_arm(seconds))), timeout=_TIMEOUT,
                   capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        raise WakeTimerError(f"{machine.name}: setting the wake timer timed out after {_TIMEOUT}s")
    except (OSError, ValueError) as exc:
        raise WakeTimerError(f"{machine.name}: could not set the wake timer: {exc}") from exc
    kv = _kv(done.stdout)
    if done.returncode == 255:
        lines = (done.stderr or "").strip().splitlines()
        raise WakeTimerError(f"{machine.name}: unreachable" + (f": {lines[-1]}" if lines else ""))
    if kv.get("armed") == "no":
        raise WakeTimerError(f"{machine.name}: setting its wake alarm needs root once "
                             f"(see {SETUP_DOC})")
    if done.returncode != 0 or kv.get("armed") not in ("direct", "sudoers", "True"):
        lines = (done.stderr or "").strip().splitlines()
        raise WakeTimerError(f"{machine.name}: could not set the wake timer (exit {done.returncode})"
                             + (f": {lines[-1]}" if lines else ""))
    problem = _rtcwake_problem(kv) if os == "windows" else ""
    if problem:
        raise WakeTimerError(f"{machine.name}: {problem}; run "
                             "`powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1` there")
    _record(state_path() if state_file is ... else state_file, machine.name, when_epoch)
    return f"{machine.name} wakes itself at {format_when(when_epoch, now())}"


def _clear(machine, run) -> bool:
    """Best effort: cancel the alarm set earlier. macOS and the Linux sudoers route have no
    password-less way to do it."""
    os = getattr(machine, "os", "linux")
    if os == "macos":
        return False
    posix = f"f={_WAKEALARM}; [ -w $f ] && echo 0 > $f && echo cleared=yes"
    powershell = (f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false "
                  "-ErrorAction SilentlyContinue\n'cleared=yes'\n")
    try:
        done = run(ssh_argv(machine, _remote(os, posix, powershell)), timeout=_TIMEOUT,
                   capture_output=True, text=True)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return False
    return done.returncode == 0 and _kv(done.stdout).get("cleared") == "yes"


def arm_before_sleep(machine, run=subprocess.run, jobs=None, now=time.time, others=None) -> str:
    """Called by power.power before a suspend/hibernate. Returns a note for the result line;
    raises WakeTimerError when the machine must not be put to sleep. ``others``: see next_job_for
    (looked up in the config when the jobs are)."""
    if jobs is None:
        jobs = cron_jobs()
        if jobs is None:
            raise WakeTimerError("cannot read the cron jobs, so its wake timer cannot be set")
        others = others_when_default(machine.name)
    current = now()
    found = next_job_for(machine.name, jobs, now=current, others=others)
    if found is None:
        note = "no upcoming job names it, so it sleeps until someone turns it on"
        path = state_path()
        old = armed_at(machine.name, path)
        if old is not None:  # an alarm armed for a job that is gone: no ghost wake-up
            if old <= current or _clear(machine, run):
                _record(path, machine.name, None)
            else:
                note += f" (its earlier wake timer at {format_when(old, current)} could not be cleared)"
        return note
    lead = getattr(machine, "timer_lead", DEFAULT_LEAD)
    when = max(found[0] - lead, current + MIN_AHEAD)
    arm(machine, when, run=run, now=lambda: current)
    label = str(found[1].get("name") or "cron job")
    return f"wake timer set for {format_when(when, current)}, for '{label}'"


# --- capability -----------------------------------------------------------------------------------

SETUP_DOC = "docs/power.md#wake-timers"


def setup_lines(os: str, user: str = "", group: str = "", rtcwake: str = "") -> list[str]:
    """One-time steps that let Flota arm the wake timer. Printed, never run by Flota.

    Route A on Linux needs no privilege when arming, so its exact commands are printed. The
    passwordless-sudo routes (Linux B, macOS) touch the sudoers policy; Flota only names the
    one command to allow and points at the documentation, where the full rule is reviewed."""
    user = user or "YOUR_USER"
    if os == "windows":
        return ["powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1",
                "powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1",
                "powercfg /setactive SCHEME_CURRENT"]
    if os == "macos":
        return [f"As root, once: a sudoers rule that lets {user} run `/usr/bin/pmset relative wake *` "
                "without a password.",
                f"Exact rule, file and undo steps: {SETUP_DOC}"]
    group = group or user
    rule = ('ACTION=="add", SUBSYSTEM=="rtc", KERNEL=="rtc0", '
            f'RUN+="/bin/chgrp {group} /sys%p/wakealarm", RUN+="/bin/chmod g+w /sys%p/wakealarm"')
    return [
        "# Either A, as root once (then arming needs no privilege: the user's group may write the RTC alarm):",
        f"echo '{rule}' | tee /etc/udev/rules.d/90-flota-wakealarm.rules",
        f"chgrp {group} {_WAKEALARM}",
        f"chmod g+w {_WAKEALARM}",
        f"# or B: a sudoers rule that lets {user} run `{rtcwake or '/usr/bin/rtcwake'} -m no -s *` "
        "without a password",
        f"# exact rule, file and undo steps for both: {SETUP_DOC}",
    ]


def capability(target=True, run=subprocess.run, os: str | None = None) -> dict:
    """Can this host (``target=True``, e.g. a ship joining a fleet) or ``target`` (a Machine, over
    SSH) arm its own wake timer without a password? {ok, os, route, reason, setup}."""
    if target is True:
        from .detect import local_os
        os = os or local_os()
    else:
        os = os or getattr(target, "os", "linux")
    posix = _MACOS_PROBE if os == "macos" else _LINUX_PROBE
    result = {"ok": False, "os": os, "route": "", "reason": "", "setup": []}
    try:
        if target is True:
            argv = (["powershell", "-NoProfile", "-EncodedCommand", _encoded(_WINDOWS_PROBE)]
                    if os == "windows" else ["sh", "-c", posix])
        else:
            argv = ssh_argv(target, _remote(os, posix, _WINDOWS_PROBE))
        done = run(argv, timeout=_TIMEOUT, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return {**result, "reason": f"unreachable: timed out after {_TIMEOUT}s"}
    except (OSError, ValueError) as exc:
        return {**result, "reason": f"unreachable: {exc}"}
    if target is not True and done.returncode == 255:
        lines = (done.stderr or "").strip().splitlines()
        return {**result, "reason": "unreachable" + (f": {lines[-1]}" if lines else "")}

    out = done.stdout or ""
    kv = _kv(out)
    routes = [line.strip()[6:] for line in out.splitlines() if line.strip().startswith("route=")]
    setup = setup_lines(os, kv.get("user", ""), kv.get("group", ""), kv.get("rtcwake", ""))
    if os == "windows":
        route = kv.get("route", "")
        if route != "True":
            why = route[6:] if route.startswith("error ") else "no WakeToRun task could be registered"
            return {**result, "reason": f"Task Scheduler refused: {why}", "setup": setup}
        problem = _rtcwake_problem(kv)
        if problem:
            return {**result, "route": "task", "reason": problem, "setup": setup}
        return {**result, "ok": True, "route": "task",
                "reason": "a WakeToRun task for this user needs no admin"}
    if os == "linux" and kv.get("rtc") == "missing":
        return {**result, "reason": f"no RTC alarm at {_WAKEALARM}"}
    for route in ("direct", "sudoers"):
        if route in routes:
            how = "the RTC alarm is writable" if route == "direct" else "a sudoers rule allows it without a password"
            return {**result, "ok": True, "route": route, "reason": how}
    return {**result, "reason": "setting the wake alarm needs root once", "setup": setup}
