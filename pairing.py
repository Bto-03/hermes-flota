"""Fleet pairing: a machine running Flota joins another one's fleet with two copy-pastes.

1. The flagship issues an invite (``FLOTA1-INVITE-...``): its name, login user, the SSH public key
   Flota reaches machines with, a nonce and an expiry (30 min). Issued nonces are kept in
   ``<hermes home>/flota/invites.json``.
2. The ship, after its user confirms, appends that public key to its own ``~/.ssh/authorized_keys``
   (a plain line with the comment ``flota-flagship:<name>``: subagents need a normal shell, so no
   ``restrict`` options) and answers with a ship code (``FLOTA1-SHIP-...``): the nonce, a suggested
   name, every address it can be reached on (Tailscale first, then LAN), user, MAC, broadcast,
   Wake-on-LAN state, OS and the names of its own bots.
3. The flagship checks the nonce is one it issued and has not expired, tries each address (TCP 22,
   then ``ssh -o BatchMode=yes true`` with its key) and adds the first one that works. A nonce is
   spent once the ship is saved; a failed attempt leaves it valid so the same code can be retried.

No private key, password or shell text ever travels in a code; every field is validated.
"""
import base64
import binascii
import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

INVITE_PREFIX = "FLOTA1-INVITE-"
SHIP_PREFIX = "FLOTA1-SHIP-"
MAX_CODE = 8192
INVITE_TTL = 30 * 60
_PUBKEY = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/=]+( .*)?$")
_FLAGSHIP = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,57}$")  # flota-<name> must be a profile id
_HOST = re.compile(r"^[A-Za-z0-9_.:%\[\]-]{1,253}$")
_USER = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.@-]{0,63}$")
_MAC = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
COMMENT = "flota-flagship:"


class PairingError(ValueError):
    """A code or key that must be refused; the message is shown to the user."""


# --- codes -----------------------------------------------------------------------------------------

def encode(prefix: str, data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
    return prefix + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(prefix: str, code) -> dict:
    if not isinstance(code, str):
        raise PairingError("the code must be text")
    code = "".join(code.split())  # pasted codes may wrap
    if len(code) > MAX_CODE:
        raise PairingError(f"the code is longer than {MAX_CODE} characters")
    if not code.startswith(prefix):
        other = SHIP_PREFIX if prefix == INVITE_PREFIX else INVITE_PREFIX
        hint = (" (that is a ship code: paste it on the flagship)" if prefix == INVITE_PREFIX
                else " (that is an invite: paste it on the machine that joins)") if code.startswith(other) else ""
        raise PairingError(f"not a Flota code: it must start with {prefix}{hint}")
    body = code[len(prefix):]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise PairingError("the code is damaged (copy all of it)") from exc
    if not isinstance(data, dict) or data.get("v") != 1:
        raise PairingError("unsupported code version; update Flota on both machines")
    return data


def _text(data: dict, key: str, pattern: re.Pattern, what: str, required: bool = True) -> str:
    value = data.get(key)
    if value in (None, "") and not required:
        return ""
    if not isinstance(value, str) or value.startswith("-") or not pattern.match(value):
        raise PairingError(f"invalid {what} in the code")
    return value


def check_pubkey(key) -> str:
    if not isinstance(key, str):
        raise PairingError("invalid public key")
    key = key.strip()
    if "\n" in key or "\r" in key or "PRIVATE KEY" in key or not _PUBKEY.match(key) or len(key) > 4096:
        raise PairingError("invalid public key (one ssh-ed25519 / ssh-rsa / ecdsa-sha2-nistp256 line)")
    return key


def fingerprint(pubkey: str) -> str:
    """``SHA256:...`` as ``ssh-keygen -lf`` prints it."""
    blob = base64.b64decode(pubkey.split()[1] + "==")
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


# --- flagship: key and invites -----------------------------------------------------------------------

def flota_key(configured: str | None = None, home: Path | None = None, run=subprocess.run) -> tuple[str, str]:
    """``(public key line, private key path)`` Flota reaches machines with: the configured key,
    else ``~/.ssh/id_ed25519``, else a new ``~/.ssh/flota_ed25519`` (made with ssh-keygen)."""
    ssh = Path(home or Path.home()) / ".ssh"
    candidates = [Path(os.path.expanduser(configured))] if configured else []
    candidates += [ssh / "id_ed25519", ssh / "flota_ed25519"]
    for private in candidates:
        public = Path(f"{private}.pub")
        if public.is_file():
            return check_pubkey(public.read_text().strip()), str(private)
    private = ssh / "flota_ed25519"
    ssh.mkdir(mode=0o700, parents=True, exist_ok=True)
    done = run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "flota", "-f", str(private)],
               timeout=30, capture_output=True, text=True)
    public = Path(f"{private}.pub")
    if done.returncode != 0 or not public.is_file():
        raise PairingError(f"ssh-keygen failed: {(done.stderr or '').strip() or done.returncode}")
    return check_pubkey(public.read_text().strip()), str(private)


