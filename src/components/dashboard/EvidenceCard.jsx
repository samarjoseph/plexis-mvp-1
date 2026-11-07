/**
 * EvidenceCard — inline evidence preview rendered inside MessageBubbleV2.
 *
 * Displayed when an AI message contains a valid message.evidence object
 * representing an investigation finding (outliers, missing values, duplicates, etc.).
 * Shows type badge, row count, key/value preview, and contextual actions.
 *
 * CONTEXTUAL VISIBILITY RULES:
 *   - [Locate] is rendered ONLY when a valid, supported location target exists:
 *     row_indices, locator filter, or source row identifiers.
 *   - [Explain] is rendered ONLY when the finding is an anomaly or diagnostic
 *     (outliers, missing, duplicates) or explicitly flagged with allow_explain.
 *   - Plain summaries, hollow badges, or records with 0 rows and no location target
 *     DO NOT render empty action containers or dead buttons.
 *
 * Actions emit EventBus events:
 *   [Locate]  → EVIDENCE_SELECTED + NAVIGATE_TO_EVIDENCE (opens workspace, scrolls to rows)
 *   [Explain] → WORKSPACE_ACTION_REQUESTED (fires follow-up question to /api/ask)
 */
import { useState, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { MapPin, Zap, ChevronDown, ChevronUp, AlertTriangle, Layers } from 'lucide-react';
import { EventBus } from '../../events/EventBus.js';
import { Events } from '../../events/Events.js';
import { EvidenceRegistry } from '../../evidence/EvidenceRegistry.js';

const TYPE_LABELS = {
  rows: 'Rows',
  top_n: 'Top Rows',
  bottom_n: 'Bottom Rows',
  outliers: 'Outliers',
  missing: 'Missing Values',
  duplicates: 'Duplicates',
  filtered: 'Filtered Rows',
  correlation: 'Correlation',
  aggregate: 'Aggregate',
  group: 'Group',
  sample: 'Sample',
};

const TYPE_COLORS = {
  top_n: '#f59e0b',
  bottom_n: '#ef4444',
  outliers: '#ef4444',
  missing: '#f97316',
  duplicates: '#8b5cf6',
  filtered: '#3b82f6',
  correlation: '#06b6d4',
  rows: '#f59e0b',
  default: '#6366f1',
};

export default function EvidenceCard({ evidence }) {
  const [expanded, setExpanded] = useState(false);

  // Normalize single-item array or malformed evidence
  const normalized = Array.isArray(evidence) ? (evidence.length === 1 ? evidence[0] : null) : evidence;
  const evidenceId = normalized?.id;

  // Register in EvidenceRegistry on first render if ID present
  useEffect(() => {
    if (evidenceId && normalized) {
      EvidenceRegistry.register(normalized);
    }
  }, [evidenceId, normalized]);

  if (!normalized || typeof normalized !== 'object') return null;

  if (normalized.type === 'collection') {
    return <EvidenceCollectionCard evidence={normalized} />;
  }

  const color = TYPE_COLORS[normalized.type] || TYPE_COLORS.default;
  const typeLabel = TYPE_LABELS[normalized.type] || normalized.type || 'Evidence';
  const previewRows = Array.isArray(normalized.preview_rows) ? normalized.preview_rows : [];
  const isStable = normalized.locator?.is_stable !== false;

  const rowCount = normalized.metadata?.outlier_count
    ?? normalized.metadata?.duplicate_count
    ?? normalized.metadata?.missing_count
    ?? normalized.metadata?.count
    ?? normalized.metadata?.n
    ?? (previewRows.length > 0 ? previewRows.length : 0);

  // Contextual action availability checks
  const canLocate = Boolean(
    (normalized.locator?.row_indices && normalized.locator.row_indices.length > 0) ||
    normalized.locator?.filter ||
    (normalized.row_indices && normalized.row_indices.length > 0) ||
    (normalized.metadata?.row_indices && normalized.metadata.row_indices.length > 0) ||
    (previewRows.length > 0 && previewRows.some(r => r && (r.source_row_number != null || r._row_number != null || r.dataframe_index != null)))
  );

  const canExplain = Boolean(
    normalized.allow_explain === true ||
    normalized.metadata?.allow_explain === true ||
    ['outliers', 'missing', 'duplicates', 'anomaly', 'drift'].includes(normalized.type)
  );

  const hasActions = canLocate || canExplain;

  // If there are no preview rows, 0 row count, no description, and no actions, don't render a hollow card
  if (previewRows.length === 0 && rowCount === 0 && !normalized.description && !hasActions) {
    return null;
  }

  const handleLocate = () => {
    const rawGuest = localStorage.getItem('plexis_guest_session');
    if (rawGuest) {
      try {
        const parsed = JSON.parse(rawGuest);
        if (parsed?.guest_token) {
          EventBus.emit(Events.WORKSPACE_ACTION_REQUESTED, { type: 'locked_feature', feature: 'spreadsheet' });
          return;
        }
      } catch (_) {}
    }
    EventBus.emit(Events.EVIDENCE_SELECTED, { evidenceId: normalized.id, evidence: normalized });
    EventBus.emit(Events.WORKSPACE_OPENED, {});
    EventBus.emit(Events.NAVIGATE_TO_EVIDENCE, { evidenceId: normalized.id });
  };

  const handleExplain = () => {
    EventBus.emit(Events.WORKSPACE_ACTION_REQUESTED, {
      type: 'explain_evidence',
      evidenceId: normalized.id,
      description: normalized.description,
      evidence: {
        id: normalized.id,
        type: normalized.type,
        description: normalized.description,
        metadata: normalized.metadata || {},
        preview_rows: (normalized.preview_rows || []).slice(0, 5),
        locator: normalized.locator || null,
      },
      prefilledQuestion: 'Explain this result.',
    });
  };

  return (
    <motion.div
      className="ev-card"
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
      style={{ '--ev-color': color }}
    >
      {/* Header */}
      <div className="ev-card-header">
        <div className="ev-card-badge" style={{ background: `${color}22`, color }}>
          <span className="ev-card-badge-dot" style={{ background: color }} />
          {typeLabel}
        </div>
        {rowCount > 0 && (
          <span className="ev-card-count">{rowCount.toLocaleString()} row{rowCount !== 1 ? 's' : ''}</span>
        )}
        {!isStable && (
          <span className="ev-card-unstable">
            <AlertTriangle size={11} /> Positional
          </span>
        )}
        {previewRows.length > 0 && (
          <button
            type="button"
            className="ev-card-expand-btn"
            onClick={() => setExpanded(v => !v)}
            aria-label={expanded ? 'Collapse preview' : 'Expand preview'}
          >
            {expanded ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
          </button>
        )}
      </div>

      {/* Description */}
      {normalized.description && (
        <div className="ev-card-desc">{normalized.description}</div>
      )}

      {/* Preview rows */}
      <AnimatePresence>
        {expanded && previewRows.length > 0 && (
          <motion.div
            className="ev-card-preview"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
          >
            {previewRows.slice(0, 3).map((row, i) => (
              <div key={i} className="ev-card-row">
                {Object.entries(row || {}).slice(0, 4).map(([k, v]) => (
                  <div key={k} className="ev-card-cell">
                    <span className="ev-card-cell-key">{k}</span>
                    <span className="ev-card-cell-val">{v == null ? '—' : String(v)}</span>
                  </div>
                ))}
              </div>
            ))}
          </motion.div>
        )}
      </AnimatePresence>

      {/* Contextual Actions — rendered ONLY when at least one action is valid and supported */}
      {hasActions && (
        <div className="ev-card-actions">
          {canLocate && (
            <motion.button
              type="button"
              className="ev-card-action ev-card-action--primary"
              onClick={handleLocate}
              whileHover={{ scale: 1.02 }}
              whileTap={{ scale: 0.97 }}
            >
              <MapPin size={12} />
              Locate
            </motion.button>
          )}
          {canExplain && (
            <motion.button
              type="button"
              className="ev-card-action ev-card-action--secondary"
              onClick={handleExplain}
              whileHover={{ scale: 1.02 }}
              whileTap={{ scale: 0.97 }}
            >
              <Zap size={12} />
              Explain
            </motion.button>
          )}
        </div>
      )}
    </motion.div>
  );
}

function EvidenceCollectionCard({ evidence }) {
  const palette = ['#f59e0b', '#3b82f6', '#10b981', '#8b5cf6', '#06b6d4', '#ef4444'];
  const refs = evidence.references || [];

  const handleLocateAll = () => {
    EventBus.emit(Events.EVIDENCE_SELECTED, { evidenceId: evidence.id, evidence });
    EventBus.emit(Events.WORKSPACE_OPENED, {});
  };

  return (
    <motion.div
      className="ev-card ev-card--collection"
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
    >
      <div className="ev-card-header">
        <div className="ev-card-badge" style={{ background: '#6366f122', color: '#6366f1' }}>
          <Layers size={11} />
          {refs.length} Evidence Groups
        </div>
      </div>
      {evidence.summary && <div className="ev-card-desc">{evidence.summary}</div>}
      <div className="ev-card-legend">
        {refs.map((ref, i) => (
          <div key={ref.id || i} className="ev-card-legend-item">
            <span className="ev-card-legend-dot" style={{ background: palette[i % palette.length] }} />
            <span className="ev-card-legend-label">{ref.description}</span>
          </div>
        ))}
      </div>
      {refs.length > 0 && (
        <div className="ev-card-actions">
          <motion.button
            type="button"
            className="ev-card-action ev-card-action--primary"
            onClick={handleLocateAll}
            whileHover={{ scale: 1.02 }}
            whileTap={{ scale: 0.97 }}
          >
            <MapPin size={12} />
            Locate All
          </motion.button>
        </div>
      )}
    </motion.div>
  );
}
