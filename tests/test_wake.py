import shlex
import socket
import subprocess
import types

import pytest

from flota_plugin import wake as w

MAC = "00:11:22:33:44:55"
MAC_BYTES = bytes([0x00, 0x11, 0x22, 0x33, 0x44, 0x55])
PACKET = b"\xff" * 6 + MAC_BYTES * 16


def machine(**over):
    base = dict(name="torre", host="10.0.0.5", port=22, wake="wol", mac=MAC,
                broadcast="10.0.0.255", via="pi@otherlan", wake_command="wake-torre",
                wake_timeout=60.0)
    return types.SimpleNamespace(**{**base, **over})


class FakeSocket:
    def __init__(self, *args, fail=False):
        self.args, self.fail = args, fail
        self.opts, self.sent, self.closed = [], [], False

    def setsockopt(self, *opt):
        self.opts.append(opt)

    def sendto(self, data, addr):
        if self.fail:
            raise OSError("network unreachable")
        self.sent.append((data, addr))

    def close(self):
        self.closed = True


# --- magic_packet ---------------------------------------------------------------------------

@pytest.mark.parametrize("mac", [MAC, "00-11-22-33-44-55", "001122334455"])
def test_magic_packet_is_the_exact_102_bytes(mac):
    packet = w.magic_packet(mac)
    assert len(packet) == 102
    assert packet == PACKET


@pytest.mark.parametrize("mac", [
    "", "00:11:22:33:44", "00:11:22:33:44:55:99", "00:11:22:33:44:zz", "00:11-22:33-44:55",
    "0011.2233.4455", "00:11:22:33:44:55; rm -rf /", " 001122334455", "0:011:22:33:44:55",
    None, 0x001122334455,
])
def test_magic_packet_rejects_anything_else(mac):
    with pytest.raises(ValueError):
        w.magic_packet(mac)


# --- send_wol -------------------------------------------------------------------------------

def test_send_wol_broadcasts_the_packet_over_udp_and_closes():
    made = []
    w.send_wol(MAC, "10.0.0.255", sock_factory=lambda *a: made.append(FakeSocket(*a)) or made[-1])
    (sock,) = made
    assert sock.args == (socket.AF_INET, socket.SOCK_DGRAM)
    assert sock.opts == [(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)]
    assert sock.sent == [(PACKET, ("10.0.0.255", 9))]
    assert sock.closed


def test_send_wol_defaults_to_the_limited_broadcast_and_honours_port():
    made = []
    w.send_wol(MAC, port=7, sock_factory=lambda *a: made.append(FakeSocket(*a)) or made[-1])
    assert made[0].sent == [(PACKET, ("255.255.255.255", 7))]


def test_send_wol_closes_the_socket_when_sending_fails():
    sock = FakeSocket(fail=True)
    with pytest.raises(OSError):
        w.send_wol(MAC, sock_factory=lambda *a: sock)
    assert sock.closed


def test_send_wol_opens_no_socket_for_a_bad_mac():
    with pytest.raises(ValueError):
        w.send_wol("nope", sock_factory=lambda *a: pytest.fail("socket opened"))


# --- is_up ----------------------------------------------------------------------------------

def test_is_up_true_when_the_connection_opens_and_closes_it():
    conn = FakeSocket()
    calls = []

    def connect(addr, timeout):
        calls.append((addr, timeout))
        return conn

    assert w.is_up("10.0.0.5", connect=connect) is True
    assert calls == [(("10.0.0.5", 22), 2.0)]
    assert conn.closed


@pytest.mark.parametrize("error", [
    ConnectionRefusedError(), socket.timeout(), TimeoutError(), socket.gaierror(-2, "no such host"),
])
def test_is_up_false_on_any_oserror(error):
    def connect(addr, timeout):
        assert (addr, timeout) == (("box", 2222), 0.5)
        raise error

    assert w.is_up("box", 2222, timeout=0.5, connect=connect) is False


# --- wake -----------------------------------------------------------------------------------

class FakeRun:
    def __init__(self, returncode=0, stderr="", error=None):
        self.returncode, self.stderr, self.error = returncode, stderr, error
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self.error:
            raise self.error
        return types.SimpleNamespace(returncode=self.returncode, stdout="", stderr=self.stderr)


def no_run(*a, **k):
    pytest.fail("subprocess run")


def no_send(*a, **k):
    pytest.fail("packet sent")


def test_wake_wol_sends_the_packet_three_times():
    sent = []
    w.wake(machine(wake="wol"), run=no_run, send=lambda *a: sent.append(a))
    assert sent == [(MAC, "10.0.0.255")] * 3


def test_wake_none_does_nothing():
    w.wake(machine(wake="none"), run=no_run, send=no_send)


def test_wake_command_runs_it_in_a_shell_capturing_output():
    run = FakeRun()
    w.wake(machine(wake="command", wake_command="curl -s http://plug/on | grep ok"),
           run=run, send=no_send)
    assert run.calls == [("curl -s http://plug/on | grep ok",
                          dict(shell=True, timeout=30, capture_output=True, text=True))]


