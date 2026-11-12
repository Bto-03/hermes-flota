"""First-run behaviour of ``hermes flota``: input is validated before anything is written, every
command ends with a next step, and a machine can be removed again. Same in-memory seams as test_cli."""
import copy
import importlib.util
from pathlib import Path

import pytest

from flota_plugin import cli, machines as machines_mod

_spec = importlib.util.spec_from_file_location("flota_test_cli_helpers", Path(__file__).with_name("test_cli.py"))
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
ACTIVE, PROFILES, START, main, machines, store = (
    _helpers.ACTIVE, _helpers.PROFILES, _helpers.START, _helpers.main, _helpers.machines, _helpers.store)


@pytest.fixture
def down(store, monkeypatch):  # noqa: F811
    monkeypatch.setattr(cli, "_is_up", lambda machine: store.probes.append("tcp") or False)
    return store


# --- setup: validate first, write nothing on bad input ------------------------------------------------

@pytest.mark.parametrize("argv, reason", [
    (("setup", "bad", "--host", "not a host!", "--user", "alice"), "'host' is not a hostname or IP address"),
    (("setup", "bad", "--host", "10.0.0.5; curl x | sh", "--user", "alice"), "'host' is not a hostname"),
    (("setup", "bad", "--host", "10.0.0.5", "--user", 'x" || true || "'), "'user' is not a login name"),
    (("setup", "bad", "--host", "10.0.0.5", "--user", "alice", "--key", "~/.ssh/nope"), "no such key file"),
    (("setup", "!!!", "--host", "10.0.0.5", "--user", "alice"), "not a usable name"),
    (("setup", "bad", "--user", "alice"), "needs a host"),
    (("setup", "bad", "--host", "10.0.0.5"), "needs the SSH user"),
])
def test_setup_refuses_bad_input_and_writes_nothing(store, monkeypatch, capsys, argv, reason):  # noqa: F811
    monkeypatch.setattr(cli, "_key_is_file", lambda path: False)
    assert main(*argv) == 2
    err = capsys.readouterr().err
    assert reason in err and "Nothing was written" in err
    assert store == {ACTIVE: START} and store.log == []


def test_setup_user_at_host_shorthand_names_the_machine_after_the_host(store, capsys):  # noqa: F811
    assert main("setup", "alice@192.0.2.20") == 0
    entry = machines(store[ACTIVE])["192-0-2-20"]
    assert (entry["host"], entry["user"], entry["profile"]) == ("192.0.2.20", "alice", "flota-192-0-2-20")
    assert "Name: '192-0-2-20'" in capsys.readouterr().out
    assert main("setup", "bob@Torre.local", "--no-detect") == 0
    assert machines(store[ACTIVE])["torre"]["user"] == "bob"
    assert main("setup", "nas", "--host", "carol@nas.lan", "--no-detect") == 0
    assert (machines(store[ACTIVE])["nas"]["host"], machines(store[ACTIVE])["nas"]["user"]) == ("nas.lan", "carol")


def test_setup_slugs_the_name_and_says_so(store, capsys):  # noqa: F811
    assert main("setup", "My Desk", "--host", "10.0.0.5", "--user", "alice") == 0
    assert "my-desk" in machines(store[ACTIVE]) and (PROFILES / "flota-my-desk" / "config.yaml") in store
    assert "saved as 'my-desk'" in capsys.readouterr().out


def test_setup_keeps_the_exact_name_of_a_machine_that_already_exists(store):  # noqa: F811
    store[ACTIVE]["plugins"] = {"entries": {"flota": {"settings": {"machines": {
        "Desk2": {"profile": "flota-desk2", "host": "10.0.0.6"}}}}}}
    store[PROFILES / "flota-desk2" / "config.yaml"] = {}
    assert main("setup", "Desk2", "--host", "10.0.0.7", "--user", "alice") == 0
    assert list(machines(store[ACTIVE])) == ["Desk2"] and machines(store[ACTIVE])["Desk2"]["host"] == "10.0.0.7"


