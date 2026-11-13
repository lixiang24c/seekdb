#!/usr/bin/env python3
"""Source-process faults in a fused indexed merge and manifest publication."""
import json
from pathlib import Path
import tempfile
import recovery

recovery.WRAPPER=r'''
import os,sys
os.environ['MINIDB_CPP_STORAGE']='0'  # This fixture interrupts Python's iterator.
from minidb.storage import Storage
root=sys.argv[1]
real_replace=os.replace
def armed():return os.path.exists(os.path.join(root,'armed'))
def replace(src,dst):
    target=armed() and os.path.basename(dst)=='manifest.json'
    if target and os.environ['CUT']=='before':os._exit(86)
    real_replace(src,dst)
    if target and os.environ['CUT']=='after':os._exit(86)
os.replace=replace
original=Storage.write_segment
def write(self,table,records,created,**kwargs):
    if armed() and os.environ['CUT']=='during':
        def interrupted(source=records):
            for i,item in enumerate(source):
                if i==200:os._exit(86)
                yield item
        records=interrupted()
    return original(self,table,records,created,**kwargs)
Storage.write_segment=write
from minidb.server import main
raise SystemExit(main())
'''

def main():
    results=[]
    for cut in ('during','before','after'):
        with tempfile.TemporaryDirectory() as directory:
            data=Path(directory);c=recovery.Client(data,cut)
            c.query('CREATE TABLE t(id BIGINT PRIMARY KEY,n BIGINT,v VECTOR(1))')
            c.query('CREATE VECTOR INDEX ix ON t(v)')
            for start in range(0,700,100):
                c.query('INSERT INTO t VALUES '+','.join(f"({i},0,'[{i%17}]')" for i in range(start,start+100)))
            c.query("UPDATE t SET v='[-2]' WHERE id=0")
            (data/'armed').touch()
            statement='INSERT INTO t VALUES '+','.join(f"({i},1,'[{i%17}]')" for i in range(700,800))
            c.p.stdin.write(json.dumps({'id':1,'op':'execute','sql':statement})+'\n');c.p.stdin.flush()
            assert c.p.stdout.readline()==''
            c.p.wait(timeout=10);assert c.p.returncode==86
            c.p.stdin.close();c.p.stdout.close();c.p.stderr.close();(data/'armed').unlink()
            for _ in range(2):
                r=recovery.Client(data)
                n=800 if cut=='after' else 700
                assert r.query('SELECT id,n FROM t ORDER BY id')['rows']==[[i,int(i>=700)] for i in range(n)]
                expected=sorted(((4 if i==0 else (i%17)**2),i) for i in range(n))[:10]
                assert r.query("SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0]') LIMIT 10")['rows']==[[i] for d,i in expected]
                doc=json.loads((data/'manifest.json').read_text())
                keep={p[k] for t in doc['tables'].values() for p in t['segments'] for k in ('file','keys','index_file') if p.get(k)}
                assert {p.name for p in data.glob('mv-*')}==keep
                r.close()
            results.append({'cut':cut,'two_restarts':'PASS','state_and_index':'PASS','orphan_cleanup':'PASS'})
    out=recovery.ROOT/'protocol-results/merge-recovery';out.mkdir(parents=True,exist_ok=True)
    report={'status':'PASS','scope':'Current Python source processes; fault injection outside production code','scenarios':results}
    (out/'result.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))

if __name__=='__main__':main()
