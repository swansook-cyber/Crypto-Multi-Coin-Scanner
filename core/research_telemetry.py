"""Unified, fail-open research telemetry for scanner candidate evidence.

This module is observational infrastructure.  It does not participate in live
selection, scoring, routing, risk, or order execution.  Candidate snapshots are
immutable; outcome and execution facts are enriched into separate tables.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import os
import sqlite3
import statistics
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from core.signal_identity import canonical_signal_key, normalize_side, normalize_symbol, normalize_timestamp


LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 2
FEATURE_SCHEMA_VERSION = 1
DEFAULT_DB_PATH = Path("research/scanner_research_v1.db")
VALID_SOURCE_MODES = {"PROSPECTIVE", "HISTORICAL_BACKFILL"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def _float(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _float(value)
    return int(number) if number is not None else None


def _bool_int(value: Any) -> int | None:
    if value is None or _text(value) == "":
        return None
    if isinstance(value, bool):
        return int(value)
    text = _text(value).lower()
    if text in {"1", "true", "yes", "y", "on", "hit"}:
        return 1
    if text in {"0", "false", "no", "n", "off"}:
        return 0
    return None


def _outcome_evidence_rank(result: Any, resolved_at: Any = "") -> int:
    normalized = _text(result).upper()
    if normalized == "LOSS" or normalized.startswith("WIN"):
        return 3
    if normalized not in {"", "OPEN"} or _text(resolved_at):
        return 2
    return 1 if normalized == "OPEN" else 0


def _execution_evidence_rank(status: Any, finality: Any, authoritative: Any) -> int:
    if _bool_int(authoritative):
        return 4
    normalized_finality = _text(finality).upper()
    if normalized_finality in {"EXECUTION_FINAL", "ACCOUNTING_FINAL"}:
        return 3
    normalized_status = _text(status).upper()
    if normalized_status in {"MATCHED", "PARTIAL", "AMBIGUOUS"}:
        return 2
    return 1 if normalized_status else 0


def _json(value: Any) -> str:
    if value in (None, "", {}, []):
        return "{}"
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except json.JSONDecodeError:
            return json.dumps({"value": value}, ensure_ascii=True, sort_keys=True)
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)


def normalize_utc(value: Any) -> str:
    text = normalize_timestamp(value, precision="second")
    return text or _text(value)


def _utc_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def make_run_id(scan_started_at_utc: Any, scan_candle_time_utc: Any = "") -> str:
    started = normalize_utc(scan_started_at_utc)
    candle = normalize_utc(scan_candle_time_utc)
    # The scheduled closed candle is the logical run identity.  A process
    # restart that replays that candle remains idempotent; later candles are
    # distinct runs.  Ad-hoc callers without a candle fall back to start time.
    logical_run = candle or started
    digest = hashlib.sha256(f"run-v1|{logical_run}".encode()).hexdigest()[:20]
    return f"run_v1_{digest}"


def make_candidate_key(
    *,
    run_id: str,
    symbol: Any,
    side: Any,
    closed_candle_time_utc: Any,
    candidate_stage: str = "FINAL_CANDIDATE",
    version: str = "v1",
) -> str:
    """Deterministic identity for one candidate event in one scanner run."""
    parts = (
        version,
        _text(run_id),
        normalize_symbol(symbol),
        normalize_side(side),
        normalize_utc(closed_candle_time_utc),
        _text(candidate_stage).upper() or "FINAL_CANDIDATE",
    )
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return f"candidate_{version}_{digest}"


def make_observation_key(
    *,
    run_id: str,
    symbol: Any,
    scan_candle_utc: Any,
    stage: str,
    category: str,
    side_hint: Any = "",
    version: str = "v1",
) -> str:
    parts = (
        version,
        _text(run_id),
        normalize_symbol(symbol),
        normalize_utc(scan_candle_utc),
        _text(stage).upper(),
        normalize_side(side_hint),
        _text(category).upper(),
    )
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return f"observation_{version}_{digest}"


@dataclass(frozen=True)
class ShadowDecision:
    name: str
    version: str
    decision: str
    reason: str = ""
    metrics: Mapping[str, Any] = field(default_factory=dict)
    evaluated_at_utc: str = ""


@dataclass(frozen=True)
class CandidateSnapshot:
    run_id: str
    timestamp_utc: str
    closed_candle_time_utc: str
    symbol: str
    side: str
    decision: str
    decision_reason: str = ""
    decision_stage: str = "FINAL_DECISION"
    signal_status: str = ""
    candidate_stage: str = "FINAL_CANDIDATE"
    canonical_signal_key: str = ""
    entry: float | None = None
    sl: float | None = None
    tp1: float | None = None
    tp2: float | None = None
    rr: float | None = None
    score: float | None = None
    confidence: float | None = None
    setup_strength: float | None = None
    tier: str = ""
    session: str = ""
    source_mode: str = "PROSPECTIVE"
    source_name: str = "scanner"
    source_version: str = "UNIFIED_SCANNER_RESEARCH_TELEMETRY_V1"
    features: Mapping[str, Any] = field(default_factory=dict)
    market: Mapping[str, Any] = field(default_factory=dict)
    exposure: Mapping[str, Any] = field(default_factory=dict)
    shadows: Sequence[ShadowDecision] = field(default_factory=tuple)
    candidate_key: str = ""

    def resolved_key(self) -> str:
        return self.candidate_key or make_candidate_key(
            run_id=self.run_id,
            symbol=self.symbol,
            side=self.side,
            closed_candle_time_utc=self.closed_candle_time_utc,
            candidate_stage=self.candidate_stage,
        )


@dataclass(frozen=True)
class PreCandidateObservation:
    run_id: str
    timestamp_utc: str
    scan_candle_utc: str
    symbol: str
    stage: str
    category: str
    outcome: str = "WAIT"
    reason: str = ""
    side_hint: str = ""
    score_long: float | None = None
    score_short: float | None = None
    confidence: float | None = None
    rsi: float | None = None
    mfi: float | None = None
    atr: float | None = None
    btc_regime: str = ""
    trend_1h: str = ""
    trend_4h: str = ""
    session: str = ""
    extras: Mapping[str, Any] = field(default_factory=dict)
    source_mode: str = "PROSPECTIVE"
    source_name: str = "scanner"
    source_version: str = "UNIFIED_SCANNER_RESEARCH_TELEMETRY_V1"
    observation_key: str = ""

    def resolved_key(self) -> str:
        return self.observation_key or make_observation_key(
            run_id=self.run_id,
            symbol=self.symbol,
            scan_candle_utc=self.scan_candle_utc,
            stage=self.stage,
            category=self.category,
            side_hint=self.side_hint,
        )


class ResearchEvaluatorRegistry:
    """Small registry for observational rules evaluated from stored snapshots."""

    def __init__(self) -> None:
        self._evaluators: dict[str, Callable[[CandidateSnapshot], ShadowDecision | None]] = {}

    def register(self, name: str, evaluator: Callable[[CandidateSnapshot], ShadowDecision | None]) -> None:
        self._evaluators[name] = evaluator

    def evaluate(self, snapshot: CandidateSnapshot) -> list[ShadowDecision]:
        decisions: list[ShadowDecision] = []
        for name, evaluator in self._evaluators.items():
            try:
                result = evaluator(snapshot)
                if result is not None:
                    decisions.append(result)
            except Exception as exc:
                LOGGER.warning("Research evaluator %s failed: %s", name, exc)
        return decisions


def default_evaluator_registry() -> ResearchEvaluatorRegistry:
    registry = ResearchEvaluatorRegistry()

    def setup_strength(snapshot: CandidateSnapshot) -> ShadowDecision | None:
        value = _float(snapshot.setup_strength)
        if value is None:
            return None
        decision = "LOW_SETUP_SHADOW" if value <= 79 else "NORMAL_SETUP_SHADOW"
        return ShadowDecision("setup_strength_v1", "1", decision, metrics={"setup_strength": value})

    def captured_state(name: str, field_name: str) -> Callable[[CandidateSnapshot], ShadowDecision | None]:
        def evaluate(snapshot: CandidateSnapshot) -> ShadowDecision | None:
            value = _text(snapshot.features.get(field_name) or snapshot.exposure.get(field_name))
            if not value:
                return None
            return ShadowDecision(name, "1", value, metrics={field_name: value})
        return evaluate

    registry.register("setup_strength_v1", setup_strength)
    registry.register("sr_weight_v1", captured_state("sr_weight_v1", "sr_state"))
    registry.register("exhaustion_v1", captured_state("exhaustion_v1", "exhaustion_state"))
    registry.register("entry_timing_v1", captured_state("entry_timing_v1", "entry_timing_state"))
    registry.register("cross_scan_exposure_v1", captured_state("cross_scan_exposure_v1", "cross_scan_state"))
    registry.register("cluster_representative_v1", captured_state("cluster_representative_v1", "representative_signal_key"))
    return registry


MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS research_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL,
    prospective_start_utc TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    last_migration_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scanner_runs (
    run_id TEXT PRIMARY KEY,
    scan_started_at_utc TEXT NOT NULL,
    scan_candle_time_utc TEXT,
    btc_regime TEXT,
    market_context_version TEXT,
    source_mode TEXT NOT NULL CHECK (source_mode IN ('PROSPECTIVE','HISTORICAL_BACKFILL')),
    source_name TEXT NOT NULL,
    source_version TEXT,
    imported_at_utc TEXT
);
CREATE TABLE IF NOT EXISTS candidates (
    candidate_key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES scanner_runs(run_id),
    timestamp_utc TEXT NOT NULL,
    closed_candle_time_utc TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    candidate_stage TEXT NOT NULL,
    decision TEXT NOT NULL,
    decision_stage TEXT,
    decision_reason TEXT,
    signal_status TEXT,
    canonical_signal_key TEXT,
    entry REAL,
    sl REAL,
    tp1 REAL,
    tp2 REAL,
    rr REAL,
    score REAL,
    confidence REAL,
    setup_strength REAL,
    tier TEXT,
    session TEXT,
    source_mode TEXT NOT NULL CHECK (source_mode IN ('PROSPECTIVE','HISTORICAL_BACKFILL')),
    source_name TEXT NOT NULL,
    source_version TEXT,
    imported_at_utc TEXT,
    recorded_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidate_features (
    candidate_key TEXT PRIMARY KEY REFERENCES candidates(candidate_key) ON DELETE CASCADE,
    feature_schema_version INTEGER NOT NULL,
    rsi REAL, mfi REAL, atr REAL, atr_pct REAL, atr_expansion REAL,
    ema9 REAL, ema20 REAL, ema21 REAL, ema50 REAL,
    ema20_distance_atr REAL, ema50_distance_atr REAL,
    body_ratio REAL, upper_wick_ratio REAL, lower_wick_ratio REAL, opposite_wick_ratio REAL,
    volume REAL, volume_ratio REAL, momentum REAL,
    breakout_confirmed INTEGER, wave_score REAL, wave_state TEXT, wave_phase TEXT,
    entry_timing_state TEXT, sr_state TEXT, exhaustion_state TEXT,
    extras_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS market_context (
    candidate_key TEXT PRIMARY KEY REFERENCES candidates(candidate_key) ON DELETE CASCADE,
    btc_regime TEXT, trend_1h TEXT, trend_4h TEXT, volatility_context TEXT,
    btc_side_alignment TEXT, long_short_mix TEXT, utc_hour INTEGER, local_hour INTEGER,
    day_of_week TEXT, session TEXT, market_extras_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS exposure_snapshots (
    candidate_key TEXT PRIMARY KEY REFERENCES candidates(candidate_key) ON DELETE CASCADE,
    actual_open_total INTEGER, actual_open_same_side INTEGER, actual_open_opposite_side INTEGER,
    same_side_altcoin_open_count INTEGER, same_scan_long_count INTEGER, same_scan_short_count INTEGER,
    same_scan_side_count INTEGER, prior_same_side_1h INTEGER, prior_same_side_3h INTEGER,
    prior_same_side_6h INTEGER, cumulative_same_side_modeled_risk REAL,
    candidate_sequence_after_first_exposure INTEGER, open_position_ages_json TEXT,
    cross_scan_same_side_count INTEGER, correlated_open_count INTEGER, max_pair_correlation REAL,
    cluster_id TEXT, representative_signal_key TEXT, cross_scan_state TEXT,
    exposure_extras_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS shadow_decisions (
    candidate_key TEXT NOT NULL REFERENCES candidates(candidate_key) ON DELETE CASCADE,
    shadow_name TEXT NOT NULL,
    shadow_version TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    evaluated_at_utc TEXT NOT NULL,
    PRIMARY KEY (candidate_key, shadow_name, shadow_version)
);
CREATE TABLE IF NOT EXISTS signal_outcomes (
    canonical_signal_key TEXT PRIMARY KEY,
    result TEXT, modeled_r REAL, resolved_at_utc TEXT,
    tp1_hit INTEGER, tp2_hit INTEGER, sl_hit INTEGER, time_to_resolution_sec REAL,
    source_name TEXT, source_version TEXT, last_enriched_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS execution_truth_links (
    canonical_signal_key TEXT PRIMARY KEY,
    execution_status TEXT, execution_finality TEXT, authoritative_eligible INTEGER,
    actual_entry_vwap REAL, actual_exit_vwap REAL,
    gross_realized_pnl REAL, commission REAL, execution_pnl REAL,
    gross_r REAL, execution_net_r REAL, funding_state TEXT,
    source_name TEXT, source_version TEXT, last_enriched_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enrichment_status (
    source_name TEXT PRIMARY KEY,
    last_success_utc TEXT, high_water TEXT, last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_candidates_run ON candidates(run_id);
CREATE INDEX IF NOT EXISTS idx_candidates_time ON candidates(timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_candidates_population ON candidates(source_mode, decision, side, session);
CREATE INDEX IF NOT EXISTS idx_candidates_signal_key ON candidates(canonical_signal_key);
CREATE INDEX IF NOT EXISTS idx_candidates_symbol_side_candle ON candidates(symbol, side, closed_candle_time_utc);
CREATE INDEX IF NOT EXISTS idx_shadow_name_decision ON shadow_decisions(shadow_name, decision);
CREATE INDEX IF NOT EXISTS idx_outcomes_result ON signal_outcomes(result);
CREATE INDEX IF NOT EXISTS idx_execution_status ON execution_truth_links(execution_status, authoritative_eligible);
"""


