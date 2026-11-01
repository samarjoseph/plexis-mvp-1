"""
Conversation and Workflow Memorizer for Plexis AI.

Provides semantic conversation awareness, workflow tracking, and tone context
so that follow-up responses to assistant questions (clarifications, confirmations, scopes)
are understood in context rather than treated as isolated, out-of-context queries.

Guiding Principles:
- No brittle regex or rigid single-word special cases.
- Captures tone, workflow state, active entity/column, and recent conversation turns.
- Generates a concise, high-density workflow summary for the LLM interpreter.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Semantic phrases that indicate an intention to leave a column or field empty/unpopulated/null
_EMPTY_NULL_INDICATORS = frozenset({
    "blank", "empty", "null", "none", "nan", "nil", "unpopulated",
    "leave blank", "leave it blank", "leave empty", "leave it empty",
    "keep blank", "keep empty", "keep it empty",
    "don't populate", "dont populate", "do not populate",
    "no value", "no values", "nothing", "just blank", "just empty",
    "without value", "without values", "initialize with null",
    "init with null", "set to null", "make it blank", "make it empty"
})


class ConversationMemorizer:
    """
    Tracks and synthesizes conversational flow and active workflow state.
    """

    def analyze_flow(
        self,
        conversation_history: List[Dict[str, Any]],
        current_message: str,
        pending_workflow: Optional[Dict[str, Any]] = None,
        workspace_action: Optional[Dict[str, Any]] = None,
        dataset_schema: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Analyze multi-turn conversation context and determine if current_message
        is a follow-up to an active workflow or prior assistant turn.

        Args:
            conversation_history: List of recent messages [{'role': ..., 'content': ...}]
            current_message: The latest message from the user.
            pending_workflow: Optional active pending transformation / clarification context.
            workspace_action: Optional structured workspace action payload from frontend.
            dataset_schema: List of column metadata dictionaries.

        Returns:
            Structured memory context dictionary.
        """
        clean_msg = (current_message or "").strip().lower()

        # 1. Identify last assistant message and question if present
        last_assistant_msg = ""
        last_user_msg = ""
        for turn in reversed(conversation_history):
            role = turn.get("role", "")
            content = str(turn.get("content", "")).strip()
            if role == "assistant" and not last_assistant_msg:
                last_assistant_msg = content
            elif role == "user" and not last_user_msg:
                last_user_msg = content
            if last_assistant_msg and last_user_msg:
                break

        # 2. Check if there is an active pending workflow or clarification
        has_pending = bool(pending_workflow or workspace_action)
        clarification_type = ""
        target_column = None
        workflow_action = None
        user_prompt = ""

        if pending_workflow:
            clarification_type = pending_workflow.get("clarification_type", "")
            target_column = pending_workflow.get("target_column")
            workflow_action = pending_workflow.get("action")
            user_prompt = pending_workflow.get("user_prompt", "")
        elif workspace_action:
            clarification_type = workspace_action.get("clarification_type", "")
            target_column = workspace_action.get("target_column") or workspace_action.get("column")
            workflow_action = workspace_action.get("action")

        # Fallback: check if the assistant's last message was a clarification question
        if not target_column and last_assistant_msg:
            # Check for column clarification patterns in assistant message
            # e.g. "How should the new column **'gender'** be calculated or populated?"
            import re
            col_match = re.search(r"(?:column|field)\s+[\*\'\"]*([A-Za-z0-9_.\-]+)[\*\'\"]*", last_assistant_msg, re.IGNORECASE)
            if col_match:
                target_column = col_match.group(1)
            if "how should the new column" in last_assistant_msg.lower() or "calculated or populated" in last_assistant_msg.lower():
                clarification_type = clarification_type or "EXPRESSION_REQUIRED"
                workflow_action = workflow_action or "CREATE_COLUMN"
            elif "which columns should i rename" in last_assistant_msg.lower():
                clarification_type = clarification_type or "COLUMN_SCOPE"
                workflow_action = workflow_action or "RENAME_COLUMN"

        # 3. Determine if current message is a follow-up response
        is_followup = False
        workflow_stage = "STANDARD_QUERY"
        inferred_intent = None
        inferred_operation = None
        context_summary = ""

        # Check if the user is answering a clarification
        if clarification_type in ("EXPRESSION_REQUIRED", "COLUMN_SCOPE", "VALUE_AMBIGUITY", "EXTREME_VALUE_AMBIGUITY") or has_pending:
            is_followup = True
            workflow_stage = "CLARIFICATION_ANSWER"

            # Check if user intends to leave the column blank / unpopulated / null
            is_empty_or_null = self.is_empty_null_intent(clean_msg)
            
            if clarification_type == "EXPRESSION_REQUIRED" or workflow_action in ("CREATE_COLUMN", "ADD_COLUMN"):
                if is_empty_or_null:
                    inferred_intent = "TRANSFORM"
                    inferred_operation = {
                        "type": "ADD_COLUMN",
                        "column": target_column or "new_column",
                        "value": None,
                    }
                    context_summary = (
                        f"User is answering assistant's question on how to populate new column '{target_column}'. "
                        f"User instructed to leave it blank/unpopulated (null value)."
                    )
                else:
                    context_summary = (
                        f"User is providing a calculation or initial value for new column '{target_column}' "
                        f"in response to assistant clarification."
                    )
            elif clarification_type == "COLUMN_SCOPE":
                context_summary = (
                    f"User is specifying column scope for pending operation '{workflow_action}'."
                )
            else:
                context_summary = (
                    f"User is resolving pending clarification for '{workflow_action}'."
                )

        elif last_assistant_msg and len(clean_msg.split()) <= 6:
            # Short response following an assistant turn
            is_followup = True
            workflow_stage = "CONVERSATIONAL_FOLLOWUP"
            context_summary = f"User gave a brief follow-up ('{current_message}') following assistant message: '{last_assistant_msg[:100]}...'"

        else:
            context_summary = "Independent dataset instruction or question."

        return {
            "is_followup": is_followup,
            "workflow_stage": workflow_stage,
            "clarification_type": clarification_type,
            "target_column": target_column,
            "workflow_action": workflow_action,
            "context_summary": context_summary,
            "inferred_intent": inferred_intent,
            "inferred_operation": inferred_operation,
            "last_assistant_question": last_assistant_msg[:250] if last_assistant_msg else None,
            "last_user_query": last_user_msg[:250] if last_user_msg else None,
        }

    @staticmethod
    def is_empty_null_intent(text: str) -> bool:
        """
        Check if user's text expresses an intent to leave a value empty, blank, or null.
        Handles natural variants without brittle regex.
        """
        cleaned = text.strip().lower()
        if cleaned in _EMPTY_NULL_INDICATORS:
            return True

        # Check multi-word phrases and common natural expressions
        # e.g., "don't populate , leave blank", "just leave it blank", "keep empty"
        tokens = set(cleaned.replace(",", " ").replace(".", " ").split())
        if "blank" in tokens or "empty" in tokens or "null" in tokens or "none" in tokens:
            # If the user says "don't populate", "leave blank", "keep empty", etc.
            if any(p in cleaned for p in ("leave", "keep", "populate", "dont", "don't", "do not", "just", "make", "set", "init")):
                return True
            if len(tokens) <= 3:
                return True

        if "don't populate" in cleaned or "dont populate" in cleaned or "do not populate" in cleaned:
            return True

        return False


conversation_memorizer = ConversationMemorizer()
