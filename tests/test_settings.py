import copy

from flota_plugin import settings

OLD = {"backends": {"flota": {"machines": {"pc": {"profile": "pc", "host": "h"}},
                              "default": "pc", "cache_ttl": "7m"}}}


def test_falls_back_to_delegation_config():
    out = settings.effective(OLD, None)
    assert out["machines"] == {"pc": {"profile": "pc", "host": "h", "sleep": "none"}}  # no wake: stays on
    assert out["default"] == "pc" and out["cache_ttl"] == "7m"
    assert out["allow_power_off"] is True and out["agent_tool"] is True


def test_panel_wins_when_set():
    panel = {"machines": {"lap": {"profile": "lap", "host": "l"}}, "default": "lap", "cache_ttl": "1m"}
    out = settings.effective(OLD, panel)
    assert list(out["machines"]) == ["lap"]
    assert out["default"] == "lap" and out["cache_ttl"] == "1m"


def test_empty_panel_machines_do_not_hide_old_ones():
    assert list(settings.effective(OLD, {"machines": {}})["machines"]) == ["pc"]


def test_nothing_configured():
    out = settings.effective(None, None)
    assert out["machines"] == {} and out["default"] is None and out["cache_ttl"] is None


def test_machine_values_win_over_panel_defaults():
    panel = {"sleep_after": "3m", "sleep_action": "hibernate",
             "machines": {"a": {"sleep_after": "1h", "sleep": "none"}, "b": {"wake": "timer"}}}
    out = settings.effective(None, panel)["machines"]
    assert out["a"] == {"sleep_after": "1h", "sleep": "none"}
    assert out["b"] == {"wake": "timer", "sleep_after": "3m", "sleep": "hibernate"}


def test_auto_sleep_off_forces_zero():
    panel = {"auto_sleep": False, "machines": {"a": {"sleep_after": "1h"}}}
    assert settings.effective(None, panel)["machines"]["a"]["sleep_after"] == 0


WOL = {"wake": "wol", "mac": "aa:bb:cc:dd:ee:ff"}


def test_poweroff_sleep_needs_a_wake_method_and_the_master_switch():
    panel = {"sleep_action": "poweroff", "machines": {
        "a": dict(WOL), "b": {**WOL, "sleep": "poweroff"}, "c": {"wake": "command", "wake_command": "on"},
        "d": {"wake": " wol_via ", "via": "r", "mac": "aa:bb:cc:dd:ee:ff"}, "bare": {}, "none": {"wake": "none"}}}
    out = settings.effective(None, panel)
    assert out["allow_power_off"] is True
    assert {n: m["sleep"] for n, m in out["machines"].items()} == {
        "a": "poweroff", "b": "poweroff", "c": "poweroff", "d": "poweroff", "bare": "none", "none": "none"}
    out = settings.effective(None, {**panel, "allow_power_off": False})
    assert out["allow_power_off"] is False
    assert {n: m["sleep"] for n, m in out["machines"].items()} == {
        "a": "suspend", "b": "suspend", "c": "suspend", "d": "suspend", "bare": "none", "none": "none"}


def test_power_off_refusal_per_machine():
    from types import SimpleNamespace
    wol = SimpleNamespace(name="torre", wake="wol")
    bare = SimpleNamespace(name="mini", wake="none")
    assert settings.can_power_off(wol, {}) and settings.power_off_refusal(wol, {}) is None
    assert settings.can_power_off(SimpleNamespace(name="x", wake="command"), {"allow_power_off": True})
    assert not settings.can_power_off(bare, {})
    assert settings.power_off_refusal(bare, {}) == settings.no_wake_message("mini") == (
        "mini has no wake method, so Flota would not be able to turn it back on; it can only be suspended.")
    assert settings.power_off_refusal(wol, {"allow_power_off": False}) == settings.POWER_OFF_DISABLED
    assert "Allow power off" in settings.POWER_OFF_DISABLED


def test_inputs_are_not_mutated():
    panel = {"auto_sleep": False, "machines": {"a": {"sleep": "poweroff"}}}
    before_panel, before_old = copy.deepcopy(panel), copy.deepcopy(OLD)
    settings.effective(OLD, panel)
    settings.effective(OLD, None)
    assert panel == before_panel and OLD == before_old


def test_bad_machine_entries_pass_through_for_load_machines_to_report():
    assert settings.effective(None, {"machines": {"x": "oops"}})["machines"] == {"x": "oops"}


def test_plugin_settings_from_config():
    cfg = {"plugins": {"entries": {"flota": {"settings": {"auto_sleep": False}}}}}
    assert settings.plugin_settings_from_config(cfg) == {"auto_sleep": False}
    assert settings.plugin_settings_from_config({}) == {}
    assert settings.plugin_settings_from_config({"plugins": {"entries": {"flota": {"settings": 3}}}}) == {}


def test_sleep_none_machine_gets_no_default_idle_time():
    out = settings.effective(None, {"machines": {"m": {"profile": "p", "host": "h", "sleep": "none"}}})
    assert "sleep_after" not in out["machines"]["m"]


def test_poweroff_with_stray_spaces_still_needs_permission():
    from flota_plugin.machines import load_machines
    out = settings.effective(None, {"machines": {"a": {"profile": "p", "host": "h", "sleep": " poweroff "}}})
    assert load_machines(out)[0]["a"].sleep == "suspend"


def test_a_machine_flota_cannot_wake_is_not_auto_slept_by_default():
    machines = settings.effective(None, {"sleep_action": "hibernate", "machines": {
        "srv": {"profile": "srv", "host": "h"},
        "srv2": {"profile": "srv2", "host": "h", "wake": " none "},
        "own": {"profile": "own", "host": "h", "wake": "none", "sleep": "suspend"},
        "alarm": {"profile": "alarm", "host": "h", "wake_timer": True},
        "timer": {"profile": "timer", "host": "h", "wake": "timer"},
        "wol": {"profile": "wol", "host": "h", "wake": "wol", "mac": "aa:bb:cc:dd:ee:ff"}}})["machines"]
    assert machines["srv"]["sleep"] == machines["srv2"]["sleep"] == "none"
    assert "sleep_after" not in machines["srv"]
    assert machines["own"]["sleep"] == "suspend" and machines["own"]["sleep_after"] == "10m"  # explicit wins
    assert [machines[n]["sleep"] for n in ("alarm", "timer", "wol")] == ["hibernate"] * 3