MIGRATION_2 = """
CREATE TABLE IF NOT EXISTS pre_candidate_observations (
    observation_key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES scanner_runs(run_id),
    timestamp_utc TEXT NOT NULL,
    scan_candle_utc TEXT NOT NULL,
    symbol TEXT NOT NULL,
    stage TEXT NOT NULL,
    side_hint TEXT,
    outcome TEXT NOT NULL,
    category TEXT NOT NULL,
    reason TEXT,
    score_long REAL,
    score_short REAL,
    confidence REAL,
    rsi REAL,
    mfi REAL,
    atr REAL,
    btc_regime TEXT,
    trend_1h TEXT,
    trend_4h TEXT,
    session TEXT,
    feature_schema_version INTEGER NOT NULL,
    extras_json TEXT NOT NULL DEFAULT '{}',
    source_mode TEXT NOT NULL CHECK (source_mode IN ('PROSPECTIVE','HISTORICAL_BACKFILL')),
    source_name TEXT NOT NULL,
    source_version TEXT,
    imported_at_utc TEXT,
    created_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pre_candidate_run ON pre_candidate_observations(run_id);
CREATE INDEX IF NOT EXISTS idx_pre_candidate_time ON pre_candidate_observations(timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_pre_candidate_population
    ON pre_candidate_observations(source_mode, category, stage, symbol);
"""


