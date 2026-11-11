# CLI: `hermes flota`

```text
hermes flota {status | dry-run | wake | sleep | off <machine> |
              setup <name> --host HOST --user USER [options] | doctor [machine] [--fix] |
              invite | join <invite> [--yes] | join-check | add-ship <ship code> [--name N] |
              leave [flagship] | role [flagship|ship|none] [--force]}
```

Every command acts on the **active profile** (`hermes -p <profile> flota ...` for another one). If the active profile is a `flota-<machine>` plumbing profile, Flota acts on the profile that owns it.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | The action failed: the machine didn't wake, sleep or power off; `doctor` found a problem; refused (busy machine, role conflict, already in a fleet); the config couldn't be read or written |
| `2` | Bad input: an unknown machine; `setup` validation failed (nothing written); an invalid, expired or already-used pairing code; `leave` when not in that fleet |

Errors go to stderr, prefixed `✗`.

## Machine commands

| Command | What it does |
|---|---|
| `status` | One line per machine: up/down, wake method, profile, fallback, auto-sleep. With a live gateway each line ends in `· busy N`. |
| `dry-run <machine>` | Explains what delegating to the machine would do right now, step by step. Sends nothing. |
| `wake <machine>` | Wakes the machine and waits until its SSH port answers (up to `wake_timeout`). Exit 0 when it's up. |
| `sleep <machine> [--force]` | Puts it to sleep now with its `sleep` action (`suspend` when that's `none`). |
| `off <machine> [--force]` | Powers it off. Allowed only for machines Flota can wake again (`wol`, `wol_via`, `command`) and while "Allow power off" is on. |

`sleep` and `off` refuse a machine that the running gateway reports busy (exit 1: `✗ desk is busy with 1 subagent(s); add --force to sleep it anyway`). `--force` skips that check. Without a live gateway there are no busy counts, so nothing is refused. See [power.md](power.md#busy-machines-are-never-put-to-sleep).

`/flota` in a chat accepts the same subcommands (`status`, `dry-run`, `wake`, `sleep`, `off`). Because it runs inside the process that counts subagents, it always refuses a busy machine; there is no `--force`.

## `setup`

Creates (or updates) the profile `flota-<name>`, whose terminal is SSH on the machine, and saves the machine in the Flota panel. The steps are described in [installation.md](installation.md#2-add-your-first-machine).

| Flag | Default | |
|---|---|---|
| `name` (positional) | — | Machine name. The profile is `flota-<name>`, unless the machine already has a profile. |
| `--host HOST` | **required** | Address the machine answers SSH on |
| `--user USER` | **required** | SSH user |
| `--key PATH` | ssh's defaults | SSH private key; also written to the profile's `terminal.ssh_key` |
| `--port N` | `22` | SSH port |
| `--description TEXT` | — | One line the agent reads about this machine |
| `--wake {none,wol,wol_via,command,timer}` | `wol` when a MAC is detected, else `none` | `wol_via` and `command` also need `via` / `wake_command`, which have no flag: add them in Plugins → Flota or `config.yaml`, then run `setup` again |
| `--mac MAC` | detected | MAC for Wake-on-LAN |
| `--broadcast ADDR` | detected, else `255.255.255.255` | Broadcast address for Wake-on-LAN |
| `--sleep {none,suspend,hibernate,poweroff}` | the panel's `sleep_action` | How auto-sleep turns it off. With `none`, the sleep check is skipped. |
| `--sleep-after DURATION` | the panel's `sleep_after` | e.g. `90s`, `10m`, `1h` |
| `--os {linux,macos,windows}` | detected | Picks the power commands |
| `--no-detect` | off | Don't probe the machine over SSH for its OS, MAC and broadcast address |

Detection runs only when the machine's SSH port answers. Otherwise `setup` saves what you gave it and tells you to run it again once the machine is on. Flags always win over detected values.

Exit: 0 when saved; 2 for invalid values or a refused profile name (nothing written); 1 when the config can't be written (a managed install, an unreadable file) or the profile clone failed.

## `doctor [machine] [--fix]`

Checks every machine (or one) and prints a `→` fix under each failed line:

```text
desk  alice@192.0.2.20:22
  ✓ SSH reachable
  ✓ profile flota-desk → ssh alice@192.0.2.20
  ✓ SSH file sync off for flota profiles (the machine's ~/.hermes is never copied)
  ✓ wake: wol (mac aa:bb:cc:dd:ee:ff, broadcast 192.0.2.255)
  ✓ Wake-on-LAN enabled on enp3s0
  ? power off: waking from power-off (S5) needs Wake-on-LAN from S5 enabled in the BIOS (...)
  ✓ can sleep without a password (systemctl works directly)
  ✓ this host is on the machine's LAN (192.0.2.0/24)
```

`✓` passed, `✗` failed, `?` couldn't be checked. Config problems (skipped machines, values replaced by defaults) are listed first. Every line is explained in [troubleshooting.md](troubleshooting.md).

`--fix` first applies the safe fixes: it hides every `flota-*` profile from the Desktop Bots roster.

Exit: 0 when everything passed; 1 when something failed or no machines are configured; 2 for an unknown machine name.

## Pairing

| Command | Side | What it does | Exit |
|---|---|---|---|
| `invite` | flagship | Prints an invite code valid for 30 minutes | 0 |
| `join <invite> [--yes]` | ship | Shows the flagship, user and key fingerprint, asks `[y/N]` (`--yes` skips the question; without a terminal and without `--yes` it refuses), adds the key, prints the reachability checks and the ship code | 0; 1 if declined or already in another fleet; 2 for a bad or expired invite |
| `join-check` | ship | Runs only the reachability checks | 0; 1 if any check failed |
| `add-ship <ship code> [--name N]` | flagship | Finds an address that logs in and runs `setup` on it. A ship already in the fleet (same MAC or address) is updated; `--name` names a new one | 0; 1 if no address logged in or setup failed; 2 for a bad, foreign, expired or used code |
| `leave [flagship]` | ship | Removes that flagship's key line(s) and forgets the fleet (default: the current fleet) | 0; 2 when not in that fleet |
| `role` | either | Shows the role (`(inferred)` if not chosen), the fleet and the number of ships | 0 |
| `role flagship\|ship\|none [--force]` | either | Chooses the role. `ship` while leading ships, or `flagship` while in a fleet, needs `--force` (removes the ships but keeps their profiles / leaves the fleet) | 0; 1 on a conflict without `--force` |

Details: [pairing.md](pairing.md).
