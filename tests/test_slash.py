"""``/flota``: status, dry-run, wake and sleep, with the network and the profile lookup injected."""
import threading

import pytest

from flota_plugin import power, slash
from flota_plugin.wake import WakeError

SETTINGS = {"machines": {
    "torre": {"profile": "flota-torre", "host": "10.0.0.5", "wake": "wol", "mac": "aa:bb:cc:dd:ee:ff",
              "broadcast": "10.0.0.255", "fallback": "mini", "sleep_after": "10m",
              "sleep_command": "systemctl suspend", "description": "GPU tower"},
    "mini": {"profile": "flota-mini", "host": "mini.lan", "port": 2222},
    "nas": {"profile": "flota-nas", "host": "nas.lan", "wake": "wol_via", "mac": "11:22:33:44:55:66",
            "via": "pi@relay", "wake_timeout": 45},
    "lab": {"profile": "flota-lab", "host": "lab.lan", "wake": "command", "wake_command": "ipmi-on lab"},
    "rota": {"profile": "x"},
}}
UP = {"mini.lan"}


class Sleeper:
    def __init__(self):
        self.slept = []

    def active(self):
        return {"torre": 2}

    def sleep_now(self, machine, action=None):
        self.slept.append((machine.name, action) if action else machine.name)
        return f"{machine.name}: {action or 'sleep'} command sent"


class Power:
    """Fake ``power.power``: records (machine, action) and never runs ssh."""

    def __init__(self):
        self.calls = []

    def __call__(self, machine, action):
        self.calls.append((machine.name, action))
        return f"{machine.name}: {action} sent"


def run(args, settings=SETTINGS, up=UP, **kwargs):
    kwargs.setdefault("profile_exists", lambda name: name != "flota-nas")
    kwargs.setdefault("ensure", lambda machine: pytest.fail("ensure must not be called"))
    return slash.handle(args, settings=settings, probe=lambda host, port: host in up, **kwargs)


def line(text, name):
    return next(l for l in text.splitlines() if l.lstrip("- ").startswith(name))


@pytest.mark.parametrize("args", ["", "  ", "status", "STATUS"])
def test_status_lists_every_machine(args):
    out = run(args)
    torre, mini, nas = line(out, "torre"), line(out, "mini"), line(out, "nas")
    assert "down" in torre and "wol" in torre and "flota-torre" in torre
    assert "fallback mini" in torre and "sleeps: `systemctl suspend` after 10m idle" in torre
    assert "up" in mini and "down" not in mini and "fallback" not in mini and "sleeps" not in mini
    assert "wol_via" in nas and "flota-nas (profile missing)" in nas
    assert "(profile missing)" not in torre


def test_status_reports_config_problems_last():
    lines = run("status").splitlines()
    assert lines[-1].startswith("! machine 'rota':")
    assert not any(l.lstrip("- ").startswith("rota") for l in lines)


def test_status_shows_running_subagents_only_with_a_sleeper():
    assert "2 running" in line(run("", sleeper=Sleeper()), "torre")
    assert "0 running" in line(run("", sleeper=Sleeper()), "mini")
    assert "running" not in run("")


def test_status_probes_machines_concurrently():
    # Each probe waits for the others: a sequential status would break the barrier.
    barrier = threading.Barrier(4, timeout=5)

    def probe(host, port):
        barrier.wait()
        return True

    out = slash.handle("", settings=SETTINGS, probe=probe, profile_exists=lambda name: True)
    assert out.count(" up") == 4


def test_status_probes_the_configured_port():
    seen = []
    slash.handle("", settings=SETTINGS, probe=lambda host, port: seen.append((host, port)),
                 profile_exists=lambda name: True)
    assert ("mini.lan", 2222) in seen and ("10.0.0.5", 22) in seen


@pytest.mark.parametrize("settings", [{}, {"machines": {}}, None])
def test_no_machines_points_at_the_panel_and_setup(settings):
    out = run("", settings=settings)
    assert "Plugins" in out and "hermes flota setup" in out


SUSPEND = {"profile": "flota-pc", "host": "pc.lan", "user": "carol", "sleep": "suspend",
           "sleep_after": "10m", "power_prefix": power.NIRI_PREFIX}


