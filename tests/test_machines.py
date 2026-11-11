import dataclasses

import pytest

from flota_plugin import machines
from flota_plugin.machines import Machine, load_machines, parse_duration


@pytest.mark.parametrize("value, expected", [
    (90, 90.0),
    (1.5, 1.5),
    (0, 0.0),
    ("90", 90.0),
    ("90s", 90.0),
    ("5m", 300.0),
    ("1h", 3600.0),
    ("1.5m", 90.0),
    (" 5M ", 300.0),
    ("1H", 3600.0),
])
def test_parse_duration_accepts_seconds_and_unit_suffixes(value, expected):
    result = parse_duration(value, 7.0)
    assert result == expected
    assert isinstance(result, float)


@pytest.mark.parametrize("value", [
    None, True, False, -1, -0.5, "-5", "-5m", "", "soon", "5d", "m", [], {},
    float("nan"), float("inf"), "nan", "inf",
])
def test_parse_duration_falls_back_to_default(value):
    assert parse_duration(value, 7.0) == 7.0


def test_machine_fields_order_and_defaults():
    fields = [(f.name, f.default) for f in dataclasses.fields(Machine)]
    assert fields == [
        ("name", dataclasses.MISSING), ("profile", dataclasses.MISSING),
        ("host", dataclasses.MISSING), ("port", 22), ("description", ""), ("wake", "none"),
        ("mac", ""), ("broadcast", "255.255.255.255"), ("via", ""), ("wake_command", ""),
        ("wake_timeout", 90.0), ("fallback", ""), ("sleep_after", 0.0), ("sleep_command", ""),
        ("user", ""), ("key", ""), ("sleep", "none"), ("power_prefix", ""),
        ("os", "linux"), ("wake_timer", False), ("timer_lead", 120.0),
    ]


def test_machine_is_frozen():
    machine = Machine("a", "p", "h")
    with pytest.raises(dataclasses.FrozenInstanceError):
        machine.host = "other"


def test_wake_methods():
    assert machines.WAKE_METHODS == ("none", "wol", "wol_via", "command", "timer")


def test_sleep_methods():
    assert machines.SLEEP_METHODS == ("none", "suspend", "hibernate", "poweroff")


def load(entries):
    return load_machines({"machines": entries})


def only_problem(problems, name):
    assert len(problems) == 1, problems
    assert name in problems[0]
    return problems[0]


def test_spec_example():
    loaded, problems = load({
        "desktop": {
            "description": "16 GB GPU", "profile": "flota-desktop", "host": "desk-pc",
            "wake": "wol", "mac": "00:11:22:33:44:55", "broadcast": "198.51.100.255",
            "wake_timeout": "90s", "fallback": "server", "sleep_after": "10m",
            "sleep_command": "ssh desktop systemctl suspend",
        },
        "server": {"profile": "flota-server", "host": "servidor"},
    })
    assert problems == []
    assert list(loaded) == ["desktop", "server"]
    assert loaded["desktop"] == Machine(
        name="desktop", profile="flota-desktop", host="desk-pc", port=22,
        description="16 GB GPU", wake="wol", mac="00:11:22:33:44:55",
        broadcast="198.51.100.255", via="", wake_command="", wake_timeout=90.0,
        fallback="server", sleep_after=600.0, sleep_command="ssh desktop systemctl suspend")
    assert loaded["server"] == Machine(name="server", profile="flota-server", host="servidor")


@pytest.mark.parametrize("settings", [{}, {"machines": None}])
def test_no_machines_is_empty_without_problems(settings):
    assert load_machines(settings) == ({}, [])


@pytest.mark.parametrize("value", [["a"], "desktop", 3])
def test_machines_not_a_mapping_is_one_problem(value):
    loaded, problems = load_machines({"machines": value})
    assert loaded == {}
    assert len(problems) == 1


OK = {"profile": "p", "host": "h"}


