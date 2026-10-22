import logging
from pathlib import Path
from typing import Literal

import boto3
from botocore.exceptions import ClientError
from o2c_common.events import Envelope
from o2c_common.kafka import EventConsumer, EventProducer, PermanentError, PostgresStore
from o2c_common.llm import strict_response_format
from o2c_common.metrics import (
    CLASSIFIED_BY,
    DOC_TYPES,
    DOCUMENTS_CLASSIFIED,
    init_labels,
    llm_http_client,
    start_health_server,
)
from o2c_common.pdf import pdf_text
from openai import OpenAI
from pydantic import BaseModel, ConfigDict
from pydantic_settings import BaseSettings, SettingsConfigDict

from classifier.hybrid import DistilBert, classify_hybrid, load_distilbert

REPO_ROOT = Path(__file__).resolve().parents[4]
# Chosen on the classifier validation split (ml/choose_threshold.py, ADR-009): all non-PO
# predictions there are correct, so it is 0.0 + the 0.005 margin. Below 0.25 (the lowest
# possible top softmax score of 4 classes) it does not bind yet; it does once a model
# makes a non-PO mistake on validation.
DEFAULT_NON_PO_THRESHOLD = 0.005


class Classification(BaseModel):
    # Strict structured outputs: no extra keys, every field required (ADR-008).
    model_config = ConfigDict(extra="forbid")
    doc_type: Literal["purchase_order", "invoice", "remittance", "other"]
    reason: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    kafka_bootstrap: str = "localhost:9092"
    s3_endpoint_url: str = "http://localhost:8333"
    s3_access_key: str
    s3_secret_key: str
    litellm_base_url: str = "http://localhost:4000"
    litellm_master_key: str
    events_database_url: str
    prompts_dir: Path = REPO_ROOT / "prompts"
    health_port: int = 8301
    mlflow_tracking_uri: str = "http://localhost:5000"
    classifier_non_po_threshold: float = DEFAULT_NON_PO_THRESHOLD


def load_prompt(prompts_dir: Path) -> str:
    # The schema goes to the API as a strict response_format, not into the prompt.
    return (prompts_dir / "classify.md").read_text(encoding="utf-8")


def classify(
    llm: OpenAI, system_prompt: str, text: str, event: Envelope
) -> Classification:
    response = llm.chat.completions.create(
        model="o2c-extract",
        temperature=0,
        response_format=strict_response_format(Classification),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text[:4000]},
        ],
        extra_body={
            "metadata": {
                "trace_name": "classify",
                "session_id": event.correlation_id,
                "tags": ["classifier"],
            }
        },
    )
    return Classification.model_validate_json(response.choices[0].message.content)


def make_handler(
    s3,
    llm: OpenAI,
    producer: EventProducer,
    system_prompt: str,
    local: DistilBert | None = None,
    non_po_threshold: float = DEFAULT_NON_PO_THRESHOLD,
):
    """`local` is the DistilBERT model; None sends every document to GPT."""

    def handle(event: Envelope) -> None:
        p = event.payload
        try:
            pdf = s3.get_object(Bucket=p["s3_bucket"], Key=p["s3_key"])["Body"].read()
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                raise PermanentError(f"document missing: {p['s3_key']}") from exc
            raise

        def gpt(text: str) -> tuple[str, str]:
            result = classify(llm, system_prompt, text, event)
            return result.doc_type, result.reason

        result = classify_hybrid(
            pdf_text(pdf, drop_invisible=True), local, non_po_threshold, gpt
        )
        DOCUMENTS_CLASSIFIED.labels(result.doc_type, result.classified_by).inc()
        producer.send(
            "docs.classified",
            event.child(
                "docs.classified",
                "classifier",
                {
                    **p,
                    "doc_type": result.doc_type,
                    "reason": result.reason,
                    "classified_by": result.classified_by,
                    "confidence": result.confidence,
                    "routing_reason": result.routing_reason,
                },
            ),
        )

    return handle


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    settings = Settings()
    init_labels(DOCUMENTS_CLASSIFIED, DOC_TYPES, CLASSIFIED_BY)
    start_health_server(settings.health_port)
    s3 = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
    )
    llm = OpenAI(
        base_url=settings.litellm_base_url,
        api_key=settings.litellm_master_key,
        http_client=llm_http_client("classifier"),  # o2c_llm_cost_usd_total
    )
    producer = EventProducer(settings.kafka_bootstrap, "classifier")
    EventConsumer(
        bootstrap=settings.kafka_bootstrap,
        group_id="classifier",
        topics=["docs.received"],
        handler=make_handler(
            s3,
            llm,
            producer,
            load_prompt(settings.prompts_dir),
            local=load_distilbert(settings.mlflow_tracking_uri),
            non_po_threshold=settings.classifier_non_po_threshold,
        ),
        producer=producer,
        store=PostgresStore(settings.events_database_url),
    ).run()


if __name__ == "__main__":
    main()
