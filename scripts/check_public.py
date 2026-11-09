#!/usr/bin/env python3
"""Run the upstream public assertions and driver directives over the local adapter."""
import argparse
import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evaluation.public_cases import steps
from evaluation.stress_run import Process,check


def run_case(root,case,data_root):
    records=steps(root,case);counts={'statements':0,'restarts':0,'mode_switches':0,'errors_checked':0}
    with tempfile.TemporaryDirectory(prefix=case['id']+'-',dir=data_root) as directory:
        process=Process([sys.executable,'-u','-m','minivectordb','--data','{data}'],directory,sys.stderr)
        try:
            for record in records:
                if record['kind']=='restart':
                    process.restart();counts['restarts']+=1
                elif record['kind']=='mode':
                    process.set_mode(record['mode']);counts['mode_switches']+=1
                else:
                    counts['statements']+=1
                    actual=process.execute(record['sql'])
                    try:check(actual,record['expected'])
                    except AssertionError as error:
                        raise AssertionError(f"{case['id']} SQL {counts['statements']}: {record['sql']}\n{error}; actual={actual!r}") from error
                    counts['errors_checked']+=bool(record['expected'].get('error'))
        finally:process.stop()
    return counts


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases',type=Path,default=Path('mysqltest'))
    parser.add_argument('--data-root',type=Path,default=Path('/data'))
    parser.add_argument('--case');parser.add_argument('--report',type=Path)
    args=parser.parse_args();args.data_root.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((args.cases/'manifest.json').read_text(encoding='utf-8'))
    cases=[case for case in manifest['cases'] if not args.case or args.case==case['id']]
    if not cases:parser.error('unknown case')
    report={'scope':'upstream public SQL and directives over local process adapter',
            'version':manifest['version'],'error_code_policy':'nonempty code; semantic mapping not frozen',
            'cases':[],'status':'PASS'}
    for case in cases:
        try:
            counts=run_case(args.cases,case,args.data_root)
            report['cases'].append({'id':case['id'],**counts,'status':'PASS'})
        except Exception as error:
            report['status']='FAIL';report['cases'].append({'id':case['id'],'status':'FAIL','error':str(error)})
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['status']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
