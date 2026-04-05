# Test Layout

- `tests/unit`: fast isolated tests with mocked or in-memory dependencies.
- `tests/integration`: multi-component tests that exercise real local integrations.

Common commands:

```bash
./.venv/bin/pytest -q
./.venv/bin/pytest -q tests/unit
./.venv/bin/pytest -q tests/integration
./.venv/bin/pytest -q -m unit
./.venv/bin/pytest -q -m integration
```

Current split:

- The prompt registry coverage lives in `tests/integration` because it exercises the local MLflow-backed prompt registry.
- The rest of the current suite is unit coverage.
