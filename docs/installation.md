# Installation

## Requirements

| Where | What |
|---|---|
| Hermes host | Hermes ≥ 0.22.5 (`requires_hermes` in `plugin.yaml`; it needs pluggable delegation providers), Linux, the OpenSSH client (`ssh`, `ssh-keygen`) |
| Each machine | `sshd`, a login shell, your SSH key authorised. No Hermes, Python or API key needed |
| Auto-sleep | A long-lived Hermes process (the gateway, or a long chat session): the idle timers live in that process |

Flota has no Python dependencies outside the standard library. The dashboard backend uses the FastAPI that Hermes ships.

If the Hermes core is too old, the plugin loads but logs `flota needs a Hermes core with pluggable delegation providers` and registers nothing.

## 1. Install the plugin

```sh
hermes plugins install https://github.com/Bto-03/hermes-flota --enable
```

Hermes clones the repository into the active profile's `plugins/flota` and runs its install security scan. To install into a named profile, run the command with that profile active (`hermes -p <name> plugins install ...`). To read the scan report before installing, clone the repository and run `hermes plugins validate <path>`. [security.md](security.md#install-scan) explains each finding.

Manual alternative: clone the repository into `<hermes home>/plugins/flota` yourself, then run `hermes plugins enable flota`.

## 2. Add your first machine

Turn the machine on and check that this works from the Hermes host:

```sh
ssh -o BatchMode=yes alice@192.0.2.20 true
```

Then add the machine:

```sh
hermes flota setup desk --host 192.0.2.20 --user alice --key ~/.ssh/id_ed25519
```

`setup` does six things:

1. **Probes the machine over SSH**, unless you pass `--no-detect`. It reads the OS (`uname -s || ver`) and finds the LAN interface that carries the default route. VPN interfaces such as `tailscale*`, `wg*`, `tun*` and `zt*` are skipped. From that interface it reads the MAC, broadcast address and Wake-on-LAN state. If it finds a MAC, wake becomes `wol`.
2. **Validates the entry.** If anything is wrong, it prints the problem, writes nothing and exits with code 2.
3. **Creates the profile `flota-desk`**, unless it already exists. The profile is a clone of the active profile's config, so the subagent keeps your model and provider. Its terminal points at the machine: `terminal.backend: ssh`, `ssh_host`, `ssh_user`, `ssh_port`, and `ssh_key` if you gave `--key`.
4. **Hides `flota-desk` from the Desktop Bots roster**, since it's plumbing, not a bot. It also records which profile owns it, in `<flota-desk>/flota/owner`.
5. **Checks how the machine can suspend without a password**, unless you pass `--sleep none`. If it can't, `setup` prints a polkit rule. See [power.md](power.md).
6. **Saves the machine in the Flota panel** of the active profile, under `plugins.entries.flota.settings.machines.desk`.

Running `setup` again updates the same entry. It keeps the keys it has no flag for (`via`, `wake_command`, `fallback`, ...).

All flags are listed in [cli.md](cli.md#setup).

## 3. Turn on the backend

```sh
hermes config set delegation.backend flota
hermes flota doctor
```

If a gateway is already running, restart it (`hermes gateway restart`) so it loads the backend. In a chat, `/flota` lists the machines, and `/flota dry-run desk` explains what a delegation would do without sending anything.

Only one delegation backend can be active at a time, so `delegation.backend: flota` replaces any other.

## 4. The Flota page (optional)

Hermes Desktop and the Webapp load the plugin's `desktop/plugin.js` as a page. Turn it on in **Capabilities → Plugins → Flota**. See [desktop-page.md](desktop-page.md).

The classic web dashboard (`hermes dashboard`) shows a **Flota** tab with no extra steps. Add `?flota_demo=1` to its URL to see five made-up machines; demo mode doesn't touch anything real.

## Updating

```sh
hermes plugins update flota
hermes gateway restart
```

After an update, restart the gateway so it picks up the new provider, hooks and SSH file-sync guard.

## Uninstalling

1. Remove `delegation.backend: flota` from `config.yaml`, or set it back to your previous backend.
2. On ships that joined a fleet, run `hermes flota leave` first, so the flagship's key line is removed from `authorized_keys`.
3. Run `hermes plugins remove flota`.
4. Optional: delete the `flota-<machine>` profiles (`hermes profile delete flota-desk`) and `<hermes home>/flota/`.
