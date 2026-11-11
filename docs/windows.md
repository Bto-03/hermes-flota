# A Windows PC as a Flota ship

Checklist to make a Windows 10/11 PC a ship that the flagship reaches over Tailscale, can put to
sleep, and that wakes itself for its scheduled jobs. Every block is one command per line: paste
lines one at a time (or the whole block) into **PowerShell**. Nothing here uses line continuations,
so you never get stuck at a `>>` prompt. Lines marked *(admin)* need "Run as administrator".

Before you start, write down what you change: section 8 undoes every step.

## 0. Who are you on this PC?

Admin accounts and normal accounts keep their SSH key in different places. Check:

```powershell
whoami
whoami /groups | findstr S-1-5-32-544
```

If the second line prints anything, your account is in the Administrators group (even when this
window is not elevated) and OpenSSH reads your key from `C:\ProgramData\ssh\administrators_authorized_keys`,
**not** from your profile. Otherwise it reads `%USERPROFILE%\.ssh\authorized_keys`.

## 1. Install and start OpenSSH Server *(admin)*

```powershell
Get-WindowsCapability -Online -Name OpenSSH.Server*
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
Get-Service sshd
```

The last line must say `Running`. The default shell stays `cmd.exe`; Flota works with cmd or
PowerShell, so there is nothing to change.

## 2. Firewall: SSH from the tailnet only, on every network profile *(admin)*

Windows often files the Tailscale adapter under the *Public* profile, so the rule covers all
profiles but only Tailscale addresses (100.64.0.0/10):

```powershell
New-NetFirewallRule -Name FlotaSSH -DisplayName "Flota SSH (Tailscale only)" -Direction Inbound -Protocol TCP -LocalPort 22 -RemoteAddress 100.64.0.0/10 -Profile Any -Action Allow
```

Optional, to make SSH reachable from the tailnet *only* (the OpenSSH installer opens port 22 to
everyone on its own rule):

```powershell
Disable-NetFirewallRule -Name OpenSSH-Server-In-TCP
```

## 3. The flagship's key

Take the flagship's public key (one line starting with
`ssh-ed25519`). Use `Add-Content`, never `echo ... >`: in Windows PowerShell 5.1 `>` writes UTF-16,
which sshd cannot read.

### 3a. Admin account *(admin)*

```powershell
Add-Content -Path "C:\ProgramData\ssh\administrators_authorized_keys" -Value "ssh-ed25519 AAAA...paste-the-key... flota"
icacls.exe "C:\ProgramData\ssh\administrators_authorized_keys" /inheritance:r /grant "*S-1-5-32-544:F" /grant "*S-1-5-18:F"
```

The SIDs are Administrators and SYSTEM; they work on any Windows language (the group is called
"Administradores" on a Spanish Windows). sshd ignores the file if anyone else can write it.

### 3b. Normal (non-admin) account

```powershell
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.ssh"
Add-Content -Path "$env:USERPROFILE\.ssh\authorized_keys" -Value "ssh-ed25519 AAAA...paste-the-key... flota"
icacls.exe "$env:USERPROFILE\.ssh\authorized_keys" /inheritance:r /grant "${env:USERNAME}:F" /grant "*S-1-5-18:F"
```

Test from the flagship: `ssh USER@100.x.y.z ver` must print `Microsoft Windows [...]` without asking
for a password.

## 4. Tailscale

```powershell
winget install --id Tailscale.Tailscale -e
```

Log in from the tray icon, then:

```powershell
tailscale ip -4
```

That 100.x address is the ship's `host`. In the Tailscale tray menu enable *Run unattended* so the
tailnet stays up when nobody is logged in.

## 5. Sleep

Flota suspends with `SetSuspendState('Suspend', 0, 0)` (the last 0 keeps wake events enabled, which
the wake timer needs) and hibernates with `shutdown /h`. The logged-in user may do both over SSH; no
admin. Check what this PC supports:

```powershell
powercfg /a
```

* `Standby (S3)` listed: `sleep: suspend` works, and wake timers wake it.
* Only `Standby (S0 Low Power Idle)` (Modern Standby, most recent laptops): suspend works, but
  Windows ignores scheduled-task wake timers in Modern Standby. Use `sleep: hibernate` for a ship
  with `wake: timer`.
* Hibernate must be enabled for `sleep: hibernate` *(admin)*:

```powershell
powercfg /hibernate on
```

## 6. Waking without Wake-on-LAN: the wake timer

