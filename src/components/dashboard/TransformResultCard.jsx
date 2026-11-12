/**
 * TransformResultCard — shows result after transformation was applied.
 *
 * Props:
 *   result: {
 *     version_id, version_number, changed_cells, operation_label, success
 *   }
 *   datasetId: string
 *   onUndo: (datasetId) => void
 *   onExport: (datasetId) => Promise<{ status?: string, filename?: string } | void>
 */
import React, { useState } from 'react';
import { CheckCircle2, RotateCcw, Download, Sparkles, Loader2 } from 'lucide-react';

export default function TransformResultCard({ result, datasetId, onUndo, onExport }) {
  const [exportState, setExportState] = useState('idle'); // 'idle' | 'exporting' | 'saved' | 'error'
  const [exportMsg, setExportMsg] = useState('');

  if (!result) return null;

  const {
    operation_label = 'Transformation',
    changed_cells = 0,
    version_number = null,
  } = result;

  if (result.success === false) return null;

  const handleExportClick = async () => {
    if (!onExport || exportState === 'exporting') return;
    setExportState('exporting');
    setExportMsg('');
    try {
      const outcome = await onExport(datasetId);
      if (outcome && outcome.status === 'cancelled') {
        setExportState('idle');
        return;
      }
      setExportState('saved');
      setExportMsg(outcome?.filename ? `Saved ${outcome.filename}` : 'Saved!');
      setTimeout(() => {
        setExportState('idle');
        setExportMsg('');
      }, 3500);
    } catch (err) {
      setExportState('error');
      setExportMsg(err?.message || 'Export failed');
      setTimeout(() => {
        setExportState('idle');
        setExportMsg('');
      }, 4500);
    }
  };

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
          onClick={handleExportClick}
          disabled={exportState === 'exporting'}
          title="Download transformed dataset as CSV"
        >
          {exportState === 'exporting' ? (
            <>
              <Loader2 size={13} className="spin" />
              Saving CSV...
            </>
          ) : exportState === 'saved' ? (
            <>
              <CheckCircle2 size={13} />
              Saved!
            </>
          ) : exportState === 'error' ? (
            <>
              <Download size={13} />
              Retry Export
            </>
          ) : (
            <>
              <Download size={13} />
              Export CSV
            </>
          )}
        </button>
      </div>
      {exportMsg && (
        <div
          style={{
            marginTop: '8px',
            fontSize: '12px',
            color: exportState === 'error' ? '#f87171' : '#34d399',
          }}
        >
          {exportMsg}
        </div>
      )}
    </div>
  );
}
