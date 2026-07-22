.PHONY: test lint

test:
	./mvnw -q -pl backend test
	cd worker && .venv/bin/python -m pytest -q

lint:
	cd worker && .venv/bin/ruff check src tests
	cd worker && .venv/bin/mypy src
