"""Kubeflow pipeline (KFP SDK v2) that trains and promotes the quantity anomaly model.

    load_history -> train -> evaluate -> register_and_promote

Every component runs in the image o2c-ml:dev (docker/ml.Dockerfile, `make ml-image`) and
calls the same functions as ml/train_anomaly.py, so the script and the pipeline cannot
drift apart. Data moves between components as files: the labelled history (CSV), the
model (pickle) and the evaluation (JSON).

Configuration: the pipeline parameters erp_database_url and mlflow_tracking_uri. The run
scripts (run_local.py, run_kfp.py) fill them from the environment variables
ERP_DATABASE_URL and MLFLOW_TRACKING_URI (or .env), with localhost replaced by
host.docker.internal so that containers reach the database and MLflow on the host.

Compile: uv run --group ml python ml/pipelines/anomaly_pipeline.py
         (writes ml/pipelines/anomaly_pipeline.yaml)
"""

from pathlib import Path

from kfp import compiler, dsl
from kfp.dsl import Artifact, Dataset, Input, Metrics, Model, Output

IMAGE = "o2c-ml:dev"
PIPELINE_NAME = "o2c-quantity-anomaly"
YAML_PATH = Path(__file__).with_name("anomaly_pipeline.yaml")


# The image already contains kfp and the workspace; PYTHONPATH=/app/ml makes
# train_anomaly importable. Imports are inside the functions: KFP ships only their source.


@dsl.component(base_image=IMAGE, install_kfp_package=False)
def load_history(erp_database_url: str, history: Output[Dataset]):
    """Completed ERP orders with the labelled outliers (history_outliers.json)."""
    import train_anomaly

    df = train_anomaly.load_labelled_history(erp_database_url)
    train_anomaly.write_history(df, history.path)
    history.metadata["lines"] = len(df)
    history.metadata["outliers"] = int(df["is_outlier"].sum())
    print(f"{len(df)} history lines, {int(df['is_outlier'].sum())} labelled outliers")


@dsl.component(base_image=IMAGE, install_kfp_package=False)
def train(history: Input[Dataset], model: Output[Model]):
    """IsolationForest + per customer-material statistics + 0.8 calibration."""
    import train_anomaly

    fitted, metrics = train_anomaly.fit(train_anomaly.read_history(history.path))
    train_anomaly.save_model(fitted, model.path)
    model.metadata["raw_cutoff"] = metrics["raw_cutoff"]
    print(
        f"trained on {metrics['lines']} lines, raw cut-off {metrics['raw_cutoff']:.4f}"
    )


@dsl.component(base_image=IMAGE, install_kfp_package=False)
def evaluate(
    history: Input[Dataset],
    model: Input[Model],
    evaluation: Output[Artifact],
    metrics: Output[Metrics],
):
    """Precision, recall, F1 on the history outliers; false alarms on poc/data and holdout."""
    import json
    from pathlib import Path

    import train_anomaly

    result = train_anomaly.evaluate(
        train_anomaly.load_model(model.path), train_anomaly.read_history(history.path)
    )
    Path(evaluation.path).write_text(json.dumps(result, indent=2), encoding="utf-8")
    m, far = result["history"], result["false_alarms"]
    for name in ("precision", "recall", "f1"):
        metrics.log_metric(name, float(m[name]))
    for name, value in train_anomaly.far_metrics(far).items():
        metrics.log_metric(name, value)
    train_anomaly.print_report(result)


@dsl.component(base_image=IMAGE, install_kfp_package=False)
def register_and_promote(
    mlflow_tracking_uri: str,
    history: Input[Dataset],
    model: Input[Model],
    evaluation: Input[Artifact],
) -> str:
    """New MLflow version of o2c-quantity-anomaly; @champion moves only if the PO-level
    false-alarm rate is lower than the champion's and recall >= 80%."""
    import json
    from pathlib import Path

    import train_anomaly

    result = train_anomaly.register_and_promote(
        train_anomaly.load_model(model.path),
        json.loads(Path(evaluation.path).read_text(encoding="utf-8")),
        train_anomaly.read_history(history.path),
        mlflow_tracking_uri,
        tags={"trained_by": "kubeflow-pipeline"},
    )
    return json.dumps(result)


@dsl.pipeline(
    name=PIPELINE_NAME,
    description="Train, evaluate and (maybe) promote the quantity anomaly model.",
)
def anomaly_pipeline(
    erp_database_url: str,
    mlflow_tracking_uri: str = "http://host.docker.internal:5000",
) -> str:
    """Returns the promotion result as JSON (version, promoted, reason, champion)."""
    history = load_history(erp_database_url=erp_database_url)
    model = train(history=history.outputs["history"])
    evaluation = evaluate(
        history=history.outputs["history"], model=model.outputs["model"]
    )
    promotion = register_and_promote(
        mlflow_tracking_uri=mlflow_tracking_uri,
        history=history.outputs["history"],
        model=model.outputs["model"],
        evaluation=evaluation.outputs["evaluation"],
    )
    # Every run must see the current ERP data and MLflow registry: never reuse cached steps.
    for task in (history, model, evaluation, promotion):
        task.set_caching_options(False)
    return promotion.output


def compile_pipeline(path: Path = YAML_PATH) -> Path:
    compiler.Compiler().compile(anomaly_pipeline, str(path))
    return path


if __name__ == "__main__":
    print(f"compiled {compile_pipeline()}")
