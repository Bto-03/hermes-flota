"""Pairing: invite -> join -> ship code -> add-ship, in a temporary HOME. No network, no real ssh."""
import argparse
import copy
import ipaddress
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from flota_plugin import cli, pairing

ACTIVE = Path("/fake-home/config.yaml")
PROFILES = Path("/fake-home/profiles")
def fake_pub(comment: str, fill: bytes = b"flota") -> str:
    """A well-formed ssh-ed25519 public key line built at runtime (32 filler bytes, no real key)."""
    import base64
    import struct
    body = (fill * 32)[:32]
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + body
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}"


PUB = fake_pub("carol@estudio")
NET = {"interface": "enp3s0", "mac": "aa:bb:cc:dd:ee:ff", "broadcast": "10.0.0.255", "wol": "enabled",
       "connection": "Wired", "reason": "", "ip": "10.0.0.5"}


def done(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


@pytest.fixture
def env(monkeypatch, tmp_path):
    configs = {ACTIVE: {}}
    flagship_home, ship_home = tmp_path / "flagship", tmp_path / "ship"
    flagship_home.mkdir()
    ship_home.mkdir(mode=0o755)
    (flagship_home / ".ssh").mkdir()
    (flagship_home / ".ssh" / "id_ed25519.pub").write_text(PUB + "\n")
    e = SimpleNamespace(configs=configs, home=flagship_home, ship_home=ship_home, ssh=[], tcp={},
                        nets=[ipaddress.ip_interface("10.0.0.2/24")], tmp=tmp_path)

    def update(path, mutate):
        data = copy.deepcopy(configs.setdefault(path, {}))
        mutate(data)
        configs[path] = data

    def create(name):
        configs[PROFILES / name / "config.yaml"] = {}
        return PROFILES / name

    monkeypatch.setattr(cli, "_active_config_path", lambda: ACTIVE)
    monkeypatch.setattr(cli, "_read_config", lambda path: copy.deepcopy(configs.get(path, {})))
    monkeypatch.setattr(cli, "_update_config", update)
    monkeypatch.setattr(cli, "_profile_exists", lambda name: PROFILES / name / "config.yaml" in configs)
    monkeypatch.setattr(cli, "_profile_dir", lambda name: PROFILES / name)
    monkeypatch.setattr(cli, "_create_profile", create)
    monkeypatch.setattr(cli, "_detect_prefix", lambda m: ("", "systemctl works directly"))
    monkeypatch.setattr(cli, "_is_up", lambda m: False)
    monkeypatch.setattr(cli, "_hermes_home", lambda: tmp_path / "hermes")
    monkeypatch.setattr(cli, "_home", lambda: e.home)
    monkeypatch.setattr(cli, "_hostname", lambda: "Torre.local")
    monkeypatch.setattr(cli, "_login_user", lambda: "carol")
    monkeypatch.setattr(cli, "_local_os", lambda: "linux")
    monkeypatch.setattr(cli, "_local_net", lambda os_name: dict(NET))
    monkeypatch.setattr(cli, "_tailscale", lambda: ("203.0.113.2", True))
    monkeypatch.setattr(cli, "_local_crew_names", lambda: ["Lia"])
    monkeypatch.setattr(cli, "_local_networks", lambda: e.nets)
    monkeypatch.setattr(cli, "_tcp", lambda host, port=22: e.tcp.get(host, True))

    def ssh(argv, **kw):
        e.ssh.append(argv)
        host = argv[-2].split("@")[1]
        return e.ssh_answers.get(host, done()) if hasattr(e, "ssh_answers") else done()

    monkeypatch.setattr(cli, "_ssh_run", ssh)
    return e


def panel(env):
    return env.configs[ACTIVE]["plugins"]["entries"]["flota"]["settings"]


def as_ship(env):
    env.home = env.ship_home


def test_round_trip_invite_join_add_ship(env):
    invite = cli.run_invite()
    assert invite["code"].startswith("FLOTA1-INVITE-") and invite["flagship"] == "Torre.local"
    data = pairing.read_invite(invite["code"])
    assert data["pubkey"] == PUB and data["user"] == "carol"

    as_ship(env)
    joined = cli.run_join(invite["code"])
    keys = env.ship_home / ".ssh" / "authorized_keys"
    assert keys.read_text() == PUB.rsplit(" ", 1)[0] + " flota-flagship:Torre.local\n"
    assert stat.S_IMODE(keys.stat().st_mode) == 0o600
    assert stat.S_IMODE((env.ship_home / ".ssh").stat().st_mode) == 0o700
    ship = pairing.decode(pairing.SHIP_PREFIX, joined["ship_code"])
    assert ship["host"] == "203.0.113.2" and ship["alt_hosts"] == ["10.0.0.5"]
    assert ship["name"] == "torre" and ship["mac"] == NET["mac"] and ship["crew_names"] == ["Lia"]
    assert ship["nonce"] == data["nonce"]
    assert panel(env)["joined_fleets"][0]["flagship"] == "Torre.local"
    assert panel(env)["joined_fleets"][0]["pubkey_fingerprint"].startswith("SHA256:")
    assert cli.role() == "ship"
    assert {c["id"] for c in joined["checks"]} >= {"sshd", "firewall", "tailscale", "wol"}

    env.configs[ACTIVE] = {}  # back on the flagship
    env.home = env.tmp / "flagship"
    result = cli.run_add_ship(joined["ship_code"])
    assert result["ok"], result["lines"]
    entry = panel(env)["machines"]["torre"]
    assert entry["host"] == "203.0.113.2" and entry["user"] == "carol" and entry["wake"] == "wol"
    assert entry["crew_names"] == ["Lia"] and entry["alt_hosts"] == ["10.0.0.5"]
    assert entry["key"] == str(env.home / ".ssh" / "id_ed25519")
    assert env.ssh[0][:7] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o",
                              "StrictHostKeyChecking=accept-new"]
    assert any("✓ carol@203.0.113.2 (Tailscale)" in line for line in result["lines"])
    assert env.configs[PROFILES / "flota-torre" / "config.yaml"]["terminal"]["ssh_host"] == "203.0.113.2"
    # nonce is spent: the same ship code is refused
    with pytest.raises(pairing.PairingError, match="already used"):
        cli.run_add_ship(joined["ship_code"])


