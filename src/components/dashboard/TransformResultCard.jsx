/**
 * TransformResultCard — shows result after transformation was applied.
 *
 * Props:
 *   result: {
 *     version_id, version_number, changed_cells, operation_label, success
 *   }
 *   datasetId: string
 *   onUndo: (datasetId) => void
 *   onExport: (datasetId) => void
 */
import React from 'react';
import { CheckCircle2, RotateCcw, Download, Sparkles } from 'lucide-react';

export default function TransformResultCard({ result, datasetId, onUndo, onExport }) {
  if (!result) return null;

  const {
    operation_label = 'Transformation',
    changed_cells = 0,
    version_number = null,
    success,
  } = result;

  if (result.success === false) return null;

  return (
    <div className="tresult-card">
      {/* ── Header ── */}
      <div className="tresult-header">
        <CheckCircle2 size={16} className="tresult-check-icon" />
        <span className="tresult-title">Transformation applied.</span>
      </div>

      {/* ── Operation label ── */}
      <div className="tresult-op-label">{operation_label}</div>

      {/* ── Stats ── */}
      <div className="tresult-stats">
        {changed_cells === 0 ? (
           <><strong>Schema updated</strong>. </>
        ) : (
           <><strong>{changed_cells.toLocaleString()}</strong> {changed_cells === 1 ? 'cell' : 'cells'} changed. </>
        )}
        {version_number != null && (
          <>Version <strong>{version_number}</strong> of dataset.</>
        )}
      </div>

      {/* ── Intelligence Suggestion Provenance ── */}
      {result.intelligence_provenance && result.intelligence_provenance.used && (
        <div className="tresult-provenance" style={{
            margin: '0 12px 12px', padding: '10px 12px', 
            backgroundColor: 'var(--bg-tertiary, rgba(255,255,255,0.05))',
            borderLeft: '3px solid var(--accent-primary, #6366f1)',
            borderRadius: '4px', fontSize: '13px'
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontWeight: 600, marginBottom: '4px', color: 'var(--text-primary)' }}>
            <Sparkles size={14} style={{ color: 'var(--accent-primary, #6366f1)' }} />
            Plexis Intelligence suggestion
          </div>
          <div style={{ color: 'var(--text-secondary)' }}>
            {result.intelligence_provenance.reason || 'An alternative value was suggested and accepted.'}
          </div>
        </div>
      )}

      {/* ── Actions ── */}
      <div className="tresult-actions">
        <button
          type="button"
          className="tresult-btn"
          onClick={() => onUndo?.(datasetId)}
          title="Undo this transformation"
        >
          <RotateCcw size={13} />
          Undo
        </button>
        <button
          type="button"
          className="tresult-btn tresult-btn--primary"
          onClick={() => onExport?.(datasetId)}
          title="Download transformed dataset as CSV"
        >
          <Download size={13} />
          Export CSV
        </button>
      </div>
    </div>
  );
}
