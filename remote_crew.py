"""Remote crew: the Hermes bots that live on a fleet machine, discovered over SSH.

A ship that never paired with this flagship (or paired before it had bots) only shows its ship
hand on the map. This asks the machine itself, in ONE ssh call (``power.ssh_argv``: BatchMode, no
password prompt), which Hermes profiles it has: ``${HERMES_HOME:-$HOME/.hermes}`` (the ``default``
profile) and every ``profiles/*`` directory.

The remote side is a POSIX ``sh`` script fed on stdin (``sh -s``), so the remote login shell (bash,
zsh, fish) never re-parses it. It runs ``python3 -I`` when present, else a pure ``sh``/``awk``
fallback. Both read only ``profile.yaml`` (the bot title / display name / colour), whether
``SOUL.md`` and ``assets/avatar.*`` exist, ``gateway.lock`` / ``gateway.pid`` and
``gateway_state.json``. They never open ``config.yaml`` or any credentials file. Output:
``{"hostname": ..., "bots": [{id, name, color, avatar_b64, mime, gateway_alive, active_agents}]}``.

Everything that comes back is untrusted: ``parse`` keeps only well-formed fields and at most
``MAX_BOTS`` bots; anything else is ``[]``.

``RemoteCrews`` caches each machine's result for ``TTL`` s and refreshes it in a background thread,
so ``GET /status`` never waits on SSH: the first poll shows what was known (ship hand and the
``crew_names`` reported at pairing); later polls include the scanned bots.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import subprocess
import threading
import time

SSH_TIMEOUT = 8.0
TTL = 120.0
MAX_BOTS = 30
AVATAR_MAX = 48 * 1024  # raw bytes; its base64 fits crew.AVATAR_CAP (64 KB)
_MIMES = ("image/png", "image/jpeg", "image/webp")
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_NAME_DROP = re.compile(r"[^\w .'-]+")
# Hex, keyword or one colour function of numbers; never url(...) (the page would fetch it).
_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}|[A-Za-z]{3,24}"
                    r"|(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch)\([0-9A-Za-z.,%/ +-]{1,48}\)")
_B64 = re.compile(r"[A-Za-z0-9+/]+={0,2}")
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
FLOTA_PREFIX = "flota-"

_PY = r'''
import base64, json, os, socket
home = os.getenv("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
home = os.path.abspath(home)
if os.path.basename(os.path.dirname(home)) == "profiles":  # HERMES_HOME is a profile: use its root
    home = os.path.dirname(os.path.dirname(home))

def text(path, limit=65536):
    try:
        with open(path, "rb") as f:
            return f.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""

def meta(path):
    """display_name, ui_meta.hermes-bots.title / .color from profile.yaml (no PyYAML needed)."""
    out, stack = {}, []
    for raw in text(path).splitlines():
        line = raw.split(" #", 1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        indent = len(line) - len(line.lstrip())
        key, _, value = line.strip().partition(":")
        key, value = key.strip().strip("'\""), value.strip().strip("'\"")
        while stack and stack[-1][0] >= indent:
            stack.pop()
        where = [k for _, k in stack]
        if not value:
            stack.append((indent, key))
        elif where == [] and key == "display_name":
            out["display_name"] = value
        elif where == ["ui_meta", "hermes-bots"] and key in ("title", "color"):
            out[key] = value
    return out

def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True

def load(path):
    try:
        data = json.loads(text(path))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}

def gateway(d):
    for name in ("gateway.lock", "gateway.pid"):
        pid = load(os.path.join(d, name)).get("pid")
        if type(pid) is int and pid > 0 and pid_alive(pid):
            state = load(os.path.join(d, "gateway_state.json"))
            active = state.get("active_agents")
            return True, active if state.get("pid") == pid and type(active) is int and active > 0 else 0
    return False, 0

dirs = [("default", home)]
try:
    names = sorted(os.listdir(os.path.join(home, "profiles")))
except OSError:
    names = []
dirs += [(n, os.path.join(home, "profiles", n)) for n in names if os.path.isdir(os.path.join(home, "profiles", n))]
bots = []
for pid_, d in dirs[:200]:
    m = meta(os.path.join(d, "profile.yaml"))
    title = (m.get("title") or m.get("display_name") or "").strip()
    avatar, mime = None, None
    for ext, kind in (("png", "image/png"), ("jpg", "image/jpeg"), ("webp", "image/webp")):
        p = os.path.join(d, "assets", "avatar." + ext)
        if os.path.isfile(p):
            mime = kind
            try:
                if os.path.getsize(p) <= 49152:
                    with open(p, "rb") as f:
                        avatar = base64.b64encode(f.read(49153)).decode()
            except OSError:
                pass
            break
    soul = os.path.isfile(os.path.join(d, "SOUL.md"))
    if pid_.startswith("flota-"):
        if not title:
            continue
    elif not (pid_ == "default" or title or soul or mime):
        continue
    alive, active = gateway(d)
    bots.append({"id": pid_, "name": title or None, "color": m.get("color") or None,
                 "avatar_b64": avatar, "mime": mime if avatar else None,
                 "gateway_alive": alive, "active_agents": active})
    if len(bots) >= 30:
        break
print(json.dumps({"hostname": socket.gethostname(), "bots": bots}))
'''

# Pure sh fallback: no avatars, names reduced to safe characters so the JSON needs no escaping.
_SH_FALLBACK = r'''
H=${HERMES_HOME:-$HOME/.hermes}
case $H in */profiles/*) H=${H%/profiles/*} ;; esac
safe() { tr -cd 'A-Za-z0-9 ._()#%,/-' | cut -c1-64; }
field() {
  [ -f "$1/profile.yaml" ] || return 0
  awk -v want="$2" '
    /^[ \t]*#/ || !/:/ { next }
    { match($0, /^ */); ind = RLENGTH; line = substr($0, ind + 1)
      k = line; sub(/:.*/, "", k); v = line; sub(/^[^:]*:[ \t]*/, "", v); gsub(/^["'"'"']|["'"'"'][ \t]*$/, "", v)
      while (n > 0 && ids[n] >= ind) n--
      path = ""; for (i = 1; i <= n; i++) path = path keys[i] "."
      if (v == "") { n++; ids[n] = ind; keys[n] = k; next }
      if (path k == want) { print v; exit } }' "$1/profile.yaml" | safe
}
first=1
printf '{"hostname": "%s", "bots": [' "$( (hostname || uname -n) 2>/dev/null | safe)"
count=0
for entry in "default:$H" $(ls "$H/profiles" 2>/dev/null | sed 's/^/x:/'); do
  case $entry in default:*) id=default; d=$H ;; *) id=${entry#x:}; d=$H/profiles/$id ;; esac
  [ -d "$d" ] || continue
  case $id in *[!a-z0-9_-]*) continue ;; esac
  title=$(field "$d" ui_meta.hermes-bots.title); [ -n "$title" ] || title=$(field "$d" display_name)
  color=$(field "$d" ui_meta.hermes-bots.color)
  case $id in
    flota-*) [ -n "$title" ] || continue ;;
    default) ;;
    *) [ -n "$title" ] || [ -f "$d/SOUL.md" ] || ls "$d"/assets/avatar.* >/dev/null 2>&1 || continue ;;
  esac
  alive=false; active=0
  for f in gateway.lock gateway.pid; do
    pid=$(sed -n 's/.*"pid"[ ]*:[ ]*\([0-9][0-9]*\).*/\1/p' "$d/$f" 2>/dev/null | head -n 1)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      alive=true
      spid=$(sed -n 's/.*"pid"[ ]*:[ ]*\([0-9][0-9]*\).*/\1/p' "$d/gateway_state.json" 2>/dev/null | head -n 1)
      [ "$spid" = "$pid" ] && active=$(sed -n 's/.*"active_agents"[ ]*:[ ]*\([0-9][0-9]*\).*/\1/p' "$d/gateway_state.json" | head -n 1)
      break
    fi
  done
  [ $first = 1 ] || printf ', '; first=0
  printf '{"id": "%s", "name": "%s", "color": "%s", "avatar_b64": null, "mime": null, "gateway_alive": %s, "active_agents": %s}' \
    "$id" "$title" "$color" "$alive" "${active:-0}"
  count=$((count + 1)); [ $count -ge 30 ] && break
done
printf ']}\n'
'''

SCRIPT = ("if command -v python3 >/dev/null 2>&1; then\n"
          "python3 -I - <<'FLOTA_PY'\n" + _PY.strip("\n") + "\nFLOTA_PY\n"
          "else\n" + _SH_FALLBACK.strip("\n") + "\nfi\n")
REMOTE = "sh -s"


def _clean_name(value) -> str | None:
    if not isinstance(value, str):
        return None
    name = _NAME_DROP.sub("", value).strip()[:64]
    return name or None


def _bot(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    pid = raw.get("id")
    if not isinstance(pid, str) or not (pid == "default" or _ID.fullmatch(pid)):
        return None
    name = _clean_name(raw.get("name"))
    if pid.startswith(FLOTA_PREFIX) and not name:
        return None  # Flota plumbing, not a bot
    color = raw.get("color")
    color = color.strip() if isinstance(color, str) and _COLOR.fullmatch(color.strip()) else None
    avatar, mime = raw.get("avatar_b64"), raw.get("mime")
    if not (isinstance(avatar, str) and mime in _MIMES and len(avatar) <= (AVATAR_MAX + 2) // 3 * 4
            and _B64.fullmatch(avatar)):
        avatar = mime = None
    else:
        try:
            base64.b64decode(avatar, validate=True)
        except (binascii.Error, ValueError):
            avatar = mime = None
    active = raw.get("active_agents")
    active = active if isinstance(active, int) and not isinstance(active, bool) and active > 0 else 0
    return {"id": pid, "name": name or ("Hermes" if pid == "default" else pid), "color": color,
            "avatar_b64": avatar, "mime": mime, "gateway_alive": raw.get("gateway_alive") is True,
            "active_agents": active}


def parse(stdout) -> dict:
    """``{"hostname": str | None, "bots": [...]}`` from the remote script's output; never raises."""
    empty = {"hostname": None, "bots": []}
    if not isinstance(stdout, str):
        return empty
    line = stdout.strip().splitlines()[-1] if stdout.strip() else ""
    try:
        data = json.loads(line)
    except ValueError:
        return empty
    if isinstance(data, list):  # a bare list of bots
        data = {"bots": data}
    if not isinstance(data, dict) or not isinstance(data.get("bots"), list):
        return empty
    host = data.get("hostname")
    host = host.strip() if isinstance(host, str) and _HOST.fullmatch(host.strip()) else None
    bots, seen = [], set()
    for raw in data["bots"]:
        bot = _bot(raw)
        if bot and bot["id"] not in seen:
            seen.add(bot["id"])
            bots.append(bot)
        if len(bots) >= MAX_BOTS:
            break
    return {"hostname": host, "bots": bots}