MIGRATIONS = {1: MIGRATION_1, 2: MIGRATION_2}


def _sql_statements(script: str) -> Iterable[str]:
    """Yield complete statements so migrations remain one rollback-safe transaction."""
    buffer = ""
    for line in script.splitlines():
        buffer += line + "\n"
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                yield statement
            buffer = ""
    if buffer.strip():
        raise sqlite3.OperationalError("Incomplete migration statement")


class ResearchTelemetryStore:
    def __init__(
        self,
        path: Path | str = DEFAULT_DB_PATH,
        *,
        busy_timeout_ms: int = 1500,
        registry: ResearchEvaluatorRegistry | None = None,
    ) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))
        self.registry = registry or default_evaluator_registry()
        self._prospective_start_utc = ""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            connection = sqlite3.connect(f"file:{self.path.resolve().as_posix()}?mode=ro", uri=True, timeout=self.busy_timeout_ms / 1000)
        else:
            connection = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        if not read_only:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def migrate(self) -> None:
        now = utc_now()
        with closing(self.connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                meta_exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_meta'"
                ).fetchone() is not None
                row = connection.execute(
                    "SELECT schema_version, prospective_start_utc FROM research_meta WHERE singleton=1"
                ).fetchone() if meta_exists else None
                current_version = int(row[0]) if row is not None else 0
                if current_version > SCHEMA_VERSION:
                    raise RuntimeError(f"Research schema {current_version} is newer than supported {SCHEMA_VERSION}")
                for target_version in range(current_version + 1, SCHEMA_VERSION + 1):
                    for statement in _sql_statements(MIGRATIONS[target_version]):
                        connection.execute(statement)
                if row is None:
                    connection.execute(
                        "INSERT INTO research_meta VALUES (1,?,?,?,?)",
                        (SCHEMA_VERSION, now, now, now),
                    )
                    self._prospective_start_utc = now
                else:
                    if current_version < SCHEMA_VERSION:
                        connection.execute(
                            "UPDATE research_meta SET schema_version=?, last_migration_utc=? WHERE singleton=1",
                            (SCHEMA_VERSION, now),
                        )
                    self._prospective_start_utc = _text(row[1])
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def boundary(self) -> str:
        if self._prospective_start_utc:
            return self._prospective_start_utc
        with closing(self.connect(read_only=True)) as connection:
            row = connection.execute("SELECT prospective_start_utc FROM research_meta WHERE singleton=1").fetchone()
            self._prospective_start_utc = _text(row[0]) if row else ""
            return self._prospective_start_utc

    def start_run(
        self,
        run_id: str,
        scan_started_at_utc: str,
        scan_candle_time_utc: str = "",
        *,
        btc_regime: str = "",
        source_mode: str = "PROSPECTIVE",
        source_name: str = "scanner",
        source_version: str = "UNIFIED_SCANNER_RESEARCH_TELEMETRY_V1",
    ) -> None:
        if source_mode not in VALID_SOURCE_MODES:
            raise ValueError(f"Invalid source_mode: {source_mode}")
        imported = utc_now() if source_mode == "HISTORICAL_BACKFILL" else None
        with closing(self.connect()) as connection, connection:
            connection.execute(
                """INSERT OR IGNORE INTO scanner_runs
                (run_id,scan_started_at_utc,scan_candle_time_utc,btc_regime,market_context_version,
                 source_mode,source_name,source_version,imported_at_utc)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (run_id, normalize_utc(scan_started_at_utc), normalize_utc(scan_candle_time_utc), btc_regime,
                 "1", source_mode, source_name, source_version, imported),
            )

    def record_candidate(self, snapshot: CandidateSnapshot) -> bool:
        if snapshot.source_mode not in VALID_SOURCE_MODES:
            raise ValueError(f"Invalid source_mode: {snapshot.source_mode}")
        if snapshot.source_mode == "PROSPECTIVE":
            candidate_time = _utc_datetime(snapshot.timestamp_utc)
            boundary_time = _utc_datetime(self.boundary())
            if candidate_time is None or boundary_time is None or candidate_time < boundary_time:
                raise ValueError("Prospective candidate timestamp precedes the telemetry boundary")
        key = snapshot.resolved_key()
        recorded = utc_now()
        imported = recorded if snapshot.source_mode == "HISTORICAL_BACKFILL" else None
        features = dict(snapshot.features)
        market = dict(snapshot.market)
        exposure = dict(snapshot.exposure)
        canonical = snapshot.canonical_signal_key or canonical_signal_key(
            symbol=snapshot.symbol,
            side=snapshot.side,
            timestamp=snapshot.timestamp_utc,
            entry=snapshot.entry,
        )
        known_feature_keys = {
            "rsi", "mfi", "atr", "atr_pct", "atr_expansion", "ema9", "ema20", "ema21", "ema50",
            "ema20_distance_atr", "ema50_distance_atr", "body_ratio", "upper_wick_ratio", "lower_wick_ratio",
            "opposite_wick_ratio", "volume", "volume_ratio", "momentum", "breakout_confirmed", "wave_score",
            "wave_state", "wave_phase", "entry_timing_state", "sr_state", "exhaustion_state",
        }
        known_market_keys = {
            "btc_regime", "trend_1h", "trend_4h", "volatility_context", "btc_side_alignment",
            "long_short_mix", "utc_hour", "local_hour", "day_of_week", "session",
        }
        known_exposure_keys = {
            "actual_open_total", "actual_open_same_side", "actual_open_opposite_side", "same_side_altcoin_open_count",
            "same_scan_long_count", "same_scan_short_count", "same_scan_side_count", "prior_same_side_1h",
            "prior_same_side_3h", "prior_same_side_6h", "cumulative_same_side_modeled_risk",
            "candidate_sequence_after_first_exposure", "open_position_ages_json", "cross_scan_same_side_count",
            "correlated_open_count", "max_pair_correlation", "cluster_id", "representative_signal_key", "cross_scan_state",
        }
        with closing(self.connect()) as connection, connection:
            inserted = connection.execute(
                """INSERT OR IGNORE INTO candidates
                (candidate_key,run_id,timestamp_utc,closed_candle_time_utc,symbol,side,candidate_stage,
                 decision,decision_stage,decision_reason,signal_status,canonical_signal_key,entry,sl,tp1,tp2,rr,
                 score,confidence,setup_strength,tier,session,source_mode,source_name,source_version,imported_at_utc,recorded_at_utc)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key, snapshot.run_id, normalize_utc(snapshot.timestamp_utc), normalize_utc(snapshot.closed_candle_time_utc),
                 normalize_symbol(snapshot.symbol), normalize_side(snapshot.side), snapshot.candidate_stage,
                 snapshot.decision, snapshot.decision_stage, snapshot.decision_reason, snapshot.signal_status, canonical,
                 snapshot.entry, snapshot.sl, snapshot.tp1, snapshot.tp2, snapshot.rr, snapshot.score,
                 snapshot.confidence, snapshot.setup_strength, snapshot.tier, snapshot.session, snapshot.source_mode,
                 snapshot.source_name, snapshot.source_version, imported, recorded),
            ).rowcount == 1
            if not inserted:
                return False
            feature_values = (key, FEATURE_SCHEMA_VERSION, _float(features.get("rsi")), _float(features.get("mfi")),
                 _float(features.get("atr")), _float(features.get("atr_pct")), _float(features.get("atr_expansion")),
                 _float(features.get("ema9")), _float(features.get("ema20")), _float(features.get("ema21")),
                 _float(features.get("ema50")), _float(features.get("ema20_distance_atr")),
                 _float(features.get("ema50_distance_atr")), _float(features.get("body_ratio")),
                 _float(features.get("upper_wick_ratio")), _float(features.get("lower_wick_ratio")),
                 _float(features.get("opposite_wick_ratio")), _float(features.get("volume")),
                 _float(features.get("volume_ratio")), _float(features.get("momentum")),
                 _bool_int(features.get("breakout_confirmed")), _float(features.get("wave_score")),
                 _text(features.get("wave_state")), _text(features.get("wave_phase")),
                 _text(features.get("entry_timing_state")), _text(features.get("sr_state")),
                 _text(features.get("exhaustion_state")),
                 _json({k: v for k, v in features.items() if k not in known_feature_keys}))
            connection.execute(
                f"INSERT INTO candidate_features VALUES ({','.join('?' for _ in feature_values)})",
                feature_values,
            )
            connection.execute(
                """INSERT INTO market_context VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key, _text(market.get("btc_regime")), _text(market.get("trend_1h")),
                 _text(market.get("trend_4h")), _text(market.get("volatility_context")),
                 _text(market.get("btc_side_alignment")), _text(market.get("long_short_mix")),
                 _int(market.get("utc_hour")), _int(market.get("local_hour")), _text(market.get("day_of_week")),
                 _text(market.get("session") or snapshot.session),
                 _json({k: v for k, v in market.items() if k not in known_market_keys})),
            )
            exposure_values = (key, _int(exposure.get("actual_open_total")), _int(exposure.get("actual_open_same_side")),
                 _int(exposure.get("actual_open_opposite_side")), _int(exposure.get("same_side_altcoin_open_count")),
                 _int(exposure.get("same_scan_long_count")), _int(exposure.get("same_scan_short_count")),
                 _int(exposure.get("same_scan_side_count")), _int(exposure.get("prior_same_side_1h")),
                 _int(exposure.get("prior_same_side_3h")), _int(exposure.get("prior_same_side_6h")),
                 _float(exposure.get("cumulative_same_side_modeled_risk")),
                 _int(exposure.get("candidate_sequence_after_first_exposure")),
                 _json(exposure.get("open_position_ages_json")), _int(exposure.get("cross_scan_same_side_count")),
                 _int(exposure.get("correlated_open_count")), _float(exposure.get("max_pair_correlation")),
                 _text(exposure.get("cluster_id")), _text(exposure.get("representative_signal_key")),
                 _text(exposure.get("cross_scan_state")),
                 _json({k: v for k, v in exposure.items() if k not in known_exposure_keys}))
            connection.execute(
                f"INSERT INTO exposure_snapshots VALUES ({','.join('?' for _ in exposure_values)})",
                exposure_values,
            )
            combined: dict[tuple[str, str], ShadowDecision] = {}
            for decision in [*self.registry.evaluate(snapshot), *snapshot.shadows]:
                combined[(decision.name, decision.version)] = decision
            for decision in combined.values():
                connection.execute(
                    """INSERT OR IGNORE INTO shadow_decisions
                    (candidate_key,shadow_name,shadow_version,decision,reason,metrics_json,evaluated_at_utc)
                    VALUES (?,?,?,?,?,?,?)""",
                    (key, decision.name, decision.version, decision.decision, decision.reason,
                     _json(decision.metrics), decision.evaluated_at_utc or recorded),
                )
        return True

    def record_observation(self, observation: PreCandidateObservation) -> bool:
        if observation.source_mode not in VALID_SOURCE_MODES:
            raise ValueError(f"Invalid source_mode: {observation.source_mode}")
        if observation.source_mode == "PROSPECTIVE":
            observation_time = _utc_datetime(observation.timestamp_utc)
            boundary_time = _utc_datetime(self.boundary())
            if observation_time is None or boundary_time is None or observation_time < boundary_time:
                raise ValueError("Prospective observation timestamp precedes the telemetry boundary")
        created = utc_now()
        imported = created if observation.source_mode == "HISTORICAL_BACKFILL" else None
        with closing(self.connect()) as connection, connection:
            return connection.execute(
                """INSERT OR IGNORE INTO pre_candidate_observations
                (observation_key,run_id,timestamp_utc,scan_candle_utc,symbol,stage,side_hint,outcome,category,reason,
                 score_long,score_short,confidence,rsi,mfi,atr,btc_regime,trend_1h,trend_4h,session,
                 feature_schema_version,extras_json,source_mode,source_name,source_version,imported_at_utc,created_at_utc)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    observation.resolved_key(), observation.run_id, normalize_utc(observation.timestamp_utc),
                    normalize_utc(observation.scan_candle_utc), normalize_symbol(observation.symbol),
                    _text(observation.stage).upper(), normalize_side(observation.side_hint) or None,
                    _text(observation.outcome).upper() or "WAIT", _text(observation.category).upper(), observation.reason,
                    _float(observation.score_long), _float(observation.score_short), _float(observation.confidence),
                    _float(observation.rsi), _float(observation.mfi), _float(observation.atr),
                    observation.btc_regime, observation.trend_1h, observation.trend_4h, observation.session,
                    FEATURE_SCHEMA_VERSION, _json(observation.extras), observation.source_mode,
                    observation.source_name, observation.source_version, imported, created,
                ),
            ).rowcount == 1

    def enrich_outcomes(self, signals_path: Path | str) -> dict[str, int]:
        path = Path(signals_path)
        if not path.exists() or path.stat().st_size == 0:
            return {"read": 0, "upserted": 0}
        rows = list(csv.DictReader(path.open("r", encoding="utf-8-sig", newline="")))
        now = utc_now()
        upserted = 0
        with closing(self.connect()) as connection, connection:
            for row in rows:
                symbol = normalize_symbol(row.get("symbol"))
                side = normalize_side(row.get("side") or row.get("direction"))
                timestamp = row.get("timestamp") or row.get("timestamp_utc")
                key = _text(row.get("canonical_signal_key")) or canonical_signal_key(
                    symbol=symbol, side=side, timestamp=timestamp,
                    entry=row.get("entry") or row.get("entry_low"),
                )
                if not key:
                    continue
                result = _text(row.get("result") or row.get("final_outcome") or row.get("outcome")).upper()
                modeled_r = _float(row.get("result_r") or row.get("net_r_estimate") or row.get("modeled_r"))
                if modeled_r is None:
                    target = _text(row.get("hit_target")).upper()
                    if result == "LOSS" and target == "SL":
                        modeled_r = -1.0
                    elif result.startswith("WIN") and target in {"TP2", "TP3"}:
                        modeled_r = _float(row.get("risk_reward") or row.get("rr"))
                    elif result.startswith("WIN") and target == "TP1":
                        entry = _float(row.get("entry") or row.get("entry_low"))
                        stop = _float(row.get("stop_loss") or row.get("sl"))
                        tp1 = _float(row.get("tp1"))
                        risk = abs(entry - stop) if entry is not None and stop is not None else 0.0
                        modeled_r = abs(tp1 - entry) / risk if entry is not None and tp1 is not None and risk else None
                resolved = _text(row.get("closed_at") or row.get("resolved_at_utc"))
                seconds = None
                try:
                    start = datetime.fromisoformat(_text(timestamp).replace("Z", "+00:00"))
                    end = datetime.fromisoformat(resolved.replace("Z", "+00:00"))
                    seconds = (end - start).total_seconds()
                except (ValueError, TypeError):
                    pass
                existing = connection.execute(
                    "SELECT result,resolved_at_utc FROM signal_outcomes WHERE canonical_signal_key=?",
                    (key,),
                ).fetchone()
                if existing is not None and _outcome_evidence_rank(result, resolved) < _outcome_evidence_rank(existing[0], existing[1]):
                    continue
                connection.execute(
                    """INSERT INTO signal_outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(canonical_signal_key) DO UPDATE SET
                    result=excluded.result, modeled_r=excluded.modeled_r, resolved_at_utc=excluded.resolved_at_utc,
                    tp1_hit=excluded.tp1_hit, tp2_hit=excluded.tp2_hit, sl_hit=excluded.sl_hit,
                    time_to_resolution_sec=excluded.time_to_resolution_sec, source_name=excluded.source_name,
                    source_version=excluded.source_version, last_enriched_at_utc=excluded.last_enriched_at_utc""",
                    (key, result, modeled_r, normalize_utc(resolved),
                     _bool_int(row.get("tp1_hit") or ("YES" if _text(row.get("hit_target")).upper() in {"TP1", "TP2", "TP3"} else "")),
                     _bool_int(row.get("tp2_hit") or ("YES" if _text(row.get("hit_target")).upper() in {"TP2", "TP3"} else "")),
                     _bool_int(row.get("sl_hit") or ("YES" if _text(row.get("hit_target")).upper() == "SL" else "")),
                     seconds, path.name, "csv_v1", now),
                )
                upserted += 1
            self._set_enrichment_status(connection, f"outcomes:{path.name}", now, str(len(rows)), "")
        return {"read": len(rows), "upserted": upserted}

    def enrich_execution(self, execution_path: Path | str) -> dict[str, int]:
        path = Path(execution_path)
        if not path.exists() or path.stat().st_size == 0:
            return {"read": 0, "upserted": 0}
        rows = list(csv.DictReader(path.open("r", encoding="utf-8-sig", newline="")))
        now = utc_now()
        upserted = 0
        with closing(self.connect()) as connection, connection:
            for row in rows:
                key = _text(row.get("canonical_signal_key"))
                if not key:
                    continue
                execution_status = _text(row.get("match_status") or row.get("execution_status"))
                finality = _text(row.get("execution_finality") or row.get("accounting_finality"))
                evidence_valid = _text(row.get("accounting_evidence_status")) == "ACCOUNTING_EVIDENCE_VALID"
                explicit_authoritative = _bool_int(row.get("authoritative_eligible"))
                authoritative = bool(explicit_authoritative) if explicit_authoritative is not None else (
                    execution_status == "MATCHED"
                    and _text(row.get("execution_finality")) == "EXECUTION_FINAL"
                    and _text(row.get("commission_finality")) == "COMMISSION_FINAL"
                    and _text(row.get("funding_finality")) == "FUNDING_FINAL"
                    and _text(row.get("accounting_finality")) == "ACCOUNTING_FINAL"
                    and evidence_valid
                )
                existing = connection.execute(
                    """SELECT execution_status,execution_finality,authoritative_eligible
                    FROM execution_truth_links WHERE canonical_signal_key=?""",
                    (key,),
                ).fetchone()
                if existing is not None and _execution_evidence_rank(
                    execution_status, finality, authoritative
                ) < _execution_evidence_rank(existing[0], existing[1], existing[2]):
                    continue
                connection.execute(
                    """INSERT INTO execution_truth_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(canonical_signal_key) DO UPDATE SET
                    execution_status=excluded.execution_status, execution_finality=excluded.execution_finality,
                    authoritative_eligible=excluded.authoritative_eligible, actual_entry_vwap=excluded.actual_entry_vwap,
                    actual_exit_vwap=excluded.actual_exit_vwap, gross_realized_pnl=excluded.gross_realized_pnl,
                    commission=excluded.commission, execution_pnl=excluded.execution_pnl, gross_r=excluded.gross_r,
                    execution_net_r=excluded.execution_net_r, funding_state=excluded.funding_state,
                    source_name=excluded.source_name, source_version=excluded.source_version,
                    last_enriched_at_utc=excluded.last_enriched_at_utc""",
                    (key, execution_status, finality, int(authoritative),
                     _float(row.get("entry_fill_price") or row.get("actual_entry_vwap")),
                     _float(row.get("exit_vwap") or row.get("actual_exit_vwap")),
                     _float(row.get("gross_realized_pnl_usdt") or row.get("gross_realized_pnl")),
                     _float(row.get("commission_usdt") or row.get("commission")),
                     _float(row.get("execution_pnl_usdt") or row.get("execution_pnl")),
                     _float(row.get("gross_realized_r") or row.get("gross_r")),
                     _float(row.get("execution_r") or row.get("net_realized_r") or row.get("execution_net_r")),
                     _text(row.get("funding_finality") or row.get("funding_status")), path.name,
                     _text(row.get("record_version") or "execution_truth_v1"), now),
                )
                upserted += 1
            self._set_enrichment_status(connection, f"execution:{path.name}", now, str(len(rows)), "")
        return {"read": len(rows), "upserted": upserted}

    @staticmethod
    def _set_enrichment_status(connection: sqlite3.Connection, name: str, success: str, high_water: str, error: str) -> None:
        connection.execute(
            """INSERT INTO enrichment_status VALUES (?,?,?,?)
            ON CONFLICT(source_name) DO UPDATE SET last_success_utc=excluded.last_success_utc,
            high_water=excluded.high_water,last_error=excluded.last_error""",
            (name, success, high_water, error),
        )


