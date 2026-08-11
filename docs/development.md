# WheelForge Development

## Prerequisites

Install these tools locally:

- Java 21 (the Maven wrapper uses the project Maven configuration).
- Python 3.12.
- MySQL Community Server 8.4 or newer. The verification workflow uses native
  MySQL; Docker, Testcontainers, Redis, MinIO, and S3 are not required.
- `make` and a POSIX shell.

On macOS with Homebrew:

```sh
brew install openjdk@21 python@3.12 mysql@8.4
brew services start mysql@8.4
"$(brew --prefix mysql@8.4)/bin/mysqladmin" ping
```

On another platform, install the same version floors with its native package
manager and start the MySQL service using that platform's service manager.

## MySQL Setup

The MySQL operator creates the empty application and disposable-test databases
before the application starts. Flyway does not create databases: it creates
`flyway_schema_history` and the V1 tables and indexes inside the selected
database. Use database-scoped users rather than `root`; the grants below are
limited to the database each user owns. The test user needs schema privileges
because Flyway creates the migration history and V1 schema inside the
disposable database during `make verify-mysql`.

```sh
mysql -u root -p <<'SQL'
CREATE DATABASE wheelforge CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE wheelforge_test CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;

CREATE USER 'wheelforge'@'localhost' IDENTIFIED BY 'replace-with-a-local-password';
CREATE USER 'wheelforge_test'@'localhost' IDENTIFIED BY 'replace-with-a-test-password';

GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES
  ON wheelforge.* TO 'wheelforge'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES
  ON wheelforge_test.* TO 'wheelforge_test'@'localhost';
FLUSH PRIVILEGES;
SQL
```

The application user is scoped to `wheelforge`, and the disposable test user
is scoped to `wheelforge_test`. `CREATE`, `ALTER`, `INDEX`, and `REFERENCES`
allow Flyway to create and maintain the current V1 migration objects; the
DML privileges support the current runtime and Flyway history updates. Neither
user has `DROP` or global privileges. Use matching passwords in the local
environment file. If the databases or users already exist, use `ALTER USER`
and `CREATE DATABASE ... IF NOT EXISTS` as appropriate for the local
installation.

## Environment and Local Roots

Copy the committed template, then replace its placeholder password and
absolute paths. The template contains the API `WF_JDBC_URL`, worker
`WF_DATABASE_URL`, credentials, queue settings, and worker ID:

```sh
cp .env.example .env
mkdir -p "$HOME/.local/share/wheelforge/data" "$HOME/.local/share/wheelforge/work"
chmod 700 "$HOME/.local/share/wheelforge/data" "$HOME/.local/share/wheelforge/work"
```

Set `WF_DATA_ROOT` to the data directory and `WF_WORKSPACE_ROOT` to the
workspace directory in `.env`; both must be absolute and different. Load the
values into a shell before starting a native process:

```sh
set -a
. ./.env
set +a
```

`WF_DATA_ROOT` is a security boundary. The API and Python Worker must read and
write the same data root. Prefer running both processes as one dedicated
WheelForge service identity; alternatively, use an ACL that grants access only
to their two dedicated identities. On POSIX systems, use mode `0700` for the
shared identity or an equivalent narrowly scoped ACL. On Windows, use an NTFS
ACL limited to the API and Worker service identities plus administrators.
Ordinary users and unrelated services must not have write access.

The API requires the filesystem provider to expose Java
`SecureDirectoryStream`. Upload publication, rollback compensation, artifact
retention, and generated-directory pruning are one complete storage contract;
the API fails startup when descriptor-relative mutation is unavailable. There
is no automatic read/write-only portable fallback. Confirm this capability on
the exact host, JDK, and mounted filesystem used in production. Exclusive write
permission on `WF_DATA_ROOT` remains required in addition to this provider
capability.

For single-user local development on a host such as macOS that lacks
`SecureDirectoryStream` and Linux `/proc/self/fd`, explicitly set
`WF_ALLOW_PORTABLE_STORAGE=true` and `WF_ALLOW_PORTABLE_WORKSPACE=true`.
Portable mode retains normalized-key, ownership, permission, file-identity,
regular-file, and symbolic-link checks, but it cannot provide Linux's
descriptor-bound protection against a same-user process replacing a path
during an operation. Use only private mode-`0700` roots on a trusted local
machine. Keep both values `false` for production and shared hosts.

The application uses Flyway migrations and Hibernate `ddl-auto: validate`
against MySQL. Flyway is the only schema owner; do not use Hibernate to create
or update tables.

## Dependencies

Resolve Java dependencies with the repository wrapper and install the Python
worker plus its development tools into `worker/.venv`, a local Python package
environment (not a VM image):

```sh
./mvnw -q -pl backend dependency:go-offline
python3.12 -m venv worker/.venv
worker/.venv/bin/python -m pip install --upgrade pip
cd worker
.venv/bin/python -m pip install -e '.[dev]'
cd ..
```

## Verification

The ordinary verification target is database-independent. Its Spring smoke
test still loads the web application context, but that test alone excludes
JDBC and Hibernate auto-configuration. Production configuration continues to
use MySQL and Flyway.

```sh
make verify
```

To verify the real Flyway baseline against the disposable native MySQL
database, provide all three variables explicitly. The target fails with a
clear message if any variable is missing and runs `BaselineMigrationTest`:

```sh
WF_TEST_JDBC_URL='jdbc:mysql://localhost:3306/wheelforge_test?connectionTimeZone=UTC' \
WF_TEST_DATABASE_USER='wheelforge_test' \
WF_TEST_DATABASE_PASSWORD='replace-with-a-test-password' \
make verify-mysql
```

The disposable database may be dropped and recreated between runs. Never point
`WF_TEST_JDBC_URL` at the development database.

## Native Startup

After MySQL is running, the environment is loaded, and the data/workspace
roots are writable, start the API with:

```sh
set -a
. ./.env
set +a
./mvnw -q -pl backend spring-boot:run
```

The API uses `WF_JDBC_URL`, `WF_DATABASE_USER`, `WF_DATABASE_PASSWORD`, and
`WF_DATA_ROOT`. The worker uses the matching `WF_DATABASE_URL`, both roots,
and its queue settings from `.env`.