def test_setup_on_a_machine_that_does_not_answer_says_not_verified_and_skips_the_sleep_check(down, capsys):
    assert main("setup", "desk", "--host", "192.0.2.20", "--user", "alice") == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out[-2] == ("⚠ Saved 'desk' but NOT verified: 192.0.2.20:22 is not answering (it is off, or the "
                       "address is wrong).")
    assert out[-1] == ("Next: turn it on and run the same command again; `hermes flota remove desk` undoes it.")
    assert not any(line.startswith("Saved machine") for line in out)
    assert down.detected == []  # no second SSH wait after the port probe already failed
    assert "desk" in machines(down[ACTIVE])


def test_setup_next_step_depends_on_the_backend(store, capsys):  # noqa: F811
    store[ACTIVE]["delegation"]["backend"] = "flota"
    assert main("setup", "desk", "--host", "192.0.2.20", "--user", "alice") == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("Next: `hermes flota doctor desk`") and "config set" not in last


# --- machines: host and user are checked wherever they come from ---------------------------------------

@pytest.mark.parametrize("entry, ok", [
    ({"host": "10.0.0.5"}, True), ({"host": "nas.lan", "user": "carol"}, True),
    ({"host": "fe80::1%eth0", "user": "DOMAIN\\carol"}, True),
    ({"host": "10.0.0.5; rm -rf x"}, False), ({"host": "h", "user": "a b"}, False),
    ({"host": "h", "user": 'x" || true || "'}, False), ({"host": "h\nx"}, False),
])
def test_load_machines_checks_host_and_user(entry, ok):
    loaded, problems = machines_mod.load_machines({"machines": {"pc": {"profile": "flota-pc", **entry}}})
    assert ("pc" in loaded) is ok and bool(problems) is not ok


# --- remove ---------------------------------------------------------------------------------------------

@pytest.fixture
def fleet2(store, monkeypatch):  # noqa: F811
    assert main("setup", "desk", "--host", "192.0.2.20", "--user", "alice") == 0
    assert main("setup", "nas", "--host", "192.0.2.21", "--user", "alice") == 0
    store.made, store.deleted = {"flota-desk", "flota-nas"}, []
    monkeypatch.setattr(cli, "_flota_made", lambda profile: profile in store.made)
    monkeypatch.setattr(cli, "_delete_profile",
                        lambda profile: store.deleted.append(profile) or store.pop(PROFILES / profile / "config.yaml"))
    return store


def test_remove_drops_the_machine_and_the_profile_flota_made(fleet2, capsys):
    capsys.readouterr()
    assert main("remove", "desk") == 0
    assert list(machines(fleet2[ACTIVE])) == ["mini", "nas"] and fleet2.deleted == ["flota-desk"]
    out = capsys.readouterr().out
    assert "Removed machine 'desk'" in out and "Deleted its profile 'flota-desk'" in out
    assert out.strip().splitlines()[-1].startswith("Next:")


def test_remove_keeps_a_profile_flota_did_not_make_or_another_machine_uses(fleet2, capsys):
    fleet2.made = {"flota-nas"}
    assert main("remove", "desk") == 0
    assert fleet2.deleted == [] and "Kept profile 'flota-desk'" in capsys.readouterr().out
    machines(fleet2[ACTIVE])["twin"] = {"profile": "flota-nas", "host": "192.0.2.22"}
    assert main("remove", "nas") == 0 and fleet2.deleted == []
    assert main("remove", "twin", "--keep-profile") == 0 and fleet2.deleted == []


def test_remove_unknown_exits_2_and_busy_needs_force(fleet2, monkeypatch, capsys):
    before = copy.deepcopy(dict(fleet2))
    assert main("remove", "nope") == 2
    assert "no machine named 'nope'" in capsys.readouterr().err
    monkeypatch.setattr(cli, "_read_busy", lambda: {"desk": 2})
    assert main("remove", "desk") == 1
    assert "busy with 2 subagent(s)" in capsys.readouterr().err and "--force" not in capsys.readouterr().err
    assert dict(fleet2) == before and fleet2.deleted == []
    assert main("remove", "desk", "--force") == 0 and "desk" not in machines(fleet2[ACTIVE])