class FailOpenResearchTelemetry:
    """Scanner-facing adapter: every database failure is swallowed and logged."""

    def __init__(self, path: Path | str = DEFAULT_DB_PATH, *, enabled: bool = True) -> None:
        self.store: ResearchTelemetryStore | None = None
        self.path = Path(path)
        if not enabled:
            return
        try:
            self.store = ResearchTelemetryStore(self.path)
        except Exception as exc:
            LOGGER.warning("Research telemetry unavailable; live scanner continues: %s", exc)

    @property
    def available(self) -> bool:
        return self.store is not None

    def start_run(self, *args: Any, **kwargs: Any) -> bool:
        if self.store is None:
            return False
        try:
            self.store.start_run(*args, **kwargs)
            return True
        except Exception as exc:
            LOGGER.warning("Research telemetry run snapshot failed open: %s", exc)
            return False

    def record_candidate(self, snapshot: CandidateSnapshot) -> bool:
        if self.store is None:
            return False
        try:
            return self.store.record_candidate(snapshot)
        except Exception as exc:
            LOGGER.warning("Research telemetry candidate snapshot failed open: %s", exc)
            return False

    def record_observation(self, observation: PreCandidateObservation) -> bool:
        if self.store is None:
            return False
        try:
            return self.store.record_observation(observation)
        except Exception as exc:
            LOGGER.warning("Research telemetry pre-candidate observation failed open: %s", exc)
            return False


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def backfill_signals(store: ResearchTelemetryStore, path: Path, *, source_version: str = "signals_csv_v1") -> dict[str, int]:
    rows = _read_csv(path)
    inserted = 0
    source_identity = hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:12]
    for row in rows:
        timestamp = _text(row.get("timestamp") or row.get("timestamp_utc"))
        symbol = normalize_symbol(row.get("symbol"))
        side = normalize_side(row.get("side") or row.get("direction"))
        if not timestamp or not symbol or side not in {"LONG", "SHORT"}:
            continue
        status = _text(row.get("signal_status") or row.get("status") or "sent").lower()
        decision = classify_decision(status)
        run_id = make_run_id(timestamp, timestamp) + f"_historical_{source_identity}"
        store.start_run(run_id, timestamp, timestamp, source_mode="HISTORICAL_BACKFILL", source_name=path.name, source_version=source_version)
        features = {
            "mfi": row.get("mfi"), "atr": row.get("atr"), "atr_pct": row.get("atr_pct"),
            "atr_expansion": row.get("atr_expansion_ratio") or row.get("atr_expansion"),
            "body_ratio": row.get("body_ratio") or row.get("body_percent"),
            "opposite_wick_ratio": row.get("opposite_wick_ratio"),
            "volume_ratio": row.get("volume_ratio"), "breakout_confirmed": row.get("breakout_confirmed"),
            "wave_score": row.get("wave_score"), "wave_state": row.get("wave_structure"),
            "wave_phase": row.get("wave_phase"), "quality_flags": row.get("quality_flags"),
        }
        snapshot = CandidateSnapshot(
            run_id=run_id, timestamp_utc=timestamp, closed_candle_time_utc=timestamp,
            symbol=symbol, side=side, decision=decision, decision_reason=_text(row.get("skip_reason") or row.get("reason")),
            signal_status=status, canonical_signal_key=_text(row.get("canonical_signal_key")),
            entry=_float(row.get("entry")), sl=_float(row.get("stop_loss") or row.get("sl")),
            tp1=_float(row.get("tp1")), tp2=_float(row.get("tp2")),
            rr=_float(row.get("risk_reward") or row.get("rr")), score=_float(row.get("score")),
            confidence=_float(row.get("confidence")), setup_strength=_float(row.get("setup_strength")),
            tier=_text(row.get("watchlist_tier") or row.get("tier")),
            session=_text(row.get("market_session") or row.get("session")), source_mode="HISTORICAL_BACKFILL",
            source_name=path.name, source_version=source_version, features=features,
            market={"btc_regime": row.get("btc_regime"), "trend_1h": row.get("market_regime"),
                    "trend_4h": row.get("htf_regime"), "session": row.get("market_session") or row.get("session")},
        )
        inserted += int(store.record_candidate(snapshot))
    return {"read": len(rows), "inserted": inserted}


