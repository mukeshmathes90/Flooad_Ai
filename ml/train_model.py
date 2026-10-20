"""
Train the flood water-level forecasting model.

Pipeline
--------
1. Load the historical CSV (default data/historical_data.csv).
2. Clean: parse timestamps, drop invalid/missing water levels, sort, de-duplicate.
3. Build leak-free sliding-window features (ml/features.py) and future targets for
   each configured forecast horizon.
4. Chronological train / validation / test split (never shuffled).
5. Train a RandomForest multi-output regressor.
6. Evaluate MAE / RMSE / R^2 on the held-out validation and test sets.
7. Persist model + feature metadata + a reproducible evaluation report.

Run:
    python -m ml.train_model                       # use data/historical_data.csv
    python -m ml.train_model --input mydata.csv    # use genuine data
    python -m ml.train_model --generate            # (re)create synthetic demo data
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config  # noqa: E402
from ml import features  # noqa: E402

try:
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.multioutput import MultiOutputRegressor
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
    import joblib
except ImportError as exc:  # pragma: no cover
    print(f"[train_model] Missing ML dependency: {exc}")
    print("[train_model] Install with: pip install -r requirements.txt")
    raise SystemExit(1)


# --------------------------------------------------------------------------- #
# Loading / cleaning
# --------------------------------------------------------------------------- #
def _to_float(value) -> float | None:
    if value is None:
        return None
    s = str(value).strip()
    if s == "" or s.lower() in {"nan", "na", "null", "none", "-"}:
        return None
    try:
        out = float(s)
    except ValueError:
        return None
    if np.isnan(out) or np.isinf(out):
        return None
    return out


def load_rows(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"historical data not found: {path}")
    rows: list[dict] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            wl = _to_float(raw.get("water_level_cm"))
            if wl is None:
                continue  # cannot train on a row without the target signal
            ts = (raw.get("timestamp") or raw.get("device_timestamp") or "").strip()
            rows.append(
                {
                    "timestamp": ts,
                    "water_level_cm": wl,
                    "ultrasonic_distance_cm": _to_float(raw.get("ultrasonic_distance_cm")),
                    "rainfall_reading": _to_float(raw.get("rainfall_reading")),
                    "rainfall_intensity": _to_float(raw.get("rainfall_intensity")),
                    "water_sensor_raw": _to_float(raw.get("water_sensor_raw")),
                }
            )

    # sort chronologically when timestamps exist
    if rows and any(r["timestamp"] for r in rows):
        rows.sort(key=lambda r: (r["timestamp"] or ""))

    # de-duplicate on timestamp (keep the last occurrence)
    seen: dict[str, dict] = {}
    deduped: list[dict] = []
    for r in rows:
        key = r["timestamp"]
        if key and key in seen:
            continue
        if key:
            seen[key] = r
        deduped.append(r)
    return deduped


# --------------------------------------------------------------------------- #
# Dataset construction
# --------------------------------------------------------------------------- #
def horizon_samples(horizon_min: float, interval_min: float) -> int:
    return max(1, int(round(horizon_min / interval_min)))


def build_dataset(rows: list[dict], horizons_min: list[float], interval_min: float):
    hs = [horizon_samples(h, interval_min) for h in horizons_min]
    max_h = max(hs)
    X: list[list[float]] = []
    Y: list[list[float]] = []
    for i in range(features.WINDOW - 1, len(rows) - max_h):
        window = rows[i - features.WINDOW + 1 : i + 1]
        try:
            X.append(features.build_feature_vector(window))
        except features.InsufficientData:
            continue
        Y.append([rows[i + h]["water_level_cm"] for h in hs])
    return np.asarray(X, dtype=float), np.asarray(Y, dtype=float), hs


def chronological_split(n: int, train_frac=0.70, val_frac=0.15):
    n_train = int(n * train_frac)
    n_val = int(n * val_frac)
    return slice(0, n_train), slice(n_train, n_train + n_val), slice(n_train + n_val, n)


def _metrics(y_true: np.ndarray, y_pred: np.ndarray, horizons_min, name: str) -> dict:
    out = {}
    for j, h in enumerate(horizons_min):
        yt, yp = y_true[:, j], y_pred[:, j]
        out[str(h)] = {
            "mae_cm": round(float(mean_absolute_error(yt, yp)), 4),
            "rmse_cm": round(float(np.sqrt(mean_squared_error(yt, yp))), 4),
            "r2": round(float(r2_score(yt, yp)) if len(yt) > 1 else float("nan"), 4),
            "n": int(len(yt)),
        }
    return out


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def train(input_path: Path, data_source_label: str, out_dir: Path) -> dict:
    rows = load_rows(input_path)
    if len(rows) < features.WINDOW + max(horizon_samples(h, config.SAMPLE_INTERVAL_MIN) for h in config.FORECAST_HORIZONS_MIN) + 20:
        raise SystemExit(
            f"[train_model] Not enough clean rows ({len(rows)}) to train. "
            "Supply a longer genuine historical dataset via --input."
        )

    X, Y, hs = build_dataset(rows, config.FORECAST_HORIZONS_MIN, config.SAMPLE_INTERVAL_MIN)
    if len(X) < 30:
        raise SystemExit(f"[train_model] Only {len(X)} feature rows; need at least 30.")

    tr, va, te = chronological_split(len(X))
    model = MultiOutputRegressor(
        RandomForestRegressor(
            n_estimators=250,
            min_samples_leaf=2,
            random_state=42,
            n_jobs=-1,
        )
    )
    model.fit(X[tr], Y[tr])

    val_metrics = _metrics(Y[va], model.predict(X[va]), config.FORECAST_HORIZONS_MIN, "val")
    test_metrics = _metrics(Y[te], model.predict(X[te]), config.FORECAST_HORIZONS_MIN, "test")

    # Persist model
    out_dir.mkdir(parents=True, exist_ok=True)
    config.MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, config.MODEL_PATH)

    meta = {
        "model_version": config.MODEL_VERSION,
        "model_type": "MultiOutputRegressor(RandomForestRegressor)",
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "feature_names": features.FEATURE_NAMES,
        "window": features.WINDOW,
        "horizons_min": config.FORECAST_HORIZONS_MIN,
        "horizon_samples": hs,
        "sample_interval_min": config.SAMPLE_INTERVAL_MIN,
        "reference_height_cm": config.REFERENCE_HEIGHT_CM,
        "data_source": data_source_label,
        "n_feature_rows": int(len(X)),
        "model_file": str(config.MODEL_PATH.name),
    }
    with config.FEATURE_META_PATH.open("w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)

    report = {
        **meta,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_data_rows": len(rows),
        "n_train": int(len(X[tr])),
        "n_val": int(len(X[va])),
        "n_test": int(len(X[te])),
        "target_stats": {
            "mean_cm": round(float(np.mean(Y)), 3),
            "std_cm": round(float(np.std(Y)), 3),
            "min_cm": round(float(np.min(Y)), 3),
            "max_cm": round(float(np.max(Y)), 3),
        },
        "metrics": {"validation": val_metrics, "test": test_metrics},
        "metrics_unit": "cm",
    }
    with config.EVAL_REPORT_PATH.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    # Console summary
    print("=" * 68)
    print("Flood model trained")
    print(f"  data source : {data_source_label}")
    print(f"  rows        : {len(rows)}  (feature rows: {len(X)})")
    print(f"  split       : train={len(X[tr])} val={len(X[va])} test={len(X[te])}")
    print(f"  model       : {config.MODEL_PATH}")
    print(f"  report      : {config.EVAL_REPORT_PATH}")
    print("  TEST metrics (held-out):")
    for h, m in test_metrics.items():
        print(f"    +{h:>2} min  MAE={m['mae_cm']:.3f} cm  RMSE={m['rmse_cm']:.3f} cm  R2={m['r2']}")
    print("=" * 68)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Train the flood forecasting model.")
    parser.add_argument("--input", type=str, default=str(config.HISTORICAL_CSV),
                        help="path to a historical CSV")
    parser.add_argument("--generate", action="store_true",
                        help="regenerate the synthetic demo dataset first")
    args = parser.parse_args()

    input_path = Path(args.input)
    explicit_input = args.input != str(config.HISTORICAL_CSV)
    if args.generate:
        from data.generate_sample_data import generate, write_csv  # noqa
        write_csv(generate(), input_path)
        print(f"[train_model] regenerated synthetic dataset -> {input_path}")

    if not input_path.exists():
        if explicit_input:
            # Do NOT silently fabricate a dataset at a mistyped path and mislabel
            # it - fail loudly instead.
            raise SystemExit(
                f"[train_model] input CSV not found: {input_path}\n"
                "           Fix the --input path, or run without --input to use "
                f"{config.HISTORICAL_CSV}."
            )
        print(f"[train_model] {input_path} missing; generating synthetic demo dataset.")
        from data.generate_sample_data import generate, write_csv  # noqa
        write_csv(generate(), input_path)

    synthetic = input_path.resolve() == config.HISTORICAL_CSV.resolve()
    label = "synthetic_demo" if synthetic else f"file:{input_path.name}"
    train(input_path, label, config.MODEL_DIR)


if __name__ == "__main__":
    main()
