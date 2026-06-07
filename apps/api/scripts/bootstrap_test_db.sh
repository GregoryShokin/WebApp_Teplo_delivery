#!/usr/bin/env bash
# Создаёт БД teplo_test если её нет. Идемпотентно.
set -e

PGHOST="${PGHOST:-teplo-postgres}"
PGUSER="${PGUSER:-teplo}"
PGPASSWORD="${PGPASSWORD:-teplo}"
PGPORT="${PGPORT:-5432}"
TEST_DB="${TEPLO_TEST_DB:-teplo_test}"

export PGPASSWORD

echo "Checking if database '$TEST_DB' exists on $PGHOST:$PGPORT..."
EXISTS=$(psql -h "$PGHOST" -U "$PGUSER" -p "$PGPORT" -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$TEST_DB'")
if [ "$EXISTS" = "1" ]; then
    echo "Database '$TEST_DB' already exists."
else
    echo "Creating database '$TEST_DB'..."
    psql -h "$PGHOST" -U "$PGUSER" -p "$PGPORT" -d postgres -c "CREATE DATABASE $TEST_DB OWNER $PGUSER;"
    echo "Done."
fi