def _ssh_argv(machine) -> list[str]:
    from . import power
    return power.ssh_argv(machine, REMOTE)


def probe(machine, run=subprocess.run) -> dict:
    """One SSH call: ``{"hostname", "bots"}``; empty on any failure."""
    try:
        done = run(_ssh_argv(machine), input=SCRIPT, timeout=SSH_TIMEOUT, capture_output=True, text=True)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return {"hostname": None, "bots": []}
    if getattr(done, "returncode", 1) != 0:
        return {"hostname": None, "bots": []}
    return parse(getattr(done, "stdout", ""))


def scan(machine, run=subprocess.run) -> list[dict]:
    """The bots living on ``machine`` (see ``parse``); [] when unreachable or the output is bad."""
    return probe(machine, run=run)["bots"]


def _spawn(fn) -> None:
    threading.Thread(target=fn, name="flota-remote-crew", daemon=True).start()


class RemoteCrews:
    """Per-machine cache of ``probe`` results, refreshed off the request thread.

    ``get(machine, up)`` returns the last result (or None) at once. When the machine is up and the
    result is missing or older than ``ttl``, one refresh is started with ``spawn`` (a daemon
    thread; tests pass ``lambda fn: fn()``). A machine that is down is never scanned; its last
    result is kept so its bots still show, asleep."""

    def __init__(self, *, probe_fn=None, clock=time.monotonic, spawn=_spawn, ttl: float = TTL):
        self._probe = probe_fn or probe
        self._clock, self._spawn, self._ttl = clock, spawn, ttl
        self._lock = threading.Lock()
        self._results: dict[tuple, tuple[float, dict]] = {}
        self._running: set[tuple] = set()

    @staticmethod
    def key(machine) -> tuple:
        return (machine.name, machine.user, machine.host, machine.port, machine.key)

    def get(self, machine, up: bool) -> dict | None:
        key = self.key(machine)
        now = self._clock()
        with self._lock:
            hit = self._results.get(key)
            stale = hit is None or now - hit[0] >= self._ttl
            start = up and stale and key not in self._running and getattr(machine, "os", "linux") != "windows"
            if start:
                self._running.add(key)
        if start:
            def refresh():
                try:
                    result = self._probe(machine)
                except Exception:
                    result = {"hostname": None, "bots": []}
                with self._lock:
                    old = self._results.get(key)
                    if result.get("bots") or result.get("hostname") or old is None:
                        self._results[key] = (self._clock(), result)
                    else:  # a failed scan keeps the last good list, retried after ttl
                        self._results[key] = (self._clock(), old[1])
                    self._running.discard(key)
            try:
                self._spawn(refresh)
            except Exception:
                with self._lock:
                    self._running.discard(key)
        with self._lock:
            hit = self._results.get(key)
        return hit[1] if hit else None

    def clear(self) -> None:
        with self._lock:
            self._results.clear()
            self._running.clear()


