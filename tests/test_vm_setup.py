"""Tests for waiting on cloud-init after deploy (``app/services/vm_setup.py``).

The console snippets are cut from real newstack VMs on 14.10.2026: the
staging VM (Ubuntu, setup done), the Kali VM whose setup failed on a full
EFI partition, and the Kali app's reboot at the end of its setup.
"""

import pytest

from app.services.vm_setup import classify_console, error_lines, wait_for_setup

READY = (
    "         Starting \x1b[0;1;39mcloud-final.service\x1b[0m - Cloud-init: Final Stage...\n"
    "[    9.42] cloud-init[1079]: Cloud-init v. 25.3 finished at Wed, 14 Oct 2026 14:16:45 +0000.\n"
    "[\x1b[0;32m  OK  \x1b[0m] Finished \x1b[0;1;39mcloud-final.service\x1b[0m - Cloud-init: Final Stage.\n"
)

FAILED = (
    "[  690.62] cloud-init[594]: install: error writing '/boot/efi/x/initrd.img': No space left on device\n"
    "[  690.64] cloud-init[594]: dpkg: error processing package initramfs-tools (--configure):\n"
    "[  708.27] cloud-init[594]: FEHLER in /opt/kali-app/02-desktop.sh Zeile 30\n"
    "[  708.30] cloud-init[594]: chpasswd: password for hannah-roth set\n"
    "[\x1b[0;1;31mFAILED\x1b[0m] Failed to start \x1b[0;1;39mcloud-final.service\x1b[0m - Cloud-init: Final Stage.\n"
)

INSTALLING = "[  805.04] cloud-init[591]: Unpacking gnome-disk-utility (46.1-3) ...\n"

# What `openstack console log show` really returns (VSCode-App, 22.10.2026):
# Nova strips control characters, so the colour codes lose their ESC.
READY_VIA_API = (
    "[  138.021271] cloud-init[1041]: VS-Code-Team-VM bereit - Log unter /var/log/vscode-provision.log\n"
    "[[0;32m  OK  [0m] Finished [0;1;39mcloud-final.service[0m - Cloud-init: Final Stage.\n"
    "[[0;32m  OK  [0m] Reached target [0;1;39mcloud-init.target[0m - Cloud-init target.\n"
)

FAILED_VIA_API = (
    "[  708.27] cloud-init[594]: FEHLER in /opt/kali-app/02-desktop.sh Zeile 30\n"
    "[[0;1;31mFAILED[0m] Failed to start [0;1;39mcloud-final.service[0m - Cloud-init: Final Stage.\n"
)

# Kali after its reboot (staging, 24.10.2026): systemd stops writing status
# lines once the login prompt is on the serial console, so no "Finished"
# line follows cloud-init's own closing line.
KALI_SECOND_BOOT = (
    "[[0;32m  OK  [0m] Started [0;1;39mssh.service[0m - OpenBSD Secure Shell server.\n"
    "Kali GNU/Linux Rolling kali ttyS0\n"
    "kali login: [  186.70] kali-app-ipv6[838]: kali-app-ipv6: globale IPv6 jetzt vorhanden\n"
    "[  187.14] cloud-init[442]: Cloud-init v. 26.2 running 'modules:final' at Sat, 24 Oct 2026 17:05:44 +0000.\n"
    "[  187.16] cloud-init[442]: Cloud-init v. 26.2 finished at Sat, 24 Oct 2026 17:05:44 +0000. Up 187.16 seconds.\n"
    "[  187.16] cloud-init[442]: Kali-App: Teams: Team #1, Nutzer: 3\n"
)


@pytest.mark.unit
class TestClassifyConsole:
    def test_finished_final_stage_is_ready(self):
        assert classify_console(READY) == "ready"

    def test_failed_final_stage_is_failed(self):
        assert classify_console(FAILED) == "failed"

    def test_still_installing_is_running(self):
        assert classify_console(INSTALLING) == "running"

    def test_empty_console_is_running(self):
        assert classify_console("") == "running"

    def test_reboot_after_finish_waits_for_the_next_boot(self):
        # Kali reboots at the end of its setup; RDP only comes up afterwards.
        assert classify_console(READY + "[  900.1] reboot: Restarting system\n") == "running"

    def test_finish_after_reboot_is_ready(self):
        assert classify_console(READY + "[  900.1] reboot: Restarting system\n" + READY) == "ready"

    def test_a_later_success_wins_over_an_earlier_failure(self):
        assert classify_console(FAILED + READY) == "ready"

    def test_finished_without_esc_is_ready(self):
        assert classify_console(READY_VIA_API) == "ready"

    def test_failed_without_esc_is_failed(self):
        assert classify_console(FAILED_VIA_API) == "failed"

    def test_cloud_init_closing_line_is_ready_without_systemd_status(self):
        assert classify_console(KALI_SECOND_BOOT) == "ready"

    def test_kali_reboot_then_closing_line_is_ready(self):
        assert classify_console(READY + "[  900.1] reboot: Restarting system\n" + KALI_SECOND_BOOT) == "ready"

    def test_start_of_final_stage_alone_is_not_ready(self):
        started = KALI_SECOND_BOOT.split("[  187.16]")[0]
        assert classify_console(started) == "running"

    def test_failure_after_closing_line_still_fails(self):
        # cloud-init prints its closing line before systemd's verdict.
        closing = "[  708.29] cloud-init[594]: Cloud-init v. 26.2 finished at Sat, 24 Oct 2026 17:05:44 +0000.\n"
        assert classify_console(FAILED.replace("[\x1b[0;1;31mFAILED", closing + "[\x1b[0;1;31mFAILED")) == "failed"

    def test_error_lines_without_esc_are_readable(self):
        assert error_lines(FAILED_VIA_API)[-1].startswith("[FAILED] Failed to start cloud-final.service")