def test_status_shows_the_sleep_action():
    out = run("", settings={"machines": {"pc": SUSPEND}})
    assert "sleeps: suspend after 10m idle" in line(out, "pc")


def test_dry_run_shows_the_real_ssh_command_and_the_graphical_session():
    out = run("dry-run pc", settings={"machines": {"pc": SUSPEND}})
    step = next(l for l in out.splitlines() if "Sleep:" in l)
    machine = slash.load_machines({"machines": {"pc": SUSPEND}})[0]["pc"]
    assert "suspend it over SSH" in step and f"`{power.remote_command(machine, 'suspend')}`" in step
    assert "graphical session" in step and "10m" in step
    assert "S5" not in out


def test_dry_run_without_prefix_has_no_graphical_session():
    out = run("dry-run pc", settings={"machines": {"pc": {**SUSPEND, "power_prefix": ""}}})
    assert "(`systemctl suspend`)" in out and "graphical session" not in out


def test_dry_run_of_a_poweroff_machine_warns_about_wol_from_s5():
    out = run("dry-run pc", settings={"machines": {"pc": {**SUSPEND, "sleep": "poweroff",
                                                          "power_prefix": ""}}})
    assert "power it off over SSH (`systemctl poweroff`)" in out
    assert "Wake-on-LAN from S5" in out and "BIOS" in out


def test_dry_run_of_a_sleeping_wol_machine_names_every_step():
    out = run("dry-run torre")
    assert "10.0.0.5:22" in out and "down" in out
    assert "aa:bb:cc:dd:ee:ff" in out and "10.0.0.255" in out
    assert "90s" in out
    assert "flota-torre" in out
    assert "mini" in out
    assert "systemctl suspend" in out and "10m" in out
    assert "nothing was sent" in out.lower()


def test_dry_run_of_a_machine_that_is_up_skips_the_wake():
    out = run("dry-run mini")
    assert "mini.lan:2222" in out and "up" in out
    assert "no wake needed" in out.lower()
    assert "no fallback" in out.lower()
    assert "stays on" in out.lower()


def test_dry_run_names_the_relay_and_the_command():
    relay = run("dry-run nas")
    assert "pi@relay" in relay and "11:22:33:44:55:66" in relay and "45s" in relay
    assert "does not exist" in relay  # the profile is missing: the delegation would be refused
    assert "ipmi-on lab" in run("dry-run lab")


def test_dry_run_of_a_down_machine_without_wake_method():
    out = run("dry-run mini", up=set())
    assert "no wake method" in out and "refused" in out


def test_dry_run_never_wakes():
    run("dry-run torre")  # the default `ensure` in run() fails the test when called


def test_wake_reports_the_wait():
    woken = []
    out = run("wake torre", ensure=lambda machine: woken.append(machine.name) or 12.4)
    assert woken == ["torre"] and out == "torre is up (waited 12s)"


def test_wake_returns_the_error_text():
    def ensure(machine):
        raise WakeError("torre did not come up within 90s of being woken")

    assert run("wake torre", ensure=ensure) == "torre did not come up within 90s of being woken"


def test_sleep_uses_the_sleeper():
    sleeper = Sleeper()
    assert run("sleep torre", sleeper=sleeper, power=Power()) == "torre: sleep command sent"
    assert sleeper.slept == ["torre"]


def test_sleep_without_a_sleeper_runs_the_default_action():
    fake = Power()
    assert run("sleep pc", settings={"machines": {"pc": {**SUSPEND, "sleep": "hibernate"}}},
               power=fake) == "pc: hibernate sent"
    assert run("sleep mini", power=fake) == "mini: suspend sent"  # sleep: none still suspends
    assert fake.calls == [("pc", "hibernate"), ("mini", "suspend")]


WAKEABLE = {**SUSPEND, "wake": "wol", "mac": "aa:bb:cc:dd:ee:ff"}


def test_off_is_refused_for_a_machine_without_a_wake_method():
    fake, sleeper = Power(), Sleeper()
    out = run("off pc", settings={"machines": {"pc": SUSPEND}}, power=fake, sleeper=sleeper)
    assert out == "pc has no wake method, so Flota would not be able to turn it back on; it can only be suspended."
    assert fake.calls == [] and sleeper.slept == []


