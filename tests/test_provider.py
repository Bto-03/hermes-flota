"""The ``flota`` provider's contract: a per-task ``machine`` enum, guidance with the cache clause,
and a route to the machine's profile once the machine answers (woken, or its fallback)."""
import json
import logging
import threading

import pytest

from agent.delegation_provider import DelegationRouteError
from flota_plugin import provider as provider_mod
from flota_plugin.machines import Machine
from flota_plugin.provider import FlotaDelegationProvider, _warned, fleet
from flota_plugin.wake import WakeError

MACHINES = {
    "desktop": {"profile": "desk", "host": "10.0.0.2", "description": "GPU box;\n  heavy builds.",
                "wake": "wol", "mac": "aa:bb:cc:dd:ee:ff", "fallback": "laptop"},
    "laptop": {"profile": "lap", "host": "10.0.0.3", "fallback": "desktop"},
    "nas": {"profile": "nas", "host": "10.0.0.4", "description": "Storage."},
}


def _config(**settings):
    return {"backend": "flota", "backends": {"flota": settings}}


class Ensure:
    """Fake ``ensure_ready``: records the machines asked for and fails the ones in ``down``."""

    def __init__(self, *down):
        self.down = set(down)
        self.calls = []

    def __call__(self, machine):
        self.calls.append(machine.name)
        if machine.name in self.down:
            raise WakeError(f"{machine.name} did not come up within 90s of being woken")
        return 0.0


REAL_PANEL = provider_mod._panel


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch):
    _warned.clear()
    # Never the developer's real config: the panel is empty unless a test sets one.
    monkeypatch.setattr(provider_mod, "_panel", lambda: {})


def _task(**extra):
    return {"goal": "g", "context": None, "index": 0, **extra}


def test_identity():
    provider = FlotaDelegationProvider(ensure=Ensure())
    assert provider.name == "flota"
    assert provider.display_name == "Flota"
    assert provider.summary == "Runs each subagent on one of your own machines, waking it first."
    assert provider.settings(_config(machines=MACHINES)) == {"machines": MACHINES}


def test_machine_parameter_is_an_enum_in_config_order_with_descriptions():
    provider = FlotaDelegationProvider(ensure=Ensure())
    config = _config(machines=MACHINES)
    params = provider.task_parameters(config)
    assert list(params) == ["machine"]
    machine = params["machine"]
    assert machine["type"] == "string"
    assert machine["enum"] == ["desktop", "laptop", "nas"]
    first, *entries = machine["description"].split("\n")
    assert "terminal" in first and "files" in first and "woken" in first and "minute" in first
    assert entries == ["desktop: GPU box; heavy builds.", "laptop", "nas: Storage."]


def test_invalid_machines_are_left_out_of_the_enum():
    provider = FlotaDelegationProvider(ensure=Ensure())
    params = provider.task_parameters(_config(machines={"bad": {"host": "h"}, "nas": MACHINES["nas"]}))
    assert params["machine"]["enum"] == ["nas"]


@pytest.mark.parametrize("config", [
    {}, {"backends": None}, {"backends": {"flota": None}}, {"backends": {"other": {"machines": MACHINES}}},
    _config(), _config(machines={}), _config(machines=None), _config(machines="desktop"),
    _config(machines={"bad": {"host": "no profile"}}),
])
def test_no_valid_machines_means_no_parameter_no_guidance_and_the_parent_route(config):
    ensure = Ensure()
    provider = FlotaDelegationProvider(ensure=ensure)
    assert provider.task_parameters(config) == {}
    assert provider.description(config) == ""
    assert provider.route_task(_task(), config) == {}
    assert ensure.calls == []


def test_schema_and_guidance_are_deterministic_and_never_probe_the_machines():
    ensure = Ensure("desktop")
    provider = FlotaDelegationProvider(ensure=ensure)
    config = _config(machines=MACHINES, cache_ttl="10m")
    first = json.dumps([provider.task_parameters(config), provider.description(config)])
    other = FlotaDelegationProvider(ensure=Ensure())
    assert json.dumps([other.task_parameters(config), other.description(config)]) == first
    assert json.dumps([provider.task_parameters(config), provider.description(config)]) == first
    assert ensure.calls == []


