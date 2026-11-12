# Troubleshooting

Start with:

```sh
hermes flota doctor          # every machine; add a name for one
/flota dry-run <machine>     # in a chat: what a delegation would do, sending nothing
```

## `doctor` lines

| Line | Meaning | Fix |
|---|---|---|
| `✗ machine 'x': ...; skipped` | The entry is invalid, so Flota ignores the machine | Fix the key named in the message (see [configuration.md](configuration.md#per-machine-keys)) |
| `✗ machine 'x': ...; using 22` / `using none` / `using linux` | A minor value was replaced by its default | Correct the value |
| `✗ machine 'x': sleep_after needs 'sleep' or 'sleep_command'; auto-sleep disabled` | `sleep: none` with an explicit `sleep_after` | Set `sleep`, or remove `sleep_after` |
| `✗ machine 'x': fallback 'y' ...; fallback cleared` | The fallback is the machine itself, or not a loaded machine | Point it at another machine |
| `✗ SSH: host:22 is not answering` | Off, asleep, wrong address, or a firewall | `hermes flota wake x`; check the address |
| `✗ SSH: port open but login failed: Permission denied ...` | The key isn't authorised, or the user is wrong | `ssh-copy-id user@host`, or pair again |
| `✗ profile flota-x is missing` / `terminal is local ..., not ssh host` | The profile was deleted or edited by hand | Run `hermes flota setup x --host ... --user ...` again (`doctor` prints the exact line) |
| `✗ profile flota-x does not set terminal.ssh_sync_files: false` | The profile was made before Flota 0.2.2, or edited by hand | `hermes flota doctor --fix` writes the key |
| `! SSH file sync is still on: this Hermes does not read terminal.ssh_sync_files yet` | A warning, not a failure: each subagent copies this host's `~/.hermes` files to the machine and tars the machine's `~/.hermes` back when it ends (slow, can time out) | Nothing to do in Flota: it already set the key. A Hermes release that includes `terminal.ssh_sync_files` ([issue #148726](https://github.com/NousResearch/hermes-agent/issues/148726)) fixes it; see [below](#subagents-on-a-flota-machine-are-slow-to-finish) |
| `✗ no wake method` | `wake: none` | Run `setup` again while the machine is on, to detect its MAC |
| `✗ Wake-on-LAN disabled on enp3s0` | The NIC has WoL off | Run the printed commands as root (`ethtool -s <iface> wol g`, then make it persistent with `nmcli ... wake-on-lan magic`), and enable WoL in the BIOS/UEFI |
| `✗ Wake-on-LAN: the LAN MAC is ..., the config has ...` | The NIC changed, or the wrong MAC was saved | Run `setup` again |
| `? power off: waking from power-off (S5) ...` | A reminder; Flota can't check it | Test once: `hermes flota off x`, then `hermes flota wake x` |
| `✗ sleep: needs a password over SSH` | polkit refuses `systemctl suspend` from SSH | Install the polkit rule that `setup` prints ([power.md](power.md#linux-polkit-and-the-graphical-session)) |
| `✗ sleep: works via the niri session, but the saved power_prefix differs` | Your session changed since setup | Run `setup` again |
| `? LAN: broadcast 255.255.255.255 only reaches this host's own LAN` | No real broadcast address saved | Let `setup` detect it, or pass `--broadcast` |
| `✗ this host is not on the LAN of 192.0.2.255` | Magic packets won't reach the machine from here (they don't cross routers or Tailscale) | Use `wake: wol_via` with `via` set to an always-on host on that LAN, or `wake: timer` |
| `No machines yet` | The panel is empty | `hermes flota setup <name> --host HOST --user USER` |

## Common problems

**The agent doesn't offer a `machine` field.** Check that `delegation.backend: flota` is set, that at least one machine loads (`hermes flota doctor`), and that the gateway was restarted after installing or updating. If the core is too old, the log says `flota needs a Hermes core with pluggable delegation providers`.

**"Machine 'x' is not available: ... did not come up within 90s of being woken".**

- WoL is off on the NIC or in the BIOS, or the Hermes host isn't on the machine's LAN. Run `doctor`.
- The machine takes longer than `wake_timeout` to boot. Raise it (e.g. `wake_timeout: 3m`). In the author's tests, WoL from suspend took 19-42 s.
- Waking from *power-off* needs WoL from S5 in the BIOS.

**A `timer` machine fails at once with "sleeps until its wake timer at ...".** That's expected: Flota can't wake it on demand. It waits only if the alarm is due within `wake_timeout`. Schedule work for it with a cron job that names the machine, or turn it on by hand.

**"suspend failed: ...; not suspended, or it could not wake again".** The machine uses a wake timer, and arming the alarm failed, so Flota refused to suspend it. Typical causes: no permission yet ([power.md](power.md#wake-timers)), or on Windows a power plan that blocks wake timers (`RTCWAKE` not 1).

**The machine never auto-sleeps.**

- `auto_sleep` is off, the machine has `sleep: none`, or `sleep_after: 0`.
- No long-lived process: timers live in the gateway. A one-shot `hermes chat -q` exits before the timer fires.
- It's still busy. `hermes flota status` with a running gateway shows `· busy N`. A child that never started counts for 10 minutes.
- The sleep command fails. The gateway log has `flota: auto-sleep desk: suspend failed: ...`. Run `doctor`.

**`hermes flota sleep` says busy, but nothing runs.** The counts come from `<hermes home>/flota/state.json`, written by the running gateway. If a subagent crashed without `subagent_stop`, use `--force`. A state file whose process has exited is ignored automatically.

**The Flota page says "Flota is not enabled on this Hermes".** The app sends plugin requests to the profile you last chatted with. Enable the plugin on that Hermes and restart its gateway, or use the card's "Open the fleet on ..." button, which finds the flagship on another connection. See [desktop-page.md](desktop-page.md#connections-and-profiles).

**`add-ship`: "its SSH host key differs from the one this host remembers".** The ship was reinstalled, or another machine now answers on that address. If you trust it: `ssh-keygen -R <host>`, then add the ship again. Otherwise stop and investigate.

**`add-ship`: "this ship code answers an invite this Hermes did not issue, or one already used".** Each invite adds one ship, and only on the flagship that issued it. Make a new invite.

**`join`: "already in a fleet: ...".** A ship belongs to one fleet. Run `hermes flota leave` first.

**`setup`: "⚠ Saved 'x' but NOT verified: ... is not answering".** The machine was saved with what you gave, but nothing was detected or checked. Turn it on and run the same command again, or undo it with `hermes flota remove x`.

**`add-ship`: "this ship code says it is 'x', but it logs in as ...".** The code matches a machine already in the fleet but logs in on an address or user that machine doesn't have. Nothing was changed. If the machine really moved, run `hermes flota add-ship <code> --replace` (or press "This machine really moved - replace it" on the page); otherwise don't add the code.

**`/flota sleep desk` answers "busy with N subagent(s), not put to sleep".** By design. If you're sure, type `/flota sleep desk force`. The agent can't do this for you: its tool has no force option.

**The agent says it may not suspend or power off machines.** That is the default. Turn on "Agent power actions" (`agent_power_actions: true`) in Plugins → Flota and start a new session (the tool's list of actions is fixed when a session starts). Each request then shows Hermes' approval prompt naming the machine and the action. With no one to answer it (a cron job, a one-shot `hermes chat -q` without approvals), Hermes refuses the call. You can always do it yourself with `hermes flota sleep|off <machine>`, `/flota` or the Flota page.

**Not sure what to do next?** Run `hermes flota` with no arguments: it prints where this Hermes stands and the one command to run next.

### Subagents on a Flota machine are slow to finish

That's Hermes' SSH file sync. When a subagent connects, core's SSH terminal copies this host's skills, caches and declared credential files into the machine's `~/.hermes`; when it ends, it tars the machine's whole `~/.hermes` back (120 s timeout, three tries). On a machine that has its own Hermes install that can be many GB, so the subagent's end is slow or times out.

Flota 0.2.1 and earlier turned this off by wrapping core's SSH terminal class. A catalog plugin may not patch core, so 0.2.2 removed that and writes `terminal.ssh_sync_files: false` into each machine's profile instead. **Hermes 0.23.1 does not read that key yet**, so until a core release does, every subagent on a Flota machine syncs again, and `doctor` shows `! SSH file sync is still on`. The key is tracked in [issue #148726](https://github.com/NousResearch/hermes-agent/issues/148726) (PRs #148791 and #145271 add it, with `TERMINAL_SSH_SYNC_FILES` as the env var).

Until then: keep the machine's own `~/.hermes` small, or run Flota subagents as a ship user that has no Hermes install. Make sure every machine profile has the key (`hermes flota doctor --fix`), so it takes effect as soon as you update Hermes.
