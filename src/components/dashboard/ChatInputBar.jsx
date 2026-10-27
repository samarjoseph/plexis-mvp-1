import React, { useState, useRef, useEffect, useCallback } from 'react';
import { motion } from 'framer-motion';
import { Plus, ArrowUp, X, FileSpreadsheet } from 'lucide-react';
import Tooltip from '../ui/Tooltip';
import { EventBus } from '../../events/EventBus';
import { Events } from '../../events/Events';

/**
 * ChatInputBar — Isolated, ultra-responsive chat input component.
 *
 * Responsiveness architecture:
 *   1. Local, urgent synchronous state for draft text. Keystrokes never bubble up to
 *      Dashboard or trigger message list re-renders.
 *   2. Synchronous textarea auto-resizing directly in the input event handler without
 *      layout thrashing or full-chat reflows.
 *   3. Subscribes to PREFILL_CHAT_INPUT and CLEAR_CHAT_INPUT via EventBus for external
 *      triggers (e.g. welcome cards, action buttons) without parent re-renders.
 *   4. Supports Enter to send, Shift+Enter for multiline, and staged file submission.
 */
export default function ChatInputBar({
  onSend,
  stagedFile,
  onStageFile,
  onCancelFile,
  onFileSizeError = null,
  isLoading,
  initialText = '',
}) {
  const [draftText, setDraftText] = useState(initialText);
  const textareaRef = useRef(null);
  const fileRef = useRef(null);

  // Synchronous height adjustment without forced full-page layout thrashing
  const adjustHeight = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    const newHeight = Math.min(Math.max(el.scrollHeight, 24), 160);
    el.style.height = `${newHeight}px`;
  }, []);

  // Subscribe to prefill and clear events from EventBus
  useEffect(() => {
    const unsubPrefill = EventBus.on(Events.PREFILL_CHAT_INPUT, (text) => {
      if (typeof text === 'string') {
        setDraftText(text);
        if (textareaRef.current) {
          textareaRef.current.focus();
          // Adjust height on next microtask after state update
          requestAnimationFrame(() => adjustHeight());
        }
      }
    });

    const unsubClear = EventBus.on(Events.CLEAR_CHAT_INPUT, () => {
      setDraftText('');
      if (textareaRef.current) {
        textareaRef.current.style.height = '24px';
      }
    });

    return () => {
      unsubPrefill();
      unsubClear();
    };
  }, [adjustHeight]);

  const handleChange = (e) => {
    // Urgent, synchronous local update
    setDraftText(e.target.value);
    adjustHeight();
  };

  const handleSendSubmit = () => {
    const trimmed = draftText.trim();
    if ((!trimmed && !stagedFile) || isLoading) return;

    // Dispatch submission with text and staged file
    onSend(draftText, stagedFile);

    // Immediately clear local input draft and reset height
    setDraftText('');
    if (textareaRef.current) {
      textareaRef.current.style.height = '24px';
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSendSubmit();
    }
  };

  const isSendDisabled = (!draftText.trim() && !stagedFile) || isLoading;

  return (
    <div className="v2-chat-input-area">
      <div className="v2-input-container">
        {stagedFile && (
          <div className="v2-staged-file">
            <FileSpreadsheet size={14} />
            <span>{stagedFile.name}</span>
            <button type="button" onClick={onCancelFile} aria-label="Remove file">
              <X size={14} />
            </button>
          </div>
        )}

        <div className="v2-input-row">
          <Tooltip label="Upload Dataset" shortcut="Ctrl+U" side="top">
            <button
              type="button"
              className="v2-attach-btn"
              onClick={() => fileRef.current?.click()}
              disabled={isLoading}
            >
              <Plus size={18} />
            </button>
          </Tooltip>

          <input
            id="dataset-upload-input"
            ref={fileRef}
            type="file"
            accept=".csv"
            hidden
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (!file) {
                e.target.value = '';
                return;
              }
              const MAX_CSV_MB = 35;
              if (file.name.endsWith('.csv') && file.size > MAX_CSV_MB * 1024 * 1024) {
                if (onFileSizeError) {
                  onFileSizeError(file, MAX_CSV_MB);
                }
                e.target.value = '';
                return;
              }
              if (file.name.endsWith('.csv') && onStageFile) {
                onStageFile(file);
              }
              e.target.value = '';
            }}
          />

          <textarea
            ref={textareaRef}
            className="v2-input-field"
            placeholder="Message Plexis..."
            rows={1}
            value={draftText}
            onChange={handleChange}
            onKeyDown={handleKeyDown}
            style={{ height: '24px' }}
          />

          <Tooltip label="Send Message" shortcut="⏎ Enter" side="top">
            <button
              type="button"
              className="v2-send-btn"
              disabled={isSendDisabled}
              onClick={(e) => {
                e.preventDefault();
                handleSendSubmit();
              }}
              aria-label="Send Message"
            >
              <ArrowUp size={16} />
            </button>
          </Tooltip>
        </div>
      </div>
    </div>
  );
}
