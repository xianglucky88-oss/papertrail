PY=python3
run:
	PYTHONPATH=src $(PY) -m papertrail.cli
run-sources:
	PYTHONPATH=src $(PY) -m papertrail.cli --sources
test:
	PYTHONPATH=src $(PY) -m pytest -q
clean:
	rm -rf .pytest_cache **/__pycache__ *.db *.db-wal *.db-shm
