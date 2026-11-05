"""
TransformEngine — handles TRANSFORM intent.

Flow:
  1. Extract operation from context.llm_operation (set by MasterRouter)
  2. Resolve DataFrame from dataset_registry
  3. Call TransformationPlanner to compute the plan (no mutation)
  4. Store plan in TransformationStore (L1) + PostgreSQL (L2 if available)
  5. Return EngineResult with transform_plan attached

The DataFrame is NEVER mutated here. Mutation happens only in /api/transform/apply
after explicit user approval.
"""
from __future__ import annotations
import logging
import re
import uuid
from typing import Optional

import pandas as pd

from engines.base import BaseEngine, EngineResult
from core.context import ExecutionContext
from transformation.planner import (
    transformation_planner, TransformationClarificationRequired, 
    ValueTypeMismatchConfirmationRequired, _compute_fingerprint,
)
from transformation.store import transformation_store

logger = logging.getLogger(__name__)


class TransformEngine(BaseEngine):

    @property
    def engine_name(self) -> str:
        return 'transform'

    def can_handle(self, context: ExecutionContext) -> bool:
        return getattr(context, 'intent', None) in ('transform', 'TRANSFORM')

    def handle(self, context: ExecutionContext) -> EngineResult:
        dataset_id = str(getattr(context, 'dataset_id', '') or '')
        if not dataset_id:
            return EngineResult(
                answer="No dataset is currently loaded. Please upload a dataset before requesting a transformation.",
                source='transform',
                provider='system',
                success=False,
                error='No dataset loaded',
                should_compose=False,
            )

        # Resolve DataFrame from L1 registry
        try:
            from datasets.registry import dataset_registry
            entry = dataset_registry.get(dataset_id)
            if entry is None or entry.dataframe is None:
                return EngineResult(
                    answer="Dataset not found in memory. Please re-upload your dataset.",
                    source='transform',
                    provider='system',
                    success=False,
                    error='Dataset not in registry',
                    should_compose=False,
                )
            df = entry.dataframe
        except Exception as e:
            logger.error('[TransformEngine] Registry lookup failed: %s', e)
            return EngineResult(
                answer='Failed to access dataset.',
                source='transform', provider='system', success=False,
                error=str(e), should_compose=False,
            )

        # Get LLM-structured operation
        operation = getattr(context, 'llm_operation', None) or {}
        if not operation or not operation.get('type'):
            return EngineResult(
                answer=(
                    "I couldn't understand what transformation to apply. "
                    "Please be more specific. For example: 'fill missing values with 0' "
                    "or 'drop duplicate rows'."
                ),
                source='transform',
                provider='system',
                success=True,
                metadata={'transform_plan': None},
                should_compose=False,
            )

        # The LLM/legacy route may supply `new_value=None` for a quoted
        # null-like word even though the raw request explicitly quoted it.
        # Canonicalize literals once, at the request boundary, from the source
        # message. The resulting TypedLiteral metadata then travels unchanged
        # through pending state, scope selection, IR, preview, and execution.
        if str(operation.get('type', '')).upper() == 'REPLACE_VALUES':
            try:
                from router.master_router import _parse_transform_from_message
                parsed_request = _parse_transform_from_message(
                    getattr(context, 'message', '') or ''
                )
                if parsed_request and parsed_request.get('type') == 'REPLACE_VALUES':
                    for typed_key, raw_key in (
                        ('_old_typed', 'old_value'),
                        ('_new_typed', 'new_value'),
                    ):
                        literal = parsed_request.get(typed_key)
                        if literal is not None:
                            operation[typed_key] = literal
                            operation[raw_key] = parsed_request.get(raw_key)
            except Exception as exc:
                # Parsing failure must not block otherwise supported plans.
                logger.debug('[TransformEngine] Literal boundary canonicalization skipped: %s', exc)

        if operation.get('_missing_slots') and operation.get('type') != 'EXTREME_VALUE_AMBIGUITY':
            slots = list(operation['_missing_slots'])
            question = self._question_for_slots(operation.get('type'), slots, operation)
            return self._create_pending_clarification(
                context, operation, df,
                candidates=list(operation.get('columns') or df.columns),
                missing_slots=slots, question=question,
                clarification_type=slots[0].upper(),
            )

        # Generate plan (no mutation)
        try:
            op_type = operation.get('type')
            operations = getattr(context, 'llm_operations', None) or ([operation] if operation else [])
            is_compound = len(operations) > 1 or op_type in ('IR_REQUIRED', 'IR_COMPOSITE') or bool(operation.get('steps'))

            if is_compound or op_type == 'IR_REQUIRED':
                from transformation.ir_planner import ir_planner
                from transformation.validator import ir_validator
                from transformation.compiler import ir_compiler
                from transformation.ir import TransformationIR, TransformStep, PRIMITIVE_REGISTRY

                user_msg = operation.get('user_prompt') or getattr(context, 'message', '')

                # If from a structured clarification choice with an already resolved expression
                if operation.get('_from_clarification') and operation.get('expression') and operation.get('column'):
                    col = operation['column']
                    is_existing = col in df.columns
                    action = operation.get('_action') or ('MODIFY_COLUMN' if is_existing else 'CREATE_COLUMN')
                    expr = operation['expression']
                    ir = TransformationIR(
                        goal=f"{'Update' if is_existing else 'Create'} column '{col}'",
                        steps=[
                            TransformStep(
                                id='step_1',
                                operation=action,
                                params={'column': col},
                                expression=expr,
                                label=f"{'Update' if is_existing else 'Create'} column '{col}'",
                            )
                        ]
                    )
                else:
                    dtypes = {col: str(dtype) for col, dtype in df.dtypes.items()}
                    nullable = {col: df[col].isnull().any() for col in df.columns}

                    try:
                        ir = ir_planner.plan(user_msg, list(df.columns), dtypes, nullable)
                    except Exception as llm_err:
                        logger.warning(
                            '[TransformEngine] IR planning LLM failed (%s); attempting operation synthesis fallback',
                            llm_err
                        )
                        ir = self._synthesize_ir_from_operations(operations, df, user_msg)
                        if not ir or not ir.steps:
                            raise

                # Check if any step requires an expression but has None
                missing_expr_step = None
                for idx, s in enumerate(ir.steps):
                    spec = PRIMITIVE_REGISTRY.get(s.operation)
                    needs_expr = (spec and spec.requires_expression and not s.expression) or (s.operation in ('CREATE_COLUMN', 'MODIFY_COLUMN') and not s.expression)
                    if needs_expr:
                        col_name = s.params.get('column')
                        subsequent_fill = any(
                            sub.operation in ('FILL_SEQUENTIAL', 'FILL_PATTERN', 'FILL', 'POPULATE_SEQUENCE', 'MAP_VALUES')
                            and sub.params.get('column') == col_name
                            for sub in ir.steps[idx + 1:]
                        )
                        if not subsequent_fill:
                            missing_expr_step = s
                            break

                # Validate IR
                validation = ir_validator.validate(ir, df)
                if not missing_expr_step and not validation.valid:
                    for err in validation.errors:
                        if "requires an expression" in err:
                            for s in ir.steps:
                                if s.id in err:
                                    missing_expr_step = s
                                    break
                            if not missing_expr_step and ir.steps:
                                missing_expr_step = ir.steps[0]
                            break

                if missing_expr_step is not None:
                    col_name = missing_expr_step.params.get('column') or target_col or 'new_column'
                    self._raise_column_clarification(df, col_name, user_msg, is_existing=(col_name in df.columns))

                if not validation.valid:
                    if getattr(validation, 'unsupported', None):
                        err_msg = f"This transformation requires a {validation.unsupported} primitive that is not currently available."
                    else:
                        err_msg = "Validation failed: " + "; ".join(validation.errors)
                    return EngineResult(
                        answer=f"Cannot create transformation plan: {err_msg}",
                        source='transform', provider='system', success=False,
                        error=err_msg, should_compose=False,
                    )
                
                # Compile and Simulate IR
                sim = ir_compiler.compile_and_simulate(ir, df, dataset_id)
                if not sim.success:
                    err_msg = "Simulation failed: " + "; ".join(sim.errors)
                    return EngineResult(
                        answer=f"Cannot create transformation plan: {err_msg}",
                        source='transform', provider='system', success=False,
                        error=err_msg, should_compose=False,
                    )
                
                # Generate final TransformationPlan contract
                fingerprint = _compute_fingerprint(df)
                plan = ir_compiler.simulation_to_plan(ir, sim, dataset_id, fingerprint)
                
            else:
                # Direct planner with resilient IR Planner fallback
                try:
                    plan = transformation_planner.plan(df, operation, dataset_id)
                except (TransformationClarificationRequired, ValueTypeMismatchConfirmationRequired):
                    raise
                except Exception as planner_err:
                    if type(planner_err).__name__ == 'ValueAmbiguityClarificationRequired':
                        raise
                    user_msg = getattr(context, 'message', '') or operation.get('user_prompt') or ''
                    if user_msg:
                        logger.info(
                            "[TransformEngine] Direct planner failed (%s). Delegating to intelligent IR Planner for: %s",
                            planner_err, user_msg
                        )
                        from transformation.ir_planner import ir_planner
                        from transformation.validator import ir_validator
                        from transformation.compiler import ir_compiler

                        dtypes = {col: str(dtype) for col, dtype in df.dtypes.items()}
                        nullable = {col: df[col].isnull().any() for col in df.columns}
                        ir = ir_planner.plan(user_msg, list(df.columns), dtypes, nullable)
                        validation = ir_validator.validate(ir, df)
                        if not validation.valid:
                            err_msg = "Validation failed: " + "; ".join(validation.errors)
                            return EngineResult(
                                answer=f"Cannot create transformation plan: {err_msg}",
                                source='transform', provider='system', success=False,
                                error=err_msg, should_compose=False,
                            )
                        sim = ir_compiler.compile_and_simulate(ir, df, dataset_id)
                        if not sim.success:
                            err_msg = "Simulation failed: " + "; ".join(sim.errors)
                            return EngineResult(
                                answer=f"Cannot create transformation plan: {err_msg}",
                                source='transform', provider='system', success=False,
                                error=err_msg, should_compose=False,
                            )
                        fingerprint = _compute_fingerprint(df)
                        plan = ir_compiler.simulation_to_plan(ir, sim, dataset_id, fingerprint)
                    else:
                        raise planner_err
                
        except TransformationClarificationRequired as e:
            extra_meta = {}
            if getattr(e, 'ambiguous_options', None):
                extra_meta['ambiguous_options'] = e.ambiguous_options
            return self._create_pending_clarification(
                context, e.operation_payload or operation, df, candidates=e.candidate_columns,
                missing_slots=e.missing_slots, question=str(e),
                clarification_type=e.clarification_type,
                extra_metadata=extra_meta if extra_meta else None,
            )
        except ValueTypeMismatchConfirmationRequired as e:
            return self._create_pending_clarification(
                context, e.operation_payload or operation, df, candidates=e.candidate_columns,
                missing_slots=['value_type_confirmation'], question=str(e),
                clarification_type=e.clarification_type,
                extra_metadata={
                    'requested_value_info': e.requested_value_info,
                    'candidate_value_info': e.candidate_value_info,
                    'candidate_count': e.candidate_count,
                }
            )
        except Exception as e:
            if type(e).__name__ == 'ValueAmbiguityClarificationRequired':
                return self._create_pending_clarification(
                    context, getattr(e, 'operation_payload', None) or operation, df, candidates=[],
                    missing_slots=['value_ambiguity_confirmation'], question=str(e),
                    clarification_type='VALUE_AMBIGUITY',
                    extra_metadata={
                        'ambiguous_options': getattr(e, 'ambiguous_options', []),
                    }
                )
            if isinstance(e, ValueError):
                return EngineResult(
                    answer=f"Cannot create transformation plan: {e}",
                    source='transform', provider='system', success=False, error=str(e),
                    should_compose=False,
                )
            logger.error('[TransformEngine] Planner error: %s', e, exc_info=True)
            return EngineResult(
                answer=(
                    "I couldn't plan that transformation right now due to a temporary model service issue. "
                    "Please try again or rephrase your request."
                ),
                source='transform', provider='system', success=False,
                error=str(e), should_compose=False,
            )

        # Store in L1 cache
        transformation_store.store(
            plan,
            session_id=getattr(context, 'chat_id', None) or getattr(context, 'session_id', None),
        )
        self._retain_active_transformation(context, operation, plan, df)

        # Persist to PostgreSQL (best-effort)
        self._persist_plan(context, plan, entry)

        # Build user-facing answer
        cells = plan.affected_cells_count
        is_schema_op = (
            plan.operation_type in ('DROP_COLUMN', 'RENAME_COLUMN', 'ADD_COLUMN')
            or (plan.operation_payload or {}).get('impact', {}).get('type') == 'schema'
            or bool(plan.affected_columns and plan.operation_type in ('DROP_COLUMN', 'RENAME_COLUMN'))
        )

        if plan.operation_type == 'IR_COMPOSITE':
            step_labels = (plan.operation_payload or {}).get('step_labels', [])
            steps_text = "\n".join(f"{i+1}. {label}" for i, label in enumerate(step_labels)) if step_labels else ""
            answer = f"{plan.operation_label}.\n\n"
            if steps_text:
                answer += f"**Planned Steps:**\n{steps_text}\n\n"
            answer += (
                f"{cells} {'cell' if cells == 1 else 'cells'} will change across {len(plan.affected_columns)} column(s). "
                f"Review the plan below and click **Apply Changes**."
            )
            if plan.warnings:
                answer += '\n\n⚠️ ' + ' '.join(plan.warnings)
        elif is_schema_op:
            cols_count = len(plan.affected_columns) or 1
            if plan.operation_type == 'DROP_COLUMN':
                col_name = plan.affected_columns[0] if plan.affected_columns else ''
                answer = (
                    f"{plan.operation_label}.\n\n"
                    f"Column '{col_name}' will be dropped from the dataset. "
                    f"Review the plan below and click **Apply Changes**."
                )
            elif plan.operation_type == 'RENAME_COLUMN':
                answer = (
                    f"{plan.operation_label}.\n\n"
                    f"{cols_count} column{' will' if cols_count == 1 else 's will'} be renamed. "
                    f"Review the plan below and click **Apply Changes**."
                )
            elif plan.operation_type == 'ADD_COLUMN':
                col_name = plan.affected_columns[0] if plan.affected_columns else ''
                answer = (
                    f"{plan.operation_label}.\n\n"
                    f"Column '{col_name}' will be added to the dataset. "
                    f"Review the plan below and click **Apply Changes**."
                )
            else:
                answer = (
                    f"{plan.operation_label}.\n\n"
                    f"Schema will be updated across {cols_count} column(s). "
                    f"Review the plan below and click **Apply Changes**."
                )
            if plan.warnings:
                answer += '\n\n⚠️ ' + ' '.join(plan.warnings)
        elif cells == 0 and not plan.affected_columns:
            answer = (
                f"{plan.operation_label}. No changes needed — "
                + (plan.warnings[0] if plan.warnings else 'data already satisfies this condition.')
            )
        else:
            answer = (
                f"{plan.operation_label}.\n\n"
                f"{cells} {'cell' if cells == 1 else 'cells'} will change "
                f"across {len(plan.affected_columns)} column(s). "
                f"Review the plan below and click **Review Changes** to see the full before/after diff."
            )
            if plan.warnings:
                answer += '\n\n⚠️ ' + ' '.join(plan.warnings)

        result_metadata = {'transform_plan': self._plan_to_dict(plan)}
        if operation.get('_supersedes_plan_id'):
            conversation_id = str(
                getattr(context, 'chat_id', None) or getattr(context, 'session_id', '') or ''
            )
            active_state = transformation_store.pending_for(conversation_id, dataset_id)
            latest_correction = (active_state.correction_history[-1]
                                 if active_state and active_state.correction_history else {})
            result_metadata['transformation_update'] = {
                'previous_plan_id': operation.get('_supersedes_plan_id'),
                'previous_operation_label': operation.get('_previous_operation_label') or '',
                'operation_label': plan.operation_label,
                'plan_id': plan.plan_id,
                'changed_slots': latest_correction.get('slots', []),
            }

        return EngineResult(
            answer=answer,
            source='transform',
            provider='system',
            success=True,
            metadata=result_metadata,
            should_compose=False,
        )

    def _retain_active_transformation(self, context, operation, plan, df):
        """Retain resolved slots through review so corrections can replan safely."""
        from transformation.store import PendingTransformationContext
        conversation_id = str(
            getattr(context, 'chat_id', None) or getattr(context, 'session_id', '') or ''
        )
        dataset_id = str(getattr(context, 'dataset_id', '') or '')
        if not conversation_id or not dataset_id:
            return
        pending = transformation_store.pending_for(conversation_id, dataset_id)
        current_payload = dict(plan.operation_payload)
        if pending is None or pending.transformation_id != operation.get('_pending_transformation_id'):
            from transformation.history import transformation_history
            dataset_version = transformation_history.get_version(dataset_id)
            try:
                db_dataset_id = uuid.UUID(dataset_id)
                from db.session import get_db
                from db.repositories.transformation_repository import transformation_repository
                db = get_db()
                if db is not None:
                    latest = transformation_repository.get_latest_version(db, db_dataset_id)
                    if latest is not None:
                        dataset_version = max(dataset_version, int(latest.version_number))
            except (ValueError, TypeError):
                pass
            except Exception as exc:
                logger.debug('[TransformEngine] Could not read active dataset version: %s', exc)
            pending = PendingTransformationContext.create(
                conversation_id=conversation_id,
                dataset_id=dataset_id,
                dataset_version=dataset_version,
                dataset_fingerprint=_compute_fingerprint(df),
                operation_type=plan.operation_type,
                operation_payload=current_payload,
                candidate_columns=list(plan.affected_columns),
                selected_columns=list(current_payload.get('columns') or plan.affected_columns),
                scope=current_payload.get('scope'),
                missing_slots=[],
                original_request=getattr(context, 'message', '') or '',
                latest_question='',
                source_typed_literal=current_payload.get('_old_typed'),
                destination_typed_literal=(current_payload.get('_new_typed') or current_payload.get('_value_typed')),
            )
        pending.operation_type = plan.operation_type
        pending.operation_payload = current_payload
        if not pending.candidate_columns:
            pending.candidate_columns = list(plan.affected_columns)
        pending.selected_columns = list(current_payload.get('columns') or plan.affected_columns)
        pending.scope = current_payload.get('scope')
        pending.source_typed_literal = current_payload.get('_old_typed')
        pending.destination_typed_literal = current_payload.get('_new_typed') or current_payload.get('_value_typed')
        pending.current_plan_id = plan.plan_id
        pending.dataset_fingerprint = plan.dataset_fingerprint
        transformation_store.activate_plan(pending)

    def _raise_column_clarification(self, df: pd.DataFrame, col_name: str, user_msg: str, is_existing: bool = False):
        # Find candidate columns:
        # 1. Matching tokens from col_name
        tokens = [t.lower() for t in re.findall(r'[A-Za-z0-9]+', col_name) if len(t) > 2]
        overlap_cols = [c for c in df.columns if c != col_name and any(t in c.lower() for t in tokens)]
        # 2. Numeric columns
        num_cols = [c for c in df.columns if c != col_name and c not in overlap_cols and pd.api.types.is_numeric_dtype(df[c])]
        # 3. Other columns
        other_cols = [c for c in df.columns if c != col_name and c not in overlap_cols and c not in num_cols]
        
        cand_cols = (overlap_cols + num_cols + other_cols)[:6]

        action_type = 'MODIFY_COLUMN' if is_existing else 'CREATE_COLUMN'
        verb_create = 'Update' if is_existing else 'Create'

        options = [
            {
                'label': 'Set values to 0' if is_existing else 'Initialize with 0',
                'description': f"{verb_create} column '{col_name}' with 0",
                'action': 'INIT_ZERO',
                'operation': {
                    'type': 'IR_REQUIRED',
                    '_action': action_type,
                    'column': col_name,
                    'expression': {
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
            },
            {
                'label': 'Set values to null / empty' if is_existing else 'Initialize with null',
                'description': f"{verb_create} column '{col_name}' with null values",
                'action': 'INIT_NULL',
                'operation': {
                    'type': 'IR_REQUIRED',
                    '_action': action_type,
                    'column': col_name,
                    'expression': {
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
            }
        ]

        if cand_cols:
            best_cand = cand_cols[0]
            options.append({
                'label': f"Copy from {best_cand}",
                'description': f"Populate '{col_name}' with values from '{best_cand}'",
                'action': f"COPY_{best_cand}",
                'operation': {
                    'type': 'IR_REQUIRED',
                    '_action': action_type,
                    'column': col_name,
                    'expression': {
                        'type': 'COLUMN',
                        'name': best_cand
                    }
                }
            })

        if is_existing:
            if cand_cols:
                example_cols = ', '.join(f'`{c}`' for c in cand_cols[:2])
                question = (
                    f"How would you like to update or populate the column **'{col_name}'**? "
                    f"Please specify a formula (for example, using columns like {example_cols}), "
                    f"values, or choose an option below."
                )
            else:
                question = (
                    f"How would you like to update or populate the column **'{col_name}'**? "
                    f"Please specify a formula, values, or choose an option below."
                )
        else:
            if cand_cols:
                example_cols = ', '.join(f'`{c}`' for c in cand_cols[:2])
                question = (
                    f"How should the new column **'{col_name}'** be calculated or populated? "
                    f"Please specify a formula (for example, using columns like {example_cols}) "
                    f"or choose an initial value below."
                )
            else:
                question = (
                    f"How should the new column **'{col_name}'** be calculated or populated? "
                    f"Please specify a formula or choose an initial value below."
                )

        raise TransformationClarificationRequired(
            question,
            missing_slots=['expression'],
            candidate_columns=cand_cols,
            clarification_type='EXPRESSION_REQUIRED',
            operation_payload={
                'type': 'IR_REQUIRED',
                '_action': action_type,
                'column': col_name,
                'original_request': user_msg,
            },
            ambiguous_options=options,
        )

    def _raise_create_column_clarification(self, df: pd.DataFrame, col_name: str, user_msg: str):
        is_existing = bool(df is not None and col_name in df.columns)
        return self._raise_column_clarification(df, col_name, user_msg, is_existing=is_existing)

    def _question_for_slots(self, operation_type, slots, operation):
        if 'fill_value' in slots:
            return 'What should I fill the missing values with? You can give a value or a strategy such as median.'
        if 'target_scope' in slots and operation_type == 'RENAME_COLUMN':
            return 'Which columns should I rename?'
        if 'new_names' in slots:
            return 'What new names should I use for those columns?'
        return 'Which columns should I change?'

    def _create_pending_clarification(
        self, context, operation, df, *, candidates, missing_slots,
        question, clarification_type, extra_metadata: dict = None
    ):
        from transformation.history import transformation_history
        from transformation.store import PendingTransformationContext

        conversation_id = str(
            getattr(context, 'chat_id', None) or getattr(context, 'session_id', '') or ''
        )
        dataset_version = transformation_history.get_version(str(getattr(context, 'dataset_id', '')))
        try:
            db_dataset_id = uuid.UUID(str(getattr(context, 'dataset_id', '')))
            from db.session import get_db
            from db.repositories.transformation_repository import transformation_repository
            db = get_db()
            if db is not None:
                latest = transformation_repository.get_latest_version(db, db_dataset_id)
                if latest is not None:
                    dataset_version = max(dataset_version, int(latest.version_number))
        except (ValueError, TypeError):
            pass
        except Exception as exc:
            logger.debug('[TransformEngine] Could not read persisted dataset version: %s', exc)
        pending = PendingTransformationContext.create(
            transformation_id=operation.get('_pending_transformation_id') or str(uuid.uuid4()),
            conversation_id=conversation_id,
            dataset_id=str(getattr(context, 'dataset_id', '')),
            dataset_version=dataset_version,
            dataset_fingerprint=_compute_fingerprint(df),
            operation_type=str(operation.get('type', '')),
            operation_payload=dict(operation),
            source_typed_literal=operation.get('_old_typed'),
            destination_typed_literal=operation.get('_new_typed') or operation.get('_value_typed'),
            candidate_columns=list(candidates),
            selected_columns=list(operation.get('columns') or []) or None,
            scope=operation.get('scope'),
            missing_slots=list(missing_slots),
            original_request=getattr(context, 'message', '') or '',
            latest_question=question,
            clarification_type=clarification_type,
        )
        transformation_store.set_pending(pending)
        logger.info(
            '[TransformEngine] Awaiting clarification transformation=%s dataset=%s slots=%s candidates=%s',
            pending.transformation_id, pending.dataset_id, pending.missing_slots,
            pending.candidate_columns,
        )
        return EngineResult(
            answer=question, source='transform', provider='system', success=True,
            metadata={
                'transform_plan': None,
                'clarification': {
                    'status': 'AWAITING_CLARIFICATION',
                    'transformation_id': pending.transformation_id,
                    'clarification_type': pending.clarification_type,
                    'question': pending.latest_question,
                    'candidate_columns': pending.candidate_columns,
                    'missing_slots': pending.missing_slots,
                    'operation': pending.operation_type,
                    'source': pending.source_typed_literal,
                    'destination': pending.destination_typed_literal,
                    'suggested_scopes': [
                        {'type': 'ALL_MATCHING_COLUMNS', 'label': 'All matching columns'},
                        *[
                            {'type': 'EXPLICIT_COLUMNS', 'column': column, 'label': str(column)}
                            for column in pending.candidate_columns
                        ],
                    ],
                    **(extra_metadata or {})
                },
            },
            should_compose=False,
        )

    def _synthesize_ir_from_operations(self, operations: list, df: pd.DataFrame, user_msg: str):
        from transformation.ir import TransformationIR, TransformStep
        steps = []
        created_cols = set(df.columns)
        step_idx = 1
        for op in operations:
            if not isinstance(op, dict):
                continue
            op_t = str(op.get('type', '')).upper()
            if op_t in ('ADD_COLUMN', 'CREATE_COLUMN'):
                col = op.get('column') or op.get('name')
                if col:
                    created_cols.add(col)
                    expr = op.get('expression') or {'type': 'LITERAL', 'value': op.get('value')}
                    steps.append(TransformStep(
                        id=f'step_{step_idx}',
                        operation='CREATE_COLUMN',
                        params={'column': col},
                        expression=expr,
                        label=f"Create column '{col}'"
                    ))
                    step_idx += 1
            elif op_t == 'FILL_SEQUENTIAL':
                col = op.get('column')
                if col and col not in created_cols:
                    created_cols.add(col)
                    steps.append(TransformStep(
                        id=f'step_{step_idx}',
                        operation='CREATE_COLUMN',
                        params={'column': col},
                        expression={'type': 'LITERAL', 'value': None},
                        label=f"Create column '{col}'"
                    ))
                    step_idx += 1
                if col:
                    steps.append(TransformStep(
                        id=f'step_{step_idx}',
                        operation='FILL_SEQUENTIAL',
                        params={'column': col, 'start': op.get('start', 1), 'step': op.get('step', 1)},
                        label=f"Fill '{col}' sequentially from {op.get('start', 1)}"
                    ))
                    step_idx += 1
            elif op_t == 'REMOVE_EMPTY_ROWS':
                steps.append(TransformStep(
                    id=f'step_{step_idx}',
                    operation='REMOVE_EMPTY_ROWS',
                    params={'how': op.get('how', 'any'), 'columns': op.get('columns')},
                    label="Remove rows containing missing values"
                ))
                step_idx += 1
            elif op_t == 'FILL_MISSING':
                cols = op.get('columns') or ([op['column']] if op.get('column') else [])
                steps.append(TransformStep(
                    id=f'step_{step_idx}',
                    operation='FILL',
                    params={'columns': cols, 'value': op.get('value', 0)},
                    label=f"Fill missing values in {cols}"
                ))
                step_idx += 1
            elif op_t == 'DROP_COLUMN':
                col = op.get('column')
                if col:
                    steps.append(TransformStep(
                        id=f'step_{step_idx}',
                        operation='DROP_COLUMN',
                        params={'column': col},
                        label=f"Drop column '{col}'"
                    ))
                    step_idx += 1
            elif op_t == 'RENAME_COLUMN':
                old = op.get('column') or op.get('old_name')
                new = op.get('new_name')
                if old and new:
                    steps.append(TransformStep(
                        id=f'step_{step_idx}',
                        operation='RENAME_COLUMN',
                        params={'old_name': old, 'new_name': new},
                        label=f"Rename '{old}' to '{new}'"
                    ))
                    step_idx += 1
            elif op_t == 'DROP_DUPLICATES':
                steps.append(TransformStep(
                    id=f'step_{step_idx}',
                    operation='DROP_DUPLICATES',
                    params={'columns': op.get('columns', [])},
                    label="Remove duplicate rows"
                ))
                step_idx += 1
            elif op_t == 'SORT_ROWS':
                steps.append(TransformStep(
                    id=f'step_{step_idx}',
                    operation='SORT_ROWS',
                    params={'column': op.get('column'), 'ascending': op.get('ascending', True)},
                    label=f"Sort by '{op.get('column')}'"
                ))
                step_idx += 1
            elif op_t == 'FILTER_ROWS':
                steps.append(TransformStep(
                    id=f'step_{step_idx}',
                    operation='FILTER_ROWS',
                    params=op,
                    expression=op.get('expression'),
                    label=f"Filter rows on '{op.get('column')}'"
                ))
                step_idx += 1
            elif op_t == 'CUSTOM_TRANSFORM':
                steps.append(TransformStep(
                    id=f'step_{step_idx}',
                    operation='CUSTOM_TRANSFORM',
                    params={'code': op.get('code', '')},
                    label="Custom transformation"
                ))
                step_idx += 1
            elif op_t == 'REPLACE_VALUES':
                steps.append(TransformStep(
                    id=f'step_{step_idx}',
                    operation='REPLACE',
                    params={'column': op.get('column'), 'old_value': op.get('old_value'), 'new_value': op.get('new_value'), '_old_typed': op.get('_old_typed'), '_new_typed': op.get('_new_typed')},
                    label=f"Replace '{op.get('old_value')}' with '{op.get('new_value')}' in '{op.get('column')}'"
                ))
                step_idx += 1
            elif op_t == 'NORMALIZE_TEXT':
                steps.append(TransformStep(
                    id=f'step_{step_idx}',
                    operation='NORMALIZE_TEXT',
                    params=op,
                    label=f"Normalize text in {op.get('columns')}"
                ))
                step_idx += 1
            elif op_t == 'UPDATE_CELL':
                steps.append(TransformStep(
                    id=f'step_{step_idx}',
                    operation='UPDATE_CELL',
                    params=op,
                    label=f"Update cell in '{op.get('column')}'"
                ))
                step_idx += 1
        return TransformationIR(
            goal=user_msg or "Execute multi-step transformation",
            steps=steps
        )

    def _persist_plan(self, context, plan, entry):
        """Persist plan to PostgreSQL — best-effort, never raises."""
        try:
            from db.session import get_db
            from db.repositories.transformation_repository import transformation_repository
            db = get_db()
            if db is None:
                return

            # Get the db-level dataset_id (may differ from registry key)
            db_dataset_id = None
            if entry and hasattr(entry, 'dataset_id'):
                try:
                    db_dataset_id = uuid.UUID(str(entry.dataset_id))
                except Exception:
                    pass
            if db_dataset_id is None:
                try:
                    db_dataset_id = uuid.UUID(plan.dataset_id)
                except Exception:
                    return

            user_id = getattr(context, 'user_id', None)
            chat_id = getattr(context, 'chat_id', None)

            transformation_repository.create_plan(
                db,
                plan_id=uuid.UUID(plan.plan_id),
                dataset_id=db_dataset_id,
                operation_type=plan.operation_type,
                operation_payload=plan.operation_payload,
                operation_code=plan.operation_code,
                affected_rows_count=plan.affected_rows_count,
                affected_cells_count=plan.affected_cells_count,
                before_sample=[{
                    'row_index': c.row_index,
                    'source_row_number': c.source_row_number,
                    'column': c.column,
                    'before': str(c.before) if c.before is not None else None,
                    'after': str(c.after) if c.after is not None else None,
                } for c in plan.before_sample],
                dataset_fingerprint=plan.dataset_fingerprint,
                user_id=uuid.UUID(str(user_id)) if user_id else None,
                chat_id=uuid.UUID(str(chat_id)) if chat_id else None,
            )
            db.commit()
        except Exception as e:
            logger.warning('[TransformEngine] Plan persist failed (non-fatal): %s', e)

    def _plan_to_dict(self, plan) -> dict:
        import math
        import pandas as pd
        def _is_nan(v):
            try:
                if v is None: return False
                if isinstance(v, float) and (math.isnan(v) or math.isinf(v)): return True
                if pd.isna(v): return True
                return False
            except:
                return False

        def _clean_val(v):
            if v is None:
                return None
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                if _is_nan(v):
                    return None
                return v
            try:
                import numpy as np
                if isinstance(v, np.integer):
                    return int(v)
                if isinstance(v, np.floating):
                    return float(v) if not np.isnan(v) and not np.isinf(v) else None
                if isinstance(v, np.bool_):
                    return bool(v)
            except ImportError:
                pass
            if isinstance(v, dict):
                return {str(k): _clean_val(val) for k, val in v.items()}
            if isinstance(v, (list, tuple, set)):
                return [_clean_val(item) for item in v]
            return str(v)

        return {
            'plan_id': plan.plan_id,
            'dataset_id': plan.dataset_id,
            'operation_type': plan.operation_type,
            'operation_label': plan.operation_label,
            'operation_code': plan.operation_code,
            'affected_rows_count': _clean_val(plan.affected_rows_count),
            'affected_cells_count': _clean_val(plan.affected_cells_count),
            'affected_columns': plan.affected_columns,
            'operation_payload': _clean_val(getattr(plan, 'operation_payload', {})),
            'intelligence_provenance': getattr(plan, 'intelligence_provenance', None),
            'before_sample': [{
                'row_index': _clean_val(c.row_index),
                'source_row_number': _clean_val(c.source_row_number),
                'column': c.column,
                'before': None if _is_nan(c.before) else _clean_val(c.before),
                'after': None if _is_nan(c.after) else _clean_val(c.after),
            } for c in plan.before_sample],
            'warnings': plan.warnings,
            'created_at': plan.created_at,
        }


transform_engine = TransformEngine()
