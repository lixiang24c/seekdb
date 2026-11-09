"""Deterministic stateful SQL test generation from typed operations, not SQL parsing."""
import gzip
import hashlib
import json
import io
import math
import random
from collections import Counter
from pathlib import Path
from .stress_model import Model,MIN_I64,MAX_I64
from .public_cases import steps

LABELS=['','a','a\'b','中','😀','甲','乙','é','é','[0,0]','x;y','--x','八'*8]
DIMS=[1,2,4,31,32,128,131]


def literal(value,quoted=False):
    if isinstance(value,str):return "'"+value.replace("'","''")+"'"
    if isinstance(value,list):
        text=json.dumps(value,separators=(',',':'),allow_nan=False)
        return literal(text) if quoted else text
    return str(value)


class Generator:
    def __init__(self,output,seed):
        self.output=output;self.seed=seed;self.counts=Counter();self.categories=Counter();self.case_id=None
    def emit(self,record):
        self.output.write(json.dumps(record,ensure_ascii=False,allow_nan=False,separators=(',',':'))+'\n')
        self.counts[record['kind']]+=1
        if record['kind']=='sql':self.categories[record['category']]+=1
    def case(self,identifier,**metadata):
        self.case_id=identifier;self.emit({'kind':'case','id':identifier,**metadata})
    def sql(self,text,expected=None,category='query',ann=None):
        record={'kind':'sql','sql':text,'expected':expected or {},'category':category}
        if ann:record['ann']=ann
        self.emit(record)
    def official(self,root):
        manifest=json.loads((root/'manifest.json').read_text())
        for case in manifest['cases']:
            self.case('public-'+case['id'],profile='upstream',seed=self.seed)
            for step in steps(root,case):self.emit(step)
            self.emit({'kind':'end'})
    def scenario(self,number,budget):
        seed=self.seed+number*1000003;rng=random.Random(seed);model=Model()
        profile='quantized' if number%32==31 else ['scalar','vector_scan','vector_index','transactions','recovery','multi_table','boundaries'][number%7]
        dimension=None if profile in ('scalar','multi_table') else DIMS[(number+number//7)%len(DIMS)]
        if profile=='quantized':dimension=[32,128,131][(number//32)%3]
        self.case(f'{number:04d}-{profile}',profile=profile,dimension=dimension,seed=seed)
        start=self.counts['sql'];names=['t','u'] if profile=='multi_table' else ['t']
        def create(name):
            model.create(name,dimension)
            columns='id BIGINT PRIMARY KEY,tag BIGINT,label VARCHAR(8),n BIGINT'+(f',v VECTOR({dimension})' if dimension else '')
            self.sql(f'CREATE TABLE {name}({columns})',category='create')
        for name in names:create(name)
        def row(key):
            value={'id':key,'tag':rng.randrange(5),'label':rng.choice(LABELS),'n':rng.choice([MIN_I64,MAX_I64,-3,-1,0,1,3,7])}
            if dimension:
                value['v']=[rng.randrange(-8,9)/4 for _ in range(dimension)]
                if key%7==0:value['v']=[0.]*dimension
                if key%17==0:value['v']=[0.1]*dimension
            return value
        def insert(name,values):
            quoted=True
            columns=list(model.tables[name].columns)
            reordered=bool(rng.randrange(4)==0)
            if reordered:columns.reverse()
            sql='INSERT INTO '+name+('('+','.join(columns)+')' if reordered else '')+' VALUES '+','.join('('+','.join(literal(value[column],quoted) for column in columns)+')' for value in values)
            affected=model.insert(name,values);self.sql(sql,{'affected_rows':affected},'insert')
        for name in names:
            values=[row(key) for key in ([MIN_I64,MAX_I64]+list(range(600 if profile=='quantized' else 12)))]
            for start_batch in range(0,len(values),100):insert(name,values[start_batch:start_batch+100])
        if profile in ('vector_index','transactions','recovery','quantized'):
            model.tables['t'].index='ix';self.sql('CREATE VECTOR INDEX ix ON t(v)',category='index')
        def query(name,terms=None,full=False):
            table=model.tables[name];terms=[] if terms is None else terms
            where=' WHERE '+' AND '.join(column+op+literal(value) for column,op,value in terms) if terms else ''
            projection=[(column,None) for column in table.columns] if full else [(column,None) for column in rng.choice([['id'],['id','tag','label'],['label','n']])]
            order=[('id',False)];limit=None
            if full:
                text='SELECT * FROM '+name+where+' ORDER BY id'
            elif dimension and rng.randrange(3)!=0:
                values=tuple(rng.randrange(-4,5)/4 for _ in range(dimension));expr=('distance',values)
                order=[(expr,False)];limit=rng.choice([0,1,3,10,1000])
                expression='L2_DISTANCE(v,'+literal(list(values),True)+')'
                if rng.randrange(2):
                    projection=[('id',None),(expr,'distance')]
                    text='SELECT id,'+expression+' AS distance FROM '+name+where+' ORDER BY distance'
                else:
                    projection=[('id',None)];text='SELECT id FROM '+name+where+' ORDER BY '+expression
                text+=' LIMIT '+str(limit)
            else:
                if rng.randrange(5)==0:
                    order=[]
                else:
                    order=[(rng.choice(['tag','label','n','id']),bool(rng.randrange(2)))]
                    limit=rng.choice([None,0,1,5,1000])
                text='SELECT '+','.join(column for column,alias in projection)+' FROM '+name+where
                if order:text+=' ORDER BY '+','.join(column+(' DESC' if desc else ' ASC') for column,desc in order)
                if limit is not None:text+=' LIMIT '+str(limit)
            expected,ann=model.select(name,projection,terms,order,limit)
            self.sql(text,expected,'snapshot' if full else 'vector_query' if any(isinstance(e,tuple) for e,d in order) else 'query',ann)
        def bad(name):
            table=model.tables[name];key=next(iter(table.rows),None)
            choices=[('unsupported',f'SELECT COUNT(*) FROM {name}'),('unsupported',f'DELETE FROM {name} WHERE id=1 OR id=2'),
                     ('unsupported',f'ALTER TABLE {name} ADD x BIGINT'),('unsupported',f'SELECT * FROM {name} JOIN {name}'),
                     ('trailing_tokens',f'DELETE FROM {name} WHERE id=1 garbage'),('null',f'UPDATE {name} SET label=NULL'),
                     ('primary_update',f'UPDATE {name} SET id=0 WHERE id=1'),('type',f"UPDATE {name} SET label='九'*9"),
                     ('varchar',f"UPDATE {name} SET label='123456789'"),('unknown_column',f'UPDATE {name} SET missing=1'),
                     ('bigint',f'UPDATE {name} SET n=9223372036854775808'),('bigint',f'UPDATE {name} SET n=1.5')]
            if dimension:
                zeros=',0'*(dimension-1)
                choices += [('vector_dimension',f"UPDATE {name} SET v='[]'"),('vector_nan',f"UPDATE {name} SET v='[NaN{zeros}]'"),
                            ('vector_infinity',f"UPDATE {name} SET v='[Infinity{zeros}]'"),
                            ('vector_overflow',f'UPDATE {name} SET v='+literal([1e39]*dimension,True)),
                            ('vector_type',f"UPDATE {name} SET v='[true{zeros}]'"),('invalid_index',f'CREATE VECTOR INDEX bad ON {name}(tag)')]
            if key is not None:
                new_key=100000+number
                values=[row(new_key),row(key)]
                text='INSERT INTO '+name+' VALUES '+','.join('('+','.join(literal(v[c],True) for c in table.columns)+')' for v in values)
                choices.append(('atomic_duplicate',text))
            category,text=rng.choice(choices)
            self.sql(text,{'error':True},'error_'+category);query(name,full=True)
        if profile=='boundaries' and dimension:
            value=row(1000);value['v']=[16777217]*dimension;insert('t',[value])
            expr=('distance',tuple([16777216]*dimension))
            expected,ann=model.select('t',[('id',None),(expr,'distance')],[('id','=',1000)],[(expr,False)],1)
            self.sql('SELECT id,L2_DISTANCE(v,'+literal([16777216]*dimension,True)+') AS distance FROM t WHERE id=1000 ORDER BY distance LIMIT 1',expected,'float32_rounding',ann)
        while self.counts['sql']-start<budget:
            step=self.counts['sql']-start;name=rng.choice(names);table=model.tables[name]
            action=rng.randrange(100)
            if profile=='quantized':action=80 if step%128 in (0,2) else 20 if step%128==1 else 60
            if action<15:
                available=[key for key in range(-20,65) if key not in table.rows]
                if available:
                    keys=rng.sample(available,min(len(available),rng.randrange(1,4)))
                    insert(name,[row(key) for key in keys])
                else:query(name)
            elif action<30:
                key=rng.choice(list(table.rows) or [0]);terms=[('id','=',key)]
                if profile!='quantized' and rng.randrange(3)==0:terms=[('tag',rng.choice(['=','>=','<']),rng.randrange(5))]
                column=rng.choice(['tag','label','n']+(['v'] if dimension else []))
                value=row(key)[column]
                affected=model.modify(name,terms,{column:value})
                where=' AND '.join(c+op+literal(v) for c,op,v in terms)
                self.sql(f'UPDATE {name} SET {column}='+literal(value,True)+' WHERE '+where,{'affected_rows':affected},'update')
            elif action<38:
                key=rng.randrange(-20,65);terms=[('id','=',key)]
                if rng.randrange(3)==0:terms=[('tag',rng.choice(['=','>=','<']),rng.randrange(5))]
                if rng.randrange(10)==0:terms=[]
                affected=model.modify(name,terms)
                where=' WHERE '+' AND '.join(c+op+literal(v) for c,op,v in terms) if terms else ''
                self.sql(f'DELETE FROM {name}'+where,{'affected_rows':affected},'delete')
            elif action<65:
                terms=[]
                if rng.randrange(2):
                    col=rng.choice(['id','tag','label','n']);op=rng.choice(list(('=','<','<=','>','>=')))
                    value=rng.choice(LABELS) if col=='label' else rng.choice([MIN_I64,-1,0,1,3,MAX_I64]) if col in ('id','n') else rng.randrange(7)
                    terms=[(col,op,value)]
                    if rng.randrange(3)==0:terms.append(('tag','>=',rng.randrange(5)))
                query(name,terms)
            elif action<77:bad(name)
            elif action<87:
                if model.pending is None:
                    model.begin();self.sql('BEGIN',category='begin')
                else:
                    commit=bool(rng.randrange(2));model.finish(commit)
                    self.sql('COMMIT' if commit else 'ROLLBACK',category='commit' if commit else 'rollback')
                    for current in names:query(current,full=True)
            elif action<90:
                if model.pending is None:model.begin();self.sql('BEGIN',category='begin')
                self.sql('BEGIN',{'error':True},'error_transaction')
            elif action<92 and profile=='recovery':
                model.restart();self.emit({'kind':'restart','kill':bool(rng.randrange(2))})
                for current in names:query(current,full=True)
            else:query(name,full=True)
        if model.pending is not None:
            model.finish(False);self.sql('ROLLBACK',category='rollback')
        model.restart();self.emit({'kind':'restart','kill':True})
        for name in names:query(name,full=True)
        self.emit({'kind':'end'})


def generate(path,public_root,operations=100000,scenarios=128,seed=20261109):
    if scenarios<1:raise ValueError('positive scenario count required')
    manifest=json.loads((Path(public_root)/'manifest.json').read_text())
    official_count=sum(case['statements'] for case in manifest['cases'])
    if operations<official_count+scenarios*20:raise ValueError('operations must allow at least 20 SQL per generated scenario')
    path=Path(path)
    if path.exists():raise FileExistsError(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('wb') as raw,gzip.GzipFile(filename='',fileobj=raw,mode='wb',mtime=0,compresslevel=6) as compressed,io.TextIOWrapper(compressed,encoding='utf-8') as output:
        generator=Generator(output,seed)
        generator.emit({'kind':'suite','version':'minivectordb-stateful-v1','seed':seed})
        generator.official(Path(public_root))
        budget=math.ceil((operations-generator.counts['sql'])/scenarios)
        for number in range(scenarios):
            generator.scenario(number,budget)
            if number%16==15:print(f'generated {number+1}/{scenarios} scenarios, {generator.counts["sql"]} SQL',flush=True)
    digest=hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):digest.update(block)
    manifest={'version':'minivectordb-stateful-v1','seed':seed,'requested_sql':operations,
              'counts':dict(generator.counts),'categories':dict(generator.categories),
              'sha256':digest.hexdigest(),'bytes':path.stat().st_size}
    path.with_suffix('.manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    return manifest
