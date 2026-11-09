import math
import os
from unittest.mock import patch
from pathlib import Path
import unittest
import numpy as np

@unittest.skipUnless((Path(__file__).resolve().parents[1]/'minivectordb/libminivector.so').exists(),'native library not built locally')
class NativeKernelTests(unittest.TestCase):
    def setUp(self):
        environment=patch.dict(os.environ,{"MINIVECTOR_NATIVE":"1"})
        environment.start();self.addCleanup(environment.stop)
    def test_simd_and_scalar_against_independent_reference(self):
        from minivectordb.native import scan
        rng=np.random.default_rng(911)
        for dim in (1,3,4,7,128,131):
            dtype=np.dtype([('pk','<i8'),('offset','<u8'),('vector','<f4',(dim,))])
            data=np.zeros(2053,dtype=dtype)
            data['pk']=rng.permutation(len(data));data['pk'][0]=-(1<<63);data['pk'][1]=(1<<63)-1
            data['offset']=np.arange(len(data))*123
            data['vector']=rng.uniform(-2,2,(len(data),dim)).astype(np.float32)
            query=rng.uniform(-1,1,dim).astype(np.float32).astype(np.float64)
            for mask in (slice(None),np.arange(len(data))%3==0,np.zeros(len(data),dtype=bool)):
                selected=range(len(data)) if isinstance(mask,slice) else np.flatnonzero(mask)
                expected=sorted([(sum((float(x)-float(y))**2 for x,y in zip(data['vector'][i],query)),int(data['pk'][i]),int(data['offset'][i])) for i in selected],key=lambda r:(r[0],r[1]))[:10]
                for simd in (True,False):
                    actual=list(scan(data,query,mask,10,use_simd=simd))
                    self.assertEqual([(r[1],r[2]) for r in actual],[(r[1],r[2]) for r in expected])
                    for a,e in zip(actual,expected):self.assertTrue(math.isclose(a[0],e[0],rel_tol=1e-12,abs_tol=1e-12))
    def test_ties_empty_and_k_larger_than_count(self):
        from minivectordb.native import scan
        data=np.zeros(31,dtype=[('pk','<i8'),('offset','<u8'),('vector','<f4',(3,))]);data['pk']=np.arange(31)[::-1];data['vector']=1
        query=np.zeros(3,dtype=np.float64)
        self.assertEqual([r[1] for r in scan(data,query,slice(None),100)],list(range(31)))
        self.assertEqual(list(scan(data,query,slice(None),0)),[])
        self.assertEqual(list(scan(data[:0],query,slice(None),10)),[])
if __name__=='__main__':unittest.main()
