#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_MLFLOW="${ROOT_DIR}/.venv/bin/mlflow"

if [[ ! -x "${VENV_MLFLOW}" ]]; then
  echo "Missing ${VENV_MLFLOW}. Run 'uv sync' first." >&2
  exit 1
fi

BACKEND_STORE_URI="${MLFLOW_SERVER_BACKEND_STORE_URI:-postgresql+psycopg://mlflow:mlflow@127.0.0.1:54329/mlflow}"
ARTIFACT_ROOT="${MLFLOW_ARTIFACT_ROOT:-${ROOT_DIR}/.mlflow/artifacts}"
MLFLOW_HOST="${MLFLOW_SERVER_HOST:-127.0.0.1}"
MLFLOW_PORT="${MLFLOW_SERVER_PORT:-5000}"

if [[ "${ARTIFACT_ROOT}" == file://* ]]; then
  ARTIFACT_PATH="${ARTIFACT_ROOT#file://}"
else
  ARTIFACT_PATH="${ARTIFACT_ROOT}"
fi

if [[ "${ARTIFACT_PATH}" != /* ]]; then
  ARTIFACT_PATH="${ROOT_DIR}/${ARTIFACT_PATH#./}"
fi

mkdir -p "${ARTIFACT_PATH}"

echo "Starting MLflow on http://${MLFLOW_HOST}:${MLFLOW_PORT}"
exec "${VENV_MLFLOW}" server \
  --backend-store-uri "${BACKEND_STORE_URI}" \
  --default-artifact-root "file://${ARTIFACT_PATH}" \
  --host "${MLFLOW_HOST}" \
  --port "${MLFLOW_PORT}"
