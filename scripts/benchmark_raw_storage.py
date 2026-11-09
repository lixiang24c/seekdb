#!/usr/bin/env python3
"""Compare storage preparation operations against an explicit baseline core.py."""
import argparse
import hashlib
import importlib.util
import json
import struct
import sys
import tempfile
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import numpy as np
from minivectordb.core import Database
from minivectordb.native import available


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-source',type=Path,required=True)
    parser.add_argument('--data',type=Path,default=Path('/data'))
    parser.add_argument('--rows',type=int,default=10000)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    spec=importlib.util.spec_from_file_location('minivectordb.baseline_core',args.baseline_source)
    baseline=importlib.util.module_from_spec(spec);spec.loader.exec_module(baseline)
    available()  # Import and native library setup are outside operation timers.
    vectors=np.random.default_rng(43).normal(size=(args.rows,128)).astype(np.float32)
    expected=hashlib.sha256()
    for key,vector in enumerate(vectors):
        expected.update(struct.pack('<qq',key,key%10));expected.update(vector.tobytes())
    report={'scope':'single in-process operation comparison, not SQL end-to-end latency',
            'rows':args.rows,'dimension':128,'seed':43,'native':available(),
            'baseline_source_sha256':hashlib.sha256(args.baseline_source.read_bytes()).hexdigest(),
            'expected_content_sha256':expected.hexdigest(),'results':{}}
    args.data.mkdir(parents=True,exist_ok=True)
    def file_hash(path):
        with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
    for label,cls in [('baseline',baseline.Database),('candidate',Database)]:
        with tempfile.TemporaryDirectory(dir=args.data) as directory:
            db=cls(directory);db.create('t',{'id':['BIGINT'],'tag':['BIGINT'],'v':['VECTOR',128]},'id')
            for start in range(0,args.rows,1000):
                db.insert('t',({'id':i,'tag':i%10,'v':vectors[i].tolist()} for i in range(start,min(start+1000,args.rows))))
            table=db.catalog['t']
            wall=time.perf_counter();cpu=time.process_time()
            checksum=sum(row['id'] for row in db._rows_of(table,scalar_only=(label=='candidate')))
            scan={'scalar_scan_seconds':time.perf_counter()-wall,'scalar_scan_cpu_seconds':time.process_time()-cpu}
            assert checksum==args.rows*(args.rows-1)//2
            wall=time.perf_counter();cpu=time.process_time()
            key_checksum=sum(db._keys(table))
            scan.update(primary_scan_seconds=time.perf_counter()-wall,primary_scan_cpu_seconds=time.process_time()-cpu)
            assert key_checksum==checksum
            wall=time.perf_counter();cpu=time.process_time()
            merged=db._merge_rows(table) if hasattr(db,'_merge_rows') else db._write(db._rows_of(table))
            item={'merge_seconds':time.perf_counter()-wall,'merge_cpu_seconds':time.process_time()-cpu,
                  'merged_sha256':file_hash(Path(directory)/merged)}
            item.update(scan)
            wall=time.perf_counter();cpu=time.process_time()
            db.create_vector_index('ix','t','v')
            item.update(index_prepare_seconds=time.perf_counter()-wall,index_prepare_cpu_seconds=time.process_time()-cpu)
            index=db.catalog['t']['index']
            item['index_layout']=index.get('layout','flat')
            item['index_sha256']=file_hash(Path(directory)/index['file'])
            item['metadata_sha256']=file_hash(Path(directory)/index['metadata'])
            db.close();db=cls(directory);actual=hashlib.sha256();count=0
            for row in db.rows('t'):
                actual.update(struct.pack('<qq',row['id'],row['tag']));actual.update(struct.pack('<128f',*row['v']));count+=1
            assert count==args.rows and actual.hexdigest()==expected.hexdigest(),'reopened contents differ'
            item['reopened_records']=count;item['content_sha256']=actual.hexdigest()
            checks=0
            for query in vectors[:3]:
                squared=np.sum((vectors.astype(np.float64)-query.astype(np.float64))**2,axis=1)
                for tag in (None,2):
                    ids=np.arange(args.rows)
                    if tag is not None:ids=ids[ids%10==tag]
                    ordered=ids[np.lexsort((ids,squared[ids]))][:10]
                    actual_rows=db.topk('t','v',query.tolist(),10,lambda row:tag is None or row['tag']==tag)
                    assert [row['id'] for row,distance in actual_rows]==ordered.tolist(),'TopK differs'
                    assert np.allclose([distance for row,distance in actual_rows],np.sqrt(squared[ordered]),rtol=1e-12,atol=1e-12)
                    checks+=1
            item['exact_topk_checks']=checks
            before={path.name for path in Path(directory).iterdir()}
            wal=Path(directory)/'database.wal'
            item['mutation_wal_bytes_before']=wal.stat().st_size if wal.exists() else 0
            wall=time.perf_counter()
            from minivectordb.sql import SQL
            sql=SQL(db)
            db.modify('t',sql.predicate('t',[('id','=',args.rows-1)]),{'v':[0.]*128})
            db.modify('t',sql.predicate('t',[('id','=',0)]))
            item['mutation_seconds']=time.perf_counter()-wall
            item['mutation_retained_new_data_bytes']=sum(path.stat().st_size for path in Path(directory).iterdir() if path.name not in before and path.name!='database.wal')
            item['mutation_wal_bytes_after']=wal.stat().st_size if wal.exists() else 0
            assert db.topk('t','v',[0.]*128,1)[0][0]['id']==args.rows-1
            db.close();db=cls(directory);expected_after=hashlib.sha256();actual_after=hashlib.sha256()
            for key in range(1,args.rows):
                expected_after.update(struct.pack('<qq',key,key%10))
                expected_after.update(vectors[key].tobytes() if key!=args.rows-1 else bytes(128*4))
            # Versioned scans are unordered; compare in primary-key order with a bounded external sort.
            from minivectordb.sql import SQL
            ordered=SQL(db).external_sort(db.rows('t'),lambda row:row['id'])
            for row in ordered:
                actual_after.update(struct.pack('<qq',row['id'],row['tag']));actual_after.update(struct.pack('<128f',*row['v']))
            assert actual_after.hexdigest()==expected_after.hexdigest(),'mutated contents differ'
            item['mutated_content_sha256']=actual_after.hexdigest()
            item['status']='PASS';db.close()
            report['results'][label]=item
    old=report['results']['baseline'];new=report['results']['candidate']
    for key in ('merged_sha256','metadata_sha256'):assert old[key]==new[key],key+' differs'
    if old['index_layout']==new['index_layout']:assert old['index_sha256']==new['index_sha256'],'index differs'
    report['ratios']={key:new[key]/old[key] for key in ('merge_seconds','merge_cpu_seconds','index_prepare_seconds','index_prepare_cpu_seconds','scalar_scan_seconds','scalar_scan_cpu_seconds','primary_scan_seconds','primary_scan_cpu_seconds','mutation_seconds','mutation_retained_new_data_bytes')}
    report['status']='PASS'
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
