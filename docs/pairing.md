# Fleet pairing

You can add a machine in two ways:

- **`hermes flota setup`** ([installation.md](installation.md#2-add-your-first-machine)). Use this when you can already SSH into the machine with a key. The machine needs no Hermes at all.
- **Pairing**, described here. Use this when the other machine runs its own Hermes with Flota. You copy two codes, and the flagship ends up able to SSH into the ship with its key.

## Roles

Each Hermes with Flota has one of three roles:

| Role | Meaning |
|---|---|
| `flagship` | Leads a fleet: it has machines (ships) and routes subagents to them. |
| `ship` | Belongs to exactly one flagship's fleet: that flagship's key can log in to it. |
| none | Neither, yet. The Flota page asks you to create or join a fleet. |

If you never chose a role, Flota infers one: a Hermes with machines is a flagship, and one with a joined fleet is a ship. `hermes flota role` shows the current role (marked `(inferred)` when it wasn't chosen), the fleet and the number of ships.

### Lifecycle

| Step | Command (Flota page equivalent) | What changes |
|---|---|---|
| Create a fleet | `hermes flota role flagship` ("Create a fleet") | Sets `role: flagship`. With no role chosen, adding the first ship has the same effect (the role is inferred). |
| Join a fleet | on the ship: `hermes flota join INVITE` (or paste the invite in the Flota page) | Adds the flagship's public key to the ship's `authorized_keys`, records the fleet, sets `role: ship` if no role was chosen, and prints the ship code. |
| Rejoin the same fleet | `hermes flota join <new invite>` from the same flagship | Allowed. The key is already there, so nothing is duplicated; you get a fresh ship code. |
| Join another fleet | — | Refused with `already in a fleet: <flagship>` (exit 1). Leave first. |
| Leave | on the ship: `hermes flota leave [flagship]` | Removes the lines Flota added for that flagship (comment `flota-flagship:<name>`) and nothing else, forgets the fleet and clears the role. |
| Ship → flagship | `hermes flota role flagship` | Refused while the machine is in a fleet. With `--force`, it leaves the fleet first (key removed). |
| Flagship → ship | `hermes flota role ship` | Refused while it leads ships. With `--force`, it removes every machine from the panel (and from the old `delegation.backends.flota` block). Their `flota-*` profiles are kept. |
| Clear | `hermes flota role none` | Forgets the chosen role. Ships and fleets are untouched. |

## The three steps

```text
flagship                                   ship
────────                                   ────
hermes flota invite      ──invite code──▶  hermes flota join <invite>
                                           (confirm: "Let <flagship> log in as <you>?")
hermes flota add-ship    ◀──ship code───   prints the ship code
<ship code>
```

1. **Invite (flagship).** `hermes flota invite` prints an invite valid for **30 minutes** and records its nonce in `<hermes home>/flota/invites.json`.
2. **Join (ship).** `hermes flota join INVITE` shows the flagship's name, its login user and the key's SHA256 fingerprint, then asks `[y/N]`. Without a terminal it refuses unless you pass `--yes`. Once you confirm, it appends the key to `~/.ssh/authorized_keys` (creating `~/.ssh` with mode 700 and setting the file to 600), runs the reachability checks below, and prints the ship code.
3. **Add the ship (flagship).** `hermes flota add-ship <ship code>` checks that the code answers an unexpired invite issued by this Hermes. It then tries each address in the code: first a TCP connection to the port, then `ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new ... true`. It uses the first address that logs in and runs `setup` on it. Wake becomes `wol` if the flagship is on the ship's LAN (same broadcast domain), otherwise `none`. A ship already in the fleet (same MAC, or one of its addresses) is updated instead of duplicated; `--name` sets the name for a new one.

**Invites are single-use.** The nonce is spent when the ship is saved, so a second ship code for the same invite is refused. If adding fails (no address logs in), the nonce stays valid, and you can fix the problem on the ship and add the same code again until the invite expires.

## Code formats

Both codes are a prefix followed by base64url-encoded compact JSON (padding stripped). Whitespace in a pasted code is ignored, and a code longer than 8192 characters is refused.

**Invite** `FLOTA1-INVITE-…`

| Field | Value |
|---|---|
| `v` | `1` |
| `flagship` | the flagship's hostname (letters, digits, `.`, `_`, `-`; max 64) |
| `user` | its login user (shown to you before you confirm) |
| `pubkey` | one OpenSSH public key line: `ssh-ed25519`, `ssh-rsa` or `ecdsa-sha2-nistp256` |
| `nonce` | 32 random hex characters |
| `exp` | expiry, epoch seconds (issued + 1800) |

**Ship code** `FLOTA1-SHIP-…`

| Field | Value |
|---|---|
| `v`, `nonce` | the invite's nonce, echoed back |
| `name` | suggested machine name, from the hostname (`[a-z0-9][a-z0-9_-]{0,57}`) |
| `host`, `alt_hosts` | addresses to try: Tailscale first, then LAN (at most 8 alternatives) |
| `port`, `user` | SSH port (22) and login user |
| `mac`, `broadcast`, `wol` | LAN interface facts for Wake-on-LAN |
| `os` | `linux`, `macos` or `windows` |
| `crew_names` | names of the ship's own Hermes bots (at most 32) |

Each side validates every field against a strict pattern. Hosts and users can't start with `-`, and a public key can't contain a newline or `PRIVATE KEY`.

## What is exchanged

- **Only public keys.** The invite carries the flagship's public key, and the ship code carries no key at all. No private key, password, token or shell command travels in either code.
- **Which key.** The flagship uses the panel's `ssh_key` if set. Otherwise it uses `~/.ssh/id_ed25519`, and if that doesn't exist either, it creates `~/.ssh/flota_ed25519`.
- **What the ship grants.** A normal login for that key, as the user who ran `join`. Subagents need a real shell, so the line has no `restrict` or `command=` options; it's equivalent to `ssh-copy-id`. The comment `flota-flagship:<name>` lets `leave` remove exactly that line.
- **Host keys.** The first contact records the ship's SSH host key (`accept-new`). A changed host key is refused, with instructions.

See [security.md](security.md) for the threat model.

## Reachability checks (`join-check`)

`hermes flota join` runs these checks on the ship, and `hermes flota join-check` runs them on their own. Each one prints a fix when it fails:

| Check | Fails when |
|---|---|
| `sshd` | nothing listens on 127.0.0.1:22 |
| `sshd_tailscale` / `sshd_lan` | sshd doesn't answer on the Tailscale or LAN address (a `ListenAddress` restriction) |
| `firewall` | Linux: `ufw` or `firewalld` is active (shown as a reminder with the rule to add); other OSes: always a reminder |
| `tailscale` | Tailscale is installed but not connected, or missing (then only the LAN can reach the ship) |
| `ssh_perms` | the home directory is group/other-writable, `~/.ssh` isn't 700, or `authorized_keys` is group/other-writable (sshd `StrictModes` would ignore the key) |
| `wol` | Wake-on-LAN is off or unknown on the LAN interface (prints how to enable it) |
| `wol_reach` | a reminder that magic packets don't cross routers or Tailscale |

`join-check` exits 1 if any check failed, otherwise 0.

## Windows ships

`hermes flota join` runs on Windows too (its fixes are PowerShell commands), but that path is unit-tested only. [windows.md](windows.md) is a step-by-step checklist for preparing a Windows ship by hand, including the admin-account key file. On an account in the Administrators group, OpenSSH ignores the per-user key file that `join` writes and reads `C:\ProgramData\ssh\administrators_authorized_keys` instead; add the key there as in step 3a of that checklist.
