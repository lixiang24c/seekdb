"""SQL execution with bounded scans, flat vector indexes and external sorting."""
import copy
from collections import OrderedDict
import heapq
import itertools
import json
import math
import operator
import struct
from dataclasses import dataclass

from .native import Screen
from .sql import DBError, Parser, convert, fail, vector
from .storage import Codec, INDEX_MAGIC, Storage, read_rows, write_rows

OPS = {'=': operator.eq, '<': operator.lt, '<=': operator.le, '>': operator.gt, '>=': operator.ge}
SORT_BYTES = 1 << 20
HEAP_BYTES = 2 << 20
MERGE_FANIN = 16


class Desc:
    __slots__ = ('value',)
    def __init__(self, value): self.value = value
    def __lt__(self, other): return self.value > other.value
    def __eq__(self, other): return isinstance(other, Desc) and self.value == other.value


class Worst:
    __slots__ = ('key', 'row', 'cost')
    def __init__(self, key, row, cost): self.key, self.row, self.cost = key, row, cost
    def __lt__(self, other): return self.key > other.key


@dataclass
class QueryResult:
    columns: list
    path: object = None
    encoded_rows: object = None


class SortCodec:
    """Temporary sort runs contain only columns used by this query."""
    def __init__(self, cols, fields):
        self.fields = sorted(fields)
        self.width = len(cols)
        self.codec = Codec([cols[i] for i in self.fields])

    def encode(self, row):
        return self.codec.encode([row[i] for i in self.fields])

    def decode(self, raw):
        row = [None] * self.width
        for i, value in zip(self.fields, self.codec.decode(raw)): row[i] = value
        return row


def row_cost(row):
    # Conservative Python object estimate, not only encoded byte length.
    return 256 + sum(64 + (40 * len(v) if isinstance(v, list) else 4 * len(v) if isinstance(v, str) else 32) for v in row)


def squared(a, b):
    return math.fsum((x - y) * (x - y) for x, y in zip(a, b))


