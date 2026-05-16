.PHONY: install run test

install:
	pip install -r requirements.txt

run:
	uvicorn app.main:app --reload --log-level debug

test:
	pytest -q
