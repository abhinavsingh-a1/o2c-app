"""Fine-tune a multilingual DistilBERT document classifier on CPU (E10, see ADR-005).

Usage: uv run --group ml python ml/train_classifier.py [--no-mlflow]

- Data: ml/data/classifier/train/labels.csv, stratified 80/20 train/validation split
  with a fixed seed. Input text: the first 1500 characters of o2c_common.pdf.pdf_text.
- Training: plain PyTorch loop (no Trainer), max_length 256, batch 16, AdamW lr 5e-5,
  3 epochs; loss and validation accuracy are printed per epoch.
- Evaluation on ml/data/classifier/holdout: accuracy, confusion matrix, mistakes and
  average ms per document (tokenizer + forward pass, one document at a time), written to
  ml/results/classifier-distilbert-holdout.json; the model is saved to
  ml/results/doc-classifier-model/.
- MLflow (MLFLOW_TRACKING_URI, default http://localhost:5000), experiment "doc-classifier":
  params, per-epoch metrics, holdout metrics, the confusion matrix as an artifact and the
  model (mlflow.transformers, task text-classification), registered as "o2c-doc-classifier".
"""

import argparse
import os
import random
import time
from contextlib import nullcontext
from pathlib import Path

import numpy as np
import torch
from classifier_common import (
    ID2LABEL,
    LABEL2ID,
    LABELS,
    MAX_CHARS,
    format_matrix,
    label_ids,
    prepare_text,
    print_summary,
    read_labels,
    summarize,
    write_results,
)
from sklearn.model_selection import train_test_split
from torch.utils.data import DataLoader, TensorDataset
from transformers import AutoModelForSequenceClassification, AutoTokenizer

ML_DIR = Path(__file__).resolve().parent
TRAIN_DIR = ML_DIR / "data" / "classifier" / "train"
HOLDOUT_DIR = ML_DIR / "data" / "classifier" / "holdout"
RESULTS_DIR = ML_DIR / "results"
MODEL_DIR = RESULTS_DIR / "doc-classifier-model"

BASE_MODEL = "distilbert-base-multilingual-cased"  # German and English documents
EXPERIMENT = "doc-classifier"
REGISTERED_MODEL = "o2c-doc-classifier"
PARAMS = {
    "base_model": BASE_MODEL,
    "max_chars": MAX_CHARS,
    "max_length": 256,
    "batch_size": 16,
    "learning_rate": 5e-5,
    "epochs": 3,
    "validation_size": 0.2,
    "seed": 2026,
    "device": "cpu",
}
DEVICE = torch.device("cpu")