@pytest.mark.parametrize("entry", [
    None,
    "host",
    ["p", "h"],
    {"host": "h"},
    {"profile": "p"},
    {"profile": "", "host": "h"},
    {"profile": "   ", "host": "h"},
    {"profile": "p", "host": " "},
    {"profile": 5, "host": "h"},
    {"profile": "p", "host": 5},
    {**OK, "wake": "magic"},
    {**OK, "wake": 5},
    {**OK, "wake": "wol"},
    {**OK, "wake": "wol", "mac": "00:11:22:33:44"},
    {**OK, "wake": "wol", "mac": "00:11:22:33:44:zz"},
    {**OK, "wake": "wol", "mac": "00:11:22:33:44:55:11"},
    {**OK, "wake": "wol", "mac": "00.11.22.33.44.55"},
    {**OK, "wake": "wol", "mac": 47161220},
    {**OK, "wake": "wol_via", "via": "server"},
    {**OK, "wake": "wol_via", "mac": "00:11:22:33:44:55"},
    {**OK, "wake": "wol_via", "mac": "00:11:22:33:44:55", "via": " "},
    {**OK, "wake": "command"},
    {**OK, "wake": "command", "wake_command": "  "},
    {**OK, "host": "-oProxyCommand=touch /tmp/x"},
    {**OK, "user": "-oProxyCommand=touch /tmp/x"},
])
def test_invalid_entry_is_skipped_with_one_problem(entry):
    loaded, problems = load({"bad-box": entry, "good": OK})
    assert list(loaded) == ["good"]
    only_problem(problems, "bad-box")


@pytest.mark.parametrize("mac", ["00:11:22:33:44:55", "00-11-22-33-44-55", "001122334455"])
def test_wol_accepts_mac_separators(mac):
    loaded, problems = load({"a": {**OK, "wake": "wol", "mac": f" {mac} "}})
    assert problems == []
    assert loaded["a"].wake == "wol"
    assert loaded["a"].mac == mac


def test_wol_via_and_command_load_when_complete():
    loaded, problems = load({
        "a": {**OK, "wake": "wol_via", "mac": "001122334455", "via": " server "},
        "b": {**OK, "wake": "command", "wake_command": " wake-it "},
    })
    assert problems == []
    assert (loaded["a"].wake, loaded["a"].via) == ("wol_via", "server")
    assert (loaded["b"].wake, loaded["b"].wake_command) == ("command", "wake-it")


def test_port_is_read():
    loaded, problems = load({"a": {**OK, "port": 2222}})
    assert problems == []
    assert loaded["a"].port == 2222


@pytest.mark.parametrize("port", [0, -1, 65536, "22", 22.0, True])
def test_bad_port_is_a_problem_and_defaults_to_22(port):
    loaded, problems = load({"box": {**OK, "port": port}})
    assert loaded["box"].port == 22
    only_problem(problems, "box")


def test_durations_are_parsed():
    loaded, problems = load({"a": {**OK, "wake_timeout": "2m", "sleep_after": 30,
                                   "sleep_command": "zzz"}})
    assert problems == []
    assert (loaded["a"].wake_timeout, loaded["a"].sleep_after) == (120.0, 30.0)


def test_unparsable_durations_use_defaults():
    loaded, problems = load({"a": {**OK, "wake_timeout": "soon", "sleep_after": "later",
                                   "sleep_command": "zzz"}})
    assert problems == []
    assert (loaded["a"].wake_timeout, loaded["a"].sleep_after) == (90.0, 0.0)


def test_sleep_after_without_sleep_command_is_a_problem_and_disabled():
    loaded, problems = load({"box": {**OK, "sleep_after": "10m"}})
    assert loaded["box"].sleep_after == 0.0
    only_problem(problems, "box")


@pytest.mark.parametrize("fallback", ["ghost", "box", "skipped"])
def test_bad_fallback_is_a_problem_and_cleared(fallback):
    loaded, problems = load({"box": {**OK, "fallback": fallback}, "skipped": {"host": "h"}})
    assert loaded["box"].fallback == ""
    assert sum("box" in p for p in problems) == 1
    assert len(problems) == 2  # the other one is the skipped entry itself


