# WheelForge Native Operations Guide

## Supported Topology

WheelForge V1 runs on one Linux host with native MySQL 8.4, Java 21, Node.js 24,
and a Python 3.12 virtual environment. Node.js is required only to build the
reviewed frontend release, not while the service runs. The Spring Boot API and Python Worker run as
the same dedicated `wheelforge` account and share only the local data root.
The Worker also owns a separate disposable workspace root.

Docker is neither installed nor required. Redis and MinIO are possible future
queue and object-storage adapters; they are not runtime dependencies in V1.
The database-backed job lease is the queue, and the filesystem is the Artifact
store.

## Validation Statement

Every Linux and Windows target uses static validation. Static validation checks
Wheel tags, archive paths and limits, metadata, RECORD hashes, and dependency
closure. It does not install or execute dependency code and does not prove installation on the target machine. Operators must preserve this wording in
the UI, delivery notes, and customer-facing build reports.

## Host Preparation

Install MySQL 8.4, Java 21, Node.js 24, Python 3.12, `curl`, `mysql-client`, `tar`, and a
POSIX shell. Python 3.9 through 3.13 are target selectors; the host only needs
Python 3.12 for the Worker. Python 3.9 is retained for V1 compatibility even
though that Python line is end-of-life.

Create the service identity and private roots:

```sh
sudo useradd --system --home-dir /var/lib/wheelforge --shell /usr/sbin/nologin wheelforge
sudo install -d -o wheelforge -g wheelforge -m 0700 \
  /var/lib/wheelforge /var/lib/wheelforge/data /var/lib/wheelforge/work
sudo install -d -o root -g root -m 0755 /opt/wheelforge
sudo install -d -o root -g root -m 0700 /etc/wheelforge
```

`WF_DATA_ROOT` and `WF_WORKSPACE_ROOT` must be different absolute paths and
must not be symlinks. Do not grant write access to interactive users or other
services. Keep `WF_ALLOW_PORTABLE_STORAGE=false` and
`WF_ALLOW_PORTABLE_WORKSPACE=false` on production hosts so missing
descriptor-bound filesystem capabilities fail startup.

## MySQL Setup

Run the following as a MySQL administrator and replace the password:

```sql
CREATE DATABASE wheelforge CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER 'wheelforge'@'127.0.0.1' IDENTIFIED BY 'replace-with-random-password';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES
  ON wheelforge.* TO 'wheelforge'@'127.0.0.1';
FLUSH PRIVILEGES;
```

Do not use the MySQL `root` account in either service. Flyway, launched by the
API, owns schema migrations. The Worker must never create or alter production
tables.

## Install Application

Build and install from a reviewed release checkout:

```sh
./mvnw -q -pl backend clean package -DskipTests
npm --prefix frontend ci
npm --prefix frontend run build
python3.12 -m venv worker/.venv
worker/.venv/bin/python -m pip install --upgrade pip
worker/.venv/bin/python -m pip install ./worker

sudo install -o root -g root -m 0644 \
  backend/target/wheelforge-api-0.1.0-SNAPSHOT.jar \
  /opt/wheelforge/wheelforge-api.jar
sudo install -d -o root -g root -m 0755 /opt/wheelforge/frontend/dist
sudo cp -a frontend/dist/. /opt/wheelforge/frontend/dist/
sudo cp -a worker docs scripts deploy /opt/wheelforge/
sudo chown -R root:root /opt/wheelforge
sudo find /opt/wheelforge/frontend/dist -type d -exec chmod 0755 {} \;
sudo find /opt/wheelforge/frontend/dist -type f -exec chmod 0644 {} \;
```

Install and edit the shared environment file. The SQLAlchemy URL must use a
URL-encoded password, while `WF_DATABASE_PASSWORD` contains the original
password. Generate `WF_AUTH_TOKEN_SECRET` from at least 32 random bytes.

```sh
sudo install -o root -g root -m 0600 deploy/.env.example \
  /etc/wheelforge/wheelforge.env
sudoedit /etc/wheelforge/wheelforge.env
sudo install -o root -g root -m 0644 deploy/wheelforge-*.service \
  /etc/systemd/system/
sudo systemctl daemon-reload
```

The environment file must remain `root:root` mode `0600`; systemd reads it
before dropping privileges. On the release host, validate both units:

`WF_FRONTEND_ROOT` must point to the absolute, root-owned Vite `dist` directory.
The API only reads these files. Keep the directory outside the writable data and
workspace roots.

