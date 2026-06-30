#!/usr/bin/env bash
set -u

PROJECT_DIR="/opt/Crypto-Multi-Coin-Scanner"
SCANNER_SERVICE="crypto-scanner.service"
WATCHER_SERVICE="crypto-position-watcher.service"
REPORT_TIMER="crypto-performance-report.timer"
REPORT_SERVICE="crypto-performance-report.service"

fail() {
  echo "FAIL: $1"
  echo ""
  echo "Rollback instructions:"
  echo "  cd \"$PROJECT_DIR\""
  echo "  ./scripts/rollback_production.sh <known-good-commit>"
  exit 1
}

pass_step() {
  echo "PASS: $1"
}

cd "$PROJECT_DIR" || fail "Project directory not found: $PROJECT_DIR"

if [ "$(pwd)" != "$PROJECT_DIR" ]; then
  fail "Refusing to run outside $PROJECT_DIR"
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  fail "Tracked local modifications detected. Commit/stash before updating."
fi
pass_step "clean tracked worktree"

OLD_REQUIREMENTS_HASH="$(sha256sum requirements.txt 2>/dev/null | awk '{print $1}')"
python3 backup_runtime_data.py || fail "runtime backup failed"
pass_step "runtime backup created"

git fetch origin main || fail "git fetch failed"
git pull --ff-only origin main || fail "git pull --ff-only failed"
pass_step "code updated"

NEW_REQUIREMENTS_HASH="$(sha256sum requirements.txt 2>/dev/null | awk '{print $1}')"
if [ "$OLD_REQUIREMENTS_HASH" != "$NEW_REQUIREMENTS_HASH" ]; then
  .venv/bin/pip install -r requirements.txt || fail "dependency install failed"
  pass_step "dependencies updated"
else
  pass_step "requirements unchanged"
fi

.venv/bin/python -m compileall -q . || fail "compileall failed"
.venv/bin/python tests/smoke_test.py || fail "smoke tests failed"
.venv/bin/python data_integrity_audit.py || echo "WARNING: data integrity audit returned warnings"
.venv/bin/python production_health.py --no-services || echo "WARNING: production health returned warnings"
pass_step "validation completed before service restart"

systemctl daemon-reload || fail "daemon-reload failed"
systemctl restart "$SCANNER_SERVICE" || fail "scanner restart failed"
systemctl restart "$WATCHER_SERVICE" || fail "position watcher restart failed"
systemctl enable "$REPORT_TIMER" || fail "performance timer enable failed"
systemctl restart "$REPORT_TIMER" || fail "performance timer restart failed"
pass_step "services restarted"

systemctl is-active "$SCANNER_SERVICE" || fail "$SCANNER_SERVICE inactive"
systemctl is-active "$WATCHER_SERVICE" || fail "$WATCHER_SERVICE inactive"
systemctl is-active "$REPORT_TIMER" || fail "$REPORT_TIMER inactive"
systemctl status "$REPORT_SERVICE" --no-pager || true

echo ""
echo "Recent scanner errors:"
journalctl -u "$SCANNER_SERVICE" -n 80 --no-pager | grep -Ei "traceback|error|exception" || true
echo ""
echo "FINAL PASS: Production update completed"
