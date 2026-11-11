# Security

Flota gives the Hermes host (the flagship) SSH access to your other machines and the ability to wake them, suspend them and power them off. This page describes what it trusts, what it does on each machine, and what it deliberately doesn't do.

## Assets and trust

| Party | Trusted to |
|---|---|
| The flagship's user and Hermes process | Run subagents on every ship, as the ship user that authorised the key. **A subagent on a ship has a normal shell there.** That's the point of Flota, so authorise only machines and users you want agents to work on. |
| A ship | Answer honestly about its own facts (addresses, MAC, OS, bot names). Everything it sends back is treated as untrusted input and validated. |
| The LAN | Nothing. A magic packet only powers a machine on, and an attacker on the LAN could send one anyway. |
| The dashboard / Flota page user | Change non-shell settings, wake/sleep/off machines, pair. It can't set shell-bearing keys. |

## What Flota does on each side

**Flagship:**

- Runs `ssh -o BatchMode=yes -o ConnectTimeout=10 [-i key] [-p port] user@host <command>` for probes, power commands, OS/network detection and the read-only crew scan. `BatchMode` means ssh never prompts for a password.
- Sends UDP magic packets (`wol`), runs `ssh <via> python3 -c ...` for `wol_via`, or runs your `wake_command` / `sleep_command` with the shell. These two keys come only from `config.yaml` or the CLI.
- Writes its own panel, the `flota-<machine>` profiles, and `<hermes home>/flota/` (state, invites with mode 600, wake timers).
- May create `~/.ssh/flota_ed25519` (`ssh-keygen -t ed25519`), but only when no key is configured and `~/.ssh/id_ed25519` doesn't exist.

**Ship:**

- `join` appends **one public key line** to the ship's own `~/.ssh/authorized_keys` (comment `flota-flagship:` + flagship name), and sets `~/.ssh` to 700 and the file to 600. Nothing else in that file is touched. `leave` removes only lines with that comment.
- Over SSH, the flagship runs only:
  - power commands (`systemctl suspend|hibernate|poweroff`, `pmset sleepnow`, `shutdown /h`, ...);
  - the polkit `CanSuspend` query;
  - `uname -s || ver`;
  - network detection (`ip`, `ethtool`/`nmcli` reads, `ifconfig`);
  - the wake-timer probe and arm;
  - the crew scan, which reads `profile.yaml`, avatars and gateway state of the ship's Hermes profiles, and never `config.yaml` or credentials;
  - and the subagents' own terminal commands.

## What Flota doesn't do

- **No private keys, passwords or tokens in pairing codes.** An invite carries the flagship's *public* key; a ship code carries no key at all. Codes are base64url JSON. Every field is validated against a strict pattern, values starting with `-` are rejected (so ssh can't read them as options), and a public key with a newline or `PRIVATE KEY` is refused.
- **No interactive sudo, no password prompts.** Flota prints the root steps it needs for you to run: a polkit rule, enabling Wake-on-LAN, wake-timer permission. The only privileged command it ever runs is `sudo -n` for exactly `rtcwake -m no -s N` (Linux) or `/usr/bin/pmset relative wake N` (macOS), and only after `sudo -n -l` confirms that you installed a rule for that exact command.
- **No shell keys from the browser.** The dashboard API refuses `wake_command`, `sleep_command`, `power_prefix`, `command` and `profile`, and keeps their existing values on update.
- **No copy of your credentials onto ships.** The SSH file sync guard stops core's SSH terminal from mirroring this host's `~/.hermes` (credentials included) into a ship, and from tarring the ship's `~/.hermes` back. See [architecture.md](architecture.md#ssh-file-sync-guard).
- **No power-off without a way back.** Only machines Flota can wake again can be powered off.
- **No network services.** Flota opens no listening sockets. Its REST endpoints live inside Hermes' own dashboard/desktop backend and use its authentication.

## Pairing threats

| Threat | Mitigation |
|---|---|
| Someone gets hold of an invite | It expires after 30 minutes, the ship asks before trusting the key, and it shows the SHA256 fingerprint. Joining only lets the *flagship's* key in; it gives the holder of the invite nothing. |
| A forged or replayed ship code | The flagship accepts a ship code only for a nonce it issued itself, that hasn't expired and hasn't been spent. Each invite adds one ship. |
| A ship code pointing at someone else's host | The flagship must actually log in to that host with its own key (`ssh ... true`) before saving it. |
| A host key changes after pairing | The first contact records the host key (`StrictHostKeyChecking=accept-new`); a changed key is refused with instructions. |
| A malicious ship returning crafted data | Ship codes, crew scans and probe output are parsed defensively: bounded sizes and counts, strict patterns, unknown fields dropped. |

## The key a ship authorises

The flagship's key gets a **normal login** on the ship: no `command=` or `restrict` options, because subagents need a full shell. Treat it like any `ssh-copy-id`. To revoke: `hermes flota leave` on the ship, or delete the line whose comment is `flota-flagship:` plus the flagship's name from `authorized_keys`.

If the flagship is compromised, every ship that authorised its key is reachable as that ship user. Use a dedicated, unprivileged ship user if that matters to you.

## Install scan

Hermes scans plugins on `hermes plugins install` and `hermes plugins validate`. Flota's report contains informational findings, and every one of them reflects disclosed behaviour:

- **`ssh_backdoor` / `ssh_dir_access` / `ssh_keygen`.** Flota's job is to add and remove one `authorized_keys` line during pairing, and to create an SSH key if you have none. The code is in `pairing.py` (`authorize`, `deauthorize`, `flota_key`).
- **`sudo_usage`.** The wake-timer probe and arm use `sudo -n` with one fixed command (`waketimer.py`). The docs show root steps for you to run yourself.
- **`python_subprocess`.** Flota runs `ssh` and your configured wake/sleep commands.
- **Test fixtures.** Hostile strings (for example a MAC address with a shell command appended) that the tests prove are rejected.

Read the full report yourself with `hermes plugins validate <path to flota>`.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md).
