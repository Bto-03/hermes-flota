# CLI: `hermes flota`

```text
hermes flota {status | dry-run <machine> | wake <machine> | sleep <machine> [--force] | off <machine> [--force] |
              setup USER@HOST | setup <name> --host HOST --user USER [options] |
              remove <machine> [--force] [--keep-profile] | uninstall [--yes] | doctor [machine] [--fix] |
              invite | join <invite> [--yes] | join-check | add-ship <ship code> [--name N] [--replace] |
              leave [flagship] | role [flagship|ship|none] [--force]}
```

Bare `hermes flota` prints the usage, then where this Hermes stands and the one command to run next (for example `Next: hermes flota setup USER@HOST` with no machines, or `Next: hermes config set delegation.backend flota` when the backend is not on yet), and exits 0. Most commands also end with a `Next:` line.

Every command acts on the **active profile** (`hermes -p <profile> flota ...` for another one). If the active profile is a `flota-<machine>` plumbing profile, Flota acts on the profile that owns it.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | The action failed: the machine didn't wake, sleep or power off; `doctor` found a problem; refused (busy machine, role conflict, already in a fleet); `join` declined; the config couldn't be read or written |
| `2` | Bad input: an unknown machine (`remove`, `doctor` and the machine commands too); `setup` validation failed (`Nothing was written.`); an invalid, expired or already-used pairing code, or a ship code for a moved machine without `--replace`; `leave` when not in a fleet |

Errors go to stderr, most prefixed `✗`. `doctor` prints its whole report, failed lines included, on stdout.

## Machine commands

