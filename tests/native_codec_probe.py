"""Child probe for the sanitized CPython extension, using malloc-backed objects."""
import importlib.util,math,struct,sys

def main():
    spec=importlib.util.spec_from_file_location('libminivector',sys.argv[1])
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    for _ in range(1000):
        values=[0.,-0.,1e-45,16777217,3.4028235e38]
        actual=module.float32_vector(values,len(values))
        expected=struct.unpack('<5f',struct.pack('<5f',*values))
        assert struct.pack('<5f',*actual)==struct.pack('<5f',*expected)
        for bad in ([1.]*127+[True],[float('nan')],[float('inf')],[1<<2000]):
            try:module.float32_vector(bad,len(bad))
            except (ValueError,OverflowError):pass
            else:raise AssertionError('invalid vector accepted')
    print('PASS: native float32 codec ASan/UBSan, malloc-backed Python objects and error cleanup')
if __name__=='__main__':main()
