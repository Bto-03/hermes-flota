"""detect: one SSH call per probe, parsed from realistic tool output; never raises when a machine is away."""
import base64
import ipaddress
import subprocess
from types import SimpleNamespace

from flota_plugin import detect
from flota_plugin.machines import Machine

M = Machine(name="escritorio", profile="flota-escritorio", host="192.0.2.28", user="alice")

LINUX_OUT = """\
if=enp8s0
mac=00:11:22:33:44:55
2: enp8s0    inet 198.51.100.10/24 brd 198.51.100.255 scope global dynamic noprefixroute enp8s0\\       valid_lft 85234sec preferred_lft 85234sec
\tSupports Wake-on: pumbg
\tWake-on: g
conn=Wired connection 1
nmcli=default
"""

MAC_OUT = """\
if=en0
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\toptions=6460<TSO4,TSO6,CHANNEL_IO,PARTIAL_CSUM,ZEROINVERT_CSUM>
\tether 00:11:22:aa:bb:cc
\tinet6 fe80::1c8a:2b3f:fe4d:9a01%en0 prefixlen 64 secured scopeid 0xb
\tinet 192.0.2.20 netmask 0xffffff00 broadcast 192.0.2.255
\tnd6 options=201<PERFORMNUD,DAD>
\tmedia: autoselect (1000baseT <full-duplex>)
\tstatus: active
 womp                 1
"""

WINDOWS_OUT = "if=Ethernet\r\nmac=00-11-22-33-44-56\r\nip=198.51.100.12/24\r\nwol=Disabled\r\n"


def fake(stdout="", returncode=0, stderr="", raises=None):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if raises:
            raise raises
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    run.calls = calls
    return run


def test_linux_default_route_interface_mac_broadcast_and_wol():
    run = fake(LINUX_OUT)
    net = detect.network(M, run=run, os="linux")
    assert net == {"interface": "enp8s0", "mac": "00:11:22:33:44:55", "broadcast": "198.51.100.255",
                   "wol": "enabled", "connection": "Wired connection 1", "reason": ""}
    [(argv, kwargs)] = run.calls
    assert argv[:2] == ["ssh", "-o"] and "alice@192.0.2.28" in argv
    assert kwargs["timeout"] == 10
    script = argv[-1]
    assert script.startswith("sh -c ") and "ip -4 route show default" in script and "tailscale" in script


def test_linux_without_root_falls_back_to_networkmanager():
    out = LINUX_OUT.replace("\tSupports Wake-on: pumbg\n\tWake-on: g\n", "").replace("nmcli=default", "nmcli=magic")
    assert detect.network(M, run=fake(out), os="linux")["wol"] == "enabled"


def test_linux_wol_disabled_and_unknown():
    assert detect.network(M, run=fake(LINUX_OUT.replace("Wake-on: g", "Wake-on: d")), os="linux")["wol"] == "disabled"
    bare = "if=enp8s0\nmac=00:11:22:33:44:55\n2: enp8s0    inet 198.51.100.10/24 scope global enp8s0\n"
    net = detect.network(M, run=fake(bare), os="linux")
    assert net["wol"] == "unknown" and net["broadcast"] == "198.51.100.255"  # computed from the prefix


def test_unknown_os_uses_the_linux_script():
    assert detect.network(M, run=fake(LINUX_OUT))["mac"] == "00:11:22:33:44:55"


def test_macos_ifconfig_and_pmset():
    run = fake(MAC_OUT)
    net = detect.network(M, run=run, os="macos")
    assert (net["interface"], net["mac"], net["broadcast"], net["wol"]) == (
        "en0", "00:11:22:aa:bb:cc", "192.0.2.255", "enabled")
    assert "route -n get default" in run.calls[0][0][-1] and "pmset -g" in run.calls[0][0][-1]


def test_macos_broadcast_from_netmask_and_womp_off():
    out = MAC_OUT.replace(" broadcast 192.0.2.255", "").replace("womp                 1", "womp                 0")
    net = detect.network(M, run=fake(out), os="macos")
    assert net["broadcast"] == "192.0.2.255" and net["wol"] == "disabled"


