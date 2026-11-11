"""Flota's state stays in the profile that owns the fleet, never in a flota-<machine> plumbing profile.

Regression: the desktop app scopes plugin calls to the profile you last chatted with; clicking a
ship's bot opens a chat as ``flota-<machine>`` (cloned from the owner, so Flota is enabled there), so
core bound the request's hermes home to that profile and ``GET /invite`` wrote the nonce into
``profiles/flota-studio/flota/invites.json``, where ``hermes -p default flota add-ship`` never
looks. Real core seams (hermes-home override, config read/write) on a temporary Hermes root.
"""
import contextlib
import importlib.util
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from flota_plugin import cli, home, idle, pairing, slash

from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override

def fake_pub(comment: str, fill: bytes = b"flota") -> str:
    """A well-formed ssh-ed25519 public key line built at runtime (32 filler bytes, no real key)."""
    import base64
    import struct
    body = (fill * 32)[:32]
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + body
    return f"ssh-ed25519 {base64.b64encode(blob).decode()} {comment}"


PUB = fake_pub("carol@estudio")
DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"


@contextlib.contextmanager
def scoped(path):
    """What core does for a request scoped to a profile (web_server_* / plugin_host)."""
    token = set_hermes_home_override(str(path))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


@pytest.fixture
def root(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    for d in (root, root / "profiles" / "aide"):
        d.mkdir(parents=True)
        (d / "config.yaml").write_text("model: x\n")
    plumbing = root / "profiles" / "flota-studio"
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(cli, "_flota_key", lambda configured: (PUB, "/k/id_ed25519"))
    monkeypatch.setattr(cli, "_hostname", lambda: "servidor")
    monkeypatch.setattr(cli, "_login_user", lambda: "bob")
    monkeypatch.setattr(cli, "_profile_exists", lambda name: (root / "profiles" / name / "config.yaml").is_file())
    monkeypatch.setattr(cli, "_profile_dir", lambda name: root / "profiles" / name)
    clones = []

    def create_profile(name, clone_config=False, no_alias=False):  # core's, minus the real profile root
        clones.append(Path(get_hermes_home()))
        target = root / "profiles" / name
        target.mkdir(parents=True)
        (target / "config.yaml").write_text((Path(get_hermes_home()) / "config.yaml").read_text())
        return target

    import hermes_cli.profiles
    monkeypatch.setattr(hermes_cli.profiles, "create_profile", create_profile)
    return SimpleNamespace(root=root, aide=root / "profiles" / "aide", plumbing=plumbing, clones=clones)


def _setup(name="studio"):
    return cli.run_setup(SimpleNamespace(name=name, host="192.0.2.28", user="carol", no_detect=True,
                                         sleep="none", wake="none"))


def _nonce(invite):
    return pairing.read_invite(invite["code"])["nonce"]


def test_invite_from_a_plumbing_scope_lands_in_the_root_profile(root):
    with scoped(root.root):
        assert _setup()[0] == 0  # the flagship's setup made flota-studio
    with scoped(root.plumbing):  # the Webapp after "open chat" with studio's bot
        invite = cli.run_invite()
        assert cli._active_config_path() == root.root / "config.yaml"
        assert idle.state_path() == root.root / "flota" / "state.json"
        assert "studio" in slash.load_settings()["machines"]
    assert not (root.plumbing / "flota" / "invites.json").exists()
    store = root.root / "flota" / "invites.json"
    assert _nonce(invite) in json.loads(store.read_text())
    # `hermes -p default flota add-ship <code>` reads the root store: the code is one it issued.
    ship = pairing.encode(pairing.SHIP_PREFIX, {"v": 1, "nonce": _nonce(invite), "name": "desk-box", "host": "h",
                                                "user": "carol"})
    with scoped(root.root):
        assert pairing.read_ship(ship, cli._invites_store())["nonce"] == _nonce(invite)


def test_plumbing_profile_maps_to_the_profile_that_set_it_up(root):
    with scoped(root.aide):
        assert _setup()[0] == 0
    assert root.clones == [root.aide]  # cloned from the active profile
    assert (root.plumbing / "flota" / "owner").read_text().strip() == str(root.aide)
    with scoped(root.plumbing):
        assert home.flota_home() == root.aide
        cli.run_invite()
        with home.owner_scope():
            assert Path(get_hermes_home()) == root.aide
        assert Path(get_hermes_home()) == root.plumbing  # restored: the scope is context-local
    assert (root.aide / "flota" / "invites.json").is_file()
    assert not (root.root / "flota").exists() and not (root.plumbing / "flota" / "invites.json").exists()
    # A setup run from the plumbing scope saves the machine in the owner, clones nothing.
    with scoped(root.plumbing):
        assert _setup()[0] == 0
    plumbing_cfg = cli._read_config(root.plumbing / "config.yaml")
    owner_cfg = cli._read_config(root.aide / "config.yaml")
    assert "plugins" not in plumbing_cfg and plumbing_cfg["terminal"]["ssh_host"] == "192.0.2.28"
    assert owner_cfg["plugins"]["entries"]["flota"]["settings"]["machines"]["studio"]["profile"] == \
        "flota-studio"


def test_owner_marker_that_points_nowhere_falls_back_to_the_root(root):
    root.plumbing.mkdir(parents=True)
    (root.plumbing / "flota").mkdir()
    (root.plumbing / "flota" / "owner").write_text("/nonexistent/profile\n")
    assert home.owner_of(root.plumbing) == root.root
    assert home.owner_of(root.aide) == root.aide
    assert home.owner_of(root.root) == root.root


def _load_api():
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_flota_home_test",
                                                  DASHBOARD / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dashboard_invite_and_worker_threads_keep_the_request_profile(root):
    api = _load_api()
    with scoped(root.root):
        _setup()
    with scoped(root.plumbing):
        invite = api.make_invite(None)  # POST /invite mints; GET /invite only reads the last one
    assert _nonce(invite) in json.loads((root.root / "flota" / "invites.json").read_text())
    assert not (root.plumbing / "flota" / "invites.json").exists()
    # Setup / add-ship run in a worker thread: it must see the request's profile, not the launch one.
    with scoped(root.aide):
        seen = api._in_worker(lambda: {"home": str(get_hermes_home()), "thread": threading.current_thread().name},
                              "x")
    assert seen == {"home": str(root.aide), "thread": "flota-setup-x"}


# --- plumbing profiles stay out of the Desktop Bots roster ---------------------------------------

def _profile_yaml(path):
    import hermes_yaml
    return hermes_yaml.safe_load((path / "profile.yaml").read_text()) or {}


def test_setup_hides_the_plumbing_profile_from_the_bots_roster(root):
    with scoped(root.root):
        assert _setup()[0] == 0
    meta = _profile_yaml(root.plumbing)
    bots = meta["ui_meta"]["hermes-bots"]
    assert bots["hidden"] is True
    assert "title" not in bots  # a title would make the Flota map count it as a bot
    assert meta["_ui_meta_revisions"]["hermes-bots"] == 1  # Desktop's CAS sees the change
    assert "studio" in meta["description"] and "Flota" in meta["description"]
    with scoped(root.root):  # a re-run changes nothing
        assert _setup()[0] == 0
    assert _profile_yaml(root.plumbing)["_ui_meta_revisions"]["hermes-bots"] == 1


def test_hiding_keeps_what_the_user_set(root):
    root.plumbing.mkdir(parents=True)
    (root.plumbing / "profile.yaml").write_text(
        "description: mine\nui_meta:\n  hermes-bots:\n    color: red\n  other: {x: 1}\n"
        "_ui_meta_revisions:\n  hermes-bots: 4\n")
    assert home.hide_from_roster(root.plumbing, "studio") is True
    meta = _profile_yaml(root.plumbing)
    assert meta["description"] == "mine" and meta["ui_meta"]["other"] == {"x": 1}
    assert meta["ui_meta"]["hermes-bots"] == {"color": "red", "hidden": True}
    assert meta["_ui_meta_revisions"]["hermes-bots"] == 5
    assert home.hide_from_roster(root.plumbing, "studio") is False  # already hidden
    assert home.hide_from_roster(root.root / "profiles" / "nope", "x") is False  # never raises


def test_doctor_fix_hides_existing_plumbing_profiles(root, capsys):
    for name in ("flota-studio", "flota-viejo"):
        (root.root / "profiles" / name).mkdir(parents=True)
        (root.root / "profiles" / name / "config.yaml").write_text("model: x\n")
    import argparse
    parser = argparse.ArgumentParser(prog="hermes flota")
    cli.setup_parser(parser)
    with scoped(root.root):
        args = parser.parse_args(["doctor", "--fix"])
        args.func(args)
    out = capsys.readouterr().out
    assert "flota-studio" in out and "flota-viejo" in out
    for name in ("flota-studio", "flota-viejo"):
        assert _profile_yaml(root.root / "profiles" / name)["ui_meta"]["hermes-bots"]["hidden"] is True
    assert not (root.aide / "profile.yaml").exists()  # a real bot is never touched


def test_deletions_reach_the_real_config(root):
    """Core's atomic_config_write refuses a write that drops keys; removing a machine, clearing the
    role, leaving a fleet and force-removing ships are deliberate deletions and must land."""
    api = _load_api()
    with scoped(root.root):
        assert _setup("studio")[0] == 0 and _setup("sinwol")[0] == 0
        assert api.delete_machine("sinwol")["ok"]
        assert list(cli._ships(cli._read_config(cli._active_config_path()))) == ["studio"]
        cli.set_role("flagship")
        assert cli.set_role(None)["role"] is None and cli.role() == "flagship"  # inferred again
        assert "role" not in cli._panel()
        cli.set_role("ship", force=True)
        assert cli.ships_count() == 0 and cli.role() == "ship"
        cli._set_panel("joined_fleets", [{"flagship": "servidor", "joined_at": "t"}])
        assert cli.leave_fleet() == 0 and cli.fleet() is None and cli.role() is None
