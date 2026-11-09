"""Load public SQL assertions and separate driver directives from SQL counts."""
import json
from pathlib import Path


def steps(root,case):
    root=Path(root)
    sql_path=root/case.get('test','cases/'+case['id']+'.test')
    result_path=root/case.get('result','cases/'+case['id']+'.result')
    expected=json.loads(result_path.read_text(encoding='utf-8'));index=0;result=[]
    for line in sql_path.read_text(encoding='utf-8').splitlines():
        text=line.strip()
        if not text:continue
        if text=='--restart':result.append({'kind':'restart','kill':False});continue
        if text.startswith('--mode '):
            mode=text.split()[1]
            if mode not in ('exact','ann') or len(text.split())!=2:raise ValueError('invalid mode directive')
            result.append({'kind':'mode','mode':mode});continue
        if text.startswith('--'):raise ValueError('unknown public driver directive: '+text)
        if index>=len(expected):raise ValueError('more SQL than assertions')
        result.append({'kind':'sql','sql':text.rstrip(';'),'expected':expected[index],'category':'official'})
        index+=1
    if index!=case['statements'] or index!=len(expected):raise ValueError('case/manifest statement count mismatch')
    return result