def test_fallback_may_name_a_later_machine():
    loaded, problems = load({"a": {**OK, "fallback": " b "}, "b": OK})
    assert problems == []
    assert loaded["a"].fallback == "b"


def test_description_is_collapsed_to_one_line():
    loaded, _ = load({"a": {**OK, "description": "  16 GB\n   GPU\tbox  "}})
    assert loaded["a"].description == "16 GB GPU box"


def test_strings_are_stripped():
    loaded, problems = load({"a": {
        "profile": " p ", "host": " h ", "wake": " none ", "broadcast": " 10.0.0.255 ",
        "sleep_after": 5, "sleep_command": " zzz "}})
    assert problems == []
    assert loaded["a"] == Machine(name="a", profile="p", host="h", broadcast="10.0.0.255",
                                  sleep_after=5.0, sleep_command="zzz")


def test_null_optional_fields_use_defaults():
    loaded, problems = load({"a": {**OK, "wake": None, "port": None, "broadcast": None,
                                   "description": None, "fallback": None}})
    assert problems == []
    assert loaded["a"] == Machine(name="a", profile="p", host="h")


def test_settings_that_are_not_a_mapping_do_not_raise():
    assert load_machines(None) == ({}, [])


def test_power_fields_are_read_and_stripped():
    loaded, problems = load({"a": {**OK, "user": " alice ", "key": " ~/.ssh/flota ",
                                   "sleep": " hibernate ", "power_prefix": " run-in "}})
    assert problems == []
    m = loaded["a"]
    assert (m.user, m.key, m.sleep, m.power_prefix) == (
        "alice", "~/.ssh/flota", "hibernate", "run-in")


@pytest.mark.parametrize("sleep", ["nap", "Suspend", 5, True])
def test_bad_sleep_is_a_problem_and_falls_back_to_none(sleep):
    loaded, problems = load({"box": {**OK, "sleep": sleep}})
    assert loaded["box"].sleep == "none"
    only_problem(problems, "box")


def test_null_sleep_is_none():
    loaded, problems = load({"a": {**OK, "sleep": None}})
    assert problems == [] and loaded["a"].sleep == "none"


def test_sleep_after_with_a_sleep_action_needs_no_sleep_command():
    loaded, problems = load({"a": {**OK, "sleep": "suspend", "sleep_after": "10m"}})
    assert problems == []
    assert loaded["a"].sleep_after == 600.0


def test_bad_sleep_with_sleep_after_disables_auto_sleep():
    loaded, problems = load({"box": {**OK, "sleep": "nap", "sleep_after": "10m"}})
    assert loaded["box"].sleep_after == 0.0
    assert len(problems) == 2 and all("box" in p for p in problems)


def test_os_names():
    assert machines.OS_NAMES == ("linux", "macos", "windows")


@pytest.mark.parametrize("value, expected", [
    (" macOS ", "macos"), ("WINDOWS", "windows"), ("linux", "linux"), (None, "linux")])
def test_os_is_read_stripped_and_lowercased(value, expected):
    loaded, problems = load({"a": {**OK, "os": value}})
    assert problems == [] and loaded["a"].os == expected


def test_missing_os_is_linux():
    loaded, problems = load({"a": OK})
    assert problems == [] and loaded["a"].os == "linux"


@pytest.mark.parametrize("value", ["darwin", "win", "", 3, True])
def test_bad_os_is_a_problem_and_falls_back_to_linux(value):
    loaded, problems = load({"box": {**OK, "os": value}})
    assert loaded["box"].os == "linux"
    only_problem(problems, "box")


def test_macos_hibernate_becomes_suspend_with_a_problem():
    loaded, problems = load({"box": {**OK, "os": "macos", "sleep": "hibernate"}})
    assert loaded["box"].sleep == "suspend"
    assert "hibernate" in only_problem(problems, "box")