def _ship_code(env, **override):
    invite = cli.run_invite()
    nonce = pairing.read_invite(invite["code"])["nonce"]
    payload = {"v": 1, "nonce": nonce, "name": "torre", "host": "203.0.113.2", "alt_hosts": ["10.0.0.5"],
               "port": 22, "user": "carol", "mac": NET["mac"], "broadcast": "10.0.0.255", "wol": "enabled",
               "os": "linux", "crew_names": []}
    payload.update(override)
    return pairing.encode(pairing.SHIP_PREFIX, payload)


def test_add_ship_falls_back_to_the_lan_address_and_explains(env):
    code = _ship_code(env)
    env.tcp["203.0.113.2"] = False
    result = cli.run_add_ship(code)
    assert result["ok"]
    assert panel(env)["machines"]["torre"]["host"] == "10.0.0.5"
    assert "port 22 does not answer" in result["lines"][0] and "firewall" in result["lines"][0]


def test_add_ship_off_lan_uses_wake_none_and_says_wol_via(env):
    env.nets = [ipaddress.ip_interface("192.0.2.2/24")]
    result = cli.run_add_ship(_ship_code(env))
    assert panel(env)["machines"]["torre"]["wake"] == "none"
    assert any("wol_via" in line for line in result["lines"])


@pytest.mark.parametrize("stderr, expect", [
    ("carol@h: Permission denied (publickey).", "run Join again"),
    ("@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\nHost key verification failed.", "ssh-keygen -R"),
    ("ssh: connect to host h port 22: Connection timed out", "firewall"),
])
def test_add_ship_failure_saves_nothing_and_keeps_the_nonce(env, stderr, expect):
    code = _ship_code(env)
    env.ssh_answers = {"203.0.113.2": done(255, err=stderr), "10.0.0.5": done(255, err=stderr)}
    result = cli.run_add_ship(code)
    assert not result["ok"] and expect in "\n".join(result["lines"])
    assert "machines" not in env.configs[ACTIVE].get("plugins", {}).get("entries", {}).get("flota", {}).get(
        "settings", {})
    del env.ssh_answers
    assert cli.run_add_ship(code)["ok"]  # same code works once fixed


