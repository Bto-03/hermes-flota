# Flota

![Flota: your machines as a fleet, woken on demand](docs/cover-wide.png)

**Run Hermes subagents on your own machines. Flota wakes the right one, runs the subagent there over SSH, and puts the machine back to sleep after the last one finishes.**

The desktop under your desk can take the heavy builds, local models and long jobs without staying on all day. Hermes stays on a small always-on host (the *flagship*); your other computers (the *ships*) sleep until a task needs them.

## Why a plugin

A skill or an MCP tool can send a magic packet. It can't do the three things Flota exists for:

1. **Choose where a subagent runs.** Flota is a `delegation.backend`. Hermes asks it to route each `delegate_task` task, and Flota answers with a Hermes profile whose terminal is SSH on the chosen machine.
2. **Wake the machine before the subagent exists.** The wake happens inside `route_task`, before Hermes builds the child, so a subagent never starts on a machine that is off.
3. **Sleep the machine after its last child.** The `subagent_start` / `subagent_stop` hooks tell Flota when a machine has nothing left to do. Flota then starts an idle timer and suspends the machine.

## Features

- **A machine per task.** `delegate_task` gets a `machine` field listing your machines and their descriptions. A task without one runs on `default` if you set one, otherwise on the normal route. If a machine won't wake, Flota tries its `fallback` once.
- **Five wake methods:**
  - `wol`: Wake-on-LAN from the Hermes host.
  - `wol_via`: an always-on host on the machine's LAN sends the packet over SSH.
  - `command`: any local command, such as a smart plug.
  - `timer` / `wake_timer: true`: before it sleeps, the machine sets its own RTC wake alarm for its next cron job.
  - `none`: you turn it on by hand.
- **Auto-sleep that respects busy machines.** `sleep_after` the last subagent, Flota suspends, hibernates or powers off the machine. It never sleeps a machine with running subagents or tasks still being routed. The CLI and dashboard refuse too, unless you force them.
- **Power-off only when Flota can turn the machine back on** (`wol`, `wol_via`, `command`). Every other machine can only be suspended, so nothing ends up switched off with no way back.
- **Agent tool `flota`**: `status`, `wake`, `sleep`, `off`.
- **Chat command `/flota`**: `status`, `dry-run <machine>` (explains every step and sends nothing), `wake`, `sleep`, `off`.
- **CLI `hermes flota`**:
  - `setup` detects the OS, LAN interface, MAC, broadcast address, Wake-on-LAN state and how the machine can sleep.
  - `doctor [--fix]` checks each machine.
  - Fleet pairing: `invite`, `join`, `join-check`, `add-ship`, `leave`, `role`.
  - Machine commands: `status`, `dry-run`, `wake`, `sleep`, `off`.
- **Flota page for Hermes Desktop and the Webapp.** A naval map with Hermes as the flagship and your machines as ships. Each ship shows its crew (the Hermes bots that live there). The page also shows the flagship's **Schedule** of cron jobs with the ship each one wakes, invite/join flows, and a card that finds your flagship on another connection.
- **Web dashboard tab** (`hermes dashboard` → Flota) with the same map and controls.
- **Settings panel** in Plugins → Flota.
- Python standard library only.

## Screenshots

| | |
|---|---|
| ![Flota page: the flagship in the centre, ships around it](docs/flota-desktop-map.png) | ![Crew of a ship and a bot tooltip](docs/flota-crew-hover.png) |
| The fleet map with the flagship's Schedule | Each ship carries its crew; bots on connected machines open a chat |
| ![A ship selected next to the Schedule](docs/flota-schedule.png) | ![Inviting a ship](docs/flota-fleet-pairing.png) |
| A ship's details next to the Schedule | Pairing another Hermes with two codes |
| ![Create or join a fleet](docs/flota-onboarding.png) | ![Any connection finds the flagship](docs/flota-switch-to-flagship.png) |
| First run: create a fleet or join one | The web dashboard tab (`?flota_demo=1`, made-up machines) |

All screenshots use made-up demo data.

## Quick start

You need Hermes 0.22.5 or later on a Linux host, the `ssh` client, and a machine you can SSH into with a key. Make sure the machine is on for step 2.

```sh
# 1. Install and enable the plugin
hermes plugins install https://github.com/Bto-03/hermes-flota --enable

# 2. Add a machine. Flota detects its OS, LAN interface, MAC, broadcast, Wake-on-LAN state
#    and how it can sleep, and creates the SSH profile flota-desk
hermes flota setup desk --host 192.0.2.20 --user alice --key ~/.ssh/id_ed25519

# 3. Route subagents through Flota and check the machine
hermes config set delegation.backend flota
hermes flota doctor
```