def test_windows_powershell_adapter_with_the_gateway():
    run = fake(WINDOWS_OUT)
    net = detect.network(M, run=run, os="windows")
    assert (net["interface"], net["mac"], net["broadcast"], net["wol"]) == (
        "Ethernet", "00:11:22:33:44:56", "198.51.100.255", "disabled")
    remote = run.calls[0][0][-1]
    assert remote.startswith("powershell -NoProfile -EncodedCommand ")
    script = base64.b64decode(remote.split()[-1]).decode("utf-16-le")
    assert "IPv4DefaultGateway" in script and "WakeOnMagicPacket" in script


def test_unreachable_returns_empty_values_and_a_reason():
    for run in (fake(returncode=255, stderr="ssh: connect to host 192.0.2.28 port 22: No route to host\n"),
                fake(raises=subprocess.TimeoutExpired("ssh", 10)), fake(raises=FileNotFoundError("ssh"))):
        net = detect.network(M, run=run, os="linux")
        assert (net["mac"], net["broadcast"], net["interface"], net["wol"]) == ("", "", "", "unknown")
        assert net["reason"].startswith("unreachable")


def test_no_default_route():
    net = detect.network(M, run=fake("if=\n"), os="linux")
    assert net["mac"] == "" and "default route" in net["reason"]


def test_enable_wol_hints_per_os():
    linux = "\n".join(detect.enable_wol_hint("linux", {"interface": "enp8s0", "connection": "Wired connection 1"}))
    assert "as root: ethtool -s enp8s0 wol g" in linux
    assert "nmcli connection modify 'Wired connection 1' 802-3-ethernet.wake-on-lan magic" in linux
    assert "BIOS" in linux
    assert "as root: pmset -a womp 1" in "\n".join(detect.enable_wol_hint("macos", {"interface": "en0"}))
    windows = "\n".join(detect.enable_wol_hint("windows", {"interface": "Ethernet"}))
    assert "Wake on Magic Packet" in windows and 'powercfg /deviceenablewake "Ethernet"' in windows


def test_local_networks_and_same_lan():
    out = ("1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever\n"
           "2: wlan0    inet 198.51.100.30/24 brd 198.51.100.255 scope global dynamic wlan0\\       valid_lft 1sec\n"
           "5: tailscale0    inet 203.0.113.3/32 scope global tailscale0\\       valid_lft forever\n")
    nets = detect.local_networks(run=fake(out))
    assert ipaddress.ip_interface("198.51.100.30/24") in nets and len(nets) == 2  # loopback skipped
    assert str(detect.same_lan("198.51.100.255", nets)) == "198.51.100.0/24"
    assert detect.same_lan("10.0.0.255", nets) is None
    assert detect.local_networks(run=fake(raises=FileNotFoundError("ip"))) == []


def test_local_network_runs_the_script_locally_and_reports_the_lan_ip():
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        out = ("if=enp3s0\nmac=AA:BB:CC:DD:EE:FF\n2: enp3s0    inet 192.0.2.20/24 brd 192.0.2.255 scope global\n"
               "\tWake-on: g\nconn=Wired\n")
        return subprocess.CompletedProcess(argv, 0, out, "")

    net = detect.local_network("linux", run=run)
    assert seen[0][:2] == ["sh", "-c"] and "ssh" not in seen[0]
    assert net["ip"] == "192.0.2.20" and net["mac"] == "aa:bb:cc:dd:ee:ff"
    assert net["broadcast"] == "192.0.2.255" and net["wol"] == "enabled"
    assert detect.local_network("windows", run=run) is not None and seen[1][0] == "powershell"


def test_local_network_without_a_shell_is_empty():
    def run(argv, **kw):
        raise OSError("no sh")
    assert detect.local_network("linux", run=run)["ip"] == ""


def test_tailscale_ip():
    def run(argv, **kw):
        if argv[0] != "tailscale":
            raise OSError
        return subprocess.CompletedProcess(argv, 0, "203.0.113.2\n", "")
    assert detect.tailscale_ip(run=run) == "203.0.113.2"

    def missing(argv, **kw):
        raise FileNotFoundError(argv[0])
    assert detect.tailscale_ip(run=missing) is None and detect.tailscale_installed(run=missing) is False

    def down(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", "Tailscale is stopped.")
    assert detect.tailscale_ip(run=down) is None and detect.tailscale_installed(run=down) is True


def test_local_os():
    assert detect.local_os(lambda: "Darwin") == "macos"
    assert detect.local_os(lambda: "Windows") == "windows"
    assert detect.local_os(lambda: "Linux") == "linux"
