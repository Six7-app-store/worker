"""Wait until the deployment's VMs have finished setting themselves up.

``tofu apply`` returns as soon as OpenStack reports the instances ACTIVE.
Since apps provision their VMs on first boot via cloud-init (ADR 0010),
an ACTIVE VM can still be minutes away from usable — or about to fail.
The deployment must not be reported as done before that.

The signal is generic and needs nothing from the app: systemd writes the
outcome of cloud-init's last stage to the serial console, which the
worker can read with the per-task credentials it already holds:

* ``[  OK  ] Finished cloud-final.service``   — setup done
* ``[FAILED] Failed to start cloud-final.service`` — setup failed

Pure decision logic (``classify_console``, ``error_lines``) is separate
from the polling loop so both can be tested without a cloud.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

SetupState = Literal["running", "ready", "failed"]

# Nova's console-log API drops control characters, ESC included, so a
# colour code arrives as plain "[0;1;39m". Match it with and without ESC.
_ANSI = re.compile(r"\x1b?\[[0-9;]*m")

_READY = "Finished cloud-final.service"
_FAILED = "Failed to start cloud-final.service"
_STARTING = "Starting cloud-final.service"
# An app may reboot at the end of its setup (power_state). The marker
# from before the reboot then doesn't describe the system that comes up.
_REBOOT = "reboot: Restarting system"

# Console lines worth showing when setup failed. Lines that look like
# they could carry a credential are never copied into the deployment log.
_ERROR_HINT = re.compile(r"FEHLER|ERROR|[Ee]rror|Failed|No space left|\bE: ")
_SECRET_HINT = re.compile(r"passw|secret|token|credential", re.IGNORECASE)

# A long setup (Kali: half an hour) must not look like a hang in the log.
_PROGRESS_EVERY_S = 300


def _strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def classify_console(text: str) -> SetupState:
    """The setup state a console log shows, judged by the latest marker."""
    clean = _strip_ansi(text)
    positions = {
        "ready": clean.rfind(_READY),
        "failed": clean.rfind(_FAILED),
        "running": max(clean.rfind(_STARTING), clean.rfind(_REBOOT)),
    }
    state, pos = max(positions.items(), key=lambda kv: kv[1])
    return state if pos >= 0 else "running"


def error_lines(text: str, limit: int = 15) -> list[str]:
    """The last ``limit`` console lines that look like an error."""
    hits = [
        line.strip()
        for line in _strip_ansi(text).splitlines()
        if _ERROR_HINT.search(line) and not _SECRET_HINT.search(line)
    ]
    return hits[-limit:]


@dataclass
class SetupOutcome:
    """How waiting ended.

    ``status``:
      * ``ready``   — every VM finished setting up (or was skipped, see below)
      * ``failed``  — a VM's setup failed; ``server_id``/``errors`` say which and why
      * ``timeout`` — not every VM was done within the time limit
    ``skipped`` lists VMs whose console never showed cloud-init or could not
    be read; they don't block the deployment.
    """

    status: Literal["ready", "failed", "timeout"]
    server_id: str | None = None
    errors: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def wait_for_setup(
    server_ids: list[str],
    read_console: Callable[[str], str | None],
    *,
    log: Callable[[str], None],
    timeout_s: float,
    poll_s: float,
    no_cloud_init_after_s: float,
    unreadable_after_s: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> SetupOutcome:
    """Poll every VM's console until each is ready, one fails, or time runs out.

    A VM whose console mentions no cloud-init at all after
    ``no_cloud_init_after_s`` (an image without it) or whose console can't
    be read for ``unreadable_after_s`` is skipped rather than blocking the
    deployment for the whole timeout.
    """
    start = clock()
    next_progress_s = _PROGRESS_EVERY_S
    pending = list(server_ids)
    skipped: list[str] = []
    unreadable_since: dict[str, float] = {}

    while True:
        elapsed = clock() - start
        for sid in list(pending):
            text = read_console(sid)
            if text is None:
                since = unreadable_since.setdefault(sid, elapsed)
                if elapsed - since >= unreadable_after_s:
                    log(f"Console of {sid} unreadable for {int(elapsed - since)}s - not waiting for it")
                    pending.remove(sid)
                    skipped.append(sid)
                continue
            unreadable_since.pop(sid, None)

            state = classify_console(text)
            if state == "failed":
                return SetupOutcome(
                    status="failed",
                    server_id=sid,
                    errors=error_lines(text),
                    pending=[s for s in pending if s != sid],
                    skipped=skipped,
                )
            if state == "ready":
                log(f"{sid} finished setting up after {int(elapsed)}s")
                pending.remove(sid)
            elif "cloud-init" not in text and elapsed >= no_cloud_init_after_s:
                log(f"{sid} shows no cloud-init after {int(elapsed)}s - not waiting for it")
                pending.remove(sid)
                skipped.append(sid)

        if not pending:
            return SetupOutcome(status="ready", skipped=skipped)
        if elapsed >= timeout_s:
            return SetupOutcome(status="timeout", pending=pending, skipped=skipped)
        if elapsed >= next_progress_s:
            log(f"Still setting up after {int(elapsed // 60)} min: {len(pending)} VM(s)")
            next_progress_s += _PROGRESS_EVERY_S
        sleep(poll_s)