A sleeping PC can only be woken from outside by a Wake-on-LAN packet sent from **its own LAN**. A
ship with no other Flota machine on its network cannot be woken on demand. It can wake itself:
with `wake: timer` (or `wake_timer: true` next to another wake method) Flota registers a one-time
scheduled task `FlotaWake` with "Wake the computer to run this task" for the ship's next cron job,
2 minutes early (`timer_lead`), right before suspending it. With no upcoming job nothing is set and
the PC sleeps until someone turns it on. Powering off is never offered for these ships: waking from
"off" by timer is unreliable.

Registering that task for your own account needs **no admin**. The power plan must allow wake timers
(value 1; 2 means "important wake timers only" and ignores scheduled tasks). First note the current
values so you can restore them (the two `0x...` numbers are AC and battery):

```powershell
powercfg /qh SCHEME_CURRENT SUB_SLEEP RTCWAKE
```

Then allow them (try without admin first; if Windows refuses, use an admin window):

```powershell
powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
powercfg /setactive SCHEME_CURRENT
```

Flota refuses to suspend a `wake: timer` ship with an upcoming job while its plan blocks wake timers, so it never goes
to sleep with no way back. Some firmware also has a "Wake on RTC" / "Resume by alarm" switch; leave
it at its default (OS-controlled), not "Disabled".

See the timer Flota set:

```powershell
Get-ScheduledTask -TaskName FlotaWake | Get-ScheduledTaskInfo
```

## 7. Ship settings on the flagship

Run `hermes flota setup escritorio --host 100.x.y.z --user alice --os windows` on the flagship, or
pair with codes ([pairing.md](pairing.md)). The resulting entry under
`plugins.entries.flota.settings.machines` looks like this; add the wake-timer keys by hand:

```yaml
escritorio:
  profile: flota-escritorio   # hermes flota setup creates it
  host: 100.x.y.z
  user: alice
  os: windows
  wake: timer        # or wol/wol_via + wake_timer: true
  sleep: suspend     # hibernate on Modern Standby laptops
  timer_lead: 2m
```

## 8. Undo everything

Run only the lines for steps you did. *(admin)* for all but the last block.

```powershell
Unregister-ScheduledTask -TaskName FlotaWake -Confirm:$false
Remove-NetFirewallRule -Name FlotaSSH
Enable-NetFirewallRule -Name OpenSSH-Server-In-TCP
Stop-Service sshd
Set-Service -Name sshd -StartupType Disabled
Remove-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
(Get-Content "C:\ProgramData\ssh\administrators_authorized_keys") | Where-Object { $_ -notmatch ' flota$' } | Set-Content "C:\ProgramData\ssh\administrators_authorized_keys"
powercfg /hibernate off
```

The `Get-Content` line removes only the key you added in step 3a (the line ending in ` flota`); other
administrators' keys stay. Delete the whole file only if Flota's key was the only line in it.

Wake timers back to what you noted in step 6 (replace 0 with your values; 0 = off, 1 = on, 2 =
important only):

```powershell
powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 0
powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 0
powercfg /setactive SCHEME_CURRENT
```

Non-admin key and Tailscale (no admin):

```powershell
(Get-Content "$env:USERPROFILE\.ssh\authorized_keys") | Where-Object { $_ -notmatch ' flota$' } | Set-Content "$env:USERPROFILE\.ssh\authorized_keys"
tailscale logout
winget uninstall --id Tailscale.Tailscale -e
```

`Remove-WindowsCapability` keeps `C:\ProgramData\ssh` (host keys, config); delete that folder too
for a clean slate: `Remove-Item -Recurse C:\ProgramData\ssh`.

## Other OSes, for comparison

| ship OS | arming its wake alarm | one-time root step |
|---|---|---|
| Windows | scheduled task with WakeToRun, own account | none (power plan: RTCWAKE 1) |
| Linux | `echo +N > /sys/class/rtc/rtc0/wakealarm` or `rtcwake -m no -s N` | yes: a udev rule making `wakealarm` group-writable, or a sudoers line for `rtcwake -m no -s *`. A `systemd-run --user` timer with `WakeSystem=true` does not work: it needs CAP_WAKE_ALARM, system manager only |
| macOS | `pmset relative wake N` | yes: a sudoers line for `/usr/bin/pmset relative wake *` |

`waketimer.capability()` checks a ship and `waketimer.setup_lines()` prints its one-time steps (the
Linux and macOS sudoers routes are spelled out in [power.md](power.md#wake-timers)). Flota never runs
an interactive `sudo`; on Linux and macOS it uses `sudo -n` for the wake alarm only when you added
that rule.
