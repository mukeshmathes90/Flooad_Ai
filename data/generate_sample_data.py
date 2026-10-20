"""
Generate a SYNTHETIC demonstration dataset for the ML pipeline.

!!! IMPORTANT !!!
There was no genuine historical flood dataset bundled with this project, so this
script produces a *synthetic* time-series that is ONLY meant to make the training
and prediction pipeline runnable and testable.  It is NOT real sensor data and
MUST NOT be presented as a measured flood event.  The training report records the
data source as "synthetic_demo" so nobody mistakes the metrics for real accuracy.

To train on genuine data, replace data/historical_data.csv with your own file (the
expected columns are listed below) or run:

    python -m data.generate_sample_data --help

Columns produced (also the columns the loader understands):
    timestamp            ISO-8601 timestamp (1-minute cadence)
    water_level_cm       water column above the reference point (cm)
    ultrasonic_distance_cm  HC-SR04 distance (cm) = REFERENCE_HEIGHT - water_level
    rainfall_reading     raw 12-bit ADC (0..4095)
    rainfall_intensity   calibrated relative percentage (0..100)
    water_sensor_raw     raw 12-bit ADC (0..4095) of the analogue water input
"""

from __future__ import annotations

import argparse
import csv
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config  # noqa: E402


COLUMNS = [
    "timestamp",
    "water_level_cm",
    "ultrasonic_distance_cm",
    "rainfall_reading",
    "rainfall_intensity",
    "water_sensor_raw",
]


def rain_to_intensity(reading: float) -> float:
    span = config.RAIN_ADC_WET - config.RAIN_ADC_DRY
    pct = (reading - config.RAIN_ADC_DRY) / span * 100.0
    return max(0.0, min(100.0, pct))


def generate(minutes: int = 2880, seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    start = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)

    rows: list[dict] = []
    level = 12.0  # starting water level (cm) - calm / normal

    # Pre-compute a couple of synthetic rain storms to drive the water level.
    storms = []
    t = 120
    while t < minutes - 60:
        duration = rng.randint(60, 240)
        peak = rng.uniform(0.5, 1.0)
        storms.append((t, t + duration, peak))
        t += duration + rng.randint(240, 720)

    def storm_intensity(minute: int) -> float:
        for s, e, peak in storms:
            if s <= minute < e:
                phase = (minute - s) / max(1, (e - s))
                return peak * math.sin(math.pi * phase)
        return 0.0

    for m in range(minutes):
        ts = start + timedelta(minutes=m)
        hour = ts.hour + ts.minute / 60.0
        # gentle diurnal baseline so the model sees temporal structure
        base = 10.0 + 2.0 * math.sin(2 * math.pi * hour / 24.0)

        rain_frac = storm_intensity(m)
        rain_reading = int(min(config.ADC_MAX, config.RAIN_ADC_DRY + rain_frac * (config.RAIN_ADC_WET - config.RAIN_ADC_DRY) + rng.gauss(0, 60)))
        rain_reading = max(0, rain_reading)

        # Water rises with rain (inertia), and slowly drains when it stops.
        inflow = rain_frac * rng.uniform(0.35, 0.65)
        level += inflow - 0.06  # baseline drainage
        level += 0.35 * (base - level) * 0.02  # pull toward baseline
        level += rng.gauss(0, 0.05)
        level = max(0.0, min(config.MAX_WATER_LEVEL_CM - 1.0, level))

        distance = config.REFERENCE_HEIGHT_CM - level + rng.gauss(0, 0.4)
        distance = max(config.ULTRASONIC_MIN_CM, distance)

        ws_raw = int(max(0, min(config.ADC_MAX, (level / config.MAX_WATER_LEVEL_CM) * config.ADC_MAX + rng.gauss(0, 40))))

        rows.append(
            {
                "timestamp": ts.isoformat(),
                "water_level_cm": round(level, 2),
                "ultrasonic_distance_cm": round(distance, 2),
                "rainfall_reading": rain_reading,
                "rainfall_intensity": round(rain_to_intensity(rain_reading), 2),
                "water_sensor_raw": ws_raw,
            }
        )
    return rows


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the synthetic demo dataset.")
    parser.add_argument("--minutes", type=int, default=2880, help="number of 1-minute samples")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=str, default=str(config.HISTORICAL_CSV))
    args = parser.parse_args()

    rows = generate(minutes=args.minutes, seed=args.seed)
    write_csv(rows, Path(args.out))
    print(f"[generate_sample_data] wrote {len(rows)} SYNTHETIC rows -> {args.out}")
    print("[generate_sample_data] This is synthetic demo data, NOT real flood measurements.")


if __name__ == "__main__":
    main()