def test_off_is_refused_when_the_master_switch_is_off():
    fake, sleeper = Power(), Sleeper()
    out = run("off pc", settings={"machines": {"pc": WAKEABLE}, "allow_power_off": False}, power=fake,
              sleeper=sleeper)
    assert "Allow power off" in out and "Plugins" in out
    assert fake.calls == [] and sleeper.slept == []


def test_off_goes_through_the_sleeper_so_busy_machines_are_kept():
    sleeper = Sleeper()
    settings = {"machines": {"pc": WAKEABLE}}
    assert run("off pc", settings=settings, sleeper=sleeper, power=Power()) == "pc: poweroff command sent"
    assert sleeper.slept == [("pc", "poweroff")]


def test_off_without_a_sleeper_powers_off():
    fake = Power()
    settings = {"machines": {"pc": WAKEABLE}}
    assert run("off pc", settings=settings, power=fake) == "pc: poweroff sent"
    assert fake.calls == [("pc", "poweroff")]


@pytest.mark.parametrize("args", ["explode", "wake", "wake nope", "dry-run", "sleep nope", "wake torre extra",
                                  "status torre"])
def test_bad_input_gets_usage_with_subcommands_and_machines(args):
    out = run(args, sleeper=Sleeper())
    assert "Usage: /flota" in out
    for word in ("status", "dry-run", "wake", "sleep", "off", "torre", "mini", "nas", "lab"):
        assert word in out


@pytest.mark.parametrize("command", ["dry-run", "wake", "sleep", "off"])
def test_unknown_machine_is_named(command):
    out = run(f"{command} nope", sleeper=Sleeper())
    assert out.startswith("No machine named 'nope'.")


@pytest.mark.parametrize("command", ["dry-run", "wake", "sleep", "off"])
def test_machine_command_with_no_machines_says_how_to_add_one(command):
    assert run(f"{command} demo", settings={}) == slash._NO_MACHINES


def test_never_raises_on_unexpected_failures():
    def boom(*_):
        raise RuntimeError("kaput")

    assert "kaput" in run("wake torre", ensure=boom)
    assert "kaput" in slash.handle("", settings=SETTINGS, probe=boom, profile_exists=lambda name: True)


def test_make_handler_reads_the_active_profiles_flota_settings(monkeypatch):
    monkeypatch.setattr(slash, "_load_config", lambda: {"delegation": {"backends": {"flota": SETTINGS}}})
    monkeypatch.setattr(slash, "is_up", lambda host, port: True)
    monkeypatch.setattr(slash, "_profile_exists", lambda name: True)
    handler = slash.make_handler(object(), sleeper=Sleeper())
    out = handler("status", session_key="abc")
    assert "torre" in out and "2 running" in out and "down" not in out


def test_make_handler_reads_the_panel_over_the_delegation_block(monkeypatch):
    fake = Power()
    monkeypatch.setattr(slash.power_mod, "power", fake)
    monkeypatch.setattr(slash, "_load_config", lambda: {
        "delegation": {"backends": {"flota": SETTINGS}},
        "plugins": {"entries": {"flota": {"settings": {"machines": {"pc": WAKEABLE}}}}}})
    monkeypatch.setattr(slash, "is_up", lambda host, port: False)
    monkeypatch.setattr(slash, "_profile_exists", lambda name: True)
    handler = slash.make_handler(object())
    out = handler("status")
    assert "pc" in out and "torre" not in out
    assert handler("off pc") == "pc: poweroff sent" and fake.calls == [("pc", "poweroff")]


@pytest.mark.parametrize("config", [{}, {"delegation": None}, {"delegation": {"backends": []}}, None])
def test_make_handler_survives_a_config_without_flota(monkeypatch, config):
    monkeypatch.setattr(slash, "_load_config", lambda: config)
    assert "hermes flota setup" in slash.make_handler(object())("")


def test_make_handler_reports_an_unreadable_config(monkeypatch):
    def broken():
        raise ValueError("config.yaml is not valid YAML")

    monkeypatch.setattr(slash, "_load_config", broken)
    assert "not valid YAML" in slash.make_handler(object())("status")
