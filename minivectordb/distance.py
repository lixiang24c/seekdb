"""Fast squared distances with exact ordering when rounding could affect rank.

Vectors contain stored float32 values. Integer arithmetic is lazy, bounded by
one retained vector per candidate, and shared with every Python merge path.
The uncertainty band only decides when to recompute; it is not a tie tolerance.
"""
import math
import sys


class DistanceOrder:
    def __init__(self, query):
        self.query = query
        self.roundoff = 64 * sys.float_info.epsilon * (len(query) + 1)
        self.scale = None
        self.integers = None

    def exact(self, vector):
        if self.integers is None:
            ratios = [float(value).as_integer_ratio() for value in self.query]
            # Every float32 is an integer multiple of 2^-149. Also support
            # binary64 query values used by the low-level math interface.
            self.scale = max(149, max((denominator.bit_length() - 1 for _, denominator in ratios), default=0))
            self.integers = [numerator << (self.scale - denominator.bit_length() + 1)
                             for numerator, denominator in ratios]
        result = 0
        for value, query in zip(vector, self.integers):
            numerator, denominator = float(value).as_integer_ratio()
            delta = (numerator << (self.scale - denominator.bit_length() + 1)) - query
            result += delta * delta
        return result

    def key(self, vector, approximate=None):
        if approximate is None:
            approximate = math.fsum((float(a) - float(b)) ** 2 for a, b in zip(vector, self.query))
        return SquaredDistance(approximate, self, vector)


class SquaredDistance(float):
    """A float-compatible output value with a lazy exact comparison key."""
    def __new__(cls, approximate, order, vector):
        result = super().__new__(cls, approximate)
        result.order = order
        result.vector = vector
        result.exact_value = None
        return result

    def _exact(self):
        if self.exact_value is None:
            self.exact_value = self.order.exact(self.vector)
        return self.exact_value

    def _compare(self, other):
        if not isinstance(other, SquaredDistance):
            return NotImplemented
        left, right = float(self), float(other)
        gap = left - right
        margin = max(self.order.roundoff, other.order.roundoff) * max(abs(left), abs(right))
        if math.isfinite(gap) and abs(gap) > margin:
            return -1 if gap < 0 else 1
        left, right = self._exact(), other._exact()
        scale = max(self.order.scale, other.order.scale)
        left <<= 2 * (scale - self.order.scale)
        right <<= 2 * (scale - other.order.scale)
        return (left > right) - (left < right)

    def __eq__(self, other):
        comparison = self._compare(other)
        return float.__eq__(self, other) if comparison is NotImplemented else comparison == 0

    def __ne__(self, other):
        comparison = self._compare(other)
        return float.__ne__(self, other) if comparison is NotImplemented else comparison != 0

    def __lt__(self, other):
        comparison = self._compare(other)
        return float.__lt__(self, other) if comparison is NotImplemented else comparison < 0

    def __le__(self, other):
        comparison = self._compare(other)
        return float.__le__(self, other) if comparison is NotImplemented else comparison <= 0

    def __gt__(self, other):
        comparison = self._compare(other)
        return float.__gt__(self, other) if comparison is NotImplemented else comparison > 0

    def __ge__(self, other):
        comparison = self._compare(other)
        return float.__ge__(self, other) if comparison is NotImplemented else comparison >= 0

    __hash__ = None