def test_wake_wol_via_runs_a_remote_python_broadcast_over_ssh(monkeypatch):
    run = FakeRun()
    w.wake(machine(wake="wol_via", mac="00-11-22-33-44-55"), run=run, send=no_send)
    ((argv, kwargs),) = run.calls
    assert argv[:6] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "pi@otherlan"]
    assert len(argv) == 7
    assert kwargs == dict(timeout=20, capture_output=True, text=True)

    # ssh hands argv[6] to the remote shell: it must parse there as exactly python3 -c <script>.
    prog, flag, script = shlex.split(argv[6])
    assert (prog, flag) == ("python3", "-c")

    # Run the script as the remote host would, against a fake socket.
    made = []
    monkeypatch.setattr(socket, "socket", lambda *a: made.append(FakeSocket(*a)) or made[-1])
    exec(script, {})
    (sock,) = made
    assert sock.args == (socket.AF_INET, socket.SOCK_DGRAM)
    assert sock.opts == [(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)]
    assert sock.sent == [(PACKET, ("10.0.0.255", 9))]


@pytest.mark.parametrize("field,value", [
    ("mac", "00:11:22:33:44:55'; touch /tmp/pwned; '"),
    ("mac", "$(touch /tmp/pwned)"),
    ("broadcast", "10.0.0.255'); import os; os.system('touch /tmp/pwned') #"),
    ("broadcast", "10.0.0.255; touch /tmp/pwned"),
    ("broadcast", "$(touch /tmp/pwned)"),
    ("broadcast", "otherlan.local"),
    ("via", "-oProxyCommand=touch /tmp/pwned"),
    ("via", "pi@otherlan; touch /tmp/pwned"),
    ("via", "pi@otherlan touch"),
    ("via", "$(touch /tmp/pwned)"),
    ("via", ""),
])
def test_wake_wol_via_refuses_hostile_values_before_running_anything(field, value):
    with pytest.raises(w.WakeError, match="torre"):
        w.wake(machine(wake="wol_via", **{field: value}), run=no_run, send=no_send)


def test_wake_wol_via_never_uses_a_local_shell():
    run = FakeRun()
    w.wake(machine(wake="wol_via"), run=run, send=no_send)
    assert isinstance(run.calls[0][0], list) and "shell" not in run.calls[0][1]


@pytest.mark.parametrize("mode", ["command", "wol_via"])
def test_wake_nonzero_exit_names_the_machine_and_the_last_stderr_line(mode):
    run = FakeRun(returncode=255, stderr="warning: noise\nssh: connect to host otherlan: No route\n")
    with pytest.raises(w.WakeError) as err:
        w.wake(machine(wake=mode), run=run, send=no_send)
    assert "torre" in str(err.value)
    assert "255" in str(err.value)
    assert "ssh: connect to host otherlan: No route" in str(err.value)
    assert "noise" not in str(err.value)


def test_wake_nonzero_exit_without_stderr_still_reports():
    with pytest.raises(w.WakeError, match="torre.*3"):
        w.wake(machine(wake="command"), run=FakeRun(returncode=3, stderr=None), send=no_send)


@pytest.mark.parametrize("mode", ["command", "wol_via"])
def test_wake_timeout_becomes_wake_error(mode):
    run = FakeRun(error=subprocess.TimeoutExpired("cmd", 30))
    with pytest.raises(w.WakeError, match="torre.*timed out"):
        w.wake(machine(wake=mode), run=run, send=no_send)


def test_wake_oserror_from_run_becomes_wake_error():
    run = FakeRun(error=FileNotFoundError("ssh not found"))
    with pytest.raises(w.WakeError, match="torre.*ssh not found"):
        w.wake(machine(wake="wol_via"), run=run, send=no_send)


def test_wake_oserror_from_send_becomes_wake_error():
    def send(*a):
        raise OSError("Network is unreachable")

    with pytest.raises(w.WakeError, match="torre.*Network is unreachable"):
        w.wake(machine(wake="wol"), run=no_run, send=send)


def test_wake_bad_mac_becomes_wake_error():
    with pytest.raises(w.WakeError, match="torre.*MAC"):
        w.wake(machine(wake="wol", mac="nope"), run=no_run, send=w.send_wol)


def test_wake_unknown_method_is_an_error():
    with pytest.raises(w.WakeError, match="torre.*telepathy"):
        w.wake(machine(wake="telepathy"), run=no_run, send=no_send)


# --- ensure_ready ---------------------------------------------------------------------------

