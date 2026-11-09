PYTHON ?= python3
MINIVECTOR_EVAL_FLAGS ?=

.PHONY: native native-check test evaluate evaluate-quick

native:
	$(PYTHON) scripts/build_native.py

native-check:
	$(PYTHON) scripts/check_native.py

test: native
	$(PYTHON) -m unittest discover -s tests -v

evaluate:
	$(PYTHON) scripts/evaluate.py --docker --benchmark --resource $(MINIVECTOR_EVAL_FLAGS)

evaluate-quick:
	$(PYTHON) scripts/evaluate.py --docker --benchmark --rows 1000 --queries 20 --rounds 1 $(MINIVECTOR_EVAL_FLAGS)
