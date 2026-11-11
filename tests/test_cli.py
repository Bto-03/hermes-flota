"""``hermes flota``: status / dry-run text and ``setup``.

Most tests replace the five core seams in ``cli`` with an in-memory config store, so they need neither
Hermes' YAML stack nor a home directory. ``test_setup_with_real_core`` runs the real helpers against a
temporary HERMES_HOME; it skips itself when core's dependencies (ruamel.yaml) are not importable.
"""
import argparse
import copy
import ipaddress
import re
from pathlib import Path

import pytest

from flota_plugin import cli, power, slash

ACTIVE = Path("/fake-home/config.yaml")
PROFILES = Path("/fake-home/profiles")
START = {"model": {"default": "some/model", "provider": "openrouter"},
         "delegation": {"backend": "default", "backends": {"flota": {"machines": {
             "mini": {"profile": "flota-mini", "host": "mini.lan"}}}}}}
SETUP = ("setup", "torre", "--host", "10.0.0.5", "--user", "carol", "--key", "~/.ssh/id_torre", "--port", "2222",
         "--description", "GPU tower", "--wake", "wol", "--mac", "aa:bb:cc:dd:ee:ff", "--broadcast", "10.0.0.255")


@pytest.fixture
def store(monkeypatch):
    """path -> config, standing in for the config files; ``store.log`` records creates and writes."""
    class Store(dict):
        log: list = []

    configs = Store({ACTIVE: copy.deepcopy(START)})
    configs.log = []

    def create(name):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
            raise ValueError(f"Invalid profile name {name!r}")
        configs.log.append(("create", name))
        configs[PROFILES / name / "config.yaml"] = copy.deepcopy(configs[ACTIVE])
        return PROFILES / name

    def update(path, mutate):
        configs.log.append(("write", path))
        mutate(configs.setdefault(path, {}))

    monkeypatch.setattr(cli, "_active_config_path", lambda: ACTIVE)
    monkeypatch.setattr(cli, "_read_config", lambda path: copy.deepcopy(configs.get(path, {})))
    monkeypatch.setattr(cli, "_update_config", update)
    monkeypatch.setattr(cli, "_profile_exists", lambda name: PROFILES / name / "config.yaml" in configs)
    monkeypatch.setattr(cli, "_profile_dir", lambda name: PROFILES / name)
    monkeypatch.setattr(cli, "_create_profile", create)
    monkeypatch.setattr(slash, "_load_config", lambda: configs[ACTIVE])
    monkeypatch.setattr(slash, "_profile_exists", lambda name: False)
    monkeypatch.setattr(slash, "is_up", lambda host, port: False)
    monkeypatch.setattr(cli, "_read_busy", lambda: None)  # no gateway state file
    configs.detected = []

    def detect(machine):  # never real ssh; tests that care replace it
        configs.detected.append(machine)
        return "", "systemctl works directly"

    monkeypatch.setattr(cli, "_detect_prefix", detect)
    # Detection seams: away by default, so no test reaches a network; detection tests turn it on.
    configs.probes = []
    monkeypatch.setattr(cli, "_is_up", lambda machine: configs.probes.append("tcp") or False)
    monkeypatch.setattr(cli, "_detect_os", lambda machine: configs.probes.append("os") or "")
    monkeypatch.setattr(cli, "_detect_network", lambda machine, os: configs.probes.append("net") or {})
    return configs


def main(*argv) -> int:
    parser = argparse.ArgumentParser(prog="hermes flota")
    cli.setup_parser(parser)
    args = parser.parse_args(argv)
    return args.func(args)


def machines(config):
    return config["plugins"]["entries"]["flota"]["settings"]["machines"]


def legacy(config):
    return config["delegation"]["backends"]["flota"]["machines"]


def test_status_prints_the_slash_text(store, capsys):
    assert main("status") == 0
    out = capsys.readouterr().out
    assert "mini" in out and "down" in out and "flota-mini (profile missing)" in out


