#!/usr/bin/env python3
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from evaluation.scoring import score
p=argparse.ArgumentParser(description='Compare two measured local reports using the published E formula; does not invent an official baseline.')
p.add_argument('--baseline',required=True,type=Path);p.add_argument('--candidate',required=True,type=Path);args=p.parse_args()
try:
 print(json.dumps(score(json.loads(args.baseline.read_text()),json.loads(args.candidate.read_text())),ensure_ascii=False,indent=2,allow_nan=False))
except (KeyError,ValueError,OSError) as error:
 print(str(error),file=sys.stderr);raise SystemExit(1)
