# VPS Commands

This project is still an internal lab signal assistant. It does not auto-trade.

## Windows Setup

The files in `tools/` use Windows OpenSSH. On Windows, confirm SSH works:

```bat
ssh -V
```

Set your VPS connection once:

```bat
setx VPS_HOST your.vps.ip.address
setx VPS_USER root
setx VPS_PORT 22
setx VPS_APP_DIR /opt/Crypto-Multi-Coin-Scanner
```

Close and reopen Command Prompt after `setx`.

You can also run any BAT directly and enter the VPS host when prompted.

## VPS Login

Manual login:

```bat
ssh root@your.vps.ip.address
```

If using a non-standard SSH port:

```bat
ssh -p 2222 root@your.vps.ip.address
```

## Easy Windows Tools

Run from the project folder:

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

Every tool connects to the VPS, runs one command, then pauses before exit.

## Service Management

Scanner:

```bash
sudo systemctl status crypto-scanner.service --no-pager
sudo systemctl restart crypto-scanner.service
journalctl -u crypto-scanner.service -n 140 --no-pager
```

Outcome checker:

```bash
sudo systemctl status crypto-outcome-checker.timer --no-pager
sudo systemctl status crypto-outcome-checker.service --no-pager
sudo systemctl restart crypto-outcome-checker.timer
systemctl list-timers crypto-outcome-checker.timer --no-pager
```

Daily summary:

```bash
sudo systemctl status crypto-daily-summary.timer --no-pager
sudo systemctl status crypto-daily-summary.service --no-pager
sudo systemctl restart crypto-daily-summary.timer
```

## Git Update

Manual update on the VPS:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
git pull origin main
.venv/bin/python -m compileall -q .
sudo systemctl restart crypto-scanner.service
sudo systemctl restart crypto-outcome-checker.timer
sudo systemctl restart crypto-daily-summary.timer
```

Windows shortcut:

```bat
tools\git_pull_update.bat
```

## Log Viewing

Scanner logs:

```bash
journalctl -u crypto-scanner.service -n 140 --no-pager
```

Outcome checker logs:

```bash
journalctl -u crypto-outcome-checker.service -n 140 --no-pager
```

Project files:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
tail -n 25 logs/signals_history.csv
tail -n 30 logs/equity_curve.csv
tail -n 220 logs/performance_report.txt
```

## Troubleshooting

SSH fails:

- Check `VPS_HOST`, `VPS_USER`, and `VPS_PORT`.
- Confirm the VPS firewall allows SSH.
- Try manual login with `ssh -p %VPS_PORT% %VPS_USER%@%VPS_HOST%`.

Service is inactive:

```bash
sudo systemctl restart crypto-scanner.service
journalctl -u crypto-scanner.service -n 200 --no-pager
```

No Telegram messages:

- Check `.env` on the VPS.
- Confirm `SEND_TELEGRAM=1`.
- Confirm `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
- Run `python test_telegram.py` on the VPS.

No performance report:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
.venv/bin/python review_signals.py --dry-run
.venv/bin/python stats_dashboard.py
```

Run a full health check from Windows:

```bat
tools\health_check.bat
```
