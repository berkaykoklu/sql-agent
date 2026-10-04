#!/usr/bin/env bash
# Start PostgreSQL 18 in Docker, load the BIRD mini-dev dump, and create a SELECT-only role for the agent.
# Needs BIRD_PG_ADMIN_PASSWORD and BIRD_PG_AGENT_PASSWORD in the environment (e.g. from .env).
set -euo pipefail
cd "$(dirname "$0")/.."
: "${BIRD_PG_ADMIN_PASSWORD:?}" "${BIRD_PG_AGENT_PASSWORD:?}"

docker run -d --name bird-pg -e POSTGRES_PASSWORD="$BIRD_PG_ADMIN_PASSWORD" -p 127.0.0.1:5433:5432 \
  -v bird-pg:/var/lib/postgresql -v "$PWD/data/minidev/MINIDEV_postgresql:/dump:ro" postgres:18
# TCP only opens once the init server has restarted, so this waits for the real server
until docker exec bird-pg pg_isready -h 127.0.0.1 -U postgres -q; do sleep 1; done

psql() { docker exec -i bird-pg psql -U postgres -v ON_ERROR_STOP=1 -q "$@"; }
psql -c "CREATE ROLE xiaolongli"  # the dump's ALTER ... OWNER TO lines expect this role
time psql -f /dump/BIRD_dev.sql
psql -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
psql <<SQL
CREATE ROLE agent_ro LOGIN PASSWORD '$BIRD_PG_AGENT_PASSWORD';
GRANT CONNECT ON DATABASE postgres TO agent_ro;
GRANT USAGE ON SCHEMA public TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO agent_ro;
ALTER ROLE agent_ro SET default_transaction_read_only = on;
SQL
echo "loaded; connect as agent_ro on 127.0.0.1:5433/postgres"
