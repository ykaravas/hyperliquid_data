"""Unit tests for the option wiring of :mod:`hyperliquid_halo.cli`.

The click declarations are inspected directly, and the window commands are
run through :class:`click.testing.CliRunner` with the exporter replaced by a
double, so nothing here touches Snowflake, ClickHouse or HALO.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from hyperliquid_halo import window
from hyperliquid_halo.cli import (
    _coin_selection,
    check_coverage_cmd,
    cli,
    export_execs_cmd,
    export_orders_cmd,
    export_window_cmd,
    upload_window_cmd,
)


def _option(command: click.Command, name: str) -> click.Option:
    """Return the option of ``command`` whose parameter name is ``name``.

    Args:
        command: The click command to inspect.
        name: The Python parameter name of the option (``exclude_post_only``).

    Returns:
        The matching :class:`click.Option`.

    Raises:
        LookupError: If the command declares no such option.
    """
    for param in command.params:
        if isinstance(param, click.Option) and param.name == name:
            return param
    raise LookupError(f"{command.name} has no option named {name!r}")


def test_export_orders_excludes_post_only_by_default() -> None:
    """Post-only quoting is left out unless --include-post-only is passed."""
    option = _option(export_orders_cmd, "exclude_post_only")
    assert option.default is True
    assert option.opts == ["--exclude-post-only"]
    assert option.secondary_opts == ["--include-post-only"]


def test_coin_option_is_repeatable_on_both_exporters() -> None:
    """--coin can be given several times; one value keeps the single-market filter."""
    for command in (export_execs_cmd, export_orders_cmd):
        option = _option(command, "coins")
        assert option.multiple is True
        assert option.opts == ["--coin"]
    assert _coin_selection(()) is None
    assert _coin_selection(("BTC",)) == "BTC"
    assert _coin_selection(("xyz:CL", "xyz:NATGAS")) == ("xyz:CL", "xyz:NATGAS")


def test_window_commands_take_a_feed() -> None:
    """The three window commands share the --feed option, executions by default."""
    for command in (check_coverage_cmd, export_window_cmd, upload_window_cmd):
        option = _option(command, "feed_name")
        assert option.default == "executions"
        assert sorted(option.type.choices) == ["executions", "orders"]
    assert _option(export_window_cmd, "coins").multiple is True
    assert _option(export_window_cmd, "include_post_only").default is False
    assert _option(upload_window_cmd, "file_type").default is None


def test_window_commands_derive_out_dir_and_prefix_from_the_tenant() -> None:
    """--out-dir is optional on both window commands; --file-prefix defaults to the tenant's."""
    for command in (export_window_cmd, upload_window_cmd):
        option = _option(command, "out_dir")
        assert option.required is False and option.default is None
    assert _option(export_window_cmd, "file_prefix").default is None


def _capture_export(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``window.export_window`` with a double that records its arguments."""
    seen: dict[str, Any] = {}

    def fake(start: date, end_exclusive: date, out_dir: Path, **kwargs: Any) -> window.WindowResult:
        seen.update(start=start, end_exclusive=end_exclusive, out_dir=out_dir, **kwargs)
        return window.WindowResult()

    monkeypatch.setattr(window, "export_window", fake)
    return seen


def test_export_window_uses_the_tenant_folder_and_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --out-dir and --file-prefix, the tenant decides both (coverage check skipped)."""
    monkeypatch.chdir(tmp_path)
    seen = _capture_export(monkeypatch)
    result = CliRunner().invoke(cli, [
        "export-window", "--start", "2026-10-10", "--end", "2026-10-20",
        "--tenant", "hlstrategiestest", "--skip-coverage-check",
    ])
    assert result.exit_code == 0, result.output
    assert seen["out_dir"] == Path("output/HLSTRATEGIESTEST/executions_20261010_20261019")
    assert seen["file_prefix"] == "hlstrategiestest"
    assert (seen["start"], seen["end_exclusive"]) == (date(2026, 10, 10), date(2026, 10, 20))


def test_export_window_explicit_out_dir_and_prefix_win(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HLRESEARCH keeps sdny by default, and explicit flags override the tenant's defaults."""
    monkeypatch.chdir(tmp_path)
    seen = _capture_export(monkeypatch)
    result = CliRunner().invoke(cli, [
        "export-window", "--start", "2026-05-17", "--end", "2026-05-18",
        "--tenant", "HLRESEARCH", "--skip-coverage-check",
    ])
    assert result.exit_code == 0, result.output
    assert seen["out_dir"] == Path("output/HLRESEARCH/executions_20260517_20260517")
    assert seen["file_prefix"] == "sdny"
    result = CliRunner().invoke(cli, [
        "export-window", "--start", "2026-05-17", "--end", "2026-05-18",
        "--tenant", "HLRESEARCH", "--skip-coverage-check",
        "--out-dir", "elsewhere", "--file-prefix", "custom",
    ])
    assert result.exit_code == 0, result.output
    assert seen["out_dir"] == Path("elsewhere") and seen["file_prefix"] == "custom"


def test_export_window_needs_a_tenant_or_an_out_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With neither flag there is no folder to write to, and the command says so."""
    monkeypatch.chdir(tmp_path)
    _capture_export(monkeypatch)
    result = CliRunner().invoke(cli, [
        "export-window", "--start", "2026-10-10", "--end", "2026-10-11", "--skip-coverage-check",
    ])
    assert result.exit_code == 2
    assert "--out-dir is required" in result.output


def test_upload_window_refuses_a_missing_tenant_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The derived folder must exist; nothing is uploaded from a folder that was never exported."""
    monkeypatch.chdir(tmp_path)
    skill = tmp_path / "script.py"
    skill.write_text("")
    result = CliRunner().invoke(cli, [
        "upload-window", "--start", "2026-10-10", "--end", "2026-10-20",
        "--tenant", "HLSTRATEGIESTEST", "--dry-run", "--skill-script", str(skill),
    ])
    assert result.exit_code == 1
    assert "output/HLSTRATEGIESTEST/executions_20261010_20261019" in result.output
