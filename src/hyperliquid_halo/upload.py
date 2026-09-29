"""Resume-safe upload of an exported window to a HALO tenant.

HALO cannot de-duplicate a part that is sent twice, and nothing can be
deleted from the uploader side, so the one property this module must have
is: **a part that has been sent successfully is never sent again.** It gets
that from three rules, each of which was learned the hard way on the first
hand-written runner:

1. Every part is uploaded by its own invocation of the ``halo-upload`` skill
   (exact file name as the ``--pattern``, ``--concurrency 1``), so an
   invocation covers exactly one file and its exit code speaks for that
   file alone.
2. Success is judged by the exit code only. The first runner matched a
   substring of the skill's log line, which never matched, so parts that
   had uploaded were re-sent. The skill exits ``0`` only when every matched
   file uploaded; that is the whole contract.
3. Each success is appended to a done-file (``upload_<TENANT>.done`` in the
   output directory) the moment it happens, and a restart skips every name
   in it. Failures are retried a bounded number of times and then reported.

Which parts exist is read from the export ledger written by
:mod:`hyperliquid_halo.window` (``export_window.jsonl``): only parts named
by an ``ok`` record are sent, and with ``wait_for_export`` the uploader
polls for days that are still exporting, so export and upload can run side
by side.

The skill itself is an external script (``HALO_UPLOAD_SCRIPT``, defaulting
to ``~/.claude/skills/halo-upload/script.py``) that holds the per-tenant API
keys in its own ``.env``; this repo never sees them. That dependency is a
known portability hazard and is documented in the README.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .window import DayRecord, read_status

logger = logging.getLogger(__name__)

SKILL_SCRIPT_ENV = "HALO_UPLOAD_SCRIPT"
"""Environment variable overriding the path of the halo-upload skill script."""

DEFAULT_SKILL_SCRIPT = "~/.claude/skills/halo-upload/script.py"
"""Where the halo-upload skill lives on a standard Claude Code install."""

DEFAULT_FILE_TYPE = "LINKED_PRIVATE_EXECUTION_V2"
"""The live executions route: ingested automatically whatever the data age."""

DEFAULT_REGION = "uat-eu-central-1"
"""HALO region of the research tenants."""

DEFAULT_WORKERS = 3
DEFAULT_ATTEMPTS = 3
DEFAULT_RETRY_WAIT_SECONDS = 20.0
DEFAULT_POLL_SECONDS = 60.0

RunFn = Callable[..., subprocess.CompletedProcess[str]]
SleepFn = Callable[[float], None]


class UploadConfigError(RuntimeError):
    """Raised when the upload cannot start (missing skill script, bad settings)."""


@dataclass(frozen=True)
class UploadSettings:
    """Everything an upload run needs, resolved once up front.

    Attributes:
        tenant: HALO tenant name; selects ``HALO_API_KEY_<TENANT>`` in the
            skill's ``.env`` and names the done-file.
        out_dir: Directory holding the parts and the export ledger.
        skill_script: Path of the halo-upload skill script.
        python: Interpreter used to run the skill (this venv by default; it
            carries ``requests`` and ``python-dotenv``).
        file_type: HALO file type applied to every part.
        region: HALO region slug.
        workers: Parts uploaded in parallel (each in its own skill process).
        attempts: Tries per part before it is reported as failed.
        retry_wait_seconds: Pause between tries of the same part.
        poll_seconds: Pause between ledger checks while waiting for a day's
            export to finish.
    """

    tenant: str
    out_dir: Path
    skill_script: Path
    python: Path = field(default_factory=lambda: Path(sys.executable))
    file_type: str = DEFAULT_FILE_TYPE
    region: str = DEFAULT_REGION
    workers: int = DEFAULT_WORKERS
    attempts: int = DEFAULT_ATTEMPTS
    retry_wait_seconds: float = DEFAULT_RETRY_WAIT_SECONDS
    poll_seconds: float = DEFAULT_POLL_SECONDS

    def __post_init__(self) -> None:
        if not self.tenant.strip():
            raise UploadConfigError("tenant must not be empty")
        if self.workers < 1 or self.attempts < 1:
            raise UploadConfigError("workers and attempts must be at least 1")


@dataclass(frozen=True)
class PartResult:
    """Outcome of uploading one part.

    Attributes:
        name: Part file name.
        ok: ``True`` when a skill invocation exited ``0`` for it.
        attempts: How many invocations were made.
        detail: The skill's ``OK``/``FAIL`` line when found, else the exit
            code and the last line of its output.
    """

    name: str
    ok: bool
    attempts: int
    detail: str = ""


@dataclass(frozen=True)
class DaySkip:
    """A day the uploader did not touch, and why."""

    day: date
    reason: str


@dataclass(frozen=True)
class UploadSummary:
    """Outcome of :func:`upload_window`.

    Attributes:
        sent: Names uploaded successfully in this run, in completion order.
        already_done: Names skipped because the done-file listed them.
        failed: Parts that exhausted their attempts, or were missing locally.
        skipped_days: Days not uploaded (export failed, or not exported and
            not waited for).
        planned: With ``dry_run``, the names that would have been sent.
    """

    sent: tuple[str, ...] = ()
    already_done: tuple[str, ...] = ()
    failed: tuple[PartResult, ...] = ()
    skipped_days: tuple[DaySkip, ...] = ()
    planned: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """``True`` when nothing failed and no day was skipped."""
        return not self.failed and not self.skipped_days


def resolve_skill_script(explicit: Path | None = None) -> Path:
    """Locate the halo-upload skill script.

    Precedence: ``explicit`` argument, then :data:`SKILL_SCRIPT_ENV`, then
    :data:`DEFAULT_SKILL_SCRIPT`. ``~`` is expanded.

    Args:
        explicit: Path given on the command line, if any.

    Returns:
        The resolved, existing script path.

    Raises:
        UploadConfigError: If the resolved path does not exist.
    """
    raw = explicit or Path(os.environ.get(SKILL_SCRIPT_ENV, "").strip() or DEFAULT_SKILL_SCRIPT)
    path = raw.expanduser()
    if not path.is_file():
        raise UploadConfigError(
            f"halo-upload skill script not found at {path}. Install the skill or set "
            f"{SKILL_SCRIPT_ENV} (or --skill-script) to its script.py."
        )
    return path


def done_file_path(out_dir: Path, tenant: str) -> Path:
    """Path of the done-file recording every part already sent to ``tenant``."""
    return out_dir / f"upload_{tenant.upper()}.done"


def read_done(path: Path) -> set[str]:
    """Names listed in a done-file (empty set when it does not exist)."""
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


class DoneLedger:
    """Append-only, thread-safe writer for the done-file.

    Each name is written and flushed as soon as its upload succeeds, so a
    process killed mid-day loses nothing already sent.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def append(self, name: str) -> None:
        """Record ``name`` as sent."""
        with self._lock, self._path.open("a", encoding="utf-8") as fh:
            fh.write(name + "\n")
            fh.flush()


