"""Upload the extraction evaluation sets to Langfuse as datasets (ADR-010).

Usage: uv run --group ml python ml/langfuse_datasets.py

poc/data -> dataset "golden" (50 POs), ml/data/holdout -> dataset "holdout" (20 POs).
Each PO is one item: input {"filename", "text"} (the layout-mode text the extractor
reads, o2c_common.pdf.pdf_text), expected_output the ground-truth JSON. Item ids are
fixed ("<dataset>-<PO id>"), so a re-run updates changed items in place and never
duplicates them; unchanged items are not sent again.
"""

import json
import logging
from pathlib import Path

from o2c_common.pdf import pdf_text

REPO_ROOT = Path(__file__).resolve().parents[1]
DATASETS = {
    "golden": REPO_ROOT / "poc" / "data",
    "holdout": REPO_ROOT / "ml" / "data" / "holdout",
}
DESCRIPTIONS = {
    "golden": "poc/data: 50 synthetic POs, six layouts (E2 golden set)",
    "holdout": "ml/data/holdout: 20 POs, two unseen layouts; final measurement only",
}


def source_name(dataset_dir: Path) -> str:
    """Repository-relative path (no local absolute paths in Langfuse)."""
    try:
        return dataset_dir.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return dataset_dir.name


def item_id(dataset_name: str, po_id: str) -> str:
    return f"{dataset_name}-{po_id}"


def dataset_items(dataset_name: str, dataset_dir: Path) -> list[dict]:
    """One item per PDF in <dataset_dir>/pos, with its ground truth."""
    items = []
    for pdf in sorted((dataset_dir / "pos").glob("*.pdf")):
        truth = (dataset_dir / "ground_truth" / f"{pdf.stem}.json").read_text(
            encoding="utf-8"
        )
        items.append(
            {
                "id": item_id(dataset_name, pdf.stem),
                "input": {
                    "filename": pdf.name,
                    "text": pdf_text(pdf.read_bytes(), drop_invisible=True),
                },
                "expected_output": json.loads(truth),
                "metadata": {
                    "po_id": pdf.stem,
                    "source": source_name(dataset_dir),
                },
            }
        )
    return items


def existing_items(client, dataset_name: str) -> dict[str, object] | None:
    """id -> item of an existing dataset, None if the dataset does not exist."""
    sdk_log = logging.getLogger("langfuse")
    sdk_log.disabled = True  # the SDK logs "Internal error" for a dataset not found
    try:
        dataset = client.get_dataset(dataset_name)
    except Exception:  # noqa: BLE001 - the SDK raises a NotFoundError subclass
        return None
    finally:
        sdk_log.disabled = False
    return {item.id: item for item in dataset.items}


def unchanged(existing, item: dict) -> bool:
    return (
        existing is not None
        and existing.input == item["input"]
        and existing.expected_output == item["expected_output"]
        and existing.metadata == item["metadata"]
    )


def upload(client, dataset_name: str, items: list[dict], description: str = "") -> dict:
    """Create the dataset if needed and upsert changed or new items by id.
    Returns {"created": n, "updated": n, "unchanged": n}."""
    existing = existing_items(client, dataset_name)
    if existing is None:
        client.create_dataset(name=dataset_name, description=description)
        existing = {}
    counts = {"created": 0, "updated": 0, "unchanged": 0}
    for item in items:
        current = existing.get(item["id"])
        if unchanged(current, item):
            counts["unchanged"] += 1
            continue
        client.create_dataset_item(
            dataset_name=dataset_name,
            id=item["id"],
            input=item["input"],
            expected_output=item["expected_output"],
            metadata=item["metadata"],
        )
        counts["updated" if current is not None else "created"] += 1
    return counts


def main() -> dict[str, dict]:
    from langfuse_common import make_langfuse

    client = make_langfuse()
    results = {}
    for name, directory in DATASETS.items():
        results[name] = upload(
            client, name, dataset_items(name, directory), DESCRIPTIONS[name]
        )
        print(f"{name}: {results[name]}")
    client.flush()
    return results


if __name__ == "__main__":
    main()
