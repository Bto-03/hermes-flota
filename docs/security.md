# Security

Flota gives the Hermes host (the flagship) SSH access to your other machines and the ability to wake them, suspend them and power them off. This page describes what it trusts, what it does on each machine, and what it deliberately doesn't do.

## Assets and trust

| Party | Trusted to | Not trusted to |
|---|---|---|
| The flagship's user and Hermes process | Run subagents on every ship, as the ship user that authorised the key. **A subagent on a ship has a normal shell there.** That's the point of Flota, so authorise only machines and users you want agents to work on. | — |
| The model / agent (and any text injected into its context) | Pick a machine for a task; `status`, `wake`, `sleep`, `off` through the `flota` tool. | Stop a busy machine: the tool has **no force option**, and a busy machine is always refused (`... so it was left on. Only the user can stop it anyway: ...`). Power off a machine Flota can't wake again, or while `allow_power_off` is off. Change any setting. |
| You, typing | Force a busy machine down: `--force` on the CLI, `force` in `/flota sleep <machine> force` (or `off`), or the confirmation on the Flota page. | — |
| A pasted pairing code | Carry a public key (invite) or self-reported addresses, MAC, OS and bot names (ship code). | Re-point a machine already in the fleet: if a ship code matches an existing machine (same MAC or address) but logs in on an address or user that machine doesn't have, `add-ship` refuses (`Nothing was changed.`) unless you pass `--replace` (CLI) or press "This machine really moved - replace it" (page). Add an unintended host: the flagship must log in to it with its own key first. |
| A ship | Answer about its own facts. | Anything else: crew data it returns (bot names, avatars, gateway state) is size-bounded, pattern-checked and sanitised (`crew.clean_crew`) before it reaches the page; probe output is parsed defensively. |
| The LAN | Nothing. A magic packet only powers a machine on, and an attacker on the LAN could send one anyway. | — |
| The dashboard / Flota page user | Change non-shell settings, wake/sleep/off machines, pair. | Set shell-bearing keys (`wake_command`, `sleep_command`, `power_prefix`, `command`) or `profile`. |
| Another website open in your browser | Nothing. | Trigger an action: see [CSRF](#csrf-and-the-rest-api). |

## What Flota does on each side

**Flagship:**

- Runs `ssh -o BatchMode=yes -o ConnectTimeout=8-10 [-i key] [-p port] user@host <command>` for probes, power commands, OS/network detection and the read-only crew scan. `BatchMode` means ssh never prompts for a password.
- Sends UDP magic packets (`wol`), runs `ssh <via> python3 -c ...` for `wol_via`, or runs your `wake_command` / `sleep_command` with the shell. These two keys can be set only in `config.yaml`: neither the CLI nor the dashboard writes them. Hosts and users starting with `-` are refused everywhere, so ssh never reads them as options.
- Writes the files listed in [Files written](#files-written).
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
- **No machine stopped by the model.** The agent tool can't force a busy machine; only a person can (CLI, `/flota ... force`, the page).
- **No copy of your credentials onto ships.** The SSH file sync guard stops core's SSH terminal from mirroring this host's `~/.hermes` (credentials included) into a ship, and from tarring the ship's `~/.hermes` back. It covers every profile Flota runs subagents as: `flota-*`, a profile marked at setup, or any configured machine's `profile`. See [architecture.md](architecture.md#ssh-file-sync-guard).
- **No power-off without a way back.** Only machines Flota can wake again can be powered off.
- **No network services.** Flota opens no listening sockets. Its REST endpoints live inside Hermes' own dashboard/desktop backend and use its authentication.

## Pairing threats

| Threat | Mitigation |
|---|---|
| Someone gets hold of an invite | It expires after 30 minutes, the ship asks before trusting the key, and it shows the SHA256 fingerprint. Joining only lets the *flagship's* key in; it gives the holder of the invite nothing. |
| A forged or replayed ship code | The flagship accepts a ship code only for a nonce it issued itself, that hasn't expired and hasn't been spent. Each invite adds one ship. |
| A ship code pointing at someone else's host | The flagship must actually log in to that host with its own key (`ssh ... true`) before saving it. |
| A ship code claiming to be a machine you already have (same MAC or address) | Accepted as an update only when it logs in on an address and user that machine already has. Otherwise refused unless you confirm with `--replace` / "This machine really moved - replace it". |
| A web page tricking the ship's browser into joining a fleet | `POST /join` needs `confirm`: the invite's flagship name or key fingerprint, as you saw it; without it nothing is trusted (409). |
| A host key changes after pairing | The first contact records the host key (`StrictHostKeyChecking=accept-new`); a changed key is refused with instructions. |
| A malicious ship returning crafted data | Ship codes, crew scans and probe output are parsed defensively: bounded sizes and counts, strict patterns, unknown fields dropped. |

## The key a ship authorises

The flagship's key gets a **normal login** on the ship: no `command=` or `restrict` options, because subagents need a full shell. Treat it like any `ssh-copy-id`. To revoke: `hermes flota leave` on the ship, or delete the line whose comment is `flota-flagship:` plus the flagship's name from `authorized_keys`.

If the flagship is compromised, every ship that authorised its key is reachable as that ship user. Use a dedicated, unprivileged ship user if that matters to you.

## CSRF and the REST API

The REST endpoints live inside Hermes' own dashboard/desktop backend and use its authentication. On top of that:

- **Every POST must be `Content-Type: application/json`**, otherwise 415. A cross-site page can't send that without a CORS preflight, so a plain form post or `<img>` can't wake, sleep or power off a machine, pair, or add a ship.
- **No GET changes anything.** `GET /invite` only returns the last unexpired invite minted in this process; `POST /invite` mints one.
- `POST /join` needs `confirm` (see above); `POST /ships` re-points a moved machine only with `replace: true`.

## Files written

| File | Where | Mode |
|---|---|---|
| Flota panel (`plugins.entries.flota.settings`) | the active profile's `config.yaml` | unchanged (core's atomic config write) |
| `flota-<machine>` profiles, and `flota/owner` inside each | `<hermes root>/profiles/` | core's defaults |
| `invites.json` (invite nonces) | `<hermes home>/flota/` | **600** |
| `state.json` (busy counts), `waketimers.json` | `<hermes home>/flota/` | process umask (no secrets: machine names and counts) |
| One public key line, comment `flota-flagship:NAME` | the ship's `authorized_keys` (SSH folder) | file 600, `~/.ssh` 700 |
| `flota_ed25519`, `flota_ed25519.pub` (only if no key exists) | `~/.ssh/` on the flagship | `ssh-keygen`'s (private key 600) |
| `FlotaWake` scheduled task (Windows ships with a wake timer) | the ship's Task Scheduler, own account | — |

Flota writes nothing as root. `hermes flota uninstall` lists and removes what it added; see [installation.md](installation.md#uninstalling).

## Install scan

Hermes scans plugins on `hermes plugins install` and `hermes plugins validate`. Real output of `hermes plugins validate` for this repository (Hermes v0.23.1, release 0.2.1; line numbers may move):

```text
✓ security scan — safe
✓ no core override — no runtime rebinds of Hermes core
✓ desktop surface — stays inside the plugin SDK surface
⚠ private core imports (catalog rule 9: public surfaces only; these can break on
any release): cli.py:71 hermes_cli.profiles.profile_exists; crew.py:80
hermes_yaml; dashboard/plugin_api.py:422 cron.jobs; home.py:81
utils.atomic_yaml_write; ssh_guard.py:55 tools.environments.ssh.SSHEnvironment;
cli.py:76 hermes_cli.profiles.get_profile_dir; cli.py:118
hermes_cli.profiles.create_profile; cli.py:134
hermes_cli.profiles.delete_profile; crew.py:255 hermes_cli.profiles;
dashboard/plugin_api.py:250 hermes_yaml; and 4 more. Use ctx.* / documented
plugin APIs, or open an issue for the seam you need.
...
Validation passed.
```

(The other checks, schema, loadable, declared tools/hooks and so on, all pass with `✓`.) The scan verdict is `safe`; it still records medium-level notes for what Flota really does with SSH keys:

- **SSH folder access.** `join` looks for this machine's own public keys (`id_ed25519.pub`, `flota_ed25519.pub`) to refuse joining a fleet with its own key, and `uninstall` prints which key files it leaves for you. Pairing itself adds and removes one `authorized_keys` line and may create an SSH key (`pairing.py`: `authorize`, `deauthorize`, `flota_key`), as described on this page.
- **Private core imports (the one warning).** `hermes_cli.profiles` (create/find/delete profiles), `cron.jobs` (read the schedule), `utils.atomic_yaml_write` / `hermes_yaml` (config writes), `tools.environments.ssh.SSHEnvironment` (the file-sync guard). These are not public plugin API and may break on a core release; `doctor` reports a guard that could not install.

Other behaviour a scanner may care about, all disclosed above: `sudo -n` with one fixed command for the wake alarm (`waketimer.py`), and `subprocess` calls to `ssh` and your configured wake/sleep commands.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md).
