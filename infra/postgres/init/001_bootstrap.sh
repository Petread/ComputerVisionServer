#!/usr/bin/env bash

app_password="$(cat /run/secrets/postgres_app_password)"

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=app_password="$app_password" <<'EOSQL'
CREATE ROLE cv_platform LOGIN PASSWORD :'app_password';
CREATE DATABASE cv_platform OWNER cv_platform;
CREATE DATABASE labelstudio;
CREATE DATABASE mlflow;
GRANT ALL PRIVILEGES ON DATABASE cv_platform TO cv_platform;
EOSQL
