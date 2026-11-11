"""Read a machine's LAN details over SSH so ``setup`` and ``doctor`` need no hand-typed MAC.

Each probe is one SSH call with a 10 s timeout and never raises: an away machine gives empty
values and a ``reason``.

Wake-on-LAN needs the physical LAN interface, not the one SSH arrives on: with Tailscale the SSH
route is tailscale0 / utun / a 100.x address, which has no usable MAC. So every script picks the
interface carrying the default route to the internet and skips VPN interfaces.
"""
import base64
import ipaddress
import platform
import re
import shlex
import subprocess

from .power import ssh_argv

_TIMEOUT = 10
_KV = re.compile(r"^(if|mac|conn|nmcli|ip|wol)=(.*)$")
_INET = re.compile(r"\binet (\d+\.\d+\.\d+\.\d+/\d+)(?: brd (\d+\.\d+\.\d+\.\d+))?")
_MAC = re.compile(r"[0-9a-f]{2}(?:[:-][0-9a-f]{2}){5}", re.IGNORECASE)

# The remote login shell may be fish (it mangles backslashes inside single quotes), so the
# POSIX scripts below contain no backslash and no single quote and run under `sh -c`.
_LINUX = (
    'i=; set -- $(ip -4 route show default 2>/dev/null); '
    'while [ $# -gt 1 ]; do if [ "$1" = dev ]; then case "$2" in '
    'tailscale*|wg*|tun*|zt*) ;; *) i=$2; break ;; esac; fi; shift; done; '
    'echo "if=$i"; [ -n "$i" ] || exit 0; '
    'echo "mac=$(cat /sys/class/net/$i/address 2>/dev/null)"; '
    'ip -o -4 addr show dev "$i" 2>/dev/null; '
    'ethtool "$i" 2>/dev/null | grep Wake-on; '
    'c=$(nmcli -g GENERAL.CONNECTION device show "$i" 2>/dev/null); echo "conn=$c"; '
    '[ -n "$c" ] && echo "nmcli=$(nmcli -g 802-3-ethernet.wake-on-lan connection show "$c" 2>/dev/null)"; '
    'true')
_MACOS = (
    'set -- $(route -n get default 2>/dev/null | grep interface:); i=$2; '
    'echo "if=$i"; [ -n "$i" ] || exit 0; '
    'ifconfig "$i" 2>/dev/null; pmset -g 2>/dev/null | grep womp; true')
_WINDOWS = (
    "$c = Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' "
    "-and $_.InterfaceDescription -notmatch 'Tailscale|WireGuard' } | Select-Object -First 1\n"
    "if (-not $c) { 'if='; exit }\n"
    "$ip = $c.IPv4Address | Select-Object -First 1\n"
    "$w = Get-NetAdapterPowerManagement -Name $c.InterfaceAlias -ErrorAction SilentlyContinue\n"
    "'if=' + $c.InterfaceAlias\n'mac=' + $c.NetAdapter.MacAddress\n"
    "'ip=' + $ip.IPAddress + '/' + $ip.PrefixLength\n'wol=' + $w.WakeOnMagicPacket\n")


def _remote(os: str) -> str:
    if os == "windows":
        encoded = base64.b64encode(_WINDOWS.encode("utf-16-le")).decode()
        return f"powershell -NoProfile -EncodedCommand {encoded}"
    return "sh -c " + shlex.quote(_MACOS if os == "macos" else _LINUX)


def _broadcast(cidr: str) -> str:
    try:
        return str(ipaddress.ip_interface(cidr).network.broadcast_address)
    except ValueError:
        return ""


def _parse(os: str, out: str) -> dict:
    kv, wol, broadcast = {}, "unknown", ""
    for line in out.splitlines():
        line = line.rstrip("\r")
        match = _KV.match(line)
        if match:
            kv.setdefault(match[1], match[2].strip())
        inet = _INET.search(line)
        if inet and not broadcast:
            broadcast = inet[2] or _broadcast(inet[1])
        if os == "macos":
            netmask = re.search(r"^\s*inet (\S+) netmask (0x[0-9a-f]+)(?: broadcast (\S+))?", line)
            if netmask:
                bits = bin(int(netmask[2], 16)).count("1")
                broadcast = netmask[3] or _broadcast(f"{netmask[1]}/{bits}")
            ether = re.match(r"^\s*ether (\S+)", line)
            if ether:
                kv["mac"] = ether[1]
            womp = re.match(r"^\s*womp\s+(\d)", line)
            if womp:
                wol = "enabled" if womp[1] == "1" else "disabled"
        ethtool = re.match(r"^\s*Wake-on:\s*(\w+)", line)  # not "Supports Wake-on:"
        if ethtool:
            wol = "enabled" if "g" in ethtool[1] else "disabled"
    if os == "linux" and wol == "unknown" and "magic" in kv.get("nmcli", ""):
        wol = "enabled"  # ethtool needs root to read Wake-on; NetworkManager's setting does not
    if os == "windows":
        broadcast = _broadcast(kv.get("ip", ""))
        wol = {"Enabled": "enabled", "Disabled": "disabled"}.get(kv.get("wol", ""), "unknown")
    mac = kv.get("mac", "")
    mac = mac.lower().replace("-", ":") if _MAC.fullmatch(mac) else ""
    interface = kv.get("if", "")
    reason = ("no default route found" if not interface
              else f"no MAC address on {interface}" if not mac else "")
    return {"interface": interface, "mac": mac, "broadcast": broadcast if interface else "",
            "wol": wol if interface else "unknown", "connection": kv.get("conn", ""), "reason": reason}


