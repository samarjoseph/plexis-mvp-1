import React, { useMemo, useState } from 'react';
import { Check, X, Send, ListChecks } from 'lucide-react';

function literalLabel(literal) {
  if (!literal) return 'unspecified';
  const { value, value_type: type } = literal;
  if (type === 'null') return 'null';
  if (type === 'boolean') return `boolean(${String(value).toLowerCase()})`;
  if (type === 'integer') return `integer(${value})`;
  if (type === 'float') return `float(${value})`;
  if (type === 'string') return `string("${value}")`;
  return String(value ?? 'unspecified');
}

export default function TransformationClarificationCard({ clarification, onAction }) {
  const candidates = clarification?.candidate_columns || [];
  const needsColumnScope = clarification?.clarification_type === 'COLUMN_SCOPE';
  const isTypeMismatch = clarification?.clarification_type === 'VALUE_TYPE_MISMATCH';
  const isAmbiguity = clarification?.clarification_type === 'VALUE_AMBIGUITY' || clarification?.clarification_type === 'EXTREME_VALUE_AMBIGUITY' || clarification?.clarification_type === 'RANKING_AMBIGUITY' || (Array.isArray(clarification?.ambiguous_options) && clarification.ambiguous_options.length > 0);
  const isRanking = clarification?.clarification_type === 'RANKING_AMBIGUITY';
  const kickerLabel = clarification?.kicker || (isRanking ? 'RANKING INTENT CLARIFICATION' : 'TRANSFORMATION CLARIFICATION');
  
  const mismatchReq = clarification?.requested_value_info;
  const mismatchCand = clarification?.candidate_value_info;
  const ambiguousOptions = clarification?.ambiguous_options || [];

  const [selected, setSelected] = useState([]);
  const [customScope, setCustomScope] = useState('');
  const columnsLabel = useMemo(() => candidates.join(', '), [candidates]);
  const source = literalLabel(clarification?.source);
  const destination = literalLabel(clarification?.destination);

  const submit = (value, message, additionalPayload = {}) => onAction?.(message, {
    type: isRanking ? 'RANKING_CLARIFICATION' : 'TRANSFORMATION_CLARIFICATION',
    clarification_type: clarification?.clarification_type,
    transformation_id: clarification?.transformation_id,
    value,
    ...additionalPayload
  });

  const toggleColumn = (column) => setSelected((current) => (
    current.includes(column)
      ? current.filter((item) => item !== column)
      : [...current, column]
  ));

  return (
    <section className="tcc-card" aria-labelledby={`tcc-title-${clarification?.transformation_id || 'active'}`}>
      <header className="tcc-header">
        <div className="tcc-kicker"><ListChecks size={14} /> {kickerLabel}</div>
        <p className="tcc-question" id={`tcc-title-${clarification?.transformation_id || 'active'}`}>
          {clarification?.question || (isRanking ? 'How would you like to rank these records?' : 'Where should I apply this transformation?')}
        </p>
      </header>

      {!isTypeMismatch && !isAmbiguity && (clarification?.source || clarification?.destination) && (
        <div className="tcc-summary">
          {clarification?.source && <div><span>Source</span><code>{source}</code></div>}
          {clarification?.destination && <div><span>Destination</span><code>{destination}</code></div>}
        </div>
      )}
      
      {isTypeMismatch && mismatchReq && mismatchCand && (
        <div className="tcc-summary" style={{flexDirection: 'column', gap: '8px', alignItems: 'stretch'}}>
          <div style={{display: 'flex', justifyContent: 'space-between', paddingBottom: '4px', borderBottom: '1px solid #333'}}>
            <span>Requested</span><code>{literalLabel(mismatchReq)}</code>
          </div>
          <div style={{display: 'flex', justifyContent: 'space-between'}}>
            <span>Suggested</span><code style={{color: '#4ade80'}}>{literalLabel(mismatchCand)}</code>
          </div>
        </div>
      )}

      {isAmbiguity && ambiguousOptions.length > 0 && (
        <div className="tcc-candidates" style={{display: 'flex', flexDirection: 'column', gap: '8px'}}>
          {ambiguousOptions.map((opt, idx) => (
            <button
              key={idx}
              type="button"
              className="tcc-all-button"
              style={{justifyContent: 'flex-start', textAlign: 'left', padding: '10px 14px'}}
              onClick={() => {
                submit(opt.action || 'RESOLVE_AMBIGUITY', `I meant: ${opt.label}`, {
                  action: opt.action,
                  selected_action: opt.action,
                  chosen_operation: opt.operation,
                  resolved_value_info: opt.value_info
                });
              }}
            >
              <div style={{display: 'flex', flexDirection: 'column', gap: '2px'}}>
                <span style={{fontWeight: 600}}>{opt.label}</span>
                {opt.description && <span style={{fontSize: '12px', opacity: 0.85}}>{opt.description}</span>}
              </div>
              {opt.value_info && <code>{literalLabel(opt.value_info)}</code>}
            </button>
          ))}
        </div>
      )}

      {needsColumnScope && candidates.length > 0 && (
        <div className="tcc-candidates">
          <div className="tcc-section-label">Matching columns: {columnsLabel}</div>
          <button
            type="button"
            className="tcc-all-button"
            onClick={() => submit('ALL_MATCHING_COLUMNS', 'All matching columns')}
          >
            <Check size={15} /> All matching columns
          </button>
          <div className="tcc-column-list">
            {candidates.map((column) => (
              <label className="tcc-column-option" key={column}>
                <input
                  type="checkbox"
                  checked={selected.includes(column)}
                  onChange={() => toggleColumn(column)}
                />
                <span>{column}</span>
              </label>
            ))}
          </div>
          <button
            type="button"
            className="tcc-action-button"
            disabled={!selected.length}
            onClick={() => submit(selected, selected.join(' and '))}
          >
            Apply selected columns
          </button>
        </div>
      )}

      {isTypeMismatch && (
        <div className="tcc-candidates" style={{display: 'flex', gap: '8px'}}>
          <button
            type="button"
            className="tcc-all-button"
            style={{flex: 1, justifyContent: 'center'}}
            onClick={() => {
              onAction?.('yes, use the suggested value', {
                type: 'TRANSFORMATION_CLARIFICATION',
                clarification_type: clarification?.clarification_type,
                transformation_id: clarification?.transformation_id,
                value: 'CONTINUE',
                candidate_value_info: mismatchCand,
              });
            }}
          >
            <Check size={15} /> Continue
          </button>
        </div>
      )}

      <form
        className="tcc-custom-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (customScope.trim()) onAction?.(customScope.trim(), null);
        }}
      >
        <input
          value={customScope}
          onChange={(event) => setCustomScope(event.target.value)}
          placeholder="Custom scope, e.g. all numeric columns"
          aria-label="Custom transformation scope"
        />
        <button type="submit" disabled={!customScope.trim()} aria-label="Send custom scope">
          <Send size={14} />
        </button>
      </form>

      <footer className="tcc-footer">
        <span>{needsColumnScope ? 'Values stay typed as shown; only the target scope is missing.' : clarification?.operation}</span>
        <button type="button" className="tcc-cancel" onClick={() => submit('CANCEL', 'Cancel transformation')}>
          <X size={14} /> Cancel
        </button>
      </footer>
    </section>
  );
}
