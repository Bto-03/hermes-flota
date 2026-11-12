from pathlib import Path

from hermes_cli.plugin_validate import ValidationReport, _check_config_spec
from hermes_cli.plugins_settings import _field_type
from utils import fast_safe_load

MANIFEST = fast_safe_load((Path(__file__).resolve().parents[1] / "plugin.yaml").read_text())
SCHEMA = MANIFEST["config_schema"]


def test_manifest_declares_the_tool_and_version():
    assert MANIFEST["provides_tools"] == ["flota"]
    assert MANIFEST["version"] == "0.2.2"
    assert MANIFEST["provides_hooks"] == ["subagent_start", "subagent_stop", "pre_tool_call"]
    # pre_tool_call "approve" (with rule_key) exists since core 0.18.1, below this floor.
    assert MANIFEST["requires_hermes"] == ">=0.22.5"


def test_config_schema_passes_core_validation():
    report = ValidationReport()
    _check_config_spec(report, MANIFEST)
    assert report.ok, report.failures


def test_panel_fields_in_order_with_types_and_defaults():
    assert [(k, _field_type(s), s["default"]) for k, s in SCHEMA.items()] == [
        ("auto_sleep", "boolean", True),
        ("sleep_after", "string", "10m"),
        ("sleep_action", "enum", "suspend"),
        ("allow_power_off", "boolean", True),
        ("agent_tool", "boolean", True),
        ("agent_power_actions", "boolean", False),
        ("cache_ttl", "string", "5m"),
        ("machines", "json", {}),
    ]
    assert SCHEMA["sleep_action"]["choices"] == ["suspend", "hibernate", "poweroff"]


def test_every_field_has_a_label_and_description():
    for key, spec in SCHEMA.items():
        assert spec.get("label") and spec.get("description"), key
    assert SCHEMA["allow_power_off"]["description"] == (
        "Allow powering off machines that Flota can wake again (Wake-on-LAN or a wake command). "
        "Machines without a wake method can only be suspended.")
    assert "hermes flota setup" in SCHEMA["machines"]["description"]