def build_upload_command(settings: UploadSettings, name: str) -> list[str]:
    """The exact skill invocation for one part.

    The file name is passed as the glob ``--pattern``; part names contain
    no glob metacharacters, so it matches that one file and nothing else.
    ``--concurrency 1`` and ``--yes`` keep the call single-file and
    non-interactive.
    """
    return [
        str(settings.python), str(settings.skill_script),
        "--tenant", settings.tenant,
        "--dir", str(settings.out_dir),
        "--pattern", name,
        "--file-type", settings.file_type,
        "--region", settings.region,
        "--concurrency", "1",
        "--yes",
    ]


def _status_line(output: str, returncode: int) -> str:
    """Pick the human-readable outcome line out of the skill's output."""
    for line in output.splitlines():
        stripped = line.split("__main__: ")[-1].strip()
        if stripped.startswith(("OK ", "FAIL ")):
            return stripped[:200]
    tail = output.strip().splitlines()
    return f"rc={returncode}" + (f": {tail[-1][:200]}" if tail else "")


def upload_part(
    settings: UploadSettings,
    name: str,
    *,
    run: RunFn = subprocess.run,
    sleep: SleepFn = time.sleep,
) -> PartResult:
    """Upload one part with bounded retries.

    Success is the skill's exit code being ``0``; nothing in its output is
    interpreted for the verdict. A part missing from ``out_dir`` is reported
    as failed without calling the skill.

    Args:
        settings: Resolved upload settings.
        name: Part file name inside ``settings.out_dir``.
        run: ``subprocess.run``-compatible callable (injectable for tests).
        sleep: ``time.sleep``-compatible callable (injectable for tests).

    Returns:
        The :class:`PartResult`.
    """
    if not (settings.out_dir / name).is_file():
        return PartResult(name=name, ok=False, attempts=0, detail="missing locally")
    command = build_upload_command(settings, name)
    detail = ""
    for attempt in range(1, settings.attempts + 1):
        completed = run(command, capture_output=True, text=True)
        detail = _status_line(completed.stdout + completed.stderr, completed.returncode)
        if completed.returncode == 0:
            logger.info("OK   %s (attempt %d): %s", name, attempt, detail)
            return PartResult(name=name, ok=True, attempts=attempt, detail=detail)
        logger.warning("FAIL %s (attempt %d of %d): %s", name, attempt, settings.attempts, detail)
        if attempt < settings.attempts:
            sleep(settings.retry_wait_seconds)
    return PartResult(name=name, ok=False, attempts=settings.attempts, detail=detail)


