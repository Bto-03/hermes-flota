# Configuration

Flota reads its settings from the **Flota panel** of the active profile: `plugins.entries.flota.settings` in `config.yaml`. You can edit them in Plugins → Flota, in `config.yaml`, or through `hermes flota setup` / pairing, which write machines for you.

```yaml
delegation:
  backend: flota              # turns the backend on (hermes config set delegation.backend flota)

plugins:
  entries:
    flota:
      settings:
        auto_sleep: true
        sleep_after: 10m
        sleep_action: suspend
        allow_power_off: true
        agent_tool: true
        cache_ttl: 5m
        default: desk           # optional
        machines:
          desk:
            profile: flota-desk
            host: 192.0.2.20
            user: alice
            key: ~/.ssh/id_ed25519
            description: GPU desktop; heavy builds and local models
            wake: wol
            mac: aa:bb:cc:dd:ee:ff
            broadcast: 192.0.2.255
            sleep: suspend
            sleep_after: 15m
            fallback: laptop
```

Durations accept a number of seconds (`90`) or a string with a unit: `90s`, `10m`, `1h`, `1.5h`.

Panel edits apply without a restart. The provider, `/flota` and the agent tool read the config on every call. An idle timer that is already running keeps the machine definition it started with.

## Panel settings

These are the keys in `plugin.yaml`'s `config_schema`, shown in Plugins → Flota.