def _load_invites(store: Path) -> dict:
    try:
        data = json.loads(Path(store).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_invites(store: Path, data: dict) -> None:
    store = Path(store)
    store.parent.mkdir(parents=True, exist_ok=True)
    tmp = store.with_name(f"{store.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data))
    os.chmod(tmp, 0o600)
    os.replace(tmp, store)


def make_invite(*, flagship: str, user: str, pubkey: str, store: Path, now=time.time) -> dict:
    if not _FLAGSHIP.match(flagship or ""):
        flagship = re.sub(r"[^A-Za-z0-9._-]", "-", flagship or "hermes").strip("-.")[:64] or "hermes"
    pubkey = check_pubkey(pubkey)
    t = now()
    nonce = secrets.token_hex(16)
    exp = int(t + INVITE_TTL)
    invites = {n: e for n, e in _load_invites(store).items() if isinstance(e, (int, float)) and e > t}
    invites[nonce] = exp
    _save_invites(store, invites)
    code = encode(INVITE_PREFIX, {"v": 1, "flagship": flagship, "user": user, "pubkey": pubkey,
                                  "nonce": nonce, "exp": exp})
    return {"code": code, "expires_at": _iso(exp), "flagship": flagship}


def read_invite(code, now=time.time) -> dict:
    data = decode(INVITE_PREFIX, code)
    exp = data.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        raise PairingError("invalid expiry in the code")
    if exp <= now():
        raise PairingError("this invite has expired; ask the flagship for a new one")
    return {"flagship": _text(data, "flagship", _FLAGSHIP, "flagship name"),
            "user": str(data.get("user") or "")[:64], "pubkey": check_pubkey(data.get("pubkey")),
            "nonce": _text(data, "nonce", _NONCE, "nonce"), "exp": exp}


def read_ship(code, store: Path, now=time.time) -> dict:
    """A validated ship code whose nonce is an unexpired invite issued here (not spent)."""
    data = decode(SHIP_PREFIX, code)
    nonce = _text(data, "nonce", _NONCE, "nonce")
    exp = _load_invites(store).get(nonce)
    if not isinstance(exp, (int, float)):
        raise PairingError("this ship code answers an invite this Hermes did not issue, or one already "
                           "used; make a new invite")
    if exp <= now():
        raise PairingError("the invite this ship code answers has expired; make a new invite")
    hosts = [_text(data, "host", _HOST, "host")]
    alt = data.get("alt_hosts") or []
    if not isinstance(alt, list) or len(alt) > 8:
        raise PairingError("invalid alt_hosts in the code")
    for value in alt:
        host = _text({"h": value}, "h", _HOST, "host")
        if host not in hosts:
            hosts.append(host)
    port = data.get("port", 22)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise PairingError("invalid port in the code")
    os_name = data.get("os") or "linux"
    if os_name not in ("linux", "macos", "windows"):
        raise PairingError("invalid os in the code")
    crew = data.get("crew_names") or []
    if not isinstance(crew, list):
        raise PairingError("invalid crew_names in the code")
    return {"nonce": nonce, "name": _text(data, "name", _NAME, "machine name"), "hosts": hosts,
            "port": port, "user": _text(data, "user", _USER, "user"),
            "mac": _text(data, "mac", _MAC, "MAC", required=False),
            "broadcast": _text(data, "broadcast", re.compile(r"^\d+\.\d+\.\d+\.\d+$"), "broadcast",
                               required=False),
            "wol": data.get("wol") if data.get("wol") in ("enabled", "disabled") else "unknown",
            "os": os_name, "crew_names": [str(c)[:64] for c in crew[:32] if isinstance(c, str) and c.strip()]}


def spend(nonce: str, store: Path) -> None:
    invites = _load_invites(store)
    if invites.pop(nonce, None) is not None:
        _save_invites(store, invites)


# --- ship: authorized_keys ----------------------------------------------------------------------------

def _key_body(line: str) -> str | None:
    """The base64 blob of an authorized_keys line (options before the key type are skipped)."""
    words = line.split()
    for i, word in enumerate(words[:-1]):
        if word in ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256"):
            return words[i + 1]
    return None


def authorize(pubkey: str, flagship: str, home: Path | None = None) -> tuple[bool, Path]:
    """Append ``pubkey`` to ``~/.ssh/authorized_keys`` (700 / 600). False when it was already there."""
    pubkey = check_pubkey(pubkey)
    if not _FLAGSHIP.match(flagship or ""):
        raise PairingError("invalid flagship name")
    ssh = Path(home or Path.home()) / ".ssh"
    path = ssh / "authorized_keys"
    if not ssh.is_dir():
        ssh.mkdir(mode=0o700, parents=True)
    os.chmod(ssh, 0o700)
    kind, body = pubkey.split()[:2]
    existing = path.read_text() if path.is_file() else ""
    if any(_key_body(line) == body for line in existing.splitlines()):
        os.chmod(path, 0o600)
        return False, path
    line = f"{kind} {body} {COMMENT}{flagship}\n"
    with open(path, "a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        handle.write(line)
    os.chmod(path, 0o600)
    return True, path


def deauthorize(flagship: str, home: Path | None = None) -> int:
    """Remove the lines Flota added for ``flagship``; other lines are left byte for byte."""
    path = Path(home or Path.home()) / ".ssh" / "authorized_keys"
    if not path.is_file():
        return 0
    lines = path.read_text().splitlines(keepends=True)
    keep = [line for line in lines if not line.rstrip("\r\n").endswith(f" {COMMENT}{flagship}")]
    if len(keep) != len(lines):
        path.write_text("".join(keep))
        os.chmod(path, 0o600)
    return len(lines) - len(keep)


# --- ship: what it knows about itself -------------------------------------------------------------------

def suggested_name(hostname: str) -> str:
    name = re.sub(r"[^a-z0-9_-]+", "-", (hostname or "").split(".")[0].lower()).strip("-_")[:58]
    return name if name and _NAME.match(name) else "ship"


def ship_payload(invite: dict, facts: dict, crew_names: list[str]) -> dict:
    hosts = [h for h in (facts.get("tailscale"), facts.get("lan")) if h]
    if not hosts:
        hosts = [facts.get("hostname") or "localhost"]
    net = facts.get("net") or {}
    return {"v": 1, "nonce": invite["nonce"], "name": suggested_name(facts.get("hostname", "")),
            "host": hosts[0], "alt_hosts": hosts[1:], "port": 22, "user": facts.get("user", ""),
            "mac": net.get("mac", ""), "broadcast": net.get("broadcast", ""), "wol": net.get("wol", "unknown"),
            "os": facts.get("os", "linux"), "crew_names": list(crew_names)[:32]}


def tcp_open(host: str, port: int = 22, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --- ship: can the flagship reach me? (run locally, before or after joining) -----------------------

_SSHD_FIX = {"linux": "sudo systemctl enable --now sshd   (Debian/Ubuntu: sudo apt install openssh-server && "
                      "sudo systemctl enable --now ssh)",
             "macos": "System Settings > General > Sharing > Remote Login: on",
             "windows": "Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0; "
                        "Start-Service sshd; Set-Service -Name sshd -StartupType Automatic   (admin PowerShell)"}
_FIREWALL_FIX = {
    "ufw": "as root: ufw allow from 100.64.0.0/10 to any port 22 proto tcp   (and your LAN: "
           "ufw allow from <LAN subnet> to any port 22 proto tcp)",
    "firewalld": "as root: firewall-cmd --permanent --add-rich-rule='rule family=ipv4 source address=100.64.0.0/10 "
                 "port port=22 protocol=tcp accept' && firewall-cmd --reload",
    "windows": "New-NetFirewallRule -Name flota-ssh -DisplayName 'Flota SSH' -Direction Inbound -Protocol TCP "
               "-LocalPort 22 -Action Allow -RemoteAddress 100.64.0.0/10,LocalSubnet -Profile Any   (admin PowerShell)",
    "macos": "Remote Login opens its own firewall rule; if the macOS firewall blocks all incoming connections, "
             "System Settings > Network > Firewall > Options: untick 'Block all incoming connections'"}
_TAILSCALE_FIX = {"linux": "install Tailscale for your distribution (https://tailscale.com/download/linux), "
                           "then as root: tailscale up",
                  "macos": "install Tailscale from https://tailscale.com/download/mac and log in",
                  "windows": "winget install tailscale.tailscale, then log in from the tray icon"}


def _active(unit: str, run) -> bool:
    try:
        done = run(["systemctl", "is-active", unit], timeout=5, capture_output=True, text=True)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return (done.stdout or "").strip() == "active"


def _perm_problem(home: Path) -> str:
    ssh = home / ".ssh"
    keys = ssh / "authorized_keys"
    try:
        if home.stat().st_mode & 0o022:
            return f"{home} is writable by group/others (sshd StrictModes refuses keys)"
        if ssh.exists() and ssh.stat().st_mode & 0o077:
            return f"{ssh} is {oct(ssh.stat().st_mode & 0o777)}, must be 700"
        if keys.exists() and keys.stat().st_mode & 0o022:
            return f"{keys} is writable by group/others, must be 600"
    except OSError as exc:
        return str(exc)
    return ""


def join_checks(*, os_name: str, net: dict, tailscale: str | None, tailscale_installed: bool,
                home: Path, run=subprocess.run, tcp=tcp_open) -> list[dict]:
    """Local checks a ship runs so the flagship can SSH in. ``ok`` is True / False / None (unknown)."""
    checks = []

    def add(id_, ok, label, fix=""):
        checks.append({"id": id_, "ok": ok, "label": label, "fix": fix})

    sshd = tcp("127.0.0.1", 22)
    add("sshd", sshd, "SSH server listening on port 22" if sshd else "No SSH server on port 22",
        "" if sshd else _SSHD_FIX[os_name])
    for id_, ip, what in (("sshd_tailscale", tailscale, "Tailscale"), ("sshd_lan", net.get("ip"), "LAN")):
        if ip and sshd:
            ok = tcp(ip, 22)
            add(id_, ok, f"SSH answers on the {what} address {ip}" if ok
                else f"SSH does not answer on the {what} address {ip}",
                "" if ok else "sshd listens only on some addresses: remove ListenAddress from "
                              "/etc/ssh/sshd_config (or add this one) and restart sshd")
    if os_name == "linux":
        walls = [name for name in ("ufw", "firewalld") if _active(name, run)]
        if walls:
            add("firewall", None, f"{walls[0]} is active: make sure it lets port 22 in", _FIREWALL_FIX[walls[0]])
        else:
            add("firewall", True, "No ufw / firewalld running")
    else:
        add("firewall", None, "Check the firewall lets SSH (port 22) in", _FIREWALL_FIX[os_name])
    if tailscale:
        add("tailscale", True, f"Tailscale up: {tailscale}")
    elif tailscale_installed:
        add("tailscale", False, "Tailscale is installed but not connected",
            "as root: tailscale up" if os_name == "linux" else "open Tailscale and log in")
    else:
        add("tailscale", None, "No Tailscale: the flagship can only reach this machine on the LAN",
            _TAILSCALE_FIX[os_name])
    if os_name != "windows":
        problem = _perm_problem(Path(home))
        add("ssh_perms", not problem, problem or "SSH directory permissions are fine",
            "cd && chmod go-w . && chmod 700 .ssh && chmod 600 .ssh/authorized_keys" if problem else "")
    wol = net.get("wol", "unknown")
    if not net.get("mac"):
        add("wol", None, f"Wake-on-LAN: no LAN interface found ({net.get('reason') or 'unknown'})", "")
    elif wol == "enabled":
        add("wol", True, f"Wake-on-LAN enabled on {net.get('interface')}")
    else:
        from .detect import enable_wol_hint
        add("wol", False if wol == "disabled" else None,
            f"Wake-on-LAN {'disabled' if wol == 'disabled' else 'state unknown'} on {net.get('interface')}",
            "; ".join(enable_wol_hint(os_name, net)))
    if net.get("broadcast"):
        add("wol_reach", None, f"Wake-on-LAN only works if the flagship is on this LAN ({net['broadcast']}); "
                               "magic packets do not cross routers or Tailscale",
            "otherwise the flagship uses wake 'wol_via' (an always-on machine on this LAN sends the packet) "
            "or 'none' (turn it on by hand)")
    return checks


# --- flagship: reach the ship ------------------------------------------------------------------------------

def classify_ssh(stderr: str, host: str) -> tuple[str, str]:
    """``(reason, fix)`` for a failed ``ssh ... true``."""
    text = stderr or ""
    if "REMOTE HOST IDENTIFICATION HAS CHANGED" in text or "Host key verification failed" in text:
        return ("its SSH host key differs from the one this host remembers",
                f"if the ship was reinstalled or the address now belongs to it: ssh-keygen -R {host}, "
                "then add it again; otherwise stop: something else answers on that address")
    if "Permission denied" in text:
        return ("the ship refused the flagship's key (not in its authorized_keys, or wrong user)",
                "on the ship, run Join again with a fresh invite (hermes flota join <invite>)")
    if "Connection refused" in text:
        return "nothing listens on port 22", "on the ship, start the SSH server (Join shows the command)"
    if "timed out" in text.lower():
        return "the connection timed out", "a firewall drops port 22 or sshd is off: run the checks in Join on the ship"
    if "Could not resolve" in text:
        return "the name does not resolve from here", "use the ship's IP (Tailscale 100.x or LAN)"
    if "No route to host" in text or "Network is unreachable" in text:
        return "no route to that address", "the ship is on another network: use Tailscale on both machines"
    last = text.strip().splitlines()[-1] if text.strip() else "unknown error"
    return last, "run `hermes flota doctor` after adding it"


def probe_host(host: str, *, user: str, port: int, key: str | None, run=subprocess.run,
               tcp=tcp_open) -> tuple[bool, str, str]:
    """``(ok, reason, fix)``: TCP 22, then a BatchMode ``ssh true`` with Flota's key. A first
    contact records the ship's host key (accept-new); a changed one is refused."""
    if host.startswith("-") or user.startswith("-"):
        return False, "invalid address", ""
    if not tcp(host, port):
        return (False, f"port {port} does not answer (timeout or refused)",
                "on the ship: is the SSH server on and does the firewall allow port 22? (Join shows the checks)")
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new",
            "-p", str(port)] + (["-i", key] if key else []) + [f"{user}@{host}", "true"]
    try:
        done = run(argv, timeout=20, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return False, "ssh timed out after 20s", "a firewall or a slow link: run the checks in Join on the ship"
    except OSError as exc:
        return False, f"could not run ssh: {exc}", "install the OpenSSH client"
    if done.returncode == 0:
        return True, "SSH login works", ""
    reason, fix = classify_ssh(done.stderr or "", host)
    return False, reason, fix


def choose_host(ship: dict, *, key: str | None, run=subprocess.run, tcp=tcp_open) -> tuple[str | None, list[str]]:
    lines = []
    for host in ship["hosts"]:
        ok, reason, fix = probe_host(host, user=ship["user"], port=ship["port"], key=key, run=run, tcp=tcp)
        kind = "Tailscale" if _is_tailnet(host) else "address"
        if ok:
            lines.append(f"✓ {ship['user']}@{host} ({kind}): {reason}")
            return host, lines
        lines.append(f"✗ {ship['user']}@{host} ({kind}): {reason}" + (f" → {fix}" if fix else ""))
    return None, lines


_TAILNET = ipaddress.ip_network("100.64.0.0/10")  # Tailscale's CGNAT range


def _is_tailnet(host: str) -> bool:
    try:
        return ipaddress.ip_address(host) in _TAILNET
    except ValueError:
        return False