def test_description_is_short_guidance_with_the_cache_clause():
    text = FlotaDelegationProvider(ensure=Ensure()).description(_config(machines=MACHINES))
    assert "`machine`" in text and "parallel" in text
    assert "silent longer than 5 minutes" in text
    assert "fresh task" in text and "summary" in text and "artifacts" in text and "cache" in text
    assert len(text.split()) <= 80


@pytest.mark.parametrize("ttl, shown", [
    ("10m", "10 minutes"), (60, "1 minute"), ("90s", "90 seconds"), ("1h", "1 hour"), ("2h", "2 hours"),
    ("1.5m", "90 seconds"), ("nonsense", "5 minutes"), (None, "5 minutes"),
])
def test_cache_clause_mentions_the_configured_ttl(ttl, shown):
    text = FlotaDelegationProvider(ensure=Ensure()).description(_config(machines=MACHINES, cache_ttl=ttl))
    assert f"silent longer than {shown}" in text


def test_chosen_machine_is_made_ready_then_routed_to_its_profile():
    ensure = Ensure()
    provider = FlotaDelegationProvider(ensure=ensure)
    route = provider.route_task(_task(machine="desktop"), _config(machines=MACHINES))
    assert route == {"profile": "desk", "label": "flota:desktop"}
    assert ensure.calls == ["desktop"]


def test_task_without_machine_runs_on_the_parent_route():
    ensure = Ensure()
    provider = FlotaDelegationProvider(ensure=ensure)
    assert provider.route_task(_task(), _config(machines=MACHINES)) == {}
    assert ensure.calls == []


def test_task_without_machine_uses_the_configured_default():
    ensure = Ensure()
    provider = FlotaDelegationProvider(ensure=ensure)
    config = _config(machines=MACHINES, default=" nas ")
    assert provider.route_task(_task(), config) == {"profile": "nas", "label": "flota:nas"}
    # An explicit choice still wins over the default.
    assert provider.route_task(_task(machine="laptop"), config)["profile"] == "lap"
    assert ensure.calls == ["nas", "laptop"]


@pytest.mark.parametrize("default", ["ghost", 7, ["nas"], "", None])
def test_default_that_is_not_a_loaded_machine_is_ignored(default):
    ensure = Ensure()
    provider = FlotaDelegationProvider(ensure=ensure)
    assert provider.route_task(_task(), _config(machines=MACHINES, default=default)) == {}
    assert ensure.calls == []


def test_invalid_default_is_warned_once(caplog):
    provider = FlotaDelegationProvider(ensure=Ensure())
    config = _config(machines=MACHINES, default="ghost")
    with caplog.at_level(logging.WARNING):
        provider.route_task(_task(), config)
        provider.route_task(_task(), config)
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1
    assert "delegation.backends.flota.default 'ghost'" in warnings[0]


def test_fallback_takes_over_when_the_machine_cannot_be_woken():
    ensure = Ensure("desktop")
    provider = FlotaDelegationProvider(ensure=ensure)
    route = provider.route_task(_task(machine="desktop"), _config(machines=MACHINES))
    assert route == {"profile": "lap", "label": "flota:laptop (fallback for desktop)"}
    assert ensure.calls == ["desktop", "laptop"]


def test_fallback_is_tried_once_and_never_chained():
    # desktop -> laptop -> desktop: the fallback's own fallback is not followed.
    ensure = Ensure("desktop", "laptop")
    provider = FlotaDelegationProvider(ensure=ensure)
    with pytest.raises(DelegationRouteError) as raised:
        provider.route_task(_task(machine="desktop"), _config(machines=MACHINES))
    assert ensure.calls == ["desktop", "laptop"]
    message = str(raised.value)
    assert "desktop did not come up within 90s" in message
    assert "laptop did not come up within 90s" in message
    # Both were tried, so only the untried machine is offered.
    assert "nas" in message and "omit `machine`" in message
    assert "desktop, " not in message and "laptop, " not in message


def test_failure_without_fallback_tells_the_model_why_and_names_the_alternatives():
    ensure = Ensure("nas")
    provider = FlotaDelegationProvider(ensure=ensure)
    with pytest.raises(DelegationRouteError) as raised:
        provider.route_task(_task(machine="nas"), _config(machines=MACHINES))
    assert ensure.calls == ["nas"]
    message = str(raised.value)
    assert "nas did not come up within 90s of being woken" in message
    assert "desktop, laptop" in message
    assert "omit `machine`" in message and "locally" in message


