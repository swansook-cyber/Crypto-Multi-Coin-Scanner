"""Offline regression tests for the execution-truth health diagnostic."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from core.binance_execution_truth_health import (
    FAIL,
    OK,
    WARNING,
    HealthConfig,
    SystemdSnapshot,
    evaluate_health,
    format_human,
)


NOW = datetime(2026, 9, 27, 8, 30, tzinfo=timezone.utc)
BOUNDARY = "2026-09-16T01:42:59.911337Z"
CSV_FIELDS = [
    "match_status", "accounting_evidence_status", "accounting_issues",
    "data_source", "prospective_start_utc",
]


def systemd(**changes) -> SystemdSnapshot:
    values = {
        "timer_active_state": "active",
        "timer_sub_state": "waiting",
        "service_active_state": "inactive",
        "service_sub_state": "dead",
        "service_result": "success",
        "service_exit_status": 0,
    }
    values.update(changes)
    return SystemdSnapshot(**values)


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.state = root / "state.json"
        self.csv = root / "truth.csv"
        self.evidence = root / "truth.evidence.json"
        self.lock = root / "truth.lock"
        self.lock.write_bytes(b"0")
        self._write_state(NOW - timedelta(minutes=11))
        self.evidence.write_text(
            json.dumps({"collector_version": "2.2.0", "prospective_start_utc": BOUNDARY}),
            encoding="utf-8",
        )
        self._write_rows()

    def tearDown(self):
        self.temp.cleanup()

    def _write_state(self, last_success: datetime, *, scanned: datetime | None = None):
        scanned = scanned or last_success
        value = {
            "collector_version": "2.2.0",
            "prospective_start_utc": BOUNDARY,
            "last_successful_collection_utc": last_success.isoformat().replace("+00:00", "Z"),
            "endpoint_high_water": {
                "scanned_through_utc": scanned.isoformat().replace("+00:00", "Z")
            },
            "pending_income": {},
        }
        self.state.write_text(json.dumps(value), encoding="utf-8")

    def _write_rows(self, *rows):
        with self.csv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for row in rows:
                value = {
                    "match_status": "MATCHED",
                    "accounting_evidence_status": "ACCOUNTING_EVIDENCE_VALID",
                    "accounting_issues": "",
                    "data_source": "BINANCE_USDM_PROSPECTIVE",
                    "prospective_start_utc": BOUNDARY,
                }
                value.update(row)
                writer.writerow(value)

    def _config(self) -> HealthConfig:
        return HealthConfig(
            state_path=self.state,
            csv_path=self.csv,
            evidence_path=self.evidence,
            lock_path=self.lock,
        )

    def _run(self, *, snapshot=None, lock_free=True):
        return evaluate_health(
            self._config(),
            now=NOW,
            systemd_snapshot=snapshot or systemd(),
            lock_probe=lambda _path: lock_free,
        )

    def test_fresh_healthy_state(self):
        result = self._run()
        self.assertEqual(result.overall, OK)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Overall: OK", format_human(result))

    def test_machine_readable_projection_is_json_serializable(self):
        payload = json.loads(json.dumps(self._run().as_dict()))
        self.assertEqual(payload["overall"], OK)
        self.assertEqual(payload["metrics"]["invalid_count"], 0)
        self.assertEqual(payload["checks"]["timer"]["status"], OK)

    def test_last_success_older_than_30_minutes(self):
        self._write_state(NOW - timedelta(minutes=31))
        result = self._run()
        self.assertEqual(result.checks["last_success"].status, FAIL)
        self.assertEqual(result.exit_code, 1)

    def test_timer_inactive(self):
        result = self._run(snapshot=systemd(timer_active_state="inactive", timer_sub_state="dead"))
        self.assertEqual(result.checks["timer"].status, FAIL)

    def test_failed_service_result(self):
        result = self._run(snapshot=systemd(service_result="exit-code", service_exit_status=1))
        self.assertEqual(result.checks["service_result"].status, FAIL)

    def test_stale_lock(self):
        result = self._run(lock_free=False)
        self.assertEqual(result.checks["lock"].status, FAIL)

    def test_busy_lock_is_expected_while_service_runs(self):
        result = self._run(
            snapshot=systemd(service_active_state="activating", service_sub_state="start"),
            lock_free=False,
        )
        self.assertEqual(result.checks["lock"].status, OK)

    def test_missing_state(self):
        self.state.unlink()
        result = self._run()
        self.assertEqual(result.checks["artifacts"].status, FAIL)
        self.assertEqual(result.checks["state_advancing"].status, FAIL)

    def test_invalid_count_above_baseline(self):
        self._write_rows({"accounting_evidence_status": "ACCOUNTING_EVIDENCE_INVALID"})
        result = self._run()
        self.assertEqual(result.metrics["invalid_count"], 1)
        self.assertEqual(result.checks["invalid"].status, FAIL)

    def test_ambiguous_count_increase(self):
        self._write_rows({"match_status": "AMBIGUOUS"})
        result = self._run()
        self.assertEqual(result.metrics["ambiguous_count"], 1)
        self.assertEqual(result.checks["ambiguous"].status, WARNING)
        self.assertEqual(result.exit_code, 1)

    def test_state_high_water_stopped_advancing(self):
        self._write_state(
            NOW - timedelta(minutes=2),
            scanned=NOW - timedelta(minutes=45),
        )
        result = self._run()
        self.assertEqual(result.checks["last_success"].status, OK)
        self.assertEqual(result.checks["state_advancing"].status, FAIL)

    def test_corrupt_json(self):
        self.state.write_text("{not-json", encoding="utf-8")
        result = self._run()
        self.assertEqual(result.checks["artifacts"].status, FAIL)

    def test_missing_csv_and_evidence(self):
        for missing in (self.csv, self.evidence):
            with self.subTest(missing=missing.name):
                self._write_rows()
                self.evidence.write_text(
                    json.dumps({"collector_version": "2.2.0", "prospective_start_utc": BOUNDARY}),
                    encoding="utf-8",
                )
                missing.unlink()
                result = self._run()
                self.assertEqual(result.checks["artifacts"].status, FAIL)


if __name__ == "__main__":
    unittest.main()
