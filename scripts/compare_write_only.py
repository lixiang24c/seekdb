#!/usr/bin/env python3
"""One paired write check; complete persisted contents verified after restart."""
import argparse
import hashlib
import json
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
import numpy as np
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from evaluation.benchmark import vector_sql
from evaluation.driver import Client
from evaluation.stress_run import Process

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--baseline',default='minivectordb:team')
parser.add_argument('--candidate',default='minivectordb:tiered-write')
parser.add_argument('--rows',type=int,default=40000)
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args();args.output=args.output.resolve();args.output.mkdir(parents=True,exist_ok=False)
vectors=np.random.default_rng(42).normal(size=(args.rows,128)).astype(np.float32)
batches=[];digest=hashlib.sha256()
for key,vector in enumerate(vectors,1):
    digest.update(struct.pack('<qq',key,key%10));digest.update(vector.tobytes())
for start in range(0,args.rows,100):
    batches.append('INSERT INTO t VALUES '+','.join(f'({i+1},{(i+1)%10},{vector_sql(vectors[i])})' for i in range(start,min(start+100,args.rows))))
report={'scope':'single paired write comparison; two CPUs, 128 MiB, no swap, offline containers',
        'rows':args.rows,'dimension':128,'batch_size':100,'seed':42,'distribution':'normal',
        'expected_content_sha256':digest.hexdigest(),'results':{}}
verify='''import hashlib,json,struct
from minivectordb.core import Database
db=Database('/data');h=hashlib.sha256();count=0
for row in db.rows('t'):
 h.update(struct.pack('<qq',row['id'],row['tag']));h.update(struct.pack('<128f',*row['v']));count+=1
db.close();print(json.dumps({'records':count,'sha256':h.hexdigest()}))
'''
def save():(args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
for label,image in [('baseline',args.baseline),('candidate',args.candidate)]:
    data=args.output/label;data.mkdir();name='minivector-write-'+uuid.uuid4().hex
    command=['docker','run','--rm','-i','--name',name,'--network','none','--cpus','2','--memory','128m','--memory-swap','128m','-v',str(data)+':/data',image]
    item={'image':image,'image_id':subprocess.check_output(['docker','inspect','-f','{{.Id}}',image],text=True).strip()}
    report['results'][label]=item;save()
    with (args.output/(label+'.stderr')).open('w') as log:
        client=Process(command,data,log,timeout=60)
        try:
            assert not client.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,tag BIGINT,v VECTOR(128))').get('error')
            started=time.perf_counter()
            for index,sql in enumerate(batches,1):
                actual=client.execute(sql)
                if actual.get('error') or actual.get('affected_rows')!=min(100,args.rows-(index-1)*100):raise AssertionError(actual)
                if index%100==0:print(label,'inserted',index*100,flush=True)
            item['seconds']=time.perf_counter()-started
            pid=int(subprocess.check_output(['docker','inspect','-f','{{.State.Pid}}',name],text=True))
            item['resources']=Client.metrics(SimpleNamespace(pid=pid));save()
        finally:
            try:client.stop()
            finally:subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    result=subprocess.run(['docker','run','--rm','--network','none','--cpus','2','--memory','128m','--memory-swap','128m',
                          '-v',str(data)+':/data','--entrypoint','python3',image,'-c',verify],text=True,capture_output=True,check=True)
    item['persisted']=json.loads(result.stdout)
    if item['persisted']!={'records':args.rows,'sha256':digest.hexdigest()}:raise AssertionError('persisted contents differ')
    item['status']='PASS';save()
base=report['results']['baseline'];candidate=report['results']['candidate']
report['ratios']={'time':candidate['seconds']/base['seconds'],
                  'io':candidate['resources']['io_bytes']/base['resources']['io_bytes'],
                  'memory':candidate['resources']['memory_peak_bytes']/base['resources']['memory_peak_bytes']}
report['status']='PASS';save();print(json.dumps(report,indent=2))
