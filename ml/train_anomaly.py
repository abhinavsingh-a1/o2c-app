"""Train the quantity anomaly model (IsolationForest) on the ERP order history.

Usage: uv run --group ml python ml/train_anomaly.py [--no-mlflow]

- Reads completed orders (vbak.status = 'COMPLETED', joined with vbap) from the ERP
  database (ERP_DATABASE_URL from the environment or .env).
- Features per line (agent.anomaly_model): log quantity, quantity / customer-material
  median, robust z-score for the customer-material, and "material never bought by this
  customer" (apart from the line itself).
- Fits an IsolationForest (fixed random_state), turns its output into a 0..1 score and
  calibrates it so that the 0.8 threshold in agent/graph.py gives the best F1 on the
  labelled outliers in poc/data/history_outliers.json; prints precision, recall and F1.
- MLflow (MLFLOW_TRACKING_URI, default http://localhost:5000), experiment
  "quantity-anomaly": params, metrics and the model (forest + statistics + calibration
  as one mlflow.pyfunc model), registered as "o2c-quantity-anomaly" with alias "champion".
  Loading the model needs the agent package (agent.anomaly_model).
"""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from agent.anomaly_model import (
    FEATURES,
    LINE_COLUMNS,
    N_ESTIMATORS,
    RANDOM_STATE,
    THRESHOLD,
    train,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
OUTLIERS = ROOT / "poc" / "data" / "history_outliers.json"
EXPERIMENT = "quantity-anomaly"
REGISTERED_MODEL = "o2c-quantity-anomaly"
ALIAS = "champion"

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
        ]
    )


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description="Train the quantity anomaly model.")
    parser.add_argument("--no-mlflow", action="store_true", help="train and print only")
    args = parser.parse_args(argv)
    settings = Settings()

    history = load_history(create_engine(settings.erp_database_url))
    outliers = json.loads(OUTLIERS.read_text(encoding="utf-8"))
    is_outlier = outlier_mask(history, outliers)
    missing = len(outliers) - int(is_outlier.sum())
    print(
        f"{len(history)} completed order lines, {int(is_outlier.sum())} labelled outliers"
    )
    if missing:
        print(f"warning: {missing} labelled outliers are not in the history")

    model, metrics = train(history[LINE_COLUMNS], is_outlier)
    print(
        f"threshold {THRESHOLD}: precision {metrics['precision']:.1%}, "
        f"recall {metrics['recall']:.1%}, F1 {metrics['f1']:.3f} "
        f"(tp {metrics['tp']}, fp {metrics['fp']}, fn {metrics['fn']})"
    )

    if not args.no_mlflow:
        log_to_mlflow(settings.mlflow_tracking_uri, model, metrics, history)
    return metrics


def log_to_mlflow(
    tracking_uri: str, model, metrics: dict, history: pd.DataFrame
) -> None:
    import mlflow
    import sklearn
    from mlflow import MlflowClient
    from mlflow.models import infer_signature

    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(EXPERIMENT)
    example = history[LINE_COLUMNS].head(3).reset_index(drop=True)
    with mlflow.start_run() as run:
        mlflow.log_params(
            {
                "model": "IsolationForest",
                "n_estimators": N_ESTIMATORS,
                "random_state": RANDOM_STATE,
                "features": ",".join(FEATURES),
                "threshold": THRESHOLD,
                "history_lines": metrics["lines"],
                "labelled_outliers": metrics["outliers"],
            }
        )
        mlflow.log_metrics(
            {
                k: float(metrics[k])
                for k in ("precision", "recall", "f1", "tp", "fp", "fn", "raw_cutoff")
            }
        )
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
        client = MlflowClient()
        version = info.registered_model_version or max(
            int(v.version)
            for v in client.search_model_versions(f"name='{REGISTERED_MODEL}'")
            if v.run_id == run.info.run_id
        )
        client.set_registered_model_alias(REGISTERED_MODEL, ALIAS, str(version))
        print(
            f"MLflow run {run.info.run_id}: {REGISTERED_MODEL} v{version} is now @{ALIAS}"
        )


if __name__ == "__main__":
    main()
