/**
 * WorkspaceContext — React context and provider for the spreadsheet workspace.
 *
 * v2.0 — Spreadsheet Redesign
 *
 * Responsibilities:
 *   - Manage workspace open/close state
 *   - Store dataset identity (datasetId, sessionId)
 *   - Store view controls (sort, filters, search) — these drive cache invalidation in Spreadsheet.jsx
 *   - Store column metadata from first API response
 *   - Store selection snapshot for AI context
 *   - Bridge evidence system (EvidenceInterpreter → renderInstructions)
 *   - Expose buildWorkspaceSummaryPayload() for /api/ask integration
 *
 * What it NO LONGER manages:
 *   - loadedRows array (now in Spreadsheet's WindowCache ref)
 *   - currentOffset / APPEND_DATA (now managed per-window in Spreadsheet)
 *   - pageSize (now FETCH_WINDOW constant in spreadsheet/constants.js)
 *
 * Data flow:
 *   WorkspaceContext (isOpen, datasetId, sort, search, filters, columns, totalRows)
 *     → Spreadsheet.jsx (owns cache, virtual range, column model, selection)
 *       → SpreadsheetGrid.jsx (renders visible rows)
 *
 * Future AI integration:
 *   AI agent will call buildWorkspaceSummaryPayload() to get context.
 *   AI agent will call setSort/setFilter/setSearch to drive view changes.
 *   AI agent will receive SpreadsheetState snapshot via buildStateSnapshot().
 */
import React, {
  createContext, useContext, useReducer, useCallback, useEffect, useRef,
} from 'react';
import {
  workspaceReducer,
  initialWorkspaceState,
  WorkspaceActions,
} from './WorkspaceReducer.js';
import { EventBus } from '../events/EventBus.js';
import { Events } from '../events/Events.js';
import { EvidenceRegistry } from '../evidence/EvidenceRegistry.js';
import { EvidenceInterpreter } from '../evidence/EvidenceInterpreter.js';
import { PlexisAPI } from '../api.js';
import { FETCH_WINDOW } from '../spreadsheet/constants.js';

const WorkspaceContext = createContext(null);

