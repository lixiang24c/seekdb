import ctypes
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
from minivectordb import native
from minivectordb.core import Database
from minivectordb.sql import SQL


@unittest.skipUnless(native.available(),'native library not built')
class QuantizedScreenTests(unittest.TestCase):
    def check_vectors(self,vectors,query):
        count,dim=vectors.shape
        rows=np.zeros(count,dtype=[('pk','<i8'),('offset','<u8'),('vector','<f4',(dim,))])
        rows['pk']=np.arange(count)[::-1];rows['offset']=np.arange(count)*113;rows['vector']=vectors
        codes=np.empty((count,dim),dtype=np.uint8);minimum=ctypes.c_double();step=ctypes.c_double()
        valid=native._library.mv_quantize(rows.ctypes.data,count,rows.strides[0],dim,
                                         ctypes.byref(minimum),ctypes.byref(step),codes.ctypes.data)
        self.assertTrue(valid)
        quantization=(codes,minimum.value,step.value)
        query=np.asarray(query,dtype=np.float64)
        for mask in (slice(None),(np.arange(count)%3!=0).astype(np.uint8),np.arange(0,count,2,dtype=np.uint32),np.arange(0,count,100,dtype=np.uint32)):
            selected=np.arange(count) if isinstance(mask,slice) else mask if mask.dtype==np.uint32 else np.flatnonzero(mask)
            delta=vectors[selected].astype(np.float64)-query
            distance=np.einsum('ij,ij->i',delta,delta)
            order=np.lexsort((rows['pk'][selected],distance))
            for simd in (False,True):
                for k in (0,1,10,count+1):
                    actual=list(native.scan(rows,query,mask,k,use_simd=simd,quantization=quantization))
                    expected=selected[order[:k]]
                    self.assertEqual([r[1] for r in actual],rows['pk'][expected].tolist())
                    np.testing.assert_allclose([r[0] for r in actual],distance[order[:k]],rtol=1e-12,atol=0)

    def test_uniform_odd_dimensions_and_out_of_range_queries(self):
        rng=np.random.default_rng(720)
        for dim in (32,128,131):
            vectors=rng.uniform(-1,1,(1200,dim)).astype(np.float32)
            self.check_vectors(vectors,rng.uniform(-1,1,dim))
        self.check_vectors(vectors,np.full(131,100.))

    def test_extreme_values_subnormals_and_exact_ties(self):
        rng=np.random.default_rng(721)
        vectors=rng.integers(-8,9,(1200,32)).astype(np.float32)
        vectors[-1]=np.finfo(np.float32).max
        vectors[-2]=-np.finfo(np.float32).max
        self.check_vectors(vectors,np.zeros(32))
        tiny=np.nextafter(np.float32(0),np.float32(1))
        self.check_vectors(rng.integers(-8,9,(1200,32)).astype(np.float32)*tiny,np.zeros(32))
        vectors=np.zeros((1200,32),dtype=np.float32);vectors[-1]=1
        self.check_vectors(vectors,np.zeros(32))

    @patch.object(Database,'INDEX_LAYOUT','flat')
    def test_sidecar_restart_mutation_rollback_and_cache_budget(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);sql=SQL(db)
            sql.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(32))')
            db.insert('t',({'id':i,'v':[float(i%17)]*32} for i in range(1200)))
            sql.execute('CREATE VECTOR INDEX ix ON t(v)')
            original=db.catalog['t']['index']['quantized']
            db.close();db=Database(root);sql=SQL(db)
            query='SELECT id FROM t ORDER BY L2_DISTANCE(v,'+json.dumps([0]*32)+') LIMIT 2'
            self.assertEqual(list(sql.execute(query)['rows']),[[0],[17]])
            self.assertLessEqual(db._scan_map_bytes,db.SCAN_MAP_LIMIT)
            db.begin();sql.execute('DELETE FROM t WHERE id=0')
            changed=db.catalog['t']['index']['quantized'];self.assertEqual(changed,original)
            self.assertTrue(db.catalog['t']['changes'])
            self.assertEqual(list(sql.execute(query)['rows']),[[17],[34]])
            db.rollback();self.assertEqual(list(sql.execute(query)['rows']),[[0],[17]])
            self.assertTrue((Path(root)/original).exists())
            sql.execute('UPDATE t SET v='+json.dumps([10]*32)+' WHERE id=0')
            self.assertTrue((Path(root)/original).exists())
            self.assertEqual(list(sql.execute(query)['rows']),[[17],[34]])
            for key in range(1,8):sql.execute('UPDATE t SET v='+json.dumps([10]*32)+' WHERE id='+str(key))
            self.assertFalse((Path(root)/original).exists())
            with patch('minivectordb.native.available',return_value=False):
                self.assertEqual(list(sql.execute(query)['rows']),[[17],[34]])
            db.close()

    def test_constant_and_large_indexes_skip_sidecars(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);sql=SQL(db)
            sql.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(32))')
            db.insert('t',({'id':i,'v':[0.]*32} for i in range(600)))
            sql.execute('CREATE VECTOR INDEX ix ON t(v)')
            self.assertNotIn('quantized',db.catalog['t']['index'])
            self.assertEqual(list(Path(root).glob('*.q8')),[])
            db.SCAN_MAP_LIMIT=1
            sql.execute('UPDATE t SET v='+json.dumps([1]*32)+' WHERE id=1')
            self.assertNotIn('quantized',db.catalog['t']['index']);db.close()

if __name__=='__main__':unittest.main()
