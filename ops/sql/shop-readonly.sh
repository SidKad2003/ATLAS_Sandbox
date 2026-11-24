#!/bin/sh
# Creates (or refreshes) the read-only role svc_sim_ro on the shop database.
# Runs once per `up`; the password comes from a mounted secret and is never echoed.
set -eu
RO_PW="$(cat /run/secrets/shop_ro_password)"
for i in $(seq 1 30); do pg_isready -q && break; sleep 2; done
psql -v ON_ERROR_STOP=1 -q -v ro_pw="$RO_PW" <<'SQL'
SELECT format('CREATE ROLE svc_sim_ro LOGIN PASSWORD %L', :'ro_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'svc_sim_ro') \gexec
SELECT format('ALTER ROLE svc_sim_ro LOGIN PASSWORD %L', :'ro_pw') \gexec
ALTER ROLE svc_sim_ro SET default_transaction_read_only = on;
GRANT CONNECT ON DATABASE astronomy_db TO svc_sim_ro;
GRANT USAGE ON SCHEMA accounting, catalog TO svc_sim_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA accounting, catalog TO svc_sim_ro;
SQL
echo "svc_sim_ro ready (read-only)"
