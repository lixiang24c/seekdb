"""Performance formula from the supplied rules. Inputs must be measured reports."""
from decimal import Decimal, ROUND_HALF_UP
import statistics
import math

EPSILON={'seconds':.001,'io_bytes':1048576,'memory_peak_bytes':1048576}


def metrics(round_result,track):
    result=round_result[track]
    resources=result['resources']
    if resources is None:raise ValueError('cgroup metrics unavailable; cannot substitute zero')
    values={'seconds':result['seconds'],'io_bytes':resources['io_bytes'],'memory_peak_bytes':resources['memory_peak_bytes']}
    if any(type(v) not in (int,float) or not math.isfinite(v) or v<0 for v in values.values()):raise ValueError('invalid measurement')
    return values


def score(baseline,candidate):
    if baseline.get('status')!='PASS' or candidate.get('status')!='PASS':raise ValueError('both local evaluations must pass')
    for key in ('rows','queries','memory','docker'):
        if baseline['configuration'].get(key)!=candidate['configuration'].get(key):raise ValueError('different configuration: '+key)
    base=baseline.get('benchmark',[]);runs=candidate.get('benchmark',[])
    if len(base)!=3 or len(runs)!=3:raise ValueError('exactly three valid benchmark rounds required')
    medians={track:{key:statistics.median(metrics(r,track)[key] for r in base) for key in EPSILON} for track in ('write','query')}
    values=[]
    for run in runs:
        if run.get('status')!='PASS':raise ValueError('invalid benchmark round')
        recalls=run['query']['recall_at_10']
        if any(recalls.get(group) is None or recalls[group]<.95 for group in ('filtered','unfiltered')):raise ValueError('invalid recall')
        efficiency={}
        for track in ('write','query'):
            measured=metrics(run,track)
            normalized={}
            for key,epsilon in EPSILON.items():
                ratio=(medians[track][key]+epsilon)/(measured[key]+epsilon)
                normalized[key]=ratio/(1+ratio)
            efficiency[track]=.6*normalized['seconds']+.2*normalized['io_bytes']+.2*normalized['memory_peak_bytes']
        values.append((efficiency['write']+2*efficiency['query'])/3)
    median=statistics.median(values)
    return {'scope':'local comparison only; not an official rank or score','baseline_medians':medians,
            'round_E':values,'median_E':median,'ranking_E':str(Decimal(str(median)).quantize(Decimal('.000001'),rounding=ROUND_HALF_UP))}
