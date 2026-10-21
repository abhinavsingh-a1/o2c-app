import json
import logging
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import boto3
import httpx
from botocore.exceptions import ClientError
from fastapi import FastAPI, File, HTTPException, UploadFile
from langchain_openai import ChatOpenAI
from o2c_common.events import Envelope
from o2c_common.kafka import EventConsumer, EventProducer, PermanentError, PostgresStore
from o2c_common.pdf import pdf_text
from o2c_common.schema import PurchaseOrder
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[4]
log = logging.getLogger("extractor")

Extract = Callable[
    [str, str], PurchaseOrder
]  # (pdf text, session id) -> purchase order


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


def load_prompt(prompts_dir: Path) -> str:
    template = (prompts_dir / "extract.md").read_text(encoding="utf-8")
    return template.replace("{schema}", json.dumps(PurchaseOrder.model_json_schema()))


def wrap_document(text: str) -> str:
    """The PDF text as the user message: inside <document> tags, so the prompt can tell
    the model that everything in them is data, never instructions."""
    return f"<document>\n{text}\n</document>"


def make_extractor(
    settings: Settings,
    model: str = "o2c-extract",
    http_client: httpx.Client | None = None,
) -> Extract:
    """`model` is a LiteLLM alias; evaluations pass another alias to compare models,
    and an `http_client` to read LiteLLM's response headers (cost). `trace_id` lets an
    evaluation choose the Langfuse trace id of a call (to link it to a dataset item)."""
    system_prompt = load_prompt(settings.prompts_dir)

    def extract(
        text: str, session_id: str, trace_id: str | None = None
    ) -> PurchaseOrder:
        metadata = {
            "trace_name": "extract",
            "session_id": session_id,
            "tags": ["extractor"],
        }
        if trace_id:
            metadata["trace_id"] = trace_id
        llm = ChatOpenAI(
            model=model,
            base_url=settings.litellm_base_url,
            api_key=settings.litellm_master_key,
            temperature=0,
            http_client=http_client,
            extra_body={"metadata": metadata},
        )
        chain = llm.with_structured_output(PurchaseOrder, method="json_mode")
        return chain.invoke([("system", system_prompt), ("human", wrap_document(text))])

    return extract


def make_handler(s3, extract: Extract, producer: EventProducer):
    def handle(event: Envelope) -> None:
        p = event.payload
        if p.get("doc_type") != "purchase_order":
            log.info("skipping %s: doc_type=%s", event.event_id, p.get("doc_type"))
            return
        try:
            pdf = s3.get_object(Bucket=p["s3_bucket"], Key=p["s3_key"])["Body"].read()
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                raise PermanentError(f"document missing: {p['s3_key']}") from exc
            raise
        po = extract(pdf_text(pdf), event.correlation_id)
        payload = {k: v for k, v in p.items() if k != "reason"}
        payload["purchase_order"] = po.model_dump(mode="json")
        producer.send(
            "orders.extracted", event.child("orders.extracted", "extractor", payload)
        )

    return handle


def create_app(
    settings: Settings | None = None,
    start_consumer: bool = True,
    extract: Extract | None = None,
) -> FastAPI:
    settings = settings or Settings()
    extract = extract or make_extractor(settings)
    state: dict[str, threading.Thread | None] = {"consumer": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_consumer:
            logging.basicConfig(
                level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
            )
            s3 = boto3.client(
                "s3",
                endpoint_url=settings.s3_endpoint_url,
                aws_access_key_id=settings.s3_access_key,
                aws_secret_access_key=settings.s3_secret_key,
            )
            producer = EventProducer(settings.kafka_bootstrap, "extractor")
            consumer = EventConsumer(
                bootstrap=settings.kafka_bootstrap,
                group_id="extractor",
                topics=["docs.classified"],
                handler=make_handler(s3, extract, producer),
                producer=producer,
                store=PostgresStore(settings.events_database_url),
            )
            thread = threading.Thread(target=consumer.run, name="consumer", daemon=True)
            thread.start()
            state["consumer"] = thread
        yield

    app = FastAPI(title="Extractor", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        thread = state["consumer"]
        if start_consumer and (thread is None or not thread.is_alive()):
            raise HTTPException(503, "consumer not running")
        return {"status": "ok"}

    @app.post("/extract", response_model=PurchaseOrder)
    def extract_upload(file: Annotated[UploadFile, File()]) -> PurchaseOrder:
        data = file.file.read()
        if not data.startswith(b"%PDF-"):
            raise HTTPException(415, "file is not a PDF")
        return extract(pdf_text(data), f"manual-{file.filename}")

    return app
