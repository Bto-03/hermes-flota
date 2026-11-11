import os
import shlex
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from flota_plugin import power
from flota_plugin.machines import Machine


def machine(**kw):
    return Machine(**{"name": "desk", "profile": "p", "host": "desk.lan", **kw})


class Run:
    """Fake subprocess.run: answers from a queue, records every call."""

    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        result = self.results.pop(0) if self.results else ok()
        if isinstance(result, BaseException):
            raise result
        return result


def ok(stdout=""):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def fail(code, stderr):
    return SimpleNamespace(returncode=code, stdout="", stderr=stderr)


def test_actions():
    assert power.ACTIONS == ("suspend", "hibernate", "poweroff")


@pytest.mark.parametrize("action", power.ACTIONS)
def test_remote_command_without_prefix(action):
    assert power.remote_command(machine(), action) == f"systemctl {action}"


def test_remote_command_with_prefix():
    assert power.remote_command(machine(power_prefix="run-in"), "suspend") == "run-in systemctl suspend"


@pytest.mark.parametrize("action", ["none", "reboot", "", "Suspend"])
def test_remote_command_rejects_unknown_action(action):
    with pytest.raises(ValueError):
        power.remote_command(machine(), action)


def test_ssh_argv_host_only():
    assert power.ssh_argv(machine(), "systemctl suspend") == [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-p", "22",
        "desk.lan", "systemctl suspend"]


def test_ssh_argv_with_user_key_and_port():
    m = machine(user="alice", key="~/.ssh/flota", port=2222)
    assert power.ssh_argv(m, "x") == [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-p", "2222",
        "-i", "~/.ssh/flota", "alice@desk.lan", "x"]


@pytest.mark.parametrize("kw", [{"host": "-oProxyCommand=evil"}, {"user": "-oProxyCommand=evil"}])
def test_ssh_argv_rejects_option_injection(kw):
    with pytest.raises(ValueError):
        power.ssh_argv(machine(**kw), "x")


SSH_KWARGS = {"timeout": 30, "capture_output": True, "text": True}


@pytest.mark.parametrize("action, line", [
    ("suspend", "desk: suspend sent"),
    ("hibernate", "desk: hibernate sent"),
    ("poweroff", "desk: power off sent"),
])
def test_power_runs_ssh_without_a_shell(action, line):
    run = Run()
    assert power.power(machine(user="u"), action, run=run) == line
    assert run.calls == [(power.ssh_argv(machine(user="u"), f"systemctl {action}"), SSH_KWARGS)]


def test_power_uses_the_prefix():
    run = Run()
    power.power(machine(power_prefix=power.NIRI_PREFIX), "suspend", run=run)
    assert run.calls[0][0][-1] == f"{power.NIRI_PREFIX} systemctl suspend"


@pytest.mark.parametrize("stderr", [
    "Connection to desk.lan closed by remote host.\n",
    "client_loop: send disconnect: Broken pipe\n",
    "client_loop: send disconnect: Connection reset by peer\n",
])
def test_connection_dropped_by_the_sleeping_machine_counts_as_sent(stderr):
    # A successful suspend/poweroff often takes sshd down before ssh hears the exit status.
    assert power.power(machine(), "poweroff", run=Run(fail(255, stderr))) == "desk: power off sent"


@pytest.mark.parametrize("result, reason", [
    (fail(255, "ssh: connect to host desk.lan port 22: No route to host\n"),
     "exit 255: ssh: connect to host desk.lan port 22: No route to host"),
    (fail(255, "kex_exchange_identification: Connection closed by remote host\n"),
     "exit 255: kex_exchange_identification: Connection closed by remote host"),
    (fail(255, "u@desk.lan: Permission denied (publickey).\n"),
     "exit 255: u@desk.lan: Permission denied (publickey)."),
    (fail(1, "Failed to suspend system via logind: Access denied\n"),
     "exit 1: Failed to suspend system via logind: Access denied"),
    (subprocess.TimeoutExpired("ssh", 30), "timed out after 30s"),
    (OSError("ssh not found"), "ssh not found"),
])
def test_power_failure_is_one_line_and_never_raises(result, reason):
    assert power.power(machine(), "suspend", run=Run(result)) == f"desk: suspend failed: {reason}"


def test_power_unknown_action_and_injection_do_not_raise():
    run = Run()
    assert power.power(machine(), "reboot", run=run).startswith("desk: reboot failed: ")
    assert power.power(machine(host="-oX=y"), "suspend", run=run).startswith("desk: suspend failed: ")
    assert run.calls == []


