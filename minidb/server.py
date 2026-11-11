"""Official JSONL handshake/execute/shutdown flow with the database connected."""
import json
import os
import sys

from .engine import Database, QueryResult
from .sql import DBError
from .storage import strict_json


def emit(message):
    print(json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(',', ':')), flush=True)


def emit_query(ident, result):
    # Query execution finishes before the first response byte. Stream only serialization.
    try:
        head = json.dumps({'id': ident, 'ok': True, 'columns': result.columns},
                          ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        if result.path is None:
            sys.stdout.write(head[:-1] + ',"rows":[' + ','.join(result.encoded_rows) + ']}\n')
            sys.stdout.flush()
            return
        sys.stdout.write(head[:-1] + ',"rows":[')
        first = True
        with open(result.path, encoding='utf-8') as f:
            for line in f:
                if not first: sys.stdout.write(',')
                sys.stdout.write(line.rstrip('\n'))
                first = False
        sys.stdout.write(']}\n')
        sys.stdout.flush()
    finally:
        if result.path is not None: result.path.unlink(missing_ok=True)


def run():
    data = sys.argv[1] if len(sys.argv) == 2 else '/data'
    db = Database(data)
    emit({'type': 'ready'})
    for line in sys.stdin:
        ident = None
        try:
            req = strict_json(line)
            if not isinstance(req, dict):
                raise ValueError('request must be an object')
            ident = req.get('id')
            op = req.get('op')
            if op == 'shutdown':
                db.close()
                emit({'id': ident, 'ok': True})
                return
            if op != 'execute' or not isinstance(req.get('sql'), str):
                raise ValueError('expected execute SQL or shutdown request')
            result = db.execute(req['sql'], req.get('search_mode', 'exact'))
        except DBError as exc:
            emit({'id': ident, 'ok': False, 'error': {'code': exc.code, 'message': str(exc)}})
            continue
        except (ValueError, UnicodeError) as exc:
            emit({'id': ident, 'ok': False, 'error': {'code': 'invalid_request', 'message': str(exc)}})
            continue
        if isinstance(result, QueryResult):
            emit_query(ident, result)
        else:
            emit({**result, 'id': ident, 'ok': True})
    db.close()


def main():
    try:
        run()
    except Exception as exc:
        # An I/O failure can follow the commit decision: stop instead of serving stale state.
        print(f'MiniVectorDB: {type(exc).__name__}: {exc}; uid={os.getuid()} gid={os.getgid()}', file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
