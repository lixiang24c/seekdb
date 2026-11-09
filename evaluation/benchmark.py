"""Reproducible local end-to-end benchmark; never emits an official score."""
import math
import time
import numpy as np


def vector_sql(vector):
    return '[' + ','.join(format(float(x), '.9g') for x in vector) + ']'


def prepare(seed, rows, dimension, queries):
    rng = np.random.default_rng(seed)
    vectors = rng.uniform(-1, 1, (rows,dimension)).astype(np.float32)
    probes = rng.uniform(-1, 1, (queries,dimension)).astype(np.float32)
    ids = np.arange(1, rows+1, dtype=np.int64)
    tags = ids % 10
    requests = []
    expected = []
    double = vectors.astype(np.float64)
    for index, query in enumerate(probes):
        filtered = index % 2 == 1
        tag = index % 10
        candidates = np.flatnonzero(tags == tag) if filtered else np.arange(rows)
        # Independent full-array oracle, never uses database storage/index/execution code.
        distance = np.linalg.norm(double[candidates] - query.astype(np.float64), axis=1)
        order = np.lexsort((ids[candidates], distance))[:10]
        expected.append([(int(ids[candidates[i]]), float(distance[i])) for i in order])
        condition = f' WHERE tag={tag}' if filtered else ''
        requests.append(f'SELECT id,L2_DISTANCE(v,{vector_sql(query)}) AS d FROM t{condition} ORDER BY d LIMIT 10')
    batches = []
    for start in range(0,rows,100):
        values = ','.join(f'({i+1},{int(tags[i])},{vector_sql(vectors[i])})' for i in range(start,min(start+100,rows)))
        batches.append('INSERT INTO t VALUES ' + values)
    return vectors, batches, requests, expected


def load(client, batches):
    client.execute('CREATE TABLE t(id BIGINT PRIMARY KEY, tag BIGINT, v VECTOR(128))')
    started = time.perf_counter()
    for batch in batches:
        client.execute(batch)
    return time.perf_counter()-started


def validate_records(client, vectors):
    actual = client.execute('SELECT id,tag,v FROM t ORDER BY id')
    if len(actual) != len(vectors):
        raise AssertionError('write row count mismatch')
    for i,(key,tag,vector) in enumerate(actual):
        if key!=i+1 or tag!=key%10 or not np.array_equal(np.asarray(vector,dtype=np.float32),vectors[i]):
            raise AssertionError('persisted row mismatch at '+str(i))


def run_round(session_factory, seed, rows, queries, number):
    vectors,batches,requests,truth = prepare(seed,rows,128,queries)
    writer = session_factory(f'benchmark-{number}-write')
    try:
        elapsed = load(writer, batches)
        write_metrics = writer.metrics()
        writer.restart()
        validate_records(writer,vectors)
        write_result = {'seconds':elapsed,'resources':write_metrics,'persistence_correct':True}
    finally:
        writer.close()
    query = session_factory(f'benchmark-{number}-query')
    try:
        load(query,batches)
        started=time.perf_counter()
        query.execute('CREATE VECTOR INDEX vector_index ON t(v)')
        preparation=time.perf_counter()-started
        for statement in requests[:min(10,len(requests))]:query.execute(statement)
        answers=[]
        started=time.perf_counter()
        for statement in requests:answers.append(query.execute(statement))
        elapsed=time.perf_counter()-started
        query_metrics=query.metrics()
        recalls={False:[],True:[]}
        for index,(answer,expected) in enumerate(zip(answers,truth)):
            wanted={key for key,d in expected};keys=[row[0] for row in answer]
            if len(keys)!=len(expected) or len(set(keys))!=len(keys):
                raise AssertionError('query count/duplicate mismatch')
            distances={key:distance for key,distance in expected}
            for key,distance in answer:
                if not 1<=key<=rows or (index%2 and key%10!=index%10):
                    raise AssertionError('query returned invalid/filtered-out key')
                if key in distances and not math.isclose(distance,distances[key],rel_tol=1e-6,abs_tol=1e-6):
                    raise AssertionError('query distance mismatch')
            recall=len(wanted.intersection(keys))/len(wanted) if wanted else None
            if recall is not None:recalls[bool(index%2)].append(recall)
            # exact mode requires exact ID order, not merely 0.95 recall.
            if keys!=[key for key,d in expected]:raise AssertionError('exact query differs from full-scan oracle')
        average={('filtered' if filtered else 'unfiltered'):sum(values)/len(values) if values else None for filtered,values in recalls.items()}
        if any(value is not None and value<.95 for value in average.values()):raise AssertionError('recall below threshold')
        return {'round':number,'seed':seed,'write':write_result,'query':{'seconds':elapsed,'index_prepare_seconds':preparation,
                'resources':query_metrics,'recall_at_10':average,'exact_ids_and_order_correct':True},'status':'PASS'}
    finally:
        query.close()
