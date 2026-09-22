"""Unit tests for the generated :mod:`hyperliquid_halo.symbol_type_map` and
its generator :mod:`hyperliquid_halo.sync_symbol_type_map`.

No Snowflake connection and no access to the production repo are needed:
the generator is exercised on an inline SQL snippet.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyperliquid_halo import symbol_type_map
from hyperliquid_halo.sync_symbol_type_map import (
    HALO_SYMBOL_TYPES,
    SymbolTypeMapError,
    parse_map_sql,
    render_module,
    sync,
)

_SAMPLE_SQL = """-- header comment with a ('fake', 'row') that is not a VALUES line
CREATE OR REPLACE VIEW hyperliquid_v_symbol_type_map AS
SELECT coin, symbol_type FROM VALUES
    ('BTC', 'Crypto'),
    ('DOGE', 'Memecoin'),
    ('xyz:TSLA', 'Equity'),
    ('hyna:1000PEPE', 'Memecoin')
AS t(coin, symbol_type);
"""


def test_generated_map_is_non_empty_and_unique() -> None:
    coins = [c for c, _ in symbol_type_map.SYMBOL_TYPE_MAP]
    assert len(coins) > 100
    assert len(coins) == len(set(coins)), "duplicate coins in the generated map"


def test_generated_map_values_are_halo_symbol_types() -> None:
    bad = {t for _, t in symbol_type_map.SYMBOL_TYPE_MAP if t not in HALO_SYMBOL_TYPES}
    assert not bad, f"non-HALO SymbolType values in generated map: {bad}"


def test_generated_map_is_sorted_and_covers_main_and_hip3_dexes() -> None:
    coins = [c for c, _ in symbol_type_map.SYMBOL_TYPE_MAP]
    assert coins == sorted(coins), "map must stay sorted by coin for stable diffs"
    assert "BTC" in coins
    assert any(":" in c for c in coins), "expected dex-prefixed HIP-3 markets"


def test_render_values_rows_is_sql_safe() -> None:
    rendered = symbol_type_map.render_values_rows()
    assert rendered.startswith("('")
    assert rendered.count("('") == len(symbol_type_map.SYMBOL_TYPE_MAP)
    # No pyformat hazards or unescaped quotes can appear inside the literals.
    for coin, symbol_type in symbol_type_map.SYMBOL_TYPE_MAP:
        assert "'" not in coin and "%" not in coin
        assert f"('{coin}', '{symbol_type}')" in rendered


def test_parse_map_sql_reads_only_values_rows() -> None:
    assert parse_map_sql(_SAMPLE_SQL) == [
        ("BTC", "Crypto"),
        ("DOGE", "Memecoin"),
        ("xyz:TSLA", "Equity"),
        ("hyna:1000PEPE", "Memecoin"),
    ]


def test_parse_map_sql_rejects_empty_duplicate_and_bad_values() -> None:
    with pytest.raises(SymbolTypeMapError, match="no .* rows"):
        parse_map_sql("SELECT 1;")
    with pytest.raises(SymbolTypeMapError, match="duplicate"):
        parse_map_sql("    ('BTC', 'Crypto'),\n    ('BTC', 'Memecoin')\n")
    with pytest.raises(SymbolTypeMapError, match="not a HALO SymbolType"):
        parse_map_sql("    ('BTC', 'Cryptoo')\n")


def test_sync_writes_importable_module(tmp_path: Path) -> None:
    source = tmp_path / "R__06b_symbol_type_map.sql"
    source.write_text(_SAMPLE_SQL, encoding="utf-8")
    output = tmp_path / "symbol_type_map.py"

    assert sync(source, output) == 4

    namespace: dict[str, object] = {}
    exec(compile(output.read_text(encoding="utf-8"), str(output), "exec"), namespace)
    generated = namespace["SYMBOL_TYPE_MAP"]
    assert generated == (
        ("BTC", "Crypto"),
        ("DOGE", "Memecoin"),
        ("xyz:TSLA", "Equity"),
        ("hyna:1000PEPE", "Memecoin"),
    )
    render = namespace["render_values_rows"]
    assert callable(render)
    assert "('xyz:TSLA', 'Equity')" in render()  # type: ignore[operator]
    assert "R__06b_symbol_type_map.sql" in render_module(list(generated), source.name)  # type: ignore[arg-type]


def test_sync_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        sync(tmp_path / "missing.sql", tmp_path / "out.py")
