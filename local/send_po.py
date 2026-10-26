"""Send a PDF to intake: uv run python local/send_po.py <pdf> (make send PDF=...).

INTAKE_URL (default http://localhost:8200/intake) selects the intake, e.g. a
port-forward to the kind cluster's o2c-dev intake. On Windows, localhost may resolve to
::1 first, where WSL's localhost relay (wslrelay.exe) can hold the Compose ports and
reset the connection; then use INTAKE_URL=http://127.0.0.1:8200/intake.
"""

import base64
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values
from intake.app import sign

INTAKE_URL = os.environ.get("INTAKE_URL", "http://localhost:8200/intake")

secret = dotenv_values(".env")["INTAKE_HMAC_SECRET"]
pdf = Path(sys.argv[1])
body = json.dumps(
    {
        "source": "upload",
        "filename": pdf.name,
        "sender": "test@example.com",
        "content_base64": base64.b64encode(pdf.read_bytes()).decode(),
    }
).encode()

response = httpx.post(
    INTAKE_URL,
    content=body,
    headers={"Content-Type": "application/json", "X-O2C-Signature": sign(secret, body)},
)
print(response.status_code, response.json())
