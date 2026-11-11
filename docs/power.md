# Power: sleep, power off and wake timers

Flota puts a machine to sleep in three situations:

- **Auto-sleep**, `sleep_after` after its last subagent.
- **On request**: `/flota sleep`, `hermes flota sleep`, the agent tool, or the Sleep button on the dashboard or Flota page.
- **Power off on request**: `off` from any of the same places.

Every power command goes over SSH (`ssh -o BatchMode=yes -o ConnectTimeout=10 [-i key] user@host <command>`) with a 30 s timeout. The one exception is a machine with a `sleep_command`: for its default action, that command runs on the Hermes host instead. A dropped SSH connection (`closed by remote host`) counts as success, since that's what a suspending machine does.

## Commands per OS

| Action | Linux | macOS (experimental) | Windows (experimental) |
|---|---|---|---|
| `suspend` | `systemctl suspend` | `pmset sleepnow` | `powershell ... [System.Windows.Forms.Application]::SetSuspendState('Suspend', 0, 0)` |
| `hibernate` | `systemctl hibernate` | refused: macOS has no on-demand hibernate (`sleep: hibernate` is read as `suspend`) | `shutdown /h` |
| `poweroff` | `systemctl poweroff` | `osascript -e 'tell application "System Events" to shut down'` | `shutdown /s /t 0` |