def test_expired_invite_and_unknown_nonce(env):
    now = [1000.0]
    invite = pairing.make_invite(flagship="estudio", user="carol", pubkey=PUB, store=env.tmp / "inv.json",
                                 now=lambda: now[0])
    assert pairing.read_invite(invite["code"], now=lambda: 1000.0 + 60)
    with pytest.raises(pairing.PairingError, match="expired"):
        pairing.read_invite(invite["code"], now=lambda: 1000.0 + pairing.INVITE_TTL + 1)
    nonce = pairing.read_invite(invite["code"], now=lambda: 1000.0)["nonce"]
    ship = pairing.encode(pairing.SHIP_PREFIX, {"v": 1, "nonce": nonce, "name": "x", "host": "h", "user": "u"})
    with pytest.raises(pairing.PairingError, match="expired"):
        pairing.read_ship(ship, env.tmp / "inv.json", now=lambda: 1000.0 + pairing.INVITE_TTL + 1)
    forged = pairing.encode(pairing.SHIP_PREFIX, {"v": 1, "nonce": "Z" * 24, "name": "x", "host": "h", "user": "u"})
    with pytest.raises(pairing.PairingError, match="did not issue"):
        pairing.read_ship(forged, env.tmp / "inv.json", now=lambda: 1000.0)


@pytest.mark.parametrize("code, match", [
    ("hello", "must start with"),
    ("FLOTA1-INVITE-!!!!", "damaged"),
    ("FLOTA1-INVITE-" + "A" * 9000, "longer than"),
    (pairing.encode(pairing.INVITE_PREFIX, {"v": 2}), "version"),
    (pairing.encode(pairing.SHIP_PREFIX, {"v": 1}), "ship code"),
    (pairing.encode(pairing.INVITE_PREFIX, {"v": 1, "flagship": "x", "pubkey": "-----BEGIN OPENSSH PRIVATE KEY-----",
                                            "nonce": "n" * 20, "exp": 9e12}), "public key"),
    (pairing.encode(pairing.INVITE_PREFIX, {"v": 1, "flagship": "x", "pubkey": PUB + "\nssh-rsa AAAA evil",
                                            "nonce": "n" * 20, "exp": 9e12}), "public key"),
    (pairing.encode(pairing.INVITE_PREFIX, {"v": 1, "flagship": "-oProxyCommand=x", "pubkey": PUB,
                                            "nonce": "n" * 20, "exp": 9e12}), "flagship"),
])
def test_malformed_invites_are_refused(code, match):
    with pytest.raises(pairing.PairingError, match=match):
        pairing.read_invite(code)


@pytest.mark.parametrize("field, value", [("host", "-oProxyCommand=x"), ("user", "-l"), ("name", "../x"),
                                          ("mac", "zz"), ("os", "plan9"), ("alt_hosts", ["a b"]), ("port", 0)])
def test_malformed_ship_fields_are_refused(env, field, value):
    with pytest.raises(pairing.PairingError):
        pairing.read_ship(_ship_code(env, **{field: value}), cli._invites_store())