@pytest.mark.unit
class TestErrorLines:
    def test_keeps_the_lines_that_explain_the_failure(self):
        lines = error_lines(FAILED)
        assert any("No space left on device" in line for line in lines)
        assert any("FEHLER in /opt/kali-app/02-desktop.sh" in line for line in lines)
        assert lines[-1].startswith("[FAILED] Failed to start cloud-final.service")

    def test_never_copies_lines_that_mention_passwords(self):
        noisy = FAILED + "[  1.0] cloud-init: error: password for anna is Geheim!1\n"
        assert not any("assword" in line for line in error_lines(noisy))

    def test_is_limited(self):
        many = "".join(f"[{i}] error {i}\n" for i in range(50))
        lines = error_lines(many, limit=5)
        assert lines == [f"[{i}] error {i}" for i in range(45, 50)]


class _FakeTime:
    """Monotonic clock that only advances when the loop sleeps."""

    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _wait(consoles, **overrides):
    """Run ``wait_for_setup`` with scripted console reads.

    ``consoles`` maps server id -> list of successive reads; the last read
    repeats once the list is exhausted.
    """
    fake = _FakeTime()
    reads = {sid: list(seq) for sid, seq in consoles.items()}
    messages = []

    def read(sid):
        seq = reads[sid]
        return seq.pop(0) if len(seq) > 1 else seq[0]

    kwargs = {
        "log": messages.append,
        "timeout_s": 600,
        "poll_s": 30,
        "no_cloud_init_after_s": 120,
        "unreadable_after_s": 120,
        "clock": fake.clock,
        "sleep": fake.sleep,
    }
    kwargs.update(overrides)
    outcome = wait_for_setup(list(consoles), read, **kwargs)
    return outcome, fake.now, messages


@pytest.mark.unit
class TestWaitForSetup:
    def test_waits_until_every_vm_is_ready(self):
        cloud_init_running = "cloud-init[1]: " + INSTALLING
        outcome, waited, _ = _wait({"a": [cloud_init_running, READY], "b": [cloud_init_running] * 3 + [READY]})
        assert outcome.status == "ready"
        assert waited == 90  # b became ready on the fourth poll

    def test_stops_at_the_first_failed_vm(self):
        outcome, waited, _ = _wait({"a": ["cloud-init " + INSTALLING, FAILED], "b": ["cloud-init " + INSTALLING]})
        assert outcome.status == "failed"
        assert outcome.server_id == "a"
        assert any("No space left" in line for line in outcome.errors)
        assert waited == 30

    def test_times_out(self):
        outcome, waited, _ = _wait({"a": ["cloud-init " + INSTALLING]}, timeout_s=300)
        assert outcome.status == "timeout"
        assert outcome.pending == ["a"]
        assert waited == 300

    def test_does_not_wait_for_a_vm_without_cloud_init(self):
        outcome, waited, messages = _wait({"win": ["Windows boot manager"]})
        assert outcome.status == "ready"
        assert outcome.skipped == ["win"]
        assert waited == 120
        assert any("no cloud-init" in m for m in messages)

    def test_does_not_wait_for_an_unreadable_console(self):
        outcome, waited, _ = _wait({"a": [None]})
        assert outcome.status == "ready"
        assert outcome.skipped == ["a"]
        assert waited == 120

    def test_a_console_that_becomes_readable_again_is_waited_for(self):
        outcome, _, _ = _wait({"a": [None, None, "cloud-init " + INSTALLING, READY]})
        assert outcome.status == "ready"
        assert outcome.skipped == []

    def test_reports_progress_in_long_setups(self):
        reads = ["cloud-init " + INSTALLING] * 25 + [READY]
        outcome, _, messages = _wait({"a": reads}, timeout_s=3600)
        assert outcome.status == "ready"
        assert any(m.startswith("Still setting up after 5 min") for m in messages)