On Linux, a saved `power_prefix` is prepended to the `systemctl` command (see [niri](#linux-polkit-and-the-graphical-session)).

`sleep` uses the machine's `sleep` action, or `suspend` when that's `none`. `off` always powers off.

## Busy machines are never put to sleep

The running Hermes process (normally the gateway) counts, per machine:

- subagents that have started and not stopped,
- tasks routed to it whose subagent hasn't started yet (forgotten after 10 minutes),
- routes still being prepared (a wake in progress).

**Auto-sleep** fires only when all three counts are zero. A new task cancels a pending timer.

**`/flota sleep`, `/flota off` and the agent tool** (which run in that same process) refuse a busy machine and answer `<name>: busy with N subagent(s), not put to sleep`.

**`hermes flota sleep|off`, the dashboard and the Flota page** run in other processes, so they read the counts from `<hermes home>/flota/state.json`. The file is ignored if the process that wrote it is gone or it's older than a day. They refuse a busy machine unless you force it: `--force` on the CLI, or a confirmation in the UI. With no live gateway they can't know, so they don't refuse.

If a task arrives for a machine that was put to sleep less than a minute ago, Flota waits up to 20 s for SSH to stop answering, then wakes it again. The task never lands on a machine that is halfway through suspending.

## Power off only where Flota can wake the machine

A machine can be powered off only if **both** of these are true:

1. **"Allow power off"** (`allow_power_off`) is on in the panel. It's on by default.
2. **Its `wake` is `wol`, `wol_via` or `command`**, so Flota can turn it back on.

Otherwise `off` is refused with a reason: `Powering off is disabled ...`, or `<name> has no wake method, so Flota would not be able to turn it back on; it can only be suspended.` Likewise, `sleep: poweroff` (or `sleep_action: poweroff`) on such a machine is read as `suspend` for auto-sleep. A `timer` machine is never powered off, because waking from "off" by RTC alarm is unreliable.

Waking from power-off (ACPI S5) by Wake-on-LAN must also be enabled in the BIOS/UEFI. Flota can't check that; `doctor` reminds you to test it once with `hermes flota off <name>` then `hermes flota wake <name>`.

## Linux: polkit and the graphical session

`systemctl suspend` over SSH usually asks for authentication: polkit only lets the *active seat session* suspend without a password, and an SSH session isn't one. `hermes flota setup` and `doctor` check in this order:

1. **Directly.** They call `org.freedesktop.login1.Manager.CanSuspend` over SSH. If it answers `yes`, Flota runs `systemctl` as is.
2. **Inside a niri session.** If the user is logged in to the [niri](https://github.com/YaLTeR/niri) compositor, Flota asks the same question from inside that session (`niri msg action spawn`). If that answers `yes`, `setup` saves `power_prefix`, and Flota runs `systemctl` inside the graphical session from then on. The niri socket is looked up when the command runs, so a new login doesn't break it.
3. **Otherwise**, `setup` prints a polkit rule to install once, as root, at `/etc/polkit-1/rules.d/49-flota.rules`:

```js
polkit.addRule(function(action, subject) {
    if (subject.user == "alice" && [
            "org.freedesktop.login1.suspend",
            "org.freedesktop.login1.hibernate",
            "org.freedesktop.login1.power-off"].indexOf(action.id) >= 0) {
        return polkit.Result.YES;
    }
});
```

The rule lets exactly that user suspend, hibernate or power off. polkit reloads its rules on its own. Run `hermes flota doctor` afterwards: it should say `can sleep without a password (systemctl works directly)`.

Other compositors and desktops aren't detected. Use the polkit rule, or a `sleep_command`.

macOS needs nothing: `pmset sleepnow` needs no password. Neither does Windows: the logged-in user may suspend.

## Wake timers

A suspended machine can be woken from outside only by a Wake-on-LAN packet from its own LAN. A ship reached only over Tailscale, with nothing else on its LAN, can't be woken on demand. It can wake itself, though.

With `wake: timer` (or `wake_timer: true` alongside another wake method), Flota does this **before suspending or hibernating** the machine:

1. It reads this Hermes's cron jobs and finds the soonest enabled future job that **names the machine**. A job names a machine through its `machine` / `machines` field, or by mentioning the machine's name as a whole word in its name, prompt or skills (`flota-desk` names `desk`; `desks` doesn't).
2. Over SSH, it arms the machine's own alarm for `timer_lead` (default 2 minutes) before that job, and never less than 60 s from now. The alarm is relative ("in N seconds"), so time zones and an RTC kept in local time don't matter.
3. Only then does it suspend the machine, and the result says `wake timer set for 02:00, for '<job>'`.

If arming fails, the machine is **not** suspended (`suspend failed: ...; not suspended, or it could not wake again`). With no upcoming job, nothing is armed and the machine sleeps until someone turns it on.

When a task arrives for a sleeping `timer` machine, Flota can't wake it. It waits for the machine only if its alarm is due within `wake_timeout` (or has just fired); otherwise the task fails at once with `<name> sleeps until its wake timer at 02:00 or until you turn it on`, and the agent can resend it elsewhere.

From a real test on 2026-11-10: a scheduled job woke a suspended Linux desktop through its own alarm (about 27 s until SSH answered), ran there, and Flota auto-suspended the desktop 2 minutes after the job.

### What arming needs, per OS

| OS | How Flota arms it | One-time setup |
|---|---|---|
| Linux | writes `+N` to `/sys/class/rtc/rtc0/wakealarm` if the user can, else runs `sudo -n rtcwake -m no -s N` if a sudoers rule allows exactly that | route A or route B below, as root |
| macOS | `sudo -n /usr/bin/pmset relative wake N` | a sudoers rule for that command |
| Windows | a one-time scheduled task `FlotaWake` with "Wake the computer to run this task", for the logged-in user | none (no admin); the power plan must allow wake timers. See [windows.md](windows.md#6-waking-without-wake-on-lan-the-wake-timer) |

Flota never asks for a password. It checks first (`sudo -k -n -l <exact command>`) and only runs `sudo -n` when the rule is already there. `waketimer.capability()` reports which route works (`direct`, `sudoers` or `task`) and returns the setup steps when none does; the Join page shows the result as the "Wakes itself on a timer" option.

A `systemd-run --user` timer with `WakeSystem=true` doesn't help on Linux: it needs `CAP_WAKE_ALARM`, which only the system manager has.

### Linux route A: the RTC alarm writable by your group (recommended)

After this, arming needs no privilege at all. As root, replacing `alice` with the machine's login group:

```sh
echo 'ACTION=="add", SUBSYSTEM=="rtc", KERNEL=="rtc0", RUN+="/bin/chgrp alice /sys%p/wakealarm", RUN+="/bin/chmod g+w /sys%p/wakealarm"' | tee /etc/udev/rules.d/90-flota-wakealarm.rules
chgrp alice /sys/class/rtc/rtc0/wakealarm
chmod g+w /sys/class/rtc/rtc0/wakealarm
```

The udev rule reapplies the permission at every boot; the last two lines apply it now. To undo, delete the rule file and reboot.

### Linux route B and macOS: a passwordless rule for exactly one command

If you'd rather not change the device permission, add a sudoers drop-in that lets the machine's user run exactly one command as root, with no password:

| OS | The one command to allow |
|---|---|
| Linux | `/usr/sbin/rtcwake -m no -s *` (use the path `command -v rtcwake` prints on that machine) |
| macOS | `/usr/bin/pmset relative wake *` |

Allow nothing broader. Flota checks for exactly this command and never uses `sudo` for anything else. To undo, delete the drop-in file. When neither route works yet, the `setup` field of `waketimer.capability()` (from `waketimer.setup_lines()`) lists the same steps, with the machine's own user, group and `rtcwake` path filled in.

### Firmware

Some boards have a "Wake on RTC" / "Resume by alarm" switch. Leave it at its default (OS-controlled), not "Disabled". On Windows laptops with Modern Standby (S0 low-power idle), wake timers are ignored during sleep; use `sleep: hibernate` there.
