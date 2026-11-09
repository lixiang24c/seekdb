"""Local SQL transport: semicolon terminated SQL, tabular JSON rows on stdout.
Official transport can adapt SQL.execute without changing storage semantics.
"""
import argparse,json,sys,signal
from .core import Database
from .sql import SQL

def statements(source):
    buffer=[];quoted=False;backtick=False;comment=False;block=False
    for line in source:
        i=0
        while i<len(line):
            c=line[i];pair=line[i:i+2]
            if block:
                buffer.append(c)
                if pair=='*/':buffer.append('/');i+=1;block=False
            elif comment:
                buffer.append(c)
                if c=='\n':comment=False
            elif not quoted and not backtick and pair=='/*':
                block=True;buffer.extend('/*');i+=1
            elif not quoted and not backtick and pair=='--':
                comment=True;buffer.extend('--');i+=1
            elif c=="'" and not backtick:
                buffer.append(c)
                if quoted and line[i+1:i+2]=="'":buffer.append("'");i+=1
                else:quoted=not quoted
            elif c=='`' and not quoted:backtick=not backtick;buffer.append(c)
            elif c==';' and not quoted and not backtick:
                yield ''.join(buffer);buffer=[]
            else:buffer.append(c)
            i+=1
    remainder=''.join(buffer)
    if quoted or backtick or block:
        # Pass incomplete text to the parser so it produces a visible error.
        yield remainder
    elif remainder.strip():yield remainder

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