def test_sleep_command_overrides_the_configured_action():
    run = Run()
    m = machine(sleep="hibernate", sleep_command="ssh desk zzz")
    assert power.power(m, "hibernate", run=run) == "desk: hibernate sent"
    assert run.calls == [("ssh desk zzz", {"shell": True, **SSH_KWARGS})]


def test_sleep_command_is_the_suspend_override_when_sleep_is_none():
    # Configs written before `sleep` existed only have sleep_command, which meant "suspend".
    run = Run()
    power.power(machine(sleep_command="zzz"), "suspend", run=run)
    assert run.calls == [("zzz", {"shell": True, **SSH_KWARGS})]


def test_sleep_command_is_not_used_for_other_actions():
    run = Run()
    power.power(machine(sleep="suspend", sleep_command="zzz"), "poweroff", run=run)
    assert run.calls[0][0][0] == "ssh"


def test_failed_sleep_command():
    m = machine(sleep_command="zzz")
    assert power.power(m, "suspend", run=Run(fail(2, "nope\n"))) == "desk: suspend failed: exit 2: nope"


@pytest.mark.parametrize("m, expected", [
    (machine(), "suspend"), (machine(sleep="hibernate"), "hibernate"),
    (machine(sleep="poweroff"), "poweroff")])
def test_default_action(m, expected):
    assert power.default_action(m) == expected


# --- NIRI_PREFIX -------------------------------------------------------------------------

def test_niri_prefix_is_one_sh_invocation_taking_the_command_as_arguments():
    words = shlex.split(f"{power.NIRI_PREFIX} systemctl suspend")
    assert words[:2] == ["sh", "-c"]
    assert words[3:] == ["_", "systemctl", "suspend"]
    script = words[2]
    assert "/run/user/$(id -u)/niri.*.sock" in script
    assert 'niri msg action spawn -- "$@"' in script


def test_niri_prefix_survives_fish_quoting():
    # fish treats backslashes inside single quotes specially; there must be none, and the
    # script must be a single single-quoted word so neither fish nor bash expands it.
    assert "\\" not in power.NIRI_PREFIX
    assert power.NIRI_PREFIX.count("'") == 2


@pytest.mark.parametrize("shell", ["sh", "bash", "fish"])
def test_composed_line_runs_inside_the_niri_session(tmp_path, shell):
    if not shutil.which(shell):
        pytest.skip(f"{shell} not installed")
    record = tmp_path / "argv"
    fake = tmp_path / "bin"
    fake.mkdir()
    # Fake `ls` stands in for the real /run/user listing; fake `niri` records how it was called.
    (fake / "ls").write_text('#!/bin/sh\necho /run/user/1000/niri.wayland-1.4242.sock\n')
    (fake / "niri").write_text(
        f'#!/bin/sh\necho "$NIRI_SOCKET" > {record}\nfor a in "$@"; do echo "$a" >> {record}; done\n')
    for f in fake.iterdir():
        f.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}"}
    line = f"{power.NIRI_PREFIX} systemctl suspend"
    subprocess.run([shell, "-c", line], env=env, check=True, timeout=10)
    assert record.read_text().splitlines() == [
        "/run/user/1000/niri.wayland-1.4242.sock",
        "msg", "action", "spawn", "--", "systemctl", "suspend"]


# --- detect_prefix -----------------------------------------------------------------------

YES, CHALLENGE = ok('s "yes"\n'), ok('s "challenge"\n')


def test_detect_direct_systemctl():
    run = Run(YES)
    assert power.detect_prefix(machine(user="u"), run=run) == ("", "systemctl works directly")
    [(argv, kwargs)] = run.calls
    assert argv[:-1] == power.ssh_argv(machine(user="u"), "")[:-1]
    assert "CanSuspend" in argv[-1]
    assert kwargs == {"timeout": 15, "capture_output": True, "text": True}


def test_detect_via_niri_session():
    run = Run(CHALLENGE, YES)
    assert power.detect_prefix(machine(), run=run) == (power.NIRI_PREFIX, "via the niri session")
    assert len(run.calls) == 2
    sh, dash_c, script = shlex.split(run.calls[1][0][-1])
    assert (sh, dash_c) == ("sh", "-c")
    assert power.NIRI_PREFIX in script and "CanSuspend" in script


