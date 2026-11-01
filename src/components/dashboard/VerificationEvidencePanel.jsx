/**
 * VerificationEvidencePanel — Collapsible disclosure for analytical verification details.
 *
 * PURPOSE:
 *   Separates the user-facing analytical answer from supporting verification metadata.
 *   - Starts collapsed by default.
 *   - Shows a discreet disclosure bar with a shield/check icon and downward chevron.
 *   - When expanded, shows deterministic verification status, execution engine,
 *     scanned rows, execution timing, and source rows (if available).
 *   - Does NOT show a green VERIFIED badge if verification failed or was skipped.
 *   - Fully accessible with keyboard support and aria attributes.
 */
import { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { ShieldCheck, AlertCircle, ChevronDown, ChevronUp, CheckCircle, Database, Clock, Layers } from 'lucide-react';
import { formatCol, opLabel, deriveDimensionHeader, formatValue } from '../../utils/analyticalFormatting.js';

export default function VerificationEvidencePanel({ analysisId, result }) {
  const [isExpanded, setIsExpanded] = useState(false);

  if (!result) return null;

  const { verified, operation, column, details = {}, rows = [] } = result;

  const primaryDimension = deriveDimensionHeader(details, column);
  const valueColumn = details?.value_column || (column !== primaryDimension ? column : null);
  const engine = details?.engine ? formatCol(details.engine) : 'Deterministic (DuckDB/Pandas)';
  const totalRows = details?.total_rows ?? result.row_count;
  const executionTime = details?.execution_time_ms;

  const panelId = `ve-details-${analysisId || 'analysis'}`;

  // Accurate verification status representation
  const isVerified = verified === true;
  const isFailed = verified === false;

  const statusLabel = isVerified
    ? 'Verified deterministic calculation'
    : (isFailed ? 'Unverified calculation' : 'Calculation details');

  return (
    <div className="ve-container">
      {/* ── Collapsed / Toggle Disclosure Bar (Compact Pill) ── */}
      <motion.button
        type="button"
        className={`ve-trigger ve-trigger--${isVerified ? 'verified' : isFailed ? 'unverified' : 'neutral'}${isExpanded ? ' ve-trigger--expanded' : ''}`}
        onClick={() => setIsExpanded(v => !v)}
        aria-expanded={isExpanded}
        aria-controls={panelId}
        id={`ve-btn-${analysisId || 'analysis'}`}
        whileHover={{ scale: 1.02 }}
        whileTap={{ scale: 0.98 }}
        transition={{ duration: 0.15 }}
      >
        <div className="ve-trigger-left">
          {isVerified ? (
            <div className="ve-icon-wrapper ve-icon-wrapper--verified">
              <ShieldCheck size={12} className="ve-icon-verified" />
            </div>
          ) : (
            <div className="ve-icon-wrapper ve-icon-wrapper--unverified">
              <AlertCircle size={12} className="ve-icon-unverified" />
            </div>
          )}
          <span className="ve-trigger-label">{statusLabel}</span>
        </div>
        <motion.div
          className="ve-trigger-right"
          animate={{ rotate: isExpanded ? 180 : 0 }}
          transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
        >
          <ChevronDown size={12} />
        </motion.div>
      </motion.button>

      {/* ── Expanded Verification Details Panel ── */}
      <AnimatePresence initial={false}>
        {isExpanded && (
          <motion.div
            id={panelId}
            role="region"
            aria-labelledby={`ve-btn-${analysisId || 'analysis'}`}
            className="ve-content"
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
          >
            {/* Status Badge */}
            <div className="ve-header-row">
              {isVerified ? (
                <span className="ve-badge ve-badge--verified">
                  <CheckCircle size={11} /> Deterministically Verified
                </span>
              ) : (
                <span className="ve-badge ve-badge--unverified">
                  <AlertCircle size={11} /> Unverified Calculation
                </span>
              )}
              {details?.filter && (
                <span className="ve-badge ve-badge--filter" title={`Filter: ${details.filter}`}>
                  Filter Applied
                </span>
              )}
            </div>

            {/* Audit Metadata Grid */}
            <div className="ve-meta-grid">
              <div className="ve-meta-item">
                <span className="ve-meta-key">Engine</span>
                <span className="ve-meta-val">
                  <Database size={11} /> {engine}
                </span>
              </div>
              <div className="ve-meta-item">
                <span className="ve-meta-key">Operation</span>
                <span className="ve-meta-val">{opLabel(operation)}</span>
              </div>
              {primaryDimension && (
                <div className="ve-meta-item">
                  <span className="ve-meta-key">Dimension</span>
                  <span className="ve-meta-val">{formatCol(primaryDimension)}</span>
                </div>
              )}
              {valueColumn && (
                <div className="ve-meta-item">
                  <span className="ve-meta-key">Metric</span>
                  <span className="ve-meta-val">{formatCol(valueColumn)}</span>
                </div>
              )}
              {totalRows != null && totalRows > 0 && (
                <div className="ve-meta-item">
                  <span className="ve-meta-key">Scanned Rows</span>
                  <span className="ve-meta-val">
                    <Layers size={11} /> {totalRows.toLocaleString()} rows
                  </span>
                </div>
              )}
              {executionTime != null && (
                <div className="ve-meta-item">
                  <span className="ve-meta-key">Execution Time</span>
                  <span className="ve-meta-val">
                    <Clock size={11} /> {executionTime} ms
                  </span>
                </div>
              )}
              {result.unit_status && (
                <div className="ve-meta-item">
                  <span className="ve-meta-key">Unit</span>
                  <span className="ve-meta-val">
                    {result.unit || (result.unit_status === 'unknown' ? 'Unspecified in metadata' : 'Dimensionless')}
                  </span>
                </div>
              )}
              {result.unit_provenance && (
                <div className="ve-meta-item">
                  <span className="ve-meta-key">Unit Source</span>
                  <span className="ve-meta-val">{formatCol(result.unit_provenance)}</span>
                </div>
              )}
            </div>

            {/* Source Rows Table (when source rows exist) */}
            {rows.length > 0 && (
              <div className="ve-source-rows-section">
                <div className="ve-source-rows-title">Sample Source Observations</div>
                <div className="ve-source-rows-table-wrap">
                  <table className="arc-rows-table">
                    <thead>
                      <tr>
                        <th className="arc-th arc-th--row">Source Row</th>
                        <th className="arc-th">DF Index</th>
                        {Object.keys(rows[0])
                          .filter(k => !['_row_number', 'dataframe_index', 'source_row_number', 'result_rank'].includes(k))
                          .slice(0, 8)
                          .map(col => (
                            <th key={col} className="arc-th">{formatCol(col)}</th>
                          ))}
                      </tr>
                    </thead>
                    <tbody>
                      {rows.slice(0, 10).map((row, i) => (
                        <tr key={i} className="arc-tr">
                          <td className="arc-td arc-td--row">Row {row.source_row_number ?? (row.dataframe_index != null ? row.dataframe_index + 1 : i + 1)}</td>
                          <td className="arc-td">{row.dataframe_index != null ? row.dataframe_index : '—'}</td>
                          {Object.keys(rows[0])
                            .filter(k => !['_row_number', 'dataframe_index', 'source_row_number', 'result_rank'].includes(k))
                            .slice(0, 8)
                            .map(col => (
                              <td key={col} className="arc-td">{formatValue(row[col])}</td>
                            ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
