"""
Plexis Centralized Final-Response Boundary.

MANDATORY SECURITY CONTRACT:
- Accepts ONLY intended user-facing answers.
- Strips any residual chain-of-thought, thinking blocks, or prompt leaks.
- Detects recognizable internal planning dumps.
- Fail-closed safeguard: if a response contains no valid final answer or is an
  internal planning dump, blocks it, logs a safe error category (without logging sensitive text),
  and returns a safe, helpful user-facing response.
- Guarantees zero internal prompt, planning, or reasoning leakage to the user.
"""
from __future__ import annotations
import logging
from typing import Optional, Any

from utils.text_sanitizer import strip_thinking_and_internal_reasoning, is_internal_planning_dump

logger = logging.getLogger(__name__)


def enforce_response_boundary(
    answer: str,
    context: Optional[Any] = None,
    fallback_type: str = "default",
) -> str:
    """
    Centralized boundary verifying that only intended user-facing prose is returned.
    Fails closed if the answer is empty or contains an internal reasoning/prompt dump.
    """
    if not answer or not isinstance(answer, str):
        return _generate_safe_fallback(context, fallback_type)

    # 1. Clean through text sanitizer
    cleaned = strip_thinking_and_internal_reasoning(answer).strip()

    # 2. Check for internal planning dump or empty result
    if not cleaned or is_internal_planning_dump(cleaned):
        logger.warning(
            "[Security] Internal reasoning dump blocked by response boundary "
            "[Category: PROMPT_LEAKAGE_REJECTED]"
        )
        return _generate_safe_fallback(context, fallback_type)

    return cleaned


def _generate_safe_fallback(context: Optional[Any], fallback_type: str) -> str:
    """
    Produce a safe, helpful user-facing message matching the execution context.
    Never exposes internal system prompt state.
    """
    dataset_id = getattr(context, "dataset_id", None)
    dataset_filename = getattr(context, "dataset_filename", None)

    if dataset_id or dataset_filename:
        fname = dataset_filename or "your dataset"
        return (
            f"I am ready to help you analyze **{fname}**. "
            "Which columns, trends, or summary statistics would you like to explore?"
        )

    # No dataset loaded in this chat
    return (
        "No dataset is currently loaded in this chat. To analyze your data, "
        "please upload a dataset (such as a CSV file) using the upload area "
        "or select an existing dataset from the sidebar to get started."
    )
