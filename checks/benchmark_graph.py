#!/usr/bin/env python3
"""ANN experiment: held-out queries, exact oracle, preparation and query timings."""
import argparse,json,math,random,statistics,struct,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'protocol_check'))
from _transport import Session
p=argparse.ArgumentParser();p.add_argument('--candidate',default='minivectordb:graph-enabled');p.add_argument('--distribution',choices=['uniform','clustered','ood'],default='uniform');p.add_argument('--seed',type=int,default=20261130);p.add_argument('--rows',type=int,default=10000);p.add_argument('--queries',type=int,default=40);p.add_argument('--output',default='protocol-results/graph-uniform');args=p.parse_args()
out=ROOT/args.output;out.mkdir(parents=True,exist_ok=True)
rng=random.Random(args.seed);dim=128;f32=lambda v:struct.unpack('<f',struct.pack('<f',v))[0]
centers=[[rng.uniform(-4,4) for _ in range(dim)] for _ in range(16)]
vectors=[]
for i in range(args.rows):
    if args.distribution=='clustered':v=[a+rng.gauss(0,.1) for a in centers[min(15,i*16//args.rows)]]
    elif args.distribution=='ood':
        v=[rng.gauss(0,1) for _ in range(dim)];norm=math.sqrt(sum(x*x for x in v));v=[x/norm for x in v]
    else:v=[rng.uniform(-1,1) for _ in range(dim)]
    vectors.append([f32(x) for x in v])
queries=[]
for i in range(args.queries):
    if args.distribution=='clustered':v=[a+rng.gauss(0,.05) for a in centers[rng.randrange(16)]]
    elif args.distribution=='ood':v=[0. if i%4<2 else 100. for _ in range(dim)]
    else:v=[rng.uniform(-1,1) for _ in range(dim)]
    queries.append([f32(x) for x in v])
truth=[]
for i,q in enumerate(queries):
    candidates=range(args.rows) if i%2==0 else range(3,args.rows,10)
    truth.append(sorted((math.fsum((a-b)*(a-b) for a,b in zip(vectors[j],q)),j) for j in candidates)[:10])
report={'rows':args.rows,'dimensions':dim,'queries':args.queries,'distribution':args.distribution,'seed':args.seed,'runs':{},'scope':'One initial experimental comparison, not formal score; independent fixtures; graph preparation timed separately'}
for label,image in [('exact','minivectordb:migration-mask-ad592573'),('graph',args.candidate)]:
    s=Session(json.loads((ROOT/'protocol_check/config.json').read_text()),out/(label+'.stderr'),image=image)
    try:
        if label=='graph':
            env=json.loads(s.docker('image','inspect',image,'--format','{{json .Config.Env}}').stdout)
            report['graph_ef']=next((v.split('=',1)[1] for v in env if v.startswith('MINIDB_ANN_EF=')),'512')
        s.start()
        def query(sql,mode='exact'):
            r=s.request(sql,mode=mode);assert r['ok'],r
            return r
        query('CREATE TABLE t(id BIGINT PRIMARY KEY,category BIGINT,v VECTOR(128))')
        for start in range(0,args.rows,100):
            query('INSERT INTO t VALUES '+','.join(f"({i},{i%10},'{json.dumps(vectors[i])}')" for i in range(start,min(args.rows,start+100))))
        started=time.perf_counter();query('CREATE VECTOR INDEX ix ON t(v)');prep=time.perf_counter()-started
        latency=[];recalls=[]
        for i,q in enumerate(queries):
            sql="SELECT id,L2_DISTANCE(v,'"+json.dumps(q)+"') AS d FROM t "+('WHERE category=3 ' if i%2 else '')+'ORDER BY d LIMIT 10'
            started=time.perf_counter();r=query(sql,'ann' if label=='graph' else 'exact');latency.append(time.perf_counter()-started)
            ids=[row[0] for row in r['rows']];assert len(ids)==10 and len(set(ids))==10
            assert all(0<=j<args.rows and (not i%2 or j%10==3) for j in ids)
            assert r['rows']==sorted(r['rows'],key=lambda row:(row[1],row[0]))
            for j,d in r['rows']:
                actual=math.sqrt(math.fsum((a-b)*(a-b) for a,b in zip(vectors[j],q)))
                assert math.isclose(d,actual,rel_tol=1e-6,abs_tol=1e-6)
            recalls.append(len(set(ids)&{j for _,j in truth[i]})/10)
            if label=='exact':assert ids==[j for _,j in truth[i]]
        # exact remains exact even with the graph present.
        for i in range(min(4,len(queries))):
            sql="SELECT id FROM t "+('WHERE category=3 ' if i%2 else '')+"ORDER BY L2_DISTANCE(v,'"+json.dumps(queries[i])+"') LIMIT 10"
            assert query(sql)['rows']==[[j] for _,j in truth[i]]
        report['runs'][label]={'prepare_seconds':prep,'total_query_seconds':sum(latency),'unfiltered_seconds':sum(latency[::2]),'filtered_seconds':sum(latency[1::2]),'unfiltered_recall':statistics.mean(recalls[::2]),'filtered_recall':statistics.mean(recalls[1::2]),'min_recall':min(recalls)}
        peak=s.docker('exec',s.name,'cat','/sys/fs/cgroup/memory.peak')
        assert peak.returncode==0,peak.stderr
        report['runs'][label]['container_memory_peak_bytes']=int(peak.stdout)
        print(json.dumps({label:report['runs'][label]}),flush=True)
        s.shutdown()
    finally:s.close()
report['status']='PASS' if report['runs']['graph']['unfiltered_recall']>=.95 and report['runs']['graph']['filtered_recall']>=.95 else 'LOW_RECALL'
(out/'result.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'status':report['status']}))