class World:
    """Fake time plus a machine that starts answering `boots_after` seconds after t=0."""

    def __init__(self, boots_after=None):
        self.now = 1000.0  # monotonic clocks do not start at zero
        self.boots_after = boots_after
        self.probes, self.wakes, self.sleeps = [], [], []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def probe(self, host, port):
        self.probes.append((host, port))
        return self.boots_after is not None and self.now - 1000.0 >= self.boots_after

    def waker(self, m):
        self.wakes.append(self.now - 1000.0)

    def ensure(self, m):
        return w.ensure_ready(m, probe=self.probe, waker=self.waker, sleep=self.sleep,
                              clock=self.clock)


def test_ensure_ready_returns_zero_without_waking_when_already_up():
    world = World(boots_after=0)
    assert world.ensure(machine(port=2222)) == 0.0
    assert world.probes == [("10.0.0.5", 2222)]
    assert world.wakes == [] and world.sleeps == []


def test_ensure_ready_already_up_needs_no_wake_method():
    world = World(boots_after=0)
    assert world.ensure(machine(wake="none")) == 0.0


def test_ensure_ready_fails_at_once_when_down_with_no_wake_method():
    world = World()
    with pytest.raises(w.WakeError, match="torre.*off.*no wake method"):
        world.ensure(machine(wake="none"))
    assert world.wakes == [] and world.sleeps == []


def test_ensure_ready_wakes_then_polls_every_two_seconds_and_returns_the_wait():
    world = World(boots_after=5)
    assert world.ensure(machine()) == 6.0
    assert world.wakes == [0.0]
    assert world.sleeps == [2, 2, 2]
    assert len(world.probes) == 4


def test_ensure_ready_resends_the_wake_every_ten_seconds():
    world = World(boots_after=35)
    assert world.ensure(machine()) == 36.0
    assert world.wakes == [0.0, 10.0, 20.0, 30.0]


def test_ensure_ready_times_out_naming_machine_address_and_timeout():
    world = World()
    with pytest.raises(w.WakeError) as err:
        world.ensure(machine(port=2222, wake_timeout=25.0))
    for part in ("torre", "10.0.0.5:2222", "25"):
        assert part in str(err.value)
    assert world.now - 1000.0 == 26.0  # first poll at or past the deadline
    assert world.wakes == [0.0, 10.0, 20.0]


def test_ensure_ready_accepts_a_machine_that_answers_on_the_last_poll():
    world = World(boots_after=10)
    assert world.ensure(machine(wake_timeout=10.0)) == 10.0
    assert world.wakes == [0.0]  # no pointless re-send once it is up


def test_ensure_ready_propagates_a_waker_failure():
    world = World()

    def waker(m):
        raise w.WakeError("torre: wake failed: no route")

    with pytest.raises(w.WakeError, match="no route"):
        w.ensure_ready(machine(), probe=world.probe, waker=waker, sleep=world.sleep,
                       clock=world.clock)
    assert world.sleeps == []


def test_ensure_ready_defaults_are_the_real_probe_and_waker():
    defaults = w.ensure_ready.__kwdefaults__
    assert defaults["probe"] is w.is_up and defaults["waker"] is w.wake


# --- ensure_ready for machines that only wake on their own timer -------------------------------

def _timer_ensure(world, m, armed, wall=5000.0):
    return w.ensure_ready(m, probe=world.probe, waker=world.waker, sleep=world.sleep,
                          clock=world.clock, timer_at=lambda name: armed, wall=lambda: wall)


def test_timer_machine_already_up():
    world = World(boots_after=0)
    assert _timer_ensure(world, machine(wake="timer", name="studio"), None) == 0.0


def test_timer_machine_without_a_timer_fails_at_once_and_is_never_woken():
    world = World(boots_after=5)
    with pytest.raises(w.WakeError, match="studio sleeps until you turn it on"):
        _timer_ensure(world, machine(wake="timer", name="studio"), None)
    assert world.wakes == [] and world.sleeps == []


def test_timer_machine_far_from_its_timer_names_the_time():
    import time as _time
    armed = _time.mktime((2027, 1, 15, 2, 0, 0, 0, 0, -1))
    world = World(boots_after=5)
    with pytest.raises(w.WakeError, match="studio sleeps until its wake timer at 02:00 "
                                          "or until you turn it on"):
        _timer_ensure(world, machine(wake="timer", name="studio"), armed, wall=armed - 3600)
    assert world.wakes == [] and world.sleeps == []


def test_timer_machine_waits_when_its_timer_is_due_within_wake_timeout():
    world = World(boots_after=30)
    m = machine(wake="timer", name="studio", wake_timeout=90.0)
    assert _timer_ensure(world, m, 5000.0 + 20, wall=5000.0) == 30.0
    assert world.wakes == []  # it cannot be woken, only waited for


def test_timer_machine_that_does_not_come_up_in_time():
    world = World()
    m = machine(wake="timer", name="studio", wake_timeout=10.0)
    with pytest.raises(w.WakeError, match="wake timer"):
        _timer_ensure(world, m, 5000.0 - 5, wall=5000.0)  # just fired, still resuming
    assert world.wakes == []
