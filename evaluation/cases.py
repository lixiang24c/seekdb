"""Local black-box cases with independently specified expected outcomes."""
import random


def equal(actual, expected):
    if actual != expected:
        raise AssertionError(f'expected {expected!r}, got {actual!r}')


def run_cases(factory):
    def sql_types(c):
        c.execute('CREATE TABLE t(id BIGINT PRIMARY KEY, text VARCHAR(3), v VECTOR(2))')
        c.execute("INSERT INTO t(text,v,id) VALUES ('中🙂',[1,2],-9223372036854775808),('a''b',[0,0],9223372036854775807)")
        equal(c.execute('SELECT id,text FROM t ORDER BY id'),[[-9223372036854775808,'中🙂'],[9223372036854775807,"a'b"]])
        for text in ("INSERT INTO t VALUES (0,'太长字符串',[0,0])", "INSERT INTO t VALUES (9223372036854775808,'ok',[0,0])",
                     "INSERT INTO t VALUES (0,'ok',[0])", "INSERT INTO t VALUES (0,'ok',[1e40,0])",
                     "INSERT INTO t VALUES (0,'ok',[NaN,0])", "INSERT INTO t VALUES (0,'ok',[Infinity,0])"):
            c.execute(text,error=True)
        equal(len(c.execute('SELECT * FROM t')),2)
    def unicode_order(c):
        c.execute('CREATE TABLE t(id BIGINT PRIMARY KEY, text VARCHAR(3))')
        c.execute("INSERT INTO t VALUES (3,'😀'),(2,'中'),(1,'a'),(4,'中')")
        equal(c.execute("SELECT id FROM t WHERE text>='中' ORDER BY text DESC"),[[3],[2],[4]])
        equal(c.execute('SELECT id FROM t WHERE id>1 AND id<=3 ORDER BY id DESC LIMIT 1'),[[3]])
    def statement_atomicity(c):
        c.execute('CREATE TABLE t(id BIGINT PRIMARY KEY, v VECTOR(1))');c.execute('BEGIN')
        c.execute('INSERT INTO t VALUES (1,[1])')
        c.execute('INSERT INTO t VALUES (2,[2]),(1,[3])',error=True)
        c.execute('INSERT INTO t VALUES (2,[2]),(3,[1e99])',error=True)
        c.execute('UPDATE t SET v=[1,2]',error=True)
        c.execute('UPDATE t SET id=3',error=True)
        equal(c.execute('SELECT id FROM t'),[[1]])
        c.execute('INSERT INTO t VALUES (2,[2])');c.execute('COMMIT')
        c.restart()
        equal(c.execute('SELECT id FROM t ORDER BY id'),[[1],[2]])
    def filtering(c):
        c.execute('CREATE TABLE t(id BIGINT PRIMARY KEY, tag BIGINT, v VECTOR(2))')
        c.execute('INSERT INTO t VALUES (1,0,[0,0]),(2,1,[1,0]),(3,1,[-1,0]),(4,0,[0,0])')
        equal(c.execute('SELECT id FROM t WHERE tag=1 ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 10'),[[2],[3]])
        equal(c.execute('SELECT id FROM t WHERE tag=9 ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 10'),[])
        equal(c.execute('SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 0'),[])
        result=c.execute('SELECT id,L2_DISTANCE(v,[0,0]) AS d FROM t ORDER BY d LIMIT 10')
        equal(result,[[1,0.0],[4,0.0],[2,1.0],[3,1.0]])
    def index_transactions(c):
        c.execute('CREATE TABLE t(id BIGINT PRIMARY KEY, v VECTOR(1))')
        c.execute('INSERT INTO t VALUES (1,[1]),(2,[2])');c.execute('CREATE VECTOR INDEX ix ON t(v)')
        c.execute('BEGIN');c.execute('UPDATE t SET v=[0] WHERE id=2')
        c.execute('INSERT INTO t VALUES (3,[-1])');c.execute('DELETE FROM t WHERE id=1')
        equal(c.execute('SELECT id FROM t ORDER BY L2_DISTANCE(v,[0]) LIMIT 10'),[[2],[3]])
        c.execute('ROLLBACK')
        equal(c.execute('SELECT id FROM t ORDER BY L2_DISTANCE(v,[0]) LIMIT 10'),[[1],[2]])
        c.execute('UPDATE t SET v=[0] WHERE id=2');c.restart()
        equal(c.execute('SELECT id FROM t ORDER BY L2_DISTANCE(v,[0]) LIMIT 10'),[[2],[1]])
    def crash(c):
        c.execute('CREATE TABLE t(id BIGINT PRIMARY KEY)');c.execute('INSERT INTO t VALUES (1)')
        c.execute('BEGIN');c.execute('INSERT INTO t VALUES (2),(3)');c.execute('COMMIT')
        c.execute('BEGIN');c.execute('DELETE FROM t WHERE id=1');c.execute('INSERT INTO t VALUES (4)')
        c.restart(kill=True)
        equal(c.execute('SELECT id FROM t ORDER BY id'),[[1],[2],[3]])
        c.restart(kill=True)
        equal(c.execute('SELECT id FROM t ORDER BY id'),[[1],[2],[3]])
    def eof_rollback(c):
        c.execute('CREATE TABLE t(id BIGINT PRIMARY KEY)');c.execute('BEGIN');c.execute('INSERT INTO t VALUES (1)')
        c.restart();equal(c.execute('SELECT * FROM t'),[])
    def rejected_sql(c):
        c.execute('CREATE TABLE t(id BIGINT PRIMARY KEY, v VECTOR(1))')
        for sql in ('ALTER TABLE t ADD n BIGINT','SELECT COUNT(*) FROM t','SELECT * FROM t JOIN t',
                    'INSERT INTO t VALUES (1,[0]) trailing','SELECT * FROM t WHERE id=1 OR id=2',
                    'UPDATE t SET v=[2] WHERE unknown=1','BEGIN; INSERT INTO t VALUES (1,[0])'):
            # Multiple statements are valid in the transport, so test trailing unsupported tokens instead.
            if sql.startswith('BEGIN;'):continue
            c.execute(sql,error=True)
        equal(c.execute('SELECT * FROM t'),[])
    def random_model(c):
        c.execute('CREATE TABLE t(id BIGINT PRIMARY KEY, n BIGINT)');model={};saved=None;rng=random.Random(719)
        for step in range(80):
            action=rng.randrange(6);key=rng.randrange(15)
            if action==0:
                if key in model:c.execute(f'INSERT INTO t VALUES ({key},0)',error=True)
                else:c.execute(f'INSERT INTO t VALUES ({key},{step})');model[key]=step
            elif action==1:
                c.execute(f'DELETE FROM t WHERE id={key}');model.pop(key,None)
            elif action==2:
                c.execute(f'UPDATE t SET n={step} WHERE id={key}')
                if key in model:model[key]=step
            elif action==3 and saved is None:
                c.execute('BEGIN');saved=dict(model)
            elif action==4 and saved is not None:
                c.execute('ROLLBACK');model=saved;saved=None
            elif action==5 and saved is not None:
                c.execute('COMMIT');saved=None
            equal(c.execute('SELECT id,n FROM t ORDER BY id'),[[k,model[k]] for k in sorted(model)])
        if saved is not None:c.execute('COMMIT')
        c.restart();equal(c.execute('SELECT id,n FROM t ORDER BY id'),[[k,model[k]] for k in sorted(model)])
    for case in (sql_types,unicode_order,statement_atomicity,filtering,index_transactions,crash,eof_rollback,rejected_sql,random_model):
        factory(case.__name__,case)