def seed_everything(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def load_documents(dataset: Path) -> tuple[list[str], list[str], list[int]]:
    """(files, model input texts, class ids) for every row of labels.csv."""
    rows = read_labels(dataset)
    files = [file for file, _ in rows]
    texts = [prepare_text((dataset / file).read_bytes()) for file in files]
    return files, texts, label_ids([doc_type for _, doc_type in rows])


def stratified_split(labels: list[int], seed: int) -> tuple[list[int], list[int]]:
    """Indices of an 80/20 train/validation split with the same class mix in both."""
    indices = list(range(len(labels)))
    train, validation = train_test_split(
        indices, test_size=PARAMS["validation_size"], stratify=labels, random_state=seed
    )
    return train, validation


def make_loader(
    tokenizer, texts: list[str], labels: list[int], shuffle: bool
) -> DataLoader:
    encoded = tokenizer(
        texts,
        truncation=True,
        max_length=PARAMS["max_length"],
        padding=True,
        return_tensors="pt",
    )
    data = TensorDataset(
        encoded["input_ids"], encoded["attention_mask"], torch.tensor(labels)
    )
    generator = torch.Generator().manual_seed(PARAMS["seed"])
    return DataLoader(
        data, batch_size=PARAMS["batch_size"], shuffle=shuffle, generator=generator
    )


def train_epoch(model, loader: DataLoader, optimizer) -> float:
    model.train()
    total, count = 0.0, 0
    for input_ids, attention_mask, labels in loader:
        optimizer.zero_grad()
        out = model(input_ids=input_ids, attention_mask=attention_mask, labels=labels)
        out.loss.backward()
        optimizer.step()
        total += out.loss.item() * len(labels)
        count += len(labels)
    return total / count


@torch.inference_mode()
def accuracy(model, loader: DataLoader) -> float:
    model.eval()
    correct = total = 0
    for input_ids, attention_mask, labels in loader:
        logits = model(input_ids=input_ids, attention_mask=attention_mask).logits
        correct += (logits.argmax(dim=-1) == labels).sum().item()
        total += len(labels)
    return correct / total


@torch.inference_mode()
def classify_one(model, tokenizer, text: str) -> tuple[str, float]:
    """(doc type, probability) for one document."""
    encoded = tokenizer(
        text, truncation=True, max_length=PARAMS["max_length"], return_tensors="pt"
    )
    probs = model(**encoded).logits.softmax(dim=-1)[0]
    best = int(probs.argmax())
    return ID2LABEL[best], float(probs[best])


def evaluate_holdout(model, tokenizer, dataset: Path) -> list[dict]:
    model.eval()
    files, texts, labels = load_documents(dataset)
    rows = []
    for file, text, label in zip(files, texts, labels, strict=True):
        start = time.perf_counter()
        predicted, probability = classify_one(model, tokenizer, text)
        rows.append(
            {
                "file": file,
                "expected": ID2LABEL[label],
                "predicted": predicted,
                "reason": f"p={probability:.2f}",
                "ms": round((time.perf_counter() - start) * 1000, 1),
            }
        )
    return rows


def model_requirements() -> list[str]:
    """pip requirements stored with the MLflow model (the CPU build's +cpu suffix dropped)."""
    import mlflow
    import tokenizers
    import transformers

    return [
        f"torch=={torch.__version__.split('+')[0]}",
        f"transformers=={transformers.__version__}",
        f"tokenizers=={tokenizers.__version__}",
        f"mlflow-skinny=={mlflow.__version__}",
    ]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Fine-tune the document classifier (CPU)."
    )
    parser.add_argument(
        "--no-mlflow", action="store_true", help="train without MLflow logging"
    )
    args = parser.parse_args(argv)

    seed_everything(PARAMS["seed"])
    _, texts, labels = load_documents(TRAIN_DIR)
    train_idx, val_idx = stratified_split(labels, PARAMS["seed"])
    print(
        f"{len(train_idx)} training and {len(val_idx)} validation documents, labels {LABELS}"
    )

    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
    model = AutoModelForSequenceClassification.from_pretrained(
        BASE_MODEL, num_labels=len(LABELS), id2label=ID2LABEL, label2id=LABEL2ID
    ).to(DEVICE)
    train_loader = make_loader(
        tokenizer,
        [texts[i] for i in train_idx],
        [labels[i] for i in train_idx],
        shuffle=True,
    )
    val_loader = make_loader(
        tokenizer,
        [texts[i] for i in val_idx],
        [labels[i] for i in val_idx],
        shuffle=False,
    )
    optimizer = torch.optim.AdamW(model.parameters(), lr=PARAMS["learning_rate"])

    mlflow = None
    if not args.no_mlflow:
        import mlflow

        mlflow.set_tracking_uri(
            os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5000")
        )
        mlflow.set_experiment(EXPERIMENT)

    with mlflow.start_run() if mlflow else nullcontext() as run:
        if mlflow:
            mlflow.log_params(
                {
                    **PARAMS,
                    "train_documents": len(train_idx),
                    "validation_documents": len(val_idx),
                }
            )
        for epoch in range(1, PARAMS["epochs"] + 1):
            start = time.perf_counter()
            loss = train_epoch(model, train_loader, optimizer)
            val_accuracy = accuracy(model, val_loader)
            print(
                f"epoch {epoch}: loss {loss:.4f}, validation accuracy {val_accuracy:.1%} "
                f"({time.perf_counter() - start:.0f} s)",
                flush=True,
            )
            if mlflow:
                mlflow.log_metrics(
                    {"train_loss": loss, "val_accuracy": val_accuracy}, step=epoch
                )

        rows = evaluate_holdout(model, tokenizer, HOLDOUT_DIR)
        summary = summarize(rows)
        print_summary(summary)

        model.save_pretrained(MODEL_DIR)
        tokenizer.save_pretrained(MODEL_DIR)
        results = write_results(
            RESULTS_DIR / "classifier-distilbert-holdout.json",
            HOLDOUT_DIR,
            summary,
            rows,
            classifier="distilbert",
            model=f"{BASE_MODEL} (fine-tuned)",
            mlflow_run_id=run.info.run_id if mlflow else None,
        )
        print(f"wrote {results} and {MODEL_DIR}")

        if mlflow:
            mlflow.log_metrics(
                {
                    "holdout_accuracy": summary["accuracy"],
                    "holdout_mistakes": len(summary["mistakes"]),
                    "holdout_ms_per_document": summary["ms_per_document"],
                }
            )
            mlflow.log_dict(summary["confusion_matrix"], "confusion_matrix.json")
            mlflow.log_text(
                format_matrix(summary["confusion_matrix"]), "confusion_matrix.txt"
            )
            mlflow.log_artifact(str(results))
            mlflow.transformers.log_model(
                transformers_model={"model": model, "tokenizer": tokenizer},
                artifact_path="model",
                task="text-classification",
                registered_model_name=REGISTERED_MODEL,
                input_example=texts[0][:300],
                # Explicit, because MLflow 2.17 otherwise adds torchvision, which we do not use.
                pip_requirements=model_requirements(),
            )
            print(
                f"MLflow run {run.info.run_id}, model registered as {REGISTERED_MODEL}"
            )


if __name__ == "__main__":
    main()
