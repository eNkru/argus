#!/usr/bin/env bash
# Supply-chain audit gate: scan the pinned runtime lockfile for known CVEs.
#
# Must exit 0 for the gate to pass. Run from the repo root:
#
#     ./scripts/audit.sh
#
# requirements.lock pins the exact runtime dependency set that was installed and
# verified clean by pip-audit during the 2026-09 security-hardening pass
# (finding #5). A non-zero exit means a pinned dependency has a known
# vulnerability — triage deliberately; do not bump versions blindly (camoufox in
# particular is a browser-build contract pin, see pyproject.toml).
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -x .venv/bin/pip-audit ]]; then
    echo "pip-audit not found in .venv — install it first: pip install 'pip-audit>=2.7'" >&2
    exit 2
fi

# --disable-pip: requirements.lock is already the fully-resolved, hashed
# transitive closure, so tell pip-audit to audit the pinned list as-is and NOT
# re-resolve with pip. Re-resolving would re-derive platform-specific branches
# (e.g. screeninfo's darwin-only Cython/pyobjc dep on macOS) that aren't in the
# Linux-targeted lockfile and would trip --require-hashes.
.venv/bin/pip-audit -r requirements.lock --disable-pip "$@"