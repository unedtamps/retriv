#!/bin/sh
# Create the read-only role the row-query tool connects as.
#
# This lives here, next to the other one-shot init service, because the
# alternative is a documented manual step that people skip. Migrations 0002
# and 0003 grant to this role *conditionally*: a password must never land in a
# migration, so the role is created operationally and the GRANT is skipped when
# it is missing. That made the failure silent and permanent -- skip the CREATE
# ROLE and the migrations succeed, the app then refuses to start, and nothing
# in the output says which step was missed.
#
# So the grants are applied here too, rather than depending on this running
# before alembic. Order stops mattering: a fresh database gets its tables from
# alembic and its grants from this script, an existing one gets both repaired
# on the next `docker compose up`.
set -eu

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${RETRIV_SQL_READER_USER:?RETRIV_SQL_READER_USER is required}"
: "${RETRIV_SQL_READER_PASSWORD:?RETRIV_SQL_READER_PASSWORD is required}"

# psql reads PGPASSWORD, not POSTGRES_PASSWORD, and prompts instead of failing
# when it finds neither -- which inside a one-shot service means a container
# that exits 0 having done nothing at all.
export PGPASSWORD="$POSTGRES_PASSWORD"

# Identifiers cannot be parameterised, so they are validated rather than
# escaped. A quote here would end the statement early and turn a typo into a
# silent partial failure.
validate_identifier() {
  case "$1" in
    ""|*[!a-z0-9_]*)
      echo "Invalid PostgreSQL identifier: $1" >&2
      exit 2
      ;;
  esac
}

validate_identifier "$POSTGRES_USER"
validate_identifier "$POSTGRES_DB"
validate_identifier "$RETRIV_SQL_READER_USER"

# The whole point of the role is that RLS constrains it. Creating it with
# either attribute set would let a query see every workspace while appearing
# scoped, and verify_reader() would then refuse the connection with a message
# about the DSN rather than about the role that was made wrong here.
if [ "$RETRIV_SQL_READER_USER" = "$POSTGRES_USER" ]; then
  echo "The SQL reader role must differ from POSTGRES_USER, which is a superuser" >&2
  exit 2
fi

if [ "$RETRIV_ENVIRONMENT" = "production" ]; then
  case "$RETRIV_SQL_READER_PASSWORD" in
    *change-this*|*replace-me*|*CHANGE_ME*|*development*)
      echo "Production SQL reader password must not use a development placeholder" >&2
      exit 2
      ;;
  esac
fi

# psql does not substitute :variables inside a dollar-quoted body, so every
# statement is built with format() and run through \gexec instead. The
# identifier and the password therefore never reach the parser as syntax:
# %I quotes one, %L quotes one as a literal, and :'name' arrives pre-quoted.
psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=reader_user="$RETRIV_SQL_READER_USER" \
  --set=reader_password="$RETRIV_SQL_READER_PASSWORD" \
  --set=database_name="$POSTGRES_DB" <<'SQL'
\echo 'creating or updating the reader role'
SELECT format(
  'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
  :'reader_user', :'reader_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'reader_user')
\gexec

-- Restated on every run so a rotated password in .env takes effect on the
-- next compose up, and so a role promoted by hand is demoted rather than
-- quietly keeping privileges the policies do not constrain.
SELECT format(
  'ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
  :'reader_user', :'reader_password'
)
\gexec

\echo 'granting read access'
GRANT CONNECT ON DATABASE :"database_name" TO :"reader_user";
GRANT USAGE ON SCHEMA public TO :"reader_user";

-- Guarded per table, because this also runs against a volume that has never
-- been migrated, which is the normal state of a fresh clone where
-- `make infra` precedes `make migrate`. The tables arrive afterwards and
-- migrations 0002/0003 grant them, the role already existing by then. Both
-- paths are covered and neither depends on the other.
SELECT format('GRANT SELECT ON public.%I TO %I', target, :'reader_user')
FROM unnest(ARRAY['document_rows', 'documents', 'document_workspaces', 'processing_runs']) AS target
WHERE to_regclass('public.' || target) IS NOT NULL
\gexec
SQL

echo "SQL reader role $RETRIV_SQL_READER_USER is ready on $POSTGRES_DB"