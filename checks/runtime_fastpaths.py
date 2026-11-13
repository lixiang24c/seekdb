#!/usr/bin/env python3
"""Differential vector/SELECT checks against a separately supplied baseline SQL module."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

PROBE = r'''
import collections,itertools,json,math,random,struct,sys,types
from minidb import sql
reference=types.ModuleType('runtime_reference_sql');sys.modules[reference.__name__]=reference
exec(compile(REFERENCE_SOURCE,'reference_sql.py','exec'),reference.__dict__)
counts=collections.Counter();hits=collections.Counter()
def bits(value):
    if type(value) is float:return ('float64',struct.pack('<d',value).hex())
    if isinstance(value,(tuple,list)):return (type(value).__name__,[bits(x) for x in value])
    if isinstance(value,dict):return {k:bits(v) for k,v in value.items()}
    return value
def vector_result(module,value,dim):
    try:
        result=module.vector(value,dim)
        return ('float32',struct.pack('<'+str(len(result))+'f',*result).hex())
    except module.DBError as e:return ('error',e.code,str(e))
def vector_check(value,dim,category):
    actual=vector_result(sql,value,dim);expected=vector_result(reference,value,dim)
    assert actual==expected,{'input':repr(value),'dimension':dim,'actual':actual,'expected':expected}
    counts[category]+=1
numbers=['0','-0','+0','01','.5','1.','1e3','-1e-999','1e309','-1e309','1e39',
         '3.4028234663852886e38','3.4028235677973366e38',repr(2**-149),repr(2**-150),
         repr(math.nextafter(2**-150,math.inf)),'١٢.٥','１２.５']
spaces=['',' ','\t','\n','\r','\v','\f','\x1c','\x1d','\x1e','\x1f','\u00a0','\u2003']
for number,space,dim in itertools.product(numbers,spaces,(1,2,3)):
    vector_check('['+space+number+space+','+space+'-0'+space+']',dim,'vector_numeric_whitespace')
for bad,dim in itertools.product(['nan','Infinity','true','false','null','"1"','[]','{}',
                                 '1e','0x1','1_0','','1e309,bad','1,,2'],(0,1,2)):
    vector_check('['+bad+']',dim,'vector_invalid_precedence')
class IntSubclass(int):pass
class FloatSubclass(float):pass
for value in ([True],['1'],[None],[IntSubclass(1)],[FloatSubclass(1)],[math.inf],[math.nan],
              [10**1000],[],[0.,-0.,2**-149],(0,1),None,b'[1]'):
    for dim in (0,1,2,3):vector_check(value,dim,'list_types')
for n in (0,1,4095,4096,4097):
    for dim in (n,n+1):vector_check('['+','.join(['-0']*n)+']',dim,'vector_count_boundary')
for length in (65535,65536,65537,65538):
    for token in ('-0','1e309','bad'):
        text='['+' '*(length-len(token)-4)+'0,'+token+']'
        assert len(text)==length
        for dim in (1,2):vector_check(text,dim,'vector_length_boundary')
rng=random.Random(2026111319)
for _ in range(500):
    values=[rng.uniform(-1e30,1e30) for _ in range(rng.randrange(1,33))]
    vector_check(json.dumps(values),len(values),'random_vector')
    vector_check(values,len(values),'random_list')
for text in ('[-0]','[-0e0]','[-1e-999]'):
    assert vector_result(sql,text,1)==('float32','00000080')

def parse(module,text,general=False):
    try:
        parser=module.Parser(text)
        if general:parser.tokens=module.tokenize(text,True)
        return ('ast',bits(parser.parse()))
    except module.DBError as e:return ('error',e.code,str(e))
    except (ValueError,RecursionError) as e:return ('exception',type(e).__name__,str(e))
def parse_check(text):
    actual=parse(sql,text);expected=parse(reference,text)
    assert actual==expected,{'sql':text,'actual':actual,'expected':expected}
    assert actual==parse(sql,text,True),{'sql':text,'general':parse(sql,text,True),'fast':actual}
    hits['fast' if sql._fast_select(text) is not None else 'fallback']+=1
    counts['select_differential']+=1
template='SELECT id,L2_DISTANCE(v,{vector}) AS d FROM t{where} ORDER BY {order}{direction} LIMIT {limit}'
for vector,where,order,direction,limit in itertools.product(
        ["'[1,-0]'",'[1,-0]',"'[.5,+1]'",'[.5,+1]'],
        ['', ' WHERE c=1',' WHERE c>=-1',' WHERE c<+01'],['id','d','v'],['',' ASC',' DESC'],['0','+1','10']):
    text=template.format(vector=vector,where=where,order=order,direction=direction,limit=limit)
    parse_check(text);parse_check(text+';')
base=template.format(vector="'[1,2]'",where=' WHERE c=1',order='d',direction='',limit='10')
for space in spaces[1:]:parse_check(base.replace(' ',space))
for a,b in [('SELECT','ſELECT'),('LIMIT','LİMIT'),('LIMIT','LıMIT'),('DISTANCE','DİSTANCE'),
            ('AS','Aſ'),('DESC','DEſC')]:parse_check((base+' DESC').replace(a,b))
for text in [base.replace('SELECT','select'),base+'; SELECT id FROM t',base+' junk',
             base.replace('id,','`id`,'),base.replace('SELECT ','SELECT /* comment */ '),
             base.replace(' WHERE c=1',' WHERE c=1 AND c<3'),base.replace(' AS d',' AS id'),
             base.replace(' AS d',' AS v'),base.replace(' ORDER BY d'," ORDER BY L2_DISTANCE(v,'[1,2]')"),
             base.replace("'[1,2]'","'[1,''2]'"),base.replace('LIMIT 10','LIMIT -0'),
             base.replace('LIMIT 10','LIMIT 1.0'),base.replace('LIMIT 10','LIMIT '+'1'*129),
             base.replace('c=1','c='+'9'*5000),base.replace("'[1,2]'",'[true]'),
             base.replace("'[1,2]'",'[1e309]'),base.replace("'[1,2]'",'[1,,2]')]:parse_check(text)
alphabet='abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789[](),;\'`+-/*=<> \n\tİſ'
for _ in range(1200):
    pos=rng.randrange(len(base)+1)
    text=base[:pos]+''.join(rng.choice(alphabet) for _ in range(rng.randrange(1,5)))+base[pos+rng.randrange(0,4):]
    parse_check(text)
assert hits['fast']>100 and hits['fallback']>100
print(json.dumps({'status':'PASS','checks':sum(counts.values()),'categories':counts,'select_paths':hits,
    'comparison':'float32 bytes, float64 AST bytes, error code and message, baseline and general-parser fallback'}))
'''


def source(reference):
    return 'REFERENCE_SOURCE = ' + repr(reference) + '\n' + PROBE


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--image')
    args = parser.parse_args()
    command = [sys.executable, '-B', '-'] if not args.image else [
        'docker','run','--rm','-i','--network=none','--cpus=2','--memory=2g','--memory-swap=2g',
        '--read-only','--entrypoint=python3',args.image,'-B','-']
    result = subprocess.run(command, input=source(args.reference.read_text()), text=True,
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=180)
    print(result.stdout,end='');print(result.stderr,end='',file=sys.stderr)
    raise SystemExit(result.returncode)
