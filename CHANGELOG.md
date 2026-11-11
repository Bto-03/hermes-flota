# Changelog

All notable changes to Flota. Versions follow `plugin.yaml`.

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
