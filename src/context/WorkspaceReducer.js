/**
 * WorkspaceReducer — pure state machine for DatasetWorkspace.
 *
 * v2.0 — Spreadsheet Redesign
 *
 * Key changes from v1:
 *   - loadedRows is now ALWAYS [] (data is in Spreadsheet's WindowCache)
 *   - currentOffset removed from meaningful use
 *   - Added allColumns (full column list from backend, before filtering)
 *   - Added filename
 *   - APPEND_DATA is now a no-op (kept for backward compat)
 *   - SET_DATA only stores metadata (columns, totalRows, totalColumns)
 *
 * Pure function — no side effects. Receives (state, action) → next state.
 */

export const WorkspaceActions = Object.freeze({
  OPEN:                       'OPEN',
  CLOSE:                      'CLOSE',
  SET_DATA:                   'SET_DATA',
  APPEND_DATA:                'APPEND_DATA',       // kept for compat — no-op in v2
  SET_FILTERS:                'SET_FILTERS',
  SET_SORT:                   'SET_SORT',
  SET_SEARCH:                 'SET_SEARCH',
  SET_SELECTION:              'SET_SELECTION',
  CLEAR_SELECTION:            'CLEAR_SELECTION',
  APPLY_RENDER_INSTRUCTIONS:  'APPLY_RENDER_INSTRUCTIONS',
  CLEAR_HIGHLIGHTS:           'CLEAR_HIGHLIGHTS',
  SET_LOADING:                'SET_LOADING',
  SET_OFFSET:                 'SET_OFFSET',
  SET_ACTIVE_EVIDENCE:        'SET_ACTIVE_EVIDENCE',
  SET_DATASET:                'SET_DATASET',
});

export const initialWorkspaceState = {
  isOpen:     false,
  datasetId:  null,
  sessionId:  null,
  filename:   '',

  // Metadata — populated by first API response
  columns:        [],   // column names (current view, may differ from allColumns after filter)
  allColumns:     [],   // all column names from original dataset (never filtered)
  totalRows:      0,
  totalColumns:   0,

  // NOT storing loadedRows here — Spreadsheet.jsx owns WindowCache
  // Kept as empty array for any external code that still reads this
  loadedRows:     [],
  currentOffset:  0,

  // View controls — these trigger cache invalidation in Spreadsheet.jsx
  filters:  [],
  sort:     null,       // { col: string, dir: 'asc' | 'desc' }
  search:   '',

  isLoading: false,

  // Selection (read by AI context via buildWorkspaceSummaryPayload)
  selection: {
    rows:    [],
    columns: [],
  },

  // Evidence rendering
  renderInstructions: null,
  activeEvidence:     null,
};

export function workspaceReducer(state, action) {
  switch (action.type) {

    case WorkspaceActions.OPEN:
      return {
        ...state,
        isOpen:             true,
        datasetId:          action.datasetId ?? state.datasetId,
        sessionId:          action.sessionId ?? state.sessionId,
        // Reset view state on open
        loadedRows:         [],
        totalRows:          0,
        currentOffset:      0,
        isLoading:          true,
        renderInstructions: null,
        activeEvidence:     null,
        sort:               null,
        filters:            [],
        search:             '',
      };

    case WorkspaceActions.CLOSE:
      return {
        ...state,
        isOpen:             false,
        renderInstructions: null,
        activeEvidence:     null,
        selection:          initialWorkspaceState.selection,
      };

    case WorkspaceActions.SET_DATASET:
      if (action.datasetId !== state.datasetId) {
        return {
          ...state,
          datasetId: action.datasetId,
          sessionId: action.sessionId ?? state.sessionId,
          columns: [],
          allColumns: [],
          totalRows: 0,
          totalColumns: 0,
          filename: '',
          isLoading: Boolean(action.datasetId),
          sort: null,
          filters: [],
          search: '',
          selection: initialWorkspaceState.selection,
          renderInstructions: null,
          activeEvidence: null,
        };
      }
      return {
        ...state,
        sessionId: action.sessionId ?? state.sessionId,
      };

    case WorkspaceActions.SET_DATA:
      // In v2, rows are NOT stored here. Only metadata.
      return {
        ...state,
        columns:        action.columns        ?? state.columns,
        allColumns:     action.allColumns      ?? action.columns ?? state.allColumns,
        totalRows:      action.totalRows       ?? state.totalRows,
        totalColumns:   action.totalColumns    ?? state.totalColumns,
        filename:       action.filename        ?? state.filename,
        loadedRows:     [],   // always empty — rows live in Spreadsheet's WindowCache
        currentOffset:  0,
        isLoading:      false,
      };

    case WorkspaceActions.APPEND_DATA:
      // No-op in v2 — WindowCache handles row storage
      return {
        ...state,
        totalRows:    action.totalRows    ?? state.totalRows,
        totalColumns: action.totalColumns ?? state.totalColumns,
        isLoading:    false,
      };

    case WorkspaceActions.SET_FILTERS:
      return {
        ...state,
        filters:        action.filters ?? [],
        currentOffset:  0,
        loadedRows:     [],
        isLoading:      true,   // triggers fetchMetadata in context
      };

    case WorkspaceActions.SET_SORT:
      return {
        ...state,
        sort:           action.sort ?? null,
        currentOffset:  0,
        loadedRows:     [],
        isLoading:      true,
      };

    case WorkspaceActions.SET_SEARCH:
      return {
        ...state,
        search:         action.search ?? '',
        currentOffset:  0,
        loadedRows:     [],
        isLoading:      true,
      };

    case WorkspaceActions.SET_SELECTION:
      return {
        ...state,
        selection: {
          rows:    action.rows    ?? state.selection.rows,
          columns: action.columns ?? state.selection.columns,
        },
      };

    case WorkspaceActions.CLEAR_SELECTION:
      return {
        ...state,
        selection: initialWorkspaceState.selection,
      };

    case WorkspaceActions.APPLY_RENDER_INSTRUCTIONS:
      return {
        ...state,
        renderInstructions: action.instructions,
        activeEvidence:     action.activeEvidence ?? state.activeEvidence,
      };

    case WorkspaceActions.CLEAR_HIGHLIGHTS:
      return {
        ...state,
        renderInstructions: null,
        activeEvidence:     null,
      };

    case WorkspaceActions.SET_LOADING:
      return {
        ...state,
        isLoading: action.isLoading ?? false,
      };

    case WorkspaceActions.SET_OFFSET:
      return {
        ...state,
        currentOffset: action.offset ?? 0,
      };

    case WorkspaceActions.SET_ACTIVE_EVIDENCE:
      return {
        ...state,
        activeEvidence: action.evidence ?? null,
      };

    default:
      return state;
  }
}
