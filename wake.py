"""Wake a fleet machine and wait until its SSH port answers."""
import ipaddress
import re
import shlex
import socket
import subprocess
import time

# One separator style throughout (":" or "-") or none at all.
_MAC = re.compile(r"[0-9a-f]{2}([:-]?)(?:[0-9a-f]{2}\1){4}[0-9a-f]{2}", re.IGNORECASE)
_SSH_TARGET = re.compile(r"[A-Za-z0-9_.@:\[\]][A-Za-z0-9_.@:\[\]-]*")


class WakeError(Exception):
    pass


def magic_packet(mac: str) -> bytes:
    if not isinstance(mac, str) or not _MAC.fullmatch(mac):
        raise ValueError(f"invalid MAC address: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(re.sub("[:-]", "", mac)) * 16


def send_wol(mac, broadcast="255.255.255.255", port=9, sock_factory=socket.socket) -> None:
    packet = magic_packet(mac)
    sock = sock_factory(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast, port))
    finally:
        sock.close()


def is_up(host, port=22, timeout=2.0, connect=socket.create_connection) -> bool:
    try:
        connect((host, port), timeout).close()
    except OSError:
        return False
    return True


def _remote_wol(mac: str, broadcast: str) -> str:
    """Shell command that broadcasts the magic packet from another host. Only validated,
    re-serialised values are interpolated, so nothing from the config reaches the remote shell."""
    mac_hex = magic_packet(mac)[6:12].hex()
    address = str(ipaddress.ip_address(broadcast))
    script = (
        "import socket;"
        "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);"
        "s.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1);"
        f"s.sendto(b'\\xff'*6+bytes.fromhex('{mac_hex}')*16,('{address}',9))"
    )
    return "python3 -c " + shlex.quote(script)


def _run(run, command, **kwargs) -> None:
    done = run(command, capture_output=True, text=True, **kwargs)
    if done.returncode != 0:
        lines = (done.stderr or "").strip().splitlines()
        raise WakeError(f"exit code {done.returncode}" + (f": {lines[-1]}" if lines else ""))


def wake(machine, run=subprocess.run, send=send_wol) -> None:
    try:
        if machine.wake == "wol":
            for _ in range(3):  # UDP is lossy and a NIC only needs to hear one
                send(machine.mac, machine.broadcast)
        elif machine.wake == "wol_via":
            # A leading "-" would be read by ssh as an option (e.g. -oProxyCommand=...).
            if not _SSH_TARGET.fullmatch(machine.via):
                raise ValueError(f"invalid ssh target: {machine.via!r}")
            remote = _remote_wol(machine.mac, machine.broadcast)
            _run(run, ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", machine.via,
                       remote], timeout=20)
        elif machine.wake == "command":
            _run(run, machine.wake_command, shell=True, timeout=30)
        elif machine.wake not in ("none", "timer"):
            raise ValueError(f"unknown wake method: {machine.wake!r}")
    except subprocess.TimeoutExpired as exc:
        raise WakeError(f"{machine.name}: wake timed out after {exc.timeout:g}s") from exc
    except (WakeError, OSError, ValueError) as exc:
        raise WakeError(f"{machine.name}: wake failed: {exc}") from exc


def _armed_at(name):
    from .waketimer import armed_at
    return armed_at(name)


def _wait_for_timer(machine, probe, sleep, clock, timer_at, wall) -> float:
    """A 'timer' machine cannot be woken, only waited for, and only when its own alarm is due
    within wake_timeout (or just fired and it is still resuming); otherwise fail at once."""
    armed = timer_at(machine.name)
    if armed is None:
        raise WakeError(f"{machine.name} sleeps until you turn it on (no wake timer is set)")
    from .waketimer import format_when
    asleep = (f"{machine.name} sleeps until its wake timer at {format_when(armed, wall())} "
              "or until you turn it on")
    if not -machine.wake_timeout <= armed - wall() <= machine.wake_timeout:
        raise WakeError(asleep)
    start = clock()
    while True:
        sleep(2)
        if probe(machine.host, machine.port):
            return clock() - start
        if clock() - start >= machine.wake_timeout:
            raise WakeError(f"{asleep}; it did not answer within {machine.wake_timeout:g}s")


def ensure_ready(machine, *, probe=is_up, waker=wake, sleep=time.sleep,
                 clock=time.monotonic, timer_at=_armed_at, wall=time.time) -> float:
    """Make sure the machine answers on host:port; return the seconds spent waiting."""
    if probe(machine.host, machine.port):
        return 0.0
    if machine.wake == "none":
        raise WakeError(f"{machine.name} is off and has no wake method")
    if machine.wake == "timer":
        return _wait_for_timer(machine, probe, sleep, clock, timer_at, wall)
    start = last_wake = clock()
    waker(machine)
    while True:
        sleep(2)
        if probe(machine.host, machine.port):
            return clock() - start
        now = clock()
        if now - start >= machine.wake_timeout:
            raise WakeError(f"{machine.name} ({machine.host}:{machine.port}) did not come up "
                            f"within {machine.wake_timeout:g}s of being woken")
        if now - last_wake >= 10:  # the first packet may have been lost or ignored mid-shutdown
            waker(machine)
            last_wake = now
