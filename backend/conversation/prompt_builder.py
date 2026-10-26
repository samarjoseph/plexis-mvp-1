"""
Plexis Conversation Engine — System Prompt Builder

This is the master assembler for the Conversation Engine's system prompt.
It pulls from the modular sub-modules (identity, reasoning, analyst_voice)
and composes the final system prompt used for LLM generation.

Responsibilities:
- Assemble the system prompt from modular components.
- Adapt the prompt based on context (is a dataset loaded? is this analytical?).
- Keep the prompt clean, focused, and non-redundant.

This module does NOT handle routing, planning, or execution.
"""

from __future__ import annotations
from typing import Optional, Dict, Any, List

from conversation.identity import PLEXIS_IDENTITY, WARMTH_PRINCIPLES
from conversation.reasoning import REASONING_FRAMEWORK, RESPONSE_BEHAVIOUR
from conversation.analyst_voice import DATA_ANALYST_VOICE
from conversation.response_planner import ConversationPlan
from prompts.formatter import get_formatting_rules


def build_system_prompt(
    intent: str,
    has_dataset: bool = False,
    has_analytical_facts: bool = False,
    plan: ConversationPlan = None,
) -> str:
    """
    Assemble the Conversation Engine system prompt.

    Arguments:
        intent: The classified intent of the current request.
        has_dataset: Whether a dataset is currently loaded.
        has_analytical_facts: Whether this response involves real analytical results.
        plan: Optional ConversationPlan with length/tone guidance.

    Returns:
        A complete, focused system prompt for the conversation LLM call.
    """

    # Core identity is always present
    sections = [
        PLEXIS_IDENTITY,
        WARMTH_PRINCIPLES,
        REASONING_FRAMEWORK,
        RESPONSE_BEHAVIOUR,
    ]

    # Inject analytical voice only when it's relevant
    if has_dataset or has_analytical_facts or intent == 'analysis':
        sections.append(DATA_ANALYST_VOICE)

    # Formatting guidance always included
    sections.append(get_formatting_rules())

    # Inject the Conversation Plan if provided
    if plan:
        sections.append(plan.to_guidance())

    # Closing instruction — strict zero thinking / process leakage
    sections.append(
        "Now respond. The context below tells you what the user said and what has happened "
        "in this conversation. Use your reasoning, your identity, and your judgment.\n"
        "CRITICAL STRICT DIRECTIVE:\n"
        "- Respond directly to the user as Plexis.\n"
        "- Output ONLY the final user-facing response.\n"
        "- NEVER output any chain-of-thought, thinking process, reasoning steps, or prompt analysis (e.g. do NOT write 'Here\'s a thinking process:', 'Analyze User Input:', or quote 'The system note says').\n"
        "- Never include <think>, <thought>, or similar reasoning tags.\n        - NEVER output Python scripts or tutorial code (e.g. import pandas as pd, df.to_csv(...)). Plexis directly executes transformations on the dataset in-platform.\n        - NEVER guess or invent column names that are not in the dataset schema."
    )

    return "\n\n".join(sections)
