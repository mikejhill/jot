"""Fractional rank keys."""

from __future__ import annotations

import random

import pytest

from jot.core.rank import Rank
from jot.exceptions import InvalidInputError


class TestRank:
    """Keys always sort strictly between their bounds."""

    @pytest.mark.parametrize(
        ("low", "high"),
        [
            (None, None),
            (None, "1"),
            (None, "i"),
            ("i", None),
            ("z", None),
            ("a", "b"),
            ("a", "ab"),
            ("az", "b"),
            ("0i", "1"),
            ("y", "z"),
            ("zz", None),
        ],
    )
    def test_between(self, low: str | None, high: str | None) -> None:
        """The key is above low and below high."""
        key = Rank.between(low, high)
        assert not key.endswith("0")
        if low is not None:
            assert low < key
        if high is not None:
            assert key < high

    @pytest.mark.parametrize(
        ("low", "high"), [("b", "a"), ("a", "a"), ("a0", None), (None, "A")]
    )
    def test_invalid(self, low: str | None, high: str | None) -> None:
        """Out-of-order, trailing-zero, and foreign keys are rejected."""
        with pytest.raises(InvalidInputError):
            Rank.between(low, high)

    def test_random_insertions_stay_ordered(self) -> None:
        """Hundreds of random insertions keep a strict total order."""
        rng = random.Random(7)  # noqa: S311 - deterministic test data
        keys = [Rank.between(None, None)]
        for _ in range(500):
            index = rng.randint(0, len(keys))
            low = keys[index - 1] if index > 0 else None
            high = keys[index] if index < len(keys) else None
            keys.insert(index, Rank.between(low, high))
        assert keys == sorted(keys)
        assert len(set(keys)) == len(keys)
