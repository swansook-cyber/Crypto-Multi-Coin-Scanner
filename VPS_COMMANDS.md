# VPS Commands

Crypto-Multi-Coin-Scanner is an internal lab Telegram signal assistant. It does not auto-trade.

## Production Layout

Production app path:

```bash
/opt/Crypto-Multi-Coin-Scanner
```

Python runtime:

```bash
/opt/Crypto-Multi-Coin-Scanner/.venv/bin/python
```

Current systemd units:

- `crypto-scanner.service`
- `crypto-outcome-checker.service`
- `crypto-outcome-checker.timer`
- `crypto-daily-summary.service`
- `crypto-daily-summary.timer`
- `crypto-performance-report.service`
- `crypto-performance-report.timer`
- `crypto-external-inbox.service`
- `crypto-position-watcher.service`

## RC1 Production Health

Run these before and after updates:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
.venv/bin/python production_health.py
.venv/bin/python data_integrity_audit.py
.venv/bin/python entry_timing_operational_summary.py
.venv/bin/python position_watcher_state_cleanup.py
.venv/bin/python production_v1_readiness.py
.venv/bin/python backup_runtime_data.py
```

Position watcher stale-state cleanup is dry-run by default:

```bash
.venv/bin/python position_watcher_state_cleanup.py
```

Apply only after reviewing every listed key:

```bash
.venv/bin/python position_watcher_state_cleanup.py --apply
```

`--apply` preserves CSV history and removes only active runtime lock files tied to confirmed closed positions after creating a backup.

Exact service checks:

```bash
systemctl status crypto-scanner --no-pager
systemctl status crypto-position-watcher --no-pager
systemctl status crypto-performance-report.timer --no-pager
systemctl status crypto-performance-report.service --no-pager
systemctl is-active crypto-scanner
systemctl is-active crypto-position-watcher
systemctl is-active crypto-performance-report.timer
systemctl is-enabled crypto-performance-report.timer
journalctl -u crypto-scanner -n 120 --no-pager
journalctl -u crypto-position-watcher -n 120 --no-pager
journalctl -u crypto-performance-report.service -n 120 --no-pager
```

Guarded update:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
./scripts/update_production.sh
```

