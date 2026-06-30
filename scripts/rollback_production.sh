#!/usr/bin/env bash
set -u

PROJECT_DIR="/opt/Crypto-Multi-Coin-Scanner"
TARGET_COMMIT="${1:-}"
SCANNER_SERVICE="crypto-scanner.service"
WATCHER_SERVICE="crypto-position-watcher.service"
REPORT_TIMER="crypto-performance-report.timer"

fail() {
  echo "FAIL: $1"
  echo "Runtime records are preserved. No logs/reports/backups/.env files were deleted."
  exit 1
}

if [ -z "$TARGET_COMMIT" ]; then
  echo "Usage: ./scripts/rollback_production.sh <commit>"
  exit 2
fi

cd "$PROJECT_DIR" || fail "Project directory not found: $PROJECT_DIR"

python3 backup_runtime_data.py || fail "runtime backup failed"

git cat-file -e "${TARGET_COMMIT}^{commit}" || fail "target commit does not exist: $TARGET_COMMIT"

if ! git diff --quiet || ! git diff --cached --quiet; then
  fail "Tracked local modifications detected. Commit/stash before rollback."
fi

git checkout "$TARGET_COMMIT" -- . || fail "tracked code checkout failed"

.venv/bin/python -m compileall -q . || fail "compileall failed after rollback"
.venv/bin/python tests/smoke_test.py || fail "smoke tests failed after rollback"
.venv/bin/python data_integrity_audit.py || echo "WARNING: data integrity audit returned warnings"
.venv/bin/python production_health.py --no-services || echo "WARNING: production health returned warnings"

systemctl daemon-reload || fail "daemon-reload failed"
systemctl restart "$SCANNER_SERVICE" || fail "scanner restart failed"
systemctl restart "$WATCHER_SERVICE" || fail "position watcher restart failed"
systemctl restart "$REPORT_TIMER" || fail "performance timer restart failed"

echo "FINAL PASS: Rolled back tracked code to $TARGET_COMMIT"
echo "Runtime records preserved: logs, reports, backups, .env, locks, and state files."
