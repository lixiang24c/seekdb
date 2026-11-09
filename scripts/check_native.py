#!/usr/bin/env python3
import os,subprocess,tempfile
from pathlib import Path
root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='minivector-native-sanitizer-') as directory:
    binary=Path(directory)/'check'
    subprocess.run([os.environ.get('CC','gcc'),'-std=c11','-O1','-g','-ffp-contract=off',
                    '-fsanitize=address,undefined','-fno-sanitize-recover=all',
                    str(root/'tests/native_sanitizer.c'),'-o',str(binary)],check=True)
    subprocess.run([str(binary)],check=True)
