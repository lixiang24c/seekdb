"""Bounded scalar value groups learned during an existing predicate scan."""
from array import array
from collections import OrderedDict

GROUP_BYTES = 256 << 10
GROUP_IDS = 4096


class GroupBuilder:
    def __init__(self, base, budget):
        self.groups = {}
        self.cost = base
        self.budget = budget

    def add(self, ident, value):
        if self.groups is None: return
        ids = self.groups.get(value)
        cost = 16 + (256 + (len(value) * 4 if isinstance(value, str) else 32) if ids is None else 0)
        if self.cost + cost > self.budget:
            self.groups = None
            return
        if ids is None:
            ids = self.groups[value] = array('q')
        ids.append(ident)
        self.cost += cost


class ValueGroups:
    def __init__(self):
        self.entries = OrderedDict()
        self.bytes = 0

    def matching(self, key, column, width, match):
        entry = self.entries.get(key)
        if entry is None: return None
        self.entries.move_to_end(key)
        groups, _ = entry
        if groups is None: return None
        row, selected, count = [None] * width, [], 0
        for value, ids in groups.items():
            row[column] = value
            if match(row):
                count += len(ids)
                if count > GROUP_IDS: return None
                selected.append(ids)
        return {ident for ids in selected for ident in ids}

    def begin(self, key, count):
        if key in self.entries: return None
        base = len(repr(key)) * 4 + 256
        if base + count * 16 > GROUP_BYTES:
            self.remember(key, None)
            return None
        return GroupBuilder(base, GROUP_BYTES)

    def remember(self, key, builder):
        groups = builder.groups if builder is not None else None
        cost = builder.cost if groups is not None else len(repr(key)) * 4 + 384
        if cost > GROUP_BYTES: return
        previous = self.entries.pop(key, None)
        if previous is not None: self.bytes -= previous[1]
        while self.entries and self.bytes + cost > GROUP_BYTES:
            _, (_, old_cost) = self.entries.popitem(last=False)
            self.bytes -= old_cost
        self.entries[key] = (groups, cost)
        self.bytes += cost
