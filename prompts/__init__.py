"""
Versioned prompt management for pipeline-resolver.

Public API:
    - registry:  MLflow-backed PromptRegistry with bundled prompt definitions
    - build_investigation_prompt()  — backward-compatible builder
    - build_approval_context_message()  — backward-compatible builder
    - PromptRegistry, PromptVersion  — for direct registry access
"""

from __future__ import annotations

from prompts.helpers import parse_agent_metadata, strip_html
from prompts.registry import PromptRegistry, PromptVersion
from prompts.templates import build_default_registry

__all__ = [
    "PromptRegistry",
    "PromptVersion",
    "registry",
    "build_investigation_prompt",
    "build_approval_context_message",
    "build_tool_retry_prompt",
    "get_system_prompt",
]

# Singleton registry — import once, resolve prompts lazily on first use.
registry: PromptRegistry = build_default_registry()


def _incident_context_block(meta: dict[str, str]) -> str:
    raw_target = meta.get("incident_target", "")
    if not raw_target:
        return ""

    lines = [
        "## Exact Incident Target",
        "Treat the following identifiers as binding scope for the investigation.",
        "Do not substitute similarly named resources from other environments.",
        f"- Raw target from alert: `{raw_target}`",
    ]

    optional_lines = [
        ("Alert rule", meta.get("alert_rule", "")),
        ("Target resource ID", meta.get("target_resource_id", "")),
        ("Target resource name", meta.get("target_resource_name", "")),
        ("Target resource group", meta.get("target_resource_group", "")),
        ("Target resource type", meta.get("target_resource_type", "")),
        ("Target subscription", meta.get("target_subscription", "")),
    ]
    for label, value in optional_lines:
        if value:
            lines.append(f"- {label}: `{value}`")

    lines += [
        "",
        "If you cannot locate this exact target, say that explicitly.",
        "A near match or similarly named resource is not sufficient evidence.",
        "",
    ]
    return "\n".join(lines)


def build_investigation_prompt(
    work_item: dict,
    comments: list[dict],
    attempt: int,
    *,
    prompt: PromptVersion | None = None,
) -> str:
    """
    Build the first HumanMessage for the agent from ADO work item data.

    Delegates to the versioned investigation template in the registry.
    Content from ADO is wrapped in UNTRUSTED delimiters.
    """
    fields = work_item.get("fields", {})
    title = fields.get("System.Title", "Unknown failure")
    tags = fields.get("System.Tags", "")
    desc = fields.get("System.Description", "")
    meta = parse_agent_metadata(desc)

    prior_history = ""
    if comments and attempt > 0:
        history_lines = [
            "## Prior Investigation History (UNTRUSTED)",
            "These are previous agent conclusions and human feedback.",
            "---",
        ]
        for comment in comments[-6:]:
            author = comment.get("createdBy", {}).get("displayName", "unknown")
            text = comment.get("text", "")
            history_lines.append(f"**{author}:** {strip_html(text)}")
            history_lines.append("")
        history_lines += ["---", "## END PRIOR HISTORY", ""]
        prior_history = "\n".join(history_lines) + "\n"

    resolved_prompt = prompt or registry.get("investigation")
    return resolved_prompt.render(
        attempt=attempt + 1,
        work_item_id=work_item.get("id", "?"),
        title=title,
        tags=tags,
        environment=meta.get("environment", "unknown"),
        description=strip_html(desc),
        incident_context=_incident_context_block(meta),
        prior_history=prior_history,
    )


def get_system_prompt(
    *,
    max_iter: int,
    target_env: str,
    attempt: int,
    max_attempts: int,
    version: str | None = None,
) -> str:
    """
    Render the system prompt for the agent ReAct loop.

    Args:
        version: Optional semver pin. Omit for latest.
    """
    prompt = registry.get("system", version)
    return prompt.render(
        max_iter=max_iter,
        target_env=target_env,
        attempt=attempt,
        max_attempts=max_attempts,
    )


def build_approval_context_message(new_feedback: str) -> str:
    """
    Message appended to the thread when a human replies 'did not work'.
    Wraps the feedback in UNTRUSTED delimiters.
    """
    prompt = registry.get("approval_feedback")
    return prompt.render(feedback=new_feedback)


def build_tool_retry_prompt() -> str:
    """Corrective HumanMessage when Gemini emits a malformed tool call."""
    prompt = registry.get("tool_retry")
    return prompt.render()
