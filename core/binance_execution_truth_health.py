# -*- coding: utf-8 -*-
"""Read-only health diagnostic for the periodic Binance execution collector."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from typing import Callable

from core.binance_execution_reconstruction import invalid_accounting_evidence


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_STATE = BASE_DIR / "state" / "binance_execution_truth_v1.json"
DEFAULT_CSV = BASE_DIR / "logs" / "binance_execution_truth_v1.csv"
DEFAULT_EVIDENCE = BASE_DIR / "logs" / "binance_execution_truth_v1.evidence.json"
DEFAULT_LOCK = BASE_DIR / "logs" / "binance_execution_truth_v1.lock"
DEFAULT_TIMER = "crypto-binance-execution-truth.timer"
DEFAULT_SERVICE = "crypto-binance-execution-truth.service"

OK = "OK"
WARNING = "WARNING"
FAIL = "FAIL"


@dataclass(frozen=True)
class SystemdSnapshot:
    timer_active_state: str
    timer_sub_state: str
    service_active_state: str
    service_sub_state: str
    service_result: str
    service_exit_status: int | None


@dataclass(frozen=True)
class HealthConfig:
    state_path: Path = DEFAULT_STATE
    csv_path: Path = DEFAULT_CSV
    evidence_path: Path = DEFAULT_EVIDENCE
    lock_path: Path = DEFAULT_LOCK
    timer_unit: str = DEFAULT_TIMER
    service_unit: str = DEFAULT_SERVICE
    max_age_seconds: float = 30 * 60
    invalid_baseline: int = 0
    ambiguous_baseline: int = 0
    max_ambiguous_increase: int = 0


@dataclass(frozen=True)
class Check:
    status: str
    detail: str


@dataclass
class HealthResult:
    checks: dict[str, Check]
    metrics: dict[str, object]

    @property
    def overall(self) -> str:
        statuses = {check.status for check in self.checks.values()}
        if FAIL in statuses:
            return FAIL
        if WARNING in statuses:
            return WARNING
        return OK

    @property
    def exit_code(self) -> int:
        return 0 if self.overall == OK else 1

    def as_dict(self) -> dict[str, object]:
        return {
            "overall": self.overall,
            "checks": {
                name: {"status": check.status, "detail": check.detail}
                for name, check in self.checks.items()
            },
            "metrics": self.metrics,
        }


def _parse_show(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            result[key] = value
    return result


def _systemctl_show(unit: str, runner: Callable[..., subprocess.CompletedProcess[str]]) -> dict[str, str]:
    completed = runner(
        [
            "systemctl", "show", unit, "--no-pager",
            "--property=ActiveState", "--property=SubState", "--property=Result",
            "--property=ExecMainStatus",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"systemctl show failed for {unit}")
    return _parse_show(completed.stdout)


def probe_systemd(
    timer_unit: str,
    service_unit: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> SystemdSnapshot:
    timer = _systemctl_show(timer_unit, runner)
    service = _systemctl_show(service_unit, runner)
    raw_exit = service.get("ExecMainStatus", "")
    try:
        exit_status = int(raw_exit)
    except (TypeError, ValueError):
        exit_status = None
    return SystemdSnapshot(
        timer_active_state=timer.get("ActiveState", "unknown"),
        timer_sub_state=timer.get("SubState", "unknown"),
        service_active_state=service.get("ActiveState", "unknown"),
        service_sub_state=service.get("SubState", "unknown"),
        service_result=service.get("Result", "unknown"),
        service_exit_status=exit_status,
    )


def probe_lock_free(path: Path) -> bool:
    """Return True when the existing collector lock can be acquired read-only."""
    if not path.is_file():
        raise FileNotFoundError(path)
    if os.name == "nt":
        raise RuntimeError("collector lock probing requires POSIX flock")
    import fcntl

    with path.open("rb") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        finally:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                pass
    return True


def _utc(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} missing")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{label} is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _age_seconds(now: datetime, timestamp: datetime) -> float:
    return (now - timestamp).total_seconds()


def _age_text(seconds: float | None) -> str:
    if seconds is None:
        return "UNKNOWN"
    if seconds < 0:
        return f"future by {abs(seconds):.0f}s"
    if seconds < 120:
        return f"{seconds:.0f}s"
    return f"{seconds / 60:.0f}m"


def _read_json(path: Path, label: str) -> dict[str, object]:
    if not path.is_file():
        raise FileNotFoundError(f"{label} missing: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} root is not an object")
    return value


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(f"CSV missing: {path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "match_status", "accounting_evidence_status", "accounting_issues",
            "data_source", "prospective_start_utc",
        }
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError("CSV is missing required columns")
        return list(reader)


def evaluate_health(
    config: HealthConfig = HealthConfig(),
    *,
    now: datetime | None = None,
    systemd_snapshot: SystemdSnapshot | None = None,
    systemd_probe: Callable[[str, str], SystemdSnapshot] = probe_systemd,
    lock_probe: Callable[[Path], bool] = probe_lock_free,
) -> HealthResult:
    """Evaluate health without modifying collector or scanner state."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    checks: dict[str, Check] = {}
    metrics: dict[str, object] = {
        "last_success_utc": None,
        "last_success_age_seconds": None,
        "scanned_through_utc": None,
        "state_age_seconds": None,
        "invalid_count": None,
        "ambiguous_count": None,
        "csv_rows": None,
    }

    try:
        systemd = systemd_snapshot or systemd_probe(config.timer_unit, config.service_unit)
        timer_ok = systemd.timer_active_state == "active"
        checks["timer"] = Check(
            OK if timer_ok else FAIL,
            f"{systemd.timer_active_state}/{systemd.timer_sub_state}",
        )
        result_ok = systemd.service_result == "success" and systemd.service_exit_status == 0
        checks["service_result"] = Check(
            OK if result_ok else FAIL,
            f"result={systemd.service_result}, exit={systemd.service_exit_status}",
        )
    except Exception as exc:
        systemd = None
        checks["timer"] = Check(FAIL, f"systemd unavailable: {type(exc).__name__}")
        checks["service_result"] = Check(FAIL, f"systemd unavailable: {type(exc).__name__}")

    state: dict[str, object] | None = None
    evidence: dict[str, object] | None = None
    rows: list[dict[str, str]] | None = None
    artifact_errors: list[str] = []
    for label, path, loader in (
        ("state", config.state_path, lambda p: _read_json(p, "state")),
        ("evidence", config.evidence_path, lambda p: _read_json(p, "evidence")),
        ("CSV", config.csv_path, _read_csv),
    ):
        try:
            value = loader(path)
            if label == "state":
                state = value
            elif label == "evidence":
                evidence = value
            else:
                rows = value
        except Exception as exc:
            artifact_errors.append(f"{label}: {type(exc).__name__}: {exc}")

    if not artifact_errors and state is not None and evidence is not None and rows is not None:
        state_boundary = state.get("prospective_start_utc")
        evidence_boundary = evidence.get("prospective_start_utc")
        csv_boundaries = {row.get("prospective_start_utc", "") for row in rows}
        if state_boundary != evidence_boundary:
            artifact_errors.append("state/evidence boundaries differ")
        if rows and csv_boundaries != {state_boundary}:
            artifact_errors.append("CSV boundary differs from state")
    checks["artifacts"] = Check(
        FAIL if artifact_errors else OK,
        "; ".join(artifact_errors) if artifact_errors else "state, evidence, and CSV readable",
    )

    try:
        if state is None:
            raise ValueError("state unavailable")
        last_success = _utc(state.get("last_successful_collection_utc"), "last success")
        last_age = _age_seconds(now, last_success)
        metrics["last_success_utc"] = last_success.isoformat().replace("+00:00", "Z")
        metrics["last_success_age_seconds"] = round(last_age, 3)
        fresh = -300 <= last_age <= config.max_age_seconds
        checks["last_success"] = Check(
            OK if fresh else FAIL,
            f"age={_age_text(last_age)}, limit={_age_text(config.max_age_seconds)}",
        )

        high_water = state.get("endpoint_high_water")
        if not isinstance(high_water, dict):
            raise ValueError("endpoint_high_water missing")
        scanned = _utc(high_water.get("scanned_through_utc"), "scanned-through high-water")
        scanned_age = _age_seconds(now, scanned)
        metrics["scanned_through_utc"] = scanned.isoformat().replace("+00:00", "Z")
        metrics["state_age_seconds"] = round(scanned_age, 3)
        advancing = (
            -300 <= scanned_age <= config.max_age_seconds
            and abs((last_success - scanned).total_seconds()) <= 300
        )
        checks["state_advancing"] = Check(
            OK if advancing else FAIL,
            f"high-water age={_age_text(scanned_age)}",
        )
    except Exception as exc:
        checks.setdefault("last_success", Check(FAIL, f"unavailable: {type(exc).__name__}: {exc}"))
        checks["state_advancing"] = Check(FAIL, f"unavailable: {type(exc).__name__}: {exc}")

    try:
        free = lock_probe(config.lock_path)
        service_running = systemd is not None and systemd.service_active_state in {"active", "activating"}
        if free:
            checks["lock"] = Check(OK, "FREE")
        elif service_running:
            checks["lock"] = Check(OK, "BUSY (collector service running)")
        else:
            checks["lock"] = Check(FAIL, "STUCK (held while collector service is inactive)")
    except Exception as exc:
        checks["lock"] = Check(FAIL, f"unavailable: {type(exc).__name__}: {exc}")

    if rows is None:
        checks["invalid"] = Check(FAIL, "CSV unavailable")
        checks["ambiguous"] = Check(FAIL, "CSV unavailable")
    else:
        primary = [row for row in rows if row.get("data_source") == "BINANCE_USDM_PROSPECTIVE"]
        invalid_count = sum(invalid_accounting_evidence(row) for row in primary)
        ambiguous_count = sum(row.get("match_status") == "AMBIGUOUS" for row in primary)
        metrics["invalid_count"] = invalid_count
        metrics["ambiguous_count"] = ambiguous_count
        metrics["csv_rows"] = len(rows)
        invalid_increase = invalid_count - config.invalid_baseline
        ambiguous_increase = ambiguous_count - config.ambiguous_baseline
        checks["invalid"] = Check(
            FAIL if invalid_increase > 0 else OK,
            f"count={invalid_count}, baseline={config.invalid_baseline}",
        )
        checks["ambiguous"] = Check(
            WARNING if ambiguous_increase > config.max_ambiguous_increase else OK,
            (
                f"count={ambiguous_count}, baseline={config.ambiguous_baseline}, "
                f"allowed increase={config.max_ambiguous_increase}"
            ),
        )

    return HealthResult(checks=checks, metrics=metrics)


