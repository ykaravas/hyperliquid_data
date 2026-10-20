"""Unit tests for :mod:`hyperliquid_halo.filters`."""

from __future__ import annotations

import pytest

from hyperliquid_halo.filters import coin_filter


def test_no_filter_for_none_or_empty() -> None:
    assert coin_filter(None) == (None, {})
    assert coin_filter(()) == (None, {})


def test_single_market_keeps_the_equality_form() -> None:
    assert coin_filter("BTC") == ("AND COIN = %(coin)s", {"coin": "BTC"})
    assert coin_filter(["xyz:CL"]) == ("AND COIN = %(coin)s", {"coin": "xyz:CL"})


def test_several_markets_are_bound_not_spliced() -> None:
    predicate, binds = coin_filter(("xyz:CL", "x' OR 1=1 --"))
    assert predicate == "AND COIN IN (%(coin_0)s, %(coin_1)s)"
    assert binds == {"coin_0": "xyz:CL", "coin_1": "x' OR 1=1 --"}
    assert "OR 1=1" not in predicate


def test_empty_market_name_is_refused() -> None:
    with pytest.raises(ValueError, match="empty market name"):
        coin_filter(("xyz:CL", ""))
