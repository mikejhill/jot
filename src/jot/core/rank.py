"""Fractional rank keys: ordering independent of urgency or importance.

Keys are base-36 strings compared bytewise (SQLite's default collation), so a
jot can always be placed between two neighbours without renumbering others.
"""

from __future__ import annotations

from jot.exceptions import InvalidInputError

DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
ZERO = DIGITS[0]


class Rank:
    """Generate keys that sort strictly between two existing keys."""

    @classmethod
    def between(cls, low: str | None, high: str | None) -> str:
        """Return a key with ``low < key < high``; ``None`` means unbounded."""
        lower = low or ""
        for key in (lower, high or ""):
            if key.endswith(ZERO) or any(char not in DIGITS for char in key):
                raise InvalidInputError(f"Invalid rank key {key!r}")
        if high is not None and lower >= high:
            raise InvalidInputError(f"Rank {lower!r} is not below {high!r}")
        return cls._midpoint(lower, high)

    @classmethod
    def _midpoint(cls, low: str, high: str | None) -> str:
        """Midpoint of two digit strings (``high`` None = past the last key)."""
        if high is not None:
            shared = 0
            while (low[shared] if shared < len(low) else ZERO) == high[shared]:
                shared += 1
            if shared:
                return high[:shared] + cls._midpoint(low[shared:], high[shared:])
        digit_low = DIGITS.index(low[0]) if low else 0
        digit_high = DIGITS.index(high[0]) if high is not None else len(DIGITS)
        if digit_high - digit_low > 1:
            return DIGITS[(digit_low + digit_high + 1) // 2]
        if high is not None and len(high) > 1:
            return high[0]
        return DIGITS[digit_low] + cls._midpoint(low[1:], None)
