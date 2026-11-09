#!/usr/bin/env python3
"""Alternate two immutable images to reduce time-dependent benchmark bias."""
import argparse,json,shutil,sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.evaluate import Session
from evaluation.benchmark import run_round
from evaluation.scoring import score


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',required=True);parser.add_argument('--candidate',required=True)
    parser.add_argument('--output',required=True,type=Path);args=parser.parse_args()
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=False)
    reports={name:{'status':'RUNNING','scope':'local paired benchmark, functional gates verified separately',
                   'configuration':{'rows':10000,'queries':1000,'memory':'128m','docker':True},
                   'image':image,'benchmark':[]} for name,image in [('baseline',args.baseline),('candidate',args.candidate)]}
    try:
        for number in range(1,4):
            order=('baseline','candidate') if number%2 else ('candidate','baseline')
            for name in order:
                print(f'round {number}: {name}',flush=True)
                settings=SimpleNamespace(docker=True,image=reports[name]['image'],memory='128m',timeout=30)
                directory=output/name;directory.mkdir(exist_ok=True)
                result=run_round(lambda label:Session(settings,directory/label),719+number,10000,1000,number)
                reports[name]['benchmark'].append(result)
                (output/(name+'.json')).write_text(json.dumps(reports[name],indent=2)+'\n')
                for track in ('write','query'):shutil.rmtree(directory/f'benchmark-{number}-{track}'/'data')
        for name,report in reports.items():
            report['status']='PASS'
            (output/(name+'.json')).write_text(json.dumps(report,indent=2)+'\n')
        (output/'comparison.json').write_text(json.dumps(score(reports['baseline'],reports['candidate']),indent=2)+'\n')
        print(str(output/'comparison.json'))
        return 0
    except BaseException:
        for name,report in reports.items():
            report['status']='FAIL'
            (output/(name+'.json')).write_text(json.dumps(report,indent=2)+'\n')
        raise
if __name__=='__main__':raise SystemExit(main())
