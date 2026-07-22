.PHONY: test lint verify verify-mysql

test:
	./mvnw -q -pl backend test
	cd worker && .venv/bin/python -m pytest -q

lint:
	cd worker && .venv/bin/ruff check src tests
	cd worker && .venv/bin/mypy src

verify:
	./mvnw -q -pl backend test
	cd worker && .venv/bin/ruff check src tests
	cd worker && .venv/bin/mypy src
	cd worker && .venv/bin/pytest -q

verify-mysql:
	@test -n "$(WF_TEST_JDBC_URL)" || { echo "make verify-mysql: WF_TEST_JDBC_URL is required"; exit 1; }
	@test -n "$(WF_TEST_DATABASE_USER)" || { echo "make verify-mysql: WF_TEST_DATABASE_USER is required"; exit 1; }
	@test -n "$(WF_TEST_DATABASE_PASSWORD)" || { echo "make verify-mysql: WF_TEST_DATABASE_PASSWORD is required"; exit 1; }
	@WF_TEST_JDBC_URL="$(WF_TEST_JDBC_URL)" \
	WF_TEST_DATABASE_USER="$(WF_TEST_DATABASE_USER)" \
	WF_TEST_DATABASE_PASSWORD="$(WF_TEST_DATABASE_PASSWORD)" \
	./mvnw -q -pl backend -Dtest=BaselineMigrationTest test
