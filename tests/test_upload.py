"""Unit tests for :mod:`hyperliquid_halo.upload` (no HALO or network access).

The halo-upload skill is replaced by a fake ``subprocess.run`` that records
each command and returns a scripted exit code, so the tests pin down the
three rules that keep a part from being sent twice: one skill call per
part, exit code as the only verdict, and the done-file.
"""

from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

import pytest

from hyperliquid_halo import upload, window

DAY = date(2026, 5, 5)
PART1 = "sdny_LINKED_PRIVATE_EXECUTION_V2_05052026_part1.csv"
PART2 = "sdny_LINKED_PRIVATE_EXECUTION_V2_05052026_part2.csv"


class _FakeRun:
    """``subprocess.run`` double returning scripted exit codes per part name."""

    def __init__(self, codes: dict[str, list[int]], stdout: str = "") -> None:
        self._codes = codes
        self._stdout = stdout
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        name = command[command.index("--pattern") + 1]
        code = self._codes[name].pop(0)
        return subprocess.CompletedProcess(command, code, stdout=self._stdout, stderr="")


def _settings(tmp_path: Path, **overrides: object) -> upload.UploadSettings:
    """Settings pointing at a fake skill script inside ``tmp_path``."""
    script = tmp_path / "script.py"
    script.write_text("# fake skill")
    values: dict[str, object] = {"tenant": "HLRESEARCH", "out_dir": tmp_path,
                                 "skill_script": script, "python": Path("/usr/bin/python3"),
                                 "attempts": 3, "retry_wait_seconds": 0.0, "poll_seconds": 1.0}
    values.update(overrides)
    return upload.UploadSettings(**values)  # type: ignore[arg-type]


def _exported(tmp_path: Path, parts: tuple[str, ...] = (PART1, PART2), day: date = DAY,
              status: str = window.STATUS_OK) -> None:
    """Write the parts and an export ledger record for ``day``."""
    for name in parts:
        (tmp_path / name).write_text("data")
    window.append_status(tmp_path, window.DayRecord(day=day, status=status, rows=2, parts=parts))


def test_command_covers_exactly_one_file(tmp_path: Path) -> None:
    """The skill is invoked with the exact name as pattern, concurrency 1 and --yes."""
    settings = _settings(tmp_path, region="uat-eu-central-1")
    command = upload.build_upload_command(settings, PART1)
    assert command[:2] == ["/usr/bin/python3", str(settings.skill_script)]
    assert command[2:] == ["--tenant", "HLRESEARCH", "--dir", str(tmp_path), "--pattern", PART1,
                           "--file-type", "LINKED_PRIVATE_EXECUTION_V2",
                           "--region", "uat-eu-central-1", "--concurrency", "1", "--yes"]


def test_success_is_judged_by_exit_code_only(tmp_path: Path) -> None:
    """Regression: exit 0 is success even when the log line lacks the old ' OK   ' spacing."""
    _exported(tmp_path, (PART1,))
    run = _FakeRun({PART1: [0]}, stdout="2026-09-23 INFO __main__: OK sdny_..._part1.csv (5 bytes)")
    result = upload.upload_part(_settings(tmp_path), PART1, run=run, sleep=lambda _: None)
    assert result.ok and result.attempts == 1
    assert result.detail.startswith("OK ")
    assert len(run.commands) == 1


def test_nonzero_exit_retries_then_fails(tmp_path: Path) -> None:
    """Each failed try waits and retries; after the last try the part is reported failed."""
    _exported(tmp_path, (PART1,))
    run = _FakeRun({PART1: [1, 2, 1]}, stdout="ERROR: 503 from HALO")
    waits: list[float] = []
    settings = _settings(tmp_path, attempts=3, retry_wait_seconds=20.0)
    result = upload.upload_part(settings, PART1, run=run, sleep=waits.append)
    assert not result.ok and result.attempts == 3
    assert len(run.commands) == 3
    assert waits == [20.0, 20.0], "no wait after the final attempt"
    assert "rc=1" in result.detail and "503" in result.detail


def test_recovers_on_a_later_attempt(tmp_path: Path) -> None:
    """A transient failure followed by exit 0 counts as success on that attempt."""
    _exported(tmp_path, (PART1,))
    run = _FakeRun({PART1: [1, 0]})
    result = upload.upload_part(_settings(tmp_path), PART1, run=run, sleep=lambda _: None)
    assert result.ok and result.attempts == 2


def test_missing_local_file_never_calls_the_skill(tmp_path: Path) -> None:
    """A part the ledger names but the disk lacks is a failure without any upload attempt."""
    window.append_status(tmp_path, window.DayRecord(day=DAY, status=window.STATUS_OK,
                                                    parts=(PART1,)))
    run = _FakeRun({})
    result = upload.upload_part(_settings(tmp_path), PART1, run=run, sleep=lambda _: None)
    assert not result.ok and result.detail == "missing locally" and run.commands == []