def test_failure_of_the_only_machine_offers_only_the_local_route():
    provider = FlotaDelegationProvider(ensure=Ensure("nas"))
    with pytest.raises(DelegationRouteError) as raised:
        provider.route_task(_task(machine="nas"), _config(machines={"nas": MACHINES["nas"]}))
    message = str(raised.value)
    assert "another machine" not in message and "omit `machine`" in message


def test_default_machine_failure_is_refused_like_a_chosen_one():
    provider = FlotaDelegationProvider(ensure=Ensure("nas"))
    with pytest.raises(DelegationRouteError):
        provider.route_task(_task(), _config(machines=MACHINES, default="nas"))


def test_machine_missing_from_the_config_is_refused_without_a_wake():
    ensure = Ensure()
    provider = FlotaDelegationProvider(ensure=ensure)
    with pytest.raises(DelegationRouteError) as raised:
        provider.route_task(_task(machine="ghost"), _config(machines=MACHINES))
    assert ensure.calls == []
    assert "ghost" in str(raised.value) and "desktop, laptop, nas" in str(raised.value)


def test_on_routed_gets_the_machine_actually_used_and_the_task():
    seen = []
    provider = FlotaDelegationProvider(ensure=Ensure("desktop"), on_routed=lambda m, t: seen.append((m, t)))
    config = _config(machines=MACHINES)
    task = _task(machine="desktop")
    provider.route_task(task, config)
    provider.route_task(_task(), config)  # parent route: no machine, no callback
    assert len(seen) == 1
    machine, routed_task = seen[0]
    assert isinstance(machine, Machine) and machine.name == "laptop"
    assert routed_task is task


def test_on_routed_is_not_called_when_the_task_is_refused():
    seen = []
    provider = FlotaDelegationProvider(ensure=Ensure("nas"), on_routed=lambda m, t: seen.append(m))
    with pytest.raises(DelegationRouteError):
        provider.route_task(_task(machine="nas"), _config(machines=MACHINES))
    assert seen == []


def test_on_routed_exceptions_are_logged_and_swallowed(caplog):
    def boom(machine, task):
        raise RuntimeError("idle tracker broke")

    provider = FlotaDelegationProvider(ensure=Ensure(), on_routed=boom)
    with caplog.at_level(logging.ERROR):
        route = provider.route_task(_task(machine="nas"), _config(machines=MACHINES))
    assert route == {"profile": "nas", "label": "flota:nas"}
    assert "idle tracker broke" in caplog.text


def test_one_wake_per_machine_at_a_time_but_different_machines_in_parallel():
    entered = {"desktop": threading.Event(), "nas": threading.Event()}
    release = threading.Event()
    active, peak, guard = {}, {}, threading.Lock()

    def ensure(machine):
        with guard:
            active[machine.name] = active.get(machine.name, 0) + 1
            peak[machine.name] = max(peak.get(machine.name, 0), active[machine.name])
        entered[machine.name].set()
        release.wait(5)
        with guard:
            active[machine.name] -= 1
        return 0.0

    provider = FlotaDelegationProvider(ensure=ensure)
    config = _config(machines=MACHINES)
    threads = [threading.Thread(target=provider.route_task, args=(_task(machine=name), config))
               for name in ("desktop", "desktop", "nas")]
    for thread in threads:
        thread.start()
    # nas is entered while a desktop wake is still in flight: locks are per machine.
    assert entered["desktop"].wait(5) and entered["nas"].wait(5)
    release.set()
    for thread in threads:
        thread.join(5)
    assert peak == {"desktop": 1, "nas": 1}


def test_fleet_loads_the_machines_from_a_delegation_config():
    machines, problems = fleet(_config(machines={**MACHINES, "bad": {"host": "h"}}))
    assert list(machines) == ["desktop", "laptop", "nas"]
    assert machines["desktop"].profile == "desk"
    assert len(problems) == 1 and "bad" in problems[0]
    assert fleet({}) == ({}, [])
    assert fleet(None) == ({}, [])


PANEL = {"machines": {"pc": {"profile": "flota-pc", "host": "10.0.0.9", "description": "Panel box."}},
         "default": "pc", "cache_ttl": "1h"}


