#!/usr/bin/env python3
import os,subprocess,sysconfig
from pathlib import Path
root=Path(__file__).resolve().parents[1]
subprocess.run([os.environ.get('CC','gcc'),'-std=c11','-O3','-fPIC','-shared','-ffp-contract=off','-DMV_WITH_PYTHON','-I'+sysconfig.get_path('include'),
                str(root/'native/topk.c'),'-o',str(root/'minivectordb/libminivector.so')],check=True)
