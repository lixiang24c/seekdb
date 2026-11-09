PYTHON ?= python3

.PHONY: native native-check test evaluate evaluate-quick

native:
	$(PYTHON) scripts/build_native.py

native-check:
	$(PYTHON) scripts/check_native.py

test: native
	$(PYTHON) -m unittest discover -s tests -v

evaluate:
	$(PYTHON) scripts/evaluate.py --docker --benchmark --resource

evaluate-quick:
	$(PYTHON) scripts/evaluate.py --docker --benchmark --rows 1000 --queries 20 --rounds 1
