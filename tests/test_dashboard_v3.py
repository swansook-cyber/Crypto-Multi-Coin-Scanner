"""Focused read-only dashboard V3 regression tests."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

import dashboard
from core.signal_identity import identity_from_record


class DashboardV3Tests(unittest.TestCase):
    def test_bangkok_time_and_local_day_boundary(self):
        self.assertEqual(dashboard._fmt_time("2026-09-27T23:36:00Z"), "06:36")
        self.assertEqual(dashboard._fmt_date("2026-09-27T23:36:00Z"), "28 Sep 2026")
        self.assertEqual(dashboard._fmt_utc_compact("2026-09-27T23:36:00Z"), "2026-09-27<br>23:36:00Z")
        rows = pd.DataFrame(
            [
                {"timestamp": "2026-09-27T16:59:59Z", "symbol": "OLD"},
                {"timestamp": "2026-09-27T17:00:00Z", "symbol": "TODAY"},
            ]
        )
        now = datetime(2026, 9, 28, 1, tzinfo=timezone.utc)
        self.assertEqual(dashboard._local_day_frame(rows, now)["symbol"].tolist(), ["TODAY"])

    def test_missing_execution_artifacts_are_unavailable_not_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot = dashboard.load_execution_truth_snapshot(
                {
                    "csv": root / "truth.csv",
                    "state": root / "state.json",
                    "evidence": root / "evidence.json",
                    "lock": root / "truth.lock",
                }
            )
        self.assertFalse(snapshot["available"])
        self.assertEqual(snapshot["collector_health"], "UNAVAILABLE")
        self.assertIsNone(snapshot["execution_pnl_usdt"])
        self.assertEqual(dashboard._execution_value(snapshot, "execution_pnl_usdt", " USDT"), "Collecting evidence")

    def test_execution_pnl_is_separate_and_funding_is_provisional(self):
        report = {
            "matched_scanner_lifecycles": 38,
            "ambiguous": 0,
            "invalid_accounting_evidence": {"rows": 0},
            "execution_complete_matched": {
                "rows": 3,
                "gross_realized_pnl_usdt": "16.00",
                "commission_usdt": "-0.49127554",
                "execution_pnl_usdt": "15.50872446",
                "gross_realized_r": "2.25",
                "execution_r": "2.18881917",
            },
            "funding_reconciled_matched": {
                "rows": 2,
                "funding_usdt": "-0.12",
                "funding_adjusted_pnl_usdt": "10.40",
            },
            "pending_execution_accounting": {"rows": 4},
        }
        health = {
            "overall": "OK",
            "metrics": {
                "last_success_utc": "2026-09-28T00:54:00Z",
                "last_success_age_seconds": 360,
                "state_age_seconds": 355,
                "invalid_count": 0,
                "ambiguous_count": 0,
            },
            "checks": {"artifacts": {"status": "OK", "detail": "readable"}},
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            csv_path = root / "truth.csv"
            state_path = root / "state.json"
            evidence_path = root / "evidence.json"
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["record_version", "data_source", "lifecycle_provenance"],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "record_version": "2",
                        "data_source": "BINANCE_USDM_PROSPECTIVE",
                        "lifecycle_provenance": "POST_BOUNDARY_AUTHORITATIVE",
                    }
                )
            state_path.write_text(
                json.dumps(
                    {
                        "prospective_start_utc": "2026-09-16T01:42:59Z",
                        "last_successful_collection_utc": "2026-09-28T00:54:00Z",
                        "endpoint_high_water": {"scanned_through_utc": "2026-09-28T00:54:00Z"},
                    }
                ),
                encoding="utf-8",
            )
            evidence_path.write_text("{}", encoding="utf-8")
            before = {path: path.read_bytes() for path in (csv_path, state_path, evidence_path)}
            with patch.object(dashboard, "report_records", return_value=report):
                snapshot = dashboard.load_execution_truth_snapshot(
                    {
                        "csv": csv_path,
                        "state": state_path,
                        "evidence": evidence_path,
                        "lock": root / "truth.lock",
                    },
                    health_result=health,
                )
            after = {path: path.read_bytes() for path in (csv_path, state_path, evidence_path)}

        self.assertEqual(before, after, "dashboard loader must not write collector artifacts")
        self.assertEqual(snapshot["collector_health"], "HEALTHY")
        self.assertEqual(snapshot["authoritative_n"], 3)
        self.assertEqual(snapshot["execution_pnl_usdt"], "15.50872446")
        self.assertEqual(snapshot["execution_net_r"], "2.18881917")
        self.assertEqual(snapshot["funding_adjusted_pnl_usdt"], "10.40")
        self.assertEqual(snapshot["invalid"], 0)
        self.assertEqual(snapshot["ambiguous"], 0)
        self.assertEqual(snapshot["last_success_age_seconds"], 360)
        self.assertEqual(snapshot["state_age_seconds"], 355)
        self.assertEqual(snapshot["provenance"], {"POST_BOUNDARY_AUTHORITATIVE": 1})
        self.assertEqual(dashboard._badge("PROVISIONAL").count("warning"), 1)

    def test_invalid_ambiguous_and_signal_linkage_are_visible(self):
        signal = {
            "timestamp": "2026-09-28T00:00:00Z",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "entry": "60000",
        }
        key = identity_from_record(signal).canonical_key
        self.assertEqual(
            dashboard.execution_status_for_signal(signal, [{"canonical_signal_key": key, "match_status": "MATCHED"}]),
            "MATCHED",
        )
        self.assertEqual(
            dashboard.execution_status_for_signal(
                signal,
                [
                    {
                        "canonical_signal_key": "",
                        "candidate_signal_keys": json.dumps([key, "id:v1:other"]),
                        "match_status": "AMBIGUOUS",
                    }
                ],
            ),
            "AMBIGUOUS",
        )
        self.assertEqual(
            dashboard.execution_status_for_signal(
                signal,
                [{"canonical_signal_key": key, "match_status": "UNMATCHED"}],
            ),
            "UNMATCHED",
        )
        self.assertEqual(dashboard.execution_status_for_signal(signal, []), "Waiting")
        self.assertIn("danger", dashboard._badge("INVALID"))
        self.assertIn("danger", dashboard._badge("AMBIGUOUS"))

    def test_failed_artifact_integrity_suppresses_authoritative_totals(self):
        report = {
            "matched_scanner_lifecycles": 1,
            "ambiguous": 0,
            "invalid_accounting_evidence": {"rows": 0},
            "execution_complete_matched": {
                "rows": 1,
                "gross_realized_pnl_usdt": "10",
                "commission_usdt": "-0.1",
                "execution_pnl_usdt": "9.9",
                "gross_realized_r": "1",
                "execution_r": "0.99",
            },
            "funding_reconciled_matched": {
                "rows": 0,
                "funding_usdt": None,
                "funding_adjusted_pnl_usdt": None,
            },
            "pending_execution_accounting": {"rows": 0},
        }
        failed_health = {
            "overall": "FAIL",
            "metrics": {"invalid_count": None, "ambiguous_count": None},
            "checks": {"artifacts": {"status": "FAIL", "detail": "evidence: JSONDecodeError"}},
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            csv_path = root / "truth.csv"
            csv_path.write_text("record_version,data_source\n", encoding="utf-8")
            state_path = root / "state.json"
            state_path.write_text('{"prospective_start_utc":"2026-09-16T00:00:00Z"}', encoding="utf-8")
            evidence_path = root / "evidence.json"
            evidence_path.write_text("{corrupt", encoding="utf-8")
            with patch.object(dashboard, "report_records", return_value=report):
                snapshot = dashboard.load_execution_truth_snapshot(
                    {
                        "csv": csv_path,
                        "state": state_path,
                        "evidence": evidence_path,
                        "lock": root / "truth.lock",
                    },
                    health_result=failed_health,
                )
        self.assertFalse(snapshot["available"])
        self.assertEqual(snapshot["collector_health"], "FAILED")
        self.assertIsNone(snapshot["execution_pnl_usdt"])
        self.assertIn("evidence: JSONDecodeError", snapshot["message"])

    def test_authoritative_sample_zero_never_renders_proven_zero(self):
        snapshot = dashboard._empty_execution_snapshot(dashboard.EXECUTION_PATHS)
        snapshot.update({"available": True, "authoritative_n": 0, "execution_pnl_usdt": "0"})
        self.assertEqual(dashboard._execution_value(snapshot, "execution_pnl_usdt", " USDT"), "Collecting evidence")


if __name__ == "__main__":
    unittest.main()
