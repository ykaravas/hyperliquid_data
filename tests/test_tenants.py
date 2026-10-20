"""Unit tests for :mod:`hyperliquid_halo.tenants` (pure functions, no I/O)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from hyperliquid_halo import tenants
from hyperliquid_halo.feeds import EXECUTIONS, ORDERS


def test_registered_tenants_keep_their_own_prefix_and_ledger() -> None:
    """HLRESEARCH keeps production's sdny prefix; HLSTRATEGIESTEST has its own name."""
    assert tenants.HLRESEARCH.file_prefix == "sdny"
    assert tenants.HLSTRATEGIESTEST.file_prefix == "hlstrategiestest"
    assert tenants.HLRESEARCH.ledger_doc == Path("docs/tenants/HLRESEARCH/HALO_UPLOAD_LEDGER.md")
    assert tenants.HLSTRATEGIESTEST.output_root == Path("output/HLSTRATEGIESTEST")
    assert set(tenants.TENANTS) == {"HLRESEARCH", "HLSTRATEGIESTEST"}


def test_lookup_is_case_insensitive_and_falls_back_to_generic_defaults() -> None:
    """Any case finds a registered tenant; an unknown name gets its lower-cased prefix."""
    assert tenants.tenant_by_name("hlresearch") is tenants.HLRESEARCH
    assert tenants.tenant_by_name(" HLStrategiesTest ") is tenants.HLSTRATEGIESTEST
    other = tenants.tenant_by_name("new_tenant")
    assert other.name == "NEW_TENANT"
    assert other.file_prefix == "newtenant"
    assert other.ledger_doc == Path("docs/tenants/NEW_TENANT/HALO_UPLOAD_LEDGER.md")


def test_unsafe_names_are_refused() -> None:
    """Only ``[A-Z0-9_]`` names are accepted (they are spliced into SQL and file names)."""
    with pytest.raises(ValueError):
        tenants.tenant_by_name("HL'; DROP TABLE")
    with pytest.raises(ValueError):
        tenants.Tenant(name="OK", file_prefix="has_underscore")


def test_default_out_dir_separates_tenants_feeds_and_windows() -> None:
    """The folder names the tenant, the feed and the first and last transact dates."""
    window = (date(2026, 10, 10), date(2026, 10, 20))
    execs = tenants.default_out_dir(tenants.HLSTRATEGIESTEST, EXECUTIONS, *window)
    orders = tenants.default_out_dir(tenants.HLSTRATEGIESTEST, ORDERS, *window)
    research = tenants.default_out_dir(tenants.HLRESEARCH, EXECUTIONS, *window)
    assert execs == Path("output/HLSTRATEGIESTEST/executions_20261010_20261019")
    assert orders == Path("output/HLSTRATEGIESTEST/orders_20261010_20261019")
    assert research == Path("output/HLRESEARCH/executions_20261010_20261019")
    assert len({execs, orders, research}) == 3
    custom = tenants.default_out_dir(tenants.HLRESEARCH, EXECUTIONS, date(2026, 1, 12),
                                     date(2026, 1, 13), root=Path("/data"))
    assert custom == Path("/data/HLRESEARCH/executions_20260112_20260112")


def test_default_out_dir_rejects_an_empty_window() -> None:
    """A window must hold at least one day."""
    with pytest.raises(ValueError):
        tenants.default_out_dir(tenants.HLRESEARCH, EXECUTIONS, date(2026, 5, 5), date(2026, 5, 5))
