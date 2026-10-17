"""Explicit UTC instants and millisecond API epochs, independent of pandas storage."""
from numbers import Number
import pandas as pd


def parse_utc_timestamp(value):
    """Parse text/datetime; naive values follow the project's UTC convention.

    Numeric inputs are rejected: an epoch requires a named unit.
    """
    if isinstance(value, Number):
        raise TypeError("Numeric timestamp requires an explicit epoch unit")
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError("Missing UTC timestamp")
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def timestamp_to_epoch_ms(value):
    """Floor to whole milliseconds, including instants before the epoch."""
    return int(parse_utc_timestamp(value).as_unit("ms").asm8.astype("int64"))


def timestamp_from_epoch_ms(value):
    return pd.Timestamp(value, unit="ms", tz="UTC")


def canonical_utc_iso(value):
    """Fixed nine fractional digits preserve nanoseconds and sort chronologically."""
    stamp = parse_utc_timestamp(value)
    return (stamp.strftime("%Y-%m-%dT%H:%M:%S")
            + f".{stamp.microsecond * 1000 + stamp.nanosecond:09d}Z")
