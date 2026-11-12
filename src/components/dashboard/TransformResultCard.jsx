/**
 * TransformResultCard — shows result after transformation was applied.
 *
 * Props:
 *   result: {
 *     version_id, version_number, changed_cells, operation_label, success
 *   }
 *   datasetId: string
 *   onUndo: (datasetId) => void
 *   onExport: (datasetId, onProgress) => Promise<{ status?: string, filename?: string, serverFilename?: string, sizeBytes?: number } | void>
 */
import React, { useState, useEffect } from 'react';
import { createPortal } from 'react-dom';
import { CheckCircle2, RotateCcw, Download, Sparkles, Loader2, X } from 'lucide-react';

function formatBytes(bytes) {
  if (!bytes || bytes <= 0) return '';
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function TransformResultCard({ result, datasetId, onUndo, onExport }) {
  const [exportState, setExportState] = useState('idle'); // 'idle' | 'exporting' | 'saved' | 'error'
  const [progress, setProgress] = useState({ percent: 0, loadedBytes: 0, totalBytes: 0 });
  const [exportError, setExportError] = useState('');
  const [savedPopup, setSavedPopup] = useState(null); // { filename: string } | null

  // Allow dismissing the saved popup via Escape key
  useEffect(() => {
    if (!savedPopup) return;
    const onKey = (e) => {
      if (e.key === 'Escape') setSavedPopup(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [savedPopup]);

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
    setExportError('');
    setProgress({ percent: 0, loadedBytes: 0, totalBytes: 0 });

    try {
      const outcome = await onExport(datasetId, (info) => {
        if (!info) return;
        setProgress({
          percent: info.percent ?? 0,
          loadedBytes: info.loadedBytes ?? 0,
          totalBytes: info.totalBytes ?? 0,
        });
      });

      if (outcome && outcome.status === 'cancelled') {
        setExportState('idle');
        setProgress({ percent: 0, loadedBytes: 0, totalBytes: 0 });
        return;
      }

      setProgress((prev) => ({ ...prev, percent: 100 }));
      setExportState('saved');

      const displayFile = outcome?.filename || outcome?.serverFilename || 'dataset_transformed.csv';
      setSavedPopup({ filename: displayFile });

      setTimeout(() => {
        setExportState('idle');
        setProgress({ percent: 0, loadedBytes: 0, totalBytes: 0 });
      }, 3000);
    } catch (err) {
      setExportState('error');
      setExportError(err?.message || 'Export failed');
      setTimeout(() => {
        setExportState('idle');
        setExportError('');
      }, 5000);
    }
  };

  return (
    <>
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
                Exporting...
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

        {/* ── Export Progress Bar ── */}
        {exportState === 'exporting' && (
          <div
            style={{
              marginTop: '12px',
              padding: '10px 12px',
              borderRadius: '8px',
              background: 'rgba(15, 23, 42, 0.72)',
              border: '1px solid rgba(99, 102, 241, 0.28)',
            }}
          >
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                fontSize: '12px',
                marginBottom: '7px',
                color: '#e2e8f0',
                fontWeight: 500,
              }}
            >
              <span>Saving your CSV...</span>
              <span style={{ fontVariantNumeric: 'tabular-nums', color: '#94a3b8' }}>
                {progress.loadedBytes > 0 ? `${formatBytes(progress.loadedBytes)} · ` : ''}
                {progress.percent > 0 ? `${progress.percent}%` : 'Preparing...'}
              </span>
            </div>
            <div
              style={{
                width: '100%',
                height: '6px',
                borderRadius: '999px',
                background: 'rgba(255, 255, 255, 0.08)',
                overflow: 'hidden',
              }}
            >
              <div
                style={{
                  width: `${Math.max(progress.percent, 6)}%`,
                  height: '100%',
                  borderRadius: '999px',
                  background: 'linear-gradient(90deg, #6366f1 0%, #10b981 100%)',
                  transition: 'width 180ms ease-out',
                }}
              />
            </div>
          </div>
        )}

        {/* ── Error Message ── */}
        {exportState === 'error' && exportError && (
          <div style={{ marginTop: '8px', fontSize: '12px', color: '#f87171' }}>
            {exportError}
          </div>
        )}
      </div>

      {/* ── Saved Confirmation Popup with Close (×) Button ── */}
      {savedPopup && typeof document !== 'undefined' && createPortal(
        <div
          role="dialog"
          aria-live="polite"
          aria-label="Export saved confirmation"
          style={{
            position: 'fixed',
            top: '24px',
            right: '24px',
            zIndex: 9999,
            maxWidth: '420px',
            width: 'calc(100vw - 32px)',
            background: 'linear-gradient(145deg, rgba(16, 24, 39, 0.98), rgba(10, 15, 28, 0.98))',
            border: '1px solid rgba(16, 185, 129, 0.45)',
            boxShadow: '0 20px 45px rgba(0, 0, 0, 0.55), 0 0 0 1px rgba(255,255,255,0.04)',
            borderRadius: '12px',
            padding: '14px 16px',
            display: 'flex',
            alignItems: 'flex-start',
            gap: '12px',
            color: '#f8fafc',
            backdropFilter: 'blur(12px)',
          }}
        >
          <div
            style={{
              flexShrink: 0,
              width: '32px',
              height: '32px',
              borderRadius: '8px',
              background: 'rgba(16, 185, 129, 0.14)',
              border: '1px solid rgba(16, 185, 129, 0.35)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: '#34d399',
              marginTop: '1px',
            }}
          >
            <CheckCircle2 size={18} />
          </div>

          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: '13px', fontWeight: 600, color: '#34d399', marginBottom: '3px' }}>
              Export Complete
            </div>
            <div style={{ fontSize: '13px', lineHeight: 1.45, color: '#e2e8f0', wordBreak: 'break-word' }}>
              Saved your <strong style={{ color: '#ffffff' }}>{savedPopup.filename}</strong> with all your latest transformations.
            </div>
          </div>

          <button
            type="button"
            onClick={() => setSavedPopup(null)}
            aria-label="Close notification"
            title="Close"
            style={{
              flexShrink: 0,
              background: 'transparent',
              border: 'none',
              color: '#94a3b8',
              cursor: 'pointer',
              padding: '4px',
              borderRadius: '6px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              lineHeight: 1,
            }}
          >
            <X size={16} />
          </button>
        </div>,
        document.body
      )}
    </>
  );
}
