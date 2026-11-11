#!/usr/bin/env python3
"""Full 10k/128D/1000-query workload, independent volumes and cgroup snapshots."""
import argparse,json,math,random,statistics,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'protocol_check'))
from _transport import Session

def main():
    p=argparse.ArgumentParser();p.add_argument('--baseline',default='minivectordb:parser-baseline-291d4a4a');p.add_argument('--candidate',default='minivectordb:parser-candidate');p.add_argument('--output',default='protocol-results/parser-workload');args=p.parse_args()
    out=ROOT/args.output;out.mkdir(parents=True,exist_ok=True)
    rng=random.Random(20261207);n,dim,nq=10000,128,1000
    vectors=[[rng.randrange(-64,65)/8 for _ in range(dim)] for _ in range(n)]
    queries=[[rng.randrange(-64,65)/8 for _ in range(dim)] for _ in range(nq)]
    inserts=['INSERT INTO t VALUES '+','.join(f"({i},{i%10},'{('x'*512)}','{json.dumps(vectors[i])}')" for i in range(start,start+100)) for start in range(0,n,100)]
    statements=["SELECT id,L2_DISTANCE(v,'"+json.dumps(q)+"') AS d FROM t "+(f'WHERE c={(i//2)%10} ' if i%2 else '')+'ORDER BY d LIMIT 10' for i,q in enumerate(queries)]
    truth=[]
    for i in range(8):
        ranked=sorted((math.fsum((a-b)*(a-b) for a,b in zip(v,queries[i])),j) for j,v in enumerate(vectors) if not i%2 or j%10==(i//2)%10)[:10]
        truth.append([[j,math.sqrt(d)] for d,j in ranked])
    report={'rows':n,'dimensions':dim,'batch_size':100,'queries':nq,'filtered_queries':nq//2,'k':10,'rounds':3,'seed':20261207,'samples':{'baseline':[],'candidate':[]},'scope':'Independent volumes and alternating image order. Query preparation outside query T but inside query B/M. Local per-device cgroup IO, filesystem sync before snapshots. No platform cache-reset procedure or fixed baseline; no formal E. Small in-container cat/sync diagnostic processes included in resource snapshots.'}
    expected=None
    for turn in range(3):
        order=[('baseline',args.baseline),('candidate',args.candidate)]
        if turn%2:order.reverse()
        for label,image in order:
            s=Session(json.loads((ROOT/'protocol_check/config.json').read_text()),out/f'{label}-{turn}.stderr',image=image)
            def request(sql):
                r=s.request(sql,mode='exact');assert r['ok'],r
                return r
            def resources():
                s.docker('exec',s.name,'sync','-f','/data')
                raw=s.docker('exec',s.name,'cat','/sys/fs/cgroup/io.stat','/sys/fs/cgroup/memory.peak').stdout.decode()
                devices={};peak=None
                for line in raw.splitlines():
                    fields=line.split()
                    if ':' in fields[0]:devices[fields[0]]={k:int(v) for k,v in (word.split('=') for word in fields[1:])}
                    else:peak=int(line)
                assert peak is not None
                return {'memory_peak_bytes':peak,'io_by_device':devices,'all_reported_device_bytes':sum(v.get('rbytes',0)+v.get('wbytes',0) for v in devices.values())}
            try:
                s.start();request('CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,body VARCHAR(1024),v VECTOR(128))')
                started=time.perf_counter()
                for sql in inserts:
                    request('BEGIN');assert request(sql)['affected_rows']==100;request('COMMIT')
                sample={'write_seconds':time.perf_counter()-started,'write_resources':resources()}
                started=time.perf_counter();request('CREATE VECTOR INDEX ix ON t(v)');sample['index_prepare_seconds']=time.perf_counter()-started
                for sql in statements[:2]:request(sql)
                started=time.perf_counter();answers=[request(sql)['rows'] for sql in statements]
                sample['query_seconds']=time.perf_counter()-started;sample['query_resources']=resources()
                assert answers[:8]==truth
                assert all(len(rows)==10 and rows==sorted(rows,key=lambda r:(r[1],r[0])) for rows in answers)
                if expected is None:expected=answers
                else:assert answers==expected
                # Persistence checks run after both measurement snapshots.
                s.shutdown();s.start();assert request('SELECT id FROM t ORDER BY id')['rows']==[[i] for i in range(n)]
                for i in (0,n//2,n-1):assert request(f'SELECT c,body,v FROM t WHERE id={i}')['rows']==[[i%10,'x'*512,vectors[i]]]
                s.shutdown();report['samples'][label].append(sample);print(json.dumps({label:sample}),flush=True)
            finally:s.close()
    report['medians']={label:{'write_seconds':statistics.median(x['write_seconds'] for x in samples),'query_seconds':statistics.median(x['query_seconds'] for x in samples),'write_memory_peak_bytes':statistics.median(x['write_resources']['memory_peak_bytes'] for x in samples),'query_memory_peak_bytes':statistics.median(x['query_resources']['memory_peak_bytes'] for x in samples),'write_device_bytes':statistics.median(x['write_resources']['all_reported_device_bytes'] for x in samples),'query_device_bytes':statistics.median(x['query_resources']['all_reported_device_bytes'] for x in samples)} for label,samples in report['samples'].items()}
    report['correctness']='6000 timed responses compared; first8 per run match independent exact oracle; committed rows checked after each restart.'
    report['status']='PASS';(out/'comparison.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report['medians']))

if __name__=='__main__':main()
