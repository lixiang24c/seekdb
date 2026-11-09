import gc,math,struct,tempfile,tracemalloc,unittest
from pathlib import Path

@unittest.skipUnless((Path(__file__).resolve().parents[1]/'minivectordb/libminivector.so').exists(),'native library not built')
class NativeCodecTests(unittest.TestCase):
    def test_rounding_and_rejection_match_independent_struct_reference(self):
        from minivectordb.libminivector import float32_vector
        values=[0.,-0.,1e-46,-1e-46,1e-45,16777217,3.4028234663852886e38,3.4028235e38,-3.4028235e38,1e39,float('nan'),float('inf'),-(1<<2000)]
        for value in values:
            try:
                expected=struct.unpack('<f',struct.pack('<f',value))[0]
                valid=math.isfinite(expected)
            except (OverflowError,struct.error):valid=False
            if valid:
                actual=float32_vector([value],1)[0]
                self.assertEqual(struct.pack('<f',actual),struct.pack('<f',expected))
            else:
                with self.assertRaises((ValueError,OverflowError)):float32_vector([value],1)
        class FloatSubclass(float):pass
        for value in (True,None,'1',FloatSubclass(1)):
            with self.assertRaises(ValueError):float32_vector([value],1)
        with self.assertRaises(ValueError):float32_vector([1.],2)
    def test_error_paths_do_not_retain_partial_python_lists(self):
        from minivectordb.libminivector import float32_vector
        bad=[1.]*127+[True]
        tracemalloc.start()
        try:
            for _ in range(20):
                try:float32_vector(bad,len(bad))
                except ValueError:pass
            gc.collect();before=tracemalloc.get_traced_memory()[0]
            for _ in range(1000):
                try:float32_vector(bad,len(bad))
                except ValueError:pass
            gc.collect();after=tracemalloc.get_traced_memory()[0]
            self.assertLess(after-before,32768)
        finally:tracemalloc.stop()
    def test_statement_validation_keeps_prior_transaction_changes(self):
        from minivectordb.core import Database,DBError
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);db.create('t',{'id':['BIGINT'],'v':['VECTOR',2]},'id')
            db.begin();db.insert('t',[{'id':1,'v':[0,0]}])
            with self.assertRaises(DBError):db.insert('t',[{'id':2,'v':[1,1]},{'id':3,'v':[True,0]}])
            db.commit();db.close();db=Database(root)
            self.assertEqual([r['id'] for r in db.rows('t')],[1]);db.close()
if __name__=='__main__':unittest.main()
