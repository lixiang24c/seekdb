"""Parser for the published SQL subset; never executes a partial parse."""
import math
import re
import struct
from dataclasses import dataclass


class DBError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def fail(message, code='unsupported_sql'):
    raise DBError(code, message)


@dataclass(frozen=True)
class Token:
    kind: str
    value: str


NUMBER = re.compile(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?')
IDENT = re.compile(r'[A-Za-z_][A-Za-z_0-9]*')


def tokenize(source):
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
        pieces = middle.split(',') if middle else []
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
        for x in values:
            if type(x) not in (int, float) or not math.isfinite(x):
                raise ValueError()
        fmt = '<' + str(dim) + 'f'
        result = list(struct.unpack(fmt, struct.pack(fmt, *values)))
        if not all(math.isfinite(x) for x in result):
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


class Parser:
    def __init__(self, text):
        self.tokens = tokenize(text)
        self.pos = 0

    def take(self, value):
        if self.pos < len(self.tokens):
            t = self.tokens[self.pos]
            if t.kind != 'str' and t.value.upper() == value:
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
