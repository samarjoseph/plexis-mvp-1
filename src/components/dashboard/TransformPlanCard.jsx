/**
 * TransformPlanCard — shows a transformation plan before execution.
 *
 * Props:
 *   plan: {
 *     plan_id, operation_type, operation_label, operation_code,
 *     affected_rows_count, affected_cells_count, affected_columns,
 *     before_sample: [{row_index, source_row_number, column, before, after}],
 *     warnings: []
 *   }
 *   datasetId: string
 *   onApply: (planId, datasetId) => void
 *   onCancel: () => void
 */
import React, { useState } from 'react';
import { Settings, AlertTriangle, ChevronRight, X, Sparkles, Table } from 'lucide-react';
import TransformReviewCard from './TransformReviewCard';
import { EventBus } from '../../events/EventBus.js';
import { Events } from '../../events/Events.js';

export default function TransformPlanCard({ plan, datasetId, onApply, onCancel, transformationUpdate }) {
  const [showReview, setShowReview] = useState(false);
  const [isApplying, setIsApplying] = useState(false);

  if (!plan) return null;

  const {
    plan_id,
    operation_label = 'Transformation',
    operation_code,
    affected_cells_count = 0,
    affected_columns = [],
    before_sample = [],
    warnings = [],
    operation_payload = {},
  } = plan;
  
  const impactType = operation_payload.impact?.type || 'cell';
  const columnsChanged = operation_payload.impact?.columns_changed || 0;

  const hasChanges = affected_cells_count > 0 || (impactType === 'schema' && columnsChanged > 0);

  // Build per-column summary from before_sample
  const columnSummary = React.useMemo(() => {
    const map = {};
    before_sample.forEach(({ column, after }) => {
      if (!map[column]) map[column] = { count: 0, after: String(after ?? '—') };
      map[column].count += 1;
    });
    // Fallback: use affected_columns list if before_sample is empty
    if (Object.keys(map).length === 0 && affected_columns.length > 0) {
      affected_columns.forEach(col => { map[col] = { count: affected_cells_count || 0, after: '—' }; });
    }
    return map;
  }, [before_sample, affected_columns]);

  const handleApply = async (planId, dsId) => {
    setIsApplying(true);
    try {
      await onApply(planId, dsId);
    } finally {
      setIsApplying(false);
    }
  };

  return (
    <div className="tpc-card">
      {/* ── Header ── */}
      <div className="tpc-header">
        {transformationUpdate && (
          <div className="tpc-updated-banner" role="status">
            <strong>Transformation updated</strong>
            {transformationUpdate.previous_operation_label && (
              <span>Previous: {transformationUpdate.previous_operation_label}</span>
            )}
            <span>Corrected: {operation_label}</span>
            <small>Updated plan {plan_id}</small>
          </div>
        )}
        <div className="tpc-header-left">
          <div className="tpc-badge">
            <Settings size={11} />
            TRANSFORMATION PLAN
          </div>
          <div className="tpc-operation-label">{operation_label}</div>
        </div>
      </div>

      {/* ── Steps Summary (IR_COMPOSITE) ── */}
      {plan.operation_type === 'IR_COMPOSITE' && plan.operation_payload?.step_labels && plan.operation_payload.step_labels.length > 0 && (
        <div className="tpc-steps" style={{ padding: '0 12px 12px', fontSize: '13px' }}>
          <div style={{ fontWeight: 600, color: 'var(--text-primary)', marginBottom: '4px' }}>Steps:</div>
          <ol style={{ margin: 0, paddingLeft: '20px', color: 'var(--text-secondary)' }}>
            {plan.operation_payload.step_labels.map((label, idx) => (
              <li key={idx} style={{ marginBottom: '2px' }}>{label}</li>
            ))}
          </ol>
        </div>
      )}

      {/* ── Column table ── */}
      {Object.keys(columnSummary).length > 0 && (
        <div className="tpc-table-wrap">
          <table className="tpc-table">
            <thead>
              <tr>
                <th>Column</th>
                <th>Affected cells</th>
                {plan.operation_type !== 'IR_COMPOSITE' && <th>Action</th>}
              </tr>
            </thead>
            <tbody>
              {Object.entries(columnSummary).map(([col, { count, after }]) => (
                <tr key={col}>
                  <td className="tpc-col-name">{col}</td>
                  <td>{count}</td>
                  {plan.operation_type !== 'IR_COMPOSITE' && (
                    <td className="tpc-col-action">
                      {plan.operation_type === 'CLIP' ? (
                        operation_payload?.max != null && operation_payload?.min != null
                          ? `Clip between ${operation_payload.min} and ${operation_payload.max}`
                          : operation_payload?.max != null
                          ? `Cap at ${operation_payload.max}`
                          : `Floor at ${operation_payload.min}`
                      ) : plan.operation_type === 'UPDATE_EXTREMUM' ? (
                        `Set ${operation_payload?.extremum || 'extremum'} to ${operation_payload?.new_value}`
                      ) : plan.operation_type === 'FILL_PATTERN' ? (
                        <span>Fill alternating <code>{(operation_payload?.pattern || []).join(', ')}</code></span>
                      ) : plan.operation_type === 'DROP_COLUMN' ? (
                        <span>Drop column</span>
                      ) : plan.operation_type === 'ADD_COLUMN' ? (
                        <span>Add column</span>
                      ) : plan.operation_type === 'RENAME_COLUMN' ? (
                        <>Rename to <code>{after}</code></>
                      ) : (
                        <>Replace with <code>{after}</code></>
                      )}
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* ── Summary line ── */}
      <div className="tpc-summary">
        {hasChanges ? (
          impactType === 'schema' ? (
            <span>
              <strong>{columnsChanged}</strong> column{columnsChanged === 1 ? '' : 's'} will be{' '}
              {plan.operation_type === 'DROP_COLUMN' ? 'dropped' : plan.operation_type === 'ADD_COLUMN' ? 'added' : plan.operation_type === 'RENAME_COLUMN' ? 'renamed' : 'changed'}.
            </span>
          ) : (
            <span>
              <strong>{affected_cells_count}</strong> {affected_cells_count === 1 ? 'cell' : 'cells'} will change.
              {operation_payload?.expected_extremum?.max != null && (
                <span> (Expected new max: <strong>{operation_payload.expected_extremum.max}</strong>)</span>
              )}
              {operation_payload?.expected_extremum?.min != null && (
                <span> (Expected new min: <strong>{operation_payload.expected_extremum.min}</strong>)</span>
              )}
            </span>
          )
        ) : (
          <span className="tpc-no-changes">No changes needed.</span>
        )}
      </div>

      {/* ── Warnings ── */}
      {warnings.length > 0 && (
        <div className="tpc-warnings">
          {warnings.map((w, i) => (
            <div key={i} className="tpc-warning-row">
              <AlertTriangle size={12} className="tpc-warning-icon" />
              <span>{w}</span>
            </div>
          ))}
        </div>
      )}

      {/* ── Intelligence Suggestion Provenance ── */}
      {plan.intelligence_provenance && plan.intelligence_provenance.used && (
        <div className="tpc-provenance" style={{
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
            {plan.intelligence_provenance.reason || 'An alternative value was suggested and accepted.'}
          </div>
        </div>
      )}

      {/* ── Actions ── */}
      <div className="tpc-actions">
        <button
          type="button"
          className="tpc-btn tpc-btn--primary"
          disabled={!hasChanges || showReview}
          onClick={() => setShowReview(true)}
          title={!hasChanges ? 'No changes needed' : 'Review the before/after diff'}
        >
          <ChevronRight size={13} />
          Review Changes
        </button>
        <button
          type="button"
          className="tpc-btn"
          onClick={() => {
            if (datasetId) {
              EventBus.emit(Events.WORKSPACE_OPENED, { datasetId });
            }
          }}
          title="Open live spreadsheet view for this dataset"
        >
          <Table size={13} />
          View Spreadsheet
        </button>
        <button
          type="button"
          className="tpc-btn"
          onClick={onCancel}
        >
          <X size={13} />
          Cancel
        </button>
      </div>

      {/* ── Inline Review ── */}
      {showReview && (
        <TransformReviewCard
          plan={plan}
          onApply={handleApply}
          onCancel={() => setShowReview(false)}
          isApplying={isApplying}
          datasetId={datasetId}
        />
      )}
    </div>
  );
}