Rollback tracked code only, preserving `.env`, logs, reports, backups, lock files, and runtime state:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
./scripts/rollback_production.sh <commit>
```

## VPS Login

Default Windows helper config is stored in:

```bat
tools\vps_env.bat
```

Current default:

```bat
set VPS_HOST=143.14.11.12
set VPS_USER=root
set VPS_PORT=22
set VPS_APP_DIR=/opt/Crypto-Multi-Coin-Scanner
```

Manual login:

```bat
ssh root@143.14.11.12
```

With explicit port:

```bat
ssh -p 22 root@143.14.11.12
```

## Windows Control Center

Main launcher:

```bat
tools\VelaFlow Scanner Control Center.bat
```

Use it to check scanner status, logs, restart scanner, pull latest code, view performance report, run daily summary, and run health checks.

Other Windows helpers:

```bat
tools\scanner_status.bat
tools\scanner_restart.bat
tools\scanner_logs.bat
tools\outcome_status.bat
tools\performance_report.bat
tools\latest_trades.bat
tools\equity_curve.bat
tools\git_pull_update.bat
tools\health_check.bat
```

## Service Status

Scanner:

```bash
systemctl status crypto-scanner.service --no-pager
systemctl is-active crypto-scanner.service
journalctl -u crypto-scanner.service -n 140 --no-pager
```

Outcome checker:

```bash
systemctl status crypto-outcome-checker.timer --no-pager
systemctl status crypto-outcome-checker.service --no-pager
systemctl list-timers crypto-outcome-checker.timer --no-pager
journalctl -u crypto-outcome-checker.service -n 140 --no-pager
```

Daily summary:

```bash
systemctl status crypto-daily-summary.timer --no-pager
systemctl status crypto-daily-summary.service --no-pager
systemctl list-timers crypto-daily-summary.timer --no-pager
journalctl -u crypto-daily-summary.service -n 100 --no-pager
```

Daily Performance Report:

```bash
systemctl status crypto-performance-report.timer --no-pager
systemctl status crypto-performance-report.service --no-pager
systemctl list-timers crypto-performance-report.timer --no-pager
journalctl -u crypto-performance-report.service -n 140 --no-pager
```

If the timer has not been installed yet:

```bash
sudo cp /opt/Crypto-Multi-Coin-Scanner/deploy/systemd/crypto-performance-report.service /etc/systemd/system/
sudo cp /opt/Crypto-Multi-Coin-Scanner/deploy/systemd/crypto-performance-report.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-performance-report.timer
```

External inbox listener:

```bash
systemctl status crypto-external-inbox.service --no-pager
systemctl is-active crypto-external-inbox.service
journalctl -u crypto-external-inbox.service -n 140 --no-pager
```

If the service has not been installed yet:

```bash
sudo cp /opt/Crypto-Multi-Coin-Scanner/deploy/systemd/crypto-external-inbox.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-external-inbox.service
```

Position watcher:

```bash
systemctl status crypto-position-watcher.service --no-pager
systemctl is-active crypto-position-watcher.service
journalctl -u crypto-position-watcher.service -n 140 --no-pager
```

If the service has not been installed yet:

```bash
sudo cp /opt/Crypto-Multi-Coin-Scanner/deploy/systemd/crypto-position-watcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-position-watcher.service
```

## Restart Services

Scanner:

```bash
sudo systemctl restart crypto-scanner.service
```

Outcome checker timer:

```bash
sudo systemctl restart crypto-outcome-checker.timer
```

Daily summary timer:

```bash
sudo systemctl restart crypto-daily-summary.timer
```

Daily Performance Report timer:

```bash
sudo systemctl restart crypto-performance-report.timer
```

External inbox listener:

```bash
sudo systemctl restart crypto-external-inbox.service
```

Restart all production units after a code update:

```bash
sudo systemctl restart crypto-scanner.service
sudo systemctl restart crypto-outcome-checker.timer
sudo systemctl restart crypto-daily-summary.timer
sudo systemctl restart crypto-performance-report.timer
sudo systemctl restart crypto-external-inbox.service
```

## Git Update On VPS

```bash
cd /opt/Crypto-Multi-Coin-Scanner
git pull origin main
.venv/bin/python -m compileall -q .
.venv/bin/python tests/smoke_test.py
sudo systemctl restart crypto-scanner.service
sudo systemctl restart crypto-outcome-checker.timer
sudo systemctl restart crypto-daily-summary.timer
sudo systemctl enable --now crypto-performance-report.timer
sudo systemctl restart crypto-external-inbox.service
systemctl status crypto-scanner.service --no-pager
systemctl status crypto-performance-report.timer --no-pager
```

Windows shortcut:

```bat
tools\git_pull_update.bat
```

## Runtime Commands

Run scanner once with current `.env` settings:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
.venv/bin/python cornix_agent.py
```

Review outcomes:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
.venv/bin/python review_signals.py
.venv/bin/python review_signals.py --notify
```

Daily summary:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
.venv/bin/python daily_summary.py
.venv/bin/python daily_summary.py --dry-run
```

Daily Performance Report:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
.venv/bin/python performance_report.py
.venv/bin/python performance_report.py --executive
.venv/bin/python performance_report.py --send
.venv/bin/python performance_report.py --test-report
```

`performance_report.py` prints the complete detailed report and writes `reports/report.html`. `--executive` previews Executive Report V2 locally. `--send` sends only Executive Report V2 to `TELEGRAM_REPORTS_CHAT_ID`; detailed analytics stay in the dashboard/web report. Entry Timing market status in the executive summary is reporting-only: `COLLECTING DATA`, `ENTERABLE`, `WAITING`, `POOR TIMING`, or `MIXED`. If Telegram delivery fails, the command exits non-zero so `crypto-performance-report.service` shows failure in `journalctl`.

Production V1 status console:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
.venv/bin/python system_status.py
.venv/bin/python system_status.py --json
```

Preferred daily shortcut:

```bash
alias scanner-status='cd /opt/Crypto-Multi-Coin-Scanner && .venv/bin/python system_status.py'
```

Do not modify the shell automatically. The status console is read-only and does
not send Telegram, repair CSVs, restart services, or place trades.

Dashboard V2:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
.venv/bin/streamlit run dashboard.py --server.address 0.0.0.0 --server.port 8501
```

Dashboard V2 is read-only. It reads CSV logs from `logs/` and does not send Telegram, call Binance, place trades, modify logs, or change strategy settings.

Position Management Advisor:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
.venv/bin/python position_manager.py
```

