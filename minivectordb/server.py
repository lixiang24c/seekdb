"""Local SQL transport: semicolon terminated SQL, tabular JSON rows on stdout.
Official transport can adapt SQL.execute without changing storage semantics.
"""
import argparse,json,sys,signal,re
from .core import Database,DBError
from .sql import SQL

# Scan complete quoted regions and delimiters in C rather than visiting every
# character in Python. Incomplete quotes/comments remain buffered across lines.
BOUNDARY=re.compile(r"'(?:''|[^'])*'|`[^`]*`|--[^\n]*(?:\n|$)|/\*.*?\*/|['`]|/\*|;",re.S)
ENCODE=json.JSONEncoder(ensure_ascii=False,allow_nan=False).encode

def statements(source):
    buffer=''
    for line in source:
        directive=line.strip().rstrip(';').strip()
        if not buffer.strip() and directive in ('--mode exact','--mode ann'):
            yield directive
            continue
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

def write_result(result, output, chunk_chars=65536):
    """Write a small reply in one batch; stream large replies in bounded chunks."""
    def lines():
        if result and 'rows' in result:
            yield '{"columns": '+ENCODE(result['columns'])+'}\n'
            for row in result['rows']:
                yield '{"row": '+ENCODE(row)+'}\n'
            yield '{"ok":true}\n'
        else:
            yield '{"ok": true, "result": '+ENCODE(result)+'}\n'
    parts=[];size=0
    for line in lines():
        parts.append(line);size+=len(line)
        if size>=chunk_chars:
            output.write(''.join(parts));output.flush();parts=[];size=0
    if parts:
        output.write(''.join(parts));output.flush()

def error_code(error):
    message=str(error)
    if 'duplicate primary key' in message:return 'duplicate_key'
    if 'unsupported' in message:return 'unsupported_sql'
    if isinstance(error,OSError):return 'io_error'
    if 'transaction' in message:return 'invalid_transaction'
    if 'float32' in message or 'vector' in message:return 'invalid_vector'
    if 'unknown' in message:return 'unknown_identifier'
    return 'invalid_statement' if isinstance(error,(DBError,ValueError)) else 'execution_error'

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
                if statement in ('--mode exact','--mode ann'):
                    args.mode=statement.split()[1]
                    write_result({'mode':args.mode},sys.stdout)
                    continue
                result=sql.execute(statement)
                write_result(result,sys.stdout)
            except Exception as error:
                print(json.dumps({'ok':False,'error':str(error),'error_code':error_code(error)},ensure_ascii=False),flush=True)
                if db.failed:break
    finally:db.close()
if __name__=='__main__':main()
