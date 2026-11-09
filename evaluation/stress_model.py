"""Independent typed reference model. Does not import database implementation code."""
import copy
import functools
import math
import operator
import struct

try:
    import numpy as np
except ImportError:
    np=None

OPS={'=':operator.eq,'<':operator.lt,'<=':operator.le,'>':operator.gt,'>=':operator.ge}
MIN_I64=-(1<<63)
MAX_I64=(1<<63)-1


def f32(value):
    if type(value) not in (int,float):raise ValueError('numeric float32 required')
    number=struct.unpack('<f',struct.pack('<f',value))[0]
    if not math.isfinite(number):raise ValueError('finite float32 required')
    return number


class Table:
    def __init__(self,dimension=None):
        self.dimension=dimension
        self.columns=['id','tag','label','n']+(['v'] if dimension else [])
        self.rows={}
        self.index=None
    def validate(self,row):
        if set(row)!=set(self.columns):raise ValueError('columns')
        for name in ('id','tag','n'):
            if type(row[name]) is not int or not MIN_I64<=row[name]<=MAX_I64:raise ValueError('BIGINT')
        if not isinstance(row['label'],str) or len(row['label'])>8:raise ValueError('VARCHAR')
        result=dict(row)
        if self.dimension:
            if not isinstance(row['v'],list) or len(row['v'])!=self.dimension:raise ValueError('dimension')
            result['v']=[f32(x) for x in row['v']]
        return result


class Model:
    def __init__(self):
        self.committed={};self.pending=None
    @property
    def tables(self):return self.committed if self.pending is None else self.pending
    def begin(self):
        if self.pending is not None:raise ValueError('nested transaction')
        self.pending=copy.deepcopy(self.committed)
    def finish(self,commit):
        if self.pending is None:raise ValueError('no transaction')
        if commit:self.committed=self.pending
        self.pending=None
    def restart(self):self.pending=None
    def create(self,name,dimension=None):
        if name in self.tables:raise ValueError('duplicate table')
        self.tables[name]=Table(dimension)
    def insert(self,name,rows):
        table=self.tables[name];batch=[table.validate(row) for row in rows]
        keys=[row['id'] for row in batch]
        if len(set(keys))!=len(keys) or any(key in table.rows for key in keys):raise ValueError('duplicate key')
        table.rows.update((row['id'],row) for row in batch)
        return len(batch)
    @staticmethod
    def matches(row,terms):return all(OPS[op](row[column],value) for column,op,value in terms)
    def modify(self,name,terms,updates=None):
        table=self.tables[name]
        selected=[key for key,row in table.rows.items() if self.matches(row,terms)]
        if updates is None:
            for key in selected:del table.rows[key]
        else:
            if 'id' in updates:raise ValueError('primary key update')
            probe={'id':0,'tag':0,'label':'','n':0}
            if table.dimension:probe['v']=[0]*table.dimension
            table.validate(dict(probe,**updates))
            changed={key:table.validate(dict(table.rows[key],**updates)) for key in selected}
            table.rows.update(changed)
        return len(selected)
    def select(self,name,projection,terms,order,limit,use_numpy=True):
        table=self.tables[name]
        rows=[row for row in table.rows.values() if self.matches(row,terms)]
        distances={}
        for expr in [e for e,alias in projection]+[e for e,desc in order]:
            if isinstance(expr,tuple) and expr not in distances:
                query=[f32(x) for x in expr[1]]
                if len(query)!=table.dimension:raise ValueError('query dimension')
                if rows and np is not None and use_numpy:
                    matrix=np.asarray([row['v'] for row in rows],dtype=np.float64)
                    delta=matrix-np.asarray(query,dtype=np.float64)
                    values=np.sqrt(np.einsum('ij,ij->i',delta,delta))
                    distances[expr]={row['id']:float(value) for row,value in zip(rows,values)}
                else:
                    distances[expr]={row['id']:math.sqrt(math.fsum((a-b)**2 for a,b in zip(row['v'],query))) for row in rows}
        def evaluate(expr,row):return distances[expr][row['id']] if isinstance(expr,tuple) else row[expr]
        def compare(a,b):
            for expr,desc in order:
                x,y=evaluate(expr,a),evaluate(expr,b)
                if x!=y:return (-1 if x<y else 1)*(-1 if desc else 1)
            return (a['id']>b['id'])-(a['id']<b['id'])
        if order:rows.sort(key=functools.cmp_to_key(compare))
        topk=bool(order and len(order)==1 and isinstance(order[0][0],tuple) and not order[0][1] and limit is not None)
        eligible=distances[order[0][0]] if topk else None
        selected=rows if limit is None else rows[:limit]
        expected={'columns':[alias or (expr if isinstance(expr,str) else 'L2_DISTANCE') for expr,alias in projection],
                  'rows':[[evaluate(expr,row) for expr,alias in projection] for row in selected]}
        if not order:expected['unordered']=True
        ann=None
        if topk and [expr for expr,alias in projection]==['id',order[0][0]]:
            ann={'truth_ids':[row['id'] for row in selected],
                 'distances':{str(key):distance for key,distance in eligible.items()},'filtered':bool(terms)}
        return expected,ann