| Command | What it does |
|---|---|
| `status` | One line per machine: up/down, wake method, profile, fallback, auto-sleep. With a live gateway each line ends in `· busy N`. |
| `dry-run <machine>` | Explains what delegating to the machine would do right now, step by step. Sends nothing. |
| `wake <machine>` | Wakes the machine and waits until its SSH port answers (up to `wake_timeout`). Exit 0 when it's up. |
| `sleep <machine> [--force]` | Puts it to sleep now with its `sleep` action (`suspend` when that's `none`). |
| `off <machine> [--force]` | Powers it off. Allowed only for machines Flota can wake again (`wol`, `wol_via`, `command`) and while "Allow power off" is on. |

`sleep` and `off` refuse a machine that the running gateway reports busy (exit 1: `✗ desk is busy with 1 subagent(s); add --force to sleep it anyway`). `--force` skips that check. Without a live gateway there are no busy counts, so nothing is refused. See [power.md](power.md#busy-machines-are-never-put-to-sleep).

`/flota` in a chat accepts the same subcommands: `status`, `dry-run <machine>`, `wake <machine>`, `sleep <machine> [force]`, `off <machine> [force]`. It runs inside the process that counts subagents, so it refuses a busy machine unless you add the word `force`. `/flota wake` also starts the machine's idle timer, so a machine woken by hand still goes back to sleep after `sleep_after`. The agent's `flota` tool has no force option: it never stops a busy machine.

## `setup`

Creates (or updates) the profile `flota-<name>`, whose terminal is SSH on the machine, and saves the machine in the Flota panel. The steps are described in [installation.md](installation.md#2-add-your-first-machine).

`hermes flota setup alice@desk` is the short form: the name is taken from the host (`desk`; the first label of a hostname such as `desk.example.ts.net`, or the whole IP address) and printed as `Name: 'desk' (from the host; ...)`. A name with other characters is saved lowercased with `-` (`Name: 'Desk PC' is saved as 'desk-pc' ...`).

| Flag | Default | |
|---|---|---|
| `name` (positional) | — | Machine name, or `USER@HOST`. The profile is `flota-<name>`, unless the machine already has a profile. |
| `--host HOST` | **required** unless the positional is `USER@HOST` | Address the machine answers SSH on (`USER@HOST` works here too). A host or user starting with `-` is refused. |
| `--user USER` | **required** unless given as `USER@` | SSH user |
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

Detection runs only when the machine's SSH port answers. Otherwise `setup` still saves what you gave it, but says so plainly: `⚠ Saved 'desk' but NOT verified: 192.0.2.20:22 is not answering (it is off, or the address is wrong).`, followed by `Next: turn it on and run the same command again; `hermes flota remove desk` undoes it.` Flags always win over detected values.

Exit: 0 when saved (verified or not); 2 for invalid input (no host, no user, a missing `--key` file, an unusable name, invalid values, a refused profile name), always followed by `Nothing was written.`; 1 when the config can't be written (a managed install, an unreadable file) or the profile clone failed.

## `remove <machine> [--force] [--keep-profile]`

Removes the machine from the Flota panel (and from the old `delegation.backends.flota.machines` block) and deletes its `flota-*` profile if Flota created it (it carries Flota's owner marker and no other machine uses it). `--keep-profile` keeps the profile; a profile Flota didn't create is always kept, and the output says how to delete it (`hermes profile delete <profile> -y`).

A machine the running gateway reports busy is refused (`✗ desk is busy with 1 subagent(s); nothing was removed. Wait for them, or add --force.`, exit 1) unless `--force`. Exit 2 for an unknown machine.

## `uninstall [--yes]`

Undoes what Flota did **on this machine**, as the step before `hermes plugins remove flota`. Without `--yes` it only lists the plan and prints `Nothing was changed.`:

```text
Machines to forget: desk, gpu-box
Profiles Flota created, to delete: flota-desk, flota-gpu-box
Flagship login keys to take back: none
Nothing was changed.
Next: hermes flota uninstall --yes
```

With `--yes` it forgets every machine, the joined fleets and the role, deletes the `flota-*` profiles Flota created, and removes the `flota-flagship:` key lines from this account's `authorized_keys`. It refuses while any machine is busy (exit 1, nothing changed). It does **not** delete keys or `known_hosts` lines, and says so: the key pair `~/.ssh/flota_ed25519` if Flota made one, the ships' `known_hosts` lines, and this flagship's line on each ship (run `hermes flota leave` there). It ends with `Next: hermes config set delegation.backend default && hermes plugins remove flota` (the first part only when the backend is `flota`).

## `doctor [machine] [--fix]`

Checks every machine (or one) and prints a `→` fix under each failed line:

```text
desk  alice@192.0.2.20:22
  ✓ SSH reachable
  ✓ profile flota-desk → ssh alice@192.0.2.20
  ! SSH file sync is still on: this Hermes does not read terminal.ssh_sync_files yet, so each subagent copies this host's ~/.hermes to the machine and, when it ends, tars the machine's whole ~/.hermes back (slow, can time out)
      → fixed by a Hermes version that includes terminal.ssh_sync_files (https://github.com/NousResearch/hermes-agent/issues/148726); Flota already sets the key in the machine's profile
  ✓ wake: wol (mac aa:bb:cc:dd:ee:ff, broadcast 192.0.2.255)
  ✓ Wake-on-LAN enabled on enp3s0
  ? power off: waking from power-off (S5) needs Wake-on-LAN from S5 enabled in the BIOS (...)
  ✓ can sleep without a password (systemctl works directly)
  ✓ this host is on the machine's LAN (192.0.2.0/24)
```

`✓` passed, `✗` failed, `?` couldn't be checked, `!` a warning that doesn't fail the run (here: Hermes 0.23.1 still syncs files over SSH; with a core that reads `terminal.ssh_sync_files` the line becomes `✓ SSH file sync off`). Config problems (skipped machines, values replaced by defaults) are listed first. Every line is explained in [troubleshooting.md](troubleshooting.md).

`--fix` first applies the safe fixes: it hides every `flota-*` profile from the Desktop Bots roster, and writes `terminal.ssh_sync_files: false` into each configured machine's profile that lacks it.

Exit: 0 when everything passed; 1 when something failed or no machines are configured; 2 for an unknown machine name.

## Pairing

| Command | Side | What it does | Exit |
|---|---|---|---|
| `invite` | flagship | Prints an invite code valid for 30 minutes, single use, and the paste-ready line `hermes flota join <invite> --yes` for the other machine | 0 |
| `join <invite> [--yes]` | ship | Shows the flagship, user and key fingerprint, asks `[y/N]` (`--yes` skips the question; without a terminal and without `--yes` it refuses), adds the key, prints the reachability checks and the paste-ready line `hermes flota add-ship <ship code>` for the flagship | 0; 1 if declined (`Not joined. Nothing was changed.`) or already in another fleet; 2 for a bad or expired invite |
| `join-check` | ship | Runs only the reachability checks | 0; 1 if any check failed |
| `add-ship <ship code> [--name N] [--replace]` | flagship | Finds an address that logs in and runs `setup` on it. A ship already in the fleet (same MAC or address) is updated, but only if it logs in on an address and user that machine already has; otherwise it is refused (`Nothing was changed.`) unless `--replace`, because the MAC and addresses in a code are self-reported. `--name` names a new one | 0; 1 if no address logged in or setup failed; 2 for a bad, foreign, expired or used code, or a moved machine without `--replace` |
| `leave [flagship]` | ship | Removes that flagship's key line(s) (comment `flota-flagship:<name>`) and forgets the fleet (default: the current fleet). With no fleet on record (the plugin was removed and installed again) it still removes stray `flota-flagship:` lines | 0; 2 when there is nothing to leave |
| `role` | either | Shows the role (`(inferred)` if not chosen), the fleet and the number of ships | 0 |
| `role flagship\|ship\|none [--force]` | either | Chooses the role. `ship` while leading ships, or `flagship` while in a fleet, needs `--force` (removes the ships but keeps their profiles / leaves the fleet) | 0; 1 on a conflict without `--force` |

Details: [pairing.md](pairing.md).
