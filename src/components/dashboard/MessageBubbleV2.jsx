/**
 * MessageBubbleV2 — Main AI message renderer.
 *
 * Changes (UX Correction Pass):
 *   - Structured tabular rendering support via AnalyticalResultCard for grouped/tabular metrics
 *   - Dense inline prose clean-up: displays clean lead summary when structured table is present
 *   - Duplicate hollow EvidenceCard suppression when analytical results are rendered
 *   - Contextual actions in VerticalActionMenu (⋮) beside Copy button
 *   - Full markdown table and prose support preserved
 */
import { useState, memo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

const markdownComponents = {
  table: ({ node, ...props }) => (
    <div className="v2-table-wrap">
      <table {...props} />
    </div>
  ),
  th: ({ node, style, ...props }) => (
    <th style={{ ...style, textAlign: 'left' }} {...props} />
  ),
  td: ({ node, style, ...props }) => (
    <td style={{ ...style, textAlign: 'left' }} {...props} />
  ),
};
import { motion } from 'framer-motion';
import { Globe, ExternalLink, FileSpreadsheet, Copy, Check, AlertTriangle } from 'lucide-react';
import PlexisLogo from '../brand/PlexisLogo';
import Tooltip from '../ui/Tooltip';
import EvidenceCard from './EvidenceCard.jsx';
import SpreadsheetResultCard from './SpreadsheetResultCard.jsx';
import AnalyticalResultCard from './AnalyticalResultCard.jsx';
import VerticalActionMenu from './VerticalActionMenu.jsx';
import TransformPlanCard from './TransformPlanCard.jsx';
import TransformResultCard from './TransformResultCard.jsx';
import TransformationClarificationCard from './TransformationClarificationCard.jsx';
import { getDisplayProse } from '../../utils/analyticalFormatting.js';

function MessageBubbleV2({
  message,
  onRetry,
  onApplyTransform,
  onCancelTransform,
  onClarificationAction,
  onUndoTransform,
  onExportDataset,
}) {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(typeof message.text === 'string' ? message.text : '');
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  /* ── User messages ── */
  if (message.sender === 'user') {
    if (message.isFileCard) {
      return (
        <motion.div
          className="v2-msg-file-card"
          initial={{ opacity: 0, scale: 0.96 }}
          animate={{ opacity: 1, scale: 1 }}
        >
          <div className="v2-msg-file-icon">
            <FileSpreadsheet size={20} />
          </div>
          <div>
            <div className="v2-msg-file-name">{message.fileName}</div>
            <div className="v2-msg-file-size">{message.fileSize}</div>
          </div>
        </motion.div>
      );
    }

    return (
      <div className="v2-msg-user-wrap">
        <div className="v2-msg-user-bubble" style={{ whiteSpace: 'pre-wrap' }}>
          {message.text}
        </div>
        <div className="v2-msg-actions v2-msg-actions-right">
          <Tooltip label="Copy message" side="top">
            <button type="button" className="v2-copy-btn" onClick={handleCopy}>
              {copied ? <Check size={14} color="#10b981" /> : <Copy size={14} />}
            </button>
          </Tooltip>
        </div>
      </div>
    );
  }

  /* ── System Error Messages ── */
  if (message.sender === 'system_error') {
    if (message.isUploadLimitError) {
      return (
        <motion.div
          className="v2-msg-system-error v2-msg-upload-limit"
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.2 }}
        >
          <div
            className="v2-error-limit-card glass-panel"
            style={{
              padding: '18px 22px',
              borderRadius: '12px',
              border: '1px solid rgba(234, 179, 8, 0.25)',
              background: 'rgba(234, 179, 8, 0.04)',
              maxWidth: '640px',
              lineHeight: '1.65',
              color: 'var(--text)',
            }}
          >
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '8px',
                marginBottom: '10px',
                color: '#eab308',
                fontWeight: '600',
                fontSize: '0.9rem',
              }}
            >
              <AlertTriangle size={16} />
              <span>Free-Tier Limit Notice</span>
            </div>
            <div className="v2-prose-container" style={{ fontSize: '0.92rem' }}>
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                {message.text}
              </ReactMarkdown>
            </div>
          </div>
        </motion.div>
      );
    }

    return (
      <motion.div
        className="v2-msg-system-error"
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.2 }}
      >
        <div className="v2-error-compact">
          <AlertTriangle size={14} className="v2-error-icon-compact" />
          <span className="v2-error-text-compact">
            {message.text || 'Please check your internet connection and try again.'}
          </span>
          {message.retryPayload && (
            <button
              type="button"
              className="v2-error-retry-compact"
              onClick={() => onRetry?.(message)}
            >
              Retry
            </button>
          )}
        </div>
      </motion.div>
    );
  }

  /* ── Transformation Clarification ── */
  if (message.transformationClarification) {
    return (
      <div className="v2-msg-ai">
        <div className="v2-msg-ai-avatar v2-msg-ai-avatar--gradient">
          <PlexisLogo width={17} height={17} />
        </div>
        <div className="v2-msg-ai-body">
          <TransformationClarificationCard
            clarification={message.transformationClarification}
            onAction={onClarificationAction}
          />
        </div>
      </div>
    );
  }

  /* ── Transformation Plan ── */
  if (message.transformPlan) {
    return (
      <div className="v2-msg-ai">
        <div className="v2-msg-ai-avatar v2-msg-ai-avatar--gradient">
          <PlexisLogo width={17} height={17} />
        </div>
        <div className="v2-msg-ai-body">
          {message.text && (
            <div className="v2-prose" style={{ marginBottom: '12px' }}>
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                {message.text}
              </ReactMarkdown>
            </div>
          )}
          <TransformPlanCard
            plan={message.transformPlan}
            datasetId={message.datasetId}
            onApply={onApplyTransform}
            onCancel={onCancelTransform}
            transformationUpdate={message.transformationUpdate}
          />
        </div>
      </div>
    );
  }

  /* ── Transformation Result ── */
  if (message.transformResult) {
    return (
      <div className="v2-msg-ai">
        <div className="v2-msg-ai-avatar v2-msg-ai-avatar--gradient">
          <PlexisLogo width={17} height={17} />
        </div>
        <div className="v2-msg-ai-body">
          {message.text && (
            <div className="v2-prose" style={{ marginBottom: '12px' }}>
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                {message.text}
              </ReactMarkdown>
            </div>
          )}
          <TransformResultCard
            result={message.transformResult}
            datasetId={message.datasetId}
            onUndo={onUndoTransform}
            onExport={onExportDataset}
          />
        </div>
      </div>
    );
  }

  /* ── AI messages ── */
  const isStreaming = !!message.isStreaming;
  const hasContent = typeof message.text === 'string' && message.text.length > 0;

  // Resolve analytical result safely from either message.result or direct evidence
  let effectiveResult = null;
  if (message.result) {
    effectiveResult = message.result;
  } else if (message.evidence && !Array.isArray(message.evidence) && message.evidence.value != null && message.evidence.operation) {
    effectiveResult = message.evidence;
  } else if (Array.isArray(message.evidence) && message.evidence[0]?.value != null && message.evidence[0]?.operation) {
    effectiveResult = message.evidence[0];
  }

  const effectiveAnalysisId = message.analysisId || effectiveResult?.analysis_id || null;
  const responseActions = message.responseActions || null;

  // Detect whether a structured analytical table is rendering
  let hasStructuredTable = false;
  if (effectiveResult) {
    const val = effectiveResult.value;
    if (effectiveResult.type === 'grouped' || effectiveResult.operation?.toLowerCase?.().startsWith('group_')) {
      hasStructuredTable = true;
    } else if (typeof val === 'object' && val !== null && !Array.isArray(val) && Object.keys(val).length > 0) {
      hasStructuredTable = true;
    } else if (Array.isArray(val) && val.length > 0 && typeof val[0] === 'object') {
      hasStructuredTable = true;
    }
  }

  // Contextual actions availability for VerticalActionMenu (⋮)
  const hasAnalyticalActions = !!(effectiveAnalysisId && responseActions);

  // Suppress duplicate EvidenceCard if the same analytical operation is already rendered
  let shouldRenderEvidenceCard = !isStreaming && Boolean(message.evidence);
  if (shouldRenderEvidenceCard && effectiveAnalysisId && effectiveResult) {
    if (message.evidence.analysis_id === effectiveAnalysisId) {
      shouldRenderEvidenceCard = false;
    } else if (Array.isArray(message.evidence) && message.evidence.some(e => e?.analysis_id === effectiveAnalysisId)) {
      shouldRenderEvidenceCard = false;
    } else if (!message.evidence.preview_rows?.length && !message.evidence.locator && !['outliers', 'missing', 'duplicates', 'collection'].includes(message.evidence.type)) {
      shouldRenderEvidenceCard = false;
    }
  }

  const displayProse = getDisplayProse(message.text, hasStructuredTable);

  return (
    <motion.div
      className="v2-msg-ai"
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4 }}
    >
      {/* ── Plexis gradient avatar ── */}
      <div className="v2-msg-ai-avatar v2-msg-ai-avatar--gradient">
        <PlexisLogo width={17} height={17} />
      </div>

      <div className="v2-msg-ai-body">
        {/*
          When streaming and no content yet, show nothing here —
          ChatAreaV2's AIThinkingIndicator is already shown.
          When streaming with content, render accumulated markdown.
          When done streaming / non-streaming, render full markdown.
        */}
        {hasContent && (
          <div className="v2-prose">
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
              {displayProse}
            </ReactMarkdown>
          </div>
        )}

        {/* Web search badge + citations */}
        {message.source === 'SOURCE_WEB_SEARCH' && (
          <div className="v2-msg-meta">
            <span className="v2-badge-web">
              <Globe size={12} /> Web Verified
            </span>
            {message.provider && (
              <span className="v2-msg-provider">{message.provider}</span>
            )}
            {message.sources?.length > 0 && (
              <div className="v2-msg-citations">
                {message.sources.slice(0, 4).map((url, idx) => {
                  let domain;
                  try {
                    domain = new URL(url).hostname.replace('www.', '');
                  } catch {
                    domain = `Link ${idx + 1}`;
                  }
                  return (
                    <a
                      key={idx}
                      href={url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="v2-citation"
                    >
                      {domain} <ExternalLink size={10} />
                    </a>
                  );
                })}
              </div>
            )}
          </div>
        )}

        {/* Evidence Card — only shown for diagnostic findings (outliers, missing, etc.), not duplicate analytical results */}
        {shouldRenderEvidenceCard && (
          <EvidenceCard evidence={message.evidence} />
        )}

        {/* Analytical Result Card — structured table or scalar metric */}
        {!isStreaming && effectiveAnalysisId && effectiveResult && (
          <AnalyticalResultCard
            analysisId={effectiveAnalysisId}
            result={effectiveResult}
          />
        )}

        {/* Spreadsheet Result Card */}
        {!isStreaming && message.spreadsheetCard && (
          <SpreadsheetResultCard
            card={message.spreadsheetCard}
            isError={message.spreadsheetCard?.isError}
          />
        )}

        {/* ── Message footer: Copy + VerticalActionMenu ── */}
        {!isStreaming && (
          <div className="v2-msg-actions">
            <Tooltip label="Copy AI response" side="top">
              <button type="button" className="v2-copy-btn" onClick={handleCopy}>
                {copied ? <Check size={14} color="#10b981" /> : <Copy size={14} />}
              </button>
            </Tooltip>

            {/* Vertical ⋮ menu — only rendered when analytical actions are available */}
            {hasAnalyticalActions && (
              <VerticalActionMenu
                analysisId={effectiveAnalysisId}
                responseActions={responseActions}
                result={effectiveResult}
              />
            )}
          </div>
        )}
      </div>
    </motion.div>
  );
}

export default memo(MessageBubbleV2);
