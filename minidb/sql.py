"""Parser for the published SQL subset; never executes a partial parse."""
import json
import math
import re
import struct
from collections import namedtuple


class DBError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def fail(message, code='unsupported_sql'):
    raise DBError(code, message)


# Immutable tokens without per-instance dictionaries or dataclasses' import graph.
Token = namedtuple('Token', ('kind', 'value'))


NUMBER = re.compile(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?')
VECTOR_BODY = re.compile(NUMBER.pattern + r'(?:\s*,\s*' + NUMBER.pattern + r')*')
IDENT = re.compile(r'[A-Za-z_][A-Za-z_0-9]*')


def _reject_constant(value):
    raise ValueError(value)


_VECTOR_DECODER = json.JSONDecoder(parse_constant=_reject_constant)
_TEXT_VECTOR_DECODER = json.JSONDecoder(parse_int=float, parse_constant=_reject_constant)


def tokenize(source, _vectors=False):
    tokens, i = [], 0
    while i < len(source):
        c = source[i]
        if c.isspace():
            i += 1
        elif source.startswith('--', i):
            end = source.find('\n', i)
            i = len(source) if end < 0 else end + 1
        elif source.startswith('/*', i):
            end = source.find('*/', i + 2)
            if end < 0:
                fail('unterminated comment')
            i = end + 2
        elif c in "'`":
            quote, buf = c, []
            start = i + 1
            while True:
                end = source.find(quote, start)
                if end < 0: fail('unterminated quoted value')
                buf.append(source[start:end])
                i = end + 1
                if i < len(source) and source[i] == quote:
                    buf.append(quote)
                    start = i + 1
                    continue
                break
            tokens.append(Token('str' if quote == "'" else 'id', ''.join(buf)))
        elif _vectors and c == '[':
            try:
                values, end = _VECTOR_DECODER.raw_decode(source, i)
            except (ValueError, RecursionError):
                values = None
            if values is not None and all(
                    type(v) is int or (type(v) is float and math.isfinite(v))
                    for v in values):
                tokens.append(Token('vec', values))
                i = end
            else:
                tokens.append(Token('sym', '['))
                i += 1
        elif (m := NUMBER.match(source, i)) is not None:
            tokens.append(Token('num', m.group()))
            i = m.end()
        elif (m := IDENT.match(source, i)) is not None:
            tokens.append(Token('id', m.group()))
            i = m.end()
        elif source[i:i + 2] in ('<=', '>='):
            tokens.append(Token('sym', source[i:i + 2]))
            i += 2
        elif c in '(),[]=<>*;':
            tokens.append(Token('sym', c))
            i += 1
        else:
            fail('unexpected SQL character')
    return tokens


def vector(value, dim):
    if isinstance(value, str):
        text = value.strip()
        if not text.startswith('[') or not text.endswith(']'):
            fail('invalid vector literal', 'invalid_vector')
        middle = text[1:-1].strip()
        values = None
        if len(text) <= 65536:
            try:
                decoded = _TEXT_VECTOR_DECODER.decode(text)
            except (ValueError, RecursionError):
                decoded = None
            if isinstance(decoded, list) and all(type(x) is float for x in decoded):
                values = decoded
        if values is None:
            pieces = middle.split(',') if middle else []
            if middle and len(pieces) <= 4096 and len(middle) <= 65536:
                if not VECTOR_BODY.fullmatch(middle):
                    fail('invalid vector element', 'invalid_vector')
                values = list(map(float, map(str.strip, pieces)))
            else:
                values = []
                for part in pieces:
                    part = part.strip()
                    if not NUMBER.fullmatch(part):
                        fail('invalid vector element', 'invalid_vector')
                    values.append(float(part))
    elif isinstance(value, list):
        values = value
    else:
        fail('expected vector', 'invalid_vector')
    if len(values) != dim:
        fail('vector dimension mismatch', 'dimension_mismatch')
    try:
        if not isinstance(value, str):
            for x in values:
                if type(x) is not int and type(x) is not float:
                    raise ValueError()
        fmt = '<' + str(dim) + 'f'
        result = list(struct.unpack(fmt, struct.pack(fmt, *values)))
        if not all(map(math.isfinite, result)):
            raise ValueError()
    except (ValueError, OverflowError, struct.error):
        fail('vector element must be finite float32', 'invalid_vector')
    return result


def convert(col, value):
    kind = col['type']
    if kind == 'BIGINT':
        if type(value) is not int or not -(1 << 63) <= value < (1 << 63):
            fail('expected signed BIGINT', 'invalid_value')
        return value
    if kind == 'VARCHAR':
        if not isinstance(value, str):
            fail('expected VARCHAR', 'invalid_value')
        try:
            value.encode('utf-8')
        except UnicodeError:
            fail('invalid Unicode string', 'invalid_value')
        if len(value) > col['n']:
            fail('VARCHAR value too long', 'value_too_long')
        return value
    return vector(value, col['n'])


# SQL keywords are ASCII; surrounding whitespace retains Python's Unicode rules.
_INSERT_HEAD = re.compile(r'\s*(?ai:INSERT)\s+(?ai:INTO)\s+')

# A complete, narrow SELECT shape; unsupported shapes use the normal parser.
_SELECT_TOPK = re.compile(
    r"\s*(?ai:SELECT)\s+(?P<plain>[A-Za-z_][A-Za-z_0-9]*)\s*,\s*"
    r"(?ai:L2_DISTANCE)\s*\(\s*(?P<vector_col>[A-Za-z_][A-Za-z_0-9]*)\s*,\s*"
    r"(?:(?P<quoted>'[^']*')|(?P<bare>\[[^\[\]']*\]))\s*\)\s+(?ai:AS)\s+(?P<alias>[A-Za-z_][A-Za-z_0-9]*)"
    r"\s+(?ai:FROM)\s+(?P<table>[A-Za-z_][A-Za-z_0-9]*)"
    r"(?:\s+(?ai:WHERE)\s+(?P<filter>[A-Za-z_][A-Za-z_0-9]*)\s*"
    r"(?P<op><=|>=|=|<|>)\s*(?P<number>[+-]?[0-9]+))?"
    r"\s+(?ai:ORDER)\s+(?ai:BY)\s+(?P<order>[A-Za-z_][A-Za-z_0-9]*)"
    r"(?:\s+(?P<direction>(?ai:ASC|DESC)))?"
    r"\s+(?ai:LIMIT)\s+(?P<limit>\+?[0-9]+)\s*;?\s*")


def _fast_select(text):
    match = _SELECT_TOPK.fullmatch(text)
    if match is None: return None
    parts = match.groupdict()
    if len(parts['limit']) > 128 or (parts['number'] is not None and len(parts['number']) > 128):
        return None
    if parts['quoted'] is not None:
        query = parts['quoted'][1:-1]
    else:
        try:
            query = _VECTOR_DECODER.decode(parts['bare'])
        except (ValueError, RecursionError):
            return None
        if not all(type(x) is int or (type(x) is float and math.isfinite(x)) for x in query):
            return None
    where = [(parts['filter'], parts['op'], int(parts['number']))] if parts['filter'] is not None else []
    return {'op': 'select', 'table': parts['table'],
            'projection': [(('column', parts['plain']), None),
                           (('distance', parts['vector_col'], query), parts['alias'])],
            'where': where, 'order': [(('column', parts['order']), parts['direction'] is not None and parts['direction'].upper() == 'DESC')],
            'limit': int(parts['limit'])}


def _fast_insert(text):
    """Parse the common bare INSERT ... VALUES shape without building tokens."""
    head = _INSERT_HEAD.match(text)
    if head is None: return None
    n, i = len(text), head.end()
    match = IDENT.match(text, i)
    if match is None: return None
    table = match.group(); i = match.end()
    while i < n and text[i].isspace(): i += 1
    keyword = text[i:i + 6]
    if not keyword.isascii() or keyword.upper() != 'VALUES': return None
    i += 6
    if i < n and (text[i].isalnum() or text[i] == '_'): return None
    rows = []
    while True:
        while i < n and text[i].isspace(): i += 1
        if i >= n or text[i] != '(': return None
        i += 1
        row = []
        while True:
            while i < n and text[i].isspace(): i += 1
            if i >= n: return None
            if text[i] == '[':
                try:
                    values, i = _VECTOR_DECODER.raw_decode(text, i)
                except (ValueError, RecursionError):
                    return None
                if not all(type(value) is int or
                           (type(value) is float and math.isfinite(value))
                           for value in values):
                    return None
                row.append(values)
            elif text[i] == "'":
                return None
            else:
                match = NUMBER.match(text, i)
                if match is None: return None
                token = match.group(); i = match.end()
                row.append(float(token) if any(c in token for c in '.eE') else int(token))
            while i < n and text[i].isspace(): i += 1
            if i < n and text[i] == ',':
                i += 1
                continue
            if i < n and text[i] == ')':
                i += 1
                break
            return None
        if not row: return None
        rows.append(row)
        while i < n and text[i].isspace(): i += 1
        if i >= n: break
        if text[i] == ',':
            i += 1
            continue
        if text[i] == ';':
            i += 1
            while i < n and text[i].isspace(): i += 1
            if i == n: break
        return None
    return {'op': 'insert', 'table': table, 'cols': None, 'rows': rows}


class Parser:
    def __init__(self, text):
        self.text = text
        self.tokens = None
        self.pos = 0

    def take(self, value):
        if self.pos < len(self.tokens):
            t = self.tokens[self.pos]
            if t.kind not in ('str', 'vec') and t.value.upper() == value:
                self.pos += 1
                return True
        return False

    def need(self, value):
        if not self.take(value):
            fail('expected ' + value)

    def ident(self):
        if self.pos == len(self.tokens) or self.tokens[self.pos].kind != 'id':
            fail('expected identifier')
        value = self.tokens[self.pos].value
        self.pos += 1
        return value

    def integer(self, positive=False):
        v = self.value()
        if type(v) is not int or v < int(positive):
            fail('expected positive integer' if positive else 'expected nonnegative integer')
        return v

    def value(self):
        if self.pos < len(self.tokens) and self.tokens[self.pos].kind == 'vec':
            items = self.tokens[self.pos].value
            self.pos += 1
            return items
        if self.take('['):
            items = []
            if not self.take(']'):
                while True:
                    v = self.value()
                    if type(v) not in (int, float):
                        fail('invalid vector element', 'invalid_vector')
                    items.append(v)
                    if self.take(']'):
                        break
                    self.need(',')
            return items
        if self.pos == len(self.tokens):
            fail('expected value')
        t = self.tokens[self.pos]
        self.pos += 1
        if t.kind == 'str':
            return t.value
        if t.kind == 'num':
            return float(t.value) if any(c in t.value for c in '.eE') else int(t.value)
        fail('unsupported value')

    def expr(self):
        name = self.ident()
        if name.upper() == 'L2_DISTANCE' and self.take('('):
            col = self.ident()
            self.need(',')
            val = self.value()
            self.need(')')
            return ('distance', col, val)
        return ('column', name)

    def conditions(self):
        out = []
        if self.take('WHERE'):
            while True:
                col = self.ident()
                for op in ('=', '<=', '>=', '<', '>'):
                    if self.take(op):
                        break
                else:
                    fail('unsupported comparison')
                out.append((col, op, self.value()))
                if not self.take('AND'):
                    break
        return out

    def parse(self):
        if self.tokens is None:
            fast = _fast_insert(self.text)
            if fast is None: fast = _fast_select(self.text)
            if fast is not None: return fast
            self.tokens = tokenize(self.text, True)
        if self.take('CREATE'):
            if self.take('TABLE'):
                name = self.ident()
                self.need('(')
                cols, pk_names = [], []
                while True:
                    if self.take('PRIMARY'):
                        self.need('KEY'); self.need('(')
                        pk_names.append(self.ident())
                        self.need(')')
                    else:
                        col = {'name': self.ident(), 'type': self.ident().upper(), 'n': 0, 'pk': False}
                        if col['type'] not in ('BIGINT', 'VARCHAR', 'VECTOR'):
                            fail('unsupported column type')
                        if col['type'] != 'BIGINT':
                            self.need('('); col['n'] = self.integer(True); self.need(')')
                        if self.take('PRIMARY'):
                            self.need('KEY'); col['pk'] = True
                        cols.append(col)
                    if self.take(')'):
                        break
                    self.need(',')
                for name_pk in pk_names:
                    found = [c for c in cols if c['name'].casefold() == name_pk.casefold()]
                    if len(found) != 1 or found[0]['pk']:
                        fail('invalid primary key')
                    found[0]['pk'] = True
                out = {'op': 'create', 'table': name, 'cols': cols}
            else:
                self.need('VECTOR'); self.need('INDEX')
                name = self.ident(); self.need('ON'); table = self.ident()
                self.need('('); col = self.ident(); self.need(')')
                out = {'op': 'index', 'table': table, 'name': name, 'col': col}
        elif self.take('INSERT'):
            self.need('INTO'); table = self.ident(); cols = None
            if self.take('('):
                cols = [self.ident()]
                while self.take(','):
                    cols.append(self.ident())
                self.need(')')
            self.need('VALUES'); rows = []
            while True:
                self.need('('); row = [self.value()]
                while self.take(','):
                    row.append(self.value())
                self.need(')'); rows.append(row)
                if not self.take(','):
                    break
            out = {'op': 'insert', 'table': table, 'cols': cols, 'rows': rows}
        elif self.take('SELECT'):
            projection = None
            if not self.take('*'):
                projection = []
                while True:
                    expr = self.expr()
                    alias = self.ident() if self.take('AS') else None
                    projection.append((expr, alias))
                    if not self.take(','):
                        break
            self.need('FROM'); table = self.ident(); where = self.conditions(); order = []
            if self.take('ORDER'):
                self.need('BY')
                while True:
                    expr = self.expr(); desc = self.take('DESC')
                    if not desc:
                        self.take('ASC')
                    order.append((expr, desc))
                    if not self.take(','):
                        break
            limit = self.integer() if self.take('LIMIT') else None
            out = {'op': 'select', 'table': table, 'projection': projection, 'where': where, 'order': order, 'limit': limit}
        elif self.take('UPDATE'):
            table = self.ident(); self.need('SET'); sets = []
            while True:
                col = self.ident(); self.need('='); sets.append((col, self.value()))
                if not self.take(','):
                    break
            out = {'op': 'update', 'table': table, 'sets': sets, 'where': self.conditions()}
        elif self.take('DELETE'):
            self.need('FROM'); table = self.ident()
            out = {'op': 'delete', 'table': table, 'where': self.conditions()}
        else:
            for op in ('BEGIN', 'COMMIT', 'ROLLBACK'):
                if self.take(op):
                    out = {'op': op.lower()}
                    if op == 'BEGIN':
                        self.take('TRANSACTION')
                    break
            else:
                fail('unsupported SQL statement')
        self.take(';')
        if self.pos != len(self.tokens):
            fail('unexpected trailing SQL')
        return out
