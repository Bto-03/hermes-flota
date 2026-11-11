"""crew.py: which profiles are bots, their identity, avatar data URL and state."""
import json
import os
from pathlib import Path

import pytest

from flota_plugin import crew
from flota_plugin.machines import Machine


@pytest.fixture(autouse=True)
def _fresh_avatar_cache():
    crew._avatars.clear()
    yield
    crew._avatars.clear()


def _profile(root: Path, name: str, *, meta: str | None = None, soul=True, avatar: bytes | None = None,
             lock_pid=None, active=None) -> Path:
    d = root / name
    d.mkdir(parents=True)
    if meta is not None:
        (d / "profile.yaml").write_text(meta)
    if soul:
        (d / "SOUL.md").write_text("you are a bot")
    if avatar is not None:
        (d / "assets").mkdir()
        (d / "assets" / "avatar.png").write_bytes(avatar)
    if lock_pid is not None:
        (d / "gateway.lock").write_text(json.dumps({"pid": lock_pid}))
        if active is not None:
            (d / "gateway_state.json").write_text(json.dumps({"pid": lock_pid, "active_agents": active}))
    return d


BOT_META = "ui_meta:\n  hermes-bots:\n    title: Hermes\n    color: hsl(270 68% 58%)\n"


def test_hub_crew_lists_bots_and_skips_flota_profiles(tmp_path):
    profiles = [("default", _profile(tmp_path, "root", meta="ui_meta:\n  hermes-bots:\n    title: escriotrio\n")),
                ("asistente", _profile(tmp_path, "asistente", meta=BOT_META, avatar=b"PNG")),
                ("flota-d2", _profile(tmp_path, "flota-d2", meta=BOT_META)),
                ("bare", _profile(tmp_path, "bare", soul=False)),
                ("writer", _profile(tmp_path, "writer", meta="display_name: Escritor\n", soul=False))]
    bots = crew.hub_crew(profiles, alive=lambda pid: False, ship_hands={"flota-d2"})
    assert [b["id"] for b in bots] == ["default", "asistente", "writer"]
    assert bots[0]["name"] == "escriotrio" and bots[0]["color"] is None and bots[0]["avatar"] is None
    assert bots[1] == {"id": "asistente", "name": "Hermes", "avatar": "data:image/png;base64,UE5H",
                       "color": "hsl(270 68% 58%)", "state": "asleep"}
    assert bots[2]["name"] == "Escritor"


def test_default_profile_without_a_title_is_called_hermes(tmp_path):
    bots = crew.hub_crew([("default", _profile(tmp_path, "root", soul=False))], alive=lambda pid: False)
    assert bots[0]["name"] == "Hermes"


def test_hub_state_follows_the_gateway(tmp_path):
    profiles = [("a", _profile(tmp_path, "a", lock_pid=111)), ("b", _profile(tmp_path, "b", lock_pid=222, active=2)),
                ("c", _profile(tmp_path, "c", lock_pid=333)), ("d", _profile(tmp_path, "d"))]
    states = {b["id"]: b["state"] for b in crew.hub_crew(profiles, alive=lambda pid: pid in (111, 222))}
    assert states == {"a": "awake", "b": "working", "c": "asleep", "d": "asleep"}


def test_avatar_over_the_cap_is_null(tmp_path):
    big = _profile(tmp_path, "big", avatar=b"x" * (crew.AVATAR_CAP + 1))
    assert crew.avatar_data_url(big) is None
    ok = _profile(tmp_path, "ok", avatar=b"x" * crew.AVATAR_CAP)
    assert crew.avatar_data_url(ok).startswith("data:image/png;base64,")


def test_avatar_is_cached_for_a_minute(tmp_path):
    d = _profile(tmp_path, "a", avatar=b"one")
    now = [100.0]
    first = crew.avatar_data_url(d, clock=lambda: now[0])
    (d / "assets" / "avatar.png").write_bytes(b"two")
    assert crew.avatar_data_url(d, clock=lambda: now[0]) == first
    now[0] += crew.AVATAR_TTL + 1
    assert crew.avatar_data_url(d, clock=lambda: now[0]) != first


def test_machine_crew_ship_hand_ssh_profiles_and_reported_names(tmp_path):
    m = Machine(name="torre", profile="flota-torre", host="10.0.0.5")
    hand = _profile(tmp_path, "flota-torre")
    coder = _profile(tmp_path, "coder", meta=BOT_META.replace("Hermes", "Coder"))
    terminals = {"flota-torre": {"backend": "ssh", "ssh_host": "10.0.0.5"},
                 "coder": {"backend": "ssh", "ssh_host": "10.0.0.5"},
                 "elsewhere": {"backend": "ssh", "ssh_host": "10.0.0.9"}}
    entry = {"crew_names": ["Lia", "Coder", "<script>", 7, "Lia"]}
    kw = dict(terminals=terminals, profiles={"flota-torre": hand, "coder": coder})
    bots = crew.machine_crew(m, entry, up=True, busy=2, **kw)
    assert [(b["id"], b["name"], b["state"], b["ship_hand"]) for b in bots] == [
        ("flota-torre", "torre", "working", True), ("coder", "Coder", "working", False),
        ("torre/Lia", "Lia", "awake", False), ("torre/script", "script", "awake", False)]
    assert {b["state"] for b in crew.machine_crew(m, entry, up=False, busy=None, **kw)} == {"asleep"}
    assert crew.machine_crew(m, None, up=True, busy=None, terminals={}, profiles={})[0]["state"] == "awake"


def test_hub_crew_includes_every_real_bot_but_not_flota_plumbing(tmp_path):
    profiles = [("pic", _profile(tmp_path, "pic", soul=False, avatar=b"PNG")),
                ("soul", _profile(tmp_path, "soul")),
                ("flota-dev", _profile(tmp_path, "flota-dev", avatar=b"PNG")),  # plumbing: SOUL + avatar, no title
                ("flota-lab", _profile(tmp_path, "flota-lab", meta=BOT_META)),  # titled, not a ship hand
                ("flota-torre", _profile(tmp_path, "flota-torre", meta=BOT_META))]  # ship hand of torre
    bots = crew.hub_crew(profiles, alive=lambda pid: False, ship_hands={"flota-torre"})
    assert [b["id"] for b in bots] == ["pic", "soul", "flota-lab"]
    report = {p: (inc, why) for p, inc, why in crew.hub_report(profiles, ship_hands={"flota-torre"})}
    assert report["flota-dev"] == (False, "flota plumbing, no bot title")
    assert report["flota-torre"] == (False, "ship hand of a registered machine")
    assert report["pic"] == (True, "avatar")
