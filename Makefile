.PHONY: install run test test-py test-js

install:
	pip install -r requirements.txt
	npm install

run:
	uvicorn app.main:app --reload --log-level debug

# Default test target runs both Python and JS suites.
test: test-py test-js

test-py:
	pytest -q

test-js:
	npm test
