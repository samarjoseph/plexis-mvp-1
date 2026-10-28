"""
Response Composer — Post-Processing Pipeline

The ResponseComposer handles results from engines that return raw analytical
facts and need an LLM to compose the final response.

WHEN IT RUNS:
Only when an EngineResult has should_compose=True. 
The Conversation Engine now handles its own generation (should_compose=False),
so the composer is primarily used by the Analysis Engine and other data engines.

WHAT IT DOES:
- Takes raw analytical facts from EngineResult.facts
- Builds a context-aware system prompt using the Conversation Engine architecture
- Generates a natural, intelligent, Plexis-flavored response to the facts

WHAT IT DOES NOT DO:
- Does not override the Conversation Engine's output
- Does not handle routing
- Does not handle planning
"""
import logging
from core.context import ExecutionContext
from engines.base import EngineResult
from providers import provider_engine
from conversation import build_system_prompt, build_context_block

logger = logging.getLogger(__name__)


class ResponseComposer:
    """
    Composes natural language responses for engines that return raw facts.
    """

    def compose(self, context: ExecutionContext, result: EngineResult) -> EngineResult:
        # Conversation Engine handles its own generation — skip if not needed
        if not getattr(result, 'should_compose', True):
            from verifier.answer_verifier import answer_verifier
            v_res = answer_verifier.verify(
                query=getattr(context, 'normalized_query', getattr(context, 'message', '')),
                answer=result.answer,
                facts=getattr(result, 'facts', None),
                evidence=getattr(result, 'evidence', None),
                context=context,
            )
            result.metadata['verification'] = v_res.to_dict()
            return result

        intent = getattr(context, 'intent', 'unknown')
        message = getattr(context, 'normalized_query', getattr(context, 'message', ''))
        conversation_history = getattr(context, 'conversation_history', [])
        dataset_filename = getattr(context, 'dataset_filename', None)
        facts = getattr(result, 'facts', None)
        evidence = getattr(result, 'evidence', None)
        workspace_summary = getattr(context, 'workspace_summary', '')
        narrative_hint = ''

        # Capability result may include a narrative_hint
        # (short prose produced by the capability — not the evidence object itself)
        if hasattr(result, '_capability_narrative_hint'):
            narrative_hint = result._capability_narrative_hint

        has_analytical_facts = bool(facts and set(facts.keys()) != {'type'} and set(facts.keys()) != {'error'})

        # Build system prompt — always include analyst voice since composer
        # is primarily used for analytical results
        system_prompt = build_system_prompt(
            intent=intent,
            has_dataset=bool(dataset_filename),
            has_analytical_facts=has_analytical_facts,
        )

        # Build context block with the full facts payload.
        # workspace_summary provides LLM with natural language workspace context.
        # evidence description (if present) is injected as an additional hint so the
        # LLM can reference what the system found deterministically.
        evidence_description = None
        if evidence and hasattr(evidence, 'description'):
            evidence_description = evidence.description
        elif evidence and hasattr(evidence, 'summary'):
            evidence_description = evidence.summary

        context_block = build_context_block(
            message=message,
            intent=intent,
            conversation_history=conversation_history,
            dataset_filename=dataset_filename,
            facts=facts,
            workspace_summary=workspace_summary or None,
            evidence_description=evidence_description,
        )

        # Generate the composed response.
        # Use near-zero temperature when analytical facts are present —
        # the LLM must read numbers from the FACTS block, not hallucinate them.
        # 0.7 is only appropriate for open-ended conversation, not data answers.
        from providers.domain.contracts import AIRequest
        compose_temperature = 0.1 if has_analytical_facts else 0.7
        response = provider_engine.generate(
            AIRequest(
                task='chat',
                messages=[{"role": "user", "content": context_block}],
                system_prompt=system_prompt,
                temperature=compose_temperature,
            )
        )

        if response.success:
            from utils.response_boundary import enforce_response_boundary
            clean_text = enforce_response_boundary(response.text, context)
            result.answer = clean_text
            result.provider = response.provider

            # Grounding verification for analytical results:
            from verifier.answer_verifier import answer_verifier, VerificationStatus
            v_res = answer_verifier.verify(
                query=message,
                answer=response.text or "",
                facts=facts,
                evidence=evidence,
                context=context,
            )

            if v_res.is_repaired and v_res.repaired_answer:
                logger.info("[ResponseComposer] Composed text repaired by AnswerVerifier.")
                result.answer = v_res.repaired_answer
            elif v_res.status == VerificationStatus.FAIL:
                computed = facts.get('computed_answer') if isinstance(facts, dict) else None
                if computed and computed.strip():
                    logger.warning(
                        "[ResponseComposer] Composed text failed verification (%s). Falling back to verified engine narrative.",
                        v_res.findings
                    )
                    result.answer = computed
                else:
                    fallback_parts = []
                    if isinstance(facts, dict):
                        ops = facts.get('operations', [])
                        for op_i in ops:
                            op_v = op_i.get('value')
                            op_n = op_i.get('operation')
                            if op_v is not None:
                                fallback_parts.append(f"{op_n or 'metric'}: {op_v}")
                    if fallback_parts:
                        logger.warning(
                            "[ResponseComposer] Composed text failed verification and no computed narrative available. Using structured fact fallback."
                        )
                        result.answer = "**Verified Analysis Result**:\n\n" + "\n".join(f"- {p}" for p in fallback_parts)
                    else:
                        logger.error("[ResponseComposer] Composed text failed verification and no authoritative facts available. Blocking corrupted draft.")
                        result.answer = "The analysis completed, but the drafted response contained unverified claims and could not be verified against dataset evidence."
                        result.success = False

                if hasattr(result, 'evidence') and result.evidence:
                    if hasattr(result.evidence, 'verified'):
                        result.evidence.verified = False
                    elif isinstance(result.evidence, list):
                        for ev in result.evidence:
                            if hasattr(ev, 'verified'):
                                ev.verified = False
                            elif isinstance(ev, dict):
                                ev['verified'] = False
                    elif isinstance(result.evidence, dict):
                        result.evidence['verified'] = False

            result.metadata['verification'] = v_res.to_dict()

            # Grounding verification for analytical results:
            # Ensure the composed response does not omit or contradict primary analytical values.
            if has_analytical_facts and isinstance(facts, dict):
                ops = facts.get('operations', [])
                for op_item in ops:
                    if op_item.get('operation') == 'hypothetical':
                        op_v = op_item.get('value', {})
                        if isinstance(op_v, dict):
                            h_val = op_v.get('hypothetical_value')
                            if h_val is not None:
                                h_2f = f"{h_val:.2f}"
                                h_4f = f"{h_val:,.4f}".rstrip('0').rstrip('.') if isinstance(h_val, float) else str(h_val)
                                h_1f = str(round(h_val, 1))
                                ans_text = response.text or ""
                                if h_2f not in ans_text and h_4f not in ans_text and h_1f not in ans_text:
                                    logger.warning(
                                        f"[ResponseComposer] Composed text omitted hypothetical_value {h_val}. "
                                        "Falling back to verified engine narrative to guarantee accuracy."
                                    )
                                    computed = facts.get('computed_answer')
                                    if computed and computed.strip():
                                        result.answer = computed
        else:
            logger.warning("ResponseComposer generation failed: %s", response.error)

            # ── Deterministic fallback when analysis succeeded but LLM failed ──
            # If the analytical result is verified, build a short factual answer
            # from the structured data rather than showing a blank/error response.
            # The verified result is NEVER discarded due to a composer failure.
            if result.answer and result.answer.strip():
                # Preserve the verified analytical narrative computed by the engine
                result.composer_failed = True
                logger.info("ResponseComposer: preserving engine computed narrative.")
            else:
                deterministic_answer = self._build_deterministic_answer(context)
                if deterministic_answer:
                    result.answer = deterministic_answer
                    result.composer_failed = True
                    logger.info("ResponseComposer: using deterministic fallback answer.")
                else:
                    computed = (getattr(result, 'facts', {}) or {}).get('computed_answer')
                    if computed and computed.strip():
                        result.answer = computed
                    else:
                        result.answer = "I computed the result but couldn't generate the full explanation right now."
                    result.composer_failed = True

            result.error = response.error

        if result and result.answer:
            from utils.response_boundary import enforce_response_boundary
            result.answer = enforce_response_boundary(result.answer, context)

        return result

    def _build_deterministic_answer(self, context: ExecutionContext) -> str:
        """
        Build a short factual answer from a verified AnalyticalResult without LLM.

        Used as a fallback when the response composer LLM fails.
        The analytical value is NEVER invented — it comes from the verified result only.
        """
        analytical_result = getattr(context, 'analytical_result', None)
        if not analytical_result:
            return ''

        verified = getattr(analytical_result, 'verified', False)
        value = getattr(analytical_result, 'value', None)
        operation = getattr(analytical_result, 'operation', None)
        column = getattr(analytical_result, 'column', None)

        if not verified or value is None:
            return ''

        op_labels = {
            'max': 'highest', 'min': 'lowest', 'mean': 'average',
            'median': 'median', 'sum': 'total', 'count': 'count',
            'std': 'standard deviation', 'mode': 'most frequent',
        }
        op_word = op_labels.get((operation or '').lower(), str(operation or 'result').lower())
        col_phrase = f" of {column}" if column else ""

        if isinstance(value, float) and value == int(value):
            value_str = str(int(value))
        else:
            value_str = str(value)

        matching_rows = getattr(analytical_result, 'matching_rows', []) or []
        dataset_name = getattr(context, 'dataset_filename', 'the dataset') or 'the dataset'
        if (operation or '').lower() in ('max', 'min', 'idxmax', 'idxmin', 'longest', 'shortest', 'highest', 'lowest'):
            from analytics.context_enrichment import enrich_extremum_narrative
            details = getattr(analytical_result, 'details', {}) or {}
            unit = details.get('unit')
            return enrich_extremum_narrative(
                op=operation,
                col=column or 'value',
                val=value_str,
                matching_rows=matching_rows,
                dataset_name=dataset_name,
                details=details,
                unit=unit,
            )

        return f"The {op_word}{col_phrase} is **{value_str}**."


response_composer = ResponseComposer()
