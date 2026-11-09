PYTHON ?= python3

.PHONY: test evaluate evaluate-quick

test:
	$(PYTHON) -m unittest discover -s tests -v

evaluate:
	$(PYTHON) scripts/evaluate.py --docker --benchmark --resource

evaluate-quick:
	$(PYTHON) scripts/evaluate.py --docker --benchmark --rows 1000 --queries 20 --rounds 1