If a gateway is already running, restart it (`hermes gateway restart`) so it loads the backend. Then ask the agent something like "build the project on desk". Run `/flota dry-run desk` first to see what would happen.

If the other machine runs its own Hermes with Flota, pair it with codes instead: `hermes flota invite` on this host, `hermes flota join <code>` on the other one. See [docs/pairing.md](docs/pairing.md).

## Requirements

- **Hermes host:** Hermes ≥ 0.22.5 (pluggable delegation backends), Linux, OpenSSH client. Auto-sleep timers need a long-lived process, such as the gateway.
- **Each machine:** `sshd` with your key authorised (Flota uses `BatchMode=yes` and never types a password), and a shell. It doesn't need Hermes, Python or an API key: the subagent's agent loop runs on the Hermes host, and only its terminal commands run on the machine.
- **To wake it**, one of:
  - Wake-on-LAN enabled in the firmware and on the NIC, with the Hermes host on the same LAN (`wol`).
  - An always-on host on that LAN with `python3` (`wol_via`).
  - A wake command (`command`).
  - A self-armed RTC alarm (`timer`).
- **To sleep it (Linux):** `systemctl suspend` must work over SSH without a password. `setup` checks this and prints a one-time polkit rule if it doesn't.

## Measured on real hardware

From the author's tests on 2026-11-10: a Linux home server controlling a Linux desktop, with SSH over Tailscale and Wake-on-LAN on the LAN.

- Waking the suspended desktop by Wake-on-LAN took **19-42 s** until SSH answered.
- A scheduled job woke a suspended desktop (about **27 s**), ran there, and Flota auto-suspended the desktop **2 minutes** after the job finished (`sleep_after: 2m`).
- Flota refused to put a busy machine to sleep while a subagent was running on it.

## Documentation

| | |
|---|---|
| [Installation](docs/installation.md) | Install, enable, first machine, the Flota page |
| [Configuration](docs/configuration.md) | Every panel setting and per-machine key |
| [Pairing](docs/pairing.md) | Fleet roles, invite and ship codes, what is exchanged |
| [Power](docs/power.md) | Suspend, hibernate and power off per OS, polkit, wake timers |
| [Desktop page](docs/desktop-page.md) | Enabling it, the map, crews, connection switching |
| [CLI](docs/cli.md) | Every `hermes flota` command, flag and exit code |
| [Architecture](docs/architecture.md) | Routing flow, hooks, state files |
| [Troubleshooting](docs/troubleshooting.md) | `doctor` lines and their fixes |
| [Security](docs/security.md) | Threat model: what Flota does and doesn't do |
| [Windows ship](docs/windows.md) | Checklist for a Windows 10/11 ship (experimental) |

## Platform support

| | Status |
|---|---|
| Linux (Hermes host and machines) | Supported, tested on real hardware |
| macOS machines | Experimental: `pmset sleepnow`, no on-demand hibernate; unit-tested only |
| Windows machines | Experimental: `SetSuspendState`, `shutdown /h`, `shutdown /s`, scheduled-task wake timers; unit-tested only |

The Hermes host itself is supported on Linux only. Wake-on-LAN works with any OS whose NIC supports it.

## Security

- Pairing exchanges **public keys only**. An invite expires after 30 minutes, and the flagship accepts one ship per invite.
- Flota never asks for a password and never runs an interactive `sudo`. It prints the one-time root steps it needs (a polkit rule, enabling Wake-on-LAN, wake-timer permission) for you to run yourself. The only privileged command Flota runs is `sudo -n` for the wake alarm (`rtcwake` / `pmset relative wake`), and only on a machine where you added that sudoers rule yourself.
- The dashboard and the Flota page can't set shell-bearing keys (`wake_command`, `sleep_command`, `power_prefix`) or a machine's `profile`. Those come only from `config.yaml` or `setup`.
- Install scan: the Hermes plugin scanner reports findings about Flota's SSH key handling and its printed admin hints. Read them with `hermes plugins validate <path>`.

Full threat model: [docs/security.md](docs/security.md). To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). No test touches the network or a real machine. [CHANGELOG.md](CHANGELOG.md) lists the changes in each release.

## License

MIT © Francisco Bastidas. See [LICENSE](LICENSE).