def format_human(result: HealthResult) -> str:
    checks = result.checks
    age = result.metrics.get("last_success_age_seconds")
    age_value = float(age) if isinstance(age, (int, float)) else None
    lock_detail = checks["lock"].detail
    invalid = result.metrics.get("invalid_count")
    ambiguous = result.metrics.get("ambiguous_count")
    lines = [
        "# Binance Execution Truth Health",
        "",
        f"Timer: {checks['timer'].status} ({checks['timer'].detail})",
        f"Last service result: {checks['service_result'].status} ({checks['service_result'].detail})",
        f"Last success age: {_age_text(age_value)} ({checks['last_success'].status})",
        f"State advancing: {checks['state_advancing'].status} ({checks['state_advancing'].detail})",
        f"Lock: {lock_detail}",
        f"INVALID: {invalid if invalid is not None else 'UNKNOWN'} ({checks['invalid'].status})",
        f"AMBIGUOUS: {ambiguous if ambiguous is not None else 'UNKNOWN'} ({checks['ambiguous'].status})",
        f"Artifacts: {checks['artifacts'].status} ({checks['artifacts'].detail})",
        "",
        f"Overall: {result.overall}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Binance execution-truth collector health diagnostic")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--max-age-minutes", type=float, default=30.0)
    parser.add_argument("--invalid-baseline", type=int, default=0)
    parser.add_argument("--ambiguous-baseline", type=int, default=0)
    parser.add_argument("--max-ambiguous-increase", type=int, default=0)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    args = parser.parse_args(argv)
    if args.max_age_minutes <= 0 or min(
        args.invalid_baseline, args.ambiguous_baseline, args.max_ambiguous_increase
    ) < 0:
        parser.error("age and baseline thresholds must be non-negative")
    result = evaluate_health(
        HealthConfig(
            state_path=args.state,
            csv_path=args.csv,
            evidence_path=args.evidence,
            lock_path=args.lock,
            max_age_seconds=args.max_age_minutes * 60,
            invalid_baseline=args.invalid_baseline,
            ambiguous_baseline=args.ambiguous_baseline,
            max_ambiguous_increase=args.max_ambiguous_increase,
        )
    )
    if args.json:
        print(json.dumps(result.as_dict(), sort_keys=True))
    else:
        print(format_human(result))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