def test_panel_settings_win_over_the_delegation_block(monkeypatch):
    monkeypatch.setattr(provider_mod, "_panel", lambda: PANEL)
    ensure = Ensure()
    provider = FlotaDelegationProvider(ensure=ensure)
    config = _config(machines=MACHINES, default="nas", cache_ttl="10m")
    assert provider.task_parameters(config)["machine"]["enum"] == ["pc"]
    assert "silent longer than 1 hour" in provider.description(config)
    assert provider.route_task(_task(), config) == {"profile": "flota-pc", "label": "flota:pc"}
    assert list(fleet(config)[0]) == ["pc"] and ensure.calls == ["pc"]


def test_panel_alone_is_enough(monkeypatch):
    monkeypatch.setattr(provider_mod, "_panel", lambda: PANEL)
    assert FlotaDelegationProvider(ensure=Ensure()).task_parameters({})["machine"]["enum"] == ["pc"]


def test_panel_reads_the_plugin_block_of_the_active_config(monkeypatch):
    config_mod = pytest.importorskip("hermes_cli.config")
    monkeypatch.setattr(config_mod, "load_config_readonly",
                        lambda: {"plugins": {"entries": {"flota": {"settings": PANEL}}}})
    assert REAL_PANEL() == PANEL
    monkeypatch.setattr(config_mod, "load_config_readonly", lambda: None)
    assert REAL_PANEL() == {}


def test_panel_is_empty_without_core(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "hermes_cli.config", None)  # import now raises ImportError
    assert REAL_PANEL() == {}


# Race with auto-sleep (review #3).

class Clock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _racing(probe_answers):
    from types import SimpleNamespace

    from flota_plugin.idle import IdleSleeper
    clock, log = Clock(), []
    sleeper = IdleSleeper(run=lambda *a, **k: SimpleNamespace(returncode=0, stderr=""),
                          timer=lambda *a: None, clock=clock)
    answers = iter(probe_answers)

    def probe(host, port):
        log.append("probe")
        return next(answers, False)

    def ensure(machine):
        # never called with the sleeper lock held
        assert sleeper._lock.acquire(blocking=False)
        sleeper._lock.release()
        log.append("ensure")

    routed = []
    provider = FlotaDelegationProvider(ensure=ensure, sleeper=sleeper, probe=probe,
                                       sleep=clock.sleep, clock=clock)
    original = sleeper.routed
    sleeper.routed = lambda m, t: (routed.append(m.name), original(m, t))
    provider._on_routed = sleeper.routed
    return provider, sleeper, clock, log, routed


def test_route_right_after_a_suspend_waits_for_the_machine_to_go_down_then_wakes_it():
    provider, sleeper, clock, log, routed = _racing([True, True, False])
    nas = fleet(_config(machines=MACHINES))[0]["nas"]
    sleeper.sleep_now(nas)
    clock.now += 1
    out = provider.route_task(_task(machine="nas"), _config(machines=MACHINES))
    assert out["profile"] == "nas"
    assert log == ["probe", "probe", "probe", "ensure"] and routed == ["nas"]
    assert "busy" in sleeper.sleep_now(nas)  # the routed child now keeps it awake


def test_route_gives_up_waiting_for_down_after_twenty_seconds():
    provider, sleeper, clock, log, routed = _racing([True] * 100)
    nas = fleet(_config(machines=MACHINES))[0]["nas"]
    sleeper.sleep_now(nas)
    provider.route_task(_task(machine="nas"), _config(machines=MACHINES))
    assert log[-1] == "ensure" and 19 <= sum(clock.sleeps) <= 21 and routed == ["nas"]


def test_normal_route_never_probes_for_down_and_releases_the_claim():
    provider, sleeper, clock, log, routed = _racing([])
    provider.route_task(_task(machine="nas"), _config(machines=MACHINES))
    assert log == ["ensure"] and routed == ["nas"] and sleeper._claims == {}


def test_failed_route_releases_its_claims():
    from flota_plugin.idle import IdleSleeper
    sleeper = IdleSleeper(timer=lambda *a: None)
    provider = FlotaDelegationProvider(ensure=Ensure("nas"), sleeper=sleeper)
    with pytest.raises(DelegationRouteError):
        provider.route_task(_task(machine="nas"), _config(machines=MACHINES))
    assert sleeper._claims == {}
