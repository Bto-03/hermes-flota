"""Suspend, hibernate or power off a fleet machine over SSH."""
import re
import shlex
import subprocess
import time

ACTIONS = ("suspend", "hibernate", "poweroff")

# Over SSH polkit answers "challenge": the SSH session is not the active seat session. The
# same call spawned inside the graphical session is allowed. The niri socket name changes on
# every login, so it is looked up at run time. One single-quoted word with no backslashes:
# fish and bash (the remote login shell runs the line) both pass it to sh untouched, and the
# command that follows reaches `niri msg action spawn` as "$@".
NIRI_PREFIX = ("sh -c 'NIRI_SOCKET=$(ls /run/user/$(id -u)/niri.*.sock | head -1) "
               "exec niri msg action spawn -- \"$@\"' _")

_LABELS = {"poweroff": "power off"}
_CAN_SUSPEND = ("busctl call org.freedesktop.login1 /org/freedesktop/login1 "
                "org.freedesktop.login1.Manager CanSuspend")
# What ssh prints when sshd vanished mid-session, which is what a successful suspend or
# poweroff does. "kex_exchange_identification: Connection closed by remote host" is the same
# words before login, so it is excluded.
_DROPPED = re.compile(r"closed by remote host|client_loop: send disconnect")


def default_action(machine) -> str:
    return machine.sleep if machine.sleep != "none" else "suspend"


# macOS: pmset and System Events need no sudo. There is no user-space hibernate (it is a
# pmset hibernatemode setting applied on sleep), so it is refused rather than faked.
_MACOS = {"suspend": "pmset sleepnow",
          "poweroff": "osascript -e 'tell application \"System Events\" to shut down'"}
# Windows: OpenSSH hands the line to cmd.exe or PowerShell, whichever is the default shell.
# Only double quotes group words in cmd, and PowerShell expands $ inside double quotes, so
# the inner script uses single quotes and 0 for $false (SetSuspendState(state, force,
# disableWakeEvent) takes bools; PowerShell converts 0).
_WINDOWS = {
    "suspend": ('powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; '
                "[System.Windows.Forms.Application]::SetSuspendState('Suspend', 0, 0)\""),
    "hibernate": "shutdown /h",
    "poweroff": "shutdown /s /t 0"}


def remote_command(machine, action) -> str:
    if action not in ACTIONS:
        raise ValueError(f"unknown power action: {action!r}")
    if machine.os == "windows":
        return _WINDOWS[action]
    if machine.os == "macos":
        if action not in _MACOS:
            raise ValueError("macOS cannot hibernate on demand; use suspend")
        return _MACOS[action]
    command = f"systemctl {action}"
    return f"{machine.power_prefix} {command}" if machine.power_prefix else command


def ssh_argv(machine, remote: str) -> list[str]:
    # A leading "-" would be read by ssh as an option (e.g. -oProxyCommand=...).
    if machine.host.startswith("-") or machine.user.startswith("-"):
        raise ValueError(f"invalid ssh target: {machine.user}@{machine.host}")
    target = f"{machine.user}@{machine.host}" if machine.user else machine.host
    return (["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-p", str(machine.port)]
            + (["-i", machine.key] if machine.key else []) + [target, remote])


def _failure(done) -> str:
    stderr = (done.stderr or "").strip()
    if done.returncode == 0 or (done.returncode == 255 and _DROPPED.search(stderr)
                                and "kex_exchange_identification" not in stderr):
        return ""
    return f"exit {done.returncode}: {stderr}"


def power(machine, action, run=subprocess.run, jobs=None, now=time.time) -> str:
    """``jobs``/``now`` only matter for a machine that wakes on its own timer: its alarm is set for
    the next cron job that names it (``jobs`` defaults to this Hermes's cron store) before it sleeps."""
    note = ""
    try:
        if action in ("suspend", "hibernate"):
            from . import waketimer  # lazy: waketimer imports ssh_argv from here
            if waketimer.uses_timer(machine):
                try:
                    note = waketimer.arm_before_sleep(machine, run=run, jobs=jobs, now=now)
                except waketimer.WakeTimerError as exc:
                    return f"{machine.name}: {action} failed: {exc}; not suspended, or it could not wake again"
        if machine.sleep_command and action == default_action(machine):
            done = run(machine.sleep_command, shell=True, timeout=30, capture_output=True,
                       text=True)
        else:
            done = run(ssh_argv(machine, remote_command(machine, action)), timeout=30,
                       capture_output=True, text=True)
        reason = _failure(done)
    except subprocess.TimeoutExpired:
        reason = "timed out after 30s"
    except (OSError, ValueError) as exc:
        reason = str(exc)
    if reason:
        return f"{machine.name}: {action} failed: {reason}"
    return f"{machine.name}: {_LABELS.get(action, action)} sent" + (f"; {note}" if note else "")


