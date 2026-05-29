# -*- coding: utf-8 -*-
"""Journal-backed global loss cooldown guard."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass
class LossCooldownStatus:
    active: bool
    loss_streak: int = 0
    pause_until: pd.Timestamp | None = None
    notes: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "loss_streak": self.loss_streak,
            "pause_until": self.pause_until.isoformat() if self.pause_until is not None else "",
            "notes": self.notes or [],
        }


class LossCooldownTracker:
    def __init__(self, journal_path: str | Path, max_losses: int = 3, pause_hours: int = 12) -> None:
        self.journal_path = Path(journal_path)
        self.max_losses = max_losses
        self.pause_hours = pause_hours

    def status(self, now: pd.Timestamp | None = None) -> LossCooldownStatus:
        now = now or pd.Timestamp.now(tz="UTC")
        if now.tzinfo is None:
            now = now.tz_localize("UTC")
        if not self.journal_path.exists():
            return LossCooldownStatus(False, notes=["journal missing"])
        try:
            df = pd.read_csv(self.journal_path)
        except (pd.errors.EmptyDataError, FileNotFoundError, OSError):
            return LossCooldownStatus(False, notes=["journal unavailable"])
        required = {"result", "closed_at"}
        if df.empty or not required.issubset(df.columns):
            return LossCooldownStatus(False, notes=["outcome columns missing"])

        df = df.copy()
        df["closed_at"] = pd.to_datetime(df["closed_at"], utc=True, errors="coerce")
        outcomes = df[df["result"].astype(str).str.upper().isin(["WIN", "LOSS"]) & df["closed_at"].notna()]
        if outcomes.empty:
            return LossCooldownStatus(False, notes=["no closed outcomes"])

        outcomes = outcomes.sort_values("closed_at", ascending=False)
        streak = 0
        latest_loss_at = None
        for _, row in outcomes.iterrows():
            if str(row["result"]).upper() != "LOSS":
                break
            streak += 1
            if latest_loss_at is None:
                latest_loss_at = row["closed_at"]

        if streak < self.max_losses or latest_loss_at is None:
            return LossCooldownStatus(False, loss_streak=streak, notes=["loss streak below threshold"])

        pause_until = latest_loss_at + pd.Timedelta(hours=self.pause_hours)
        if now < pause_until:
            return LossCooldownStatus(
                True,
                loss_streak=streak,
                pause_until=pause_until,
                notes=[f"{streak} consecutive losses; paused until {pause_until.isoformat()}"],
            )
        return LossCooldownStatus(False, loss_streak=streak, pause_until=pause_until, notes=["cooldown expired"])
