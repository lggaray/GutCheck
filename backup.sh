#!/usr/bin/env bash
# Back up the Supabase Postgres database to a gzipped SQL dump.
# Loads DATABASE_URL from .env at runtime — the URL is never printed.
set -euo pipefail

cd "$(dirname "$0")"

# Load .env (DATABASE_URL etc.) without echoing any values.
if [[ ! -f .env ]]; then
  echo "ERROR: .env not found in project root." >&2
  exit 1
fi
set -a
# shellcheck disable=SC1091
source .env
set +a

# Fall back to SUPABASE_CONN_STRING (same URL, older name used in .env)
DATABASE_URL="${DATABASE_URL:-${SUPABASE_CONN_STRING:-}}"
if [[ -z "${DATABASE_URL}" ]]; then
  echo "ERROR: DATABASE_URL (or SUPABASE_CONN_STRING) is not set in .env." >&2
  exit 1
fi

# pg_dump lives in the multiagent conda env (installed Jun 12, no system package).
PATH="$HOME/miniconda3/envs/multiagent/bin:$PATH"

# Verify pg_dump is available.
if ! command -v pg_dump > /dev/null 2>&1; then
  echo "ERROR: pg_dump not found." >&2
  echo "Install it with: sudo apt install postgresql-client" >&2
  exit 1
fi

mkdir -p backups

OUT_FILE="backups/nutrition_$(date +%Y%m%d_%H%M).sql.gz"

# Parse the URL from the right (raw ':' '@' '/' in password break naive parsing
# and pg_dump's own URI parser) and pass discrete PG* vars. Values never printed.
export DATABASE_URL
eval "$(python3 - <<'PY'
import os, shlex
from urllib.parse import unquote
rest = os.environ["DATABASE_URL"].split("://", 1)[1]
creds, hostpart = rest.rsplit("@", 1)
user, _, password = creds.partition(":")
hostport, _, dbname = hostpart.partition("/")
host, _, port = hostport.partition(":")
print(f"export PGHOST={shlex.quote(host)}")
print(f"export PGPORT={shlex.quote(port if port.isdigit() else '5432')}")
print(f"export PGDATABASE={shlex.quote(dbname.split('?')[0] or 'postgres')}")
print(f"export PGUSER={shlex.quote(unquote(user))}")
print(f"export PGPASSWORD={shlex.quote(unquote(password))}")
PY
)"

echo "==> Dumping database to ${OUT_FILE}"
pg_dump | gzip > "${OUT_FILE}"

echo "==> Backup complete:"
du -h "${OUT_FILE}"