def _niri_check() -> str:
    """POSIX script: exit 3 without a niri session, else print CanSuspend as asked from
    inside it. A spawned process's output goes to the session, not to us, so it goes
    through a file."""
    inner = shlex.quote(_CAN_SUSPEND + ' > "$1"')
    return ('d=/run/user/$(id -u); ls $d/niri.*.sock >/dev/null 2>&1 || exit 3; '
            'f=$d/flota-can-suspend; rm -f "$f"; '
            f'{NIRI_PREFIX} sh -c {inner} _ "$f" || exit 4; '
            'for i in 1 2 3 4 5; do [ -s "$f" ] && break; sleep 1; done; cat "$f"; rm -f "$f"')


def _polkit_rule(user: str) -> str:
    return (
        "polkit.addRule(function(action, subject) {\n"
        f'    if (subject.user == "{user}" && [\n'
        '            "org.freedesktop.login1.suspend",\n'
        '            "org.freedesktop.login1.hibernate",\n'
        '            "org.freedesktop.login1.power-off"].indexOf(action.id) >= 0) {\n'
        "        return polkit.Result.YES;\n"
        "    }\n"
        "});")


def detect_prefix(machine, run=subprocess.run) -> tuple[str | None, str]:
    if machine.os == "macos":
        return "", "pmset sleepnow needs no password"
    if machine.os == "windows":
        return "", "Windows allows the logged-in user to suspend"

    def ask(remote):
        return run(ssh_argv(machine, remote), timeout=15, capture_output=True, text=True)

    try:
        direct = ask(_CAN_SUSPEND)
        if direct.returncode == 255:
            return None, f"unreachable: {(direct.stderr or '').strip()}"
        if '"yes"' in (direct.stdout or ""):
            return "", "systemctl works directly"
        if '"yes"' in (ask("sh -c " + shlex.quote(_niri_check())).stdout or ""):
            return NIRI_PREFIX, "via the niri session"
    except subprocess.TimeoutExpired:
        return None, "unreachable: ssh timed out after 15s"
    except (OSError, ValueError) as exc:
        return None, f"unreachable: {exc}"
    user = machine.user or "YOUR_USER"
    return None, (
        f"{machine.name}: polkit does not let {user} suspend over SSH and no niri session "
        f"was found. Install this rule once, as root, at /etc/polkit-1/rules.d/49-flota.rules "
        f"(e.g. with `tee` from a root shell), then Flota can run systemctl directly:\n\n{_polkit_rule(user)}")


# One probe for every login shell. bash, zsh and fish (3+) run `uname -s` and stop there:
# Darwin or Linux. cmd.exe has no uname, so `||` runs its `ver` builtin, which prints
# "Microsoft Windows [...]" in every locale. PowerShell either rejects `||` (5.1: ParserError)
# or runs it and fails on uname/ver (7: CommandNotFoundException); those error ids are not
# translated, and a redirected PowerShell may serialise errors as "#< CLIXML".
# uname from Git for Windows / Cygwin on the PATH prints MINGW*/MSYS*/CYGWIN*.
_OS_PROBE = "uname -s || ver"
_WINDOWS_MARKERS = re.compile(r"Microsoft Windows|Windows_NT|CommandNotFoundException|"
                              r"ParserError|InvalidEndOfLine|#< CLIXML|^(MINGW|MSYS|CYGWIN)",
                              re.MULTILINE)


def detect_os(machine, run=subprocess.run) -> str:
    """'linux' | 'macos' | 'windows', or '' when unreachable or unrecognised."""
    try:
        done = run(ssh_argv(machine, _OS_PROBE), timeout=10, capture_output=True, text=True)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return ""
    if done.returncode == 255:
        return ""
    out = (done.stdout or "").strip()
    first = out.split()[0] if out else ""
    if first == "Linux":
        return "linux"
    if first == "Darwin":
        return "macos"
    if _WINDOWS_MARKERS.search(out + "\n" + (done.stderr or "")):
        return "windows"
    return ""
