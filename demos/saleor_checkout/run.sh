#!/usr/bin/env bash
# Run the Saleor demo in Saleor's own environment.
#
# The environment is built from Saleor's uv.lock at the pinned commit, so the
# code under test runs with exactly the versions Saleor ships. The harness is
# added with --no-deps: its dependencies (pytest, pydantic) are already in
# Saleor's lock, and no version Saleor pins is changed.
#
# Needs: uv, git, a PostgreSQL reachable through DATABASE_URL (Saleor's own
# variable), and demos/fetch_upstream.py run first. Extra arguments go to pytest.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
saleor="$repo/demos/.upstream/saleor"
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$saleor/.venv}"

# Saleor's own requires-python picks its interpreter, whatever a caller set in UV_PYTHON.
(cd "$saleor" && env -u UV_PYTHON uv sync --frozen --quiet)
# The harness takes its version from git tags; a checkout without them still installs.
SETUPTOOLS_SCM_PRETEND_VERSION_FOR_DUE_WORK_HARNESS="${SETUPTOOLS_SCM_PRETEND_VERSION_FOR_DUE_WORK_HARNESS:-0.0.0}" \
  uv pip install --quiet --python "$UV_PROJECT_ENVIRONMENT/bin/python" --no-deps -e "$repo"

cd "$here"
if [[ "${1:-}" == "--typecheck" ]]; then
  # The harness's own pinned Pyrefly, against Saleor's interpreter and packages.
  pyrefly_version="$(cd "$repo" && uv export --frozen --only-group lint --no-hashes | sed -n 's/^pyrefly==//p')"
  exec uvx "pyrefly==$pyrefly_version" check --python-interpreter-path "$UV_PROJECT_ENVIRONMENT/bin/python"
fi
exec "$UV_PROJECT_ENVIRONMENT/bin/python" -m pytest "$@"