External Signal Inbox poll once:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
.venv/bin/python telegram_external_inbox.py
```

External Signal Inbox listener:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
.venv/bin/python telegram_external_inbox.py --loop
```

External Signal Refine V2 is approved-only:

- APPROVED external signals may route to Signals and Cornix
- WAIT / SKIP / RISKY / FAILED signals are stored in `logs/external_signals.csv` only and appear in summaries
- Cornix output is production-ready signal text

## Telegram Multi-Channel Config

Required:

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Optional channel-specific IDs:

```env
TELEGRAM_SIGNALS_CHAT_ID=
TELEGRAM_CORNIX_CHAT_ID=
TELEGRAM_REPORTS_CHAT_ID=
TELEGRAM_EXTERNAL_INBOX_CHAT_ID=
POSITION_WATCHER_COMMAND_MODE=report_only
POSITION_WATCHER_CORNIX_CHAT_ID=
POSITION_WATCHER_SEND_REPORT_COPY=1
POSITION_WATCHER_DRY_RUN=0
CORNIX_BREAKEVEN_FORMAT=v1
```

Routing:

- Signals: full scanner signal and chart
- Cornix: production-ready Cornix-format text
- Reports: daily summaries, performance reports, and position advisories
- External Inbox: incoming external messages are parsed by External Signal Refine V2
- External Signal Refine V2: only APPROVED forwarded VIP signals with scanner agreement can route to Signals/Cornix

For production channel routing, set the channel-specific IDs so Signals, Cornix, and Reports do not mix in one destination.

## Log And Data Files

```bash
cd /opt/Crypto-Multi-Coin-Scanner
tail -n 80 logs/cornix_agent.log
tail -n 25 logs/signals.csv
tail -n 25 logs/signals_history.csv
tail -n 25 logs/external_signals.csv
tail -n 220 logs/performance_report.txt
tail -n 30 logs/equity_curve.csv
```

Dashboard service check:

```bash
pgrep -af streamlit || echo "streamlit dashboard is not running"
```

## Health Check

Manual:

```bash
hostname
date
uptime
df -h /
cd /opt/Crypto-Multi-Coin-Scanner
.venv/bin/python system_status.py
test -f .env && echo ".env OK" || echo ".env MISSING"
.venv/bin/python -m compileall -q . && echo "compile OK"
.venv/bin/python tests/smoke_test.py
systemctl is-active crypto-scanner.service
systemctl is-active crypto-outcome-checker.timer
systemctl is-active crypto-daily-summary.timer
systemctl is-active crypto-external-inbox.service
```

Windows shortcut:

```bat
tools\health_check.bat
```

## Troubleshooting

SSH fails:

- Check `VPS_HOST`, `VPS_USER`, and `VPS_PORT`
- Confirm VPS firewall allows SSH
- Try `ssh -p 22 root@143.14.11.12`

Scanner inactive:

```bash
sudo systemctl restart crypto-scanner.service
journalctl -u crypto-scanner.service -n 200 --no-pager
```

No Telegram messages:

- Check `.env` on the VPS
- Confirm `SEND_TELEGRAM=1`
- Confirm `TELEGRAM_BOT_TOKEN`
- Confirm `TELEGRAM_CHAT_ID`
- Confirm channel IDs if using multi-channel routing
- Run `.venv/bin/python test_telegram.py`

No outcome alerts:

```bash
systemctl status crypto-outcome-checker.timer --no-pager
journalctl -u crypto-outcome-checker.service -n 200 --no-pager
```

No daily summary:

```bash
systemctl status crypto-daily-summary.timer --no-pager
journalctl -u crypto-daily-summary.service -n 200 --no-pager
cd /opt/Crypto-Multi-Coin-Scanner
.venv/bin/python daily_summary.py --dry-run
```

No external signals:

```bash
systemctl status crypto-external-inbox.service --no-pager
journalctl -u crypto-external-inbox.service -n 200 --no-pager
cd /opt/Crypto-Multi-Coin-Scanner
test -f logs/external_signals.csv && tail -n 25 logs/external_signals.csv || echo "external_signals.csv missing"
.venv/bin/python telegram_external_inbox.py
```

No performance report/dashboard:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
.venv/bin/python performance_report.py
.venv/bin/streamlit run dashboard.py --server.address 0.0.0.0 --server.port 8501
```