def crew_entries(machine, result: dict | None, *, up: bool) -> list[dict]:
    """``result`` bots as /status crew entries of ``machine``."""
    if not result:
        return []
    host = result.get("hostname")
    out = []
    for bot in result.get("bots") or []:
        if not up:
            state = "asleep"
        elif bot["active_agents"] > 0:
            state = "working"
        elif bot["gateway_alive"]:
            state = "awake"
        else:
            state = "asleep"  # the bot exists but its gateway is not running
        avatar = f"data:{bot['mime']};base64,{bot['avatar_b64']}" if bot.get("avatar_b64") else None
        out.append({"id": f"{machine.name}/{bot['id']}", "profile": bot["id"], "name": bot["name"],
                    "avatar": avatar, "color": bot["color"], "state": state, "remote": True,
                    "machine": machine.name, "hostname": host})
    return out


def merge(existing: list[dict], remote: list[dict]) -> list[dict]:
    """``existing`` (ship hand, SSH profiles, pairing ``crew_names``) plus ``remote``, deduped by id.
    A ``crew_names`` placeholder (not a ship hand, no avatar) whose name a scanned bot carries is
    replaced by that bot."""
    names = {bot["name"] for bot in remote}
    kept = [b for b in existing
            if b.get("ship_hand") or not (str(b.get("id", "")).count("/") == 1 and b.get("name") in names)]
    ids = {b.get("id") for b in kept}
    for bot in remote:
        if bot["id"] not in ids:
            ids.add(bot["id"])
            kept.append(bot)
    return kept
