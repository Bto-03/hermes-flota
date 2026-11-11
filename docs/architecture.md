# Architecture

Flota is a standalone Hermes plugin with no Python dependencies beyond the standard library. `register(ctx)` in `__init__.py` wires up:

| Registration | Module | Purpose |
|---|---|---|
| `register_delegation_provider` | `provider.py` | The `flota` delegation backend: `task_parameters`, `description`, `route_task` |
| `register_hook("subagent_start" / "subagent_stop")` | `idle.py` | Busy counts and idle timers |
| `register_tool("flota")` | `tool.py` | Agent tool: `status`, `wake`, `sleep`, `off` (offered while `agent_tool` is on) |
| `register_command("flota")` | `slash.py` | `/flota` in chats |
| `register_cli_command("flota")` | `cli.py` | `hermes flota ...` |
| `ssh_guard.install()` | `ssh_guard.py` | Turns off SSH file sync for `flota-*` profiles |

The dashboard tab and the Flota page are served by `dashboard/plugin_api.py` (a FastAPI router loaded by Hermes from `dashboard/manifest.json`). It reuses the same modules.

If the Hermes core has no `agent.delegation_provider`, `register` logs a warning and returns without registering anything.

## Routing a task

```mermaid
sequenceDiagram
    participant A as Parent agent
    participant H as Hermes core
    participant F as Flota provider
    participant S as IdleSleeper
    participant M as Machine (ship)

    A->>H: delegate_task(tasks=[{goal, machine: "desk"}])
    H->>F: route_task(task, config)
    F->>F: load machines (panel over delegation.backends.flota)
    F->>S: claim(desk)  (cancels idle timer, counts as busy)
    alt a sleep was sent < 60 s ago
        F->>M: wait up to 20 s for SSH to stop answering
    end
    F->>M: TCP probe host:port
    alt down
        F->>M: wake (wol / wol_via / command), resend every 10 s
        F->>M: probe every 2 s until up or wake_timeout
    end
    alt desk did not come up
        F->>M: same for desk.fallback (once)
        F-->>H: DelegationRouteError "Resend this task: use another machine ... or omit machine"
    end
    F->>S: routed(desk, task)  (expects a child with this goal)
    F->>S: release(desk)
    F-->>H: {profile: "flota-desk", label: "flota:desk"}
    H->>H: build child with profile flota-desk (terminal = SSH on desk)
    H->>S: subagent_start(child_goal, child_session_id)
    Note over H,M: the child's agent loop runs on the Hermes host,<br/>its terminal commands run on desk over SSH
    H->>S: subagent_stop(child_session_id)
    S->>S: busy(desk) == 0 → start sleep_after timer
    S->>M: (timer fires, still idle) [arm wake timer] → suspend over SSH
```

Notes:

- **The schema.** `task_parameters` adds a `machine` string enum, built from the loaded machines and their descriptions, to `delegate_task`'s task schema. A task without `machine` uses `default` when it's set to a loaded machine; otherwise `route_task` returns `{}` and Hermes uses its normal route.
- **One wake per machine.** A per-machine lock means two sessions asking for the same sleeping machine send one wake; the second finds the machine already up.
- **Fallback once.** If `machine` doesn't come up, its `fallback` is tried. The fallback's own fallback is never followed. The error lists the other machines, so the agent can resend the task.
- **Guidance.** `description()` tells the agent that tasks for different machines run in parallel, and that a subagent silent for longer than `cache_ttl` should be replaced rather than revived.

## Busy state

`IdleSleeper` (one per process) tracks, per machine:

- `_claims`: routes being prepared (from `claim` until `release`).
- `_expected`: tasks routed whose child hasn't started yet, as `(goal, machine, time)`. The hooks carry no route label, so a child is matched to its machine by **goal text**. An expectation older than 10 minutes counts as a child that never started.
- `_running`: `child_session_id → machine` between `subagent_start` and `subagent_stop`.

`busy(machine)` is the sum of the three. Auto-sleep, `/flota sleep|off` and the agent tool all go through the sleeper and refuse when it's non-zero.

Whenever the counts change, the sleeper writes them atomically to `<hermes home>/flota/state.json`:

```json
{"pid": 12345, "updated": 1794355200.0, "busy": {"desk": 1}}
```

The CLI (`hermes flota sleep|off|status`) and the dashboard run in other processes and read this file (`idle.read_busy`). They trust it only if `pid` is alive and `updated` is less than a day old; otherwise "busy unknown". One state file per profile: two processes running subagents at once (a gateway and a chat session) overwrite each other's counts.

## Owner scope

`hermes flota setup` clones each `flota-<machine>` profile from the profile that owns the fleet, so the plumbing profile's config also enables Flota. The desktop app sends plugin requests to the profile you last chatted with, which may be a plumbing profile if you opened a ship bot's chat. Without a guard, Flota would read and write a stale copy of the fleet there.

`home.py` maps every Hermes home to its **owner**. A `<root>/profiles/flota-*` home maps to the profile named in its `flota/owner` file (written at setup), else to the root Hermes home. Every other home owns itself. Config reads and writes, invites, busy state, wake-timer state, cron reads and profile clones all run inside `home.owner_scope()`. That scope sets core's context-local Hermes-home override and never touches environment variables.

## SSH file sync guard

Core's SSH terminal (`tools/environments/ssh.py`, `SSHEnvironment`) mirrors local skills, credentials and caches into `<remote $HOME>/.hermes` when it connects. On cleanup it tars that whole directory back (120 s timeout, three tries). That suits a throwaway box, but a Flota machine is one of your own computers: its `~/.hermes` may be a real Hermes install of many GB. The push would write this host's files, credentials included, into it, and the pull would time out after holding the parent for minutes.

`ssh_guard.install()` wraps `SSHEnvironment.__init__` and passes `sync_files=False` when the active Hermes home is a `profiles/flota-*` profile, which is how core runs a Flota subagent. Other profiles are untouched. If the core's constructor has no `sync_files` parameter, the guard logs a warning and `doctor` reports `✗ SSH file sync is on`.

## Modules

| Module | Responsibility |
|---|---|
| `machines.py` | Parse and validate machine entries; never raises |
| `settings.py` | Merge the panel over the old `delegation.backends.flota` block; power-off rules |
| `wake.py` | Magic packets, `wol_via`, wake commands, `ensure_ready` (probe → wake → wait) |
| `power.py` | Power commands per OS, `ssh_argv`, polkit/niri detection, OS detection |
| `waketimer.py` | Next cron job per machine, arming RTC / `pmset` / scheduled-task alarms, capability probe |
| `idle.py` | `IdleSleeper`, `state.json` |
| `detect.py` | LAN interface, MAC, broadcast, Wake-on-LAN state (remote and local), Tailscale address |
| `pairing.py` | Invite and ship codes, `authorized_keys` add/remove, join checks, host selection |
| `crew.py`, `remote_crew.py` | Bots on the flagship and on each ship (read-only SSH scan, cached) |
| `home.py` | Owner scope; hiding plumbing profiles from the Bots roster |
| `ssh_guard.py` | SSH file sync guard |
| `cli.py`, `slash.py`, `tool.py` | User surfaces |
| `dashboard/plugin_api.py` | REST backend for the dashboard tab and the Flota page |

## Known limits

- Only one delegation backend can be active: `delegation.backend: flota` replaces any other.
- Auto-sleep timers live in the running process, so auto-sleep needs a long-lived gateway.
- Hermes routes the tasks of one `delegate_task` call one after another, so a slow wake delays the later tasks of that batch.
- Children are matched to machines by goal text. Identical goals sent to different machines in one batch can be swapped.
