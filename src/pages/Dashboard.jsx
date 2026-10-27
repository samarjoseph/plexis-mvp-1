import React, { useState, useEffect, useCallback, useRef } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { PlexisAPI } from '../api';
import { useAuth } from '../context/AuthContext';
import { useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts';
import {
  loadConversations, saveConversations, createConversation,
  updateConversation, deleteConversation, renameConversation, togglePinConversation,
} from '../utils/conversationStorage';
import { extractResponseText, extractMetadata } from '../utils/responseHelpers';
import { usePlexisStream } from '../utils/streaming/index.js';
import { WorkspaceProvider, useWorkspace } from '../context/WorkspaceContext.jsx';
import { EventBus } from '../events/EventBus.js';
import { Events } from '../events/Events.js';
import { EvidenceRegistry } from '../evidence/EvidenceRegistry.js';
import SidebarV2 from '../components/dashboard/SidebarV2';
import ChatAreaV2 from '../components/dashboard/ChatAreaV2';
import AnalyticsPanelV2 from '../components/dashboard/AnalyticsPanelV2';
import SettingsPanelV2 from '../components/dashboard/SettingsPanelV2';
import DatasetWorkspace from '../components/workspace/DatasetWorkspace.jsx';
import LocateResultSelector from '../components/dashboard/LocateResultSelector.jsx';
import '../styles/dashboard.css';
import '../styles/workspace.css';

// Inner component — must be inside WorkspaceProvider to use useWorkspace
function DashboardInner() {
  const { user } = useAuth();
  const userId = user?.id || 'anonymous';
  const { isOpen: workspaceOpen, setDataset, buildWorkspaceSummaryPayload } = useWorkspace();

  // ── State ──────────────────────────────────────────────────
  const [conversations, setConversations] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [sidebarMobileOpen, setSidebarMobileOpen] = useState(false);
  const [analyticsCollapsed, setAnalyticsCollapsed] = useState(true);
  const [searchQuery, setSearchQuery] = useState('');
  const [inputText, setInputText] = useState('');
  const [stagedFile, setStagedFile] = useState(null);
  const [loadingChatIds, setLoadingChatIds] = useState(() => new Set());
  const isLoading = activeId ? loadingChatIds.has(activeId) : false;
  const [chartType, setChartType] = useState('bar');
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [streamingMessageId, setStreamingMessageId] = useState(null);
  const streamingTargetIdRef = useRef(null);
  // Track the most recent successful spreadsheet operation card.
  // Follow-up explanation messages (explain_evidence) reuse this card so
  // their Locate/Undo/Redo buttons work via the same SPREADSHEET_CARD_ACTION pipeline.
  const lastSpreadsheetCardRef = useRef(null);
  const uploadStream = usePlexisStream();
  // Spreadsheet panel state
  const [workspacePct, setWorkspacePct]     = useState(48);  // % of split area
  const [viewMode,     setViewMode]         = useState('side_by_side'); // 'side_by_side' | 'popup'
  const prevSidebarRef = useRef(false); // remember sidebar state before workspace opened
  // LocateResultSelector state — shown when ANALYSIS_LOCATE_MULTI fires
  const [locateSelector, setLocateSelector] = useState(null); // {analysisId, rows, column, value}

  const activeConversation = conversations.find((c) => c.id === activeId) || null;

  // ── Load from storage ──────────────────────────────────────
  useEffect(() => {
    const stored = loadConversations(userId);
    if (stored.length > 0) {
      setConversations(stored);
      setActiveId(stored[0].id);
    }
  }, [userId]);

  // ── Persist to storage ─────────────────────────────────────
  useEffect(() => {
    if (conversations.length > 0) saveConversations(userId, conversations);
  }, [conversations, userId]);

  // ── Sync active dataset with WorkspaceContext ──────────
  useEffect(() => {
    const datasetId = activeConversation?.datasetId;
    if (datasetId) {
      setDataset(datasetId, activeId);
    }
  }, [activeConversation?.datasetId, activeId, setDataset]);

  // ── Auto-collapse sidebar when workspace opens ──────────
  useEffect(() => {
    if (workspaceOpen) {
      prevSidebarRef.current = sidebarCollapsed;
      setSidebarCollapsed(true);
    } else {
      // Restore sidebar when workspace closes
      setSidebarCollapsed(prevSidebarRef.current);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceOpen]);

  // ── Subscribe to workspace action events ───────────────────
  // When user right-clicks "Explain this row", auto-send as chat message.
  // Also handles 'ask_chat' — conversational fallback from the spreadsheet agent.
  useEffect(() => {
    const unsub = EventBus.on(Events.WORKSPACE_ACTION_REQUESTED, (action) => {
      const actionType = action.type;
      let prefilledText = action.prefilledQuestion;

      if (!prefilledText) {
        if (actionType === 'explain_row' && action.row_data) {
          const preview = Object.entries(action.row_data)
            .slice(0, 4)
            .map(([k, v]) => `${k}=${v}`)
            .join(', ');
          prefilledText = `Explain this row: ${preview}`;
        } else if (actionType === 'ask_about_rows') {
          prefilledText = `Explain the ${action.row_indices?.length || 'selected'} selected rows`;
        } else if (actionType === 'explain_evidence') {
          prefilledText = action.prefilledQuestion || 'Explain this result.';
        }
      }

      if (prefilledText) {
        if (actionType === 'ask_chat') {
          // Pure conversational query from the spreadsheet agent — send directly
          // to normal chat WITHOUT a workspace_action payload (no row/evidence context needed)
          handleSend(prefilledText, null, null, {});
        } else if (actionType === 'explain_evidence') {
          // EXPLAIN_EVIDENCE: merge the lastSpreadsheetCard operation context into the action
          // so the backend has the ACTUAL computed result to explain (operation type, column,
          // stats, rowIndices) — not just the bare text description.
          const card = lastSpreadsheetCardRef.current;
          const enrichedAction = {
            ...action,
            // Operation result data from the last successful spreadsheet operation
            operation_context: card ? {
              operation_type:  card.operation?.type || null,
              column:          card.operation?.column || null,
              n:               card.operation?.n || null,
              result_value:    card.result?.columnStats?.result ?? card.result?.columnStats?.max ?? card.result?.columnStats?.min ?? null,
              row_count:       card.result?.rowIndices?.length ?? null,
              row_indices:     (card.result?.rowIndices || []).slice(0, 10),
              stats:           card.result?.columnStats || card.result?.stats || null,
              result_summary:  card.result?.resultSummary || null,
            } : null,
          };
          handleSend(prefilledText, null, null, {
            workspaceAction: enrichedAction,
            workspace_action: enrichedAction,
          });
        } else {
          // Data-contextual workspace action — send with workspace context
          handleSend(prefilledText, null, null, {
            workspaceAction: action,
            workspace_action: action,
          });
        }
      }
    });
    return unsub;
  }, [activeConversation, isLoading]);

  // ── Subscribe to Spreadsheet Agent chat sync ──────────────────
  // When Spreadsheet Agent processes a query, inject the interaction into
  // the main chat as a user message + AI response pair (no second API call).
  // The aiMsg carries a `spreadsheetCard` payload so MessageBubbleV2 can
  // render a polished result card with Locate / Undo / Redo buttons.
  useEffect(() => {
    // Track how many spreadsheet operations have been injected — used to
    // compute canUndo / canRedo for the card buttons.
    let ssOpCount = 0;

    const unsub = EventBus.on(Events.SPREADSHEET_AGENT_CHAT_MESSAGE,
      ({ query, answer, operation, result, source, operationId, isError }) => {
        const currentConv = conversations.find((c) => c.id === activeId);
        if (!currentConv) return;

        const ts = Date.now();

        const userMsg = {
          id: `sa-user-${ts}`,
          sender: 'user',
          text: query,
          fromSpreadsheetAgent: true,
        };

        // Compute canUndo/canRedo based on op sequence
        if (!isError && result?.success) ssOpCount += 1;
        const cardCanUndo = ssOpCount > 0;
        const cardCanRedo = false; // redo stack only valid before new ops

        const cardData = {
          operation,
          result,
          source,
          operationId,
          isError,
          errorText: isError ? answer : null,
          canUndo: cardCanUndo,
          canRedo: cardCanRedo,
        };

        // Save the card so follow-up explanation messages can reference it
        if (!isError && result?.success) {
          lastSpreadsheetCardRef.current = cardData;
        }

        const aiMsg = {
          id: `sa-ai-${ts + 1}`,
          sender: isError ? 'system_error' : 'ai',
          text: answer,
          fromSpreadsheetAgent: true,
          source: 'spreadsheet_agent',
          // Attach the full card data for SpreadsheetResultCard rendering
          spreadsheetCard: cardData,
        };

        setConversations((prev) =>
          prev.map((c) =>
            c.id === activeId
              ? { ...c, messages: [...(c.messages || []), userMsg, aiMsg] }
              : c
          )
        );
      }
    );
    return unsub;
  }, [activeId, conversations]);

  // ── Subscribe to ANALYSIS_* events ────────────────────────────────────────
  useEffect(() => {
    const unsubLocateSingle = EventBus.on(Events.ANALYSIS_LOCATE_SINGLE, ({ source_row_number }) => {
      // Single row → direct navigate (0-based index)
      EventBus.emit(Events.NAVIGATE_TO_ROW, {
        rowIndex: source_row_number - 1,
        highlight: true,
      });
    });

    const unsubLocateMulti = EventBus.on(Events.ANALYSIS_LOCATE_MULTI, (payload) => {
      setLocateSelector(payload);
    });

    const unsubExplain = EventBus.on(Events.ANALYSIS_EXPLAIN_REQUESTED, async ({ analysisId }) => {
      try {
        const data = await PlexisAPI.explainAnalysis(analysisId);
        const text = data.explanation || `The ${data.operation?.toLowerCase()} of ${data.column} is ${data.verified_value}.`;
        const ts = Date.now();
        const aiMsg = {
          id: `explain-${ts}`,
          sender: 'ai',
          text,
          source: 'analysis_explain',
        };
        setConversations((prev) =>
          prev.map((c) =>
            c.id === activeId
              ? { ...c, messages: [...(c.messages || []), aiMsg] }
              : c
          )
        );
      } catch (err) {
        console.error('[Dashboard] explain failed:', err);
      }
    });

    const unsubRetry = EventBus.on(Events.ANALYSIS_RETRY_REQUESTED, async ({ analysisId }) => {
      try {
        const data = await PlexisAPI.retryAnalysis(analysisId);
        const ts = Date.now();
        const aiMsg = {
          id: `retry-${ts}`,
          sender: 'ai',
          text: `Retried the analysis. New result: ${data.value !== undefined ? data.value : 'N/A'}`,
          analysisId: data.analysis_id,
          result: {
            type: 'scalar_with_rows',
            value: data.value,
            operation: data.operation,
            column: data.column,
            row_count: data.row_indices?.length || 0,
            verified: data.verified,
            rows: (data.matching_rows || []).map((r, i) => ({
              ...r,
              source_row_number: r._row_number ?? (data.row_indices?.[i] ?? i) + 1,
            })),
          },
          responseActions: { retry: true, explain: true, locate: true },
          source: 'analysis_retry',
        };
        setConversations((prev) =>
          prev.map((c) =>
            c.id === activeId
              ? { ...c, messages: [...(c.messages || []), aiMsg] }
              : c
          )
        );
      } catch (err) {
        console.error('[Dashboard] retry failed:', err);
      }
    });

    return () => {
      unsubLocateSingle();
      unsubLocateMulti();
      unsubExplain();
      unsubRetry();
    };
  }, [activeId]);

  // ── Helpers ────────────────────────────────────────────────
  const handleNewChat = () => {
    setActiveId(null);
    setInputText('');
    EventBus.emit(Events.CLEAR_CHAT_INPUT);
    setStagedFile(null);
    setSidebarMobileOpen(false);
  };

  const handleSelectChat = (id) => {
    setActiveId(id);
    setSidebarMobileOpen(false);
  };

  // ── Send Message ───────────────────────────────────────────
  async function handleSend(retryOrText = null, retryOrFile = null, errorMsgIdToRemove = null, extraOptions = {}) {
    const isRetry = Boolean(errorMsgIdToRemove || extraOptions.isRetry);
    const runtimeText = extraOptions.message ?? (typeof retryOrText === 'string' ? retryOrText : inputText);
    const runtimeFile = retryOrFile !== null ? retryOrFile : stagedFile;

    if (!runtimeText?.trim() && !runtimeFile) return;

    // Lazy conversation creation
    let currentConv = activeConversation;
    let targetId = activeId;

    if (!currentConv) {
      currentConv = createConversation();
      currentConv.messages = [];
      targetId = currentConv.id;
      setConversations((prev) => [currentConv, ...prev]);
      setActiveId(targetId);
    }

    let updatedMessages = currentConv.messages;
    if (errorMsgIdToRemove) {
      updatedMessages = updatedMessages.filter(m => m.id !== errorMsgIdToRemove);
    }

    if (!isRetry) {
      const userMessages = [];
      if (runtimeFile) {
        userMessages.push({
          id: `file-${Date.now()}`,
          sender: 'user',
          isFileCard: true,
          fileName: runtimeFile.name,
          fileSize: `${(runtimeFile.size / 1024).toFixed(1)} KB`,
        });
      }
      if (runtimeText.trim()) {
        userMessages.push({ id: `text-${Date.now()}`, sender: 'user', text: runtimeText });
      }
      updatedMessages = [...updatedMessages, ...userMessages];
      setInputText('');
      setStagedFile(null);
    }

    // Prevent sending another message on this specific chat while its response is generating
    if (targetId && loadingChatIds.has(targetId)) return;

    setConversations((prev) => updateConversation(prev, targetId, { messages: updatedMessages }));
    setLoadingChatIds((prev) => new Set(prev).add(targetId));

    // Dynamic AI title generation (only once at the start of conversation)
    const shouldNameChat = (!currentConv.named && (currentConv.title === 'New Chat' || !currentConv.title));
    if (shouldNameChat) {
      currentConv.named = true;
      setConversations((prev) => updateConversation(prev, targetId, { named: true }));
      PlexisAPI.generateChatTitle({
        message: runtimeText,
        filename: runtimeFile?.name || '',
      }).then((aiTitle) => {
        if (aiTitle && aiTitle !== 'New Chat') {
          setConversations((prev) =>
            updateConversation(prev, targetId, { title: aiTitle, named: true })
          );
        }
      }).catch((err) => {
        console.warn('[Dashboard] Title generation failed:', err);
      });
    }

    // ── File Upload → SSE Streaming Path ────────────────────────
    if (runtimeFile) {
      const streamMsgId = `ai-stream-${Date.now()}`;
      streamingTargetIdRef.current = targetId;

      const placeholderMsg = { id: streamMsgId, sender: 'ai', text: '', isStreaming: true };
      setStreamingMessageId(streamMsgId);
      setConversations((prev) =>
        updateConversation(prev, targetId, { messages: [...updatedMessages, placeholderMsg] })
      );

      uploadStream.reset();

      try {
        const response = await PlexisAPI.uploadCSVStream(runtimeFile, runtimeText);
        if (!response.ok) {
          let errBody = {};
          try { errBody = await response.json(); } catch (_) {}
          const isFileTooLarge = response.status === 413 || errBody?.error_code === 'FILE_TOO_LARGE';
          const limitMb = errBody?.limit_mb || 35;
          const userText = isFileTooLarge
            ? (errBody?.user_message || `**Need to upload a larger dataset?**\n\nPlexis is currently in its free development phase, with CSV uploads limited to ${limitMb} MB.\n\nWe're working toward expanded upload limits on our upcoming paid plans. We apologize for the inconvenience and appreciate your patience as we continue improving Plexis.`)
            : (errBody?.error || errBody?.message || "Upload failed. Please try again.");

          setStreamingMessageId(null);
          setLoadingChatIds((prev) => {
            const next = new Set(prev);
            next.delete(targetId);
            return next;
          });
          setConversations((prev) =>
            updateConversation(prev, targetId, {
              messages: [
                ...(prev.find(c => c.id === targetId)?.messages.filter(m => m.id !== streamMsgId) ?? []),
                {
                  id: `err-${Date.now()}`,
                  sender: 'system_error',
                  isUploadLimitError: isFileTooLarge,
                  text: userText,
                  ...(!isFileTooLarge && { retryPayload: { text: runtimeText, file: runtimeFile } }),
                },
              ],
            })
          );
          return;
        }
        let accumulatedText = '';
        const { StreamController } = await import('../utils/streaming/StreamController.js');
        const controller = new StreamController();

        controller
          .on('analyzing', ({ status }) => {
            console.debug('[Plexis] Analyzing:', status);
          })
          .on('module_ready', () => {
            // Module cards no longer shown in UI per RFC v1.2.
            // Backend analysis continues as private LLM context only.
          })
          .on('chunk', ({ text: chunk }) => {
            if (!chunk) return;
            accumulatedText += chunk;
            setConversations((prev) =>
              updateConversation(prev, streamingTargetIdRef.current, {
                messages: prev
                  .find(c => c.id === streamingTargetIdRef.current)
                  ?.messages.map(m => m.id === streamMsgId ? { ...m, text: accumulatedText } : m) ?? [],
              })
            );
          })
          .on('done', (data) => {
            const datasetInfo = data?.dataset_info || {};
            const datasetId = data?.dataset_id || null;
            const nextDatasetName = runtimeFile.name;
            const nextDatasetStats = datasetInfo.row_count
              ? { rows: datasetInfo.row_count, columns: datasetInfo.column_count, memory: datasetInfo.memory_usage_bytes }
              : null;

            // Notify workspace and EvidenceRegistry of new dataset
            if (datasetId) {
              setDataset(datasetId, streamingTargetIdRef.current);
              EventBus.emit(Events.DATASET_LOADED, { datasetId, filename: nextDatasetName });
              EvidenceRegistry.setCurrentFingerprint(datasetInfo.schema_fingerprint || datasetId);
            }

            // Auto-open Investigation workspace after upload
            // ARCHITECTURE: Upload → Investigation tab opens with deterministic stats
            if (datasetId && data?.trigger_investigation) {
              EventBus.emit(Events.INVESTIGATION_REQUESTED, { datasetId });
              setAnalyticsCollapsed(false);
            }

            setStreamingMessageId(null);
            setLoadingChatIds((prev) => {
              const next = new Set(prev);
              next.delete(targetId);
              return next;
            });
            setConversations((prev) =>
              updateConversation(prev, targetId, {
                messages: prev
                  .find(c => c.id === targetId)
                  ?.messages.map(m => m.id === streamMsgId ? { ...m, text: accumulatedText, isStreaming: false } : m) ?? [],
                activeDatasetName: nextDatasetName,
                datasetName: nextDatasetName,
                datasetId,
                ...(nextDatasetStats && { datasetStats: nextDatasetStats }),
                title: prev.find(c => c.id === targetId)?.title || `Dataset: ${runtimeFile.name}`,
              })
            );
          })
          .on('error', ({ message }) => {
            setStreamingMessageId(null);
            setLoadingChatIds((prev) => {
              const next = new Set(prev);
              next.delete(targetId);
              return next;
            });
            const errMsg = {
              id: `err-${Date.now()}`,
              sender: 'system_error',
              text: message || "Couldn't stream the dataset presentation. Please try again.",
              retryPayload: { text: runtimeText, file: runtimeFile },
            };
            setConversations((prev) =>
              updateConversation(prev, streamingTargetIdRef.current, {
                messages: [
                  ...(prev.find(c => c.id === streamingTargetIdRef.current)?.messages.filter(m => m.id !== streamMsgId) ?? []),
                  errMsg,
                ],
              })
            );
          });

        await controller.consume(response);
      } catch (err) {
        console.error('Upload stream error:', err);
        setStreamingMessageId(null);
        setLoadingChatIds((prev) => {
          const next = new Set(prev);
          next.delete(targetId);
          return next;
        });
        setConversations((prev) =>
          updateConversation(prev, targetId, {
            messages: [...updatedMessages, {
              id: `err-${Date.now()}`,
              sender: 'system_error',
              text: "Plexis couldn't process the upload. Please try again.",
              retryPayload: { text: runtimeText, file: runtimeFile },
            }],
          })
        );
      }
      return;
    }

    // ── Regular Chat → Synchronous Path ─────────────────────────
    try {
      // Build workspace state snapshot for context injection
      const workspaceState = buildWorkspaceSummaryPayload();
      const workspaceAction = extraOptions.workspaceAction || null;

      const apiResult = await PlexisAPI.askQuestion(
        runtimeText,
        currentConv.datasetId || currentConv.activeDatasetName || '',
        {
          workspaceState: workspaceOpen ? workspaceState : null,
          workspaceAction,
          sessionId: activeId,
          chatId: currentConv.chatId || null,   // ← DB chat UUID for operation persistence
        },
      );

      const responseText = extractResponseText(apiResult);
      const responseMeta = extractMetadata(apiResult);

      // Store auto-created DB chat_id so subsequent queries pass it to the backend
      if (apiResult?.chat_id && !currentConv.chatId) {
        setConversations((prev) =>
          prev.map((c) =>
            c.id === targetId
              ? { ...c, chatId: apiResult.chat_id }
              : c
          )
        );
      }
      const evidence = apiResult?.evidence || null;

      // Register evidence if present
      if (evidence?.id) {
        EvidenceRegistry.register(evidence);
      }

      let nextChartData = currentConv.chartData;
      if (apiResult?.chart_data) {
        nextChartData = {
          labels: apiResult.chart_data.labels,
          datasets: [{
            label: apiResult.chart_data.metric_label || 'Dataset Trace',
            data: apiResult.chart_data.values,
            borderColor: '#3b82f6',
            backgroundColor: 'rgba(59, 130, 246, 0.6)',
            borderWidth: 2,
          }],
        };
      }

      // For explain_evidence follow-up messages: reuse the last spreadsheet card
      // so Locate/Undo/Redo buttons are functional via the same SPREADSHEET_CARD_ACTION pipeline.
      // Only attach if there is an active lastSpreadsheetCard AND this is an explain-type action.
      const isExplainFollowUp = extraOptions.workspaceAction?.type === 'explain_evidence';
      const inheritedCard = isExplainFollowUp && lastSpreadsheetCardRef.current
        ? { ...lastSpreadsheetCardRef.current, source: 'follow_up' }
        : null;

      const aiMessage = {
        id: `ai-${Date.now()}`,
        sender: 'ai',
        text: responseText,
        hasChart: !!(apiResult?.chart_data),
        source: responseMeta.source,
        provider: responseMeta.provider,
        sources: responseMeta.sources,
        evidence,  // EvidenceReference | null — rendered by EvidenceCard in ChatAreaV2
        // Analytical result fields (from enriched /api/ask response)
        analysisId: apiResult?.analysis_id || null,
        responseActions: apiResult?.response_actions || null,
        result: apiResult?.result || null,
        // Inherit the spreadsheet card for follow-up explain messages
        ...(inheritedCard && { spreadsheetCard: inheritedCard }),
        // Transformation plan — set by TransformEngine
        ...(apiResult?.transform_plan && {
          transformPlan: apiResult.transform_plan,
          datasetId: currentConv.datasetId || '',
        }),
        ...(apiResult?.transformation_clarification && {
          transformationClarification: apiResult.transformation_clarification,
        }),
        ...(apiResult?.transformation_update && {
          transformationUpdate: apiResult.transformation_update,
        }),
      };

      // Open Investigation tab if backend flags it
      if (apiResult?.trigger_investigation && currentConv.datasetId) {
        EventBus.emit(Events.INVESTIGATION_REQUESTED, { datasetId: currentConv.datasetId });
        setAnalyticsCollapsed(false);
      }

      // Emit evidence event if present
      if (evidence) {
        EventBus.emit(Events.CHAT_MESSAGE_WITH_EVIDENCE, {
          messageId: aiMessage.id,
          evidence,
        });
      }

      setConversations((prev) =>
        updateConversation(prev, targetId, {
          messages: [...updatedMessages, aiMessage],
          chartData: nextChartData,
          title: prev.find(c => c.id === targetId)?.title || currentConv.title,
        })
      );
    } catch (err) {
      console.error('Plexis API Error:', err);
      setConversations((prev) =>
        updateConversation(prev, targetId, {
          messages: [...updatedMessages, {
            id: `err-${Date.now()}`,
            sender: 'system_error',
            text: "Plexis couldn't reach the server. Please check your internet connection.",
            retryPayload: { text: runtimeText, file: runtimeFile },
          }],
        })
      );
    } finally {
      setLoadingChatIds((prev) => {
        const next = new Set(prev);
        next.delete(targetId);
        return next;
      });
    }
  };

  const handleFileSizeError = useCallback((file, limitMb = 35) => {
    const errMsg = {
      id: `err-file-size-${Date.now()}`,
      sender: 'system_error',
      isUploadLimitError: true,
      text: `**Need to upload a larger dataset?**\n\nPlexis is currently in its free development phase, with CSV uploads limited to ${limitMb} MB.\n\nWe're working toward expanded upload limits on our upcoming paid plans. We apologize for the inconvenience and appreciate your patience as we continue improving Plexis.`,
    };
    setConversations((prev) =>
      updateConversation(prev, activeId, {
        messages: [...(prev.find((c) => c.id === activeId)?.messages ?? []), errMsg],
      })
    );
  }, [activeId]);

  const handleRetry = (msg) => {
    if (msg.retryPayload) handleSend(msg.retryPayload.text, msg.retryPayload.file, msg.id, { isRetry: true });
  };

  // ── Transform Handlers ─────────────────────────────────────
  const [isApplyingTransform, setIsApplyingTransform] = useState(false);

  const handleApplyTransform = useCallback(async (planId, datasetId) => {
    setIsApplyingTransform(true);
    try {
      const result = await PlexisAPI.applyTransformation(planId, datasetId);
      const ts = Date.now();
      if (result.error) {
        const errText = result.conflict
          ? '⚠️ The dataset changed after this plan was created. Please ask again to regenerate the plan.'
          : `Transformation failed: ${result.error}`;
        setConversations((prev) =>
          updateConversation(prev, activeId, {
            messages: [...(prev.find(c => c.id === activeId)?.messages ?? []),
              { id: `tf-err-${ts}`, sender: 'ai', text: errText }],
          })
        );
        return;
      }
      // Add TransformResultCard message
      setConversations((prev) =>
        updateConversation(prev, activeId, {
          messages: [...(prev.find(c => c.id === activeId)?.messages ?? []),
            {
              id: `tf-result-${ts}`,
              sender: 'ai',
              transformResult: result,
              datasetId,
            }],
        })
      );
      EventBus.emit(Events.TRANSFORM_APPLIED, { datasetId, result });
      PlexisAPI.startInvestigation(datasetId, { refresh: true })
        .then(() => EventBus.emit(Events.INVESTIGATION_REFRESHED, { datasetId }))
        .catch((error) => console.warn('[investigation refresh after transform]', error));
    } catch (e) {
      console.error('[handleApplyTransform]', e);
    } finally {
      setIsApplyingTransform(false);
    }
  }, [activeId]);

  const handleCancelTransform = useCallback(() => {
    const ts = Date.now();
    setConversations((prev) =>
      updateConversation(prev, activeId, {
        messages: [...(prev.find(c => c.id === activeId)?.messages ?? []),
          { id: `tf-cancel-${ts}`, sender: 'ai', text: 'Transformation cancelled.' }],
      })
    );
  }, [activeId]);

  const handleUndoTransform = useCallback(async (datasetId) => {
    try {
      const result = await PlexisAPI.undoTransformation(datasetId);
      const ts = Date.now();
      setConversations((prev) =>
        updateConversation(prev, activeId, {
          messages: [...(prev.find(c => c.id === activeId)?.messages ?? []),
            {
              id: `tf-undo-${ts}`,
              sender: 'ai',
              text: result.success ? `↩️ ${result.message}` : `Undo failed: ${result.error}`,
            }],
        })
      );
      if (result.success) {
        EventBus.emit(Events.TRANSFORM_UNDONE, { datasetId, result });
        PlexisAPI.startInvestigation(datasetId, { refresh: true })
          .then(() => EventBus.emit(Events.INVESTIGATION_REFRESHED, { datasetId }))
          .catch((error) => console.warn('[investigation refresh after undo]', error));
      }
    } catch (e) {
      console.error('[handleUndoTransform]', e);
    }
  }, [activeId]);

  const handleExportDataset = useCallback(async (datasetId) => {
    try {
      await PlexisAPI.exportDataset(datasetId);
    } catch (e) {
      console.error('[handleExportDataset]', e);
    }
  }, []);

  // ── Keyboard Shortcuts ─────────────────────────────────────
  useKeyboardShortcuts([
    { key: 'o', mod: true, shift: true, action: handleNewChat },
    { key: 'a', mod: true, shift: true, action: (e) => {
        e.preventDefault();
        setAnalyticsCollapsed((v) => !v);
    }},
    { key: 'k', mod: true, action: (e) => {
        e.preventDefault();
        document.getElementById('chat-search-input')?.focus();
    }},
    { key: 'u', mod: true, action: (e) => {
        e.preventDefault();
        document.getElementById('dataset-upload-input')?.click();
    }},
    { key: 'b', mod: true, action: (e) => {
        e.preventDefault();
        setSidebarCollapsed((v) => !v);
    }},
  ]);

  // ── Render ─────────────────────────────────────────────────
  return (
    <motion.div
      className={`v2-dashboard ${workspaceOpen ? 'v2-dashboard--workspace-open' : ''}`}
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.4 }}
    >
      {/* Mobile sidebar overlay */}
      <div
        className={`v2-sidebar-overlay ${sidebarMobileOpen ? 'visible' : ''}`}
        onClick={() => setSidebarMobileOpen(false)}
      />

      <SidebarV2
        collapsed={sidebarCollapsed}
        mobileOpen={sidebarMobileOpen}
        onToggleCollapse={() => setSidebarCollapsed((v) => !v)}
        onCloseMobile={() => setSidebarMobileOpen(false)}
        conversations={conversations}
        activeId={activeId}
        searchQuery={searchQuery}
        onSearchChange={setSearchQuery}
        onNewChat={handleNewChat}
        onSelectChat={handleSelectChat}
        onRenameChat={(id, title) => setConversations((p) => renameConversation(p, id, title))}
        onDeleteChat={(id) => {
          const next = deleteConversation(conversations, id);
          setConversations(next);
          if (activeId === id) setActiveId(next.length > 0 ? next[0].id : null);
        }}
        onTogglePin={(id) => setConversations((p) => togglePinConversation(p, id))}
        onOpenSettings={() => setIsSettingsOpen(true)}
      />

      {/* Chat + Workspace split pane */}
      <div className="v2-chat-workspace-split">
        <ChatAreaV2
          messages={activeConversation?.messages || []}
          inputText={inputText}
          onInputChange={setInputText}
          onSend={handleSend}
          onRetry={handleRetry}
          stagedFile={stagedFile}
          onStageFile={setStagedFile}
          onCancelFile={() => setStagedFile(null)}
          onFileSizeError={handleFileSizeError}
          isLoading={isLoading}
          activeDatasetName={activeConversation?.activeDatasetName}
          activeDatasetId={activeConversation?.datasetId}
          activeSessionId={activeId}
          onOpenSidebar={() => setSidebarMobileOpen(true)}
          sidebarCollapsed={sidebarCollapsed}
          onToggleSidebar={() => setSidebarCollapsed((v) => !v)}
          analyticsCollapsed={analyticsCollapsed}
          onToggleAnalytics={() => setAnalyticsCollapsed((v) => !v)}
          onApplyTransform={handleApplyTransform}
          onCancelTransform={handleCancelTransform}
          onClarificationAction={(message, workspaceAction) =>
            handleSend(null, null, null, { message, workspaceAction })
          }
          onUndoTransform={handleUndoTransform}
          onExportDataset={handleExportDataset}
          isApplyingTransform={isApplyingTransform}
        />

        {/* DatasetWorkspace panel — side-by-side mode */}
        {workspaceOpen && viewMode === 'side_by_side' && (
          <>
            {/* Drag divider */}
            <div
              className="v2-split-divider"
              onMouseDown={e => {
                e.preventDefault();
                const container = e.currentTarget.parentElement;
                const onMove = mv => {
                  const rect = container.getBoundingClientRect();
                  const pct = Math.round(((rect.right - mv.clientX) / rect.width) * 100);
                  setWorkspacePct(Math.min(75, Math.max(25, pct)));
                };
                const onUp = () => {
                  window.removeEventListener('mousemove', onMove);
                  window.removeEventListener('mouseup', onUp);
                  document.body.style.cursor = '';
                  document.body.style.userSelect = '';
                };
                document.body.style.cursor = 'col-resize';
                document.body.style.userSelect = 'none';
                window.addEventListener('mousemove', onMove);
                window.addEventListener('mouseup', onUp);
              }}
              title="Drag to resize"
            />
            <div
              className="v2-workspace-pane"
              style={{ width: `${workspacePct}%`, flexShrink: 0, height: '100%' }}
            >
              <DatasetWorkspace
                viewMode={viewMode}
                onViewModeChange={setViewMode}
              />
            </div>
          </>
        )}
      </div>

      {/* DatasetWorkspace panel — popup mode */}
      <AnimatePresence>
        {workspaceOpen && viewMode === 'popup' && (
          <motion.div
            className="v2-workspace-popup"
            initial={{ opacity: 0, scale: 0.96 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.96 }}
            transition={{ duration: 0.22, ease: 'easeOut' }}
          >
            <DatasetWorkspace
              viewMode={viewMode}
              onViewModeChange={setViewMode}
            />
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {!analyticsCollapsed && (
          <AnalyticsPanelV2
            chartData={activeConversation?.chartData}
            chartType={chartType}
            onChartTypeChange={setChartType}
            collapsed={analyticsCollapsed}
            onToggleCollapse={() => setAnalyticsCollapsed(!analyticsCollapsed)}
            datasetName={activeConversation?.activeDatasetName}
            datasetStats={activeConversation?.datasetStats}
            activeDatasetId={activeConversation?.datasetId || null}
          />
        )}
      </AnimatePresence>

      <SettingsPanelV2
        isOpen={isSettingsOpen}
        onClose={() => setIsSettingsOpen(false)}
      />

      {/* Locate Result Selector — shown when Locate finds multiple matching rows */}
      <AnimatePresence>
        {locateSelector && (
          <div className="locate-selector-overlay">
            <LocateResultSelector
              analysisId={locateSelector.analysisId}
              rows={locateSelector.rows}
              column={locateSelector.column}
              value={locateSelector.value}
              onClose={() => setLocateSelector(null)}
            />
          </div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

// Outer component wraps with WorkspaceProvider
export default function Dashboard() {
  return (
    <WorkspaceProvider>
      <DashboardInner />
    </WorkspaceProvider>
  );
}