@pytest.mark.skipif(not os.path.isdir("/run/user"), reason="needs /run/user")
@pytest.mark.parametrize("has_session, expected", [(True, 's "yes"'), (False, None)])
@pytest.mark.parametrize("shell", ["bash", "fish"])
def test_niri_check_script_runs_busctl_inside_the_session(tmp_path, has_session, expected, shell):
    if not shutil.which(shell):
        pytest.skip(f"{shell} not installed")
    fake = tmp_path / "bin"
    fake.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    if has_session:
        (runtime / "niri.wayland-1.77.sock").touch()
    # `id -u` is the only part of /run/user/<uid> we control: climb out to the temp dir.
    (fake / "id").write_text(f"#!/bin/sh\necho ../..{runtime}\n")
    # Fake niri "spawns" by running everything after `--`; fake busctl answers yes.
    (fake / "niri").write_text('#!/bin/sh\nshift 4\n"$@"\n')
    (fake / "busctl").write_text('#!/bin/sh\necho \'s "yes"\'\n')
    for f in fake.iterdir():
        f.chmod(0o755)
    run = Run(CHALLENGE, CHALLENGE)
    power.detect_prefix(machine(), run=run)
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}"}
    done = subprocess.run([shell, "-c", run.calls[1][0][-1]], env=env, capture_output=True,
                          text=True, timeout=20)
    if expected:
        assert done.stdout.strip() == expected
        assert not (runtime / "flota-can-suspend").exists()
    else:
        assert done.returncode == 3 and done.stdout == ""


@pytest.mark.parametrize("niri_answer", [CHALLENGE, fail(3, ""), ok("")])
def test_detect_neither_gives_a_polkit_rule(niri_answer):
    prefix, why = power.detect_prefix(machine(user="alice"), run=Run(CHALLENGE, niri_answer))
    assert prefix is None
    assert "/etc/polkit-1/rules.d/49-flota.rules" in why
    assert 'subject.user == "alice"' in why
    for action in ("suspend", "hibernate", "power-off"):
        assert f"org.freedesktop.login1.{action}" in why


@pytest.mark.parametrize("result", [
    fail(255, "ssh: connect to host desk.lan port 22: No route to host\n"),
    subprocess.TimeoutExpired("ssh", 15), OSError("no ssh")])
def test_detect_unreachable_never_raises(result):
    run = Run(result)
    prefix, why = power.detect_prefix(machine(), run=run)
    assert prefix is None and why
    assert len(run.calls) == 1


def test_detect_rejects_injection_without_running():
    run = Run()
    assert power.detect_prefix(machine(host="-oX=y"), run=run)[0] is None
    assert run.calls == []


# --- macOS and Windows ------------------------------------------------------------------

WIN_SUSPEND = ('powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; '
               "[System.Windows.Forms.Application]::SetSuspendState('Suspend', 0, 0)\"")


@pytest.mark.parametrize("action, expected", [
    ("suspend", "pmset sleepnow"),
    ("poweroff", "osascript -e 'tell application \"System Events\" to shut down'"),
])
def test_macos_commands_ignore_the_prefix(action, expected):
    assert power.remote_command(machine(os="macos", power_prefix="run-in"), action) == expected


def test_macos_poweroff_is_one_applescript_argument_for_sh():
    line = power.remote_command(machine(os="macos"), "poweroff")
    assert shlex.split(line) == ["osascript", "-e", 'tell application "System Events" to shut down']


def test_macos_hibernate_is_refused():
    with pytest.raises(ValueError, match="macOS"):
        power.remote_command(machine(os="macos"), "hibernate")
    run = Run()
    result = power.power(machine(os="macos"), "hibernate", run=run)
    assert result == "desk: hibernate failed: macOS cannot hibernate on demand; use suspend"
    assert run.calls == []


@pytest.mark.parametrize("action, expected", [
    ("suspend", WIN_SUSPEND), ("hibernate", "shutdown /h"), ("poweroff", "shutdown /s /t 0")])
def test_windows_commands_ignore_the_prefix(action, expected):
    assert power.remote_command(machine(os="windows", power_prefix="run-in"), action) == expected


def test_windows_suspend_survives_cmd_and_powershell():
    # cmd groups only on double quotes; PowerShell expands $ and ` inside them.
    inner = WIN_SUSPEND.split('-Command ', 1)[1]
    assert inner.startswith('"') and inner.endswith('"') and inner.count('"') == 2
    assert "$" not in WIN_SUSPEND and "`" not in WIN_SUSPEND
    assert not set("&|<>^%") & set(WIN_SUSPEND)


