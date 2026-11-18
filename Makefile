PY=python3
run:
	PYTHONPATH=src $(PY) -m papertrail.cli
run-papers:
	PYTHONPATH=src $(PY) -m papertrail.cli papers
test:
	PYTHONPATH=src $(PY) -m pytest -q
smoke:
	$(PY) tests/smoke.py
clean:
	rm -rf .pytest_cache **/__pycache__ *.db
