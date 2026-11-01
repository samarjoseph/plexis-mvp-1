/**
 * AnalyticalResultCard — Chat-first analytical result container.
 *
 * PURPOSE:
 *   1. Displays context-enriched analytical results directly in the chat message
 *      for immediate comprehension (prominent metric, grounded record context,
 *      tied record summaries, comparisons, and grouped tables).
 *   2. Displays verification details in a separate, collapsible disclosure panel
 *      (VerificationEvidencePanel) placed underneath the answer.
 *   3. Does NOT contain action buttons — those live contextually in VerticalActionMenu.
 */
import { useState, useMemo } from 'react';
import { ChevronDown, ChevronUp } from 'lucide-react';
import { motion } from 'framer-motion';
import VerificationEvidencePanel from './VerificationEvidencePanel.jsx';
import {
  formatCol,
  formatNumber,
  formatValue,
  opLabel,
  deriveMetricHeader,
  deriveDimensionHeader,
  getOrderedGroupedEntries,
  getRankingLabel,
  extractClientContextFields,
} from '../../utils/analyticalFormatting.js';

// ── AnalyticalResultCard ──────────────────────────────────────────────────────

export default function AnalyticalResultCard({ analysisId, result }) {
  if (!analysisId || !result) return null;

  return (
    <motion.div
      className="arc-root"
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
    >
      {/* ── 1. Main Chat-First Analytical Result ── */}
      <AnalyticalResultView result={result} />

      {/* ── 2. Separate Collapsible Verification Evidence Panel ── */}
      <VerificationEvidencePanel
        analysisId={analysisId}
        result={result}
      />
    </motion.div>
  );
}

// ── Main Analytical Result View (Chat-First) ──────────────────────────────────

// ── Sub-component: Ranking Result Table (Top-N / Bottom-N) ───────────────────

