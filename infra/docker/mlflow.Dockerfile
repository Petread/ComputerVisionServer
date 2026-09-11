FROM ghcr.io/mlflow/mlflow:v2.16.2

RUN python -m pip install --no-cache-dir psycopg2-binary==2.9.9