def network(machine, run=subprocess.run, os: str | None = None) -> dict:
    """LAN interface, MAC, broadcast and Wake-on-LAN state ('enabled'|'disabled'|'unknown')."""
    os = os if os is not None else getattr(machine, "os", "") or ""
    os = os if os in ("macos", "windows") else "linux"
    empty = {"interface": "", "mac": "", "broadcast": "", "wol": "unknown", "connection": ""}
    try:
        done = run(ssh_argv(machine, _remote(os)), timeout=_TIMEOUT, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return {**empty, "reason": f"unreachable: ssh timed out after {_TIMEOUT}s"}
    except (OSError, ValueError) as exc:
        return {**empty, "reason": f"unreachable: {exc}"}
    if done.returncode == 255:
        lines = (done.stderr or "").strip().splitlines()
        return {**empty, "reason": "unreachable" + (f": {lines[-1]}" if lines else "")}
    return _parse(os, done.stdout or "")


def enable_wol_hint(os: str, net: dict) -> list[str]:
    """Commands that turn Wake-on-LAN on for this OS."""
    interface = net.get("interface") or "<interface>"
    if os == "macos":
        lines = ["as root: pmset -a womp 1"]
    elif os == "windows":
        lines = [f"Device Manager > {interface} > Power Management / Advanced: enable 'Wake on Magic Packet'",
                 f'powercfg /deviceenablewake "{interface}"   (admin prompt)']
    else:
        connection = shlex.quote(net.get("connection") or "<connection>")
        lines = [f"as root: ethtool -s {interface} wol g",
                 f"as root: nmcli connection modify {connection} 802-3-ethernet.wake-on-lan magic   (keeps it after reboot)"]
    return lines + ["the BIOS/UEFI must also allow waking from the network (Wake on LAN / Power on by PCI-E)"]


def local_networks(run=subprocess.run) -> list:
    """IPv4 interfaces of this (the Hermes) host, loopback left out. [] when `ip` is unavailable."""
    try:
        done = run(["ip", "-o", "-4", "addr", "show"], timeout=_TIMEOUT, capture_output=True, text=True)
    except (OSError, subprocess.TimeoutExpired):
        return []
    nets = [ipaddress.ip_interface(m[1]) for m in _INET.finditer(done.stdout or "")]
    return [n for n in nets if not n.is_loopback]


def same_lan(broadcast: str, nets) -> "ipaddress.IPv4Network | None":
    """The local network whose broadcast address is ``broadcast``, if this host is on it."""
    for net in nets:
        if str(net.network.broadcast_address) == broadcast:
            return net.network
    return None


# --- this host (a ship joining a fleet reads its own details, no SSH) ------------------------------

_TAILNET = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_BINARIES = ("tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale")


def local_os(system=platform.system) -> str:
    name = (system() or "").lower()
    return "macos" if name == "darwin" else "windows" if name.startswith("win") else "linux"


def _local_argv(os: str) -> list[str]:
    if os == "windows":
        encoded = base64.b64encode(_WINDOWS.encode("utf-16-le")).decode()
        return ["powershell", "-NoProfile", "-EncodedCommand", encoded]
    return ["sh", "-c", _MACOS if os == "macos" else _LINUX]


def _first_ip(os: str, out: str) -> str:
    for line in out.splitlines():
        line = line.rstrip("\r")
        found = (re.match(r"^ip=(\d+\.\d+\.\d+\.\d+)/", line) if os == "windows"
                 else re.search(r"^\s*inet (\d+\.\d+\.\d+\.\d+) netmask", line) if os == "macos"
                 else _INET.search(line))
        if found:
            return found[1].split("/")[0]
    return ""


def local_network(os: str | None = None, run=subprocess.run) -> dict:
    """``network()`` for this host: LAN interface, MAC, broadcast, Wake-on-LAN, plus its LAN ``ip``."""
    os = os or local_os()
    empty = {"interface": "", "mac": "", "broadcast": "", "wol": "unknown", "connection": "", "ip": ""}
    try:
        done = run(_local_argv(os), timeout=_TIMEOUT, capture_output=True, text=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**empty, "reason": f"could not read the network: {exc}"}
    out = done.stdout or ""
    return {**_parse(os, out), "ip": _first_ip(os, out)}


def tailscale_ip(run=subprocess.run) -> str | None:
    """This host's Tailscale IPv4 (100.64.0.0/10), or None when Tailscale is missing or down."""
    for binary in _TAILSCALE_BINARIES:
        try:
            done = run([binary, "ip", "-4"], timeout=5, capture_output=True, text=True)
        except (OSError, subprocess.TimeoutExpired):
            continue
        for word in (done.stdout or "").split():
            try:
                if ipaddress.ip_address(word) in _TAILNET:
                    return word
            except ValueError:
                continue
        return None
    return None


def tailscale_installed(run=subprocess.run) -> bool:
    for binary in _TAILSCALE_BINARIES:
        try:
            run([binary, "version"], timeout=5, capture_output=True, text=True)
        except OSError:
            continue
        except subprocess.TimeoutExpired:
            pass
        return True
    return False
