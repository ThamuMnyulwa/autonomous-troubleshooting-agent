"""
MLflow-backed prompt registry for the troubleshooting agent.

The bundled prompt definitions are registered into a local or remote MLflow prompt
registry and resolved at runtime through aliases. This keeps the existing helper API
while making prompt versions visible and controllable through MLflow.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

import mlflow
from dotenv import load_dotenv
from mlflow import MlflowClient
from mlflow.exceptions import MlflowException


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _to_single_brace_format(template: str) -> str:
    return template.replace("{{", "{").replace("}}", "}")


def _semver_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _semver_alias(version: str) -> str:
    return f"v{version.replace('.', '_')}"


@dataclass(frozen=True)
class PromptVersion:
    """
    Prompt definition and resolved prompt artifact.

    `version` is the semantic version tracked in code and mirrored into MLflow tags.
    `registry_version` is the MLflow integer revision.
    """

    name: str
    version: str
    template: str
    description: str = ""
    author: str = "system"
    registry_version: int | None = None
    uri: str | None = None

    @property
    def content_hash(self) -> str:
        return _content_hash(self.template)

    @property
    def fingerprint(self) -> str:
        suffix = f":r{self.registry_version}" if self.registry_version is not None else ""
        return f"{self.name}@{self.version}{suffix}:{self.content_hash}"

    def render(self, **kwargs: Any) -> str:
        return _to_single_brace_format(self.template).format(**kwargs)


class PromptRegistry:
    """Prompt adapter that seeds and resolves prompt versions through MLflow."""

    def __init__(self) -> None:
        self._definitions: dict[str, list[PromptVersion]] = {}
        self._client: MlflowClient | None = None
        self._initialized = False
        self._lock = Lock()

    def register(self, prompt: PromptVersion) -> None:
        versions = self._definitions.setdefault(prompt.name, [])
        if any(existing.version == prompt.version for existing in versions):
            raise ValueError(f"Duplicate prompt definition for {prompt.name}@{prompt.version}")
        versions.append(prompt)
        versions.sort(key=lambda item: _semver_key(item.version))

    def get(self, name: str, version: str | None = None) -> PromptVersion:
        self._initialize()

        if version is None:
            return self._load_alias(name, _active_alias())
        if version.isdigit():
            return self._load_revision(name, int(version))
        return self._load_alias(name, _semver_alias(version))

    def list_prompts(self) -> dict[str, list[str]]:
        return {
            name: [definition.version for definition in definitions]
            for name, definitions in self._definitions.items()
        }

    def latest_fingerprints(self) -> dict[str, str]:
        self._initialize()
        return {name: self.get(name).fingerprint for name in self._definitions}

    def _initialize(self) -> None:
        if self._initialized:
            return

        with self._lock:
            if self._initialized:
                return

            load_dotenv()
            tracking_uri = _tracking_uri()
            registry_uri = _registry_uri(tracking_uri)
            _ensure_local_sqlite_parent_exists(tracking_uri)
            _ensure_local_sqlite_parent_exists(registry_uri)

            mlflow.set_tracking_uri(tracking_uri)
            mlflow.set_registry_uri(registry_uri)
            self._client = MlflowClient()

            if _auto_seed():
                for definitions in self._definitions.values():
                    for definition in definitions:
                        self._ensure_definition(definition)
                    self._ensure_active_alias(definitions[-1])

            self._initialized = True

    def _ensure_definition(self, definition: PromptVersion) -> None:
        alias = _semver_alias(definition.version)
        try:
            existing = self._client.get_prompt_version_by_alias(definition.name, alias)
        except MlflowException:
            registered = mlflow.genai.register_prompt(
                name=definition.name,
                template=definition.template,
                commit_message=f"Seed {definition.name}@{definition.version}",
                tags={
                    "semantic_version": definition.version,
                    "content_hash": definition.content_hash,
                    "author": definition.author,
                    "description": definition.description,
                },
            )
            mlflow.genai.set_prompt_alias(definition.name, alias, int(registered.version))
            return

        existing_hash = (getattr(existing, "tags", None) or {}).get("content_hash")
        if existing_hash != definition.content_hash:
            raise RuntimeError(
                "MLflow prompt alias drift detected for "
                f"{definition.name}@{definition.version}: expected {definition.content_hash}, "
                f"found {existing_hash or 'missing'}"
            )

    def _ensure_active_alias(self, latest_definition: PromptVersion) -> None:
        if not _is_default_active_alias():
            return

        latest_versioned = self._load_alias(
            latest_definition.name,
            _semver_alias(latest_definition.version),
        )
        try:
            active = self._client.get_prompt_version_by_alias(
                latest_definition.name, _active_alias()
            )
            active_version = (getattr(active, "tags", None) or {}).get("semantic_version", "")
            if active_version and _semver_key(active_version) >= _semver_key(
                latest_definition.version
            ):
                return
        except MlflowException:
            pass

        mlflow.genai.set_prompt_alias(
            latest_definition.name,
            _active_alias(),
            int(latest_versioned.registry_version),
        )

    def _load_alias(self, name: str, alias: str) -> PromptVersion:
        prompt = self._client.get_prompt_version_by_alias(name, alias)
        return self._from_mlflow(prompt)

    def _load_revision(self, name: str, revision: int) -> PromptVersion:
        prompt = self._client.get_prompt_version(name, revision)
        return self._from_mlflow(prompt)

    @staticmethod
    def _from_mlflow(prompt) -> PromptVersion:
        tags = getattr(prompt, "tags", None) or {}
        semantic_version = tags.get("semantic_version") or f"mlflow-{prompt.version}"
        return PromptVersion(
            name=prompt.name,
            version=semantic_version,
            template=prompt.template,
            description=tags.get("description", ""),
            author=tags.get("author", "mlflow"),
            registry_version=int(prompt.version),
            uri=getattr(prompt, "uri", None),
        )


def _ensure_local_sqlite_parent_exists(uri: str) -> None:
    if not uri.startswith("sqlite:///"):
        return

    sqlite_target = uri.removeprefix("sqlite:///")
    path = Path(sqlite_target)
    if not path.is_absolute():
        path = Path.cwd() / sqlite_target
    path.parent.mkdir(parents=True, exist_ok=True)


def _tracking_uri() -> str:
    return os.getenv(
        "MLFLOW_TRACKING_URI",
        "postgresql+psycopg://mlflow:mlflow@127.0.0.1:54329/mlflow",
    )


def _registry_uri(tracking_uri: str) -> str:
    return os.getenv("MLFLOW_REGISTRY_URI", tracking_uri)


def _active_alias() -> str:
    return os.getenv("PROMPT_REGISTRY_ACTIVE_ALIAS", "active")


def _is_default_active_alias() -> bool:
    return _active_alias() == "active"


def _auto_seed() -> bool:
    return os.getenv("PROMPT_REGISTRY_AUTO_SEED", "true").lower() not in {"0", "false", "no"}
