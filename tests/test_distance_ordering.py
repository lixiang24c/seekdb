"""Distance ordering uses the stored values, not a floating-point tie tolerance."""
import json
import ctypes
import tempfile
import unittest
from fractions import Fraction
from unittest.mock import patch

import numpy as np

from minivectordb import native
from minivectordb.core import Database
from minivectordb.sql import SQL


def vectors(dimension):
    return ([100000000., 1., 1., 1., 0., 0., 0., 0.] + [0.] * (dimension - 8),
            [100000000., 1., 1., 0., 1., 0., 0., 0.] + [0.] * (dimension - 8))


def truth(vector, query):
    return sum((Fraction(float(a)) - Fraction(float(b))) ** 2
               for a, b in zip(vector, query))


@unittest.skipUnless(native.available(), 'native library not built')
class NativeDistanceOrderingTests(unittest.TestCase):
    def check(self, values, query):
        dimension = len(query)
        entries = np.zeros(len(values), dtype=[('pk', '<i8'), ('offset', '<u8'),
                                               ('vector', '<f4', (dimension,))])
        entries['pk'] = np.arange(len(values)) + 1
        entries['offset'] = np.arange(len(values)) * 113
        entries['vector'] = values
        expected = sorted(range(len(values)), key=lambda i: (truth(entries['vector'][i], query), i))
        for order in (np.arange(len(values)), np.arange(len(values))[::-1]):
            ordered = entries[order].copy()
            for simd in (False, True):
                for mask in (slice(None), np.ones(len(values), dtype=np.uint8),
                             np.arange(len(values), dtype=np.uint32)):
                    for k in (1, len(values)):
                        with self.subTest(dimension=dimension, simd=simd, k=k, order=order.tolist()):
                            actual = list(native.scan(ordered, np.asarray(query, dtype=np.float64), mask, k, use_simd=simd))
                            self.assertEqual([hit[1] for hit in actual], [i + 1 for i in expected[:k]])

    def test_permuted_equal_distances(self):
        for dimension in (8, 128, 131):
            first, second = vectors(dimension)
            self.assertEqual(truth(first, [0.] * dimension), truth(second, [0.] * dimension))
            self.check([first, second], [0.] * dimension)

    def test_distinct_distances_that_round_to_the_same_double(self):
        for dimension in (8, 128):
            farther = [100000000., 1.] + [0.] * (dimension - 2)
            nearer = [100000000.] + [0.] * (dimension - 1)
            self.assertGreater(truth(farther, [0.] * dimension), truth(nearer, [0.] * dimension))
            self.check([farther, nearer], [0.] * dimension)

    def test_extreme_float32_and_double_query(self):
        maximum = float(np.finfo(np.float32).max)
        tiny = float(np.nextafter(np.float32(0), np.float32(1)))
        self.check([[maximum, tiny, 0., 0.], [maximum, 0., tiny, 0.],
                    [maximum, 0., 0., 0.]], [0.] * 4)
        first, second = vectors(8)
        self.check([first, second], [0.1] * 8)
        self.check([first, second], [float(np.nextafter(0., 1.))] * 8)

    def test_quantized_screen_keeps_equal_distance_boundary(self):
        dimension = 32
        first, second = vectors(dimension)
        entries = np.zeros(1200, dtype=[('pk', '<i8'), ('offset', '<u8'), ('vector', '<f4', (dimension,))])
        entries['pk'] = np.arange(len(entries)) + 1
        entries['vector'][:] = 200000000.
        entries['vector'][0] = first; entries['vector'][1] = second
        codes = np.empty((len(entries), dimension), dtype=np.uint8)
        minimum = ctypes.c_double(); step = ctypes.c_double()
        self.assertTrue(native._library.mv_quantize(entries.ctypes.data, len(entries), entries.strides[0],
            dimension, ctypes.byref(minimum), ctypes.byref(step), codes.ctypes.data))
        for simd in (False, True):
            for mask in (slice(None), np.ones(len(entries), dtype=np.uint8), np.arange(600, dtype=np.uint32)):
                for k in (1, 2):
                    actual = list(native.scan(entries, np.zeros(dimension), mask, k, use_simd=simd,
                                              quantization=(codes, minimum.value, step.value)))
                    self.assertEqual([hit[1] for hit in actual], [1, 2][:k])

    def test_permutations_against_fraction_oracle(self):
        rng = np.random.default_rng(417)
        original = np.asarray([1e30, -1e30, 1e8, -1e8, 1., -1., 1e-30, -1e-30], dtype=np.float32)
        self.check([rng.permutation(original).tolist() for _ in range(12)], [0.1] * 8)