def test_authorize_is_idempotent_and_leave_removes_only_its_line(tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir(mode=0o755)
    keys = ssh / "authorized_keys"
    keys.write_text("ssh-rsa AAAAother me@laptop")  # no trailing newline
    os.chmod(keys, 0o644)
    assert pairing.authorize(PUB, "estudio", tmp_path)[0] is True
    assert pairing.authorize(PUB, "estudio", tmp_path)[0] is False
    assert pairing.authorize(PUB.replace("carol@estudio", "other comment"), "estudio", tmp_path)[0] is False
    assert keys.read_text().count(PUB.split()[1]) == 1
    assert keys.read_text().startswith("ssh-rsa AAAAother me@laptop\n")
    assert stat.S_IMODE(ssh.stat().st_mode) == 0o700 and stat.S_IMODE(keys.stat().st_mode) == 0o600
    other = fake_pub("x", b"other").rsplit(" ", 1)[0]
    pairing.authorize(other, "oficina", tmp_path)
    assert pairing.deauthorize("estudio", tmp_path) == 1
    assert keys.read_text() == f"ssh-rsa AAAAother me@laptop\n{other} flota-flagship:oficina\n"


def test_leave_fleet_via_cli(env):
    invite = cli.run_invite()
    as_ship(env)
    cli.run_join(invite["code"])
    assert [f["flagship"] for f in cli.fleets()] == ["Torre.local"]
    assert cli.leave_fleet("Torre.local") == 1
    assert cli.fleets() == []
    assert "flota-flagship" not in (env.ship_home / ".ssh" / "authorized_keys").read_text()
    with pytest.raises(KeyError):
        cli.leave_fleet("Torre.local")


def test_flota_key_generates_one_when_missing(tmp_path):
    calls = []

    def keygen(argv, **kw):
        calls.append(argv)
        Path(argv[-1] + ".pub").write_text(PUB.replace("carol@estudio", "flota") + "\n")
        return done()

    pub, private = pairing.flota_key(None, tmp_path, run=keygen)
    assert private == str(tmp_path / ".ssh" / "flota_ed25519") and pub.endswith(" flota")
    assert calls[0][:2] == ["ssh-keygen", "-q"] and "-N" in calls[0] and calls[0][calls[0].index("-N") + 1] == ""
    assert pairing.flota_key(None, tmp_path, run=keygen)[1] == private and len(calls) == 1  # reused


def test_role_is_inferred_then_chosen(env):
    assert cli.role() is None
    env.configs[ACTIVE] = {"plugins": {"entries": {"flota": {"settings": {"machines": {"a": {}}}}}}}
    assert cli.role() == "flagship"
    with pytest.raises(cli.RoleConflict):  # it leads a ship
        cli.set_role("ship")
    env.configs[ACTIVE] = {}
    cli.set_role("ship")
    assert cli.role() == "ship"
    with pytest.raises(ValueError):
        cli.set_role("captain")


def test_join_checks_give_os_specific_fixes(tmp_path):
    def run(argv, **kw):
        return done(out="active\n" if argv[-1] == "ufw" else "inactive\n")

    linux = pairing.join_checks(os_name="linux", net=dict(NET, wol="disabled"), tailscale=None,
                                tailscale_installed=False, home=tmp_path, run=run, tcp=lambda h, p=22: False)
    by = {c["id"]: c for c in linux}
    assert by["sshd"]["ok"] is False and "systemctl enable --now sshd" in by["sshd"]["fix"]
    assert by["firewall"]["ok"] is None and "ufw allow from 100.64.0.0/10 to any port 22" in by["firewall"]["fix"]
    assert by["tailscale"]["ok"] is None and by["wol"]["ok"] is False and "ethtool" in by["wol"]["fix"]
    assert "wol_via" in by["wol_reach"]["fix"]
    win = {c["id"]: c for c in pairing.join_checks(os_name="windows", net={}, tailscale="203.0.113.9",
                                                    tailscale_installed=True, home=tmp_path,
                                                    tcp=lambda h, p=22: True)}
    assert "Add-WindowsCapability" in pairing.join_checks(
        os_name="windows", net={}, tailscale=None, tailscale_installed=False, home=tmp_path,
        tcp=lambda h, p=22: False)[0]["fix"]
    assert "New-NetFirewallRule" in win["firewall"]["fix"] and "-RemoteAddress 100.64.0.0/10" in win["firewall"]["fix"]
    assert "-Profile Any" in win["firewall"]["fix"] and win["tailscale"]["ok"] is True
    assert win["sshd_tailscale"]["ok"] is True
    mac = pairing.join_checks(os_name="macos", net={}, tailscale=None, tailscale_installed=False,
                              home=tmp_path, tcp=lambda h, p=22: False)
    assert "Remote Login" in mac[0]["fix"]


def test_ssh_perms_check(tmp_path):
    home = tmp_path / "h"
    (home / ".ssh").mkdir(parents=True)
    os.chmod(home, 0o755)
    os.chmod(home / ".ssh", 0o755)
    by = {c["id"]: c for c in pairing.join_checks(os_name="linux", net={}, tailscale=None, tailscale_installed=False,
                                                   home=home, run=lambda *a, **k: done(), tcp=lambda h, p=22: True)}
    assert by["ssh_perms"]["ok"] is False and "chmod 700 .ssh" in by["ssh_perms"]["fix"]


def test_cli_invite_join_commands(env, capsys, monkeypatch):
    parser = argparse.ArgumentParser(prog="hermes flota")
    cli.setup_parser(parser)
    args = parser.parse_args(["invite"])
    assert args.func(args) == 0
    code = capsys.readouterr().out.strip().splitlines()[-1]
    as_ship(env)
    args = parser.parse_args(["join", code])  # not a tty: refuses without --yes
    assert args.func(args) == 1
    args = parser.parse_args(["join", code, "--yes"])
    assert args.func(args) == 0
    assert capsys.readouterr().out.strip().splitlines()[-1].startswith("FLOTA1-SHIP-")
    args = parser.parse_args(["join", "garbage", "--yes"])
    assert args.func(args) == 2
    args = parser.parse_args(["leave", "nobody"])
    assert args.func(args) == 2


# --- a ship already in the fleet is updated, not duplicated --------------------------------------

ESCRITORIO = {"profile": "flota-escritorio", "host": "192.0.2.28", "user": "carol", "wake": "wol",
              "mac": "AA:BB:CC:DD:EE:FF", "description": "the desktop", "port": 22}


def _fleet(env, **machines):
    env.configs[ACTIVE] = {"plugins": {"entries": {"flota": {"settings": {"machines": machines}}}}}


@pytest.mark.parametrize("override", [
    {"mac": NET["mac"], "host": "203.0.113.2", "alt_hosts": []},  # same MAC, new address
    {"mac": "", "host": "192.0.2.28", "alt_hosts": []},  # same host, no MAC
    {"mac": "", "host": "10.9.9.9", "alt_hosts": ["192.0.2.28"]},  # one of its addresses
])
def test_add_ship_updates_the_machine_with_the_same_mac_or_host(env, override):
    _fleet(env, escritorio=dict(ESCRITORIO), other={"profile": "flota-other", "host": "10.0.0.9"})
    env.configs[PROFILES / "flota-escritorio" / "config.yaml"] = {"terminal": {"backend": "ssh"}}
    result = cli.run_add_ship(_ship_code(env, name="desk-pc", crew_names=["Lia"], **override))
    assert result["ok"], result["lines"]
    assert result["machine"] == "escritorio"
    machines = panel(env)["machines"]
    assert set(machines) == {"escritorio", "other"}  # no desk-pc duplicate
    entry = machines["escritorio"]
    assert entry["profile"] == "flota-escritorio" and entry["description"] == "the desktop"
    assert entry["crew_names"] == ["Lia"] and entry["host"] == override["host"]
    assert not any(path.parent.name == "flota-desk-pc" for path in env.configs)
    assert any("already in the fleet as 'escritorio'" in line for line in result["lines"])


def test_add_ship_keeps_a_hand_picked_profile_and_wake(env):
    _fleet(env, escritorio={**ESCRITORIO, "profile": "pc", "wake": "wol_via", "via": "mini"},
           mini={"profile": "flota-mini", "host": "10.0.0.7", "wake": "none"})
    env.configs[PROFILES / "pc" / "config.yaml"] = {}
    result = cli.run_add_ship(_ship_code(env, name="desk-box", mac=NET["mac"]))
    assert result["ok"], result["lines"]
    entry = panel(env)["machines"]["escritorio"]
    assert entry["profile"] == "pc" and entry["wake"] == "wol_via" and entry["via"] == "mini"
    assert env.configs[PROFILES / "pc" / "config.yaml"]["terminal"]["ssh_host"] == "203.0.113.2"
    assert PROFILES / "flota-escritorio" / "config.yaml" not in env.configs


def test_add_ship_name_override_and_name_clashes(env):
    _fleet(env, torre={"profile": "flota-torre", "host": "10.0.0.66", "mac": "11:22:33:44:55:66"})
    # a different machine already holds the suggested name: the new one gets a free name
    result = cli.run_add_ship(_ship_code(env, mac="aa:aa:aa:aa:aa:aa", host="10.0.0.70", alt_hosts=[]))
    assert result["machine"] == "torre-2" and panel(env)["machines"]["torre"]["host"] == "10.0.0.66"
    result = cli.run_add_ship(_ship_code(env, mac="bb:bb:bb:bb:bb:bb", host="10.0.0.71", alt_hosts=[]),
                              name="estudio")
    assert result["ok"] and result["machine"] == "estudio"
    assert panel(env)["machines"]["estudio"]["profile"] == "flota-estudio"
    with pytest.raises(pairing.PairingError, match="already named 'torre'"):
        cli.run_add_ship(_ship_code(env, mac="cc:cc:cc:cc:cc:cc", host="10.0.0.72", alt_hosts=[]), name="torre")
    with pytest.raises(pairing.PairingError, match="invalid name"):
        cli.run_add_ship(_ship_code(env), name="Bad Name")


def test_cli_add_ship_accepts_name(env, capsys):
    parser = argparse.ArgumentParser(prog="hermes flota")
    cli.setup_parser(parser)
    args = parser.parse_args(["add-ship", _ship_code(env), "--name", "estudio"])
    assert args.func(args) == 0
    assert "estudio" in panel(env)["machines"] and "torre" not in panel(env)["machines"]


# --- role lifecycle: flagship <-> ship, one fleet per ship ----------------------------------------

def _joined(env, flagship="Torre.local"):
    """This machine (the ship) joined ``flagship``'s fleet."""
    invite = pairing.make_invite(flagship=flagship, user="carol", pubkey=PUB, store=env.tmp / "inv.json")
    as_ship(env)
    return cli.run_join(invite["code"])


def test_status_fields_fleet_and_ships_count(env):
    assert cli.fleet() is None and cli.ships_count() == 0
    _joined(env)
    fleet = cli.fleet()
    assert fleet["flagship"] == "Torre.local" and fleet["joined_at"]
    assert set(fleet) >= {"flagship", "joined_at"}
    _fleet(env, a={"host": "h"}, b={"host": "i"})
    assert cli.ships_count() == 2 and cli.fleet() is None


def test_join_when_already_in_another_fleet_is_refused(env):
    _joined(env, "Torre.local")
    keys = (env.ship_home / ".ssh" / "authorized_keys").read_text()
    other = pairing.make_invite(flagship="oficina", user="ana", pubkey=fake_pub("carol@estudio", b"oficina"),
                                store=env.tmp / "inv2.json")
    with pytest.raises(cli.AlreadyInFleet) as caught:
        cli.run_join(other["code"])
    assert caught.value.fleet["flagship"] == "Torre.local"
    assert (env.ship_home / ".ssh" / "authorized_keys").read_text() == keys  # no key added
    assert [f["flagship"] for f in cli.fleets()] == ["Torre.local"]


def test_rejoining_the_same_flagship_refreshes_it(env):
    _joined(env, "Torre.local")
    _joined(env, "Torre.local")  # a fresh invite from the same flagship (e.g. after it changed key)
    assert [f["flagship"] for f in cli.fleets()] == ["Torre.local"]


def test_flagship_to_ship_needs_its_ships_removed(env):
    _fleet(env, torre={"host": "10.0.0.5", "profile": "flota-torre"}, mini={"host": "m"})
    env.configs[PROFILES / "flota-torre" / "config.yaml"] = {"terminal": {"backend": "ssh"}}
    assert cli.role() == "flagship"
    with pytest.raises(cli.RoleConflict) as caught:
        cli.set_role("ship")
    assert caught.value.needs == "remove_ships" and sorted(caught.value.extra["ships"]) == ["mini", "torre"]
    assert cli.role() == "flagship" and cli.ships_count() == 2  # nothing changed
    result = cli.set_role("ship", force=True)
    assert result["role"] == "ship" and sorted(result["removed_ships"]) == ["mini", "torre"]
    assert cli.role() == "ship" and cli.ships_count() == 0
    assert PROFILES / "flota-torre" / "config.yaml" in env.configs  # profiles are kept


def test_force_to_ship_also_clears_the_legacy_block(env):
    env.configs[ACTIVE] = {"delegation": {"backends": {"flota": {"machines": {"old": {"host": "o"}}}}}}
    assert cli.ships_count() == 1
    with pytest.raises(cli.RoleConflict):
        cli.set_role("ship")
    cli.set_role("ship", force=True)
    assert cli.ships_count() == 0 and cli.role() == "ship"


def test_ship_to_flagship_needs_to_leave_its_fleet(env):
    _joined(env)
    assert cli.role() == "ship"
    with pytest.raises(cli.RoleConflict) as caught:
        cli.set_role("flagship")
    assert caught.value.needs == "leave_fleet" and caught.value.extra["fleet"]["flagship"] == "Torre.local"
    assert "flota-flagship:Torre.local" in (env.ship_home / ".ssh" / "authorized_keys").read_text()
    result = cli.set_role("flagship", force=True)
    assert result["role"] == "flagship" and result["left_fleet"] == "Torre.local" and result["removed_keys"] == 1
    assert cli.fleet() is None and cli.role() == "flagship"
    assert "flota-flagship" not in (env.ship_home / ".ssh" / "authorized_keys").read_text()


def test_free_transitions_and_clearing_the_role(env):
    assert cli.set_role("flagship")["role"] == "flagship"  # no ships, no fleet: free
    assert cli.set_role("ship")["role"] == "ship"
    assert cli.set_role("flagship")["role"] == "flagship"
    assert cli.set_role(None)["role"] is None
    assert "role" not in panel(env)
    assert cli.role() is None
    with pytest.raises(ValueError):
        cli.set_role("captain")


def test_leave_without_a_name_leaves_the_current_fleet_and_clears_the_role(env):
    _joined(env)
    assert cli.leave_fleet() == 1
    assert cli.fleet() is None and "role" not in panel(env) and cli.role() is None
    with pytest.raises(KeyError):
        cli.leave_fleet()


def test_cli_role_and_leave_commands(env, capsys):
    parser = argparse.ArgumentParser(prog="hermes flota")
    cli.setup_parser(parser)

    def run(*argv):
        args = parser.parse_args(list(argv))
        return args.func(args)

    assert run("role") == 0 and "none" in capsys.readouterr().out.lower()
    _joined(env)
    assert run("role") == 0 and "Torre.local" in capsys.readouterr().out
    assert run("role", "flagship") == 1
    assert "leave" in capsys.readouterr().err
    assert run("role", "flagship", "--force") == 0 and cli.role() == "flagship"
    assert run("role", "none") == 0 and cli.role() is None
    _joined(env)
    assert run("leave") == 0 and cli.fleet() is None
    assert "Torre.local" in capsys.readouterr().out
    assert run("leave") == 2


def test_join_check_lists_wake_options(env, monkeypatch):
    monkeypatch.setattr(cli, "_wake_timer", lambda: {"ok": True, "os": "linux", "route": "direct", "reason": "",
                                                     "setup": []})
    options = {o["id"]: o for o in cli.join_check()["wake_options"]}
    assert list(options) == ["wol", "wol_via", "timer", "none"]
    assert options["wol"]["ok"] is True and options["timer"]["ok"] is True and options["none"]["ok"] is True
    assert all(isinstance(o["label"], str) and o["label"] for o in options.values())
    monkeypatch.setattr(cli, "_local_net", lambda os_name: dict(NET, wol="disabled"))
    monkeypatch.setattr(cli, "_wake_timer", lambda: None)  # no waketimer module: the option is left out
    options = {o["id"]: o for o in cli.join_check()["wake_options"]}
    assert list(options) == ["wol", "wol_via", "none"] and options["wol"]["ok"] is False
    monkeypatch.setattr(cli, "_local_net", lambda os_name: {"reason": "no LAN"})
    assert {o["id"]: o for o in cli.join_check()["wake_options"]}["wol"]["ok"] is False
