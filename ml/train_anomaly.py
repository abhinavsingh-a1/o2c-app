"""Train the quantity anomaly model (IsolationForest) on the ERP order history.

Usage: uv run --group ml python ml/train_anomaly.py [--no-mlflow]

The steps are importable functions, used by this script and by the Kubeflow pipeline
(ml/pipelines/anomaly_pipeline.py: load_history -> train -> evaluate -> register_and_promote):

- load_labelled_history: completed orders (vbak.status = 'COMPLETED', joined with vbap)
  from the ERP database (ERP_DATABASE_URL from the environment or .env), with the labels
  from poc/data/history_outliers.json.
- fit: features per line (agent.anomaly_model: log quantity, quantity / customer-material
  median, robust z-score for the customer-material, "material never bought by this
  customer" apart from the line itself), an IsolationForest (fixed random_state), and a
  0..1 score calibrated so that the 0.8 threshold in agent/graph.py gives the best F1 on
  the labelled outliers.
- evaluate: precision, recall and F1 on the labelled history outliers, and the false-alarm
  rate: share of normal PO lines (and of POs) scored above 0.8 on poc/data and
  ml/data/holdout, which the model is not trained on. Targets: under 5% of POs flagged,
  recall at least 80%.
- register_and_promote: MLflow (MLFLOW_TRACKING_URI, default http://localhost:5000),
  experiment "quantity-anomaly": params, metrics and the model (forest + statistics +
  calibration as one mlflow.pyfunc model), registered as "o2c-quantity-anomaly". The alias
  "champion" moves to the new version only if its recall is at least 80% and its PO
  false-alarm rate (poc/data + holdout) is lower than the current champion's.
  Loading the model needs the agent package (agent.anomaly_model).
"""

