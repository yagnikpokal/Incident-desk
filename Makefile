PY := .venv/bin/python

.PHONY: setup test dev demo

setup:
	python3 -m venv .venv
	$(PY) -m pip install -e ".[dev]"

test:
	$(PY) -m pytest -q

dev:
	$(PY) -m uvicorn incident_desk.main:app --reload --host 127.0.0.1 --port 8787

demo:
	$(PY) -m incident_desk.demo