def test_done_file_skips_sent_parts_and_records_new_ones(tmp_path: Path) -> None:
    """Names in the done-file are never re-sent; new successes are appended at once."""
    _exported(tmp_path)
    settings = _settings(tmp_path, workers=1)
    done_path = upload.done_file_path(tmp_path, "HLRESEARCH")
    done_path.write_text(f"{PART1}\n")
    run = _FakeRun({PART2: [0]})

    summary = upload.upload_window(settings, [DAY], run=run, sleep=lambda _: None)

    assert summary.ok
    assert summary.sent == (PART2,) and summary.already_done == (PART1,)
    assert [c[c.index("--pattern") + 1] for c in run.commands] == [PART2]
    assert upload.read_done(done_path) == {PART1, PART2}
    assert done_path.name == "upload_HLRESEARCH.done"


def test_rerun_after_full_success_sends_nothing(tmp_path: Path) -> None:
    """The second run over an uploaded window makes no skill call at all."""
    _exported(tmp_path)
    settings = _settings(tmp_path, workers=2)
    first = upload.upload_window(settings, [DAY], run=_FakeRun({PART1: [0], PART2: [0]}),
                                 sleep=lambda _: None)
    assert set(first.sent) == {PART1, PART2}
    run = _FakeRun({})
    second = upload.upload_window(settings, [DAY], run=run, sleep=lambda _: None)
    assert second.sent == () and set(second.already_done) == {PART1, PART2}
    assert run.commands == []


def test_failed_part_is_reported_and_not_recorded(tmp_path: Path) -> None:
    """A part that exhausts its attempts stays out of the done-file for the next run."""
    _exported(tmp_path)
    settings = _settings(tmp_path, workers=1, attempts=2)
    run = _FakeRun({PART1: [0], PART2: [1, 1]})
    summary = upload.upload_window(settings, [DAY], run=run, sleep=lambda _: None)
    assert not summary.ok
    assert summary.sent == (PART1,)
    assert [r.name for r in summary.failed] == [PART2]
    assert upload.read_done(upload.done_file_path(tmp_path, "HLRESEARCH")) == {PART1}


def test_waits_for_the_export_record(tmp_path: Path) -> None:
    """With wait on, the uploader polls until the day's record appears, then sends."""
    (tmp_path / PART1).write_text("data")
    settings = _settings(tmp_path, workers=1)
    polls: list[float] = []

    def sleep(seconds: float) -> None:
        polls.append(seconds)
        if len(polls) == 2:  # the export "finishes" during the second poll
            window.append_status(tmp_path, window.DayRecord(day=DAY, status=window.STATUS_OK,
                                                            rows=1, parts=(PART1,)))

    events: list[str] = []
    summary = upload.upload_window(settings, [DAY], run=_FakeRun({PART1: [0]}), sleep=sleep,
                                   on_event=events.append)
    assert summary.sent == (PART1,) and polls == [1.0, 1.0]
    assert any("waiting for the export" in e for e in events)


def test_no_wait_skips_unexported_and_failed_days(tmp_path: Path) -> None:
    """Without waiting, a missing record or a failed export skips the day and fails the run."""
    _exported(tmp_path, (PART1,), day=date(2026, 5, 6), status=window.STATUS_DQ_FAILED)
    run = _FakeRun({})
    summary = upload.upload_window(_settings(tmp_path), [DAY, date(2026, 5, 6)],
                                   wait_for_export=False, run=run, sleep=lambda _: None)
    assert run.commands == [] and not summary.ok
    assert [(s.day, s.reason) for s in summary.skipped_days] == [
        (DAY, "not exported"), (date(2026, 5, 6), "export dq_failed"),
    ]


def test_dry_run_plans_without_calling_anything(tmp_path: Path) -> None:
    """Dry run lists the parts that would go and touches neither HALO nor the done-file."""
    _exported(tmp_path)
    upload.done_file_path(tmp_path, "HLRESEARCH").write_text(f"{PART1}\n")
    run = _FakeRun({})
    summary = upload.upload_window(_settings(tmp_path), [DAY], dry_run=True, run=run,
                                   sleep=lambda _: None)
    assert summary.planned == (PART2,) and summary.sent == () and run.commands == []
    assert upload.read_done(upload.done_file_path(tmp_path, "HLRESEARCH")) == {PART1}


def test_resolve_skill_script_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit path beats the environment variable; a missing script is a config error."""
    explicit, from_env = tmp_path / "explicit.py", tmp_path / "env.py"
    explicit.write_text(""), from_env.write_text("")
    monkeypatch.setenv(upload.SKILL_SCRIPT_ENV, str(from_env))
    assert upload.resolve_skill_script(explicit) == explicit
    assert upload.resolve_skill_script() == from_env
    monkeypatch.setenv(upload.SKILL_SCRIPT_ENV, str(tmp_path / "missing.py"))
    with pytest.raises(upload.UploadConfigError, match="not found"):
        upload.resolve_skill_script()


def test_settings_reject_nonsense(tmp_path: Path) -> None:
    """An empty tenant or zero attempts cannot produce a runnable configuration."""
    with pytest.raises(upload.UploadConfigError):
        _settings(tmp_path, tenant="  ")
    with pytest.raises(upload.UploadConfigError):
        _settings(tmp_path, attempts=0)