import argparse
import json
import math
import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from agent.anomaly_model import (
    FEATURES,
    LINE_COLUMNS,
    N_ESTIMATORS,
    RANDOM_STATE,
    THRESHOLD,
    QuantityAnomalyModel,
    classification_metrics,
    features,
    raw_scores,
    train,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
OUTLIERS = ROOT / "poc" / "data" / "history_outliers.json"
MASTER = ROOT / "poc" / "data" / "master_data.json"
# Normal PO lines for the false-alarm rate; neither set is used to train the model.
FAR_DATASETS = {
    "poc": ROOT / "poc" / "data",
    "holdout": ROOT / "ml" / "data" / "holdout",
}
TARGET_PO_RATE = 0.05  # at most 5% of normal POs with an anomaly flag
TARGET_RECALL = 0.80  # at least 80% of the labelled history outliers found
EXPERIMENT = "quantity-anomaly"
REGISTERED_MODEL = "o2c-quantity-anomaly"
ALIAS = "champion"
HISTORY_COLUMNS = ["vbeln", "posnr", *LINE_COLUMNS, "is_outlier"]

HISTORY_SQL = text(
    """
    SELECT k.vbeln, p.posnr, k.kunnr, p.matnr, p.kwmeng
    FROM vbak k JOIN vbap p ON p.vbeln = k.vbeln
    WHERE k.status = 'COMPLETED'
    ORDER BY k.vbeln, p.posnr
    """
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    erp_database_url: str
    mlflow_tracking_uri: str = "http://localhost:5000"


# --- step 1: history ---------------------------------------------------------


def load_history(engine) -> pd.DataFrame:
    """Completed order lines: vbeln, posnr and the LINE_COLUMNS."""
    with engine.connect() as conn:
        rows = conn.execute(HISTORY_SQL).all()
    return pd.DataFrame(
        [
            {
                "vbeln": r.vbeln,
                "posnr": int(r.posnr),
                "customer_number": r.kunnr,
                "material_number": r.matnr,
                "quantity": float(r.kwmeng),
            }
            for r in rows
        ],
        columns=["vbeln", "posnr", *LINE_COLUMNS],
    )


def outlier_mask(history: pd.DataFrame, outliers: list[dict]) -> np.ndarray:
    """True for history lines listed in history_outliers.json ({vbeln, posnr, reason})."""
    labelled = {(o["vbeln"], int(o["posnr"])) for o in outliers}
    return np.array(
        [
            (v, int(p)) in labelled
            for v, p in zip(history["vbeln"], history["posnr"], strict=True)
        ],
        dtype=bool,
    )


def load_labelled_history(
    database_url: str, outliers_path: Path | None = None
) -> pd.DataFrame:
    """The completed order history with an is_outlier column (HISTORY_COLUMNS)."""
    outliers_path = OUTLIERS if outliers_path is None else outliers_path
    history = load_history(create_engine(database_url))
    outliers = json.loads(Path(outliers_path).read_text(encoding="utf-8"))
    history["is_outlier"] = outlier_mask(history, outliers)
    missing = len(outliers) - int(history["is_outlier"].sum())
    if missing:
        print(f"warning: {missing} labelled outliers are not in the history")
    return history


def write_history(history: pd.DataFrame, path: str | Path) -> None:
    history[HISTORY_COLUMNS].to_csv(path, index=False)


def read_history(path: str | Path) -> pd.DataFrame:
    return pd.read_csv(
        path,
        dtype={
            "vbeln": str,
            "customer_number": str,
            "material_number": str,
            "is_outlier": bool,
        },
    )


# --- step 2: train -----------------------------------------------------------


def fit(history: pd.DataFrame) -> tuple[QuantityAnomalyModel, dict]:
    return train(history[LINE_COLUMNS], history["is_outlier"].to_numpy(dtype=bool))


def save_model(model: QuantityAnomalyModel, path: str | Path) -> None:
    Path(path).write_bytes(pickle.dumps(model))


def load_model(path: str | Path) -> QuantityAnomalyModel:
    return pickle.loads(Path(path).read_bytes())


# --- step 3: evaluate --------------------------------------------------------


def load_po_lines(dataset: Path, master: dict) -> pd.DataFrame:
    """Order lines of a PO dataset (ground_truth/*.json), with customer and material
    numbers looked up in the master data by name and description (the ground truth has
    no customer number when the PO does not print one)."""
    customer_by_name = {c["name"]: c["customer_number"] for c in master["customers"]}
    material_by_text = {
        m["description"]: m["material_number"] for m in master["materials"]
    }
    rows = []
    for path in sorted((dataset / "ground_truth").glob("*.json")):
        po = json.loads(path.read_text(encoding="utf-8"))
        for line in po["lines"]:
            rows.append(
                {
                    "po": path.stem,
                    "line_no": line["line_no"],
                    "customer_number": customer_by_name[po["customer_name"]],
                    "material_number": material_by_text[line["description"]],
                    "quantity": float(line["quantity"]),
                }
            )
    return pd.DataFrame(rows, columns=["po", "line_no", *LINE_COLUMNS])


def false_alarms(lines: pd.DataFrame, scores: np.ndarray, stats: dict) -> dict:
    """PO lines are all normal, so every score above the threshold is a false alarm.
    Line rate: share of lines flagged. PO rate: share of POs with at least one flag.
    Flags are split into "material never ordered" (no history for that customer-material)
    and "quantity"."""
    flagged = np.asarray(scores) > THRESHOLD
    never = np.array(
        [
            (c, m) not in stats
            for c, m in zip(
                lines["customer_number"], lines["material_number"], strict=True
            )
        ]
    )
    flagged_pos = lines.loc[flagged, "po"].nunique()
    pos = lines["po"].nunique()
    return {
        "lines": len(lines),
        "flagged_lines": int(flagged.sum()),
        "line_rate": float(flagged.mean()) if len(lines) else 0.0,
        "pos": pos,
        "flagged_pos": int(flagged_pos),
        "po_rate": flagged_pos / pos if pos else 0.0,
        "flagged_never_ordered": int((flagged & never).sum()),
        "flagged_quantity": int((flagged & ~never).sum()),
    }


def evaluate_false_alarms(
    score, stats: dict, datasets: dict[str, Path] = FAR_DATASETS
) -> dict:
    """false_alarms per dataset, plus "combined" over all their POs.
    `score` maps a DataFrame of LINE_COLUMNS to scores."""
    master = json.loads(MASTER.read_text(encoding="utf-8"))
    results, all_lines, all_scores = {}, [], []
    for name, dataset in datasets.items():
        lines = load_po_lines(dataset, master)
        lines["po"] = name + "/" + lines["po"]
        scores = np.asarray(score(lines[LINE_COLUMNS].reset_index(drop=True)))
        results[name] = false_alarms(lines, scores, stats)
        all_lines.append(lines)
        all_scores.append(scores)
    results["combined"] = false_alarms(
        pd.concat(all_lines, ignore_index=True), np.concatenate(all_scores), stats
    )
    return results


def history_metrics(model: QuantityAnomalyModel, history: pd.DataFrame) -> dict:
    """Precision, recall and F1 on the labelled history outliers at the 0.8 threshold,
    scored as in training (each line left out of its own purchase count)."""
    x = features(history[LINE_COLUMNS], model.stats, in_history=True)
    scores = model.calibration.score(raw_scores(model.forest, x))
    actual = history["is_outlier"].to_numpy(dtype=bool)
    calibration = model.calibration
    return {
        **classification_metrics(scores > THRESHOLD, actual),
        "lines": len(history),
        "outliers": int(actual.sum()),
        # the raw IsolationForest score that maps to the 0.8 threshold
        "raw_cutoff": calibration.center
        + calibration.scale * math.log(THRESHOLD / (1 - THRESHOLD)),
    }


def evaluate(model: QuantityAnomalyModel, history: pd.DataFrame) -> dict:
    return {
        "history": history_metrics(model, history),
        "false_alarms": evaluate_false_alarms(model.score, model.stats),
    }


def format_false_alarms(far: dict) -> str:
    return "; ".join(
        f"{name}: {r['flagged_pos']}/{r['pos']} POs ({r['po_rate']:.1%}), "
        f"{r['flagged_lines']}/{r['lines']} lines ({r['line_rate']:.1%}; "
        f"{r['flagged_never_ordered']} never ordered, {r['flagged_quantity']} quantity)"
        for name, r in far.items()
    )


def print_report(evaluation: dict) -> None:
    m, far = evaluation["history"], evaluation["false_alarms"]
    print(f"{m['lines']} completed order lines, {m['outliers']} labelled outliers")
    print(
        f"threshold {THRESHOLD}: precision {m['precision']:.1%}, "
        f"recall {m['recall']:.1%}, F1 {m['f1']:.3f} "
        f"(tp {m['tp']}, fp {m['fp']}, fn {m['fn']})"
    )
    print(f"false alarms on normal PO lines - {format_false_alarms(far)}")
    print(
        f"targets: PO false-alarm rate < {TARGET_PO_RATE:.0%} "
        f"({'met' if far['combined']['po_rate'] < TARGET_PO_RATE else 'NOT met'}), "
        f"recall >= {TARGET_RECALL:.0%} ({'met' if m['recall'] >= TARGET_RECALL else 'NOT met'})"
    )


def far_metrics(far: dict, prefix: str = "false_alarm") -> dict[str, float]:
    """MLflow metric names, e.g. false_alarm_po_rate_holdout."""
    return {
        f"{prefix}_{kind}_rate_{name}": float(r[f"{kind}_rate"])
        for name, r in far.items()
        for kind in ("po", "line")
    }


# --- step 4: register and promote --------------------------------------------


def should_promote(evaluation: dict, champion_far: dict | None) -> tuple[bool, str]:
    """Promotion rule for @champion: recall on the history outliers at least 80%, and
    fewer false alarms (PO rate over poc/data and holdout combined) than the current
    champion. Returns the decision and the reason."""
    recall = evaluation["history"]["recall"]
    rate = evaluation["false_alarms"]["combined"]["po_rate"]
    if recall < TARGET_RECALL:
        return False, f"recall {recall:.1%} is below {TARGET_RECALL:.0%}"
    if champion_far is None:
        return True, "there is no champion yet"
    champion_rate = champion_far["combined"]["po_rate"]
    if rate < champion_rate:
        return True, f"PO false-alarm rate {rate:.1%} < champion {champion_rate:.1%}"
    return (
        False,
        f"PO false-alarm rate {rate:.1%} is not below champion {champion_rate:.1%}",
    )


def load_champion_false_alarms(client) -> tuple[str, dict] | None:
    """(version, false alarms) of the current @champion, or None if there is none."""
    import mlflow

    try:
        version = client.get_model_version_by_alias(REGISTERED_MODEL, ALIAS).version
    except Exception:  # noqa: BLE001 - no model or no alias yet
        return None
    champion = mlflow.pyfunc.load_model(f"models:/{REGISTERED_MODEL}@{ALIAS}")
    python_model = champion.unwrap_python_model()
    return version, evaluate_false_alarms(champion.predict, python_model.stats)


def register_and_promote(
    model: QuantityAnomalyModel,
    evaluation: dict,
    example: pd.DataFrame,
    tracking_uri: str,
    tags: dict[str, str] | None = None,
) -> dict:
    """Log and register a new version; move @champion if should_promote says so."""
    import mlflow
    import sklearn
    from mlflow import MlflowClient
    from mlflow.models import infer_signature

    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(EXPERIMENT)
    client = MlflowClient()
    current = load_champion_false_alarms(client)
    m, far = evaluation["history"], evaluation["false_alarms"]
    example = example[LINE_COLUMNS].head(3).reset_index(drop=True)
    with mlflow.start_run(tags=tags) as run:
        mlflow.log_params(
            {
                "model": "IsolationForest",
                "n_estimators": N_ESTIMATORS,
                "random_state": RANDOM_STATE,
                "features": ",".join(FEATURES),
                "threshold": THRESHOLD,
                "history_lines": m["lines"],
                "labelled_outliers": m["outliers"],
                "false_alarm_datasets": ",".join(FAR_DATASETS),
            }
        )
        mlflow.log_metrics(
            {
                **{
                    k: float(m[k])
                    for k in (
                        "precision",
                        "recall",
                        "f1",
                        "tp",
                        "fp",
                        "fn",
                        "raw_cutoff",
                    )
                },
                **far_metrics(far),
            }
        )
        if current:
            mlflow.log_metrics(far_metrics(current[1], prefix="champion_false_alarm"))
        mlflow.log_dict(far, "false_alarms.json")
        info = mlflow.pyfunc.log_model(
            artifact_path="model",
            python_model=model,
            input_example=example,
            signature=infer_signature(example, model.score(example)),
            registered_model_name=REGISTERED_MODEL,
            pip_requirements=[
                f"scikit-learn=={sklearn.__version__}",
                f"pandas=={pd.__version__}",
                f"numpy=={np.__version__}",
                f"mlflow-skinny=={mlflow.__version__}",
            ],
        )
        version = str(
            info.registered_model_version
            or max(
                int(v.version)
                for v in client.search_model_versions(f"name='{REGISTERED_MODEL}'")
                if v.run_id == run.info.run_id
            )
        )
        if current:
            print(f"current @{ALIAS} v{current[0]}: {format_false_alarms(current[1])}")
        promoted, reason = should_promote(evaluation, current[1] if current else None)
        if promoted:
            client.set_registered_model_alias(REGISTERED_MODEL, ALIAS, version)
        mlflow.set_tags(
            {"promoted_to_champion": str(promoted).lower(), "promotion_reason": reason}
        )
        champion = version if promoted else (current[0] if current else None)
        print(
            f"MLflow run {run.info.run_id}: {REGISTERED_MODEL} v{version} registered; "
            f"{'promoted to' if promoted else 'not promoted, champion stays'} "
            f"@{ALIAS}{'' if promoted else f' v{champion}'} ({reason})"
        )
        return {
            "run_id": run.info.run_id,
            "version": version,
            "promoted": promoted,
            "reason": reason,
            "champion_version": champion,
        }


# --- script ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description="Train the quantity anomaly model.")
    parser.add_argument("--no-mlflow", action="store_true", help="train and print only")
    args = parser.parse_args(argv)
    settings = Settings()

    history = load_labelled_history(settings.erp_database_url)
    model, _ = fit(history)
    evaluation = evaluate(model, history)
    print_report(evaluation)

    if not args.no_mlflow:
        register_and_promote(model, evaluation, history, settings.mlflow_tracking_uri)
    return {**evaluation["history"], "false_alarms": evaluation["false_alarms"]}


if __name__ == "__main__":
    main()
