"""Local debugging adapter only; not the official SQL wire protocol."""
import argparse
import json
import sys
from .core import Database

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--data',default='/data')
args=parser.parse_args()
db=Database(args.data)
print('LOCAL_DEBUG_READY',flush=True)
try:
    for line in sys.stdin:
        try:
            request=json.loads(line)
            op=request['op']
            if op in ('begin','commit','rollback'):
                value=getattr(db,op)()
            elif op=='create':
                value=db.create(request['table'],request['columns'],request['primary'])
            elif op=='insert':
                value=db.insert(request['table'],request['rows'])
            elif op=='scan':
                # Stream rows so the debugging adapter does not materialize tables.
                for row in db.rows(request['table']):
                    print(json.dumps({'row':row},ensure_ascii=False,allow_nan=False),flush=True)
                value=None
            elif op=='topk':
                value=db.topk(request['table'],request['column'],request['query'],request['k'])
            elif op=='quit':
                break
            else:
                raise ValueError('unknown debug operation')
            print(json.dumps({'ok':True,'result':value},ensure_ascii=False,allow_nan=False),flush=True)
        except Exception as error:
            print(json.dumps({'ok':False,'error':str(error)},ensure_ascii=False),flush=True)
            if db.failed:
                break
finally:
    db.close()
