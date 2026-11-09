#!/usr/bin/env python3
import os,subprocess,tempfile,sys,sysconfig
from pathlib import Path
root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='minivector-native-sanitizer-') as directory:
    binary=Path(directory)/'check'
    subprocess.run([os.environ.get('CC','gcc'),'-std=c11','-O1','-g','-ffp-contract=off',
                    '-fsanitize=address,undefined','-fno-sanitize-recover=all',
                    str(root/'tests/native_sanitizer.c'),'-o',str(binary),'-lm'],check=True)
    subprocess.run([str(binary)],check=True)
    library=Path(directory)/'libminivector.so'
    compiler=os.environ.get('CC','gcc')
    subprocess.run([compiler,'-std=c11','-O1','-g','-fPIC','-shared','-ffp-contract=off',
                    '-fsanitize=address,undefined','-fno-sanitize-recover=all','-DMV_WITH_PYTHON',
                    '-I'+sysconfig.get_path('include'),str(root/'native/topk.c'),'-o',str(library),'-lm'],check=True)
    asan=subprocess.check_output([compiler,'-print-file-name=libasan.so'],text=True).strip()
    environment=os.environ.copy()
    environment['LD_PRELOAD']=asan+(':'+environment['LD_PRELOAD'] if environment.get('LD_PRELOAD') else '')
    environment['ASAN_OPTIONS']='detect_leaks=0'
    environment['PYTHONMALLOC']='malloc'
    subprocess.run([sys.executable,str(root/'tests/native_codec_probe.py'),str(library)],env=environment,check=True)
