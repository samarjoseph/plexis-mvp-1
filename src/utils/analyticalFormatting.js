/**
 * analyticalFormatting.js — Helper functions for formatting analytical results,
 * numbers, columns, and operations.
 */

const SYSTEM_COLS = new Set([
  '_row_number', 'dataframe_index', 'source_row_number', 'result_rank',
  'index', 'level_0', 'unnamed: 0'
]);

export function extractClientContextFields(row, targetCol = null, maxFields = 8) {
  if (!row || typeof row !== 'object') return {};
  const targetNorm = (targetCol || '').trim().toLowerCase();

  const entityHints = [
    'name', 'airline', 'carrier', 'customer', 'product', 'category', 'type',
    'status', 'city', 'state', 'country', 'region', 'origin', 'destination',
    'dest', 'model', 'make', 'brand', 'department', 'title', 'contract', 'service'
  ];
  const idHints = ['id', 'code', 'number', 'num', 'key', 'flight'];
  const temporalHints = ['date', 'time', 'year', 'month', 'day', 'timestamp'];

  const suppressedKeys = new Set();
  const composed = [];

  // Check for composite hour + minute columns (e.g. Scheduled_Departure_Hour & Scheduled_Departure_Minute)
  let hourKey = null;
  let minKey = null;
  for (const k of Object.keys(row)) {
    const kl = k.toLowerCase().replace(/[_\s]+/g, '_');
    if (kl.includes('delay')) continue;
    if (kl.includes('hour') && (kl.includes('sched') || kl.includes('dep') || kl.includes('arr'))) {
      hourKey = k;
    } else if (kl.includes('minute') && (kl.includes('sched') || kl.includes('dep') || kl.includes('arr'))) {
      minKey = k;
    }
  }

  if (hourKey && minKey && row[hourKey] != null && row[minKey] != null && row[hourKey] !== '' && row[minKey] !== '') {
    const h = parseInt(row[hourKey], 10);
    const m = parseInt(row[minKey], 10);
    if (!Number.isNaN(h) && !Number.isNaN(m)) {
      const kl = hourKey.toLowerCase();
      let label = 'Scheduled Time';
      if (kl.includes('sched') && kl.includes('dep')) label = 'Scheduled Departure Time';
      else if (kl.includes('dep')) label = 'Departure Time';
      else if (kl.includes('arr')) label = 'Arrival Time';

      const timeStr = `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
      composed.push({ k: label, v: timeStr, score: 105 });
      suppressedKeys.add(hourKey);
      suppressedKeys.add(minKey);
    }
  }

  const entries = Object.entries(row).filter(([k, v]) => {
    if (suppressedKeys.has(k)) return false;
    const kNorm = k.toLowerCase().trim();
    if (SYSTEM_COLS.has(kNorm) || k.startsWith('_')) return false;
    if (targetNorm && kNorm === targetNorm) return false;
    if (v === null || v === undefined || v === '') return false;
    return true;
  });

  const scored = composed.concat(entries.map(([k, v]) => {
    const kLow = k.toLowerCase().replace(/[_\s]+/g, '_');
    let score = 0;
    if (entityHints.some(h => kLow.includes(h))) {
      score += 100;
    }
    if (['flight_number', 'flight_no', 'order_id', 'customer_id', 'product_id', 'transaction_id', 'patient_id'].some(h => kLow.includes(h))) {
      score += 95;
    } else if (idHints.some(h => kLow.includes(h))) {
      score += 85;
    } else if (temporalHints.some(h => kLow.includes(h))) {
      score += 75;
    }

    if (typeof v === 'string' && isNaN(Number(v))) {
      score += 20;
    } else if (typeof v === 'number') {
      score += 5;
    }
    return { k, v, score };
  }));

  scored.sort((a, b) => b.score - a.score);
  const result = {};
  for (const item of scored.slice(0, maxFields)) {
    result[item.k] = item.v;
  }
  return result;
}

export function formatCol(col) {
  if (!col) return '';
  return String(col)
    .replace(/[_-]+/g, ' ')
    .trim()
    .replace(/\b\w/g, c => c.toUpperCase());
}

export function formatNumber(val, isAverage = false) {
  if (val === null || val === undefined) return '—';
  if (typeof val === 'number') {
    if (!Number.isFinite(val)) return String(val);
    if (Number.isInteger(val)) return val.toLocaleString();
    if (isAverage) {
      return val.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    // General float
    return val.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 });
  }
  return String(val);
}

export function formatValue(value) {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'number') {
    if (Number.isFinite(value) && value % 1 === 0) return value.toLocaleString();
    return value.toLocaleString(undefined, { maximumFractionDigits: 4 });
  }
  if (Array.isArray(value)) return value.map(String).join(', ');
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

export function opLabel(op) {
  const labels = {
    max: 'Maximum', min: 'Minimum', mean: 'Average', median: 'Median',
    sum: 'Total', std: 'Std Dev', variance: 'Variance', range: 'Range',
    count: 'Count', nunique: 'Unique Count', mode: 'Mode',
    top_n: 'Top N', bottom_n: 'Bottom N',
    filter: 'Filter', sort: 'Sort',
    duplicates: 'Duplicates', missing_values: 'Missing Values',
    unique_values: 'Unique Values', outliers: 'Outliers',
    frequency: 'Frequency', quantile: 'Quantile',
    correlation: 'Correlation', covariance: 'Covariance',
    locate_rows: 'Located Rows', group_by: 'Group By',
    group_sum: 'Total', group_mean: 'Average', group_count: 'Count',
    group_min: 'Minimum', group_max: 'Maximum', group_median: 'Median',
  };
  return labels[op?.toLowerCase?.()] || op || 'Result';
}

export function deriveMetricHeader(operation, valueCol, fallbackCol, details) {
  const col = valueCol || fallbackCol;
  const op = (operation || '').toLowerCase();
  const label = opLabel(op);

  if (op.includes('count_filtered')) {
    if (details?.filter_desc) {
      return `Count (${details.filter_desc})`;
    }
    return col ? `Count (${formatCol(col)} > 0)` : 'Count';
  }
  if (op.includes('mean') || op.includes('avg')) {
    return col ? `Average ${formatCol(col)}` : 'Average';
  }
  if (op.includes('sum') || op.includes('total')) {
    return col ? `Total ${formatCol(col)}` : 'Total';
  }
  if (op.includes('count')) {
    return col ? `Count of ${formatCol(col)}` : 'Count';
  }
  if (op.includes('min')) {
    return col ? `Min ${formatCol(col)}` : 'Minimum';
  }
  if (op.includes('max')) {
    return col ? `Max ${formatCol(col)}` : 'Maximum';
  }
  if (op.includes('median')) {
    return col ? `Median ${formatCol(col)}` : 'Median';
  }
  if (op.includes('freq')) {
    return col ? `Frequency (${formatCol(col)})` : 'Frequency';
  }
  return col ? `${label} · ${formatCol(col)}` : label;
}

export function deriveDimensionHeader(details, fallbackCol) {
  let raw = '';
  if (Array.isArray(details?.group_by) && details.group_by.length > 0) {
    raw = details.group_by[0];
  } else if (typeof details?.group_by === 'string' && details.group_by.trim()) {
    raw = details.group_by.trim();
  } else if (details?.dimension) {
    raw = details.dimension;
  } else if (fallbackCol) {
    raw = fallbackCol;
  }
  return formatCol(raw) || 'Category';
}

export function getOrderedGroupedEntries(value, details = {}, ranking = null) {
  if (Array.isArray(ranking?.items)) {
    return ranking.items.map((entry, index) => [entry.entity, entry.value, entry.rank ?? index + 1]);
  }
  if (Array.isArray(details?.ordered_entries)) {
    return details.ordered_entries.map(entry => [entry.entity, entry.value, entry.rank]);
  }
  return Object.entries(value || {}).map(([entity, metric], index) => [entity, metric, index + 1]);
}

export function getRankingLabel(totalRows, requestedN, isAscending = false, eligibleCount = null) {
  const direction = isAscending ? 'Bottom' : 'Top';
  const requested = Number(requestedN) || 0;
  if (requested > 0 && totalRows < requested) {
    // If eligibleCount equals or is smaller than totalRows the dataset is exhausted:
    // we returned every available entity. Show 'All N' rather than 'N of M requested'.
    const eligible = Number(eligibleCount);
    if (Number.isFinite(eligible) && eligible <= totalRows) {
      return `All ${totalRows}`;
    }
    return `${direction} ${totalRows} of ${requested} requested`;
  }
  return `${direction} ${requested > 0 ? requested : totalRows}`;
}

export function getDisplayProse(text, hasStructuredTable) {
  if (!hasStructuredTable || typeof text !== 'string') return text;
  // Match standard breakdown pattern: "**Header** breakdown: **Key**: Val, ..."
  const breakdownMatch = text.match(/^(\*\*.*?\*\*|\w+.*?)\s+breakdown:\s+\*\*.*$/is);
  if (breakdownMatch) {
    const lead = breakdownMatch[1].trim();
    return lead.endsWith(':') ? lead : `${lead}:`;
  }
  return text;
}