def ingest_shadow_csv(store: ResearchTelemetryStore, path: Path, shadow_name: str) -> dict[str, int]:
    rows = _read_csv(path)
    linked = 0
    now = utc_now()
    with closing(store.connect()) as connection, connection:
        for row in rows:
            canonical = _text(row.get("canonical_signal_key"))
            if not canonical:
                continue
            candidates = connection.execute(
                "SELECT candidate_key FROM candidates WHERE canonical_signal_key=? ORDER BY recorded_at_utc DESC",
                (canonical,),
            ).fetchall()
            decision = _text(row.get("shadow_decision") or row.get("recommendation") or row.get("setup_shadow_class")
                             or row.get("exhaustion_class") or row.get("sr_class") or "OBSERVED")
            version = _text(row.get("shadow_version") or "1")
            for candidate in candidates:
                linked += connection.execute(
                    "INSERT OR IGNORE INTO shadow_decisions VALUES (?,?,?,?,?,?,?)",
                    (candidate[0], shadow_name, version, decision,
                     _text(row.get("shadow_reason") or row.get("reason") or row.get("rejection_reason")),
                     _json(row), normalize_utc(row.get("generated_at_utc") or row.get("timestamp") or now)),
                ).rowcount
        store._set_enrichment_status(connection, f"shadow:{path.name}", now, str(len(rows)), "")
    return {"read": len(rows), "linked": linked}