@pytest.mark.parametrize("os_name, action", [
    (o, a) for o in ("macos", "windows") for a in power.ACTIONS
    if (o, a) != ("macos", "hibernate")])
def test_power_sends_the_os_command_over_ssh(os_name, action):
    m = machine(os=os_name, user="me")
    run = Run(fail(255, "Connection to desk.lan closed by remote host."))
    assert power.power(m, action, run=run).endswith(" sent")
    assert run.calls[0][0] == power.ssh_argv(m, power.remote_command(m, action))
    assert run.calls[0][0][-1] == power.remote_command(m, action)


@pytest.mark.parametrize("os_name", ["linux", "macos", "windows"])
def test_sleep_command_overrides_on_every_os(os_name):
    run = Run()
    m = machine(os=os_name, sleep="suspend", sleep_command="my-sleep")
    assert power.power(m, "suspend", run=run) == "desk: suspend sent"
    assert run.calls[0][0] == "my-sleep" and run.calls[0][1]["shell"] is True


@pytest.mark.parametrize("os_name, reason", [
    ("macos", "pmset sleepnow needs no password"),
    ("windows", "Windows allows the logged-in user to suspend")])
def test_detect_prefix_needs_no_ssh_off_linux(os_name, reason):
    run = Run()
    assert power.detect_prefix(machine(os=os_name), run=run) == ("", reason)
    assert run.calls == []


# detect_os: realistic answers of each login shell to `uname -s || ver`.
@pytest.mark.parametrize("result, expected", [
    (ok("Linux\n"), "linux"),                                   # bash / zsh / fish
    (ok("Darwin\n"), "macos"),                                  # zsh / bash on macOS
    (ok("\r\nMicrosoft Windows [Version 10.0.22631.4460]\r\n"), "windows"),  # cmd.exe
    (SimpleNamespace(returncode=0, stdout="\r\nMicrosoft Windows [Versión 10.0.19045.5011]\r\n",
                     stderr="'uname' no se reconoce como un comando interno o externo,\r\n"
                            "programa o archivo por lotes ejecutable.\r\n"), "windows"),
    (fail(1, "At line:1 char:10\r\n+ uname -s || ver\r\n+          ~~\r\nThe token '||' is not "
             "a valid statement separator in this version.\r\n    + CategoryInfo          : "
             "ParserError: (:) [], ParentContainsErrorRecordException\r\n    + "
             "FullyQualifiedErrorId : InvalidEndOfLine\r\n"), "windows"),   # PowerShell 5.1
    (fail(1, "uname: The term 'uname' is not recognized as a name of a cmdlet, function, "
             "script file, or executable program.\r\n"
             "+ FullyQualifiedErrorId : CommandNotFoundException\r\n"), "windows"),  # pwsh 7
    (fail(1, "#< CLIXML\r\n<Objs Version=\"1.1.0.1\"></Objs>"), "windows"),
    (ok("MINGW64_NT-10.0-22631\n"), "windows"),                 # Git for Windows uname
    (fail(255, "ssh: connect to host desk.lan port 22: No route to host"), ""),
    (fail(255, "Microsoft Windows"), ""),
    (ok("FreeBSD\n"), ""),
    (ok(""), ""),
    (subprocess.TimeoutExpired("ssh", 10), ""),
    (OSError("ssh not found"), ""),
])
def test_detect_os(result, expected):
    run = Run(result)
    assert power.detect_os(machine(user="me"), run=run) == expected
    assert len(run.calls) == 1
    command, kwargs = run.calls[0]
    assert command == power.ssh_argv(machine(user="me"), "uname -s || ver")
    assert kwargs["timeout"] == 10


def test_detect_os_rejects_injection_without_running():
    run = Run()
    assert power.detect_os(machine(host="-oProxyCommand=x"), run=run) == ""
    assert run.calls == []


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh", "fish"])
def test_os_probe_runs_in_local_posix_shells(shell):
    if not shutil.which(shell):
        pytest.skip(f"{shell} not installed")

    def run(argv, **kwargs):
        return subprocess.run([shell, "-c", argv[-1]], capture_output=True, text=True,
                              timeout=kwargs["timeout"])

    assert power.detect_os(machine(), run=run) == "linux"
