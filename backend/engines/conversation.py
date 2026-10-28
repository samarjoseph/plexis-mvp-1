"""
Conversation Engine — Plexis Communication Layer

This is the Conversation Engine. It is responsible for HOW Plexis communicates
after the Router has already decided WHAT should happen.

The Router decides WHAT.
The Conversation Engine decides HOW.

This engine handles:
- General conversation (greetings, casual chat, knowledge questions)
- Analytical result presentation (after the Analysis Engine has computed facts)
- Help system responses
- Conversation title generation
- Grounded explanation of existing results (explain_evidence workspace action)

This engine does NOT:
- Route requests
- Plan analytical operations
- Execute data computations
- Handle file uploads or dataset management
"""

import json
import logging
from core.context import ExecutionContext
from engines.base import BaseEngine, EngineResult
from providers import provider_engine
from conversation import build_system_prompt, build_context_block, response_planner

logger = logging.getLogger(__name__)


class ConversationEngine(BaseEngine):
    """
    The Conversation Engine.

    Converts execution context and engine results into natural, intelligent,
    warm, and contextually-aware responses using the modular conversation
    architecture.
    """

    @property
    def engine_name(self) -> str:
        return 'conversation'

    def can_handle(self, context: ExecutionContext) -> bool:
        return context.intent in ('conversation', 'title_generation', 'help_system')

    def handle(self, context: ExecutionContext) -> EngineResult:
        if context.intent == 'title_generation':
            return self._generate_title(context)

        # Route explain_evidence to a dedicated grounded explanation path.
        # This MUST happen before generic chat so the LLM receives structured
        # result context rather than a bare text trigger.
        workspace_action = getattr(context, 'workspace_action', None) or {}
        if isinstance(workspace_action, dict) and workspace_action.get('type') == 'explain_evidence':
            return self._explain_existing_result(context, workspace_action)

        return self._chat(context)

    # ------------------------------------------------------------------
    # Internal: Grounded Explanation of an Existing Spreadsheet Result
    # ------------------------------------------------------------------

    def _explain_existing_result(self, context: ExecutionContext, workspace_action: dict) -> EngineResult:
        """
        Explain an analytical result that Plexis has ALREADY computed.

        Contract:
          - The user clicked 'Explain' on an existing result card.
          - We have the operation type, column, computed stats, and row evidence.
          - We must explain WHAT THE RESULT MEANS — not HOW to compute it.
          - No Python code. No Pandas tutorial. No "steps to find" instructions.
          - No unsupported demographic inferences (names ≠ gender, etc.)
          - Length: concise — 1-3 sentences for simple results, more for complex ones.

        Grounding precedence:
          1. operation_context.stats — actual computed column stats
          2. evidence.metadata       — evidence metadata (count, n, etc.)
          3. evidence.description    — evidence text description
          4. dataset profile         — column distribution from the dataset registry
        """
        dataset_filename = getattr(context, 'dataset_filename', None)
        dataset_profile = getattr(context, 'dataset_profile', None) or {}
        column_profiles = getattr(context, 'column_profiles', None) or {}

        # --- 1. Extract structured result context from workspace_action ---
        evidence = workspace_action.get('evidence') or {}
        op_ctx = workspace_action.get('operation_context') or {}

        evidence_desc   = evidence.get('description') or workspace_action.get('description') or ''
        evidence_type   = evidence.get('type', '')
        evidence_meta   = evidence.get('metadata') or {}
        preview_rows    = evidence.get('preview_rows') or []

        op_type         = op_ctx.get('operation_type') or ''
        column          = op_ctx.get('column') or ''
        result_value    = op_ctx.get('result_value')
        row_count       = op_ctx.get('row_count')
        stats           = op_ctx.get('stats') or {}
        result_summary  = op_ctx.get('result_summary') or ''

        # --- 2. Pull dataset column profile for additional context ---
        col_profile = {}
        if column and column_profiles:
            col_profile = column_profiles.get(column) or {}

        # --- 3. Build structured result context block for LLM ---
        context_lines = []

        if op_type:
            context_lines.append(f"OPERATION: {op_type}")
        if column:
            context_lines.append(f"COLUMN: {column}")
        if result_value is not None:
            context_lines.append(f"RESULT VALUE: {result_value}")
        if row_count is not None:
            context_lines.append(f"AFFECTED ROWS: {row_count}")

        # Include computed column statistics if available
        if stats:
            stat_lines = []
            for k, v in stats.items():
                if v is not None:
                    stat_lines.append(f"  {k}: {v}")
            if stat_lines:
                context_lines.append("COLUMN STATISTICS (computed):")
                context_lines.extend(stat_lines)
        elif col_profile:
            # Fall back to dataset profile if no runtime stats
            stat_lines = []
            for k in ('min', 'max', 'mean', 'median', 'std', 'count', 'null_count'):
                v = col_profile.get(k)
                if v is not None:
                    stat_lines.append(f"  {k}: {v}")
            if stat_lines:
                context_lines.append("COLUMN STATISTICS (from dataset profile):")
                context_lines.extend(stat_lines)

        # Evidence description (what Plexis deterministically found)
        if evidence_desc:
            context_lines.append(f"\nEVIDENCE DESCRIPTION: {evidence_desc}")

        # Preview rows (up to 3)
        if preview_rows:
            context_lines.append("\nSAMPLE AFFECTED ROWS (first 3):")
            for i, row in enumerate(preview_rows[:3]):
                row_str = ', '.join(f"{k}={v}" for k, v in list(row.items())[:6])
                context_lines.append(f"  Row {i+1}: {row_str}")

        if result_summary:
            context_lines.append(f"\nRESULT SUMMARY: {result_summary}")

        if dataset_filename:
            context_lines.append(f"\nDATASET: {dataset_filename}")

        result_context = '\n'.join(context_lines)

        # --- 4. Grounded explanation system prompt ---
        system_prompt = (
            "You are Plexis, an analytical assistant.\n\n"
            "Your task is to explain an analytical result that you have ALREADY computed.\n\n"
            "RULES:\n"
            "- Explain WHAT THE RESULT MEANS and WHY IT MATTERS based on the supplied facts.\n"
            "- Do NOT provide Python, Pandas, SQL, or any code.\n"
            "- Do NOT explain how to calculate or reproduce the result.\n"
            "- Do NOT say 'steps to find', 'here is how to', or similar tutorial language.\n"
            "- Do NOT invent facts not present in the supplied context.\n"
            "- Do NOT make demographic inferences from names, emails, or locations "
            "(names do not establish gender, age, or background).\n"
            "- Keep the explanation concise: 1-3 sentences for simple results; "
            "up to a short paragraph for complex multi-row results.\n"
            "- Reference the actual numbers from the context. Do not guess or paraphrase statistics.\n"
            "- If the result is a minimum or maximum value, contextualise it relative to "
            "the dataset range and mean if those stats are provided.\n"
            "- If the result is a count of rows, explain what those rows represent.\n"
            "- Speak as Plexis — warm, clear, analytical. Not as a programming tutor.\n\n"
            "Now explain the result below using only the supplied facts."
        )

        # --- 5. User-facing context block ---
        user_block = (
            f"The user wants an explanation of this analytical result:\n\n"
            f"{result_context}\n\n"
            f"Explain what this result means. "
            f"Use ONLY the facts above. Do not provide code or calculation steps."
        )

        logger.info(
            f"[EXPLAIN_RESULT] type={op_type} column={column} "
            f"value={result_value} stats_keys={list(stats.keys()) if stats else []}"
        )

        from providers.domain.contracts import AIRequest
        response = provider_engine.generate(
            AIRequest(
                task='chat',
                messages=[{"role": "user", "content": user_block}],
                system_prompt=system_prompt,
                temperature=0.45,   # Lower temp for grounded factual explanation
            )
        )

        if response.success:
            from utils.response_boundary import enforce_response_boundary
            clean_answer = enforce_response_boundary(response.text, context)
            return EngineResult(
                answer=clean_answer,
                source='conversation',
                provider=response.provider,
                success=True,
                should_compose=False,
            )
        else:
            logger.warning(f"Explain result generation failed: {response.error}")
            return EngineResult(
                answer=f"Here's what I found: {evidence_desc}" if evidence_desc else
                       "I ran into an issue generating the explanation. Try again?",
                source='conversation',
                provider='system',
                success=False,
                error=response.error,
                should_compose=False,
            )

    # ------------------------------------------------------------------
    # Internal: General Conversation
    # ------------------------------------------------------------------

    def _chat(self, context: ExecutionContext) -> EngineResult:
        """
        Handle general conversation.

        This delegates ONLY the communication layer to the Conversation Engine.
        The engine builds the system prompt using modular identity components,
        assembles context from the execution state, and generates the response.
        """
        intent = getattr(context, 'intent', 'conversation')
        message = getattr(context, 'normalized_query', getattr(context, 'message', ''))
        conversation_history = getattr(context, 'conversation_history', [])
        dataset_filename = getattr(context, 'dataset_filename', None)

        # 0. DIRECT ANSWER: If the router produced a grounded direct answer (e.g. column not found,
        # schema inquiry, or deterministic calculation), return it immediately without calling LLM
        route_payload = getattr(context, 'route_payload', None) or {}
        direct_answer = route_payload.get('direct_answer')
        if direct_answer and isinstance(direct_answer, str) and direct_answer.strip():
            from utils.response_boundary import enforce_response_boundary
            return EngineResult(
                answer=enforce_response_boundary(direct_answer, context),
                source='conversation',
                provider='deterministic',
                success=True,
                should_compose=False,
            )

        # 1. PLAN: Determine conversational shape BEFORE generation
        plan = response_planner.plan(
            message=message,
            intent=intent,
            conversation_history=conversation_history,
            has_dataset=bool(dataset_filename)
        )

        logger.info(f"Conversation Plan: length={plan.response_length}, tone={plan.tone}, emoji={plan.emoji_level}")

        # 2. BUILD: Adaptive system prompt based on context and plan
        system_prompt = build_system_prompt(
            intent=intent,
            has_dataset=bool(dataset_filename),
            has_analytical_facts=False,
            plan=plan,
        )

        # Extract deterministic dataset schema context
        schema_profile = getattr(context, 'schema_profile', None)
        dataset_id = getattr(context, 'dataset_id', None)
        schema_context = None

        if not schema_profile and dataset_id:
            try:
                from datasets.registry import dataset_registry
                entry = dataset_registry.get(str(dataset_id))
                if entry:
                    schema_profile = getattr(entry, 'schema_profile', None)
            except Exception as e:
                logger.debug(f"[ConversationEngine] Error fetching schema_profile: {e}")

        if schema_profile:
            columns = []
            column_types = {}
            if isinstance(schema_profile, list):
                for col in schema_profile:
                    if isinstance(col, dict) and 'name' in col:
                        c_name = str(col['name'])
                        columns.append(c_name)
                        column_types[c_name] = str(col.get('dtype', 'unknown'))
            elif isinstance(schema_profile, dict):
                if 'columns' in schema_profile:
                    columns = [str(c) for c in schema_profile['columns']]
                else:
                    columns = [str(k) for k in schema_profile.keys()]
                column_types = {str(k): str(v) for k, v in schema_profile.get('types', {}).items()}

            if columns:
                schema_context = {
                    "dataset_id": str(dataset_id) if dataset_id else "unknown",
                    "columns": columns,
                    "column_types": column_types,
                    "schema": columns,
                    "column_count": len(columns),
                }

        # Build the structured context block for the LLM
        context_block = build_context_block(
            message=message,
            intent=intent,
            conversation_history=conversation_history,
            dataset_filename=dataset_filename,
            facts=None,
            schema_context=schema_context,
        )

        # Generate the response
        from providers.domain.contracts import AIRequest
        response = provider_engine.generate(
            AIRequest(
                task='chat',
                messages=[{"role": "user", "content": context_block}],
                system_prompt=system_prompt,
                temperature=0.75,
                max_tokens=600,
            )
        )

        if response.success and response.text and response.text.strip():
            from utils.response_boundary import enforce_response_boundary
            cleaned_text = enforce_response_boundary(response.text, context)
            return EngineResult(
                answer=cleaned_text,
                source='conversation',
                provider=response.provider,
                success=True,
                should_compose=False,  # Engine handles its own generation — no composer needed
            )
        else:
            logger.warning(f"Conversation engine generation failed: {response.error}")
            # ── Preserve grounded answers when LLM provider is unavailable ──
            # If the router already produced a direct_answer (e.g. column-not-found,
            # schema info, or analytical narrative), return it instead of the
            # generic error stub.
            route_payload = getattr(context, 'route_payload', None) or {}
            direct_answer = route_payload.get('direct_answer')
            if not direct_answer:
                direct_answer = getattr(context, 'direct_answer', None)
            if direct_answer and isinstance(direct_answer, str) and direct_answer.strip():
                return EngineResult(
                    answer=direct_answer,
                    source='conversation',
                    provider='system',
                    success=True,
                    should_compose=False,
                )
            return EngineResult(
                answer="Sorry, I ran into an issue generating a response. Try again?",
                source='conversation',
                provider='system',
                success=False,
                error=response.error,
                should_compose=False,
            )

    # ------------------------------------------------------------------
    # Internal: Title Generation
    # ------------------------------------------------------------------

    def _generate_title(self, context: ExecutionContext) -> EngineResult:
        """
        Generate a short, descriptive conversation title.

        Uses the smallest/fastest model via 'title' task affinity.
        Optimized for speed and cost — not personality.
        """
        from prompts.title_generator import build_title_prompt

        prompt = build_title_prompt(context.message)
        from providers.domain.contracts import AIRequest
        response = provider_engine.generate(
            AIRequest(
                task='title',
                messages=[{"role": "user", "content": prompt}],
                system_prompt="Generate a concise, descriptive title (3-6 words). Return ONLY the title text.",
                temperature=0.3,
                max_tokens=50,
            )
        )

        if not response.success:
            return EngineResult(
                answer='New Conversation',
                source='title',
                provider='system',
                should_compose=False,
            )

        title = response.text.strip().strip('"').strip("'").strip()
        if len(title) > 60:
            title = title[:57] + '...'

        return EngineResult(
            answer=title,
            source='title',
            provider=response.provider,
            should_compose=False,
        )


conversation_engine = ConversationEngine()
