#!/usr/bin/env python3
"""Actual-image ranking with 128D ties, extreme BIGINT keys and large K."""
import argparse,json,math,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'protocol_check'))
from _transport import Session
parser=argparse.ArgumentParser()
parser.add_argument('--image',default='minivectordb:topk-candidate')
parser.add_argument('--output',default='protocol-results/topk-sql')
args=parser.parse_args()
out=ROOT/args.output;out.mkdir(parents=True,exist_ok=True)
s=Session(json.loads((ROOT/'protocol_check/config.json').read_text()),out/'stderr',image=args.image)
ids=[-(1<<63),-1,0,(1<<63)-1]+list(range(1,197))
state={ident:(i%3,[float(i%17)]*128) for i,ident in enumerate(ids)}
checks=0
try:
    s.start()
    def q(sql,mode='exact'):
        r=s.request(sql,mode=mode);assert r['ok'],r
        return r
    q('CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,v VECTOR(128))')
    q('INSERT INTO t VALUES '+','.join(f"({ident},{c},'{json.dumps(v)}')" for ident,(c,v) in state.items()))
    q('CREATE VECTOR INDEX ix ON t(v)')
    for filtered in (False,True):
        ranked=sorted((math.fsum(x*x for x in v),i) for i,(c,v) in state.items() if not filtered or c==1)
        for k in [1,10,127,128,512,1024]:
            sql="SELECT id,L2_DISTANCE(v,'"+json.dumps([0.]*128)+"') AS d FROM t "+('WHERE c=1 ' if filtered else '')+f'ORDER BY d LIMIT {k}'
            for mode in ('exact','ann'):
                assert q(sql,mode)['rows']==[[i,math.sqrt(d)] for d,i in ranked[:k]]
                checks+=1
    q('BEGIN');q("UPDATE t SET v='"+json.dumps([0.]*128)+"' WHERE id=-1")
    q(f'DELETE FROM t WHERE id={-(1<<63)}')
    nearest="SELECT id FROM t ORDER BY L2_DISTANCE(v,'"+json.dumps([0.]*128)+"') LIMIT 1"
    assert q(nearest)['rows']==[[-1]]
    q('ROLLBACK');assert q(nearest)['rows']==[[ids[0]]]
    s.shutdown()
finally:s.close()
result={'status':'PASS','oracle_queries':checks,'transaction_visibility':'PASS','dimensions':128,'rows':200,'K':[1,10,127,128,512,1024],'extreme_bigint_keys':True}
(out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
