"""Watcher CSV flag contract, including pandas 3 inferred string inputs."""
import pandas as pd
import pytest

from core.durable_journal import read_frame, merge_frame
from position_watcher import ensure_columns, alert_already_sent

FLAGS = ("tp1_alert_sent", "breakeven_recommended", "cornix_be_command_sent",
         "new_stop_notification_sent")


@pytest.mark.parametrize("value", ["0", "1", "1.0", "true", " YES ", "sent", "",
                                  None, "unknown", -1, 0.5, 2])
def test_flag_contract_preserves_existing_delivery_semantics(value):
    frame = pd.DataFrame({column: [value] for column in FLAGS})
    result = ensure_columns(frame)
    for column in FLAGS:
        assert str(result[column].dtype) == "int64"
        assert result.at[0, column] == int(alert_already_sent(value))
        result.at[0, column] = 1
        assert result.at[0, column] == 1


def test_csv_string_flags_accept_updates_without_changing_prices_or_lifecycle(tmp_path):
    path = tmp_path / "signals.csv"
    original = dict(timestamp="2026-10-10T00:00:00Z", symbol="BTCUSDT", side="LONG",
                    entry=0.123456789012345, stop_loss=0.111111111111111,
                    tp1=0.135802466913579, tp2=0.148148144814813,
                    result="OPEN", signal_status="sent", lifecycle_state="TP1_TOUCHED_REMAINDER_OPEN")
    merge_frame(path, pd.DataFrame([{**original, **{column: "0" for column in FLAGS}}]))
    raw = read_frame(path)
    # Force the exact extension-string input even when running on pandas 2.
    for column in FLAGS:
        raw[column] = raw[column].astype("string")
    normalized = ensure_columns(raw)
    assert normalized.attrs == raw.attrs  # Preserve the optimistic merge baseline.
    for column in FLAGS:
        assert str(normalized[column].dtype) == "int64"
        assert normalized.at[0, column] == 0
        normalized.at[0, column] = 1
    for column in ("entry", "stop_loss", "tp1", "tp2"):
        assert normalized.at[0, column] == original[column]
    assert normalized.at[0, "result"] == original["result"]
    assert normalized.at[0, "lifecycle_state"] == original["lifecycle_state"]
    merge_frame(path, normalized)
    saved = read_frame(path)
    for column in FLAGS:
        assert saved.at[0, column] == "1"
    for column in ("entry", "stop_loss", "tp1", "tp2"):
        assert saved.at[0, column] == raw.at[0, column]
    assert saved.at[0, "lifecycle_state"] == original["lifecycle_state"]


@pytest.mark.parametrize("dtype,values", [("string", ["", None, "0", "1"]),
                                         ("Int64", [pd.NA, None, 0, 1])])
def test_nullable_flag_inputs_have_explicit_false_for_missing(dtype, values):
    source = pd.DataFrame({column: pd.Series(values, dtype=dtype) for column in FLAGS})
    result = ensure_columns(source)
    for column in FLAGS:
        assert str(result[column].dtype) == "int64"
        assert result[column].tolist() == [0, 0, 0, 1]
