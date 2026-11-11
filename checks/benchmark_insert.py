#!/usr/bin/env python3
"""Fresh-volume, alternating batch-insert comparison with local cgroup snapshots."""
import argparse
import json
import math
from pathlib import Path
import random
import statistics
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'protocol_check'))
from _transport import Session

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--baseline',default='minivectordb:merge-baseline-a246a098')
    p.add_argument('--candidate',default='minivectordb:merge-candidate')
    p.add_argument('--output',default='protocol-results/merge-insert')
    p.add_argument('--rows',type=int,default=10000)
    p.add_argument('--rounds',type=int,default=3)
    args=p.parse_args()
    out=ROOT/args.output;out.mkdir(parents=True,exist_ok=True)
    rng=random.Random(20261204)
    vectors=[[rng.randrange(-64,65)/8 for _ in range(128)] for _ in range(args.rows)]
    statements=['INSERT INTO t VALUES '+','.join(f"({i},{i%10},'{('x'*512)}','{json.dumps(vectors[i])}')" for i in range(start,min(start+100,args.rows))) for start in range(0,args.rows,100)]
    report={'rows':args.rows,'dimensions':128,'batch_size':100,'explicit_transaction_per_batch':True,'rounds':args.rounds,'samples':{'baseline':[],'candidate':[]},'scope':'Independent data volume per run; T includes BEGIN/INSERT/COMMIT and output. Local cgroup snapshots are per reported device, not a certified dedicated-device metric; no formal E.'}
    def text(b):return b.decode() if isinstance(b,bytes) else b
    for turn in range(args.rounds):
        images=[('baseline',args.baseline),('candidate',args.candidate)]
        if turn%2:images.reverse()
        for label,image in images:
            s=Session(json.loads((ROOT/'protocol_check/config.json').read_text()),out/f'{label}-{turn}.stderr',image=image)
            def request(sql):
                r=s.request(sql);assert r['ok'],r
                return r
            def io():
                r=s.docker('exec',s.name,'cat','/sys/fs/cgroup/io.stat');assert r.returncode==0,r.stderr
                return {line.split()[0]:{k:int(v) for k,v in (kv.split('=') for kv in line.split()[1:])} for line in text(r.stdout).splitlines()}
            try:
                s.start();request('CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,body VARCHAR(1024),v VECTOR(128))')
                before=io();started=time.perf_counter()
                for batch_index,sql in enumerate(statements):
                    request('BEGIN');assert request(sql)['affected_rows']==min(100,args.rows-batch_index*100)
                    request('COMMIT')
                elapsed=time.perf_counter()-started
                after=io();peak=s.docker('exec',s.name,'cat','/sys/fs/cgroup/memory.peak');assert peak.returncode==0
                sample={'seconds':elapsed,'memory_peak_bytes':int(peak.stdout),'io_delta_by_device':{dev:{k:v-before.get(dev,{}).get(k,0) for k,v in values.items()} for dev,values in after.items()}}
                # Verify committed records after process restart, outside timing/resources.
                s.shutdown();s.start()
                assert request('SELECT id FROM t ORDER BY id')['rows']==[[i] for i in range(args.rows)]
                for i in (0,args.rows//2,args.rows-1):
                    assert request(f'SELECT c,body,v FROM t WHERE id={i}')['rows']==[[i%10,'x'*512,vectors[i]]]
                request('CREATE VECTOR INDEX ix ON t(v)')
                for filtered in (False,True):
                    q=vectors[args.rows//3]
                    truth=sorted((math.fsum((a-b)*(a-b) for a,b in zip(v,q)),i) for i,v in enumerate(vectors) if not filtered or i%10==3)[:10]
                    sql="SELECT id FROM t "+('WHERE c=3 ' if filtered else '')+"ORDER BY L2_DISTANCE(v,'"+json.dumps(q)+"') LIMIT 10"
                    assert request(sql)['rows']==[[i] for d,i in truth]
                s.shutdown();report['samples'][label].append(sample)
                print(json.dumps({label:sample}),flush=True)
            finally:s.close()
    report['medians']={label:{k:statistics.median(sample[k] for sample in samples) for k in ('seconds','memory_peak_bytes')} for label,samples in report['samples'].items()}
    report['status']='PASS';(out/'comparison.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report['medians']))

if __name__=='__main__':main()
