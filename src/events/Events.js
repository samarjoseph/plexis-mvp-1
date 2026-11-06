/**
 * Plexis Event Catalog — all valid EventBus event names.
 *
 * Rules:
 *   1. Every EventBus.emit() call MUST use a constant from this file.
 *   2. Adding a new event = add it here only. No other file needs to change.
 *   3. Event names follow DOMAIN_ACTION format for easy grep/discovery.
 *   4. Backend-originated events (from SSE) are prefixed with STREAM_.
 *
 * Dev-mode: EventBus validates all emit() calls against this catalog.
 */
export const Events = Object.freeze({

  // ── Dataset lifecycle ───────────────────────────────────────────────────
  /** Fired after a dataset upload SSE stream completes with dataset_id. */
  DATASET_LOADED:   'dataset:loaded',
  /** Fired when the active dataset changes (e.g. switching conversations). */
  DATASET_CHANGED:  'dataset:changed',

  // ── Evidence lifecycle ──────────────────────────────────────────────────
  /** Fired when a new EvidenceReference is stored in EvidenceRegistry. */
  EVIDENCE_CREATED:  'evidence:created',
  /** Fired when user clicks a locator action (Locate / navigate to rows). */
  EVIDENCE_SELECTED: 'evidence:selected',
  /** Fired to clear all active evidence highlights from the workspace. */
  EVIDENCE_CLEARED:  'evidence:cleared',
  /** Fired when a new dataset upload makes previous evidence stale. */
  EVIDENCE_STALE:    'evidence:stale',

  // ── Workspace panel ─────────────────────────────────────────────────────
  /** Fired to open the DatasetWorkspace panel for a given dataset. */
  WORKSPACE_OPENED:             'workspace:opened',
  /** Fired to close the DatasetWorkspace panel. */
  WORKSPACE_CLOSED:             'workspace:closed',
  /** Fired when workspace column filter changes. */
  WORKSPACE_FILTER_CHANGED:     'workspace:filter_changed',
  /** Fired when workspace sort changes. */
  WORKSPACE_SORT_CHANGED:       'workspace:sort_changed',
  /** Fired when row selection changes (multi-select). */
  WORKSPACE_SELECTION_CHANGED:  'workspace:selection_changed',
  /** Fired when workspace search text changes. */
  WORKSPACE_SEARCH_CHANGED:     'workspace:search_changed',

  // ── Navigation ──────────────────────────────────────────────────────────
  /** Navigate workspace to a specific EvidenceReference. */
  NAVIGATE_TO_EVIDENCE:  'navigate:evidence',
  /** Navigate workspace to a specific row by index. */
  NAVIGATE_TO_ROW:       'navigate:row',

  // ── Workspace actions (user-initiated from workspace UI) ─────────────────
  /**
   * Fired when user right-clicks "Explain this row" or selects rows and
   * clicks "Ask about selection". Payload:
   *   { type: 'explain_row' | 'ask_about_rows', row_indices, row_data, column_names }
   */
  WORKSPACE_ACTION_REQUESTED: 'workspace:action_requested',

  // ── Chat ────────────────────────────────────────────────────────────────
  /**
   * Fired after an AI chat message arrives that contains evidence.
   * Payload: { messageId, evidence }
   */
  CHAT_MESSAGE_WITH_EVIDENCE: 'chat:message_with_evidence',
  /** Fired to prefill the chat input bar with a question or query. Payload: text */
  PREFILL_CHAT_INPUT:         'chat:prefill_input',
  /** Fired to clear the chat input bar. */
  CLEAR_CHAT_INPUT:           'chat:clear_input',

  // ── AI context ──────────────────────────────────────────────────────────
  /** Fired when the LLM context changes (workspace summary, session, etc.). */
  AI_CONTEXT_CHANGED: 'ai:context_changed',

  // ── Spreadsheet AI operations ────────────────────────────────────────────
  /**
   * Fired when user submits a query via the Ask Agent input in the spreadsheet.
   * Payload: { query, spreadsheetContext }
   */
  SPREADSHEET_AGENT_QUERY: 'spreadsheet:agent_query',

  /**
   * Fired when the AI returns a structured operation to execute.
   * Payload: { operation: { type, ...params }, explanation }
   */
  SPREADSHEET_OPERATION: 'spreadsheet:operation',

  /**
   * Fired when a spreadsheet operation completes successfully.
   * Payload: { operation, result }
   */
  SPREADSHEET_OP_COMPLETE: 'spreadsheet:op_complete',

  /**
   * Fired when a spreadsheet operation fails.
   * Payload: { operation, error }
   */
  SPREADSHEET_OP_ERROR: 'spreadsheet:op_error',

  /**
   * Fired when AI Mode is toggled on or off.
   * Payload: { enabled: boolean }
   */
  AI_MODE_TOGGLED: 'ai:mode_toggled',

  /**
   * Fired when Spreadsheet Agent processes a query.
   * Synchronizes the interaction into the main chat conversation.
   * Payload:
   *   query       (str)   — the user's original query
   *   answer      (str)   — the natural language AI explanation
   *   operation   (obj)   — the structured operation ({ type, column, n, ... })
   *   result      (obj)   — the deterministic result ({ rowIndices, stats, success, ... })
   *   source      (str)   — 'llm' | 'heuristic'
   *   operationId (str)   — unique ID to reference this op from chat card actions
   *   isError     (bool)  — true if the operation failed
   */
  SPREADSHEET_AGENT_CHAT_MESSAGE: 'spreadsheet:agent_chat_message',

  /**
   * Fired when the user clicks Locate/Undo/Redo inside a spreadsheet result
   * card that was rendered inside the main chat area.
   * Payload: { action: 'locate' | 'undo' | 'redo', operationId }
   */
  SPREADSHEET_CARD_ACTION: 'spreadsheet:card_action',

  /**
   * Fired by Spreadsheet.jsx whenever the operation history state changes:
   * new operation added, undo, redo, or redo-stack cleared.
   *
   * ALL SpreadsheetResultCard instances subscribe to this event so they can
   * derive fresh canUndo / canRedo values from the CANONICAL history state
   * rather than the frozen snapshot that was baked into the message object.
   *
   * Payload:
   *   currentOpIndex  (number)  — current index into operationHistory (-1 = nothing)
   *   historyLength   (number)  — operationHistory.length
   *   redoStackLength (number)  — redoStack.length (0 = redo disabled)
   */
  SPREADSHEET_HISTORY_CHANGED: 'spreadsheet:history_changed',

  // ── Analytical result UX ────────────────────────────────────────────────
  /**
   * Fired when an analytical result is ready and actions are available.
   * Payload: { analysisId, responseActions: {retry, explain, locate}, result }
   */
  ANALYSIS_RESULT_READY: 'analysis:result_ready',

  /**
   * Fired when the Locate action yields multiple result rows.
   * The Dashboard renders a LocateResultSelector to let the user pick one.
   * Payload: { analysisId, rows: [{source_row_number, ...}], column, value }
   */
  ANALYSIS_LOCATE_MULTI: 'analysis:locate_multi',

  /**
   * Fired when the Locate action yields exactly one result row.
   * The workspace navigates directly without showing a selector.
   * Payload: { analysisId, source_row_number, dataframe_index, column }
   */
  ANALYSIS_LOCATE_SINGLE: 'analysis:locate_single',

  /**
   * Fired when the user selects a specific row from the LocateResultSelector.
   * Payload: { analysisId, source_row_number }
   */
  ANALYSIS_LOCATE_SELECTION: 'analysis:locate_selection',

  /**
   * Fired when the user clicks the Explain button on an analytical result card.
   * Payload: { analysisId }
   */
  ANALYSIS_EXPLAIN_REQUESTED: 'analysis:explain_requested',

  /**
   * Fired when the user clicks the Retry button on an analytical result card.
   * Payload: { analysisId, originalQuery }
   */
  ANALYSIS_RETRY_REQUESTED: 'analysis:retry_requested',

  // ── Investigation ────────────────────────────────────────────────────────
  /**
   * Fired to open the Investigation tab in AnalyticsPanelV2.
   * Payload: { datasetId }
   */
  INVESTIGATION_REQUESTED: 'investigation:requested',
  /** A refreshed investigation run has started for the current dataset. */
  INVESTIGATION_REFRESHED: 'investigation:refreshed',

  // ── Transformation lifecycle ─────────────────────────────────────────────
  /**
   * Fired when the backend returns a transformation plan for user review.
   * Payload: { plan, datasetId }
   */
  TRANSFORM_PLAN_RECEIVED: 'transform:plan_received',

  /**
   * Fired after a transformation is successfully applied to the dataset.
   * Payload: { datasetId, result }
   */
  TRANSFORM_APPLIED: 'transform:applied',

  /**
   * Fired after a transformation is successfully undone.
   * Payload: { datasetId, result }
   */
  TRANSFORM_UNDONE: 'transform:undone',

  // ── Plan & Subscription limits ───────────────────────────────────────────
  /** Fired when user hits or encounters a free plan limitation. Payload: { message, linkText, duration } */
  PLAN_LIMIT_NOTICE: 'plan:limit_notice',
});
