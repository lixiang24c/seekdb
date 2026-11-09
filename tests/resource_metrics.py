"""Resource-controller verification; retain raw peak and an optional strict peak gate.

Linux documents temporary usage above memory.max. Controller completion and raw
peak comparison are separate checks; never clamp the peak used for scoring.
https://docs.kernel.org/admin-guide/cgroup-v2.html#memory-interface-files
"""
import os,json,sys
from pathlib import Path


def check(stats,strict=False):
    if stats['limit']<=0:raise AssertionError('finite memory.max required')
    if not all(name in stats['events'] for name in ('oom','oom_kill')):
        raise AssertionError('required OOM counters unavailable')
    if stats['swap_limit']!=0:raise AssertionError('swap must be disabled')
    if any(stats['events'].get(name,0) for name in ('oom','oom_kill','oom_group_kill')):
        raise AssertionError('OOM event occurred')
    within=stats['peak']<=stats['limit']
    if strict and not within:raise AssertionError('strict memory.peak limit exceeded')
    return dict(stats,controller_pass=True,strict_peak_pass=within,
                transient_peak_excess_bytes=max(stats['peak']-stats['limit'],0),
                strict_peak_required=strict)


def snapshot():
    root=Path('/sys/fs/cgroup')
    stats={'peak':int((root/'memory.peak').read_text()),'limit':int((root/'memory.max').read_text()),
           'current':int((root/'memory.current').read_text()),'swap_limit':int((root/'memory.swap.max').read_text()),
           'events':{key:int(value) for key,value in (line.split() for line in (root/'memory.events').read_text().splitlines())}}
    try:
        return check(stats,strict=os.environ.get('MINIVECTOR_STRICT_PEAK','1')=='1')
    except AssertionError:
        print(json.dumps({'resource_diagnostic':stats}),file=sys.stderr,flush=True)
        raise