class Database:
    def __init__(self, directory):
        self.store = Storage(directory)
        self.filter_cache = OrderedDict()
        self.filter_cache_bytes = 0

    def column(self, table, name):
        for i, c in enumerate(table['cols']):
            if c['name'].casefold() == name.casefold():
                return i
        fail('unknown column ' + name, 'unknown_column')

    def conditions(self, table, conditions, columns=None):
        bound = []
        for name, op, value in conditions:
            i = self.column(table, name)
            if columns is not None: columns.add(i)
            col = table['cols'][i]
            if col['type'] == 'VECTOR':
                fail('VECTOR comparison is unsupported')
            # Comparison strings may be longer than the declared storage width.
            if col['type'] == 'VARCHAR':
                if not isinstance(value, str):
                    fail('invalid VARCHAR comparison', 'invalid_value')
                value.encode('utf-8')
            else:
                value = convert(col, value)
            bound.append((i, OPS[op], value))
        return lambda row: all(op(row[i], value) for i, op, value in bound)

    def primary_equal(self, table, conditions):
        for name, op, value in conditions:
            if op == '=' and self.column(table, name) == table['pk']:
                return convert(table['cols'][table['pk']], value)
        return None

    def primary_bounds(self, table, conditions):
        lower, upper = -(1 << 63), (1 << 63) - 1
        found = False
        for name, op, value in conditions:
            if self.column(table, name) != table['pk']: continue
            found = True
            value = convert(table['cols'][table['pk']], value)
            if op in ('=', '>=', '>'): lower = max(lower, value + (op == '>'))
            if op in ('=', '<=', '<'): upper = min(upper, value - (op == '<'))
        return (lower, upper) if found else None

    def expression(self, table, expr):
        i = self.column(table, expr[1])
        if expr[0] == 'column':
            return ('column', i)
        if table['cols'][i]['type'] != 'VECTOR':
            fail('L2_DISTANCE requires VECTOR', 'invalid_vector')
        return ('distance', i, vector(expr[2], table['cols'][i]['n']))

    def execute(self, sql, mode='exact'):
        if mode not in ('exact', 'ann'):
            fail('invalid search mode', 'invalid_request')
        st = Parser(sql).parse()
        op = st['op']
        if op == 'begin':
            if self.store.in_txn:
                fail('nested transaction', 'invalid_transaction')
            self.store.begin(); return {}
        if op == 'commit':
            if self.store.in_txn:
                self.store.commit()
            return {}
        if op == 'rollback':
            self.store.rollback(); return {}
        name = st['table'].casefold()
        if op == 'create':
            if name in self.store.tables:
                fail('table already exists', 'already_exists')
            cols = st['cols']
            if len({c['name'].casefold() for c in cols}) != len(cols):
                fail('duplicate column')
            pks = [i for i, c in enumerate(cols) if c['pk']]
            if len(pks) != 1 or cols[pks[0]]['type'] != 'BIGINT':
                fail('exactly one BIGINT primary key required')
            if sum(c['type'] == 'VECTOR' for c in cols) > 1:
                fail('at most one VECTOR column allowed')
            try:
                Codec(cols)
            except (OverflowError, struct.error):
                fail('column size out of range', 'invalid_value')
            table = {'cols': cols, 'pk': pks[0], 'indexes': [], 'count': 0}
            table['segments'] = []
            self.store.install(name, table)
            return {}
        table = self.store.tables.get(name)
        if table is None:
            fail('unknown table ' + name, 'unknown_table')
        if op == 'index':
            i = self.column(table, st['col'])
            if table['cols'][i]['type'] != 'VECTOR':
                fail('index requires VECTOR')
            if any(ix.casefold() == st['name'].casefold() for ix in table['indexes']):
                fail('index already exists', 'already_exists')
            new = copy.deepcopy(table)
            new['indexes'].append(st['name'])
            new = self.store.add_index(new)
            self.store.install(name, new)
            return {}
        if op == 'select':
            return self.select(table, st)
        if op == 'insert':
            order = list(range(len(table['cols']))) if st['cols'] is None else [self.column(table, c) for c in st['cols']]
            if len(set(order)) != len(table['cols']) or len(order) != len(table['cols']):
                fail('insert requires every column exactly once')
            pk, incoming = table['pk'], []
            for values in st['rows']:
                if len(values) != len(order):
                    fail('insert arity mismatch')
                row = [None] * len(order)
                for i, v in zip(order, values):
                    row[i] = convert(table['cols'][i], v)
                incoming.append(row)
            incoming.sort(key=lambda row: row[pk])
            if any(a[pk] == b[pk] for a, b in zip(incoming, incoming[1:])):
                fail('duplicate primary key', 'duplicate_key')
            new = self.store.insert(table, incoming)
            self.store.install(name, new)
            return {'affected_rows': len(incoming)}
        filter_columns = set()
        match = self.conditions(table, st['where'], filter_columns)
        sets = []
        if op == 'update':
            for col, value in st['sets']:
                i = self.column(table, col)
                if i == table['pk']:
                    fail('primary key update unsupported')
                if any(j == i for j, _ in sets):
                    fail('duplicate assignment')
                sets.append((i, convert(table['cols'][i], value)))
        if op == 'delete' and not st['where']:
            count = table['count']
            if count:
                new = copy.deepcopy(table)
                new.update(segments=[], count=0)
                new.pop('file', None); new.pop('index_file', None)
                self.store.install(name, new)
            return {'affected_rows': count}
        ident = self.primary_equal(table, st['where'])
        if ident is not None:
            part, raw = self.store.lookup(table, ident)
            if raw is None: return {'affected_rows': 0}
            row = Codec(table['cols']).decode(raw)
            if not match(row): return {'affected_rows': 0}
            if op == 'update' and all(row[i] == value for i, value in sets):
                return {'affected_rows': 1}
            for i, value in sets: row[i] = value
            new = self.store.replace_point(table, part, ident, None if op == 'delete' else row)
            self.store.install(name, new)
            return {'affected_rows': 1}
        new, count = self.store.mutate(table, match, filter_columns, sets, op == 'delete')
        if count and new != table: self.store.install(name, new)
        return {'affected_rows': count}

    def external_sort(self, table, source, key, columns):
        paths, codec = [], SortCodec(table['cols'], columns)
        def chunk(rows):
            rows.sort(key=key)
            path = self.store.fresh('sort')
            paths.append(path)
            write_rows(path, codec, rows)
            return path
        try:
            runs, buf, cost = [], [], 0
            for row in source:
                buf.append(row); cost += row_cost(row)
                if cost >= SORT_BYTES:
                    runs.append(chunk(buf)); buf, cost = [], 0
            if not runs:
                yield from sorted(buf, key=key)
                return
            if buf:
                runs.append(chunk(buf))
            while len(runs) > MERGE_FANIN:
                next_runs = []
                for start in range(0, len(runs), MERGE_FANIN):
                    group = runs[start:start + MERGE_FANIN]
                    path = self.store.fresh('sort'); paths.append(path)
                    readers = [read_rows(p, codec) for p in group]
                    try:
                        write_rows(path, codec, heapq.merge(*readers, key=key))
                    finally:
                        for reader in readers: reader.close()
                    for p in group: p.unlink()
                    next_runs.append(path)
                runs = next_runs
            readers = [read_rows(p, codec) for p in runs]
            try:
                yield from heapq.merge(*readers, key=key)
            finally:
                for reader in readers: reader.close()
        finally:
            for p in paths: p.unlink(missing_ok=True)

    def topk(self, source, key, k):
        heap, cost = [], 0
        for row in source:
            item = Worst(key(row), row, row_cost(row))
            if len(heap) < k:
                heapq.heappush(heap, item); cost += item.cost
            elif item.key < heap[0].key:
                removed = heapq.heapreplace(heap, item); cost += item.cost - removed.cost
            if cost > HEAP_BYTES:
                return None  # Caller restarts scan using bounded external sort.
        return [item.row for item in sorted(heap, key=lambda x: x.key)]

    def filter_key(self, table, conditions):
        key = (tuple((p['file'], tuple(p.get('excluded', ()))) for p in self.store.segments(table)), tuple(conditions))
        return key if len(repr(key)) * 4 + 256 <= 8192 else None

    def remember_filter(self, key, ids):
        cost = len(repr(key)) * 4 + 256 + (len(ids) * 128 if ids is not None else 128)
        while self.filter_cache and self.filter_cache_bytes + cost > (512 << 10):
            _, (_, old_cost) = self.filter_cache.popitem(last=False)
            self.filter_cache_bytes -= old_cost
        self.filter_cache[key] = (ids, cost)
        self.filter_cache_bytes += cost

    def flat_topk(self, table, expr, k, allowed=None):
        vc, query = expr[1], expr[2]
        fmt = struct.Struct('<q' + str(len(query)) + 'f')
        heap, radius = [], math.inf
        if allowed is not None and not allowed: return {}
        key_format = struct.Struct('<q')
        screen = Screen(query)
        for path, excluded in self.store.index_segments(table):
            with open(path, 'rb') as f:
                if f.read(8) != INDEX_MAGIC: raise OSError('invalid index header')
                block_bytes = fmt.size * max(1, 65536 // fmt.size)
                while data := f.read(block_bytes):
                    if len(data) % fmt.size: raise OSError('incomplete index record')
                    mask = None
                    if screen.query is not None and (allowed is not None or excluded):
                        mask = bytes(int((allowed is None or ident in allowed) and ident not in excluded)
                                     for ident in (key_format.unpack_from(data, pos)[0] for pos in range(0, len(data), fmt.size)))
                    positions = screen.select(data, -heap[0][0] if len(heap) == k else math.inf, mask, k=k if allowed is None else None)
                    values_iter = ((fmt.unpack_from(data, pos * fmt.size) for pos in positions) if positions is not None else
                                   fmt.iter_unpack(data) if allowed is None else
                                   (fmt.unpack_from(data, pos) for pos in range(0, len(data), fmt.size)
                                    if key_format.unpack_from(data, pos)[0] in allowed))
                    for values in values_iter:
                        if values[0] in excluded: continue
                        # A conservative rejection bound only; accepted candidates are
                        # ranked with the original fsum squared distance, including ties.
                        if math.dist(query, values[1:]) > radius: continue
                        key = (squared(query, values[1:]), values[0])
                        item = (-key[0], -key[1], values[0])
                        if len(heap) < k:
                            heapq.heappush(heap, item)
                        elif item > heap[0]:
                            heapq.heapreplace(heap, item)
                        if len(heap) == k:
                            radius = math.sqrt(-heap[0][0]) * (1.0 + 1e-12) + 1e-300
        return {item[2]: -item[0] for item in heap}

    def vector_topk(self, table, expr, k, columns, match, filter_columns, conditions):
        """Filter scalars first, retain binary candidates, decode only the winners."""
        codec = Codec(table['cols'])
        vc, query = expr[1], expr[2]
        selected, cost = [], 0
        cache_key = self.filter_key(table, conditions) if filter_columns and table.get('indexes') else None
        entry = self.filter_cache.get(cache_key) if cache_key is not None else None
        if entry is not None: self.filter_cache.move_to_end(cache_key)
        allowed = entry[0] if entry is not None else None
        collect_ids = set() if cache_key is not None and entry is None else None
        collect_budget = (512 << 10) - (len(repr(cache_key)) * 4 + 256) if cache_key is not None else 0
        scores = self.flat_topk(table, expr, k, allowed) if table.get('indexes') and (not filter_columns or allowed is not None) else None
        heap, radius = [], math.inf
        records = (self.store.records(table, ordered=False) if scores is None else
                   ((ident, self.store.lookup(table, ident)[1]) for ident in scores))
        try:
            for ident, raw in records:
                if scores is not None:
                    if ident not in scores: continue
                    selected.append((scores[ident], ident, raw))
                    cost += len(raw) * 10 + 256 + len(table['cols']) * 64
                    if cost > HEAP_BYTES: return None
                    if len(selected) == len(scores): break
                    continue
                if filter_columns and not match(codec.decode_fields(raw, filter_columns)):
                    continue
                if collect_ids is not None:
                    collect_ids.add(ident)
                    if len(collect_ids) * 128 > collect_budget: collect_ids = None
                offset, _ = codec.field(raw, vc)
                vec = codec.formats[vc].unpack_from(raw, offset)
                if math.dist(query, vec) > radius: continue
                distance = squared(query, vec)
                candidate = (-distance, -ident, raw)
                if len(heap) < k:
                    heapq.heappush(heap, candidate)
                    cost += len(raw) * 10 + 256 + len(table['cols']) * 64
                elif candidate[:2] > heap[0][:2]:
                    removed = heapq.heapreplace(heap, candidate)
                    cost += (len(raw) - len(removed[2])) * 10
                if cost > HEAP_BYTES: return None
                if len(heap) == k:
                    radius = math.sqrt(-heap[0][0]) * (1.0 + 1e-12) + 1e-300
        finally:
            records.close()
        if cache_key is not None and entry is None:
            self.remember_filter(cache_key, collect_ids)
        if scores is None:
            selected = [(-neg_distance, -neg_ident, raw) for neg_distance, neg_ident, raw in heap]
        selected.sort(key=lambda item: (item[0], item[1]))
        return [codec.decode_fields(raw, columns) for _, _, raw in selected]

    def select(self, table, st):
        filter_columns = set()
        match = self.conditions(table, st['where'], filter_columns)
        outs, names = [], []
        if st['projection'] is None:
            outs = [('column', i) for i in range(len(table['cols']))]
            names = [c['name'] for c in table['cols']]
        else:
            for expr, alias in st['projection']:
                bound = self.expression(table, expr)
                outs.append(bound)
                names.append(alias or ('L2_DISTANCE' if expr[0] == 'distance' else table['cols'][bound[1]]['name']))
        keys = []
        for expr, desc in st['order']:
            aliases = [i for i, name in enumerate(names) if name.casefold() == expr[1].casefold()] if expr[0] == 'column' else []
            bound = outs[aliases[0]] if aliases else self.expression(table, expr)
            if bound[0] == 'column' and table['cols'][bound[1]]['type'] == 'VECTOR':
                fail('cannot ORDER BY VECTOR')
            keys.append((bound, desc))
        def value(expr, row, square=False):
            if expr[0] == 'column': return row[expr[1]]
            d = squared(expr[2], row[expr[1]])
            return d if square else math.sqrt(d)
        def key(row):
            return tuple(Desc(value(e, row, True)) if desc else value(e, row, True) for e, desc in keys) + (row[table['pk']],)
        needed = {table['pk']} | filter_columns | {e[1] for e in outs} | {e[1] for e, _ in keys}
        ident = self.primary_equal(table, st['where'])
        bounds = self.primary_bounds(table, st['where'])
        reverse_pk = keys == [(('column', table['pk']), True)] and self.store.has_offsets(table)
        if reverse_pk and bounds is None: bounds = (-(1 << 63), (1 << 63) - 1)
        def source(full=False):
            if ident is not None:
                _, raw = self.store.lookup(table, ident)
                if raw is not None:
                    row = Codec(table['cols']).decode(raw)
                    if match(row): yield row
                return
            yield from self.store.rows(table, None if full else needed,
                                       match if filter_columns else None, filter_columns, bounds=bounds, reverse=reverse_pk,
                                       ordered=not keys or keys == [(('column', table['pk']), False)])
        result = self.store.fresh('result')
        rows, limit = None, st['limit']
        try:
            if limit == 0:
                rows = iter(())
            elif not keys or keys == [(('column', table['pk']), False)] or reverse_pk:
                rows = source()
            else:
                if limit is not None and limit <= 1024:
                    if ident is None and bounds is None and len(keys) == 1 and keys[0][0][0] == 'distance' and not keys[0][1]:
                        rows = self.vector_topk(table, keys[0][0], limit, needed, match, filter_columns, st['where'])
                    else:
                        selected = source()
                        try:
                            rows = self.topk(selected, key, limit)
                        finally:
                            selected.close()
                if rows is None:
                    rows = self.external_sort(table, source(), key, needed)
            encoded, size, stream = [], 0, None
            encode = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode
            try:
                for row in itertools.islice(rows, limit):
                    line = encode([value(expr, row) for expr in outs])
                    size += len(line) * 4 + 64
                    if stream is None and size > 65536:
                        stream = open(result, 'x', encoding='utf-8')
                        for previous in encoded: stream.write(previous + '\n')
                        encoded = []
                    if stream is None: encoded.append(line)
                    else: stream.write(line + '\n')
            finally:
                if stream is not None: stream.close()
            return QueryResult(names, result if stream is not None else None, encoded)
        except BaseException:
            result.unlink(missing_ok=True)
            raise
        finally:
            if hasattr(rows, 'close'): rows.close()

    def close(self):
        self.store.close()
