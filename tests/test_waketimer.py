import base64
import json
import shlex
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from flota_plugin import power
from flota_plugin import waketimer as wt
from flota_plugin.machines import Machine, load_machines

NOW = 1_800_000_000.0  # a fixed wall clock


def machine(**kw):
    return Machine(**{"name": "studio", "profile": "p", "host": "203.0.113.7", "user": "carol",
                      "wake": "timer", "sleep": "suspend", **kw})


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def job(name="backup nocturno", prompt="run the backup on studio", at=NOW + 3600, **kw):
    return {"name": name, "prompt": prompt, "next_run_at": iso(at) if isinstance(at, float) else at,
            "enabled": True, **kw}


class Run:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        result = self.results.pop(0) if self.results else ok()
        if isinstance(result, BaseException):
            raise result
        return result


def ok(stdout=""):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def fail(code, stderr="", stdout=""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def decoded(argv):
    """The PowerShell script inside `powershell -NoProfile -EncodedCommand <b64>`."""
    remote = argv[-1] if isinstance(argv, list) else argv
    return base64.b64decode(remote.split()[-1]).decode("utf-16-le")


@pytest.fixture
def state(tmp_path):
    return tmp_path / "waketimers.json"


# --- next_wake_for -----------------------------------------------------------------------------

def test_no_jobs_means_no_wake():
    assert wt.next_wake_for("studio", [], now=NOW) is None
    assert wt.next_wake_for("studio", None, now=NOW) is None


def test_wakes_the_lead_time_before_the_next_job_that_names_the_machine():
    assert wt.next_wake_for("studio", [job()], now=NOW) == NOW + 3600 - 120


def test_custom_lead_time():
    assert wt.next_wake_for("studio", [job()], lead=600, now=NOW) == NOW + 3600 - 600


def test_soonest_matching_job_wins():
    jobs = [job(at=NOW + 7200), job(at=NOW + 1800), job(prompt="other", at=NOW + 600)]
    assert wt.next_wake_for("studio", jobs, now=NOW) == NOW + 1800 - 120


def test_paused_and_disabled_jobs_are_ignored():
    jobs = [job(enabled=False, at=NOW + 600), job(state="paused", at=NOW + 900), job(at=NOW + 5000)]
    assert wt.next_wake_for("studio", jobs, now=NOW) == NOW + 5000 - 120


def test_job_for_another_machine_is_ignored():
    jobs = [job(name="x", prompt="clean torre"), job(name="y", prompt="z", machine="torre"),
            job(name="z", prompt="studios are furniture")]  # whole word only
    assert wt.next_wake_for("studio", jobs, now=NOW) is None


def test_explicit_machine_fields_and_skills_match():
    assert wt.next_wake_for("studio", [job(name="a", prompt="b", machine="studio")], now=NOW)
    assert wt.next_wake_for("studio", [job(name="a", prompt="b", machines=["torre", "studio"])],
                            now=NOW)
    assert wt.next_wake_for("studio", [job(name="a", prompt="b", skills=["studio-backup"])],
                            now=NOW)
    assert wt.next_wake_for("Studio", [job(prompt="STUDIO: update")], now=NOW)


def test_past_and_missing_run_times_are_ignored():
    jobs = [job(at=NOW - 60), job(at=None), job(at="not a date"), job(at=NOW)]
    assert wt.next_wake_for("studio", jobs, now=NOW) is None


def test_a_job_due_within_the_lead_time_wakes_as_soon_as_possible():
    assert wt.next_wake_for("studio", [job(at=NOW + 90)], now=NOW) == NOW + wt.MIN_AHEAD


def test_epoch_and_naive_local_run_times_are_understood():
    naive = datetime.fromtimestamp(NOW + 3600).replace(microsecond=0).isoformat()  # local, no tz
    assert wt.next_wake_for("studio", [job(at=naive)], now=NOW) == NOW + 3600 - 120
    assert wt.next_wake_for("studio", [{"prompt": "studio", "next_run_at": NOW + 3600}],
                            now=NOW) == NOW + 3600 - 120


def test_garbage_jobs_do_not_raise():
    assert wt.next_wake_for("studio", ["x", 3, None, {"next_run_at": []}], now=NOW) is None


# --- arm commands per OS -----------------------------------------------------------------------

def test_linux_arm_writes_a_relative_rtc_alarm_or_uses_sudo_rtcwake():
    run = Run(ok("armed=direct\n"))
    note = wt.arm(machine(), NOW + 3000, run=run, now=lambda: NOW, state_file=None)
    argv = run.calls[0][0]
    assert argv[:2] == ["ssh", "-o"] and argv[-2] == "carol@203.0.113.7"
    script = shlex.split(argv[-1])[2]
    assert "/sys/class/rtc/rtc0/wakealarm" in script
    assert "echo 0 > $f && echo +3000 > $f" in script
    assert "sudo -n rtcwake -m no -s 3000" in script
    assert "\\" not in script and "'" not in script  # fish-safe
    assert "studio" in note


def test_macos_arm_uses_pmset_relative_wake_through_sudo():
    run = Run(ok("armed=sudoers\n"))
    wt.arm(machine(os="macos"), NOW + 600, run=run, now=lambda: NOW, state_file=None)
    script = shlex.split(run.calls[0][0][-1])[2]
    assert "sudo -n /usr/bin/pmset relative wake 600" in script


def test_windows_arm_registers_a_wake_to_run_task():
    run = Run(ok("armed=True\r\nrtcwake=0x00000001,0x00000001\r\n"))
    wt.arm(machine(os="windows"), NOW + 7200, run=run, now=lambda: NOW, state_file=None)
    remote = run.calls[0][0][-1]
    assert remote.startswith("powershell -NoProfile -EncodedCommand ")
    script = decoded(remote)
    assert "-WakeToRun" in script and "Register-ScheduledTask" in script
    assert "'FlotaWake'" in script and "AddSeconds(7200)" in script
    assert "powercfg /qh SCHEME_CURRENT SUB_SLEEP RTCWAKE" in script


def test_arm_records_the_time_for_ensure_ready(state):
    wt.arm(machine(), NOW + 3000, run=Run(ok("armed=direct")), now=lambda: NOW, state_file=state)
    assert json.loads(state.read_text()) == {"studio": NOW + 3000}
    assert wt.armed_at("studio", state) == NOW + 3000
    assert wt.armed_at("torre", state) is None
    assert wt.armed_at("studio", state.with_name("missing.json")) is None


@pytest.mark.parametrize("result, why", [
    (ok("armed=no\n"), "root"),
    (fail(3, stdout="armed=no\n"), "root"),
    (fail(255, "ssh: connect to host 203.0.113.7 port 22: Connection refused"), "Connection refused"),
    (None, "timed out"),
])
def test_arm_failures_raise(result, why):
    import subprocess
    if result is None:
        result = subprocess.TimeoutExpired("ssh", 20)
    with pytest.raises(wt.WakeTimerError, match=why):
        wt.arm(machine(), NOW + 3000, run=Run(result), now=lambda: NOW, state_file=None)


def test_windows_arm_fails_when_the_power_plan_blocks_wake_timers():
    for value in ("0x00000000", "0x00000002"):  # disabled / important wake timers only
        run = Run(ok(f"armed=True\nrtcwake={value},0x00000001\n"))
        with pytest.raises(wt.WakeTimerError, match="RTCWAKE"):
            wt.arm(machine(os="windows"), NOW + 600, run=run, now=lambda: NOW, state_file=None)


def test_windows_arm_failure_from_task_scheduler():
    run = Run(fail(1, "Register-ScheduledTask : Access is denied."))
    with pytest.raises(wt.WakeTimerError, match="Access is denied"):
        wt.arm(machine(os="windows"), NOW + 600, run=run, now=lambda: NOW, state_file=None)


def test_arm_refuses_a_time_in_the_past():
    with pytest.raises(wt.WakeTimerError, match="past"):
        wt.arm(machine(), NOW - 1, run=Run(), now=lambda: NOW, state_file=None)


# --- capability --------------------------------------------------------------------------------

def test_linux_capability_direct_route():
    run = Run(ok("rtc=yes\nroute=direct\nuser=carol\ngroup=carol\nrtcwake=/usr/bin/rtcwake\n"))
    cap = wt.capability(machine(), run=run)
    assert cap["ok"] and cap["route"] == "direct" and cap["os"] == "linux"


def test_linux_capability_sudoers_route():
    cap = wt.capability(machine(), run=Run(ok("rtc=yes\nroute=sudoers\nuser=carol\n")))
    assert cap["ok"] and cap["route"] == "sudoers"


def test_linux_capability_needs_root_once_and_prints_the_setup():
    cap = wt.capability(machine(), run=Run(ok("rtc=yes\nuser=carol\ngroup=users\nrtcwake=/usr/sbin/rtcwake\n")))
    assert not cap["ok"] and "root" in cap["reason"]
    text = "\n".join(cap["setup"])
    assert "/etc/udev/rules.d/90-flota-wakealarm.rules" in text and "chgrp users" in text
    assert "carol run `/usr/sbin/rtcwake -m no -s *` without a password" in text and wt.SETUP_DOC in text
    assert "sudo" not in text.replace("sudoers", "")


def test_linux_capability_without_an_rtc():
    cap = wt.capability(machine(), run=Run(ok("rtc=missing\n")))
    assert not cap["ok"] and "RTC" in cap["reason"]


def test_capability_unreachable():
    cap = wt.capability(machine(), run=Run(fail(255, "Connection timed out")))
    assert not cap["ok"] and "unreachable" in cap["reason"]


def test_local_capability_runs_without_ssh():
    run = Run(ok("rtc=yes\nroute=direct\nuser=carol\n"))
    cap = wt.capability(True, run=run, os="linux")
    assert cap["ok"] and run.calls[0][0][0] == "sh"


def test_macos_capability():
    assert wt.capability(machine(os="macos"), run=Run(ok("route=sudoers\nuser=carol\n")))["ok"]
    cap = wt.capability(machine(os="macos"), run=Run(ok("user=carol\n")))
    assert not cap["ok"]
    assert "carol run `/usr/bin/pmset relative wake *` without a password" in "\n".join(cap["setup"])


def test_windows_capability_probe_task_and_power_plan():
    cap = wt.capability(machine(os="windows"), run=Run(ok("route=True\r\nrtcwake=0x00000001,0x00000001\r\nuser=carol\r\n")))
    assert cap["ok"] and cap["route"] == "task"
    cap = wt.capability(machine(os="windows"), run=Run(ok("route=True\nrtcwake=0x00000000,0x00000000\nuser=carol\n")))
    assert not cap["ok"] and "RTCWAKE" in cap["reason"]
    assert "powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1" in cap["setup"]
    cap = wt.capability(machine(os="windows"), run=Run(ok("route=error Access is denied\nuser=carol\n")))
    assert not cap["ok"] and "Access is denied" in cap["reason"]


def test_windows_capability_script_cleans_up_its_probe_task():
    run = Run(ok("route=True\n"))
    wt.capability(machine(os="windows"), run=run)
    script = decoded(run.calls[0][0])
    assert "Unregister-ScheduledTask -TaskName 'FlotaWakeProbe' -Confirm:$false" in script


# --- arm_before_sleep / power integration ------------------------------------------------------

def test_power_suspend_arms_the_timer_first(state, monkeypatch):
    monkeypatch.setattr(wt, "state_path", lambda: state)
    run = Run(ok("armed=direct"), ok())
    line = power.power(machine(), "suspend", run=run, jobs=[job()], now=lambda: NOW)
    assert " failed: " not in line and "suspend sent" in line
    assert "wake timer" in line and "backup nocturno" in line
    assert "wakealarm" in run.calls[0][0][-1]
    assert run.calls[1][0][-1] == "systemctl suspend"
    assert wt.armed_at("studio", state) == NOW + 3600 - 120


def test_power_suspend_without_upcoming_job_does_not_arm(monkeypatch):
    monkeypatch.setattr(wt, "state_path", lambda: None)
    run = Run(ok())
    line = power.power(machine(), "suspend", run=run, jobs=[], now=lambda: NOW)
    assert len(run.calls) == 1 and run.calls[0][0][-1] == "systemctl suspend"
    assert "until someone turns it on" in line


def test_power_does_not_suspend_when_the_timer_cannot_be_armed(monkeypatch):
    monkeypatch.setattr(wt, "state_path", lambda: None)
    run = Run(ok("armed=no"))
    line = power.power(machine(), "suspend", run=run, jobs=[job()], now=lambda: NOW)
    assert " failed: " in line and "not suspended" in line
    assert len(run.calls) == 1


def test_power_refuses_when_cron_jobs_are_unreadable(monkeypatch):
    monkeypatch.setattr(wt, "cron_jobs", lambda: None)
    run = Run()
    line = power.power(machine(), "suspend", run=run, now=lambda: NOW)
    assert " failed: " in line and "cron" in line and run.calls == []


def test_power_secondary_wake_timer_on_a_wol_machine(monkeypatch):
    monkeypatch.setattr(wt, "state_path", lambda: None)
    m = machine(wake="wol", mac="00:11:22:33:44:55", wake_timer=True)
    run = Run(ok("armed=direct"), ok())
    power.power(m, "hibernate", run=run, jobs=[job()], now=lambda: NOW)
    assert len(run.calls) == 2 and run.calls[1][0][-1] == "systemctl hibernate"


def test_power_off_and_plain_machines_never_arm(monkeypatch):
    monkeypatch.setattr(wt, "cron_jobs", lambda: pytest.fail("cron read"))
    run = Run(ok())
    power.power(machine(wake="none"), "suspend", run=run)
    power.power(machine(), "poweroff", run=run)
    assert [c[0][-1] for c in run.calls] == ["systemctl suspend", "systemctl poweroff"]


def test_timer_lead_comes_from_the_machine(monkeypatch):
    monkeypatch.setattr(wt, "state_path", lambda: None)
    run = Run(ok("armed=direct"), ok())
    power.power(machine(timer_lead=600.0), "suspend", run=run, jobs=[job()], now=lambda: NOW)
    assert f"+{3600 - 600}" in run.calls[0][0][-1]


def test_format_when():
    when = time.mktime((2027, 1, 15, 2, 0, 0, 0, 0, -1))
    assert wt.format_when(when, when - 3600) == "02:00"
    assert wt.format_when(when, when - 3 * 86400).endswith("02:00")
    assert wt.format_when(when, when - 3 * 86400) != "02:00"


# --- config ------------------------------------------------------------------------------------

def test_machines_accepts_timer_and_wake_timer():
    loaded, problems = load_machines({"machines": {
        "studio": {"profile": "p", "host": "h", "wake": "timer", "timer_lead": "5m"},
        "torre": {"profile": "p", "host": "h", "wake": "wol", "mac": "00:11:22:33:44:55",
                  "wake_timer": True},
        "mini": {"profile": "p", "host": "h", "wake_timer": True},
        "bad": {"profile": "p", "host": "h", "wake_timer": "yes"},
    }})
    assert loaded["studio"].wake == "timer" and loaded["studio"].timer_lead == 300.0
    assert loaded["torre"].wake == "wol" and loaded["torre"].wake_timer
    assert loaded["mini"].wake == "timer"  # a timer alone is the wake method
    assert not loaded["bad"].wake_timer and any("wake_timer" in p for p in problems)
    assert Machine(name="n", profile="p", host="h").timer_lead == wt.DEFAULT_LEAD


# --- audit: an alarm left armed, and the default machine ---------------------------------------

def test_sleep_without_a_job_clears_the_alarm_armed_earlier(state, monkeypatch):
    monkeypatch.setattr(wt, "state_path", lambda: state)
    state.write_text(json.dumps({"studio": NOW + 3600, "otra": NOW + 50}))
    run = Run(ok("cleared=yes"), ok())
    line = power.power(machine(), "suspend", run=run, jobs=[], now=lambda: NOW)
    assert "echo 0 > $f" in run.calls[0][0][-1] and run.calls[1][0][-1] == "systemctl suspend"
    assert "until someone turns it on" in line and "could not be cleared" not in line
    assert json.loads(state.read_text()) == {"otra": NOW + 50}


def test_windows_clears_its_wake_task(state, monkeypatch):
    monkeypatch.setattr(wt, "state_path", lambda: state)
    state.write_text(json.dumps({"studio": NOW + 3600}))
    run = Run(ok("cleared=yes"), ok())
    power.power(machine(os="windows"), "suspend", run=run, jobs=[], now=lambda: NOW)
    assert "Unregister-ScheduledTask -TaskName 'FlotaWake'" in decoded(run.calls[0][0])
    assert wt.armed_at("studio", state) is None


def test_an_alarm_that_cannot_be_cleared_is_kept_and_said(state, monkeypatch):
    monkeypatch.setattr(wt, "state_path", lambda: state)
    state.write_text(json.dumps({"studio": NOW + 3600}))
    run = Run(fail(1), ok())  # wakealarm not writable (sudoers route)
    line = power.power(machine(), "suspend", run=run, jobs=[], now=lambda: NOW)
    assert "suspend sent" in line and "could not be cleared" in line
    assert wt.armed_at("studio", state) == NOW + 3600  # still true: it will wake then


def test_an_alarm_already_past_is_just_forgotten(state, monkeypatch):
    monkeypatch.setattr(wt, "state_path", lambda: state)
    state.write_text(json.dumps({"studio": NOW - 5}))
    run = Run(ok())
    power.power(machine(), "suspend", run=run, jobs=[], now=lambda: NOW)
    assert len(run.calls) == 1 and wt.armed_at("studio", state) is None


def test_the_default_machine_wakes_for_jobs_that_name_no_other_machine():
    nightly = job(name="nightly", prompt="run the nightly build", at=NOW + 600)
    other = job(name="sync", prompt="sync the photos on portatil", at=NOW + 300)
    assert wt.next_job_for("studio", [nightly, other], now=NOW) is None
    found = wt.next_job_for("studio", [nightly, other], now=NOW, others=["portatil"])
    assert found[1] is nightly


def test_arm_before_sleep_uses_the_default_only_when_told(state, monkeypatch):
    monkeypatch.setattr(wt, "state_path", lambda: state)
    monkeypatch.setattr(wt, "others_when_default", lambda name: pytest.fail("config read"))
    nightly = job(name="nightly", prompt="run the nightly build", at=NOW + 3600)
    run = Run(ok("armed=direct"))
    assert "nightly" in wt.arm_before_sleep(machine(), run=run, jobs=[nightly], now=lambda: NOW, others=[])
    assert wt.armed_at("studio", state) == NOW + 3600 - 120


def test_others_when_default_reads_the_fleet(monkeypatch):
    import hermes_cli.config as config_mod
    panel = {"default": " studio ", "machines": {"studio": {}, "portatil": {}}}
    monkeypatch.setattr(config_mod, "load_config_readonly",
                        lambda: {"plugins": {"entries": {"flota": {"settings": panel}}}})
    assert wt.others_when_default("studio") == ["portatil"]
    assert wt.others_when_default("portatil") is None
    monkeypatch.setattr(config_mod, "load_config_readonly", lambda: 1 / 0)
    assert wt.others_when_default("studio") is None