def test_dry_run_prints_the_slash_text(store, capsys):
    assert main("dry-run", "mini") == 0
    out = capsys.readouterr().out
    assert "mini.lan:22" in out and "no wake method" in out


def test_no_subcommand_prints_usage(store, capsys):
    assert main() == 0
    assert "usage: hermes flota" in capsys.readouterr().out


def test_setup_creates_the_profile_and_writes_both_configs(store, capsys):
    assert main(*SETUP) == 0
    profile_config = PROFILES / "flota-torre" / "config.yaml"
    assert store.log == [("create", "flota-torre"), ("write", profile_config), ("write", ACTIVE)]
    assert store[profile_config]["terminal"] == {
        "backend": "ssh", "ssh_host": "10.0.0.5", "ssh_user": "carol", "ssh_port": 2222,
        "ssh_key": "~/.ssh/id_torre"}
    assert store[profile_config]["model"] == START["model"]
    assert machines(store[ACTIVE])["torre"] == {
        "profile": "flota-torre", "host": "10.0.0.5", "user": "carol", "key": "~/.ssh/id_torre", "port": 2222,
        "description": "GPU tower", "wake": "wol", "mac": "aa:bb:cc:dd:ee:ff", "broadcast": "10.0.0.255"}
    out = capsys.readouterr().out
    assert "Created profile 'flota-torre'" in out and "carol@10.0.0.5:2222" in out
    assert "hermes config set delegation.backend flota" in out and "/flota" in out
    assert "Plugins > Flota" in out
    assert len(store.detected) == 1  # no --sleep: panel default suspends, so it is checked


def test_setup_copies_legacy_machines_into_the_empty_panel_and_keeps_the_old_block(store, capsys):
    assert main(*SETUP) == 0
    assert machines(store[ACTIVE])["mini"] == {"profile": "flota-mini", "host": "mini.lan"}
    assert legacy(store[ACTIVE]) == legacy(START)
    assert "Copied 1 machine(s)" in capsys.readouterr().out


def test_setup_does_not_copy_legacy_machines_when_the_panel_has_some(store, capsys):
    store[ACTIVE]["plugins"] = {"entries": {"flota": {"settings": {"auto_sleep": False, "machines": {
        "lap": {"profile": "flota-lap", "host": "lap.lan"}}}}}}
    assert main(*SETUP) == 0
    assert list(machines(store[ACTIVE])) == ["lap", "torre"]
    assert store[ACTIVE]["plugins"]["entries"]["flota"]["settings"]["auto_sleep"] is False
    assert "Copied" not in capsys.readouterr().out


def test_setup_sleep_flags_are_stored(store):
    assert main(*SETUP, "--sleep", "hibernate", "--sleep-after", "15m") == 0
    torre = machines(store[ACTIVE])["torre"]
    assert (torre["sleep"], torre["sleep_after"]) == ("hibernate", "15m")
    assert "power_prefix" not in torre
    [machine] = store.detected
    assert (machine.host, machine.user, machine.key, machine.port) == ("10.0.0.5", "carol", "~/.ssh/id_torre", 2222)


def test_setup_rejects_a_bad_duration(store, capsys):
    with pytest.raises(SystemExit):
        main(*SETUP, "--sleep-after", "soon")
    assert store.log == []


@pytest.mark.parametrize("flag", ["--host=-oProxyCommand=touch /tmp/x", "--user=-oProxyCommand=touch /tmp/x"])
def test_setup_refuses_an_ssh_option_as_host_or_user(store, capsys, flag):
    argv = [a for a in SETUP]
    i = argv.index(flag.split("=")[0])
    del argv[i:i + 2]
    assert main(*argv, flag, "--no-detect") == 2
    assert store.log == []  # no profile whose terminal would hand it to ssh
    assert "must not start with '-'" in capsys.readouterr().err


