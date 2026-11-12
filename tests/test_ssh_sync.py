"""ssh_sync: Flota turns SSH file sync off through terminal.ssh_sync_files, never by patching core."""
import hermes_cli.config as config_mod

from flota_plugin import ssh_sync


def test_disable_sets_the_key_once():
    terminal = {"backend": "ssh"}
    assert ssh_sync.disable(terminal) is True and terminal == {"backend": "ssh", "ssh_sync_files": False}
    assert ssh_sync.disable(terminal) is False
    hand_set = {"ssh_sync_files": True}  # a profile that had sync on is turned off
    assert ssh_sync.disable(hand_set) is True and hand_set["ssh_sync_files"] is False


def test_disabled_reads_a_profile_config():
    assert ssh_sync.disabled({"terminal": {"ssh_sync_files": False}}) is True
    for config in ({}, {"terminal": {}}, {"terminal": {"ssh_sync_files": "false"}}, {"terminal": None}, None):
        assert ssh_sync.disabled(config) is False


def test_core_support_comes_from_the_public_default_config(monkeypatch):
    terminal = dict(config_mod.DEFAULT_CONFIG["terminal"])
    terminal.pop("ssh_sync_files", None)
    monkeypatch.setitem(config_mod.DEFAULT_CONFIG, "terminal", terminal)
    assert ssh_sync.core_reads_key() is False  # core 0.23.1: the key is not known yet
    monkeypatch.setitem(config_mod.DEFAULT_CONFIG, "terminal", {**terminal, "ssh_sync_files": True})
    assert ssh_sync.core_reads_key() is True  # a core with PR #148791


def test_the_plugin_never_rebinds_a_core_class():
    from pathlib import Path
    root = Path(ssh_sync.__file__).resolve().parent
    for path in root.rglob("*.py"):
        if "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        assert "setattr(" not in text and ".__init__ =" not in text, path