function RankingResultTable({ result, expanded, onToggleExpand }) {
  const { operation = '', column = '', details = {} } = result || {};
  const rows = result?.rows || result?.matching_rows || [];
  const isAsc = operation?.toLowerCase?.() === 'bottom_n' || details?.order === 'asc' || details?.ascending;
  const totalRows = rows.length;
  const isTruncated = totalRows > 10;
  const displayRows = isTruncated && !expanded ? rows.slice(0, 10) : rows;

  const contextColNames = useMemo(() => {
    if (!rows || rows.length === 0) return [];
    const sample = rows[0];
    const systemCols = new Set(['_row_number', 'dataframe_index', 'source_row_number', 'result_rank']);
    const targetNorm = (column || '').toLowerCase().trim();
    return Object.keys(sample).filter(k => {
      const kl = k.toLowerCase().trim();
      return !systemCols.has(kl) && !k.startsWith('_') && kl !== targetNorm;
    }).slice(0, 5);
  }, [rows, column]);

  const metricHeader = deriveMetricHeader(operation, column, 'Value');

  return (
    <div className="arc-table-container">
      <div className="arc-record-header" style={{ marginBottom: '8px' }}>
        <span className="arc-record-tag">
          {isAsc ? 'Bottom' : 'Top'} {totalRows} Records
        </span>
        <span className="arc-record-title">
          Ranked by {isAsc ? 'lowest' : 'highest'} {formatCol(column)}
        </span>
      </div>
      <div className="arc-table-scroll">
        <table className="arc-table" aria-label={`Top ${totalRows} records by ${formatCol(column)}`}>
          <thead>
            <tr>
              <th scope="col" className="arc-th arc-th-category">Rank</th>
              <th scope="col" className="arc-th">Source Row</th>
              <th scope="col" className="arc-th arc-th-num" style={{ color: 'var(--accent-color, #38bdf8)', fontWeight: 600 }}>
                {metricHeader}
              </th>
              {contextColNames.map(colName => (
                <th key={colName} scope="col" className="arc-th">{formatCol(colName)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {displayRows.map((r, i) => {
              const rankVal = r.result_rank ?? (i + 1);
              const srcRow = r.source_row_number ?? (r.dataframe_index != null ? r.dataframe_index + 1 : i + 1);
              const metricVal = r[column] ?? (Array.isArray(result.value) ? result.value[i] : null);
              return (
                <tr key={i} className="arc-tr">
                  <td className="arc-td arc-td-category" style={{ fontWeight: 600 }}>#{rankVal}</td>
                  <td className="arc-td">Row {srcRow}</td>
                  <td className="arc-td arc-td-num" style={{ fontWeight: 600, color: 'var(--accent-color, #38bdf8)' }}>
                    {formatValue(metricVal)}
                  </td>
                  {contextColNames.map(colName => (
                    <td key={colName} className="arc-td">{formatValue(r[colName])}</td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {isTruncated && (
        <button
          type="button"
          className="arc-table-toggle"
          onClick={onToggleExpand}
          aria-expanded={expanded}
        >
          {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
          <span>
            {expanded
              ? `Collapse to first 10 rows`
              : `Showing 10 of ${totalRows} records · Show all ${totalRows}`}
          </span>
        </button>
      )}
    </div>
  );
}

export function AnalyticalResultView({ result }) {
  const { value, operation, column, details = {} } = result || {};
  const [expandedAllRows, setExpandedAllRows] = useState(false);

  // Grouped table detection
  const is1DDict = useMemo(() => {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) return false;
    const vals = Object.values(value);
    return vals.length > 0 && vals.every(v => typeof v !== 'object' || v === null);
  }, [value]);

  const is2DDict = useMemo(() => {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) return false;
    const vals = Object.values(value);
    return vals.length > 0 && vals.every(v => typeof v === 'object' && v !== null && !Array.isArray(v));
  }, [value]);

  const isRecordList = useMemo(() => {
    return Array.isArray(value) && value.length > 0 && typeof value[0] === 'object' && value[0] !== null;
  }, [value]);

  const isArrayList = useMemo(() => {
    return Array.isArray(value) && value.every(v => typeof v !== 'object' || v === null);
  }, [value]);

  const isRanking = Boolean(
    operation?.toLowerCase?.() === 'top_n' ||
    operation?.toLowerCase?.() === 'bottom_n' ||
    (Array.isArray(value) && ((result?.rows && result.rows.length > 0) || (result?.matching_rows && result.matching_rows.length > 0)) && !is1DDict && !is2DDict)
  );

  const isGrouped = Boolean(
    result?.type === 'grouped' ||
    operation?.toLowerCase?.().startsWith('group_') ||
    details?.group_by != null ||
    is1DDict ||
    is2DDict ||
    isRecordList
  );

  const isMean = Boolean(operation?.toLowerCase?.().includes('mean') || operation?.toLowerCase?.().includes('avg'));
  const primaryDimension = deriveDimensionHeader(details, column);
  const valueColumn = details?.value_column || (column !== primaryDimension ? column : null);

  return (
    <div className="arc-result-view">
      {/* 1D Grouped Table */}
      {isGrouped && is1DDict && (
        <Grouped1DTable
          data={value}
          dimensionCol={primaryDimension}
          valueCol={valueColumn}
          operation={operation}
          isAverage={isMean}
          details={details}
          ranking={result?.ranking}
          expanded={expandedAllRows}
          onToggleExpand={() => setExpandedAllRows(v => !v)}
        />
      )}

      {/* 2D Grouped Matrix */}
      {isGrouped && is2DDict && (
        <Grouped2DTable
          data={value}
          dimensionCol={primaryDimension}
          isAverage={isMean}
          details={details}
          expanded={expandedAllRows}
          onToggleExpand={() => setExpandedAllRows(v => !v)}
        />
      )}

      {/* Record List */}
      {isGrouped && isRecordList && (
        <RecordListTable
          records={value}
          expanded={expandedAllRows}
          onToggleExpand={() => setExpandedAllRows(v => !v)}
        />
      )}

      {/* Row-Level Ranking Table (Top-N / Bottom-N on records) */}
      {isRanking && (
        <RankingResultTable
          result={result}
          expanded={expandedAllRows}
          onToggleExpand={() => setExpandedAllRows(v => !v)}
        />
      )}

      {/* List of Categorical / Distinct Values (e.g. Unique Values, Column Names) */}
      {!isGrouped && !isRanking && isArrayList && (
        <ArrayListResultView result={result} />
      )}

      {/* Enriched Scalar / Extremum / Comparison View (when not grouped, not row ranking, and not array list) */}
      {!isGrouped && !isRanking && !isArrayList && (
        <ScalarOrEnrichedResultView result={result} />
      )}
    </div>
  );
}

// ── Enriched Scalar / Extremum / Comparison Result View ────────────────────────

function ScalarOrEnrichedResultView({ result }) {
  const {
    value,
    unit = null,
    unit_status = null,
    unit_label = '',
    unit_provenance = null,
    operation = '',
    column = '',
    rows = [],
    context_record = null,
    details = {},
    row_count = 0,
    tie_count = 0,
    is_tied = false,
  } = result || {};

  const opNorm = (operation || '').toLowerCase();
  const isExtremum = ['max', 'min', 'idxmax', 'idxmin', 'longest', 'shortest', 'highest', 'lowest'].includes(opNorm);
  const isRanking = ['top_n', 'bottom_n', 'rank', 'ranking'].includes(opNorm);
  const hasRows = rows && rows.length > 0;
  const isComparison = opNorm.includes('difference') || opNorm.includes('comparison') || Boolean(details.group1 && details.group2) || details.difference != null;
  const isDataQuality = ['column_uniqueness', 'duplicates', 'duplicate_count', 'missing_values'].includes(opNorm);

  const totalRows = details?.total_rows ?? row_count ?? (rows ? rows.length : 0);
  const effectiveTieCount = tie_count || (isExtremum && rows ? rows.length : 1);
  const showTie = isExtremum && !isRanking && (is_tied || effectiveTieCount > 1) && rows.length > 1;

  // Extract contextual fields for single matching record (up to 8 grounded attributes)
  const contextFields = useMemo(() => {
    if (context_record && Object.keys(context_record).length > 0) {
      return context_record;
    }
    if (hasRows) {
      return extractClientContextFields(rows[0], column, 8);
    }
    return {};
  }, [context_record, hasRows, rows, column]);

  const metricHeader = deriveMetricHeader(operation, column, 'Result');

  // Resolved unit vs unknown unit display
  const renderedUnit = useMemo(() => {
    if (unit) {
      return <span className="arc-scalar-unit">{unit}</span>;
    }
    if (typeof value !== 'number') {
      return null;
    }
    if (unit_status === 'unknown' || (!unit && unit_label && unit_status !== 'not_applicable')) {
      return <span className="arc-unit-unknown">{unit_label || 'Unit not specified in dataset metadata'}</span>;
    }
    return null;
  }, [unit, unit_status, unit_label, value]);

  // ── A. Comparison Result View ──
  if (isComparison) {
    const diffVal = details?.difference != null ? details.difference : value;
    return (
      <div className="arc-scalar-container">
        <div className="arc-metric-headline">
          <span className="arc-scalar-value">{formatValue(diffVal)}</span>
          {renderedUnit}
          <span className="arc-metric-title">Difference · {metricHeader}</span>
        </div>
        {details.group1 && details.group2 && (
          <div className="arc-record-context">
            <div className="arc-record-header">
              <span className="arc-record-tag">Compared Groups</span>
              <span className="arc-record-title">{formatCol(details.group1)} vs {formatCol(details.group2)}</span>
            </div>
            <div className="arc-context-grid">
              <div className="arc-context-item">
                <span className="arc-context-key">{formatCol(details.group1)}</span>
                <span className="arc-context-val">{formatValue(details.val1 ?? details.group1_value ?? '—')}</span>
              </div>
              <div className="arc-context-item">
                <span className="arc-context-key">{formatCol(details.group2)}</span>
                <span className="arc-context-val">{formatValue(details.val2 ?? details.group2_value ?? '—')}</span>
              </div>
            </div>
          </div>
        )}
      </div>
    );
  }

  // ── B. Data Quality Operations View ──
  if (isDataQuality && typeof value === 'object' && value !== null) {
    return (
      <div className="arc-scalar-container">
        <div className="arc-record-context">
          <div className="arc-record-header">
            <span className="arc-record-tag">Data Quality Summary</span>
            <span className="arc-record-title">{formatCol(column || 'Dataset')}</span>
          </div>
          <div className="arc-context-grid">
            {Object.entries(value).map(([k, v]) => (
              <div key={k} className="arc-context-item">
                <span className="arc-context-key">{formatCol(k)}</span>
                <span className="arc-context-val">{formatValue(v)}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    );
  }

  // ── C. Extremum / Scalar With Provenance Rows ──
  if (isExtremum || hasRows) {
    const topRow = rows[0] || {};
    const srcRowDisplay = topRow.source_row_number ?? (topRow.dataframe_index != null ? topRow.dataframe_index + 1 : 1);
    const dfIdxDisplay = topRow.dataframe_index;

    return (
      <div className="arc-scalar-container">
        {/* Metric Headline */}
        <div className="arc-metric-headline">
          <span className="arc-scalar-value">{formatValue(value)}</span>
          {renderedUnit}
          <span className="arc-metric-title">{metricHeader}</span>
          {showTie && (
            <span className="arc-tie-tag">Tied across {effectiveTieCount} records</span>
          )}
        </div>

        {/* Tied Records Table */}
        {showTie && (
          <div className="arc-record-context">
            <div className="arc-record-header">
              <span className="arc-record-tag">Tied Records ({effectiveTieCount} total)</span>
              <span className="arc-record-title">Top records sharing the {opNorm.includes('min') ? 'minimum' : 'maximum'} value</span>
            </div>
            <div className="arc-table-scroll">
              <table className="arc-table">
                <thead>
                  <tr>
                    <th scope="col" className="arc-th arc-th-category">Rank</th>
                    <th scope="col" className="arc-th">Source Row</th>
                    <th scope="col" className="arc-th arc-th-num" style={{ color: 'var(--accent-color, #38bdf8)', fontWeight: 600 }}>{formatCol(column)}</th>
                    {Object.keys(extractClientContextFields(rows[0], column, 4)).map(colName => (
                      <th key={colName} scope="col" className="arc-th">{formatCol(colName)}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.slice(0, 10).map((r, i) => (
                    <tr key={i} className="arc-tr">
                      <td className="arc-td arc-td-category">Match {r.result_rank ?? i + 1} of {effectiveTieCount}</td>
                      <td className="arc-td">Row {r.source_row_number ?? (r.dataframe_index != null ? r.dataframe_index + 1 : i + 1)}</td>
                      <td className="arc-td arc-td-num" style={{ fontWeight: 600, color: 'var(--accent-color, #38bdf8)' }}>{formatValue(r[column] ?? value)}</td>
                      {Object.keys(extractClientContextFields(rows[0], column, 4)).map(colName => (
                        <td key={colName} className="arc-td">{formatValue(r[colName])}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Single Record Context Details */}
        {!showTie && hasRows && Object.keys(contextFields).length > 0 && (
          <div className="arc-record-context">
            <div className="arc-record-header">
              <span className="arc-record-tag">
                Source Row {srcRowDisplay}
                {dfIdxDisplay != null && (
                  <span className="arc-record-df-idx"> (index {dfIdxDisplay})</span>
                )}
              </span>
              <span className="arc-record-title">Matching Record Provenance</span>
            </div>
            <div className="arc-context-grid">
              {Object.entries(contextFields).map(([colName, colVal]) => (
                <div key={colName} className="arc-context-item">
                  <span className="arc-context-key">{formatCol(colName)}</span>
                  <span className="arc-context-val" title={String(colVal)}>{formatValue(colVal)}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Single Record with No Additional Columns */}
        {!showTie && hasRows && Object.keys(contextFields).length === 0 && (
          <div className="arc-scalar-sub">
            Observation from source row {srcRowDisplay} (no additional context columns in record)
          </div>
        )}
      </div>
    );
  }

  // ── D. Standard Scalar Metric (Mean, Sum, Count with no matching rows) ──
  return (
    <div className="arc-scalar-container">
      <div className="arc-metric-headline">
        <span className="arc-scalar-value">{formatValue(value)}</span>
        {renderedUnit}
        <span className="arc-metric-title">{metricHeader}</span>
      </div>
      {totalRows > 0 && (
        <span className="arc-scalar-sub">
          Calculated across {totalRows.toLocaleString()} observations
        </span>
      )}
    </div>
  );
}

// ── Sub-component: Array List / Distinct Values View ───────────────────────

function ArrayListResultView({ result }) {
  const { value, operation, column, details = {}, row_count } = result || {};
  const items = Array.isArray(value) ? value : [];
  const totalRows = details?.total_rows ?? row_count ?? 0;
  const colName = formatCol(column || 'Entity');
  const [copied, setCopied] = useState(false);
  const [viewMode, setViewMode] = useState('table'); // 'table' | 'chips'
  const [expanded, setExpanded] = useState(false);

  const isTruncated = items.length > 10;
  const displayItems = isTruncated && !expanded ? items.slice(0, 10) : items;

  const handleCopy = () => {
    navigator.clipboard.writeText(items.map(String).join(', '));
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="arc-table-container">
      <div className="arc-record-header" style={{ marginBottom: '8px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <span className="arc-record-tag">
            {items.length} {items.length === 1 ? 'Distinct Value' : 'Distinct Values'}
          </span>
          <span className="arc-record-title">
            Column: {colName}
          </span>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <div className="arc-view-toggle">
            <button
              type="button"
              className={`arc-toggle-btn ${viewMode === 'table' ? 'active' : ''}`}
              onClick={() => setViewMode('table')}
              title="Table view"
            >
              Table
            </button>
            <button
              type="button"
              className={`arc-toggle-btn ${viewMode === 'chips' ? 'active' : ''}`}
              onClick={() => setViewMode('chips')}
              title="Chips view"
            >
              Chips
            </button>
          </div>

          <button
            type="button"
            className="arc-list-copy-btn"
            onClick={handleCopy}
            title="Copy list as comma-separated values"
          >
            {copied ? 'Copied!' : 'Copy'}
          </button>
        </div>
      </div>

      {totalRows > 0 && (
        <div className="arc-scalar-sub" style={{ marginBottom: '10px' }}>
          Calculated across {totalRows.toLocaleString()} observations
        </div>
      )}

      {viewMode === 'table' ? (
        <>
          <div className="arc-table-scroll">
            <table className="arc-table" aria-label={`Distinct values for ${colName}`}>
              <thead>
                <tr>
                  <th scope="col" className="arc-th arc-th-rank" style={{ width: '56px' }}>#</th>
                  <th scope="col" className="arc-th arc-th-category">{colName}</th>
                </tr>
              </thead>
              <tbody>
                {displayItems.map((item, idx) => (
                  <tr key={idx} className="arc-tr">
                    <td className="arc-td arc-td-rank" style={{ fontWeight: 600, color: 'var(--dash-dim, #94a3b8)' }}>
                      #{idx + 1}
                    </td>
                    <td className="arc-td arc-td-category" style={{ fontWeight: 500, color: '#f8fafc' }}>
                      {String(item)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {isTruncated && (
            <button
              type="button"
              className="arc-table-toggle"
              onClick={() => setExpanded(v => !v)}
              aria-expanded={expanded}
            >
              {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
              <span>
                {expanded
                  ? 'Collapse to first 10 items'
                  : `Showing 10 of ${items.length} items · Show all ${items.length}`}
              </span>
            </button>
          )}
        </>
      ) : (
        <div className="arc-chips-grid">
          {items.map((item, idx) => (
            <div key={idx} className="arc-chip-item">
              <span className="arc-chip-index">{idx + 1}</span>
              <span className="arc-chip-value">{String(item)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Sub-component: 1D Grouped Table ──────────────────────────────────────────

function Grouped1DTable({
  data,
  dimensionCol,
  valueCol,
  operation,
  isAverage,
  details,
  ranking,
  expanded,
  onToggleExpand,
}) {
  const entries = useMemo(() => getOrderedGroupedEntries(data, details, ranking), [data, details, ranking]);
  const totalRows = entries.length;
  const isTruncated = totalRows > 10;
  const displayEntries = isTruncated && !expanded ? entries.slice(0, 10) : entries;
  const requestedN = Number(ranking?.requested_limit ?? details?.top_n) || 0;
  const eligibleCount = ranking?.eligible_entity_count ?? details?.eligible_group_count ?? details?.total_groups;

  const dimHeader = formatCol(dimensionCol || 'Category');
  const metricHeader = ranking?.metric_label || deriveMetricHeader(operation, valueCol, 'Value', details);

  const opNorm = (operation || '').toLowerCase();
  const isAsc = ranking?.direction === 'asc' || opNorm.includes('asc') || opNorm.includes('bottom') || opNorm.includes('min') || opNorm.includes('least');
  const rankingLabel = getRankingLabel(totalRows, requestedN, isAsc, eligibleCount);
  const statLabel = details?.filter_desc
    ? `count of ${details.filter_desc}`
    : opNorm.includes('count_filtered')
      ? 'count of positive'
      : opNorm.includes('mean') || opNorm.includes('avg')
        ? 'average'
        : opNorm.includes('max')
          ? 'maximum'
          : opNorm.includes('min')
            ? 'minimum'
            : opNorm.includes('median')
              ? 'median'
              : opNorm.includes('sum') || opNorm.includes('total')
                ? 'total'
                : opNorm.includes('count')
                  ? 'count of'
                  : 'aggregated';
  const rankedMetricLabel = opNorm.includes('count_filtered') && details?.filter_desc
    ? statLabel
    : `${statLabel} ${formatCol(valueCol || dimHeader)}`;

  return (
    <div
      className="arc-table-container"
      data-ranking-requested-count={requestedN || undefined}
      data-ranking-computed-count={ranking?.computed_count ?? entries.length}
      data-ranking-serialized-count={ranking?.serialized_count ?? entries.length}
      data-ranking-rendered-count={displayEntries.length}
    >
      <div className="arc-record-header" style={{ marginBottom: '8px' }}>
        <span className="arc-record-tag">
          {rankingLabel} {dimHeader}
        </span>
        <span className="arc-record-title">
          Ranked by {rankedMetricLabel}
        </span>
      </div>
      {requestedN > totalRows && (
        <div className="arc-record-title" role="note" style={{ marginBottom: '8px' }}>
          {(() => {
            const eligible = Number(eligibleCount);
            const exhausted = Number.isFinite(eligible) && eligible <= totalRows;
            if (exhausted) {
              return `Found ${totalRows.toLocaleString()} eligible ${dimHeader.toLowerCase()}(s); all ${totalRows.toLocaleString()} are shown because fewer than ${requestedN} are available.`;
            }
            return Number.isFinite(eligible)
              ? `${eligible.toLocaleString()} eligible ${dimHeader.toLowerCase()}(s) in this dataset`
              : `${totalRows.toLocaleString()} of ${requestedN} requested`;
          })()}
        </div>
      )}
      <div className="arc-table-scroll">
        <table className="arc-table" aria-label={`${dimHeader} ranked by ${statLabel} ${metricHeader}`}>
          <thead>
            <tr>
              <th scope="col" className="arc-th arc-th-rank" style={{ width: '60px' }}>Rank</th>
              <th scope="col" className="arc-th arc-th-category">{dimHeader}</th>
              <th scope="col" className="arc-th arc-th-num">{metricHeader}</th>
            </tr>
          </thead>
          <tbody>
            {displayEntries.map(([cat, val, rank]) => (
              <tr key={rank} className="arc-tr">
                <td className="arc-td arc-td-rank" style={{ fontWeight: 600 }}>#{rank}</td>
                <td className="arc-td arc-td-category" title={String(cat)}>
                  {cat || '—'}
                </td>
                <td className="arc-td arc-td-num">
                  {formatNumber(val, isAverage)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {isTruncated && (
        <button
          type="button"
          className="arc-table-toggle"
          onClick={onToggleExpand}
          aria-expanded={expanded}
        >
          {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
          <span>
            {expanded
              ? `Collapse to first 10 rows`
              : `Showing 10 of ${totalRows} rows · Show all ${totalRows}`}
          </span>
        </button>
      )}
    </div>
  );
}

// ── Sub-component: 2D Grouped Matrix Table ───────────────────────────────────

function Grouped2DTable({
  data,
  dimensionCol,
  isAverage,
  details,
  expanded,
  onToggleExpand,
}) {
  const primaryKeys = useMemo(() => Object.keys(data || {}), [data]);
  const secondaryKeys = useMemo(() => {
    const keys = new Set();
    Object.values(data || {}).forEach(sub => {
      if (typeof sub === 'object' && sub !== null) {
        Object.keys(sub).forEach(k => keys.add(k));
      }
    });
    return Array.from(keys);
  }, [data]);

  const totalRows = primaryKeys.length;
  const isTruncated = totalRows > 10;
  const displayKeys = isTruncated && !expanded ? primaryKeys.slice(0, 10) : primaryKeys;

  const dimHeader = formatCol(dimensionCol || 'Category');

  return (
    <div className="arc-table-container">
      <div className="arc-table-scroll">
        <table className="arc-table" aria-label={dimHeader}>
          <thead>
            <tr>
              <th scope="col" className="arc-th arc-th-category">{dimHeader}</th>
              {secondaryKeys.map(k => (
                <th key={k} scope="col" className="arc-th arc-th-num">{formatCol(k)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {displayKeys.map(pk => (
              <tr key={pk} className="arc-tr">
                <td className="arc-td arc-td-category" title={String(pk)}>
                  {pk || '—'}
                </td>
                {secondaryKeys.map(sk => {
                  const val = data[pk]?.[sk];
                  return (
                    <td key={sk} className="arc-td arc-td-num">
                      {formatNumber(val, isAverage)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {isTruncated && (
        <button
          type="button"
          className="arc-table-toggle"
          onClick={onToggleExpand}
          aria-expanded={expanded}
        >
          {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
          <span>
            {expanded
              ? `Collapse to first 10 rows`
              : `Showing 10 of ${totalRows} rows · Show all ${totalRows}`}
          </span>
        </button>
      )}
    </div>
  );
}

// ── Sub-component: Record List Table ─────────────────────────────────────────

function RecordListTable({ records, expanded, onToggleExpand }) {
  if (!Array.isArray(records) || records.length === 0) return null;

  const cols = useMemo(() => {
    return Object.keys(records[0]).filter(c => !c.startsWith('_') && c !== 'source_row_number');
  }, [records]);

  const totalRows = records.length;
  const isTruncated = totalRows > 10;
  const displayRecords = isTruncated && !expanded ? records.slice(0, 10) : records;

  return (
    <div className="arc-table-container">
      <div className="arc-table-scroll">
        <table className="arc-table" aria-label="Analytical Records">
          <thead>
            <tr>
              {cols.map(c => (
                <th key={c} scope="col" className="arc-th">{formatCol(c)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {displayRecords.map((r, idx) => (
              <tr key={idx} className="arc-tr">
                {cols.map(c => (
                  <td key={c} className="arc-td">{formatValue(r[c])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {isTruncated && (
        <button
          type="button"
          className="arc-table-toggle"
          onClick={onToggleExpand}
          aria-expanded={expanded}
        >
          {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
          <span>
            {expanded
              ? `Collapse to first 10 rows`
              : `Showing 10 of ${totalRows} rows · Show all ${totalRows}`}
          </span>
        </button>
      )}
    </div>
  );
}
