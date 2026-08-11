.PHONY: test lint frontend-test frontend-verify verify verify-mysql verify-worker-mysql

test:
	./mvnw -q -pl backend test
	cd worker && .venv/bin/python -m pytest -q
	cd frontend && npm run test:run

lint:
	cd worker && .venv/bin/ruff check src tests
	cd worker && .venv/bin/mypy src
	cd frontend && npm run typecheck

frontend-test:
	cd frontend && npm run test:run

frontend-verify:
	cd frontend && npm run typecheck
	cd frontend && npm run test:run
	cd frontend && npm run build

verify:
	./mvnw -q -pl backend test
	cd worker && .venv/bin/ruff check src tests
	cd worker && .venv/bin/mypy src
	cd worker && .venv/bin/pytest -q
	$(MAKE) frontend-verify

verify-mysql: verify-worker-mysql
	@test -n "$(WF_TEST_JDBC_URL)" || { echo "make verify-mysql: WF_TEST_JDBC_URL is required"; exit 1; }
	@test -n "$(WF_TEST_DATABASE_USER)" || { echo "make verify-mysql: WF_TEST_DATABASE_USER is required"; exit 1; }
	@test -n "$(WF_TEST_DATABASE_PASSWORD)" || { echo "make verify-mysql: WF_TEST_DATABASE_PASSWORD is required"; exit 1; }
	@WF_TEST_JDBC_URL="$(WF_TEST_JDBC_URL)" \
	WF_TEST_DATABASE_USER="$(WF_TEST_DATABASE_USER)" \
	WF_TEST_DATABASE_PASSWORD="$(WF_TEST_DATABASE_PASSWORD)" \
	./mvnw -q -pl backend -Dtest=BaselineMigrationTest test

verify-worker-mysql:
	@test -n "$(WF_TEST_DATABASE_URL)" || { echo "make verify-worker-mysql: WF_TEST_DATABASE_URL is required"; exit 1; }
	@test "$(WF_TEST_DATABASE_DISPOSABLE)" = "1" || { echo "make verify-worker-mysql: WF_TEST_DATABASE_DISPOSABLE=1 is required"; exit 1; }
	@cd worker && \
	WF_TEST_DATABASE_URL="$(WF_TEST_DATABASE_URL)" \
	WF_TEST_DATABASE_DISPOSABLE="$(WF_TEST_DATABASE_DISPOSABLE)" \
	.venv/bin/pytest tests/jobs/test_mysql_integration.py -q -rs
