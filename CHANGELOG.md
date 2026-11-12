# Changelog

All notable changes to Flota. Versions follow `plugin.yaml`.

## 0.2.2 — 2026-11-11

Changes asked for in the catalog review ([PR #153549](https://github.com/NousResearch/hermes-agent/pull/153549)).

### Changed

- **No more core patching.** Flota no longer wraps core's SSH terminal constructor to turn off SSH file sync. It writes `terminal.ssh_sync_files: false` into every machine profile it creates (`setup`, `add-ship`, the Flota page), and `hermes flota doctor --fix` writes it into existing ones. **Hermes 0.23.1 does not read that key yet** ([issue #148726](https://github.com/NousResearch/hermes-agent/issues/148726), PRs #148791 / #145271), so until a core release does, subagents on a Flota machine sync files again: slow on a machine with its own Hermes install. See [troubleshooting.md](docs/troubleshooting.md#subagents-on-a-flota-machine-are-slow-to-finish).
- `doctor`: `✗ profile ... does not set terminal.ssh_sync_files: false` (fixed by `--fix`), and a `!` warning, which doesn't fail the run, while the core still syncs. The old `✗ SSH file sync is on` line is gone.
- **The agent tool offers only `status` and `wake` by default.** New panel option `agent_power_actions` (default `false`) adds `sleep` and `off`; while it is off, the handler refuses them and says how to turn them on.
- **Every agent `sleep` / `off` asks you first.** A `pre_tool_call` hook sends those calls to Hermes' approval prompt, naming the machine and the action and warning that whoever uses it loses their session. "Always" covers one machine and action. With nobody to answer, Hermes refuses. `/flota`, `hermes flota sleep|off` and the Flota page are unchanged.
- README: the pairing key is created without a passphrase, and a ship authorises it with no restrictions.
- The `plugin-catalog/` directory left the repository; the catalog entry lives in the catalog PR.

### Removed

- `ssh_guard.py` (replaced by `ssh_sync.py`, which only reads and writes config).

## 0.2.1 — 2026-11-11

### Added

- `hermes flota setup USER@HOST`: the machine name is taken from the host.
- `hermes flota remove <machine> [--force] [--keep-profile]`: forget a machine and delete the `flota-*` profile Flota created for it.
- `hermes flota uninstall [--yes]`: lists, then undoes, what Flota did on this machine (machines, Flota-made profiles, `flota-flagship:` key lines) before `hermes plugins remove flota`.
- `hermes flota add-ship <code> --replace`, and "This machine really moved - replace it" on the page.
- Bare `hermes flota` prints where this Hermes stands and the next command; commands end with a `Next:` hint. `invite` and `join` print paste-ready `hermes flota join ...` / `hermes flota add-ship ...` lines.
- `/flota sleep|off <machine> force` for a person in a chat.
- Flota page: choose among several connections that run Flota; opening the flagship automatically is opt-in and can be turned off in the header; optional ship name when adding by code; the Wake-on-LAN MAC is detected over SSH.

### Changed

- The agent tool has no `force` option any more: a busy machine is always refused for the model. Only a person can force it (CLI, `/flota ... force`, the page).
- A machine with `wake: none` (and no `wake_timer`) defaults to `sleep: none`: Flota no longer auto-sleeps what it can't turn on again.
- REST API: every POST must be `application/json` (415 otherwise); `GET /invite` is read-only and `POST /invite` mints; `POST /join` needs `confirm` (flagship name or key fingerprint); `POST /ships` accepts `name` and `replace`; sleep/off take `{"force": true}` in the body.
- `setup` on a machine that doesn't answer says `Saved ... but NOT verified`; invalid input exits 2 with `Nothing was written.`

### Fixed

- Idle watch: a machine woken by the tool, `/flota wake`, its wake timer, or left busy by a gateway that restarted (picked up on the first delegation) now goes back to sleep after a full `sleep_after`, never while busy.
- A `default` machine is also woken for tasks that name no machine.
- A failed auto-sleep is retried once.
- The SSH file sync guard covers any configured machine profile, not only `flota-*` names.
- Hosts and users starting with `-` are rejected everywhere.
- A ship code can no longer silently re-point an existing machine to a new address or user.
- Crew data returned by ships is sanitised before it reaches the page.
- `leave` removes stray `flota-flagship:` key lines even when no fleet is recorded.
- Docs: install-scan output, the Windows undo steps, the README screenshot caption and the CLI reference now match the code.

## 0.2.0 — 2026-11-10

### Added

- **Fleet pairing.** `hermes flota invite` / `join` / `join-check` / `add-ship` / `leave`. A ship joins with two copy-paste codes: public keys only, invites valid for 30 minutes, one ship per invite.
- **Fleet roles.** `hermes flota role [flagship|ship|none] [--force]` covers the whole lifecycle: create a fleet, join, leave, rejoin, become a flagship.
- **Flota page** for Hermes Desktop and the Webapp: a naval map with the flagship and its ships, crews (the bots on each machine, discovered over SSH), a click-to-chat bot list, the flagship's cron **Schedule** with the ship each job wakes, and invite/join wizards. If the current connection has no Flota, the page finds the flagship on another one.
- **Web dashboard tab** with a live network map, busy counts, and a confirmation before sleeping a busy machine.
- **Wake timers** (`wake: timer`, `wake_timer`, `timer_lead`). A ship that nothing can wake arms its own RTC alarm (Linux), `pmset` wake (macOS) or WakeToRun scheduled task (Windows) for its next cron job before it sleeps.
- **Windows ships** (experimental): suspend, hibernate, power off and wake timers; a setup checklist in `docs/windows.md`.
- **macOS and Windows power control** (experimental).
- **Zero-config setup.** `hermes flota setup` detects the OS, LAN interface, MAC, broadcast address, Wake-on-LAN state and how the machine can suspend (directly, through a niri session, or with a printed polkit rule).
- **`hermes flota doctor [--fix]`** checks every machine and prints a fix for each failed line.
- **Busy state.** The gateway publishes busy counts per machine to `<hermes home>/flota/state.json`. `hermes flota sleep|off` and the dashboard refuse a busy machine without `--force`, and `status` shows the counts.
- **Power off only for machines Flota can wake again** (`wol`, `wol_via`, `command`), behind the `allow_power_off` switch.
- The `/status` endpoint reports whether the gateway is up, so the flagship's crew can be shown.

### Fixed

- `flota-*` profiles no longer sync the remote `~/.hermes` back after each subagent (SSH file sync guard).
- No delegation onto a machine that is going to sleep: a route waits for a just-sent sleep to take effect, then wakes the machine.
- Pairing state and settings stay in the profile that owns the fleet, even when a request arrives scoped to a `flota-<machine>` plumbing profile.
- Removing a machine, the role or a fleet now really removes it from the config (atomic replace).
- `add-ship` updates a machine already in the fleet (same MAC or address) instead of adding a duplicate.
- A host or user that ssh would read as an option is refused before `setup` writes the profile.
- Machine commands exit 1 on failure and 2 for an unknown machine.

## 0.1.0

Initial release.

- `flota` delegation provider: a `machine` field per `delegate_task` task, wake before routing, one fallback.
- Wake methods `wol`, `wol_via`, `command`; readiness probe on the SSH port.
- Idle auto-sleep after the last subagent (`subagent_start` / `subagent_stop` hooks); remote suspend, hibernate and power off.
- `/flota` command, `hermes flota` CLI, `flota` agent tool, settings panel.
