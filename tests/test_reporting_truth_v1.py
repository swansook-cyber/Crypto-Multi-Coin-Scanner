"""Focused Reporting Truth V1 regression coverage; no production files used."""

from __future__ import annotations

import json
import argparse
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from core.performance_analytics_v1 import build_complete_report
from core.performance_analytics_v3 import build_performance_v3
import performance_report
from core.reporting_truth import (
    ANALYTICS_V3,
    LIVE_SENT_PERFORMANCE,
    PERFORMANCE_V1,
    build_snapshot_manifest,
    canonical_signal_keys,
    live_sent_rows,
    research_all_status_rows,
    routing_snapshot_from_config,
    stable_sha256,
    write_snapshot_manifest,
)


class _Config:
    watchlist = ["BTCUSDT", "ETHUSDT"]
    watchlist_tiers = {"BTCUSDT": "A", "ETHUSDT": "B"}
    enable_tier_c_report_only = True
    weak_symbol_report_only_symbols = ["BNBUSDT"]
    session_report_only_sessions = ["NewYork"]
    london_long_report_only = True


def _row(symbol: str, status: str, result: str, target: str, timestamp: str) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "symbol": symbol,
        "side": "LONG",
        "signal_status": status,
        "result": result,
        "hit_target": target,
        "risk_reward": 2.0,
        "entry": 100.0,
    }