def classify_decision(signal_status: str) -> str:
    status = _text(signal_status).lower()
    if status == "sent":
        return "SENT"
    if "report_only" in status:
        return "REPORT_ONLY"
    if status.startswith("skipped"):
        return "SKIPPED"
    if "reject" in status or status == "logged_quality_filter":
        return "REJECTED"
    return status.upper() or "SKIPPED"


ANALYSIS_QUERIES: dict[str, str] = {
    "scan_population": """SELECT COUNT(DISTINCT run_id) observed_scan_runs,
        COUNT(*) pre_candidate_observations,
        (SELECT COUNT(*) FROM candidates WHERE source_mode='PROSPECTIVE') constructed_candidates,
        ROUND(1.0*(SELECT COUNT(*) FROM candidates WHERE source_mode='PROSPECTIVE') /
        NULLIF(COUNT(*)+(SELECT COUNT(*) FROM candidates WHERE source_mode='PROSPECTIVE'),0),4) constructed_candidate_rate
        FROM pre_candidate_observations WHERE source_mode='PROSPECTIVE'""",
    "pre_candidate_rates": """SELECT category, COUNT(*) n,
        ROUND(1.0*COUNT(*)/NULLIF(SUM(COUNT(*)) OVER (),0),4) population_rate
        FROM pre_candidate_observations WHERE source_mode='PROSPECTIVE' GROUP BY category ORDER BY n DESC""",
    "correlation_sent": """SELECT CASE WHEN e.max_pair_correlation IS NULL THEN 'UNKNOWN'
        WHEN e.max_pair_correlation<0.25 THEN '<0.25' WHEN e.max_pair_correlation<0.5 THEN '0.25-0.49'
        WHEN e.max_pair_correlation<0.75 THEN '0.50-0.74' ELSE '0.75+' END correlation_band, COUNT(*) n
        FROM candidates c JOIN exposure_snapshots e USING(candidate_key)
        WHERE c.source_mode='PROSPECTIVE' AND c.decision='SENT' GROUP BY correlation_band""",
    "correlation_rejected": """SELECT CASE WHEN e.max_pair_correlation IS NULL THEN 'UNKNOWN'
        WHEN e.max_pair_correlation<0.25 THEN '<0.25' WHEN e.max_pair_correlation<0.5 THEN '0.25-0.49'
        WHEN e.max_pair_correlation<0.75 THEN '0.50-0.74' ELSE '0.75+' END correlation_band, COUNT(*) n
        FROM candidates c JOIN exposure_snapshots e USING(candidate_key)
        WHERE c.source_mode='PROSPECTIVE' AND c.decision='REJECTED' GROUP BY correlation_band""",
    "correlation_exposure_outcome": """SELECT e.correlated_open_count, e.actual_open_same_side,
        CASE WHEN e.max_pair_correlation IS NULL THEN 'UNKNOWN' WHEN e.max_pair_correlation>=0.75 THEN 'HIGH' ELSE 'LOW' END correlation_band,
        COUNT(*) n, SUM(o.result LIKE 'WIN%') wins, ROUND(AVG(o.modeled_r),4) avg_r, ROUND(SUM(o.modeled_r),4) net_r
        FROM candidates c JOIN exposure_snapshots e USING(candidate_key) JOIN signal_outcomes o USING(canonical_signal_key)
        WHERE c.source_mode='PROSPECTIVE' GROUP BY e.correlated_open_count,e.actual_open_same_side,correlation_band""",
    "long_vs_short": """SELECT c.side, COUNT(*) n, SUM(o.result LIKE 'WIN%') wins,
        ROUND(AVG(o.modeled_r),4) avg_r, ROUND(SUM(o.modeled_r),4) net_r
        FROM candidates c JOIN signal_outcomes o USING(canonical_signal_key)
        WHERE c.source_mode='PROSPECTIVE' GROUP BY c.side""",
    "session": """SELECT c.session, COUNT(*) n, SUM(o.result LIKE 'WIN%') wins, ROUND(SUM(o.modeled_r),4) net_r
        FROM candidates c JOIN signal_outcomes o USING(canonical_signal_key) WHERE c.source_mode='PROSPECTIVE' GROUP BY c.session""",
    "setup_strength": """SELECT CASE WHEN c.setup_strength<70 THEN '<70' WHEN c.setup_strength<80 THEN '70-79'
        WHEN c.setup_strength<90 THEN '80-89' ELSE '90+' END band, COUNT(*) n, ROUND(SUM(o.modeled_r),4) net_r
        FROM candidates c JOIN signal_outcomes o USING(canonical_signal_key) WHERE c.source_mode='PROSPECTIVE' GROUP BY band""",
    "mfi": """SELECT CAST(f.mfi/10 AS INT)*10 band, COUNT(*) n, ROUND(SUM(o.modeled_r),4) net_r
        FROM candidates c JOIN candidate_features f USING(candidate_key) JOIN signal_outcomes o USING(canonical_signal_key)
        WHERE c.source_mode='PROSPECTIVE' AND f.mfi IS NOT NULL GROUP BY band""",
    "atr_expansion": """SELECT CASE WHEN f.atr_expansion<1 THEN '<1' WHEN f.atr_expansion<1.5 THEN '1-1.49' ELSE '1.5+' END band,
        COUNT(*) n, ROUND(SUM(o.modeled_r),4) net_r FROM candidates c JOIN candidate_features f USING(candidate_key)
        JOIN signal_outcomes o USING(canonical_signal_key) WHERE c.source_mode='PROSPECTIVE' GROUP BY band""",
    "breakout": """SELECT f.breakout_confirmed, COUNT(*) n, ROUND(SUM(o.modeled_r),4) net_r
        FROM candidates c JOIN candidate_features f USING(candidate_key) JOIN signal_outcomes o USING(canonical_signal_key)
        WHERE c.source_mode='PROSPECTIVE' GROUP BY f.breakout_confirmed""",
    "same_side_exposure": """SELECT e.actual_open_same_side, COUNT(*) n, ROUND(SUM(o.modeled_r),4) net_r
        FROM candidates c JOIN exposure_snapshots e USING(candidate_key) JOIN signal_outcomes o USING(canonical_signal_key)
        WHERE c.source_mode='PROSPECTIVE' GROUP BY e.actual_open_same_side""",
    "cross_scan_shadow": """SELECT s.decision, COUNT(*) n, ROUND(SUM(o.modeled_r),4) net_r
        FROM shadow_decisions s JOIN candidates c USING(candidate_key) JOIN signal_outcomes o USING(canonical_signal_key)
        WHERE c.source_mode='PROSPECTIVE' AND s.shadow_name='cross_scan_exposure_v1' GROUP BY s.decision""",
    "btc_regime": """SELECT m.btc_regime, COUNT(*) n, ROUND(SUM(o.modeled_r),4) net_r
        FROM candidates c JOIN market_context m USING(candidate_key) JOIN signal_outcomes o USING(canonical_signal_key)
        WHERE c.source_mode='PROSPECTIVE' GROUP BY m.btc_regime""",
    "decision_population": """SELECT decision, COUNT(*) n, ROUND(AVG(score),2) avg_score,
        ROUND(AVG(confidence),2) avg_confidence FROM candidates WHERE source_mode='PROSPECTIVE' GROUP BY decision""",
    "theoretical_vs_execution": """SELECT COUNT(*) n, ROUND(AVG(o.modeled_r),4) theoretical_avg_r,
        ROUND(AVG(e.execution_net_r),4) execution_avg_r FROM candidates c JOIN signal_outcomes o USING(canonical_signal_key)
        JOIN execution_truth_links e USING(canonical_signal_key) WHERE c.source_mode='PROSPECTIVE' AND e.authoritative_eligible=1""",
    "commission_drag": """SELECT COUNT(*) n, ROUND(SUM(gross_realized_pnl),4) gross_pnl,
        ROUND(SUM(commission),4) commission, ROUND(SUM(execution_pnl),4) execution_pnl
        FROM execution_truth_links WHERE authoritative_eligible=1""",
    "loss_clusters": """SELECT COALESCE(e.cluster_id,'UNCLUSTERED') cluster_id, COUNT(*) losses,
        ROUND(SUM(o.modeled_r),4) net_r FROM candidates c JOIN exposure_snapshots e USING(candidate_key)
        JOIN signal_outcomes o USING(canonical_signal_key) WHERE c.source_mode='PROSPECTIVE' AND o.result='LOSS'
        GROUP BY cluster_id ORDER BY losses DESC""",
}


