"""ssh_guard: a flota-<machine> profile's SSH terminal never syncs the remote ~/.hermes."""
from pathlib import Path

import pytest

from flota_plugin import ssh_guard


class FakeSSH:
    """Stands in for tools.environments.ssh.SSHEnvironment (same keyword)."""

    def __init__(self, host, user, cwd="~", sync_files=True, probe_only=False):
        self.host, self.user, self.sync_files = host, user, sync_files


@pytest.fixture
def home(monkeypatch):
    holder = {"home": Path("/h/.hermes")}
    monkeypatch.setattr(ssh_guard, "_hermes_home", lambda: holder["home"])
    return holder


@pytest.mark.parametrize("path, flota", [
    ("/home/bob/.hermes/profiles/flota-escritorio", True),
    ("/home/bob/.hermes/profiles/flota-test", True),
    ("/home/bob/.hermes", False),
    ("/home/bob/.hermes/profiles/assistant", False),
    ("/home/bob/flota-x", False),  # not a profile directory
])
def test_only_flota_profiles_are_matched(path, flota):
    assert ssh_guard.is_flota_profile(Path(path)) is flota


def test_a_flota_profile_gets_no_file_sync(home):
    cls = type("SSH", (FakeSSH,), {})
    assert ssh_guard.install(cls) is True
    home["home"] = Path("/home/bob/.hermes/profiles/flota-escritorio")
    assert cls("192.0.2.3", "alice").sync_files is False
    assert cls("192.0.2.3", "alice", sync_files=True).sync_files is False


def test_other_profiles_keep_core_behaviour(home):
    cls = type("SSH", (FakeSSH,), {})
    ssh_guard.install(cls)
    home["home"] = Path("/home/bob/.hermes/profiles/assistant")
    assert cls("h", "u").sync_files is True
    assert cls("h", "u", sync_files=False).sync_files is False


def test_install_is_idempotent(home):
    cls = type("SSH", (FakeSSH,), {})
    ssh_guard.install(cls)
    wrapped = cls.__init__
    assert ssh_guard.install(cls) is True and cls.__init__ is wrapped
    assert ssh_guard.installed(cls)


def test_a_core_without_sync_files_is_left_alone():
    class Other:
        def __init__(self, host, user):
            pass

    original = Other.__init__
    assert ssh_guard.install(Other) is False
    assert Other.__init__ is original and not ssh_guard.installed(Other)


def test_the_real_core_class_takes_sync_files():
    # Breaks loudly if core renames the parameter the guard relies on.
    from tools.environments.ssh import SSHEnvironment
    import inspect
    assert "sync_files" in inspect.signature(SSHEnvironment.__init__).parameters