def test_remove_a_machine_that_only_lives_in_the_old_block(store, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(cli, "_flota_made", lambda profile: False)
    assert main("remove", "mini") == 0
    assert store[ACTIVE]["delegation"]["backends"]["flota"]["machines"] == {}


# --- guide, hints, help ---------------------------------------------------------------------------------

def test_bare_command_guides_to_the_next_step(store, capsys):  # noqa: F811
    store[ACTIVE] = {}
    assert main() == 0
    out = capsys.readouterr().out
    assert "No machines yet" in out and "hermes flota setup USER@HOST" in out and "hermes flota invite" in out
    store[ACTIVE] = copy.deepcopy(START)
    assert main() == 0
    out = capsys.readouterr().out
    assert "1 machine(s): mini" in out
    assert out.strip().splitlines()[-1] == "Next: hermes config set delegation.backend flota   (then: hermes gateway restart)"
    store[ACTIVE]["delegation"]["backend"] = "flota"
    assert main() == 0
    assert capsys.readouterr().out.strip().splitlines()[-1].startswith("Next: hermes flota doctor")
    store[ACTIVE] = {"plugins": {"entries": {"flota": {"settings": {"role": "ship", "joined_fleets": [
        {"flagship": "Torre"}]}}}}}
    assert main() == 0
    assert "a ship in the fleet of Torre" in capsys.readouterr().out
    assert main("status") == 0
    out = capsys.readouterr().out
    assert "a ship in the fleet of Torre" in out and "hermes flota setup" not in out


def test_every_subcommand_has_a_description(store):  # noqa: F811
    import argparse
    parser = argparse.ArgumentParser(prog="hermes flota")
    cli.setup_parser(parser)
    [sub] = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    assert {"remove", "uninstall"} <= set(sub.choices)
    assert all(command.description for command in sub.choices.values())


def test_unknown_machine_gets_the_cli_usage_not_the_slash_one(store, capsys):  # noqa: F811
    assert main("dry-run", "MINI") == 0  # names match in any case when that is unambiguous
    capsys.readouterr()
    assert main("wake", "nope") == 2
    err = capsys.readouterr().err
    assert "No machine named 'nope'" in err and "Machines: mini" in err and "/flota" not in err


def test_doctor_reports_a_missing_key_and_quotes_its_hints(store, monkeypatch, capsys):  # noqa: F811
    store[ACTIVE]["plugins"] = {"entries": {"flota": {"settings": {"machines": {
        "pc": {"profile": "flota-pc", "host": "10.0.0.5", "user": "alice", "key": "~/my keys/id"}}}}}}
    monkeypatch.setattr(cli, "_key_is_file", lambda path: False)
    monkeypatch.setattr(cli, "_is_up", lambda machine: False)
    monkeypatch.setattr(cli, "_core_reads_sync_key", lambda: True)
    assert main("doctor", "pc") == 1
    out = capsys.readouterr().out
    assert "✗ SSH key ~/my keys/id is missing" in out
    assert "hermes flota setup pc --host 10.0.0.5 --user alice --key '~/my keys/id'" in out


def test_role_says_what_to_do_next(store, capsys):  # noqa: F811
    assert main("role") == 0
    out = capsys.readouterr().out
    assert "Fleet: leads its own (1 ship)" in out and "Next:" in out
    assert main("role", "ship") == 1
    assert "hermes flota remove <machine>" in capsys.readouterr().err
    store[ACTIVE] = {}
    assert main("role", "ship") == 0
    assert "Next: `hermes flota join <invite>`" in capsys.readouterr().out
    store[ACTIVE] = {"plugins": {"entries": {"flota": {"settings": {"role": "flagship", "machines": {
        "pc": {"profile": "flota-pc", "host": "10.0.0.5"}}}}}}}
    assert main("role", "none") == 0
    assert "flagship (inferred" in capsys.readouterr().out
