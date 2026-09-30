#!/usr/bin/env bash
# Tell a CI job's PostgreSQL service it holds only throwaway test databases.
#
# Every transactional test flushes its tables (Saleor's ~250, Wagtail's through
# serialized rollback), and skipping the fsyncs makes each flush several times
# faster. Nothing any test proves depends on durability: the harness's deaths are
# simulated in the test process, never by stopping the server.
#
# The server is DATABASE_URL's, or the PG* variables' one.
set -euo pipefail

psql "${DATABASE_URL:+${DATABASE_URL%/*}/}postgres" \
  -c "ALTER SYSTEM SET fsync = off" \
  -c "ALTER SYSTEM SET synchronous_commit = off" \
  -c "ALTER SYSTEM SET full_page_writes = off" \
  -c "SELECT pg_reload_conf()"