class DatabaseDistanceOrderingTests(unittest.TestCase):
    def query(self, sql, dimension, *, suffix='LIMIT 2', where=''):
        return list(sql.execute('SELECT id FROM t ' + where + ' ORDER BY L2_DISTANCE(v,\'' +
                                json.dumps([0] * dimension) + "') " + suffix)['rows'])

    def test_index_restart_filters_and_transaction_delta(self):
        for dimension in (8, 128):
            with self.subTest(dimension=dimension), tempfile.TemporaryDirectory() as root:
                first, second = vectors(dimension)
                db = Database(root); sql = SQL(db)
                try:
                    sql.execute(f'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR({dimension}))')
                    db.insert('t', [{'id': 1, 'v': first}, {'id': 2, 'v': second}] +
                              [{'id': i, 'v': [200000000.] * dimension} for i in range(3, 103)])
                    self.assertEqual(self.query(sql, dimension), [[1], [2]])
                    sql.execute('CREATE VECTOR INDEX ix ON t(v)')
                    self.assertEqual(self.query(sql, dimension), [[1], [2]])
                    self.assertEqual(self.query(sql, dimension, where='WHERE id >= 1', suffix='LIMIT 1'), [[1]])
                    db.begin()
                    db.modify('t', lambda row: row['id'] == 1, {'v': second})
                    self.assertTrue(db.catalog['t'].get('changes'))
                    self.assertEqual(self.query(sql, dimension), [[1], [2]])
                    db.rollback()
                    self.assertEqual(self.query(sql, dimension), [[1], [2]])
                    db.begin(); db.modify('t', lambda row: row['id'] == 1)
                    self.assertEqual(self.query(sql, dimension, suffix='LIMIT 1'), [[2]])
                    db.rollback(); db.close()
                    db = Database(root); sql = SQL(db)
                    self.assertEqual(self.query(sql, dimension), [[1], [2]])
                finally:
                    db.close()

    def test_chunk_merge_and_numpy_fallback_keep_true_order(self):
        with tempfile.TemporaryDirectory() as root:
            dimension = 128
            first, second = vectors(dimension)
            db = Database(root); sql = SQL(db)
            try:
                sql.execute(f'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR({dimension}))')
                db.insert('t', [{'id': 1, 'v': first}] +
                          [{'id': i + 3, 'v': [200000000.] * dimension} for i in range(2048)] +
                          [{'id': 2, 'v': second}])
                sql.execute('CREATE VECTOR INDEX ix ON t(v)')
                db.WHOLE_MASK_ROWS = 0  # Force the real bounded block/merge path.
                self.assertEqual(self.query(sql, dimension, where='WHERE id > 0', suffix='LIMIT 1'), [[1]])
                with patch('minivectordb.native.available', return_value=False):
                    self.assertEqual(self.query(sql, dimension, where='WHERE id > 0', suffix='LIMIT 1'), [[1]])
            finally:
                db.close()

    def test_near_unequal_distances_in_all_sql_ordering_paths(self):
        with tempfile.TemporaryDirectory() as root:
            db = Database(root); sql = SQL(db)
            try:
                sql.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(8))')
                db.insert('t', [{'id': 1, 'v': [100000000., 1.] + [0.] * 6},
                                {'id': 2, 'v': [100000000.] + [0.] * 7}])
                for indexed in (False, True):
                    if indexed: sql.execute('CREATE VECTOR INDEX ix ON t(v)')
                    self.assertEqual(self.query(sql, 8, suffix='LIMIT 1'), [[2]])
                    self.assertEqual(self.query(sql, 8, suffix=''), [[2], [1]])
                    self.assertEqual(self.query(sql, 8, suffix='DESC LIMIT 2'), [[1], [2]])
                    with patch('minivectordb.native.available', return_value=False):
                        self.assertEqual(self.query(sql, 8, suffix='LIMIT 1'), [[2]])
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
