# Local MLflow Prompt Registry

This repo uses a local PostgreSQL-backed MLflow store for prompt registry development.
You can also run a local MLflow server for UI inspection, but the application itself
should point directly at the PostgreSQL-backed store.

## Local Topology

- Local PostgreSQL-backed MLflow store on `127.0.0.1:54329`
- Optional MLflow UI/server on `http://127.0.0.1:5000`
- Local artifact store at `./.mlflow/artifacts`

## Start Local PostgreSQL

```bash
docker compose -f docker-compose.mlflow.yml up -d
```

This starts a local Postgres container with:

- database: `mlflow`
- user: `mlflow`
- password: `mlflow`

The data directory is mounted into `./.mlflow/postgres`, which is gitignored.

## Start Local MLflow Server

```bash
./scripts/run_local_mlflow.sh
```

The script will:

1. create `./.mlflow/artifacts` if needed
2. start the MLflow server on port `5000`

## Environment Variables

The application should point directly at the MLflow backend store:

```bash
MLFLOW_TRACKING_URI=postgresql+psycopg://mlflow:mlflow@127.0.0.1:54329/mlflow
MLFLOW_REGISTRY_URI=
PROMPT_REGISTRY_ACTIVE_ALIAS=active
PROMPT_REGISTRY_AUTO_SEED=true
MLFLOW_RUNTIME_TRACING_ENABLED=true
MLFLOW_TRACING_EXPERIMENT_NAME=pipeline-resolver-runtime
```

The optional local MLflow server uses:

```bash
MLFLOW_SERVER_BACKEND_STORE_URI=postgresql+psycopg://mlflow:mlflow@127.0.0.1:54329/mlflow
MLFLOW_ARTIFACT_ROOT=./.mlflow/artifacts
```

You can still browse the local MLflow UI at `http://127.0.0.1:5000`, but prompt-registry
runtime operations should use the direct PostgreSQL URI shown above.

## Runtime Traces

The worker and graph now emit MLflow runtime traces to the same PostgreSQL-backed
MLflow store. The UI on `http://127.0.0.1:5000` can be used to inspect those traces
after you run the agent locally. Traces are persisted under the MLflow experiment
named by `MLFLOW_TRACING_EXPERIMENT_NAME`.

## Stop Local Services

```bash
docker compose -f docker-compose.mlflow.yml down
```
