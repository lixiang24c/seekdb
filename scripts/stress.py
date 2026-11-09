#!/usr/bin/env python3
"""Generate and replay deterministic SQL suites with an independent reference model."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evaluation.stress_generate import generate
from evaluation.stress_run import run


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    gen=sub.add_parser('generate');gen.add_argument('--suite',type=Path,required=True)
    gen.add_argument('--public',type=Path,default=Path('mysqltest'))
    gen.add_argument('--operations',type=int,default=100000);gen.add_argument('--scenarios',type=int,default=128)
    gen.add_argument('--seed',type=int,default=20261109)
    replay=sub.add_parser('run');replay.add_argument('--suite',type=Path,required=True)
    replay.add_argument('--output',type=Path,required=True);replay.add_argument('--protocol',choices=('json','text'),default='json')
    replay.add_argument('--command-json',default=json.dumps([sys.executable,'-u','-m','minivectordb','--data','{data}']))
    replay.add_argument('--cwd');replay.add_argument('--case')
    replay.add_argument('--data-root',type=Path)
    args=parser.parse_args()
    if args.action=='generate':
        print(json.dumps(generate(args.suite,args.public,args.operations,args.scenarios,args.seed),ensure_ascii=False,indent=2));return 0
    command=json.loads(args.command_json)
    if not isinstance(command,list) or not command or not all(isinstance(part,str) for part in command):parser.error('command-json must be a nonempty string array')
    report=run(args.suite,args.output,command,args.protocol,args.cwd,args.case,args.data_root)
    print(json.dumps({key:report[key] for key in ('status','sql_attempted','sql_completed','sql_passed','sql_failed','restarts','elapsed_seconds')},indent=2))
    return 0 if report['status']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
