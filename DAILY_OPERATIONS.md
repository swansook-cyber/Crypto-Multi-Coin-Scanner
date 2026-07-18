# Daily Operations Checklist

Use this checklist on the VPS. These commands are operational checks only and do not create trades.

## Morning

Check services:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
systemctl status crypto-scanner --no-pager
systemctl status crypto-position-watcher --no-pager
systemctl status crypto-performance-report.timer --no-pager
systemctl list-timers crypto-performance-report.timer --no-pager
```

Run health command:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
.venv/bin/python production_health.py
.venv/bin/python production_v1_readiness.py
```

Check previous executive report delivery:

```bash
journalctl -u crypto-performance-report.service -n 80 --no-pager
```

Check disk:

```bash
df -h /opt/Crypto-Multi-Coin-Scanner
```

## During Day

Verify Signals and Cornix delivery:

```bash
journalctl -u crypto-scanner -n 120 --no-pager
grep -i "sent_to_signals\\|sent_to_cornix\\|signal_status" logs/signals.csv | tail -20
```

Verify no duplicate TP1 / NEW STOP alerts:

```bash
journalctl -u crypto-position-watcher -n 160 --no-pager | grep -Ei "TP1|NEW STOP|DUPLICATE|breakeven"
```

Verify Entry Timing rows are increasing:

```bash
wc -l logs/entry_timing_engine.csv
.venv/bin/python entry_timing_operational_summary.py
.venv/bin/python position_watcher_state_cleanup.py
```

## Evening

Run Daily Signal Summary / Executive Performance Report:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
.venv/bin/python daily_summary.py --dry-run
.venv/bin/python performance_report.py --executive
.venv/bin/python performance_report.py --send
```

Back up runtime data:

```bash
.venv/bin/python backup_runtime_data.py
.venv/bin/python data_integrity_audit.py
.venv/bin/python position_watcher_state_cleanup.py
```

Run cleanup apply only after reviewing dry-run output:

```bash
.venv/bin/python position_watcher_state_cleanup.py --apply
```

Never use `--apply` without reviewing the listed stale state keys.

Check recent errors:

```bash
journalctl -u crypto-scanner -n 200 --no-pager | grep -Ei "traceback|error|exception" || true
journalctl -u crypto-position-watcher -n 200 --no-pager | grep -Ei "traceback|error|exception" || true
journalctl -u crypto-performance-report.service -n 120 --no-pager | grep -Ei "traceback|error|exception|failed" || true
```

## Update Procedure

Use the guarded update script:

```bash
cd /opt/Crypto-Multi-Coin-Scanner
./scripts/update_production.sh
```

If validation fails, do not restart services manually. Review output and rollback if needed:

```bash
./scripts/rollback_production.sh <known-good-commit>
```
