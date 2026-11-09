"""Small local .test/.result reader. Supported directives are explicitly local."""
import json


def run_fixture(client, path):
    actual=[];buffer=[];expect_error=False;sorted_result=False
    for number,line in enumerate(path.read_text().splitlines(),1):
        stripped=line.strip()
        if not stripped:continue
        if not buffer and stripped in ('--restart','--kill','--error','--sorted_result'):
            if stripped=='--error':expect_error=True
            elif stripped=='--sorted_result':sorted_result=True
            else:client.restart(kill=stripped=='--kill')
            continue
        if stripped.startswith('--'):continue
        buffer.append(line)
        if stripped.endswith(';'):
            sql='\n'.join(buffer);buffer=[]
            rows=client.execute(sql,error=expect_error)
            if sorted_result:rows=sorted(rows,key=lambda row:json.dumps(row,ensure_ascii=False))
            actual.append({'error':expect_error,'rows':rows})
            expect_error=False;sorted_result=False
    if buffer or expect_error or sorted_result:raise ValueError(str(path)+': incomplete statement/directive')
    expected=json.loads(path.with_suffix('.result').read_text())
    if actual!=expected:
        for index,(a,e) in enumerate(zip(actual,expected)):
            if a!=e:raise AssertionError(f'{path.name} statement {index+1}: expected {e!r}, got {a!r}')
        raise AssertionError(f'{path.name}: expected {len(expected)} results, got {len(actual)}')