def health(store: ResearchTelemetryStore) -> dict[str, Any]:
    result: dict[str, Any] = {"db_readable": False, "db_size_bytes": store.path.stat().st_size if store.path.exists() else 0}
    with closing(store.connect(read_only=True)) as connection:
        result["db_readable"] = True
        result["integrity"] = connection.execute("PRAGMA quick_check").fetchone()[0]
        result["journal_mode"] = connection.execute("PRAGMA journal_mode").fetchone()[0]
        meta = connection.execute("SELECT schema_version,prospective_start_utc FROM research_meta WHERE singleton=1").fetchone()
        result["schema_version"] = meta[0]
        result["prospective_start_utc"] = meta[1]
        result["last_candidate_timestamp"] = connection.execute("SELECT MAX(timestamp_utc) FROM candidates").fetchone()[0]
        result["pre_candidate_observations"] = connection.execute(
            "SELECT COUNT(*) FROM pre_candidate_observations"
        ).fetchone()[0]
        result["latest_pre_candidate_timestamp"] = connection.execute(
            "SELECT MAX(timestamp_utc) FROM pre_candidate_observations"
        ).fetchone()[0]
        result["pre_candidate_by_reason"] = {
            row[0]: row[1] for row in connection.execute(
                "SELECT category,COUNT(*) FROM pre_candidate_observations GROUP BY category ORDER BY category"
            )
        }
        result["last_enrichment_timestamp"] = connection.execute("SELECT MAX(last_success_utc) FROM enrichment_status").fetchone()[0]
        result["duplicate_candidate_keys"] = connection.execute(
            "SELECT COUNT(*) FROM (SELECT candidate_key FROM candidates GROUP BY candidate_key HAVING COUNT(*)>1)"
        ).fetchone()[0]
        result["orphan_rows"] = sum(connection.execute(
            f"SELECT COUNT(*) FROM {table} x LEFT JOIN candidates c ON c.candidate_key=x.candidate_key WHERE c.candidate_key IS NULL"
        ).fetchone()[0] for table in ("candidate_features", "market_context", "exposure_snapshots", "shadow_decisions"))
        total = connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        critical_null = connection.execute(
            "SELECT COUNT(*) FROM candidates WHERE symbol='' OR side='' OR decision='' OR closed_candle_time_utc=''"
        ).fetchone()[0]
        result["critical_null_rate"] = round(critical_null / total, 6) if total else 0.0
    return result


