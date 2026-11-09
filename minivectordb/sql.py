"""SQL subset executor. Result transport is kept separate from execution."""
import re
import math
import json
import sys
from collections import OrderedDict
from typing import NamedTuple
from .core import DBError

TOKEN=re.compile(r"\s+|--[^\n]*|/\*.*?\*/|'(?:''|[^'])*'|`[^`]+`|\[[0-9eE+.,\-\s]*\]|\[(?:/\*.*?\*/|--[^\n]*(?:\n|$)|[^\[\]])*\]|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|[A-Za-z_][A-Za-z_0-9]*|<=|>=|!=|<>|[(),;*=<>\[\]+-]",re.S)

class VectorParameter(NamedTuple):
    token_index:int

class SQL:
    PLAN_CACHE_LIMIT=128*1024
    def __init__(self, db):
        self.db=db
        self._plans=OrderedDict();self._plan_bytes=0;self._parameterized=False;self._vector_parameters=set()
    def execute(self, text):
        self.tokens=[]; end=0
        for match in TOKEN.finditer(text):
            if match.start()!=end: raise DBError('invalid SQL token')
            end=match.end(); token=match.group()
            if not token.isspace() and not token.startswith(('--','/*')): self.tokens.append(token)
        if end!=len(text): raise DBError('invalid SQL token')
        self.i=0
        if self.tokens and self.tokens[-1]==';':self.tokens.pop()
        self._parameterized=bool(self.tokens and self.tokens[0].upper()=='SELECT' and
                                 not any(token in ('[',']') for token in self.tokens))
        self._vector_parameters={i for i,token in enumerate(self.tokens) if token.startswith('[') or
            (token.startswith("'") and i>=4 and self.tokens[i-4].upper()=='L2_DISTANCE' and
             self.tokens[i-3]=='(' and self.tokens[i-1]==',')} if self._parameterized else set()
        key=tuple(None if i in self._vector_parameters else token for i,token in enumerate(self.tokens)) if self._parameterized else None
        cached=self._plans.get(key) if key is not None else None
        if cached is not None:
            self._plans.move_to_end(key)
            return cached[0]()
        statement=self.parse()
        if self.i!=len(self.tokens): raise DBError('unsupported trailing SQL')
        if key is not None and None in key:
            # Account for the token key, parsed nodes and closure; vector payloads
            # stay in the current request rather than being retained by a plan.
            size=4096+4*sum(sys.getsizeof(token) for token in key)
            if size<=self.PLAN_CACHE_LIMIT:
                while self._plans and self._plan_bytes+size>self.PLAN_CACHE_LIMIT:
                    self._plan_bytes-=self._plans.popitem(last=False)[1][1]
                self._plans[key]=(statement,size);self._plan_bytes+=size
        return statement()
    def peek(self):return self.tokens[self.i] if self.i<len(self.tokens) else ''
    def pop(self):
        value=self.peek()
        if not value:raise DBError('unexpected SQL end')
        self.i+=1;return value
    def take(self, token):
        if self.peek().upper()==token:self.i+=1;return True
        return False
    def need(self,token):
        if not self.take(token):raise DBError('expected '+token)
    def identifier(self):
        value=self.pop()
        if value.startswith('`'):return value[1:-1]
        if not re.fullmatch('[A-Za-z_][A-Za-z_0-9]*',value):raise DBError('expected identifier')
        return value.lower()
    def literal(self):
        token=self.pop()
        if self.i-1 in self._vector_parameters:return VectorParameter(self.i-1)
        if token.startswith("'"):return token[1:-1].replace("''", "'")
        if token.startswith('[') and token!='[':
            return self.vector_literal(token)
        if token=='[':
            result=[]
            if not self.take(']'):
                while True:
                    result.append(self.literal())
                    if self.take(']'):break
                    self.need(',')
            return result
        sign=-1 if token=='-' else 1
        if token in ('-','+'):token=self.pop()
        try:return sign*(float(token) if any(c in token.lower() for c in '.e') else int(token))
        except ValueError:raise DBError('expected literal')
    @staticmethod
    def vector_literal(token):
        cleaned=re.sub(r'--[^\n]*|/\*.*?\*/',' ',token,flags=re.S)
        try:
            return json.loads(cleaned)
        except (ValueError,OverflowError):
            parts=cleaned[1:-1].split(',')
            numeric=r'[+-]?\s*(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
            if not all(re.fullmatch(numeric,part.strip()) for part in parts):
                raise DBError('invalid vector literal')
            return [float(re.sub(r'\s+','',part)) for part in parts]
    def expression(self):
        if self.take('L2_DISTANCE'):
            self.need('(');column=self.identifier();self.need(',');vector=self.literal();self.need(')')
            return ('distance',column,vector)
        return ('column',self.identifier())
    def where(self):
        terms=[]
        if self.take('WHERE'):
            while True:
                col=self.identifier();op=self.pop()
                if op not in ('=','<','<=','>','>='):raise DBError('unsupported comparison')
                terms.append((col,op,self.literal()))
                if not self.take('AND'):break
        return terms
    def predicate(self,table,terms):
        schema=self.db._table(table)['columns']
        for col,op,val in terms:
            if col not in schema:raise DBError('unknown column')
            kind=schema[col][0]
            if kind=='VECTOR' or (kind=='BIGINT' and type(val) is not int) or (kind=='VARCHAR' and not isinstance(val,str)):
                raise DBError('invalid comparison type')
        def matches(row):
            for col,op,val in terms:
                x=row[col]
                if not {'=':lambda:x==val,'<':lambda:x<val,'<=':lambda:x<=val,'>':lambda:x>val,'>=':lambda:x>=val}[op]():return False
            return True
        matches.terms=terms
        return matches
    def parse(self):
        if self.take('BEGIN'):
            self.take('TRANSACTION');return lambda:self.db.begin()
        if self.take('COMMIT'):return lambda:self.db.commit()
        if self.take('ROLLBACK'):return lambda:self.db.rollback()
        if self.take('CREATE'):
            if self.take('VECTOR'):
                self.need('INDEX');index=self.identifier();self.need('ON');table=self.identifier()
                self.need('(');column=self.identifier();self.need(')')
                return lambda:self.db.create_vector_index(index,table,column)
            self.need('TABLE');name=self.identifier();self.need('(');columns={};primary=None
            while True:
                if self.take('PRIMARY'):
                    self.need('KEY');self.need('(');key=self.identifier();self.need(')')
                    if primary is not None:raise DBError('multiple primary keys')
                    primary=key
                else:
                    col=self.identifier();kind=self.pop().upper();spec=[kind]
                    if col in columns:raise DBError('duplicate column')
                    if kind in ('VARCHAR','VECTOR'):
                        self.need('(');spec.append(self.literal());self.need(')')
                    columns[col]=spec
                    if self.take('PRIMARY'):
                        self.need('KEY')
                        if primary is not None:raise DBError('multiple primary keys')
                        primary=col
                if self.take(')'):break
                self.need(',')
            return lambda:self.db.create(name,columns,primary)
        if self.take('INSERT'):
            self.need('INTO');name=self.identifier();cols=None
            if self.take('('):
                cols=[]
                while True:
                    cols.append(self.identifier())
                    if self.take(')'):break
                    self.need(',')
                if len(set(cols))!=len(cols):raise DBError('duplicate insert column')
            self.need('VALUES');rows=[]
            while True:
                self.need('(');row=[]
                while True:
                    row.append(self.literal())
                    if self.take(')'):break
                    self.need(',')
                rows.append(row)
                if not self.take(','):break
            def insert():
                names=cols or list(self.db._table(name)['columns'])
                if any(len(r)!=len(names) for r in rows):raise DBError('insert arity mismatch')
                self.db.insert(name,(dict(zip(names,r)) for r in rows))
                return {'affected':len(rows)}
            return insert
        if self.take('UPDATE'):
            name=self.identifier();self.need('SET');updates={}
            while True:
                col=self.identifier();self.need('=')
                if col in updates:raise DBError('duplicate assignment')
                updates[col]=self.literal()
                if not self.take(','):break
            terms=self.where()
            return lambda:{'affected':self.db.modify(name,self.predicate(name,terms),updates)}
        if self.take('DELETE'):
            self.need('FROM');name=self.identifier();terms=self.where()
            return lambda:{'affected':self.db.modify(name,self.predicate(name,terms))}
        if self.take('SELECT'):
            projection=[]
            if self.take('*'):projection=None
            else:
                while True:
                    expr=self.expression();alias=None
                    if self.take('AS'):alias=self.identifier()
                    projection.append((expr,alias))
                    if not self.take(','):break
            self.need('FROM');name=self.identifier();terms=self.where();order=[];limit=None
            if self.take('ORDER'):
                self.need('BY')
                while True:
                    expr=self.expression();desc=self.take('DESC')
                    if not desc:self.take('ASC')
                    order.append((expr,desc))
                    if not self.take(','):break
            if self.take('LIMIT'):
                limit=self.literal()
                if type(limit) is not int or limit<0:raise DBError('invalid LIMIT')
            return lambda:self.select(name,projection,terms,order,limit)
        raise DBError('unsupported SQL statement')
    def select(self,name,projection,terms,order,limit):
        import functools
        import heapq
        table=self.db._table(name);schema=table['columns'];pk=table['primary']
        if projection is None:projection=[(('column',c),None) for c in schema]
        aliases={a:e for e,a in projection if a}
        bound_vectors={}
        def prepare(expr):
            if expr[0]=='column' and expr[1] not in schema and expr[1] in aliases:expr=aliases[expr[1]]
            if expr[1] not in schema:raise DBError('unknown column')
            if expr[0]=='distance':
                spec=schema[expr[1]]
                if spec[0]!='VECTOR':raise DBError('distance requires vector')
                value=expr[2]
                if isinstance(value,VectorParameter):
                    key=(expr[1],value.token_index)
                    if key not in bound_vectors:
                        token=self.tokens[value.token_index]
                        raw=token[1:-1].replace("''", "'") if token.startswith("'") else self.vector_literal(token)
                        bound_vectors[key]=self.db.vector(raw,spec[1])
                    return ('distance',expr[1],bound_vectors[key])
                return ('distance',expr[1],self.db.vector(value,spec[1]))
            return expr
        expressions=[prepare(e) for e,a in projection]
        ordering=[(prepare(e),d) for e,d in order]
        if any(schema[e[1]][0]=='VECTOR' and e[0]=='column' for e,d in ordering):raise DBError('cannot order vector')
        predicate=self.predicate(name,terms)
        def evaluate(expr,row):
            if expr[0]=='column':return row[expr[1]]
            return math.sqrt(sum((a-b)**2 for a,b in zip(row[expr[1]],expr[2])))
        distances=None
        reuse_distance=[False]*len(expressions)
        rows=(row for row in self.db.rows(name) if predicate(row))
        if ordering and len(ordering)==1 and ordering[0][0][0]=='distance' and not ordering[0][1] and limit is not None:
            expr=ordering[0][0]
            reuse_distance=[e==expr for e in expressions]
            need_vector=any((e[0]=='column' and schema[e[1]][0]=='VECTOR') or (e[0]=='distance' and not reuse_distance[i]) for i,e in enumerate(expressions))
            primary_only=not need_vector and all(e[0]!='column' or e[1]==pk for e in expressions)
            winners=self.db.topk(name,expr[1],expr[2],limit,predicate,scalar_only=not need_vector,primary_only=primary_only)
            distances={row[pk]:distance for row,distance in winners}
            rows=(row for row,distance in winners)
        elif ordering:
            def compare(a,b):
                for expr,desc in ordering:
                    x,y=evaluate(expr,a),evaluate(expr,b)
                    if x!=y:return (-1 if x<y else 1)*(-1 if desc else 1)
                return (a[pk]>b[pk])-(a[pk]<b[pk])
            key=functools.cmp_to_key(compare)
            if limit is not None:rows=heapq.nsmallest(limit,rows,key=key)
            else:
                # External merge sort keeps unbounded ORDER BY out of RAM.
                rows=self.external_sort(rows,key)
        import itertools
        if limit is not None:rows=itertools.islice(rows,limit)
        return {'columns':[a or (e[1] if e[0]=='column' else 'L2_DISTANCE') for e,a in projection],
                'rows':([distances[row[pk]] if reuse_distance[i] else evaluate(e,row) for i,e in enumerate(expressions)] for row in rows)}
    def external_sort(self,rows,key):
        import tempfile,json,heapq,contextlib
        # Fan-in bounded by 32. Files belong to the metered data directory.
        with tempfile.TemporaryDirectory(prefix='sort-',dir=self.db.root) as directory:
            from pathlib import Path
            counter=0;files=[];chunk=[];size=0
            def write(items):
                nonlocal counter
                path=Path(directory)/str(counter);counter+=1
                with path.open('w') as out:
                    for item in items:out.write(json.dumps(item,ensure_ascii=False)+'\n')
                return path
            for row in rows:
                chunk.append(row);size+=len(json.dumps(row,ensure_ascii=False).encode())
                if size>=1024*1024:
                    files.append(write(sorted(chunk,key=key)));chunk=[];size=0
            if chunk:files.append(write(sorted(chunk,key=key)))
            def merge(paths):
                with contextlib.ExitStack() as stack:
                    streams=[stack.enter_context(p.open()) for p in paths]
                    yield from heapq.merge(*((json.loads(line) for line in stream) for stream in streams),key=key)
            while len(files)>32:
                following=[]
                for start in range(0,len(files),32):following.append(write(merge(files[start:start+32])))
                for path in files:path.unlink()
                files=following
            yield from merge(files)
