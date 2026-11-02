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

    @staticmethod
    def extract_target_column(text: str, valid_columns: Optional[List[str]] = None) -> Optional[str]:
        """Extract referenced column name from text."""
        import re
        if valid_columns:
            for c in valid_columns:
                if re.search(rf'\b{re.escape(c)}\b', text, re.IGNORECASE):
                    return c

        # 1. "gender column" or "gender col"
        m = re.search(r'\b([a-zA-Z0-9_.\-]+)\s+columns?\b', text, re.IGNORECASE)
        if m and m.group(1).lower() not in ('the', 'a', 'new', 'this', 'that', 'every', 'each', 'drop', 'add', 'fill', 'in', 'on', 'for'):
            return m.group(1).strip()

        # 2. "column 'gender'" or "column gender"
        m = re.search(r'''\bcolumns?\s+['"`*]*([a-zA-Z0-9_.\-]+)['"`*]*\b''', text, re.IGNORECASE)
        if m and m.group(1).lower() not in ('the', 'a', 'new', 'this', 'that', 'every', 'each', 'with', 'to', 'for', 'in', 'is', 'has'):
            return m.group(1).strip()

        return None

    def detect_pattern_transformation(self, text: str, valid_columns: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        """
        Detect if user query requests populating or filling a column with consecutive,
        alternating, or repeating values.
        """
        import re
        clean = text.strip()
        clean_lower = clean.lower()

        is_pattern = bool(re.search(
            r'\b(?:consecutively|alternating|alternate|alternately|cyclical|in\s+turn|repeating|1st\s+.*2nd|first\s+.*second|so\s+on\s+till|so\s+on\s+to)\b',
            clean_lower
        ))
        if not is_pattern:
            return None

        col = self.extract_target_column(clean, valid_columns)

        # Extract pattern values from quotes or word tokens
        quotes = re.findall(r'''["']([^"']+)["']''', clean)
        if len(quotes) >= 2:
            pattern = quotes[:10]
        else:
            val_m = re.search(r'\b(?:values?|pattern)\s+([a-zA-Z0-9_]+)\s+and\s+([a-zA-Z0-9_]+)\b', clean_lower)
            if val_m:
                pattern = [val_m.group(1), val_m.group(2)]
            else:
                pattern = None

        if not col or not pattern:
            return None

        return {
            "type": "FILL_PATTERN",
            "column": col,
            "pattern": pattern,
            "mode": "alternating"
        }

    def detect_retrigger_request(
        self, text: str, conversation_history: List[Dict[str, Any]], valid_columns: Optional[List[str]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Detect if user requests to re-trigger, re-create, or view a previously discussed transformation plan.
        """
        import re
        clean_lower = text.strip().lower()
        is_retrigger = bool(re.search(
            r'\b(?:re-?trigger|where\s+is\s+the\s+.*plan|show\s+(?:the\s+)?plan|re-?create\s+(?:the\s+)?plan|generate\s+(?:the\s+)?plan|make\s+(?:the\s+)?plan|re-?run\s+(?:the\s+)?plan)\b',
            clean_lower
        ))
        if not is_retrigger:
            return None

        target_col = None
        target_pattern = None
        for turn in reversed(conversation_history):
            content = turn.get("content", "")
            if not target_col:
                target_col = self.extract_target_column(content, valid_columns)
            if not target_pattern:
                quotes = re.findall(r'''["']([^"']+)["']''', content)
                if len(quotes) >= 2:
                    target_pattern = quotes[:10]
            if target_col and target_pattern:
                break

        if not target_col or not target_pattern:
            return None

        return {
            "type": "FILL_PATTERN",
            "column": target_col,
            "pattern": target_pattern,
            "mode": "alternating"
        }

    @staticmethod
    def detect_chat_command(text: str, current_chat_title: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Detect user natural-language commands to rename, delete, pin, or unpin chats.
        Supports case-insensitive matching.
        """
        import re
        clean = text.strip()

        # 1. RENAME CHAT
        rename_m = re.search(
            r'^(?:please\s+)?(?:rename|change\s+(?:the\s+)?(?:chat\s+)?title(?:\s+of)?)\s+(?:(?:the\s+)?(?:chat\s+)?)?(?:["\']([^"\']+)["\']|([a-zA-Z0-9_\s\-]+?))\s+(?:chat\s+)?to\s+["\']?([^"\']+)["\']?$',
            clean,
            re.IGNORECASE
        )
        if rename_m:
            raw_target = (rename_m.group(1) or rename_m.group(2) or '').strip()
            new_title = rename_m.group(3).strip().strip('"\'')
            is_current = raw_target.lower() in ('this', 'this chat', 'current', 'current chat', 'chat', '')
            return {
                'action': 'rename_chat',
                'target_title': None if is_current else raw_target,
                'new_title': new_title
            }

        # 2. DELETE CHAT
        delete_m = re.search(
            r'^(?:please\s+)?(?:delete|remove)\s+(?:the\s+)?(?:chat\s+)?(?:["\']([^"\']+)["\']|([a-zA-Z0-9_\s\-]+?))(?:\s+chat)?$',
            clean,
            re.IGNORECASE
        )
        if delete_m:
            raw_target = (delete_m.group(1) or delete_m.group(2) or '').strip()
            # Avoid matching non-chat deletes like "delete column age" or "delete rows"
            if not re.search(r'\b(?:column|columns|col|row|rows|duplicates|nulls|missing)\b', raw_target, re.IGNORECASE):
                is_current = raw_target.lower() in ('this', 'this chat', 'current', 'current chat')
                return {
                    'action': 'delete_chat',
                    'target_title': None if is_current else raw_target
                }

        # 3. UNPIN CHAT
        unpin_m = re.search(
            r'^(?:please\s+)?unpin\s+(?:the\s+)?(?:chat\s+)?(?:["\']([^"\']+)["\']|([a-zA-Z0-9_\s\-]+?))(?:\s+chat)?$',
            clean,
            re.IGNORECASE
        )
        if unpin_m:
            raw_target = (unpin_m.group(1) or unpin_m.group(2) or '').strip()
            is_current = raw_target.lower() in ('this', 'this chat', 'current', 'current chat')
            return {
                'action': 'unpin_chat',
                'target_title': None if is_current else raw_target
            }

        # 4. PIN CHAT
        pin_m = re.search(
            r'^(?:please\s+)?pin\s+(?:the\s+)?(?:chat\s+)?(?:["\']([^"\']+)["\']|([a-zA-Z0-9_\s\-]+?))(?:\s+chat)?$',
            clean,
            re.IGNORECASE
        )
        if pin_m:
            raw_target = (pin_m.group(1) or pin_m.group(2) or '').strip()
            is_current = raw_target.lower() in ('this', 'this chat', 'current', 'current chat')
            return {
                'action': 'pin_chat',
                'target_title': None if is_current else raw_target
            }

        return None

    @staticmethod
    def detect_ui_command(text: str) -> Optional[List[Dict[str, Any]]]:
        """
        Detect user natural-language commands to control workspace views and panels.
        """
        import re
        clean_lower = text.strip().lower()

        # Spreadsheet commands
        if re.search(r'\b(?:open|show|view|display|launch)\s+(?:the\s+)?(?:live\s+)?(?:spreadsheet|sheet|table|grid|dataset)\b', clean_lower):
            return [{"action": "open_spreadsheet"}]
        if re.search(r'\b(?:close|hide|collapse|minimize)\s+(?:the\s+)?(?:spreadsheet|sheet|table|grid)\b', clean_lower):
            return [{"action": "close_spreadsheet"}]

        # Analytics panel commands
        if re.search(r'\b(?:open|show|view|display)\s+(?:the\s+)?(?:analytics|charts?|insights?)\s*(?:panel)?\b', clean_lower):
            return [{"action": "open_analytics"}]
        if re.search(r'\b(?:close|hide|collapse)\s+(?:the\s+)?(?:analytics|charts?|insights?)\s*(?:panel)?\b', clean_lower):
            return [{"action": "close_analytics"}]

        # Sidebar commands
        if re.search(r'\b(?:open|show)\s+(?:the\s+)?sidebar\b', clean_lower):
            return [{"action": "open_sidebar"}]
        if re.search(r'\b(?:close|hide|collapse)\s+(?:the\s+)?sidebar\b', clean_lower):
            return [{"action": "close_sidebar"}]

        # Column locator commands
        loc_m = re.search(r'\b(?:locate|find|highlight)\s+(?:column\s+)?([a-zA-Z0-9_.\-]+)\s+(?:in|on)\s+(?:the\s+)?(?:spreadsheet|sheet|table)\b', clean_lower)
        if loc_m:
            return [{"action": "open_spreadsheet"}, {"action": "locate_column", "column": loc_m.group(1).strip()}]

        return None


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
            if not target_column and workspace_action.get("chosen_operation"):
                target_column = workspace_action["chosen_operation"].get("column")
            workflow_action = workspace_action.get("action")

        # Fallback: check if the assistant's last message was a clarification question
        if not target_column and last_assistant_msg:
            # Check for column clarification patterns in assistant message
            # e.g. "How should the new column **'gender'** be calculated or populated?"
            import re
            col_match = re.search(r"(?:new\s+column|column|field)\s+[\*\'\"]*([A-Za-z0-9_.\-]+)[\*\'\"]*", last_assistant_msg, re.IGNORECASE)
            if col_match and col_match.group(1).lower() not in ('the', 'a', 'new', 'column', 'col', 'field'):
                target_column = col_match.group(1)
            if "how should the new column" in last_assistant_msg.lower() or "calculated or populated" in last_assistant_msg.lower():
                clarification_type = clarification_type or "EXPRESSION_REQUIRED"
                workflow_action = workflow_action or "CREATE_COLUMN"
            elif "which columns should i rename" in last_assistant_msg.lower():
                clarification_type = clarification_type or "COLUMN_SCOPE"
                workflow_action = workflow_action or "RENAME_COLUMN"

        if not target_column and last_user_msg:
            valid_cols = [c.get("name") or c.get("label") for c in dataset_schema] if dataset_schema else []
            target_column = self.extract_target_column(last_user_msg, valid_cols)

        # 3. Determine if current message is a follow-up response
        is_followup = False
        workflow_stage = "STANDARD_QUERY"
        inferred_intent = None
        inferred_operation = None
        context_summary = ""

        # Check for pattern-based transformation instruction (e.g. alternating male/female)
        valid_cols = [c.get("name") or c.get("label") for c in dataset_schema] if dataset_schema else []
        pat_op = self.detect_pattern_transformation(current_message, valid_cols)
        if pat_op:
            is_followup = True
            workflow_stage = "TRANSFORMATION_REFINEMENT"
            inferred_intent = "TRANSFORM"
            inferred_operation = pat_op
            context_summary = (
                f"User requested populating column '{pat_op.get('column')}' with pattern "
                f"{pat_op.get('pattern')} ({pat_op.get('mode')})."
            )
            return {
                "is_followup": is_followup,
                "workflow_stage": workflow_stage,
                "clarification_type": clarification_type,
                "target_column": pat_op.get("column"),
                "workflow_action": "FILL_PATTERN",
                "context_summary": context_summary,
                "inferred_intent": inferred_intent,
                "inferred_operation": inferred_operation,
                "last_assistant_question": last_assistant_msg[:250] if last_assistant_msg else None,
                "last_user_query": last_user_msg[:250] if last_user_msg else None,
            }

        # Check for re-trigger / show plan request
        retrigger_op = self.detect_retrigger_request(current_message, conversation_history, valid_cols)
        if retrigger_op:
            is_followup = True
            workflow_stage = "TRANSFORMATION_RETRIGGER"
            inferred_intent = "TRANSFORM"
            inferred_operation = retrigger_op
            context_summary = (
                f"User requested re-triggering the transformation plan for column '{retrigger_op.get('column')}'."
            )
            return {
                "is_followup": is_followup,
                "workflow_stage": workflow_stage,
                "clarification_type": clarification_type,
                "target_column": retrigger_op.get("column"),
                "workflow_action": retrigger_op.get("type"),
                "context_summary": context_summary,
                "inferred_intent": inferred_intent,
                "inferred_operation": inferred_operation,
                "last_assistant_question": last_assistant_msg[:250] if last_assistant_msg else None,
                "last_user_query": last_user_msg[:250] if last_user_msg else None,
            }

        # Check if the user is answering a clarification
        if clarification_type in ("EXPRESSION_REQUIRED", "COLUMN_SCOPE", "VALUE_AMBIGUITY", "EXTREME_VALUE_AMBIGUITY") or has_pending:
            is_followup = True
            workflow_stage = "CLARIFICATION_ANSWER"

            # Check if user intends to leave the column blank / unpopulated / null
            is_empty_or_null = self.is_empty_null_intent(clean_msg)
            is_zero = clean_msg in ('0', 'zero', 'initialize with 0', 'init with 0', 'i meant: initialize with 0', 'option 1', '1')
            
            if clarification_type == "EXPRESSION_REQUIRED" or workflow_action in ("CREATE_COLUMN", "ADD_COLUMN", "MODIFY_COLUMN"):
                col_to_use = target_column
                if not col_to_use:
                    for turn in reversed(conversation_history):
                        content = turn.get("content", "")
                        col_to_use = self.extract_target_column(content, valid_cols)
                        if col_to_use:
                            break
                if not col_to_use and valid_cols:
                    col_to_use = valid_cols[0]

                is_existing = bool(valid_cols and col_to_use in valid_cols)
                resolved_action = "MODIFY_COLUMN" if is_existing else "CREATE_COLUMN"

                if not col_to_use:
                    return {
                        "is_followup": is_followup,
                        "workflow_stage": workflow_stage,
                        "clarification_type": clarification_type,
                        "target_column": None,
                        "workflow_action": workflow_action,
                        "context_summary": "Follow-up without target column",
                        "inferred_intent": None,
                        "inferred_operation": None,
                    }

                if is_empty_or_null:
                    inferred_intent = "TRANSFORM"
                    inferred_operation = {
                        "type": "IR_REQUIRED",
                        "_action": resolved_action,
                        "column": col_to_use,
                        "expression": {
                            'type': 'LITERAL',
                            'value': None,
                            'typed_literal': {
                                '__typed_literal__': True,
                                'value': None,
                                'value_type': 'null',
                                'source_form': 'null',
                                'explicit': True,
                                'was_quoted': False,
                            }
                        }
                    }
                    context_summary = (
                        f"User is answering assistant's question on how to populate column '{col_to_use}'. "
                        f"User instructed to leave it blank/unpopulated (null value)."
                    )
                elif is_zero:
                    inferred_intent = "TRANSFORM"
                    inferred_operation = {
                        "type": "IR_REQUIRED",
                        "_action": resolved_action,
                        "column": col_to_use,
                        "expression": {
                            'type': 'LITERAL',
                            'value': 0,
                            'typed_literal': {
                                '__typed_literal__': True,
                                'value': 0,
                                'value_type': 'integer',
                                'source_form': '0',
                                'explicit': True,
                                'was_quoted': False,
                            }
                        }
                    }
                    context_summary = (
                        f"User is answering assistant's question on how to populate column '{col_to_use}'. "
                        f"User instructed to initialize with 0."
                    )
                else:
                    context_summary = (
                        f"User is providing a calculation or initial value for new column '{col_to_use}' "
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
