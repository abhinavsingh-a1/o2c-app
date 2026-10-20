import base64
import json
import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values
from intake.app import sign

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
    "http://localhost:8200/intake",
    content=body,
    headers={"Content-Type": "application/json", "X-O2C-Signature": sign(secret, body)},
)
print(response.status_code, response.json())