def _wait_for_record(
    settings: UploadSettings,
    day: date,
    *,
    wait: bool,
    sleep: SleepFn,
    on_event: Callable[[str], None],
) -> DayRecord | None:
    """Return the day's ledger record, polling for it when ``wait`` is set."""
    announced = False
    while True:
        record = read_status(settings.out_dir).get(day)
        if record is not None or not wait:
            return record
        if not announced:
            on_event(f"{day}: waiting for the export to finish")
            announced = True
        sleep(settings.poll_seconds)


def upload_window(
    settings: UploadSettings,
    days: Sequence[date],
    *,
    wait_for_export: bool = True,
    dry_run: bool = False,
    on_event: Callable[[str], None] | None = None,
    run: RunFn = subprocess.run,
    sleep: SleepFn = time.sleep,
) -> UploadSummary:
    """Upload every not-yet-sent part of the given days, in date order.

    Args:
        settings: Resolved upload settings.
        days: Transact dates to upload, processed in the order given.
        wait_for_export: Poll the export ledger for days it does not list
            yet (the export is still running). When ``False`` such days are
            skipped and reported.
        dry_run: Only compute what would be sent; call nothing.
        on_event: Optional progress callback receiving one line per event.
        run: ``subprocess.run``-compatible callable (injectable for tests).
        sleep: ``time.sleep``-compatible callable (injectable for tests).

    Returns:
        The :class:`UploadSummary` for the run.
    """
    notify = on_event or (lambda line: logger.info("%s", line))
    done_path = done_file_path(settings.out_dir, settings.tenant)
    done = read_done(done_path)
    ledger = DoneLedger(done_path)
    sent: list[str] = []
    already: list[str] = []
    failed: list[PartResult] = []
    skipped: list[DaySkip] = []
    planned: list[str] = []
    notify(f"Upload to {settings.tenant} ({settings.region}, {settings.file_type}); "
           f"{len(done)} part(s) already recorded in {done_path.name}")

    for day in days:
        record = _wait_for_record(settings, day, wait=wait_for_export, sleep=sleep, on_event=notify)
        if record is None:
            skipped.append(DaySkip(day, "not exported"))
            notify(f"{day}: SKIPPED, no export record")
            continue
        if not record.ok:
            skipped.append(DaySkip(day, f"export {record.status}"))
            notify(f"{day}: SKIPPED, export status {record.status}")
            continue
        todo = [name for name in record.parts if name not in done]
        already.extend(name for name in record.parts if name in done)
        notify(f"{day}: {len(record.parts)} part(s), {len(todo)} to send")
        if dry_run:
            planned.extend(todo)
            continue

        def send(name: str) -> PartResult:
            result = upload_part(settings, name, run=run, sleep=sleep)
            if result.ok:
                ledger.append(name)
            return result

        with ThreadPoolExecutor(max_workers=settings.workers) as pool:
            results = list(pool.map(send, todo))
        day_failed: list[str] = []
        for result in results:
            if result.ok:
                sent.append(result.name)
                done.add(result.name)
            else:
                failed.append(result)
                day_failed.append(result.name)
        notify(f"{day}: uploaded {len(todo) - len(day_failed)}/{len(todo)}"
               + (f", FAILED {', '.join(day_failed)}" if day_failed else ""))

    return UploadSummary(sent=tuple(sent), already_done=tuple(already), failed=tuple(failed),
                         skipped_days=tuple(skipped), planned=tuple(planned))
