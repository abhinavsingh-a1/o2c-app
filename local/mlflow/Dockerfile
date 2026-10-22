# MLflow tracking server for the local stack. Built on python:3.12-slim instead of the
# official mlflow image (Debian 11 with outdated system and Python packages); the
# server version matches the clients (mlflow-skinny 2.17.2, ADR-015).
FROM python:3.12-slim
# sqlalchemy<2.1: MLflow 2.17.2 imports a pool class that SQLAlchemy 2.1 removed
RUN pip install --no-cache-dir "mlflow==2.17.2" "sqlalchemy<2.1" psycopg2-binary boto3
# Not root: the server only talks to Postgres and S3 (no local files)
RUN useradd --create-home --uid 10001 mlflow
USER 10001