def summary(store: ResearchTelemetryStore) -> dict[str, Any]:
    with closing(store.connect(read_only=True)) as connection:
        data = {"boundary": store.boundary()}
        data["candidates"] = connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        data["pre_candidate_observations"] = connection.execute(
            "SELECT COUNT(*) FROM pre_candidate_observations"
        ).fetchone()[0]
        for name in ("SENT", "REJECTED", "SKIPPED", "REPORT_ONLY"):
            data[name.lower()] = connection.execute("SELECT COUNT(*) FROM candidates WHERE decision=?", (name,)).fetchone()[0]
        data["resolved_outcomes"] = connection.execute(
            "SELECT COUNT(*) FROM signal_outcomes WHERE result NOT IN ('','OPEN')"
        ).fetchone()[0]
        data["execution_linked"] = connection.execute("SELECT COUNT(*) FROM execution_truth_links").fetchone()[0]
        data["feature_coverage"] = connection.execute(
            "SELECT COUNT(*) FROM candidate_features WHERE rsi IS NOT NULL OR mfi IS NOT NULL OR atr IS NOT NULL"
        ).fetchone()[0]
        data["shadow_coverage"] = connection.execute("SELECT COUNT(DISTINCT candidate_key) FROM shadow_decisions").fetchone()[0]
        return data


def benchmark(count: int, path: Path | None = None) -> dict[str, Any]:
    owns_path = path is None
    temp_dir: tempfile.TemporaryDirectory[str] | None = tempfile.TemporaryDirectory() if owns_path else None
    db_path = Path(temp_dir.name) / "benchmark.db" if temp_dir else Path(path)
    store = ResearchTelemetryStore(db_path)
    latencies: list[float] = []
    started_dt = _utc_datetime(store.boundary()) or datetime.now(timezone.utc)
    started = started_dt.isoformat().replace("+00:00", "Z")
    run_id = make_run_id(started, started)
    store.start_run(run_id, started, started)
    for index in range(count):
        candle = (started_dt + timedelta(seconds=index + 1)).isoformat().replace("+00:00", "Z")
        snap = CandidateSnapshot(
            run_id=run_id, timestamp_utc=candle, closed_candle_time_utc=candle,
            symbol=f"COIN{index % 100}USDT", side="LONG" if index % 2 else "SHORT",
            decision="SENT" if index % 5 else "REJECTED", score=80, confidence=82, setup_strength=82,
            features={"rsi": 55, "mfi": 60, "atr": 1.2, "atr_expansion": 1.1, "body_ratio": .6},
            market={"btc_regime": "sideways", "session": "Asia"},
            exposure={"actual_open_same_side": index % 4},
        )
        before = time.perf_counter()
        store.record_candidate(snap)
        latencies.append((time.perf_counter() - before) * 1000)
    with closing(store.connect(read_only=True)) as connection:
        before = time.perf_counter()
        connection.execute(ANALYSIS_QUERIES["decision_population"]).fetchall()
        query_ms = (time.perf_counter() - before) * 1000
    result = {
        "candidates": count,
        "median_write_ms": round(statistics.median(latencies), 4),
        "p95_write_ms": round(sorted(latencies)[max(0, int(len(latencies) * .95) - 1)], 4),
        "db_size_bytes": db_path.stat().st_size,
        "common_query_ms": round(query_ms, 4),
    }
    if temp_dir:
        temp_dir.cleanup()
    return result


def _print_mapping(title: str, data: Mapping[str, Any]) -> None:
    print(title)
    print("=" * len(title))
    for key, value in data.items():
        print(f"{key.replace('_', ' ').title()}: {value if value not in (None, '') else 'N/A'}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Unified scanner research telemetry (read-only by default).")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "summary", "shadows", "coverage", "health"):
        sub.add_parser(command)
    analysis_parser = sub.add_parser("analysis")
    analysis_parser.add_argument("name", choices=sorted(ANALYSIS_QUERIES))
    backfill_parser = sub.add_parser("backfill")
    backfill_parser.add_argument("--signals", type=Path, default=Path("logs/signals.csv"))
    backfill_parser.add_argument(
        "--shadow",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Compatibility-ingest a shadow CSV after candidates (repeatable).",
    )
    enrich_parser = sub.add_parser("enrich")
    enrich_parser.add_argument("--signals", type=Path, default=Path("logs/signals.csv"))
    enrich_parser.add_argument("--execution", type=Path, default=Path("logs/binance_execution_truth_v1.csv"))
    bench_parser = sub.add_parser("benchmark")
    bench_parser.add_argument("--count", type=int, default=10_000)
    args = parser.parse_args(argv)
    if args.command == "benchmark":
        _print_mapping("Research Telemetry Benchmark", benchmark(args.count))
        return 0
    store = ResearchTelemetryStore(args.db)
    if args.command in {"status", "summary"}:
        _print_mapping("Research Telemetry", summary(store))
    elif args.command == "health":
        _print_mapping("Research Telemetry Health", health(store))
    elif args.command == "coverage":
        with closing(store.connect(read_only=True)) as connection:
            rows = connection.execute("""SELECT COUNT(*) candidates,
                SUM(f.rsi IS NOT NULL) rsi, SUM(f.mfi IS NOT NULL) mfi, SUM(f.atr IS NOT NULL) atr,
                SUM(f.atr_expansion IS NOT NULL) atr_expansion, SUM(f.breakout_confirmed IS NOT NULL) breakout,
                SUM(e.actual_open_same_side IS NOT NULL) same_side_exposure
                FROM candidates c JOIN candidate_features f USING(candidate_key)
                JOIN exposure_snapshots e USING(candidate_key)""").fetchone()
            _print_mapping("Research Telemetry Coverage", dict(rows))
    elif args.command == "shadows":
        with closing(store.connect(read_only=True)) as connection:
            rows = connection.execute(
                "SELECT shadow_name,shadow_version,decision,COUNT(*) n FROM shadow_decisions GROUP BY 1,2,3 ORDER BY 1,2,3"
            ).fetchall()
            print("Shadow Coverage\n===============")
            for row in rows:
                print(" | ".join(str(item) for item in row))
            if not rows:
                print("N/A: no shadow decisions")
    elif args.command == "analysis":
        with closing(store.connect(read_only=True)) as connection:
            rows = connection.execute(ANALYSIS_QUERIES[args.name]).fetchall()
            print(f"Analysis: {args.name}\n{'=' * (10 + len(args.name))}")
            if rows:
                print(" | ".join(rows[0].keys()))
                for row in rows:
                    print(" | ".join("N/A" if value is None else str(value) for value in row))
            else:
                print("N/A: no qualifying rows")
    elif args.command == "backfill":
        result: dict[str, Any] = {"signals": backfill_signals(store, args.signals)}
        for spec in args.shadow:
            if "=" not in spec:
                parser.error("--shadow must use NAME=PATH")
            name, raw_path = spec.split("=", 1)
            result[f"shadow:{name}"] = ingest_shadow_csv(store, Path(raw_path), name)
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.command == "enrich":
        result = {"outcomes": store.enrich_outcomes(args.signals), "execution": store.enrich_execution(args.execution)}
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
