import base64
import binascii
import hashlib
import hmac
import json
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

import boto3
from flask import Flask, jsonify, request
from o2c_common.events import Envelope
from o2c_common.ids import idempotency_key
from o2c_common.kafka import EventProducer
from pydantic import BaseModel, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

MAX_PDF_BYTES = 10 * 1024 * 1024  # 10 MB


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    intake_hmac_secret: str
    kafka_bootstrap: str = "localhost:9092"
    s3_endpoint_url: str = "http://localhost:8333"
    s3_access_key: str
    s3_secret_key: str
    s3_bucket: str = "o2c-documents"


class IntakeRequest(BaseModel):
    source: Literal["email", "upload"]
    filename: str
    sender: str | None = None
    content_base64: str


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def create_app(settings: Settings | None = None, s3=None, producer=None) -> Flask:
    settings = settings or Settings()
    s3 = s3 or boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
    )
    producer = producer or EventProducer(settings.kafka_bootstrap, "intake")

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = (
        15 * 1024 * 1024
    )  # base64 makes files ~33% larger

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/intake")
    def intake():
        body = request.get_data()
        signature = request.headers.get("X-O2C-Signature", "")
        if not hmac.compare_digest(sign(settings.intake_hmac_secret, body), signature):
            return jsonify(error="invalid signature"), 401

        try:
            req = IntakeRequest.model_validate_json(body)
        except ValidationError as exc:
            return jsonify(error="invalid request", details=json.loads(exc.json())), 400
        if not req.filename.lower().endswith(".pdf"):
            return jsonify(error="only PDF files are accepted"), 415
        try:
            content = base64.b64decode(req.content_base64, validate=True)
        except binascii.Error:
            return jsonify(error="content_base64 is not valid base64"), 400
        if len(content) > MAX_PDF_BYTES:
            return jsonify(error="file larger than 10 MB"), 413
        if not content.startswith(b"%PDF-"):
            return jsonify(error="file is not a PDF"), 415

        key = idempotency_key(req.source, content)
        s3_key = f"incoming/{key}.pdf"
        s3.put_object(
            Bucket=settings.s3_bucket,
            Key=s3_key,
            Body=content,
            ContentType="application/pdf",
        )

        event = Envelope(
            event_id=uuid5(NAMESPACE_URL, f"intake/{key}"),
            event_type="docs.received",
            producer="intake",
            idempotency_key=key,
            correlation_id=key,
            payload={
                "s3_bucket": settings.s3_bucket,
                "s3_key": s3_key,
                "filename": req.filename,
                "source": req.source,
                "sender": req.sender,
                "size_bytes": len(content),
            },
        )
        producer.send("docs.received", event)
        producer.flush()
        return jsonify(idempotency_key=key, event_id=str(event.event_id)), 202

    return app