class ReportingTruthV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = pd.DataFrame(
            [
                _row("BTCUSDT", "sent", "WIN", "TP2", "2026-09-05T00:00:00Z"),
                _row("ETHUSDT", "sent", "LOSS", "SL", "2026-09-05T01:00:00Z"),
                _row("BNBUSDT", "weak_symbol_report_only", "WIN", "TP2", "2026-09-05T02:00:00Z"),
            ]
        )

    def test_sent_only_and_research_populations_are_distinct(self) -> None:
        sent = live_sent_rows(self.rows)
        research = research_all_status_rows(self.rows)
        self.assertEqual(sent["symbol"].tolist(), ["BTCUSDT", "ETHUSDT"])
        self.assertEqual(len(research), 3)
        report, _ = build_complete_report(self.rows, pd.DataFrame(), pd.DataFrame(), "2026-09-05")
        self.assertEqual(report["production_population"], LIVE_SENT_PERFORMANCE)
        self.assertEqual(report["production_formula_version"], PERFORMANCE_V1)
        self.assertEqual(report["research_formula_version"], ANALYTICS_V3)
        self.assertEqual(report["closed_signals"], 2)
        self.assertEqual(report["net_r_estimate"], 1.0)  # unchanged V1 arithmetic: +2R -1R

    def test_report_only_bnb_can_rank_for_research_without_changing_routing(self) -> None:
        rows = []
        for index in range(5):
            rows.append(_row("BNBUSDT", "weak_symbol_report_only", "WIN", "TP2", f"2026-09-05T0{index}:00:00Z"))
            rows.append(_row("BTCUSDT", "sent", "LOSS", "SL", f"2026-09-05T1{index}:00:00Z"))
        ranking = build_performance_v3(pd.DataFrame(rows))["production_universe_ranking"]
        self.assertIn("BNBUSDT", ranking["Symbol"].tolist())
        routing = routing_snapshot_from_config(_Config())
        self.assertNotIn("BNBUSDT", routing["symbols"])
        self.assertEqual(routing["tiers"], {"BTCUSDT": "A", "ETHUSDT": "B"})

    def test_snapshot_evidence_identity_is_date_scoped_and_secret_safe(self) -> None:
        keys = canonical_signal_keys(self.rows.iloc[[1, 0]])
        self.assertEqual(keys, canonical_signal_keys(self.rows.iloc[[0, 1]]))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "signals.csv"
            self.rows.to_csv(source, index=False)
            routing = routing_snapshot_from_config(_Config())
            manifest = build_snapshot_manifest(
                report_date_utc="2026-09-05",
                source_path=source,
                source_rows=self.rows,
                routing=routing,
                output_hashes={"full_report_text_sha256": stable_sha256("fixture")},
                generated_at_utc="2026-09-05T12:00:00Z",
            )
            same_evidence = build_snapshot_manifest(
                report_date_utc="2026-09-05",
                source_path=source,
                source_rows=self.rows,
                routing=routing,
                output_hashes={"full_report_text_sha256": stable_sha256("formatting-only change")},
                generated_at_utc="2026-09-05T13:00:00Z",
            )
            same_evidence["git_commit"] = "different-provenance-only-commit"
            self.assertEqual(manifest["content_sha256"], same_evidence["content_sha256"])
            serialized = json.dumps(manifest)
            self.assertNotIn("TELEGRAM_BOT_TOKEN", serialized)
            self.assertNotIn("token", serialized.lower())
            snapshots = root / "snapshots"
            self.assertEqual(write_snapshot_manifest(manifest, snapshots).status, "created")
            original_bytes = (snapshots / "2026-09-05.json").read_bytes()
            self.assertEqual(write_snapshot_manifest(same_evidence, snapshots).status, "identical")

            # A source-file append outside the report date changes file SHA but
            # not the date-scoped normalized evidence identity.
            next_day = _row("SOLUSDT", "sent", "WIN", "TP1", "2026-09-06T00:00:00Z")
            all_rows = pd.concat([self.rows, pd.DataFrame([next_day])], ignore_index=True)
            all_rows.to_csv(source, index=False)
            unrelated_append = build_snapshot_manifest(
                report_date_utc="2026-09-05",
                source_path=source,
                source_rows=self.rows,
                routing=routing,
                source_file_row_count=len(all_rows),
                generated_at_utc="2026-09-05T14:00:00Z",
            )
            self.assertEqual(write_snapshot_manifest(unrelated_append, snapshots).status, "identical")

            changed_rows = self.rows.copy()
            changed_rows.loc[0, "result"] = "LOSS"
            changed = build_snapshot_manifest(
                report_date_utc="2026-09-05",
                source_path=source,
                source_rows=changed_rows,
                routing=routing,
                generated_at_utc="2026-09-05T15:00:00Z",
            )
            self.assertNotEqual(changed["content_sha256"], manifest["content_sha256"])
            first_conflict = write_snapshot_manifest(changed, snapshots)
            self.assertEqual(first_conflict.status, "conflict")
            self.assertEqual(write_snapshot_manifest(changed, snapshots).status, "conflict")
            self.assertEqual(len(list(snapshots.glob("2026-09-05.conflict-*.json"))), 1)
            self.assertEqual((snapshots / "2026-09-05.json").read_bytes(), original_bytes)
            conflict = json.loads(first_conflict.conflict_path.read_text())
            self.assertIn("evidence_identity_sha256", conflict["conflict"]["reason"])

            changed_population = self.rows.copy()
            changed_population.loc[2, "signal_status"] = "sent"
            self.assertNotEqual(
                build_snapshot_manifest(
                    report_date_utc="2026-09-05", source_path=source, source_rows=changed_population, routing=routing
                )["content_sha256"],
                manifest["content_sha256"],
            )

    def test_snapshot_conflict_and_all_view_do_not_block_reporting_or_telegram(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = root / "signals.csv"
            history = root / "history.csv"
            external = root / "external.csv"
            entry_timing = root / "entry_timing.csv"
            snapshots = root / "snapshots"
            logs = root / "logs"
            reports = root / "reports"
            self.rows.to_csv(journal, index=False)
            pd.DataFrame().to_csv(history, index=False)
            pd.DataFrame().to_csv(external, index=False)
            pd.DataFrame().to_csv(entry_timing, index=False)
            initial = build_snapshot_manifest(
                report_date_utc="2026-09-05", source_path=journal, source_rows=self.rows, routing=routing_snapshot_from_config(_Config())
            )
            write_snapshot_manifest(initial, snapshots)
            changed_rows = self.rows.copy()
            changed_rows.loc[0, "result"] = "LOSS"
            changed_rows.to_csv(journal, index=False)

            calls: list[dict[str, str]] = []

            class FakeSession:
                def post(self, _url: str, data: dict[str, str] | None = None, timeout: int | None = None):
                    calls.append(data or {})

                    class Response:
                        status_code = 200
                        text = "ok"

                    return Response()

            args = argparse.Namespace(
                date="2026-09-05", send=True, executive=False, test_report=False,
                journal=journal, history=history, external=external, entry_timing=entry_timing, snapshot_dir=snapshots,
            )
            old_token = os.environ.get("TELEGRAM_BOT_TOKEN")
            old_reports = os.environ.get("TELEGRAM_REPORTS_CHAT_ID")
            os.environ["TELEGRAM_BOT_TOKEN"] = "fixture-token"
            os.environ["TELEGRAM_REPORTS_CHAT_ID"] = "fixture-reports"
            output = io.StringIO()
            try:
                with (
                    patch.object(performance_report, "LOGS_DIR", logs),
                    patch.object(performance_report, "REPORTS_DIR", reports),
                    patch.object(performance_report, "write_full_web_report"),
                    contextlib.redirect_stdout(output),
                ):
                    self.assertEqual(performance_report.run_report(args, session=FakeSession()), 0)
                self.assertTrue(calls)
                self.assertIn("Daily Performance Report", output.getvalue())
                self.assertTrue(list(snapshots.glob("2026-09-05.conflict-*.json")))

                all_args = argparse.Namespace(**{**vars(args), "date": "ALL", "send": False})
                with (
                    patch.object(performance_report, "LOGS_DIR", logs),
                    patch.object(performance_report, "REPORTS_DIR", reports),
                    patch.object(performance_report, "write_full_web_report"),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(performance_report.run_report(all_args, session=FakeSession()), 0)
                self.assertFalse((snapshots / "ALL.json").exists())
            finally:
                if old_token is None:
                    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
                else:
                    os.environ["TELEGRAM_BOT_TOKEN"] = old_token
                if old_reports is None:
                    os.environ.pop("TELEGRAM_REPORTS_CHAT_ID", None)
                else:
                    os.environ["TELEGRAM_REPORTS_CHAT_ID"] = old_reports


if __name__ == "__main__":
    unittest.main()