| Key | Type | Default | What it does |
|---|---|---|---|
| `auto_sleep` | bool | `true` | Put machines back to sleep after their last subagent. `false` sets every machine's `sleep_after` to 0. |
| `sleep_after` | duration | `10m` | Idle time before auto-sleep, for machines that don't set their own `sleep_after`. Not applied to a machine with `sleep: none` and no `sleep_command`. |
| `sleep_action` | `suspend` \| `hibernate` \| `poweroff` | `suspend` | What auto-sleep does, for machines that don't set their own `sleep`. |
| `allow_power_off` | bool | `true` | Master switch for powering off. Only an explicit `false` turns it off. Even when it's on, only machines Flota can wake again (`wol`, `wol_via`, `command`) can be powered off; any other `poweroff` becomes `suspend`. |
| `agent_tool` | bool | `true` | Offer the agent the `flota` tool (status, wake, sleep, off). |
| `cache_ttl` | duration | `5m` | The provider's prompt-cache lifetime. Flota quotes it when it tells the agent to replace a silent subagent instead of reviving it. |
| `machines` | mapping | `{}` | Your machines by name. See [Per-machine keys](#per-machine-keys). |

## Other panel keys

These keys aren't in the settings form, but Flota reads them:

| Key | Type | Default | What it does |
|---|---|---|---|
| `default` | machine name | unset | Machine for `delegate_task` tasks that don't set `machine`. If unset, those tasks use Hermes' normal route. If it names a machine that isn't loaded, Flota logs a warning once and uses the normal route. |
| `ssh_key` | path | unset | Private key whose public half goes into invites, and that `add-ship` uses to log in to new ships. Unset: `~/.ssh/id_ed25519`, else `~/.ssh/flota_ed25519` (created with `ssh-keygen` if missing). |
| `role` | `flagship` \| `ship` | unset | Written by `hermes flota role` and the Flota page. See [pairing.md](pairing.md#roles). |
| `joined_fleets` | list | unset | Written by `join`: `[{flagship, pubkey_fingerprint, joined_at}]`. A ship belongs to one fleet. |

### Older config block

Flota 0.1 kept its settings under `delegation.backends.flota`. Flota still reads `machines`, `default` and `cache_ttl` from there; the panel value wins for each of those keys. The first time `setup` or `add-ship` writes the panel, the old machines are copied into the panel, and the old block is left as it is.

## Per-machine keys

Each entry under `machines.<name>` describes one machine. Flota validates every entry. Bad values are reported by `hermes flota doctor` and `/flota status`, and never raise. A fatal problem skips the machine; a minor one falls back to the default shown.

| Key | Type | Default | Notes |
|---|---|---|---|
| `profile` | string | **required** | Hermes profile whose terminal is SSH on this machine. `setup` creates `flota-<name>`. |
| `host` | string | **required** | Address the machine answers SSH on (IP, LAN name or Tailscale name). Must not start with `-`. |
| `port` | int 1-65535 | `22` | SSH port. An invalid value falls back to 22. |
| `user` | string | `""` | SSH user. Empty means ssh's own default. Must not start with `-`. |
| `key` | path | `""` | Private key for Flota's own SSH calls (`ssh -i`). Empty means ssh's defaults. |
| `description` | string | `""` | One line the agent reads in the `machine` field's enum help. Whitespace is collapsed. |
| `os` | `linux` \| `macos` \| `windows` | `linux` | Picks the power commands. `setup` detects it. |
| `wake` | `none` \| `wol` \| `wol_via` \| `command` \| `timer` | `none` | How Flota turns the machine on. See [Wake methods](#wake-methods). |
| `mac` | MAC | `""` | Required for `wol` and `wol_via`. Twelve hex digits, with `:` or `-` separators or none. |
| `broadcast` | IPv4 | `255.255.255.255` | Where the magic packet goes. `setup` detects the LAN broadcast address, for example `192.0.2.255`. |
| `via` | ssh target | `""` | Required for `wol_via`: an always-on host on the machine's LAN, as `user@host` or an ssh alias. It needs `python3`. |
| `wake_command` | shell command | `""` | Required for `wake: command`. Runs on the Hermes host with a 30 s timeout. Only settable in `config.yaml`. |
| `wake_timeout` | duration | `90s` | How long to wait for SSH to answer after waking. The packet is resent every 10 s. |
| `wake_timer` | bool | `false` | Also arm the machine's own RTC wake alarm before it suspends. With `wake: none`, this turns wake into `timer`. |
| `timer_lead` | duration | `120s` | How early before the next cron job the wake timer fires. Never less than 60 s from the moment of sleeping. |
| `fallback` | machine name | `""` | Tried once if this machine won't wake. Must be another loaded machine; the fallback's own fallback is never followed. |
| `sleep` | `none` \| `suspend` \| `hibernate` \| `poweroff` | panel `sleep_action`; `none` for a machine with `wake: none` and no `wake_timer` | What auto-sleep and `sleep` do. `none` means never auto-sleep. macOS can't hibernate on demand, so `hibernate` becomes `suspend` there. `poweroff` becomes `suspend` unless power-off is allowed for this machine. |
| `sleep_after` | duration | panel `sleep_after` | Idle time with no subagents before auto-sleep (after the last subagent, or after a wake with none). `0` disables auto-sleep for this machine. Needs `sleep` or `sleep_command`. |
| `sleep_command` | shell command | `""` | Runs on the Hermes host instead of the SSH power command, for the machine's default action. Only settable in `config.yaml`. |
| `power_prefix` | string | `""` | Prepended to `systemctl <action>` on Linux. `setup` writes it when suspend works only inside a niri session. Only settable in `config.yaml` or by `setup`. |
| `alt_hosts` | list | — | Written by `add-ship`: the ship's other addresses. Used to recognise the same ship when it's added again. |
| `crew_names` | list | — | Written by `add-ship`: the names of the ship's own Hermes bots, shown on the map until a live scan replaces them. |

### Wake methods

| `wake` | Needs | What happens |
|---|---|---|
| `none` | — | Flota can't turn the machine on. A task for it fails at once if it's off. |
| `wol` | `mac`, `broadcast` | Three magic packets (UDP port 9) from the Hermes host. Packets don't cross routers or Tailscale, so the Hermes host must be on the machine's LAN. |
| `wol_via` | `mac`, `broadcast`, `via` | `ssh -o BatchMode=yes <via> python3 -c ...` broadcasts the packet from the relay host. Only the validated, re-serialised MAC and broadcast address reach the remote shell. |
| `command` | `wake_command` | Runs the command locally (`shell=True`, 30 s timeout), for example a smart-plug API call. |
| `timer` | a wake-timer permission on the machine | Nothing can wake the machine on demand. Before Flota suspends it, the machine arms its own wake alarm for its next cron job. A task that arrives while it sleeps waits only if that alarm is due within `wake_timeout`. See [power.md](power.md#wake-timers). |

### Which keys the UI can set

The dashboard tab and the Flota page can set `host`, `user`, `key`, `port`, `description`, `wake`, `mac`, `broadcast`, `via`, `sleep`, `sleep_after`, `wake_timeout`, `fallback` and `os`. A request carrying `sleep_command`, `wake_command`, `power_prefix`, `command` or `profile` is refused. When a machine is updated from the UI, its existing values for those keys are kept.

## Files Flota writes

| Path | What |
|---|---|
| `<hermes home>/config.yaml` | The panel (`plugins.entries.flota.settings`), through core's atomic config writer |
| `<hermes home>/profiles/flota-<name>/config.yaml` | The machine profile's `terminal` block |
| `<hermes home>/profiles/flota-<name>/profile.yaml` | Bots-roster `hidden: true` and a description |
| `<hermes home>/profiles/flota-<name>/flota/owner` | The profile that owns this plumbing profile |
| `<hermes home>/flota/state.json` | Busy counts published by the running process: `{pid, updated, busy}` |
| `<hermes home>/flota/invites.json` | Unexpired invite nonces (mode 600) |
| `<hermes home>/flota/waketimers.json` | When each `timer` machine's alarm is due |
| `~/.ssh/authorized_keys` (ships) | One line per flagship, with the comment `flota-flagship:` followed by the flagship's name |
| `~/.ssh/flota_ed25519` (flagship) | Only if no key was configured and `~/.ssh/id_ed25519` doesn't exist |
