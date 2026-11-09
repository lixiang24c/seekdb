import json
import math
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minivectordb.core import Database, DBError
from minivectordb.sql import SQL


class RawStorageTests(unittest.TestCase):
    def test_scalar_projection_skips_vectors_and_keeps_ordering_and_visibility(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);sql=SQL(db)
            sql.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(4),v VECTOR(2))')
            sql.execute("INSERT INTO t VALUES (3,'中',[3,0]),(1,'文',[1,0]),(2,'中',[2,0])")
            read=db._read_record;flags=[]
            def record(source,scalar_only=False):
                flags.append(scalar_only);return read(source,scalar_only)
            with patch.object(db,'_read_record',side_effect=record):
                self.assertEqual(list(sql.execute("SELECT id,tag AS x FROM t WHERE tag='中' ORDER BY x DESC,id DESC")['rows']),[[3,'中'],[2,'中']])
                self.assertTrue(flags and all(flags));flags.clear()
                self.assertEqual(list(sql.execute('SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) DESC LIMIT 1')['rows']),[[3]])
                self.assertTrue(flags and not any(flags));flags.clear()
                self.assertEqual(list(sql.execute('SELECT v FROM t ORDER BY id LIMIT 1')['rows']),[[[1.,0.]]])
                self.assertTrue(flags and not any(flags))
            sql.execute('BEGIN');sql.execute('DELETE FROM t WHERE id=1')
            self.assertEqual(list(sql.execute('SELECT id FROM t ORDER BY id')['rows']),[[2],[3]])
            sql.execute('ROLLBACK')
            self.assertEqual(list(sql.execute('SELECT id FROM t ORDER BY id')['rows']),[[1],[2],[3]])
            db.close()

    def test_merge_and_index_preserve_float32_bits_unicode_and_offsets(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root)
            db.create('t',{'id':['BIGINT'],'文本':['VARCHAR',20],'向量':['VECTOR',4]},'id')
            rows=[{'id':i,'文本':'汉字\n😀'+str(i),'向量':[-0.0,1e-40,3e38,float(i)]} for i in (7,-9,2)]
            for row in rows:db.insert('t',[row])
            expected=list(db.rows('t'))
            with patch.object(db,'_read_record',side_effect=AssertionError('vector decoding')):
                db.create_vector_index('ix','t','向量')
            table=db.catalog['t'];index=table['index']
            stride=16+16;raw=(Path(root)/index['file']).read_bytes()
            with (Path(root)/table['file']).open('rb') as source:
                for position,row in enumerate(expected):
                    key,offset=struct.unpack_from('<qQ',raw,position*stride)
                    self.assertEqual(key,row['id']);source.seek(offset)
                    self.assertEqual(db._read_record(source),row)
                    self.assertEqual(raw[position*stride+16:(position+1)*stride],struct.pack('<4f',*row['向量']))
            self.assertEqual([json.loads(s) for s in (Path(root)/index['metadata']).read_text().splitlines()],
                             [{k:v for k,v in r.items() if k!='向量'} for r in expected])
            db.close();db=Database(root)
            actual=list(db.rows('t'));self.assertEqual(actual,expected)
            self.assertEqual(math.copysign(1,actual[0]['向量'][0]),-1)
            self.assertEqual([r['id'] for r,_ in db.topk('t','向量',[0,0,3e38,0],3)],[2,7,-9])
            db.close()

    def test_failed_merge_sync_preserves_transaction_and_retry(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);db.create('t',{'id':['BIGINT'],'v':['VECTOR',1]},'id');db.insert('t',[{'id':1,'v':[1.]}]);db.begin()
            db.insert('t',[{'id':2,'v':[2.]}])
            original=db._merge_rows
            def fail(table):
                with patch('minivectordb.core.os.fsync',side_effect=OSError('merge sync fault')):
                    return original(table)
            with patch.object(db,'_merge_rows',side_effect=fail):
                with self.assertRaises(OSError):db.create_vector_index('ix','t','v')
            self.assertEqual(list(db.rows('t')),[{'id':1,'v':[1.]},{'id':2,'v':[2.]}]);db.create_vector_index('ix','t','v');db.commit();db.close()
            db=Database(root);self.assertEqual(list(db.rows('t')),[{'id':1,'v':[1.]},{'id':2,'v':[2.]}]);db.close()

    def test_legacy_json_rows_can_merge_and_build_index(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);db.create('t',{'id':['BIGINT'],'v':['VECTOR',1]},'id')
            path=Path(root)/'table-legacy.jsonl';path.write_text('{"id":4,"v":[1.5]}\n')
            table=dict(db.catalog['t'],file=path.name,keys=db._keyfile([4]))
            db.begin();db.pending['t']=table;db.commit()
            db.insert('t',[{'id':8,'v':[2.5]}]);db.create_vector_index('ix','t','v')
            self.assertEqual([r['id'] for r,_ in db.topk('t','v',[2.5],2)],[8,4]);db.close()

    def test_chunked_keys_respect_embedded_footer_and_truncation(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);keys=list(range(-1100,1100))
            file,run=db._write(({'id':key} for key in reversed(keys)),key_column='id')
            self.assertEqual(list(db._filekeys(run)),keys)
            standalone=db._keyfile(iter(keys));self.assertEqual(list(db._filekeys(standalone)),keys)
            with (Path(root)/standalone).open('ab') as out:out.write(b'x')
            with self.assertRaises(DBError):list(db._filekeys(standalone))
            short=dict(run,key_count=len(keys)+1)
            with self.assertRaises(DBError):list(db._filekeys(short))
            db.close()


if __name__=='__main__':unittest.main()
