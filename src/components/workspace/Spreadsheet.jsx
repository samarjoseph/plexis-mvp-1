/**
 * Spreadsheet — the main spreadsheet orchestrator component.
 *
 * Architecture:
 *   This component owns:
 *     - Window cache (WindowCache ref — NOT React state, no re-renders on cache hit)
 *     - Virtual render range (startRow, endRow — React state, triggers re-render)
 *     - Column model (colModel — React state: { name, dtype, width, hidden })
 *     - Selection state (activeCell, anchorCell, selectedRows, selectedCols)
 *     - Scroll handler (throttled, uses RAF)
 *
 *   The WorkspaceContext owns:
 *     - datasetId, isOpen, totalRows, totalColumns, columns (names)
 *     - sort, filters, search (view controls)
 *     - renderInstructions, activeEvidence (evidence system)
 *     - selection snapshot (for AI context)
 *
 *   Data flow:
 *     1. WorkspaceContext opens → provides datasetId + columns
 *     2. Spreadsheet builds colModel from columns
 *     3. User scrolls → VirtualEngine calculates visible range
 *     4. WindowCache.firstMissingWindow() → fetch via PlexisAPI
 *     5. Cache stores rows by index → SpreadsheetGrid reads via getRow(i)
 *     6. Re-render only when virtual range changes
 *
 *   Virtualization invariant:
 *     The scrollbar height = totalRows * ROW_HEIGHT (the full dataset).
 *     Only [startRow … endRow] rows exist in the DOM.
 *     Scrolling to row 9999 in a 10,000-row dataset works correctly.
 *
 * Phase 1 limitations (by design, to be added in later phases):
 *   - Horizontal virtualization: all visible columns rendered (sufficient for < 300 cols)
 *   - No column pinning (pinned: false in model, architecture prepared)
 *   - No formula evaluation
 *   - No AI Mode
 */

import React, {
  useRef, useState, useEffect, useCallback, useMemo,
  useLayoutEffect,
} from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { EyeOff, Eye, Copy } from 'lucide-react';
import { useWorkspace } from '../../context/WorkspaceContext.jsx';
import { PlexisAPI } from '../../api.js';
import { EventBus } from '../../events/EventBus.js';
import { Events } from '../../events/Events.js';
import { WindowCache } from '../../spreadsheet/WindowCache.js';
import {
  calculateVisibleRange,
  scrollTopForRow,
  totalGridHeight,
} from '../../spreadsheet/VirtualEngine.js';
import {
  ROW_HEIGHT, HEADER_HEIGHT, DEFAULT_COL_WIDTH, MIN_COL_WIDTH, FETCH_WINDOW,
} from '../../spreadsheet/constants.js';
import { buildStateSnapshot } from '../../models/SpreadsheetState.js';
import {
  SpreadsheetOperationEngine,
  buildSpreadsheetContext,
} from '../../spreadsheet/SpreadsheetOperationEngine.js';
import SpreadsheetGrid from './SpreadsheetGrid.jsx';
import SpreadsheetToolbar from './SpreadsheetToolbar.jsx';
import SpreadsheetStatusBar from './SpreadsheetStatusBar.jsx';
import SpreadsheetOperationPanel from './SpreadsheetOperationPanel.jsx';
import ConversationLoadingScreen, { SPREADSHEET_LOADING_STEPS } from '../dashboard/ConversationLoadingScreen';
import '../../styles/spreadsheet.css';

// ─── Helpers ─────────────────────────────────────────────────────────────────

/** Build normalized cell selection set: Set<"rowIndex:colIndex"> */
function buildCellSet(anchor, focus) {
  if (!anchor || !focus) return new Set();
  const minRow = Math.min(anchor.rowIndex, focus.rowIndex);
  const maxRow = Math.max(anchor.rowIndex, focus.rowIndex);
  const minCol = Math.min(anchor.colIndex, focus.colIndex);
  const maxCol = Math.max(anchor.colIndex, focus.colIndex);
  const set = new Set();
  for (let r = minRow; r <= maxRow; r++) {
    for (let c = minCol; c <= maxCol; c++) {
      set.add(`${r}:${c}`);
    }
  }
  return set;
}

function normalizeRange(a, b) {
  return {
    minRow: Math.min(a.rowIndex, b.rowIndex),
    maxRow: Math.max(a.rowIndex, b.rowIndex),
    minCol: Math.min(a.colIndex, b.colIndex),
    maxCol: Math.max(a.colIndex, b.colIndex),
  };
}

// ─── Component ────────────────────────────────────────────────────────────────