def test_setup_stores_the_niri_prefix(store, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_prefix", lambda machine: (power.NIRI_PREFIX, "via the niri session"))
    assert main(*SETUP, "--sleep", "suspend") == 0
    assert machines(store[ACTIVE])["torre"]["power_prefix"] == power.NIRI_PREFIX
    assert "niri" in capsys.readouterr().out


def test_setup_drops_a_stale_prefix_when_systemctl_works_directly(store, monkeypatch):
    monkeypatch.setattr(cli, "_detect_prefix", lambda machine: (power.NIRI_PREFIX, "via the niri session"))
    assert main(*SETUP, "--sleep", "suspend") == 0
    monkeypatch.setattr(cli, "_detect_prefix", lambda machine: ("", "systemctl works directly"))
    assert main(*SETUP, "--sleep", "suspend") == 0
    assert "power_prefix" not in machines(store[ACTIVE])["torre"]


def test_setup_keeps_the_machine_and_prints_the_polkit_rule(store, monkeypatch, capsys):
    rule = "torre: polkit does not let carol suspend ... polkit.addRule(...)"
    monkeypatch.setattr(cli, "_detect_prefix", lambda machine: (None, rule))
    assert main(*SETUP, "--sleep", "suspend") == 0
    assert machines(store[ACTIVE])["torre"]["sleep"] == "suspend"
    out = capsys.readouterr().out
    assert rule in out and "auto-sleep will fail until" in out


def test_setup_unreachable_machine_is_kept_with_a_hint(store, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_prefix", lambda machine: (None, "unreachable: ssh timed out after 15s"))
    assert main(*SETUP, "--sleep", "suspend") == 0
    out = capsys.readouterr().out
    assert "ssh timed out" in out and "run this again" in out
    assert "torre" in machines(store[ACTIVE])


def test_setup_sleep_none_skips_the_check(store):
    assert main(*SETUP, "--sleep", "none") == 0
    assert store.detected == [] and machines(store[ACTIVE])["torre"]["sleep"] == "none"


def test_setup_without_sleep_flag_still_checks_how_to_sleep(store):
    assert main(*SETUP) == 0
    assert len(store.detected) == 1


def test_setup_poweroff_mentions_the_panel_switch(store, capsys):
    assert main(*SETUP, "--sleep", "poweroff") == 0
    assert "Allow power off" in capsys.readouterr().out


def test_wake_sleep_and_off_mirror_the_slash_command(store, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(power, "power", lambda machine, action: calls.append((machine.name, action))
                        or f"{machine.name}: {action} sent")
    monkeypatch.setattr(slash, "ensure_ready", lambda machine: 3.0)
    assert main("wake", "mini") == 0
    assert "mini is up (waited 3s)" in capsys.readouterr().out
    assert main("sleep", "mini") == 0
    assert "mini: suspend sent" in capsys.readouterr().out
    assert main("off", "mini") == 1
    assert ("mini has no wake method, so Flota would not be able to turn it back on; it can only be "
            "suspended.") in capsys.readouterr().err
    _wakeable_mini(store)
    store[ACTIVE]["plugins"] = {"entries": {"flota": {"settings": {"allow_power_off": False}}}}
    assert main("off", "mini") == 1
    assert "Powering off is disabled. Turn on \"Allow power off\"" in capsys.readouterr().err
    store[ACTIVE]["plugins"] = {}
    assert main("off", "mini") == 0
    assert "mini: poweroff sent" in capsys.readouterr().out
    assert calls == [("mini", "suspend"), ("mini", "poweroff")]


def _wakeable_mini(store):
    """Give the legacy-config machine 'mini' a wake method so it may be powered off."""
    store[ACTIVE]["delegation"]["backends"]["flota"]["machines"]["mini"].update(
        wake="command", wake_command="smartplug on")


def _fake_power(monkeypatch, line="{name}: {action} sent"):
    calls = []
    monkeypatch.setattr(power, "power", lambda machine, action: calls.append((machine.name, action))
                        or line.format(name=machine.name, action=action))
    return calls


@pytest.mark.parametrize("command", ["dry-run", "wake", "sleep", "off"])
def test_an_unknown_machine_exits_2(store, capsys, command):
    assert main(command, "nope") == 2
    assert "No machine named 'nope'" in capsys.readouterr().err


def test_a_failed_wake_exits_1(store, monkeypatch, capsys):
    def ensure(machine):
        raise slash.WakeError("mini did not come up within 90s")
    monkeypatch.setattr(slash, "ensure_ready", ensure)
    assert main("wake", "mini") == 1
    assert "did not come up" in capsys.readouterr().err


def test_a_failed_sleep_exits_1(store, monkeypatch, capsys):
    _fake_power(monkeypatch, "{name}: {action} failed: exit 1: denied")
    assert main("sleep", "mini") == 1
    assert "denied" in capsys.readouterr().err


def test_an_unreadable_config_exits_1(store, monkeypatch, capsys):
    def broken():
        raise RuntimeError("config.yaml is not valid YAML")
    monkeypatch.setattr(slash, "_load_config", broken)
    assert main("sleep", "mini") == 1
    assert "not valid YAML" in capsys.readouterr().err


@pytest.mark.parametrize("command, verb", [("sleep", "sleep it"), ("off", "power it off")])
def test_a_busy_machine_is_refused_without_force(store, monkeypatch, capsys, command, verb):
    _wakeable_mini(store)
    calls = _fake_power(monkeypatch)
    monkeypatch.setattr(cli, "_read_busy", lambda: {"mini": 2})
    assert main(command, "mini") == 1
    assert (f"mini is busy with 2 subagent(s); add --force to {verb} anyway"
            in capsys.readouterr().err)
    assert calls == []
    assert main(command, "mini", "--force") == 0
    assert len(calls) == 1


def test_an_idle_machine_sleeps_when_the_gateway_is_known(store, monkeypatch):
    calls = _fake_power(monkeypatch)
    monkeypatch.setattr(cli, "_read_busy", lambda: {"other": 1})
    assert main("sleep", "mini") == 0 and calls == [("mini", "suspend")]


def test_status_shows_busy_counts_when_the_gateway_is_known(store, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_read_busy", lambda: {"mini": 2})
    assert main("status") == 0
    assert "· busy 2" in capsys.readouterr().out
    monkeypatch.setattr(cli, "_read_busy", lambda: {})
    main("status")
    assert "· busy 0" in capsys.readouterr().out
    monkeypatch.setattr(cli, "_read_busy", lambda: None)
    main("status")
    assert "busy" not in capsys.readouterr().out


def test_setup_leaves_other_keys_and_machines_alone(store):
    assert main(*SETUP) == 0
    active = store[ACTIVE]
    assert active["model"] == START["model"] and active["delegation"] == START["delegation"]


def test_setup_is_idempotent(store, capsys):
    assert main(*SETUP) == 0
    first = copy.deepcopy(dict(store))
    capsys.readouterr()
    assert main(*SETUP) == 0
    assert dict(store) == first
    assert store.log.count(("create", "flota-torre")) == 1
    assert "already exists" in capsys.readouterr().out


def test_setup_minimal_on_an_empty_config(store):
    store[ACTIVE] = {}
    assert main("setup", "nas", "--host", "nas.lan", "--user", "admin") == 0
    assert store[ACTIVE] == {"plugins": {"entries": {"flota": {"settings": {"machines": {
        "nas": {"profile": "flota-nas", "host": "nas.lan", "user": "admin", "port": 22, "wake": "none"}}}}}}}
    assert store[PROFILES / "flota-nas" / "config.yaml"]["terminal"] == {
        "backend": "ssh", "ssh_host": "nas.lan", "ssh_user": "admin", "ssh_port": 22}


def test_setup_again_updates_and_keeps_hand_written_keys(store):
    assert main(*SETUP) == 0
    machines(store[ACTIVE])["torre"].update(sleep_after="10m", sleep_command="systemctl suspend")
    assert main("setup", "torre", "--host", "10.0.0.9", "--user", "root") == 0
    torre = machines(store[ACTIVE])["torre"]
    assert torre["host"] == "10.0.0.9" and torre["sleep_command"] == "systemctl suspend"
    assert torre["wake"] == "wol" and torre["port"] == 2222  # not given this time: kept
    terminal = store[PROFILES / "flota-torre" / "config.yaml"]["terminal"]
    assert (terminal["ssh_host"], terminal["ssh_user"], terminal["ssh_port"]) == ("10.0.0.9", "root", 2222)
    assert terminal["ssh_key"] == "~/.ssh/id_torre"


def test_setup_accepts_a_relay_once_via_is_in_the_config(store):
    legacy(store[ACTIVE])["nas"] = {"via": "pi@relay"}
    assert main("setup", "nas", "--host", "nas.lan", "--user", "admin", "--wake", "wol_via",
                "--mac", "aa:bb:cc:dd:ee:ff") == 0
    assert machines(store[ACTIVE])["nas"]["via"] == "pi@relay"


@pytest.mark.parametrize("extra, reason", [
    (("--wake", "wol"), "needs a valid 'mac'"),
    (("--wake", "wol", "--mac", "nope"), "needs a valid 'mac'"),
    (("--wake", "wol_via", "--mac", "aa:bb:cc:dd:ee:ff"), "needs 'via'"),
    (("--wake", "command"), "needs 'wake_command'"),
    (("--port", "70000"), "port must be"),
    (("--host", " "), "non-empty 'profile' and 'host'"),
])
def test_setup_refuses_an_invalid_machine_before_writing(store, capsys, extra, reason):
    assert main("setup", "torre", "--host", "10.0.0.5", "--user", "carol", *extra) == 2
    assert reason in capsys.readouterr().err
    assert store == {ACTIVE: START} and store.log == []


def test_setup_refuses_a_name_that_is_not_a_profile_id(store, capsys):
    assert main("setup", "Bad Name!", "--host", "h", "--user", "u") == 2
    assert "Invalid profile name" in capsys.readouterr().err
    assert store == {ACTIVE: START}


def test_setup_reports_a_refused_write(store, capsys, monkeypatch):
    def refuse(path, mutate):
        raise RuntimeError("managed install")

    monkeypatch.setattr(cli, "_update_config", refuse)
    assert main("setup", "nas", "--host", "nas.lan", "--user", "admin") == 1
    assert "managed install" in capsys.readouterr().err


def test_setup_with_real_core(tmp_path, monkeypatch, capsys):
    """The real seams: create_profile + atomic_config_write under a throwaway HOME / HERMES_HOME."""
    pytest.importorskip("ruamel.yaml", reason="Hermes core's YAML dependency is not importable here")
    import hermes_yaml

    home = tmp_path / "os-home" / ".hermes"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(cli, "_detect_prefix", lambda machine: ("", "systemctl works directly"))  # no real ssh
    monkeypatch.setattr(cli, "_is_up", lambda machine: False)  # no real network probing either
    assert cli._active_config_path() == home / "config.yaml"  # never the developer's real ~/.hermes
    (home / "config.yaml").write_text(
        "# mine\nmodel:\n  default: some/model\n  provider: openrouter\n"
        "delegation:\n  backend: default  # keep\n  backends:\n    flota:\n      machines:\n"
        "        mini:\n          profile: flota-mini\n          host: mini.lan\n", encoding="utf-8")

    def read(path):
        return hermes_yaml.safe_load(path.read_text(encoding="utf-8"))

    assert main(*SETUP) == 0
    profile_config = home / "profiles" / "flota-torre" / "config.yaml"
    terminal = read(profile_config)["terminal"]
    assert {k: terminal[k] for k in ("backend", "ssh_host", "ssh_user", "ssh_port", "ssh_key")} == {
        "backend": "ssh", "ssh_host": "10.0.0.5", "ssh_user": "carol", "ssh_port": 2222,
        "ssh_key": "~/.ssh/id_torre"}
    assert read(profile_config)["model"] == {"default": "some/model", "provider": "openrouter"}
    active = read(home / "config.yaml")
    assert machines(active) == {
        "mini": {"profile": "flota-mini", "host": "mini.lan"},
        "torre": {"profile": "flota-torre", "host": "10.0.0.5", "user": "carol", "key": "~/.ssh/id_torre",
                  "port": 2222, "description": "GPU tower", "wake": "wol", "mac": "aa:bb:cc:dd:ee:ff",
                  "broadcast": "10.0.0.255"}}
    assert legacy(active) == {"mini": {"profile": "flota-mini", "host": "mini.lan"}}
    assert active["delegation"]["backend"] == "default"
    text = (home / "config.yaml").read_text(encoding="utf-8")
    assert "# mine" in text and "# keep" in text  # core's writer keeps comments

    before = [text, profile_config.read_text(encoding="utf-8")]
    capsys.readouterr()
    assert main(*SETUP) == 0
    assert "already exists" in capsys.readouterr().out
    assert [(home / "config.yaml").read_text(encoding="utf-8"), profile_config.read_text(encoding="utf-8")] == before
    assert main("status") == 0
    assert "torre" in capsys.readouterr().out


# --- detection during setup ----------------------------------------------------------------------

NET = {"interface": "enp8s0", "mac": "00:11:22:33:44:55", "broadcast": "198.51.100.255", "wol": "enabled",
       "connection": "Wired connection 1", "reason": ""}
MINIMAL = ("setup", "escritorio", "--host", "192.0.2.28", "--user", "alice")


@pytest.fixture
def reachable(store, monkeypatch):
    """The machine answers: OS and network detection return ``store.net`` (edit it per test)."""
    store.net, store.os = dict(NET), "linux"
    monkeypatch.setattr(cli, "_is_up", lambda machine: True)
    monkeypatch.setattr(cli, "_detect_os", lambda machine: store.probes.append("os") or store.os)
    monkeypatch.setattr(cli, "_detect_network",
                        lambda machine, os: store.probes.append(("net", machine.host, os)) or dict(store.net))
    return store


def test_minimal_setup_detects_os_mac_broadcast_and_picks_wol(reachable, capsys):
    assert main(*MINIMAL) == 0
    assert machines(reachable[ACTIVE])["escritorio"] == {
        "profile": "flota-escritorio", "host": "192.0.2.28", "user": "alice", "port": 22,
        "os": "linux", "wake": "wol", "mac": "00:11:22:33:44:55", "broadcast": "198.51.100.255"}
    assert reachable.probes == ["os", ("net", "192.0.2.28", "linux")]
    out = capsys.readouterr().out
    print(out)
    assert ("detected: linux · enp8s0 · mac 00:11:22:33:44:55 · broadcast 198.51.100.255 · "
            "wake-on-lan enabled") in out


def test_explicit_flags_win_over_detection(reachable):
    assert main(*MINIMAL, "--wake", "none", "--mac", "aa:bb:cc:dd:ee:ff", "--broadcast", "10.0.0.255") == 0
    entry = machines(reachable[ACTIVE])["escritorio"]
    assert (entry["wake"], entry["mac"], entry["broadcast"]) == ("none", "aa:bb:cc:dd:ee:ff", "10.0.0.255")


def test_explicit_wol_takes_the_detected_mac(reachable):
    assert main(*MINIMAL, "--wake", "wol") == 0
    assert machines(reachable[ACTIVE])["escritorio"]["mac"] == "00:11:22:33:44:55"


def test_disabled_wol_prints_how_to_enable_it(reachable, capsys):
    reachable.net["wol"] = "disabled"
    assert main(*MINIMAL) == 0
    out = capsys.readouterr().out
    print(out)
    assert "wake-on-lan disabled" in out and "as root: ethtool -s enp8s0 wol g" in out
    assert "802-3-ethernet.wake-on-lan magic" in out and "BIOS" in out
    assert machines(reachable[ACTIVE])["escritorio"]["wake"] == "wol"  # the MAC is right; only the NIC needs a fix


def test_reachable_but_no_mac_keeps_wake_none(reachable, capsys):
    reachable.net = {"interface": "", "mac": "", "broadcast": "", "wol": "unknown", "connection": "",
                     "reason": "no default route found"}
    reachable.os = ""
    assert main(*MINIMAL) == 0
    entry = machines(reachable[ACTIVE])["escritorio"]
    assert entry["wake"] == "none" and "os" not in entry and "mac" not in entry
    assert "no default route found" in capsys.readouterr().out


def test_unreachable_machine_is_saved_and_detection_skipped(store, capsys):
    assert main(*MINIMAL) == 0
    assert store.probes == ["tcp"]
    entry = machines(store[ACTIVE])["escritorio"]
    assert entry["wake"] == "none" and "os" not in entry
    assert "Detection skipped" in capsys.readouterr().out


def test_no_detect_probes_nothing(reachable, capsys):
    assert main(*MINIMAL, "--no-detect", "--sleep", "none") == 0
    assert reachable.probes == [] and reachable.detected == []
    assert machines(reachable[ACTIVE])["escritorio"]["wake"] == "none"


def test_os_detection_missing_from_power_is_treated_as_unknown(store, monkeypatch):
    monkeypatch.setattr(cli, "_is_up", lambda machine: True)
    monkeypatch.delattr(power, "detect_os", raising=False)
    monkeypatch.setattr(cli, "_detect_os", cli._real_detect_os)
    monkeypatch.setattr(cli, "_detect_network", lambda machine, os: dict(NET))
    assert main(*MINIMAL) == 0
    entry = machines(store[ACTIVE])["escritorio"]
    assert "os" not in entry and entry["wake"] == "wol"


def test_setup_with_detection_is_idempotent(reachable, capsys):
    assert main(*MINIMAL) == 0
    first = copy.deepcopy(dict(reachable))
    assert main(*MINIMAL) == 0
    assert dict(reachable) == first


def test_rerun_keeps_a_relay_wake_method(reachable):
    assert main(*MINIMAL) == 0
    machines(reachable[ACTIVE])["escritorio"].update(wake="wol_via", via="pi@relay")
    assert main(*MINIMAL) == 0
    assert machines(reachable[ACTIVE])["escritorio"]["wake"] == "wol_via"


# --- doctor --------------------------------------------------------------------------------------

@pytest.fixture
def doctor(reachable, monkeypatch):
    """escritorio set up and healthy; tests break one thing at a time."""
    assert main(*MINIMAL) == 0
    reachable.ssh = (True, "")
    reachable.lan = ["198.51.100.30/24"]
    monkeypatch.setattr(cli, "_ssh_ok", lambda machine: reachable.ssh)
    monkeypatch.setattr(cli, "_local_networks",
                        lambda: [ipaddress.ip_interface(n) for n in reachable.lan])
    monkeypatch.setattr(cli, "_file_sync_guarded", lambda: True)
    return reachable


def test_doctor_says_file_sync_is_off(doctor, capsys):
    code, out = run_doctor(capsys, "escritorio")
    assert code == 0 and "file sync off" in out


def test_doctor_fails_when_the_ssh_file_sync_guard_is_missing(doctor, monkeypatch, capsys):
    # Without the guard core tars the machine's whole ~/.hermes back after every subagent.
    monkeypatch.setattr(cli, "_file_sync_guarded", lambda: False)
    code, out = run_doctor(capsys, "escritorio")
    assert code == 1
    assert "✗ SSH file sync" in out and "~/.hermes" in out


def run_doctor(capsys, *names):
    capsys.readouterr()
    code = main("doctor", *names)
    out = capsys.readouterr().out
    print(out)
    return code, out


def test_doctor_all_good(doctor, capsys):
    code, out = run_doctor(capsys, "escritorio")
    assert code == 0 and "✗" not in out
    for text in ("SSH", "flota-escritorio", "wol", "Wake-on-LAN enabled on enp8s0", "sleep", "198.51.100.0/24"):
        assert text in out


def test_doctor_checks_every_machine_without_an_argument(doctor, capsys):
    code, out = run_doctor(capsys)
    assert "escritorio" in out and "mini" in out


def test_doctor_wol_disabled_fails_with_the_fix(doctor, capsys):
    doctor.net["wol"] = "disabled"
    code, out = run_doctor(capsys, "escritorio")
    assert code == 1 and "✗ Wake-on-LAN disabled on enp8s0" in out and "as root: ethtool -s enp8s0 wol g" in out


def test_doctor_notes_that_power_off_needs_wol_from_s5(doctor, capsys):
    code, out = run_doctor(capsys, "escritorio")
    assert code == 0 and "? power off: waking from power-off (S5) needs Wake-on-LAN from S5 enabled in the BIOS" in out


def test_doctor_unknown_wol_is_not_a_failure(doctor, capsys):
    doctor.net["wol"] = "unknown"
    code, out = run_doctor(capsys, "escritorio")
    assert code == 0 and "? Wake-on-LAN" in out


def test_doctor_unreachable_machine(doctor, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_is_up", lambda machine: False)
    code, out = run_doctor(capsys, "escritorio")
    assert code == 1 and "✗ SSH" in out and "hermes flota wake escritorio" in out
    assert "? Wake-on-LAN" in out and "? sleep" in out


def test_doctor_ssh_login_refused(doctor, capsys):
    doctor.ssh = (False, "Permission denied (publickey).")
    code, out = run_doctor(capsys, "escritorio")
    assert code == 1 and "Permission denied" in out and "ssh-copy-id alice@192.0.2.28" in out


def test_doctor_other_lan_suggests_a_relay(doctor, capsys):
    doctor.lan = ["10.8.0.2/24"]
    code, out = run_doctor(capsys, "escritorio")
    assert code == 1 and "wol_via" in out


def test_doctor_profile_pointing_elsewhere(doctor, capsys):
    doctor[PROFILES / "flota-escritorio" / "config.yaml"]["terminal"]["ssh_host"] = "old.lan"
    code, out = run_doctor(capsys, "escritorio")
    assert code == 1 and "old.lan" in out and "hermes flota setup escritorio" in out


def test_doctor_wake_none_and_sleep_check(doctor, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_detect_prefix", lambda machine: (None, "torre: polkit does not let ..."))
    machines(doctor[ACTIVE])["escritorio"]["wake"] = "none"
    code, out = run_doctor(capsys, "escritorio")
    assert code == 1 and "✗ no wake method" in out and "✗ sleep" in out and "polkit" in out


def test_doctor_sleep_none_skips_the_sleep_check(doctor, capsys):
    machines(doctor[ACTIVE])["escritorio"]["sleep"] = "none"
    doctor.detected.clear()
    code, out = run_doctor(capsys, "escritorio")
    assert code == 0 and doctor.detected == []


def test_doctor_unknown_machine(doctor, capsys):
    assert main("doctor", "nope") == 2
    assert "nope" in capsys.readouterr().err


def test_run_setup_returns_the_lines_and_takes_any_object(store, capsys):
    from types import SimpleNamespace
    seen = []
    code, lines = cli.run_setup(SimpleNamespace(name="torre", host="10.0.0.5", user="carol"),
                                emit=lambda text, err: seen.append((text, err)))
    assert code == 0 and capsys.readouterr().out == ""
    assert lines == [t for t, _ in seen] and lines[1] == "Created profile 'flota-torre' from the active profile's config."
    assert any(line.startswith("Saved machine 'torre'") for line in lines)
    code, lines = cli.run_setup(SimpleNamespace(name="torre", host="10.0.0.5", user="carol", wake="wol"))
    assert code == 2 and lines[-1] == "Nothing was written."


def test_setup_extra_keys_and_os_flag(store):
    from types import SimpleNamespace
    cli.run_setup(SimpleNamespace(name="torre", host="h", user="u", os="macos", extra={"crew_names": ["Lia"]}))
    entry = machines(store[ACTIVE])["torre"]
    assert entry["os"] == "macos" and entry["crew_names"] == ["Lia"]
