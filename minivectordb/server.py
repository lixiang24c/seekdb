"""Local SQL transport: semicolon terminated SQL, tabular JSON rows on stdout.
Official transport can adapt SQL.execute without changing storage semantics.
"""
import argparse,json,sys,signal,re
from .core import Database
from .sql import SQL

# Scan complete quoted regions and delimiters in C rather than visiting every
# character in Python. Incomplete quotes/comments remain buffered across lines.
BOUNDARY=re.compile(r"'(?:''|[^'])*'|`[^`]*`|--[^\n]*(?:\n|$)|/\*.*?\*/|['`]|/\*|;",re.S)

def statements(source):
    buffer=''
    for line in source:
        buffer+=line
        start=0
        for match in BOUNDARY.finditer(buffer):
            token=match.group()
            if token in ("'",'`','/*'):
                break
            if token==';':
                yield buffer[start:match.start()]
                start=match.end()
        buffer=buffer[start:]
    if buffer.strip():yield buffer

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--data',default='/data');parser.add_argument('--mode',choices=('exact','ann'),default='exact');args=parser.parse_args()
    def terminate(signum,frame):raise SystemExit(0)
    signal.signal(signal.SIGTERM,terminate)
    db=Database(args.data);sql=SQL(db)
    print('LOCAL_SQL_READY',flush=True)
    try:
        for statement in statements(sys.stdin):
            if not statement.strip():continue
            try:
                result=sql.execute(statement)
                if result and 'rows' in result:
                    print(json.dumps({'columns':result['columns']},ensure_ascii=False),flush=True)
                    for row in result['rows']:print(json.dumps({'row':row},ensure_ascii=False,allow_nan=False),flush=True)
                    print('{"ok":true}',flush=True)
                else:print(json.dumps({'ok':True,'result':result},ensure_ascii=False),flush=True)
            except Exception as error:
                print(json.dumps({'ok':False,'error':str(error)},ensure_ascii=False),flush=True)
                if db.failed:break
    finally:db.close()
if __name__=='__main__':main()