export default function Spreadsheet({ onClose, viewMode, onViewModeChange }) {
  const {
    datasetId, totalRows, totalColumns, columns: columnNames,
    sort, filters, search, renderInstructions, activeEvidence,
    isLoading: ctxLoading,
    setSort, setSearch, setFilter, setSelection, clearHighlights,
    loadPage,
    filename: ctxFilename,
  } = useWorkspace();

  // ── Refs (mutable, no re-render) ─────────────────────────────────────────
  const cacheRef          = useRef(new WindowCache());
  const scrollRef         = useRef(null);
  const isFetchingRef     = useRef(false);
  const lastScrollTopRef  = useRef(0);
  const rafRef            = useRef(null);
  const stableColsRef     = useRef([]);
  const activeCellRef     = useRef(null);
  const anchorCellRef     = useRef(null);
  const selectedRowsRef   = useRef(new Set());
  const hiddenColsRef     = useRef(new Set());
  const totalRowsRef      = useRef(totalRows);
  totalRowsRef.current    = totalRows;

  // ── React state (triggers re-render) ─────────────────────────────────────
  const [virtualRange, setVirtualRange] = useState({ startRow: 0, endRow: 0 });
  const [colModel,     setColModel]     = useState([]); // { name, dtype, width, hidden }
  const [hiddenCols,   setHiddenCols]   = useState(new Set());
  const [activeCell,   setActiveCell]   = useState(null); // { rowIndex, colIndex }
  const [anchorCell,   setAnchorCell]   = useState(null);
  const [selectedRows, setSelectedRows] = useState(new Set());
  const [selectedCols, setSelectedCols] = useState(new Set());
  const [highlightedRows, setHighlightedRows] = useState(new Map());
  const [focusModeEnabled, setFocusModeEnabled] = useState(false);
  const [contextMenu, setContextMenu] = useState(null);
  const [copyFlash,   setCopyFlash]   = useState(false);
  const [filename,    setFilename]    = useState('');
  const [searchResultCount, setSearchResultCount]  = useState(0);
  const [searchResultIndex, setSearchResultIndex]  = useState(0);
  const [searchResults,     setSearchResults]      = useState([]); // [{rowIndex}]
  const [renderTick, setRenderTick]   = useState(0); // bump to force grid re-render after cache fill
  const [isVisualLoading, setIsVisualLoading] = useState(Boolean(datasetId));
  const loadStartRef = useRef(Date.now());

  useEffect(() => {
    if (!datasetId) {
      setIsVisualLoading(false);
      return;
    }

    // Keep visual loading active if dataset is loading or has rows but initial window hasn't arrived
    const isWaitingForInitialRows = totalRows > 0 && !cacheRef.current.hasRow(0);
    const isDatasetMetadataLoading = ctxLoading || (totalRows === 0 && !ctxFilename && !filename);

    if (isWaitingForInitialRows || isDatasetMetadataLoading) {
      setIsVisualLoading(true);
      loadStartRef.current = Date.now();
      return;
    }

    const elapsed = Date.now() - loadStartRef.current;
    const remaining = Math.max(0, 2200 - elapsed);
    const timer = setTimeout(() => {
      setIsVisualLoading(false);
    }, remaining);
    return () => clearTimeout(timer);
  }, [ctxLoading, totalRows, datasetId, renderTick, ctxFilename, filename]);

  // ── AI / Operation state ──────────────────────────────────────────────────
  const [aiModeEnabled,     setAiModeEnabled]     = useState(false);
  const [isAiLoading,       setIsAiLoading]       = useState(false);
  const [operationHistory,  setOperationHistory]  = useState([]); // [{ id, type, params, result, explanation }]
  const [redoStack,         setRedoStack]         = useState([]); // ops truncated by undo
  const [currentOpIndex,    setCurrentOpIndex]    = useState(-1);
  const [activeOperation,   setActiveOperation]   = useState(null); // shown in operation panel
  const [opPanelVisible,    setOpPanelVisible]    = useState(false);
  const lastQueryRef = useRef(''); // track last agent query for chat sync

  // ── Find bar (Ctrl+F) state ────────────────────────────────────────────────
  const [findBarOpen,  setFindBarOpen]  = useState(false);
  const [findQuery,    setFindQuery]    = useState('');

  // Keep refs in sync
  activeCellRef.current  = activeCell;
  anchorCellRef.current  = anchorCell;
  selectedRowsRef.current = selectedRows;
  hiddenColsRef.current  = hiddenCols;
  stableColsRef.current  = colModel.filter(c => !hiddenCols.has(c.name));

  // ── Data fetching ─────────────────────────────────────────────────────────

  const fetchWindow = useCallback(async (offset) => {
    if (!datasetId) return;
    const cache = cacheRef.current;
    if (cache.isPending(offset)) return;

    // Cooldown: prevent spamming an offset that just failed
    if (cache.failedCooldowns?.get(offset) && Date.now() - cache.failedCooldowns.get(offset) < 4000) {
      return;
    }

    cache.markPending(offset);
    const requestedCacheVersion = cache.version;

    try {
      const result = await PlexisAPI.getDatasetWindow(
        datasetId,
        offset,
        FETCH_WINDOW,
        sort?.col,
        sort?.dir,
        search,
        filters[0]?.col,
        filters[0]?.val,
      );

      // Ignore a response that raced with apply/undo or a view invalidation.
      if (cacheRef.current.version !== requestedCacheVersion) return;

      cache.setWindow(offset, result.rows ?? []);
      if (result.filename) setFilename(result.filename);
      if (result.total_rows && totalRowsRef.current === 0) {
        totalRowsRef.current = result.total_rows;
      }
      if (cache.failedCooldowns) cache.failedCooldowns.delete(offset);

      // Trigger re-render so grid picks up newly cached rows
      setRenderTick(t => t + 1);
    } catch (err) {
      console.error('[Spreadsheet] fetchWindow error:', err);
      cacheRef.current.pendingOffsets?.delete(offset);
      if (!cacheRef.current.failedCooldowns) {
        cacheRef.current.failedCooldowns = new Map();
      }
      cacheRef.current.failedCooldowns.set(offset, Date.now());
    }
  }, [datasetId, sort, filters, search]);

  const maybeFetch = useCallback((startRow, endRow) => {
    if (!datasetId) return;
    const cache = cacheRef.current;
    const missing = cache.firstMissingWindow(startRow, endRow);
    if (missing !== null) {
      // Window 0 is always allowed even if totalRows is not yet known
      if (missing === 0 || totalRowsRef.current === 0 || missing < totalRowsRef.current) {
        fetchWindow(missing);
      }
    }
  }, [datasetId, fetchWindow]);

  // ── Derived ───────────────────────────────────────────────────────────────

  const visibleCols = useMemo(
    () => colModel.filter(c => !hiddenCols.has(c.name)),
    [colModel, hiddenCols],
  );

  const selectedCellSet = useMemo(
    () => buildCellSet(anchorCell, activeCell),
    [anchorCell, activeCell],
  );

  // ── Column model: built from context columns on first load ────────────────

  useEffect(() => {
    if (!columnNames?.length) return;
    setColModel(prev => {
      // Preserve widths/hidden state for columns that already exist
      const prevMap = new Map(prev.map(c => [c.name, c]));
      return columnNames.map(name => prevMap.get(name) ?? {
        name,
        dtype: '',       // populated when backend provides schema
        width: DEFAULT_COL_WIDTH,
        hidden: false,
        pinned: false,   // Phase 2+
      });
    });
  }, [columnNames]);

  // ── Cache invalidation on view-control change ─────────────────────────────

  useEffect(() => {
    cacheRef.current.invalidate();
    setVirtualRange({ startRow: 0, endRow: 0 });
    // Reset scroll to top
    if (scrollRef.current) {
      scrollRef.current.scrollTop = 0;
    }
    // Fetch the first window
    maybeFetch(0, FETCH_WINDOW - 1);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sort, filters, search]);

  // Transformations update the backend dataframe without changing the current
  // view controls. Drop cached windows so the grid re-reads the applied/undone
  // values from the same dataset version.
  useEffect(() => {
    const refreshRows = ({ datasetId: changedDatasetId } = {}) => {
      if (!datasetId || changedDatasetId !== datasetId) return;
      cacheRef.current.invalidate();
      setVirtualRange({ startRow: 0, endRow: 0 });
      if (scrollRef.current) scrollRef.current.scrollTop = 0;
      maybeFetch(0, FETCH_WINDOW - 1);
    };
    const offApplied = EventBus.on(Events.TRANSFORM_APPLIED, refreshRows);
    const offUndone = EventBus.on(Events.TRANSFORM_UNDONE, refreshRows);
    return () => { offApplied(); offUndone(); };
  }, [datasetId, maybeFetch]);

  // ── Evidence render instructions ──────────────────────────────────────────

  useEffect(() => {
    if (!renderInstructions) {
      setHighlightedRows(new Map());
      setFocusModeEnabled(false);
      return;
    }
    const map = new Map();
    // rowIndices are original DataFrame indices (_row_idx).
    // Translate to display indices via cache when sort/filter is active.
    (renderInstructions.highlights || []).forEach(({ rowIndices, color, pulse, columnNames: cols }) => {
      rowIndices.forEach(origIdx => {
        const displayIdx = cacheRef.current.findDisplayIndexByRowIdx(origIdx);
        const idx = displayIdx !== null ? displayIdx : origIdx;
        map.set(idx, { color, pulse, columnNames: cols });
      });
    });
    setHighlightedRows(map);
    setFocusModeEnabled(renderInstructions.focus?.enabled ?? false);

    // Navigate to first highlighted row (translate original -> display)
    const nav = renderInstructions.navigation;
    if (nav?.action !== 'none' && nav?.targetRowIndex >= 0) {
      const origTarget = nav.targetRowIndex;
      const displayTarget = cacheRef.current.findDisplayIndexByRowIdx(origTarget);
      const scrollTarget = displayTarget !== null ? displayTarget : origTarget;
      setTimeout(() => jumpToRow(scrollTarget), 150);
    }
  }, [renderInstructions]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Broadcast canonical history state to all chat cards ──────────────────
  // This is the SINGLE source of truth for canUndo / canRedo.
  // SpreadsheetResultCard instances subscribe and update their button state
  // reactively — they never rely on the frozen snapshot in the message object.
  useEffect(() => {
    EventBus.emit(Events.SPREADSHEET_HISTORY_CHANGED, {
      currentOpIndex,
      historyLength: operationHistory.length,
      redoStackLength: redoStack.length,
    });
  }, [operationHistory, currentOpIndex, redoStack]);

  // ── Initial fetch when workspace opens ────────────────────────────────────

  useEffect(() => {
    if (!datasetId) return;
    cacheRef.current.invalidate();
    maybeFetch(0, FETCH_WINDOW - 1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [datasetId]);

  // ── Push selection to WorkspaceContext ────────────────────────────────────

  useEffect(() => {
    setSelection(Array.from(selectedRows), Array.from(selectedCols));
  }, [selectedRows, selectedCols, setSelection]);


  // ── getRow — used by SpreadsheetGrid ─────────────────────────────────────

  const getRow = useCallback((rowIndex) => {
    return cacheRef.current.getRow(rowIndex);
  }, [renderTick]); // re-bind when cache fills so grid re-reads // eslint-disable-line react-hooks/exhaustive-deps

  // ── Scroll handler (RAF-throttled) ────────────────────────────────────────

  const handleScroll = useCallback(() => {
    if (rafRef.current) return; // already queued
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      const el = scrollRef.current;
      if (!el) return;

      const { scrollTop, clientHeight } = el;
      lastScrollTopRef.current = scrollTop;

      const { startRow, endRow, firstVisible } = calculateVisibleRange(
        scrollTop, clientHeight, totalRowsRef.current,
      );

      setVirtualRange(prev => {
        if (prev.startRow === startRow && prev.endRow === endRow) return prev;
        return { startRow, endRow };
      });

      // Fetch any missing windows in the visible range
      maybeFetch(startRow, endRow);
    });
  }, [maybeFetch]);

  // ── Jump to arbitrary row (used by evidence nav + search nav) ─────────────

  const jumpToRow = useCallback((rowIndex) => {
    const el = scrollRef.current;
    if (!el) return;

    const clamped = Math.max(0, Math.min(rowIndex, totalRowsRef.current - 1));
    const targetScrollTop = HEADER_HEIGHT + clamped * ROW_HEIGHT;
    el.scrollTop = Math.max(0, targetScrollTop - el.clientHeight / 2);

    // Ensure the window around the target row is cached
    const windowOffset = Math.floor(clamped / FETCH_WINDOW) * FETCH_WINDOW;
    fetchWindow(windowOffset);
  }, [fetchWindow]);

  // ── Column sort ───────────────────────────────────────────────────────────

  const handleHeaderClick = useCallback((colName, colIndex, e) => {
    if (e.shiftKey) {
      // Column selection
      setSelectedCols(prev => {
        const next = new Set(prev);
        if (next.has(colName)) next.delete(colName);
        else next.add(colName);
        return next;
      });
      setActiveCell(null);
      setAnchorCell(null);
      return;
    }
    if (e.button === 2) return; // right click
    const isCurrentCol = sort?.col === colName;
    const newDir = isCurrentCol && sort?.dir === 'asc' ? 'desc' : 'asc';
    setSort({ col: colName, dir: newDir });
  }, [sort, setSort]);

  const handleSortClear = useCallback(() => {
    setSort(null);
  }, [setSort]);

  const handleFiltersClear = useCallback(() => {
    setFilter([]);
  }, [setFilter]);

  // ── Column resize ─────────────────────────────────────────────────────────

  const handleResizeStart = useCallback((e, colName) => {
    e.preventDefault();
    e.stopPropagation();
    const th = e.currentTarget.parentElement;
    const startW = th.getBoundingClientRect().width;
    const startX = e.clientX;

    const onMove = (mv) => {
      const newW = Math.max(MIN_COL_WIDTH, startW + mv.clientX - startX);
      setColModel(prev => prev.map(c => c.name === colName ? { ...c, width: newW } : c));
    };
    const onUp = () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }, []);

  // ── Column hide/show ──────────────────────────────────────────────────────

  const handleHideColumn = useCallback((colName) => {
    setHiddenCols(prev => new Set([...prev, colName]));
    setContextMenu(null);
  }, []);

  const handleShowAllColumns = useCallback(() => {
    setHiddenCols(new Set());
    setContextMenu(null);
  }, []);

  // ── Cell selection ────────────────────────────────────────────────────────

  const handleCellClick = useCallback((rowIndex, colIndex, e) => {
    e.stopPropagation();
    setSelectedCols(new Set());

    if (e.shiftKey && anchorCellRef.current) {
      setActiveCell({ rowIndex, colIndex });
      const { minRow, maxRow } = normalizeRange(anchorCellRef.current, { rowIndex, colIndex });
      const rows = new Set();
      for (let r = minRow; r <= maxRow; r++) rows.add(r);
      setSelectedRows(rows);
    } else if (e.ctrlKey || e.metaKey) {
      setAnchorCell({ rowIndex, colIndex });
      setActiveCell({ rowIndex, colIndex });
      setSelectedRows(prev => {
        const next = new Set(prev);
        if (next.has(rowIndex)) next.delete(rowIndex); else next.add(rowIndex);
        return next;
      });
    } else {
      setAnchorCell({ rowIndex, colIndex });
      setActiveCell({ rowIndex, colIndex });
      setSelectedRows(new Set([rowIndex]));
    }
    scrollRef.current?.focus({ preventScroll: true });
  }, []);

  // ── Row number click → select entire row ─────────────────────────────────

  const handleRowNumClick = useCallback((rowIndex, e) => {
    e.stopPropagation();
    const vis = stableColsRef.current;
    setAnchorCell({ rowIndex, colIndex: 0 });
    setActiveCell({ rowIndex, colIndex: Math.max(0, vis.length - 1) });
    setSelectedCols(new Set());

    if (e.shiftKey && anchorCellRef.current) {
      const { minRow, maxRow } = normalizeRange(anchorCellRef.current, { rowIndex, colIndex: 0 });
      const rows = new Set();
      for (let r = minRow; r <= maxRow; r++) rows.add(r);
      setSelectedRows(rows);
    } else if (e.ctrlKey || e.metaKey) {
      setSelectedRows(prev => {
        const next = new Set(prev);
        if (next.has(rowIndex)) next.delete(rowIndex); else next.add(rowIndex);
        return next;
      });
    } else {
      setSelectedRows(new Set([rowIndex]));
    }
    scrollRef.current?.focus({ preventScroll: true });
  }, []);

  // ── Scroll active cell into view ──────────────────────────────────────────

  const scrollCellIntoView = useCallback((rowIndex, colIndex) => {
    const el = scrollRef.current;
    if (!el) return;

    // Vertical
    const targetScrollTop = scrollTopForRow(rowIndex, el.scrollTop, el.clientHeight);
    if (targetScrollTop !== null) el.scrollTop = targetScrollTop;

    // Horizontal — scroll the column header into view
    const vis = stableColsRef.current;
    const colName = vis[colIndex]?.name;
    if (colName) {
      const hdrEl = document.getElementById(`ssg-col-${colName}`);
      hdrEl?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }
  }, []);

  // ── Copy to clipboard ─────────────────────────────────────────────────────

  const handleCopy = useCallback(() => {
    const anchor = anchorCellRef.current;
    const focus  = activeCellRef.current;
    const vis    = stableColsRef.current;
    const cache  = cacheRef.current;

    let text = '';
    if (!anchor || !focus) {
      const rows = Array.from(selectedRowsRef.current).sort((a, b) => a - b);
      if (!rows.length) return;
      const header = vis.map(c => c.name).join('\t');
      const body = rows.map(r => {
        const row = cache.getRow(r);
        return row ? vis.map(c => row[c.name] ?? '').join('\t') : '';
      }).join('\n');
      text = `${header}\n${body}`;
    } else {
      const { minRow, maxRow, minCol, maxCol } = normalizeRange(anchor, focus);
      const lines = [];
      for (let r = minRow; r <= maxRow; r++) {
        const row = cache.getRow(r);
        const cells = [];
        for (let c = minCol; c <= maxCol; c++) {
          cells.push(row && vis[c] ? (row[vis[c].name] ?? '') : '');
        }
        lines.push(cells.join('\t'));
      }
      text = lines.join('\n');
    }

    navigator.clipboard.writeText(text).catch(console.error);
    setCopyFlash(true);
    setTimeout(() => setCopyFlash(false), 1500);
  }, []);

  // ── Keyboard navigation ────────────────────────────────────────────────────

  const handleKeyDown = useCallback((e) => {
    // Ctrl+F — open find bar
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f') {
      e.preventDefault();
      setFindBarOpen(true);
      return;
    }

    // Ctrl+Z — undo last operation
    if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === 'z') {
      e.preventDefault();
      handleOpUndo();
      return;
    }

    // Ctrl+Y or Ctrl+Shift+Z — redo
    if ((e.ctrlKey || e.metaKey) && (e.key.toLowerCase() === 'y' || (e.shiftKey && e.key.toLowerCase() === 'z'))) {
      e.preventDefault();
      handleOpRedo();
      return;
    }

    // Copy
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'c') {
      e.preventDefault();
      handleCopy();
      return;
    }

    const current = activeCellRef.current;
    if (!current) return;

    const vis = stableColsRef.current;
    const maxRow = Math.max(0, totalRowsRef.current - 1);
    const maxCol = Math.max(0, vis.length - 1);
    let { rowIndex: nextRow, colIndex: nextCol } = current;
    let handled = true;

    switch (e.key) {
      case 'ArrowUp':
        nextRow = e.ctrlKey || e.metaKey ? 0 : Math.max(0, nextRow - 1);
        break;
      case 'ArrowDown':
        nextRow = e.ctrlKey || e.metaKey ? maxRow : Math.min(maxRow, nextRow + 1);
        break;
      case 'ArrowLeft':
        nextCol = e.ctrlKey || e.metaKey ? 0 : Math.max(0, nextCol - 1);
        break;
      case 'ArrowRight':
        nextCol = e.ctrlKey || e.metaKey ? maxCol : Math.min(maxCol, nextCol + 1);
        break;
      case 'Home':
        nextCol = 0;
        if (e.ctrlKey || e.metaKey) nextRow = 0;
        break;
      case 'End':
        nextCol = maxCol;
        if (e.ctrlKey || e.metaKey) nextRow = maxRow;
        break;
      case 'Enter':
        nextRow = Math.min(maxRow, nextRow + 1);
        break;
      case 'Tab':
        if (e.shiftKey) nextCol = Math.max(0, nextCol - 1);
        else nextCol = Math.min(maxCol, nextCol + 1);
        break;
      case 'PageDown':
        nextRow = Math.min(maxRow, nextRow + 20);
        break;
      case 'PageUp':
        nextRow = Math.max(0, nextRow - 20);
        break;
      case 'Escape':
        setActiveCell(null);
        setAnchorCell(null);
        setSelectedRows(new Set());
        setSelectedCols(new Set());
        return;
      default:
        handled = false;
    }

    if (!handled) return;
    e.preventDefault();

    const next = { rowIndex: nextRow, colIndex: nextCol };
    setActiveCell(next);

    if (e.shiftKey && e.key.startsWith('Arrow')) {
      const { minRow, maxRow: mxR } = normalizeRange(anchorCellRef.current ?? next, next);
      const rows = new Set();
      for (let r = minRow; r <= mxR; r++) rows.add(r);
      setSelectedRows(rows);
    } else {
      setAnchorCell(next);
      setSelectedRows(new Set([nextRow]));
    }

    scrollCellIntoView(nextRow, nextCol);

    // Ensure the row is cached (fetches window if missing)
    maybeFetch(nextRow, Math.min(nextRow + FETCH_WINDOW, maxRow));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [handleCopy, scrollCellIntoView, maybeFetch]);

  // ── Search result navigation ──────────────────────────────────────────────

  const handleSearchNav = useCallback((direction) => {
    if (!searchResults.length) return;
    const newIndex = direction === 'next'
      ? (searchResultIndex + 1) % searchResults.length
      : (searchResultIndex - 1 + searchResults.length) % searchResults.length;
    setSearchResultIndex(newIndex);
    const targetRow = searchResults[newIndex]?.rowIndex ?? 0;
    jumpToRow(targetRow);
  }, [searchResults, searchResultIndex, jumpToRow]);

  // When search results come in (via total_rows being smaller), navigate to first
  useEffect(() => {
    if (search && totalRows < totalRowsRef.current) {
      setSearchResultCount(totalRows);
    } else if (!search) {
      setSearchResultCount(0);
      setSearchResultIndex(0);
      setSearchResults([]);
    }
  }, [search, totalRows]);

  // ── Context menu ──────────────────────────────────────────────────────────

  const handleContextMenu = useCallback((e, rowIndex, rowData) => {
    e.preventDefault();
    setContextMenu({ x: e.clientX, y: e.clientY, type: 'row', rowIndex, rowData });
  }, []);

  const handleHeaderContextMenu = useCallback((e, colName) => {
    e.preventDefault();
    e.stopPropagation();
    setContextMenu({ x: e.clientX, y: e.clientY, type: 'col', colName });
  }, []);

  const handleExplainRow = useCallback(() => {
    if (!contextMenu?.rowData) return;
    EventBus.emit(Events.WORKSPACE_ACTION_REQUESTED, {
      type: 'explain_row',
      row_indices: [contextMenu.rowIndex],
      row_data: contextMenu.rowData,
      column_names: visibleCols.map(c => c.name),
    });
    setContextMenu(null);
  }, [contextMenu, visibleCols]);

  const handleAskAboutSelection = useCallback(() => {
    const rows = Array.from(selectedRows);
    if (!rows.length) return;
    EventBus.emit(Events.WORKSPACE_ACTION_REQUESTED, {
      type: 'ask_about_rows',
      row_indices: rows,
      column_names: visibleCols.map(c => c.name),
    });
    setContextMenu(null);
  }, [selectedRows, visibleCols]);

  // ── Initial virtual range calculation (after mount + totalRows known) ──────

  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el || totalRows === 0) return;
    const { startRow, endRow } = calculateVisibleRange(
      el.scrollTop, el.clientHeight, totalRows,
    );
    setVirtualRange({ startRow, endRow });
    maybeFetch(startRow, endRow);
  }, [totalRows, maybeFetch]);

  // Keep visible viewport populated even before the user scrolls
  useEffect(() => {
    if (!datasetId) return;
    maybeFetch(virtualRange.startRow, virtualRange.endRow);
  }, [virtualRange.startRow, virtualRange.endRow, renderTick, datasetId, maybeFetch]);

  // Viewport resize observer: re-calculate visible range and fetch if height changes
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      if (totalRowsRef.current === 0) return;
      const { startRow, endRow } = calculateVisibleRange(
        el.scrollTop, el.clientHeight, totalRowsRef.current,
      );
      setVirtualRange(prev => {
        if (prev.startRow === startRow && prev.endRow === endRow) return prev;
        return { startRow, endRow };
      });
      maybeFetch(startRow, endRow);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [maybeFetch]);

  // ── Register WorkspaceNavigator scroll callback ────────────────────────────

  useEffect(() => {
    // Dynamic import to avoid circular dependency
    import('../../workspace/WorkspaceNavigator.js').then(({ WorkspaceNavigator }) => {
      WorkspaceNavigator.setScrollCallback((rowIndex) => jumpToRow(rowIndex));
      return () => WorkspaceNavigator.setScrollCallback(null);
    });
  }, [jumpToRow]);

  // ── Listen for card actions from main chat (Locate/Undo/Redo on chat cards) ─


  // ── AI / Ask Agent ────────────────────────────────────────────────────────

  const engineRef = useRef(null);
  useEffect(() => {
    if (!datasetId || !columnNames?.length) return;
    engineRef.current = new SpreadsheetOperationEngine(
      {
        setSort,
        setFilter,
        setSearch,
        setHighlightedRows,
        setSelectedRows,
        setFocusMode: setFocusModeEnabled,
        jumpToRow,
        clearHighlights,
        onUndo: () => handleOpUndo(),
      },
      datasetId,
      columnNames,
    );
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [datasetId, columnNames?.join(','), setSort, setFilter, setSearch, jumpToRow, clearHighlights]);

  const handleAskAgent = useCallback(async (query) => {
    if (!query.trim() || isAiLoading) return;
    setIsAiLoading(true);
    lastQueryRef.current = query;

    const context = buildSpreadsheetContext({
      datasetId,
      filename,
      totalRows,
      totalColumns,
      columnNames,
      sort,
      filters,
      search,
      activeCell,
      selectedRows,
      selectedCols,
      highlightedRows,
      operationHistory,
      currentOpIndex,
      viewportStart: virtualRange.startRow,
      viewportEnd: virtualRange.endRow,
    });

    EventBus.emit(Events.SPREADSHEET_AGENT_QUERY, { query, spreadsheetContext: context });

    try {
      const resp = await PlexisAPI.interpretSpreadsheetQuery({
        dataset_id: datasetId,
        query,
        spreadsheet_context: context,
      });

      // Guard: if backend returns a non-operation failure, show error card
      if (resp.success === false && !resp.operation) {
        const errEntry = {
          id: `op-${Date.now()}`,
          type: 'ERROR',
          params: {},
          result: null,
          explanation: resp.explanation || '',
          error: resp.error || 'AI model unavailable.',
          source: resp.source || null,
        };
        setActiveOperation(errEntry);
        setOpPanelVisible(true);
        // Emit chat sync for error
        EventBus.emit(Events.SPREADSHEET_AGENT_CHAT_MESSAGE, {
          query,
          answer: resp.error || 'AI model unavailable.',
          isError: true,
        });
        return;
      }

      const op = resp.operation;
      if (!op) {
        const explanation = resp.explanation || "I couldn't understand that spreadsheet request. Try rephrasing.";

        // Detect conversational fallback: LLM said this is a general question
        // not a spreadsheet operation — route through the normal Plexis chat.
        const isConversationalFallback = (
          resp.confidence === 0 &&
          (
            explanation.toLowerCase().includes('general question') ||
            explanation.toLowerCase().includes('main chat') ||
            explanation.toLowerCase().includes('rephrase')
          )
        );

        if (isConversationalFallback) {
          // Fire the chat-routing event so Dashboard sends it to the normal chat
          EventBus.emit(Events.WORKSPACE_ACTION_REQUESTED, {
            type: 'ask_chat',
            prefilledQuestion: query,
          });
          return;
        }

        // Non-conversational null operation: genuine parsing failure
        const errEntry = {
          id: `op-${Date.now()}`,
          type: 'UNKNOWN',
          params: {},
          result: null,
          explanation,
          error: explanation,
          source: resp.source || null,
        };
        setActiveOperation(errEntry);
        setOpPanelVisible(true);
        // Chat sync for unknown op
        EventBus.emit(Events.SPREADSHEET_AGENT_CHAT_MESSAGE, {
          query,
          answer: explanation,
          isError: false,
        });
        return;
      }

      EventBus.emit(Events.SPREADSHEET_OPERATION, { operation: op, explanation: resp.explanation });

      // Execute via deterministic engine
      const engine = engineRef.current;
      if (!engine) throw new Error('Operation engine not ready.');

      const result = await engine.execute(op);

      const histEntry = {
        id: `op-${Date.now()}`,
        type: op.type,
        params: op,
        result,
        explanation: resp.explanation || '',
        answer: resp.explanation || '',
        query,
        error: result.success ? null : result.error,
        source: resp.source || null,   // 'llm' | 'heuristic' | null
      };

      if (result.success) {
        EventBus.emit(Events.SPREADSHEET_OP_COMPLETE, { operation: op, result });
        // New operation:
        //   1. Clear redo stack (any future redos are destroyed)
        //   2. Trim history to [0..currentOpIndex] — drops stale redo-branch entries
        //   3. Append the new op
        //   4. Advance pointer to the new entry
        setRedoStack([]);
        setOperationHistory(prev => {
          const trimmed = prev.slice(0, currentOpIndex + 1);
          return [...trimmed, histEntry];
        });
        setCurrentOpIndex(prev => {
          // After trim, new index = trimmed.length = currentOpIndex + 1
          return currentOpIndex + 1;
        });
        setActiveOperation(histEntry);
        setOpPanelVisible(true);

        // Sync the interaction to main chat — include full operation data
        // so the chat card can render a polished result and call Locate/Undo/Redo
        const chatMessage = resp.explanation || result.resultSummary || 'Done.';
        EventBus.emit(Events.SPREADSHEET_AGENT_CHAT_MESSAGE, {
          query,
          answer: chatMessage,
          operation: op,
          result,
          histEntry,
          operationId: histEntry.id,
          source: resp.source || null,
          isError: false,
        });
      } else {
        EventBus.emit(Events.SPREADSHEET_OP_ERROR, { operation: op, error: result.error });
        setActiveOperation(histEntry);
        setOpPanelVisible(true);
        EventBus.emit(Events.SPREADSHEET_AGENT_CHAT_MESSAGE, {
          query,
          answer: result.error || 'Operation failed.',
          isError: true,
        });
      }
    } catch (err) {
      console.error('[Spreadsheet] Ask Agent error:', err);
      const errEntry = { type: 'ERROR', params: {}, result: null, error: err.message, explanation: '' };
      setActiveOperation(errEntry);
      setOpPanelVisible(true);
      EventBus.emit(Events.SPREADSHEET_AGENT_CHAT_MESSAGE, {
        query,
        answer: "I couldn't reach the AI model. Please try again.",
        isError: true,
      });
    } finally {
      setIsAiLoading(false);
    }
  }, [
    isAiLoading, datasetId, filename, totalRows, totalColumns, columnNames,
    sort, filters, search, activeCell, selectedRows, selectedCols,
    highlightedRows, operationHistory, currentOpIndex,
    virtualRange.startRow, virtualRange.endRow,
  ]);


  const handleToggleAiMode = useCallback(() => {
    setAiModeEnabled(prev => {
      EventBus.emit(Events.AI_MODE_TOGGLED, { enabled: !prev });
      return !prev;
    });
  }, []);

  function handleOpUndo() {
    if (currentOpIndex < 0) return;
    // Push current op to redo stack before clearing
    const currentOp = operationHistory[currentOpIndex];
    if (currentOp) setRedoStack(prev => [currentOp, ...prev]);
    // Clear current operation effects
    clearHighlights();
    setHighlightedRows(new Map());
    setFocusModeEnabled(false);
    setCurrentOpIndex(prev => prev - 1);
    const prevOp = operationHistory[currentOpIndex - 1];
    if (prevOp) setActiveOperation(prevOp);
    else { setOpPanelVisible(false); setActiveOperation(null); }
  }

  function handleOpRedo() {
    if (redoStack.length === 0) return;
    const [nextOp, ...rest] = redoStack;
    setRedoStack(rest);
    // Re-apply the operation effects
    if (nextOp?.result?.rowIndices?.length > 0) {
      const map = new Map();
      const col = nextOp.result.resultColumn || nextOp.params?.column || null;
      nextOp.result.rowIndices.forEach(i => map.set(i, {
        color: 'purple',
        pulse: nextOp.result.rowIndices.length === 1,
        columnNames: col ? [col] : [],
      }));
      setHighlightedRows(map);
      requestAnimationFrame(() => jumpToRow(nextOp.result.rowIndices[0]));
    }
    setOperationHistory(prev => [...prev, nextOp]);
    setCurrentOpIndex(prev => prev + 1);
    setActiveOperation(nextOp);
    setOpPanelVisible(true);
  }

  // ── Locate: translate original row indices → current display positions ─────
  //
  // Evidence rowIndices contain ORIGINAL DataFrame indices (_row_idx).
  // After sort/filter, the same row may appear at a different display position.
  // We must scan the cache for the matching _row_idx to get the current
  // display index, then scroll there.
  //
  // Strategy:
  //   1. Try to find each originalIdx in the cache via findDisplayIndexByRowIdx()
  //   2. If not in cache: fetch the window containing that area, wait, then retry
  //   3. Highlight using display indices in the highlightedRows Map
  //   4. Scroll to the first found display index
  //
  // Falls back gracefully: if no display index found (no sort/filter info in cache),
  // treats originalIdx as display idx (correct behavior with no sort/filter active).

  const locateByOriginalIndices = useCallback((originalIndices, col) => {
    if (!originalIndices?.length) return;
    const cache = cacheRef.current;

    // Try to resolve original indices → display indices from current cache
    const resolved = [];
    const missing = [];
    originalIndices.forEach(origIdx => {
      // Check if any cached row has _row_idx === origIdx
      const displayIdx = cache.findDisplayIndexByRowIdx(origIdx);
      if (displayIdx !== null) {
        resolved.push({ origIdx, displayIdx });
      } else {
        missing.push(origIdx);
      }
    });

    const applyHighlight = (displayIndices, firstDisplayIdx) => {
      const map = new Map();
      displayIndices.forEach(di => map.set(di, {
        color: 'purple',
        pulse: displayIndices.length === 1,
        columnNames: col ? [col] : [],
      }));
      setHighlightedRows(map);
      requestAnimationFrame(() => jumpToRow(firstDisplayIdx));
    };

    if (resolved.length > 0 && missing.length === 0) {
      // All in cache — apply immediately
      const displayIndices = resolved.map(r => r.displayIdx);
      applyHighlight(displayIndices, displayIndices[0]);
      return;
    }

    if (resolved.length === 0 && missing.length > 0) {
      // None in cache — no sort/filter active, or rows haven't been loaded yet
      // Fallback: treat original indices as display indices (correct when no sort/filter)
      // Also trigger a fetch of the window containing the first target row
      const firstOrig = missing[0];
      const windowOffset = Math.floor(firstOrig / FETCH_WINDOW) * FETCH_WINDOW;
      fetchWindow(windowOffset);

      // Apply highlight with original indices as display indices (best effort)
      // The highlight will appear correct if no sort/filter is active
      applyHighlight(missing.map(i => i), missing[0]);
      return;
    }

    // Partial: some resolved, some not — apply what we have
    const displayIndices = resolved.map(r => r.displayIdx);
    applyHighlight(displayIndices, displayIndices[0]);
  }, [jumpToRow, fetchWindow]);

  function handleLocate() {
    const op = activeOperation;
    if (!op?.result?.rowIndices?.length) return;
    const col = op.result.resultColumn || op.params?.column || null;
    locateByOriginalIndices(op.result.rowIndices, col);
  }


  // ── Listen for card actions from main chat (Locate/Undo/Redo on chat cards) ─
  // Placed after handler defs so closures capture current callbacks.
  useEffect(() => {
    const unsub = EventBus.on(Events.SPREADSHEET_CARD_ACTION, ({ action, operationId }) => {
      if (action === 'locate') {
        // Look up the specific operation by ID — NOT activeOperation which may differ
        const targetOp = operationId
          ? operationHistory.find(op => op.id === operationId)
          : activeOperation;
        const locateTarget = targetOp || activeOperation;
        if (!locateTarget?.result?.rowIndices?.length) return;
        const col = locateTarget.result.resultColumn || locateTarget.params?.column || null;
        setActiveOperation(locateTarget);
        // Use _row_idx-aware locate
        locateByOriginalIndices(locateTarget.result.rowIndices, col);
      } else if (action === 'undo') {
        handleOpUndo();
      } else if (action === 'redo') {
        handleOpRedo();
      }
    });
    return unsub;
  }, [handleOpUndo, handleOpRedo, operationHistory, activeOperation, jumpToRow, locateByOriginalIndices]);


  const handleOpPrevious = useCallback(() => {
    if (currentOpIndex <= 0) return;
    const idx = currentOpIndex - 1;
    setCurrentOpIndex(idx);
    setActiveOperation(operationHistory[idx] || null);
  }, [currentOpIndex, operationHistory]);


  // ── Ctrl+F find bar ───────────────────────────────────────────────────────

  const handleFindChange = useCallback((val) => {
    setFindQuery(val);
    setSearch(val);
  }, [setSearch]);

  const handleFindNav = useCallback((dir) => {
    handleSearchNav(dir);
  }, [handleSearchNav]);

  // ── Keyboard: Ctrl+F to open find bar, Ctrl+Z undo, Ctrl+Y redo ──────────

  // ─── Render ───────────────────────────────────────────────────────────────

  return (
    <div className="ssg-panel" id="dataset-workspace-panel">
      {/* Futuristic AI Spreadsheet Loader */}
      <AnimatePresence>
        {isVisualLoading && (
          <ConversationLoadingScreen
            steps={SPREADSHEET_LOADING_STEPS}
            className="ssg-conv-loading-screen"
          />
        )}
      </AnimatePresence>

      {/* Toolbar + Find bar */}
      <SpreadsheetToolbar
        totalRows={totalRows}
        totalColumns={totalColumns}
        filename={filename || ctxFilename || ''}
        isLoading={ctxLoading}
        activeCell={activeCell}
        visibleCols={visibleCols}
        columnNames={visibleCols.map(c => c.name)}
        sort={sort}
        filters={filters}
        search={search}
        searchResultCount={searchResultCount}
        searchResultIndex={searchResultIndex}
        selectedRows={selectedRows}
        selectedCols={selectedCols}
        copyFlash={copyFlash}
        activeEvidence={activeEvidence}
        hiddenColCount={hiddenCols.size}
        onAskAgent={handleAskAgent}
        isAiLoading={isAiLoading}
        aiModeEnabled={aiModeEnabled}
        onToggleAiMode={handleToggleAiMode}
        viewMode={viewMode || 'side_by_side'}
        onViewModeChange={onViewModeChange}
        findBarOpen={findBarOpen}
        onFindBarOpen={() => setFindBarOpen(true)}
        onFindBarClose={() => { setFindBarOpen(false); setFindQuery(''); setSearch(''); }}
        findQuery={findQuery}
        onFindChange={handleFindChange}
        onFindNav={handleFindNav}
        onSortClear={handleSortClear}
        onFiltersClear={handleFiltersClear}
        onCopy={handleCopy}
        onShowAllColumns={handleShowAllColumns}
        onClearHighlights={clearHighlights}
        onClose={onClose}
      />

      {/* Grid */}
      {totalRows === 0 && !isVisualLoading ? (
        <div className="ssg-empty-state">
          <span>No data to display.</span>
        </div>
      ) : (
        <SpreadsheetGrid
          totalRows={totalRows}
          visibleCols={visibleCols}
          getRow={getRow}
          currentOffset={0}
          startRow={virtualRange.startRow}
          endRow={virtualRange.endRow}
          scrollRef={scrollRef}
          onScroll={handleScroll}
          onKeyDown={handleKeyDown}
          sort={sort}
          selectedCols={selectedCols}
          activeCell={activeCell}
          anchorCell={anchorCell}
          selectedCellSet={selectedCellSet}
          selectedRows={selectedRows}
          highlightedRows={highlightedRows}
          focusModeEnabled={focusModeEnabled}
          onCellClick={handleCellClick}
          onRowNumClick={handleRowNumClick}
          onContextMenu={handleContextMenu}
          onHeaderClick={handleHeaderClick}
          onHeaderContextMenu={handleHeaderContextMenu}
          onResizeStart={handleResizeStart}
        />
      )}

      {/* Loading overlay */}
      {ctxLoading && totalRows > 0 && (
        <div className="ssg-loading-overlay">
          <RefreshCwIcon />
          Loading…
        </div>
      )}

      {/* Context Menu */}
      <AnimatePresence>
        {contextMenu && (
          <>
            <div className="ssg-ctx-overlay" onClick={() => setContextMenu(null)} />
            <motion.div
              className="ssg-ctx-menu"
              style={{ top: contextMenu.y, left: contextMenu.x }}
              initial={{ opacity: 0, scale: 0.95, y: -4 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.95 }}
              transition={{ duration: 0.1 }}
            >
              {contextMenu.type === 'row' && (
                <>
                  <button type="button" className="ssg-ctx-item" onClick={handleExplainRow}>
                    🔍 Explain this row
                  </button>
                  {selectedRows.size > 1 && (
                    <button type="button" className="ssg-ctx-item" onClick={handleAskAboutSelection}>
                      💬 Ask about {selectedRows.size} selected rows
                    </button>
                  )}
                  <div className="ssg-ctx-sep" />
                  <button type="button" className="ssg-ctx-item" onClick={handleCopy}>
                    <Copy size={13} /> Copy row
                  </button>
                </>
              )}
              {contextMenu.type === 'col' && (
                <>
                  <div className="ssg-ctx-item ssg-ctx-item--label">
                    Column: {contextMenu.colName}
                  </div>
                  <div className="ssg-ctx-sep" />
                  <button
                    type="button"
                    className="ssg-ctx-item"
                    onClick={() => handleHideColumn(contextMenu.colName)}
                  >
                    <EyeOff size={13} /> Hide column
                  </button>
                  {hiddenCols.size > 0 && (
                    <button type="button" className="ssg-ctx-item" onClick={handleShowAllColumns}>
                      <Eye size={13} /> Show all columns
                    </button>
                  )}
                </>
              )}
            </motion.div>
          </>
        )}
      </AnimatePresence>

      {/* Operation Panel — shown after AI operation */}
      <AnimatePresence>
        {opPanelVisible && activeOperation && (
          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 8 }}
            transition={{ duration: 0.18 }}
          >
            <SpreadsheetOperationPanel
              operation={activeOperation}
              operationIndex={currentOpIndex}
              operationTotal={operationHistory.length}
              onClose={() => setOpPanelVisible(false)}
              onLocate={activeOperation?.result?.rowIndices?.length > 0 ? handleLocate : null}
              onUndo={handleOpUndo}
              onRedo={handleOpRedo}
              canUndo={currentOpIndex >= 0}
              canRedo={redoStack.length > 0}
              onRetry={null}
            />
          </motion.div>
        )}
      </AnimatePresence>

      {/* Status Bar */}
      <SpreadsheetStatusBar
        totalRows={totalRows}
        totalColumns={totalColumns}
        activeCell={activeCell}
        selectedRows={selectedRows}
        selectedCols={selectedCols}
        highlightedRows={highlightedRows}
        isAiLoading={isAiLoading}
        operationLabel={
          operationHistory.length > 0
            ? `Operation ${currentOpIndex + 1} of ${operationHistory.length}`
            : null
        }
        onOperationClick={() => setOpPanelVisible(v => !v)}
        isReady={!ctxLoading && totalRows > 0}
      />
    </div>
  );
}

// Small inline icon to avoid import pollution
function RefreshCwIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      style={{ animation: 'ssg-spin 0.8s linear infinite', flexShrink: 0 }}>
      <polyline points="23 4 23 10 17 10" />
      <polyline points="1 20 1 14 7 14" />
      <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
    </svg>
  );
}
