"""Property-based tests for the transforms whose failure would corrupt every metric."""

from __future__ import annotations

import contextlib
import hashlib
import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vizanix_atlas.analytics.robust import cap_weights, weighted_median
from vizanix_atlas.core.errors import MqlSyntaxError
from vizanix_atlas.core.identifiers import evm_asset_id, shard_for
from vizanix_atlas.mql.parser import parse_query
from vizanix_atlas.storage.writer import sha256_file

finite = st.floats(min_value=1e-6, max_value=1e9, allow_nan=False, allow_infinity=False)
weights = st.floats(min_value=1e-6, max_value=1e6, allow_nan=False, allow_infinity=False)
pairs = st.lists(st.tuples(finite, weights), min_size=1, max_size=40)


@given(pairs)
def test_weighted_median_lies_within_the_data(items: list[tuple[float, float]]) -> None:
    values, ws = zip(*items, strict=True)
    result = weighted_median(values, ws)
    assert result is not None
    assert min(values) <= result <= max(values)


@given(pairs, st.randoms(use_true_random=False))
def test_weighted_median_ignores_input_order(items: list[tuple[float, float]], rnd) -> None:
    shuffled = items[:]
    rnd.shuffle(shuffled)
    a = weighted_median(*zip(*items, strict=True))
    b = weighted_median(*zip(*shuffled, strict=True))
    assert a == b


@given(pairs, st.floats(min_value=0.5, max_value=1000.0))
def test_weighted_median_is_scale_invariant_in_weights(
    items: list[tuple[float, float]], scale: float
) -> None:
    values, ws = zip(*items, strict=True)
    assert weighted_median(values, ws) == pytest.approx(
        weighted_median(values, [w * scale for w in ws]), rel=1e-9
    )


@given(st.lists(weights, min_size=1, max_size=30), st.floats(min_value=0.05, max_value=1.0))
def test_capped_weights_sum_to_one_and_respect_the_cap(ws: list[float], cap: float) -> None:
    capped = cap_weights(ws, maximum_share=cap)
    assert math.isclose(sum(capped), 1.0, rel_tol=1e-9)
    # When the cap is unsatisfiable (cap * n <= 1) equal weights are the documented answer.
    limit = max(cap, 1.0 / len(ws))
    assert all(w <= limit + 1e-9 for w in capped)


@given(st.text(min_size=1, max_size=60), st.integers(min_value=1, max_value=512))
def test_shard_assignment_is_stable_and_in_range(asset_id: str, shard_count: int) -> None:
    index = shard_for(asset_id, shard_count)
    assert 0 <= index < shard_count
    expected = int.from_bytes(hashlib.sha256(asset_id.encode()).digest()[:8], "big") % shard_count
    assert index == expected


@given(
    st.integers(min_value=1, max_value=10**6),
    st.text(alphabet="0123456789abcdefABCDEF", min_size=40, max_size=40),
)
def test_evm_identifiers_are_case_insensitive(chain_id: int, address: str) -> None:
    assert evm_asset_id(chain_id, "0x" + address.lower()) == evm_asset_id(
        chain_id, "0x" + address.upper().replace("0X", "0x")
    )


@given(st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False))
def test_mql_numeric_literals_round_trip_including_negatives(value: float) -> None:
    text = f"{value:.6f}"
    query = parse_query(f"SELECT asset FROM market WHERE price_change_24h < {text}")
    parsed = query.where.right.value  # type: ignore[union-attr]
    assert parsed == pytest.approx(float(text))


@given(st.text(max_size=80))
@settings(max_examples=200)
def test_mql_never_raises_anything_but_a_typed_error(text: str) -> None:
    with contextlib.suppress(MqlSyntaxError):
        parse_query(text)


@given(st.binary(max_size=4096))
def test_file_checksum_matches_hashlib(
    tmp_path_factory: pytest.TempPathFactory, data: bytes
) -> None:
    path = tmp_path_factory.mktemp("c") / "f.bin"
    path.write_bytes(data)
    assert sha256_file(path) == hashlib.sha256(data).hexdigest()