```sh
systemd-analyze verify /etc/systemd/system/wheelforge-api.service \
  /etc/systemd/system/wheelforge-worker.service
```

If the distribution names MySQL `mysqld.service`, update the two unit `After=`
lines before verification.

## First Start

Start MySQL and the API first so Flyway creates the schema, then initialize the
20 target profiles and start the Worker:

```sh
sudo systemctl enable --now mysql.service
sudo systemctl enable --now wheelforge-api.service
sudo bash -c 'set -a; . /etc/wheelforge/wheelforge.env; set +a; exec runuser -u wheelforge --preserve-environment -- /opt/wheelforge/scripts/bootstrap-target-profiles.sh'
sudo systemctl enable --now wheelforge-worker.service
sudo bash -c 'set -a; . /etc/wheelforge/wheelforge.env; set +a; exec runuser -u wheelforge --preserve-environment -- /opt/wheelforge/deploy/smoke.sh'
```

Remove `WF_BOOTSTRAP_ADMIN_PASSWORD` and
`WF_BOOTSTRAP_ADMIN_USERNAME` from the environment file after the first
administrator has been created, then restart the API. The bootstrap is not a
password reset mechanism.

## Routine Operations

Start, stop, restart, and inspect the services together:

```sh
sudo systemctl start wheelforge-api wheelforge-worker
sudo systemctl stop wheelforge-worker wheelforge-api
sudo systemctl restart wheelforge-api wheelforge-worker
sudo systemctl status wheelforge-api wheelforge-worker
journalctl -u wheelforge-api -u wheelforge-worker --since today
```

Use `journalctl` for stage failures and lease recovery. Do not log environment
files, access tokens, database URLs, or passwords. The Worker reclaims an
expired lease after `WF_JOB_LEASE_SECONDS`; the prior execution cannot publish
after takeover. Startup maintenance removes abandoned generated workspaces and
objects older than `WF_MAINTENANCE_AGE_SECONDS`. Cancellation cleanup removes
the owned workspace and any unpublished Artifact; a published terminal
Artifact is retained according to policy.

## Limits And Retention

The API rejects files larger than 512 KiB and the parser rejects more than
2,000 logical lines. Default build ceilings are 500 packages, 512 MiB per
Wheel, 2 GiB total download/Artifact bytes, 10,000 archive entries, expansion
ratio 100, 20 candidates per direct requirement, 100 resolution attempts, and
3,600 seconds per task. Each build snapshots its database configuration once;
administrator updates affect later builds only and may tighten, not exceed,
the executable safety ceilings.

Artifact retention defaults to 30 days. With `WF_RETENTION_ENABLED=true`, the
API claims expired records in bounded batches, skips active download leases,
deletes the generated object, and records cleanup. Monitor free disk space and
the oldest uncleaned `artifacts.expires_at` value.

## Backup And Restore

For a consistent backup, stop the Worker first, then the API. Back up MySQL and
the data root in the same maintenance window; the workspace root is disposable
and must not be restored.

```sh
sudo systemctl stop wheelforge-worker wheelforge-api
MYSQL_PWD='database-password' mysqldump --host=127.0.0.1 --user=wheelforge \
  --single-transaction --skip-lock-tables --no-tablespaces wheelforge > wheelforge.sql
sudo tar --create --gzip --file=wheelforge-data.tgz \
  --directory=/var/lib/wheelforge data
sudo systemctl start wheelforge-api wheelforge-worker
```

Restore only to an empty, tested database and empty data root. Stop both
services, import `wheelforge.sql`, extract the data archive under
`/var/lib/wheelforge`, restore ownership to `wheelforge:wheelforge` and mode
`0700`, import the SQL through a database administrator account, start the API
then Worker, and run `deploy/smoke.sh`. Perform a sample
Artifact download and checksum verification before reopening traffic.

## Credential Rotation

For database credential rotation, stop both services, run `ALTER USER` from a
separate MySQL administrator session, update `WF_DATABASE_PASSWORD` and the
URL-encoded password in `WF_DATABASE_URL`, keep the environment file mode
`0600`, and start API then Worker. Rotating `WF_AUTH_TOKEN_SECRET` invalidates
all access tokens; schedule it as a user-visible logout event. Run the smoke
check and inspect `journalctl` after every credential rotation.

## Release Checks

Run `make verify` and `npm --prefix frontend run test:e2e` on every build. On a
disposable native MySQL database, run `make verify-mysql`. On a Linux release host with the services running, run
the complete integration suite, `systemd-analyze verify`, and
`deploy/smoke.sh`. Keep the resulting command output with the release record.
