"""Official JSONL transport, with an explicit legacy local SQL mode."""
import argparse,json,sys,signal,re,os,tempfile,shutil,math
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

def local_main(db,sql,args):
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



def strict_request(line):
    def pairs(items):
        value={}
        for key,item in items:
            if key in value:raise ValueError('duplicate request key')
            value[key]=item
        return value
    def invalid(value):raise ValueError('non-finite JSON number')
    def number(value):
        result=float(value)
        if not math.isfinite(result):raise ValueError('non-finite JSON number')
        return result
    value=json.loads(line,object_pairs_hook=pairs,parse_constant=invalid,parse_float=number)
    if not isinstance(value,dict):raise ValueError('request must be an object')
    return value


WIRE=json.JSONEncoder(ensure_ascii=True,allow_nan=False,separators=(',',':'))


def send_json(value,output):
    output.write(WIRE.encode(value)+'\n');output.flush()


def send_query(ident,result,output,data_dir):
    # Stage the complete response before publication: lazy SQL errors cannot
    # leave half a JSON response on stdout. Large results spill only to /data.
    with tempfile.SpooledTemporaryFile(max_size=65536,mode='w+',encoding='utf-8',dir=data_dir) as staged:
        staged.write('{"id":'+WIRE.encode(ident)+',"ok":true,"columns":'+WIRE.encode(result['columns'])+',"rows":[')
        first=True
        for row in result['rows']:
            if not first:staged.write(',')
            first=False
            for part in WIRE.iterencode(row):staged.write(part)
        staged.write(']}\n');staged.seek(0)
        shutil.copyfileobj(staged,output,length=65536)
    output.flush()


def jsonl_main(db,sql,source,output,default_mode):
    send_json({'type':'ready'},output)
    for line in source:
        ident=None
        try:
            request=strict_request(line);ident=request.get('id')
            if 'id' not in request:raise ValueError('missing request id')
            if request.get('op')=='shutdown':
                db.close()
                send_json({'id':ident,'ok':True},output)
                return
            if request.get('op')!='execute':raise ValueError('unsupported operation')
            if not isinstance(request.get('sql'),str):raise ValueError('sql must be a string')
            mode=request.get('search_mode',default_mode)
            if mode not in ('exact','ann'):raise ValueError('invalid search mode')
            # Both modes currently use exact search, including filtering.
            result=sql.execute(request['sql'])
            if isinstance(result,dict) and 'rows' in result:send_query(ident,result,output,db.root)
            else:
                reply={'id':ident,'ok':True}
                if isinstance(result,dict) and 'affected' in result:reply['affected_rows']=result['affected']
                send_json(reply,output)
        except Exception as error:
            send_json({'id':ident,'ok':False,'error':{'code':error_code(error),'message':str(error)}},output)
            if db.failed:return


def main():
    parser=argparse.ArgumentParser(description='MiniVectorDB official JSONL stdin/stdout server')
    parser.add_argument('--data',default=os.environ.get('VECTORDB_DATA_DIR','/data'))
    parser.add_argument('--mode',choices=('exact','ann'),default='exact')
    parser.add_argument('--protocol',choices=('jsonl','local'),default='jsonl')
    args=parser.parse_args()
    def terminate(signum,frame):raise SystemExit(0)
    signal.signal(signal.SIGTERM,terminate)
    db=Database(args.data);sql=SQL(db)
    try:
        if args.protocol=='local':local_main(db,sql,args)
        else:jsonl_main(db,sql,sys.stdin,sys.stdout,args.mode)
    finally:db.close()


if __name__=='__main__':main()