export function WorkspaceProvider({ children }) {
  const [state, dispatch] = useReducer(workspaceReducer, initialWorkspaceState);
  const stateRef = useRef(state);
  stateRef.current = state;

  // ── EventBus subscriptions ─────────────────────────────────────────────────

  useEffect(() => {
    // Evidence selected → interpret → apply render instructions
    const unsubEvidenceSelected = EventBus.on(
      Events.EVIDENCE_SELECTED,
      ({ evidenceId, evidence: inlineEvidence }) => {
        const ev = inlineEvidence || EvidenceRegistry.get(evidenceId);
        if (!ev) return;

        // Note: EvidenceInterpreter.interpret() receives empty rows/columns here
        // because the Spreadsheet now owns the data via WindowCache.
        // The interpreter should use row indices from the evidence object directly.
        const { columns } = stateRef.current;
        const instructions = EvidenceInterpreter.interpret(ev, [], columns);

        dispatch({
          type: WorkspaceActions.APPLY_RENDER_INSTRUCTIONS,
          instructions,
          activeEvidence: {
            id: ev.id,
            description: ev.description || ev.summary || '',
            type: ev.type,
          },
        });
      },
    );

    const unsubEvidenceCleared = EventBus.on(Events.EVIDENCE_CLEARED, () => {
      dispatch({ type: WorkspaceActions.CLEAR_HIGHLIGHTS });
    });

    const unsubOpen = EventBus.on(Events.WORKSPACE_OPENED, ({ datasetId, sessionId } = {}) => {
      dispatch({
        type: WorkspaceActions.OPEN,
        datasetId: datasetId || stateRef.current.datasetId,
        sessionId: sessionId || stateRef.current.sessionId,
      });
    });

    const unsubClose = EventBus.on(Events.WORKSPACE_CLOSED, () => {
      dispatch({ type: WorkspaceActions.CLOSE });
    });

    const refreshMetadataAfterTransform = ({ datasetId } = {}) => {
      const current = stateRef.current;
      if (current.isOpen && current.datasetId && current.datasetId === datasetId) {
        dispatch({ type: WorkspaceActions.SET_LOADING, isLoading: true });
      }
    };
    const unsubTransformApplied = EventBus.on(Events.TRANSFORM_APPLIED, refreshMetadataAfterTransform);
    const unsubTransformUndone = EventBus.on(Events.TRANSFORM_UNDONE, refreshMetadataAfterTransform);

    return () => {
      unsubEvidenceSelected();
      unsubEvidenceCleared();
      unsubOpen();
      unsubClose();
      unsubTransformApplied();
      unsubTransformUndone();
    };
  }, []);

  // ── Initial metadata fetch when workspace opens ────────────────────────────
  // Only fetches the FIRST window to get column names and totalRows.
  // Subsequent data fetching is handled by Spreadsheet.jsx's WindowCache.

  const fetchMetadata = useCallback(async () => {
    const { datasetId, sort, search, filters } = stateRef.current;
    if (!datasetId) return;

    dispatch({ type: WorkspaceActions.SET_LOADING, isLoading: true });

    try {
      const result = await PlexisAPI.getDatasetWindow(
        datasetId,
        0,
        FETCH_WINDOW,
        sort?.col,
        sort?.dir,
        search,
        filters[0]?.col,
        filters[0]?.val,
      );

      dispatch({
        type: WorkspaceActions.SET_DATA,
        rows: [],              // rows NOT stored here — WindowCache owns them
        columns: result.columns,
        allColumns: result.all_columns ?? result.columns,
        totalRows: result.total_rows,
        totalColumns: result.total_columns ?? result.columns?.length ?? 0,
        offset: 0,
        filename: result.filename,
      });
    } catch (err) {
      console.error('[WorkspaceContext] fetchMetadata error:', err);
      dispatch({ type: WorkspaceActions.SET_LOADING, isLoading: false });
    }
  }, []);

  // Auto-fetch metadata when workspace opens
  useEffect(() => {
    if (state.isOpen && state.datasetId && !state.isLoading && state.totalRows === 0) {
      fetchMetadata();
    }
  }, [state.isOpen, state.datasetId, fetchMetadata]);

  // Re-fetch metadata on sort/filter/search change
  useEffect(() => {
    if (state.isLoading && state.isOpen && state.datasetId) {
      fetchMetadata();
    }
  }, [state.sort, state.filters, state.search, state.isLoading, fetchMetadata]);

  // ── loadPage — kept for backward compat (evidence system may call it) ──────
  // In v2, this is a no-op: data loading is owned by Spreadsheet's WindowCache.
  const loadPage = useCallback(() => {
    // No-op: data fetching is now managed by Spreadsheet.jsx via WindowCache.
    // Left here to preserve any external callers.
  }, []);

  // ── Public API ─────────────────────────────────────────────────────────────

  const openWorkspace = useCallback((datasetId, sessionId) => {
    dispatch({ type: WorkspaceActions.OPEN, datasetId, sessionId });
    EventBus.emit(Events.WORKSPACE_OPENED, { datasetId, sessionId });
  }, []);

  const closeWorkspace = useCallback(() => {
    dispatch({ type: WorkspaceActions.CLOSE });
    EventBus.emit(Events.WORKSPACE_CLOSED);
  }, []);

  const toggleWorkspace = useCallback((datasetId, sessionId) => {
    if (stateRef.current.isOpen) {
      closeWorkspace();
    } else {
      openWorkspace(datasetId, sessionId);
    }
  }, [openWorkspace, closeWorkspace]);

  const setFilter = useCallback((filters) => {
    dispatch({ type: WorkspaceActions.SET_FILTERS, filters });
    EventBus.emit(Events.WORKSPACE_FILTER_CHANGED, { filters });
  }, []);

  const setSort = useCallback((sort) => {
    dispatch({ type: WorkspaceActions.SET_SORT, sort });
    EventBus.emit(Events.WORKSPACE_SORT_CHANGED, { sort });
  }, []);

  const setSearch = useCallback((search) => {
    dispatch({ type: WorkspaceActions.SET_SEARCH, search });
    EventBus.emit(Events.WORKSPACE_SEARCH_CHANGED, { search });
  }, []);

  const setSelection = useCallback((rows, columns = []) => {
    dispatch({ type: WorkspaceActions.SET_SELECTION, rows, columns });
    EventBus.emit(Events.WORKSPACE_SELECTION_CHANGED, { rows, columns });
  }, []);

  const clearHighlights = useCallback(() => {
    dispatch({ type: WorkspaceActions.CLEAR_HIGHLIGHTS });
    EventBus.emit(Events.EVIDENCE_CLEARED);
  }, []);

  const setDataset = useCallback((datasetId, sessionId) => {
    dispatch({ type: WorkspaceActions.SET_DATASET, datasetId, sessionId });
  }, []);

  /**
   * Build workspace state snapshot for /api/ask.
   * Phase 2 will expand this to include full SpreadsheetState from Spreadsheet.jsx.
   */
  const buildWorkspaceSummaryPayload = useCallback(() => {
    const { isOpen, totalRows, totalColumns, columns, filters, sort, search, selection, activeEvidence } =
      stateRef.current;
    return {
      isOpen,
      totalRows,
      totalColumns,
      columns,
      filters,
      sort,
      search,
      selection,
      activeEvidence,
    };
  }, []);

  const value = {
    // State
    ...state,
    // Actions
    openWorkspace,
    closeWorkspace,
    toggleWorkspace,
    loadPage,
    setFilter,
    setSort,
    setSearch,
    setSelection,
    clearHighlights,
    setDataset,
    buildWorkspaceSummaryPayload,
  };

  return (
    <WorkspaceContext.Provider value={value}>
      {children}
    </WorkspaceContext.Provider>
  );
}

export function useWorkspace() {
  const ctx = useContext(WorkspaceContext);
  if (!ctx) throw new Error('useWorkspace must be used within <WorkspaceProvider>');
  return ctx;
}
