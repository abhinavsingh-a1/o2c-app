import os

import boto3
from dotenv import load_dotenv

load_dotenv()
s3 = boto3.client(
    "s3",
    endpoint_url="http://localhost:8333",
    aws_access_key_id=os.environ["S3_ACCESS_KEY"],
    aws_secret_access_key=os.environ["S3_SECRET_KEY"],
)
# o2c-audit: hash-chained audit archive (services/audit-archiver, ADR-014)
for bucket in ["mlflow", "o2c-documents", "langfuse", "o2c-audit"]:
    try:
        s3.create_bucket(Bucket=bucket)
        print(f"created {bucket}")
    except s3.exceptions.BucketAlreadyOwnedByYou:
        print(f"exists {bucket}")
