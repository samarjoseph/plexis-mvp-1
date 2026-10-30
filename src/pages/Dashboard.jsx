import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
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

function isUUID(str) {
  return typeof str === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(str);
}

// Inner component — must be inside WorkspaceProvider to use useWorkspace
function DashboardInner() {
  const { chatRef } = useParams();
  const navigate = useNavigate();
  const { user } = useAuth();
  const userId = user?.id || 'anonymous';
  const { isOpen: workspaceOpen, setDataset, buildWorkspaceSummaryPayload } = useWorkspace();

  // ── State ──────────────────────────────────────────────────
  const [conversations, setConversations] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [isChatRouteLoading, setIsChatRouteLoading] = useState(() => Boolean(chatRef));
  const prevChatRef = useRef(chatRef);

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
  const internalChatNavRef = useRef(null);
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

  // ── Load from storage & Remote DB ───────────────────────────
  useEffect(() => {
    let isMounted = true;
    const stored = loadConversations(userId);
    if (stored.length > 0) {
      setConversations(stored);
      if (chatRef) {
        const matched = stored.find(
          (c) => c.slug === chatRef || c.shareSlug === chatRef || c.id === chatRef || c.chatId === chatRef
        );
        setActiveId(matched ? matched.id : null);
      } else {
        setActiveId(null);
      }
    } else {
      setConversations([]);
      setActiveId(null);
    }

    async function syncRemoteChats() {
      try {
        const res = await PlexisAPI.listChats();
        if (!isMounted || !res || !Array.isArray(res.chats)) return;

        const storedMap = new Map((stored || []).map((c) => [c.chatId || c.id, c]));

        const remoteConvs = res.chats.map((chat) => {
          const cached = storedMap.get(chat.id);
          const hasDbTitle = chat.title && chat.title !== 'New Chat';
          const hasCachedTitle = cached?.title && cached.title !== 'New Chat';
          const finalTitle = hasDbTitle ? chat.title : (hasCachedTitle ? cached.title : (chat.title || 'New Chat'));

          // Auto-heal DB title if local cache had generated AI title but DB had 'New Chat'
          if (!hasDbTitle && hasCachedTitle) {
            PlexisAPI.renameChat(chat.id, cached.title).catch((e) =>
              console.warn('[Dashboard] Auto-healing chat title in DB:', e)
            );
          }

          const isolationSlug = chat.slug || chat.share_slug || (chat.account_tag && chat.chat_tag ? `${chat.account_tag}-${chat.chat_tag}` : cached?.slug) || chat.id;

          return {
            id: chat.id,
            chatId: chat.id,
            slug: isolationSlug,
            shareSlug: isolationSlug,
            accountTag: chat.account_tag || cached?.accountTag || null,
            chatTag: chat.chat_tag || cached?.chatTag || null,
            title: finalTitle,
            pinned: cached?.pinned || false,
            datasetId: chat.current_dataset_id || cached?.datasetId || null,
            activeDatasetName: cached?.activeDatasetName || (chat.current_dataset_id ? 'Active Dataset' : 'No active dataset'),
            datasetName: cached?.datasetName || (chat.current_dataset_id ? 'Active Dataset' : null),
            messages: cached?.messages?.length ? cached.messages : [],
            chartData: cached?.chartData || { labels: [], datasets: [] },
            createdAt: chat.created_at ? new Date(chat.created_at).getTime() : Date.now(),
            updatedAt: chat.last_message_at ? new Date(chat.last_message_at).getTime() : Date.now(),
            named: finalTitle !== 'New Chat',
          };
        });

        // Retain un-synced local legacy conversations (never had a DB chatId or UUID)
        const localOnly = (stored || []).filter(
          (c) =>
            !c.chatId &&
            !isUUID(c.id) &&
            c.messages &&
            c.messages.some((m) => m.sender === 'user')
        );

        const initialMerged = [...remoteConvs, ...localOnly];
        if (initialMerged.length > 0) {
          setConversations((prev) => {
            return initialMerged.map((remote) => {
              const existing = prev.find((c) => c.id === remote.id || c.chatId === remote.id);
              if (existing && existing.messages && existing.messages.length > remote.messages.length) {
                return { ...remote, messages: existing.messages };
              }
              return remote;
            });
          });
          saveConversations(userId, initialMerged);

          // If chatRef is in URL, activate matching chat; otherwise keep activeId null (home page)
          if (chatRef) {
            const matched = initialMerged.find(
              (c) => c.slug === chatRef || c.shareSlug === chatRef || c.id === chatRef || c.chatId === chatRef
            );
            setActiveId(matched ? matched.id : null);
          } else {
            setActiveId(null);
          }
        } else if (res.chats.length === 0 && localOnly.length === 0) {
          setConversations([]);
          saveConversations(userId, []);
          setActiveId(null);
        }

        // Migrate local-only chats to PostgreSQL in the background
        if (localOnly.length > 0) {
          for (const localChat of localOnly) {
            if (!isMounted) break;
            const messagesToUpload = (localChat.messages || [])
              .filter((m) => m.id !== 'welcome')
              .map((m) => ({
                role: m.sender === 'user' ? 'user' : (m.sender === 'system_error' ? 'system' : 'assistant'),
                content: m.text || '',
                message_type: 'conversation',
                metadata: {
                  has_chart: m.hasChart || false,
                  chart_data: m.chartData || null,
                  evidence: m.evidence || null,
                  isFileCard: m.isFileCard || false,
                  fileName: m.fileName || null,
                  fileSize: m.fileSize || null,
                },
              }));

            try {
              const created = await PlexisAPI.createChat({
                title: localChat.title || 'New Chat',
                dataset_id: localChat.datasetId || null,
                messages: messagesToUpload,
              });
              if (created?.chat_id && isMounted) {
                const oldId = localChat.id;
                const newId = created.chat_id;
                localChat.id = newId;
                localChat.chatId = newId;
                localChat.slug = created.slug;

                setConversations((prev) => {
                  const updated = prev.map((item) =>
                    item.id === oldId
                      ? { ...item, id: newId, chatId: newId, slug: created.slug }
                      : item
                  );
                  saveConversations(userId, updated);
                  return updated;
                });
                setActiveId((prev) => (prev === oldId ? newId : prev));
              }
            } catch (migErr) {
              console.warn('[Dashboard] Error migrating local-only chat to DB:', migErr);
            }
          }
        }
      } catch (err) {
        console.warn('[Dashboard] Could not sync remote chats (using cached):', err);
      }
    }

    syncRemoteChats();
    return () => {
      isMounted = false;
    };
  }, [userId]);

  // ── Sync URL chatRef param to active chat & load direct links ─────────
  useEffect(() => {
    // 1. If this navigation was triggered internally by an active chat receiving a slug or ID:
    if (internalChatNavRef.current && (internalChatNavRef.current === chatRef || internalChatNavRef.current === activeId)) {
      internalChatNavRef.current = null;
      prevChatRef.current = chatRef;
      setIsChatRouteLoading(false);
      return;
    }

    const hadChatRef = Boolean(prevChatRef.current);
    prevChatRef.current = chatRef;

    if (!chatRef) {
      setIsChatRouteLoading(false);
      // ONLY reset activeId if the user explicitly navigated from a chat route back to /dashboard (e.g. browser back button)
      if (hadChatRef && activeId !== null) {
        setActiveId(null);
      }
      return;
    }

    // 2. If the user is ALREADY actively viewing this chat in memory:
    const currentActive = conversations.find((c) => c.id === activeId);
    const isAlreadyActive = currentActive && (
      currentActive.slug === chatRef ||
      currentActive.shareSlug === chatRef ||
      currentActive.chatId === chatRef ||
      currentActive.id === chatRef ||
      (!hadChatRef && currentActive.messages && currentActive.messages.length > 0)
    );

    if (isAlreadyActive) {
      setIsChatRouteLoading(false);
      return;
    }

    const MIN_LOAD_MS = 2400;
    const loadStart = Date.now();

    const target = conversations.find(
      (c) => c.slug === chatRef || c.shareSlug === chatRef || c.id === chatRef || c.chatId === chatRef
    );
    if (target) {
      if (activeId !== target.id) {
        setActiveId(target.id);
      }
      // Re-hydrate messages from backend to ensure multi-device & reload consistency
      PlexisAPI.loadChat(chatRef).then((fullChat) => {
        if (!fullChat?.messages?.length) return;
        const backendMsgs = fullChat.messages.map((m) => {
          const isUser = m.role === 'user';
          const meta = m.metadata || {};
          return {
            id: m.id || `msg-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
            sender: isUser ? 'user' : (m.role === 'system' ? 'system_error' : 'ai'),
            text: m.content || '',
            hasChart: Boolean(meta.evidence?.has_chart || meta.chart_data),
            chartData: meta.chart_data || null,
            evidence: meta.evidence || null,
            createdAt: m.created_at ? new Date(m.created_at).getTime() : Date.now(),
            ...(meta.isFileCard && {
              isFileCard: true,
              fileName: meta.fileName,
              fileSize: meta.fileSize,
            }),
          };
        });
        setConversations((prev) => {
          const c = prev.find((x) => x.id === target.id || x.chatId === target.id);
          if (!c) return prev;
          if (backendMsgs.length >= (c.messages?.length || 0)) {
            const next = updateConversation(prev, c.id, { messages: backendMsgs });
            saveConversations(userId, next);
            return next;
          }
          return prev;
        });
      }).catch((e) => console.warn('[Dashboard] Background loadChat sync error:', e));

      const elapsed = Date.now() - loadStart;
      const delay = Math.max(100, MIN_LOAD_MS - elapsed);
      const timer = setTimeout(() => {
        setIsChatRouteLoading(false);
      }, delay);
      return () => clearTimeout(timer);
    }

    // Direct link to chat not yet in local state -> load from backend
    let isMounted = true;
    setIsChatRouteLoading(true);

    async function loadDirectChat() {
      try {
        const fullChat = await PlexisAPI.loadChat(chatRef);
        if (!isMounted || !fullChat || !fullChat.chat) return;

        const backendMsgs = (fullChat.messages || []).map((m) => {
          const isUser = m.role === 'user';
          const meta = m.metadata || {};
          return {
            id: m.id || `msg-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
            sender: isUser ? 'user' : (m.role === 'system' ? 'system_error' : 'ai'),
            text: m.content || '',
            hasChart: Boolean(meta.evidence?.has_chart || meta.chart_data),
            chartData: meta.chart_data || null,
            evidence: meta.evidence || null,
            createdAt: m.created_at ? new Date(m.created_at).getTime() : Date.now(),
            ...(meta.isFileCard && {
              isFileCard: true,
              fileName: meta.fileName,
              fileSize: meta.fileSize,
            }),
          };
        });

        const newConv = {
          id: fullChat.chat.id,
          chatId: fullChat.chat.id,
          slug: fullChat.chat.slug || chatRef,
          shareSlug: fullChat.chat.slug || chatRef,
          title: fullChat.chat.title || 'New Chat',
          pinned: false,
          datasetId: fullChat.current_dataset_id || null,
          activeDatasetName: fullChat.current_dataset_id ? 'Active Dataset' : 'No active dataset',
          datasetName: fullChat.current_dataset_id ? 'Active Dataset' : null,
          messages: backendMsgs.length > 0 ? backendMsgs : [
            {
              id: 'welcome',
              sender: 'ai',
              text: '# Welcome to Plexis\nUpload a dataset or ask a question to begin your analysis session.',
              hasChart: false,
            },
          ],
          chartData: { labels: [], datasets: [] },
          createdAt: fullChat.chat.created_at ? new Date(fullChat.chat.created_at).getTime() : Date.now(),
          updatedAt: fullChat.chat.last_message_at ? new Date(fullChat.chat.last_message_at).getTime() : Date.now(),
          named: fullChat.chat.title !== 'New Chat',
        };

        setConversations((prev) => {
          const exists = prev.some((c) => c.id === newConv.id);
          const next = exists ? prev : [newConv, ...prev];
          saveConversations(userId, next);
          return next;
        });
        setActiveId(newConv.id);
      } catch (err) {
        console.warn('[Dashboard] Direct link load failed for', chatRef, err);
        navigate('/dashboard', { replace: true });
      } finally {
        const elapsed = Date.now() - loadStart;
        const delay = Math.max(100, MIN_LOAD_MS - elapsed);
        setTimeout(() => {
          if (isMounted) setIsChatRouteLoading(false);
        }, delay);
      }
    }

    loadDirectChat();
    return () => {
      isMounted = false;
    };
  }, [chatRef, conversations, activeId, userId, navigate]);

  // ── Load full messages for active chat from backend if not yet loaded ──
  useEffect(() => {
    if (!activeId) return;
    const conv = conversations.find((c) => c.id === activeId);
    if (!conv) return;

    // Check if we already have real messages loaded
    const hasRealMessages = conv.messages && conv.messages.some(
      (m) => m.sender === 'user' || (m.sender === 'ai' && m.id !== 'welcome')
    );
    if (hasRealMessages) return;

    const lookupTarget = conv.slug || conv.chatId || (isUUID(conv.id) ? conv.id : null);
    if (!lookupTarget) return;

    let isMounted = true;
    async function fetchChatMessages() {
      try {
        const fullChat = await PlexisAPI.loadChat(lookupTarget);
        if (!isMounted || !fullChat) return;

        const backendMsgs = (fullChat.messages || []).map((m) => {
          const isUser = m.role === 'user';
          const meta = m.metadata || {};
          return {
            id: m.id || `msg-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
            sender: isUser ? 'user' : 'ai',
            text: m.content || '',
            hasChart: Boolean(meta.evidence?.has_chart || meta.chart_data),
            chartData: meta.chart_data || null,
            evidence: meta.evidence || null,
            createdAt: m.created_at ? new Date(m.created_at).getTime() : Date.now(),
            ...(meta.isFileCard && {
              isFileCard: true,
              fileName: meta.fileName,
              fileSize: meta.fileSize,
            }),
          };
        });

        const finalMessages = backendMsgs.length > 0 ? backendMsgs : [
          {
            id: 'welcome',
            sender: 'ai',
            text: '# Welcome to Plexis\nUpload a dataset or ask a question to begin your analysis session.',
            hasChart: false,
          },
        ];

        setConversations((prev) => {
          const next = updateConversation(prev, activeId, {
            messages: finalMessages,
            chatId: fullChat.chat?.id || conv.chatId || activeId,
            datasetId: fullChat.current_dataset_id || conv.datasetId || null,
            title: fullChat.chat?.title || conv.title,
          });
          saveConversations(userId, next);
          return next;
        });
      } catch (err) {
        console.warn('[Dashboard] Could not load chat messages for', lookupTarget, err);
      }
    }

    fetchChatMessages();
    return () => {
      isMounted = false;
    };
  }, [activeId, conversations]);

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
    navigate('/dashboard');
  };

  const handleSelectChat = (id) => {
    if (id !== activeId) {
      setIsChatRouteLoading(true);
      setTimeout(() => {
        setIsChatRouteLoading(false);
      }, 2200);
    }
    setActiveId(id);
    setSidebarMobileOpen(false);
    const conv = conversations.find((c) => c.id === id);
    const targetSlug = conv?.slug || conv?.shareSlug || conv?.chatId || conv?.id;
    if (targetSlug) {
      navigate(`/dashboard/${targetSlug}`);
    } else {
      navigate('/dashboard');
    }
  };

  // ── Send Message ───────────────────────────────────────────
  async function handleSend(retryOrText = null, retryOrFile = null, errorMsgIdToRemove = null, extraOptions = {}) {
    const isRetry = Boolean(errorMsgIdToRemove || extraOptions.isRetry);
    const runtimeText = extraOptions.message ?? (typeof retryOrText === 'string' ? retryOrText : inputText);
    const runtimeFile = retryOrFile !== null ? retryOrFile : stagedFile;

    if (!runtimeText?.trim() && !runtimeFile) return;

    // 1. Resolve or immediately create optimistic conversation
    let currentConv = activeConversation;
    let targetId = activeId;

    if (!currentConv) {
      currentConv = createConversation();
      currentConv.messages = [];
      targetId = currentConv.id;
    }

    // 2. Build updated messages with user's new message(s)
    let updatedMessages = currentConv.messages || [];
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
      currentConv.messages = updatedMessages;

      // INSTANTLY clear input bar and staged file
      setInputText('');
      setStagedFile(null);
    }

    // Prevent sending another message on this specific chat while its response is generating
    if (targetId && loadingChatIds.has(targetId)) return;

    // 3. INSTANTLY transition UI to active chat mode (0ms latency)
    // Synchronously update activeId, loadingChatIds, and conversations so
    // showWelcome immediately flips to false and the chat view + message bubble + thinking spinner render.
    setActiveId(targetId);
    setLoadingChatIds((prev) => new Set(prev).add(targetId));
    setConversations((prev) => {
      const exists = prev.some((c) => c.id === targetId);
      const next = exists
        ? updateConversation(prev, targetId, { messages: updatedMessages })
        : [{ ...currentConv, messages: updatedMessages }, ...prev];
      saveConversations(userId, next);
      return next;
    });

    // 4. Ensure backend chat record exists (non-blocking for instantaneous 0ms UI transition)
    if (!currentConv.chatId && !isUUID(targetId)) {
      const initialTitle = currentConv.title || 'New Chat';
      PlexisAPI.createChat(initialTitle).then((created) => {
        if (created?.chat_id) {
          const newChatId = created.chat_id;
          const oldTargetId = targetId;
          targetId = newChatId;
          streamingTargetIdRef.current = newChatId;
          currentConv.chatId = newChatId;
          currentConv.id = newChatId;
          currentConv.slug = created.slug;
          currentConv.shareSlug = created.share_slug || created.slug;

          setConversations((prev) => {
            const next = prev.map((c) =>
              c.id === oldTargetId || c.id === newChatId
                ? { ...c, id: newChatId, chatId: newChatId, slug: created.slug, shareSlug: created.share_slug || created.slug }
                : c
            );
            saveConversations(userId, next);
            return next;
          });
          setActiveId((prev) => (prev === oldTargetId ? newChatId : prev));
          setLoadingChatIds((prev) => {
            const next = new Set(prev);
            next.delete(oldTargetId);
            next.add(newChatId);
            return next;
          });

          if (created.slug) {
            internalChatNavRef.current = created.slug;
            navigate(`/dashboard/${created.slug}`, { replace: true });
          }
        }
      }).catch((err) => {
        console.warn('[Dashboard] createChat background pre-creation skipped, backend will resolve in /api/ask:', err);
      });
    }

    // 5. Dynamic AI title generation (only once at the start of conversation)
    const shouldNameChat = (!currentConv.named && (currentConv.title === 'New Chat' || !currentConv.title));
    if (shouldNameChat) {
      currentConv.named = true;
      setConversations((prev) =>
        prev.map((c) =>
          c.id === targetId || c.id === currentConv.id || (currentConv.chatId && c.chatId === currentConv.chatId)
            ? { ...c, named: true }
            : c
        )
      );
      PlexisAPI.generateChatTitle({
        message: runtimeText,
        filename: runtimeFile?.name || '',
      }).then(async (aiTitle) => {
        if (aiTitle && aiTitle !== 'New Chat') {
          currentConv.title = aiTitle;
          const dbId = currentConv?.chatId || (isUUID(targetId) ? targetId : null);
          setConversations((prev) => {
            const next = prev.map((c) =>
              c.id === targetId || c.id === currentConv.id || (dbId && c.chatId === dbId)
                ? { ...c, title: aiTitle, named: true, updatedAt: Date.now() }
                : c
            );
            saveConversations(userId, next);
            return next;
          });
          if (dbId) {
            try {
              await PlexisAPI.renameChat(dbId, aiTitle);
            } catch (err) {
              console.warn('[Dashboard] renameChat after generateChatTitle error:', err);
            }
          }
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
        const response = await PlexisAPI.uploadCSVStream(runtimeFile, runtimeText, currentConv?.chatId || null);
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
          setConversations((prev) => {
            const next = updateConversation(prev, targetId, {
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
            });
            saveConversations(userId, next);
            return next;
          });
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
            const activeTargetId = streamingTargetIdRef.current || targetId;
            setConversations((prev) => {
              const conv = prev.find(c => c.id === activeTargetId || c.chatId === activeTargetId);
              if (!conv) return prev;
              return updateConversation(prev, conv.id, {
                messages: conv.messages.map(m => m.id === streamMsgId ? { ...m, text: accumulatedText } : m),
              });
            });
          })
          .on('done', (data) => {
            const datasetInfo = data?.dataset_info || {};
            const datasetId = data?.dataset_id || null;
            const finalChatId = data?.chat_id || streamingTargetIdRef.current || targetId;
            const finalSlug = data?.slug || currentConv.slug;
            const nextDatasetName = runtimeFile.name;
            const nextDatasetStats = datasetInfo.row_count
              ? { rows: datasetInfo.row_count, columns: datasetInfo.column_count, memory: datasetInfo.memory_usage_bytes }
              : null;

            if (data?.chat_id) {
              streamingTargetIdRef.current = data.chat_id;
              currentConv.chatId = data.chat_id;
              currentConv.id = data.chat_id;
              if (data.slug) {
                currentConv.slug = data.slug;
                currentConv.shareSlug = data.slug;
              }
            }

            // Notify workspace and EvidenceRegistry of new dataset
            if (datasetId) {
              setDataset(datasetId, finalChatId);
              EventBus.emit(Events.DATASET_LOADED, { datasetId, filename: nextDatasetName });
              EvidenceRegistry.setCurrentFingerprint(datasetInfo.schema_fingerprint || datasetId);
            }

            // Auto-open Investigation workspace after upload (desktop only)
            if (datasetId && data?.trigger_investigation && typeof window !== 'undefined' && window.innerWidth >= 1024) {
              EventBus.emit(Events.INVESTIGATION_REQUESTED, { datasetId });
              setAnalyticsCollapsed(false);
            }

            setStreamingMessageId(null);
            setLoadingChatIds((prev) => {
              const next = new Set(prev);
              next.delete(targetId);
              if (data?.chat_id) next.delete(data.chat_id);
              if (finalChatId) next.delete(finalChatId);
              return next;
            });

            const finalSummaryText = accumulatedText || data?.summary_text || `**${runtimeFile.name}**\n\nDataset loaded successfully (${(runtimeFile.size / 1024).toFixed(1)} KB).\n\nInvestigation ready.`;

            setConversations((prev) => {
              const conv = prev.find(c => c.id === finalChatId || c.chatId === finalChatId || c.id === targetId);
              const targetConvId = conv ? conv.id : finalChatId;
              const currentMsgs = conv?.messages || prev.find(c => c.id === targetConvId)?.messages || [];
              const hasStreamMsg = currentMsgs.some(m => m.id === streamMsgId);
              const finalMsgs = hasStreamMsg
                ? currentMsgs.map(m => m.id === streamMsgId ? { ...m, text: finalSummaryText, isStreaming: false } : m)
                : [...currentMsgs, { id: streamMsgId, sender: 'ai', text: finalSummaryText, isStreaming: false }];

              const next = updateConversation(prev, targetConvId, {
                ...(data?.chat_id && { id: data.chat_id, chatId: data.chat_id }),
                ...(data?.slug && { slug: data.slug, shareSlug: data.slug }),
                messages: finalMsgs,
                activeDatasetName: nextDatasetName,
                datasetName: nextDatasetName,
                datasetId,
                ...(nextDatasetStats && { datasetStats: nextDatasetStats }),
                title: conv?.title && conv.title !== 'New Chat' ? conv.title : `Dataset: ${runtimeFile.name}`,
              });
              saveConversations(userId, next);
              return next;
            });

            if (finalChatId && activeId !== finalChatId) {
              setActiveId(finalChatId);
            }
            if (finalSlug) {
              internalChatNavRef.current = finalSlug;
              navigate(`/dashboard/${finalSlug}`, { replace: true });
            }
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

      // Extract prior conversation history (excluding the current user message just appended)
      const allMsgs = currentConv.messages || [];
      const priorMsgs = allMsgs.length > 0 ? allMsgs.slice(0, -1) : [];
      const historyPayload = priorMsgs
        .filter(m => (m.sender === 'user' || m.sender === 'ai') && m.text && !m.isStreaming && m.id !== 'welcome' && !m.isFileCard)
        .slice(-10)
        .map(m => ({
          role: m.sender === 'user' ? 'user' : 'assistant',
          content: m.text,
        }));

      const resolvedChatId = currentConv.chatId || (isUUID(targetId) ? targetId : (isUUID(activeId) ? activeId : null));

      const apiResult = await PlexisAPI.askQuestion(
        runtimeText,
        currentConv.datasetId || (currentConv.datasetName && currentConv.datasetName !== 'No active dataset' && !currentConv.datasetName.startsWith('New Chat') ? currentConv.datasetName : '') || '',
        {
          workspaceState: workspaceOpen ? workspaceState : null,
          workspaceAction,
          sessionId: activeId,
          chatId: resolvedChatId,
          messages: historyPayload,
        },
      );

      const responseText = extractResponseText(apiResult);
      const responseMeta = extractMetadata(apiResult);

      // Store auto-created DB chat_id so subsequent queries pass it to the backend
      if (apiResult?.chat_id && currentConv.chatId !== apiResult.chat_id) {
        const finalChatId = apiResult.chat_id;
        const finalSlug = apiResult.slug || currentConv.slug;
        const oldTargetId = targetId;
        targetId = finalChatId;
        streamingTargetIdRef.current = finalChatId;
        currentConv.chatId = finalChatId;
        currentConv.id = finalChatId;
        if (finalSlug) {
          currentConv.slug = finalSlug;
          currentConv.shareSlug = finalSlug;
        }
        const currentTitle = currentConv.title;
        setConversations((prev) => {
          const next = prev.map((c) =>
            c.id === oldTargetId || c.id === targetId || c.chatId === finalChatId || c.id === currentConv.id
              ? { ...c, chatId: finalChatId, id: finalChatId, slug: finalSlug || c.slug, shareSlug: finalSlug || c.shareSlug }
              : c
          );
          saveConversations(userId, next);
          return next;
        });
        setActiveId(finalChatId);

        if (finalSlug) {
          internalChatNavRef.current = finalSlug;
          navigate(`/dashboard/${finalSlug}`, { replace: true });
        }

        // If the conversation already has a non-default title, persist it to finalChatId in DB
        if (currentTitle && currentTitle !== 'New Chat') {
          PlexisAPI.renameChat(finalChatId, currentTitle).catch((e) =>
            console.warn('[Dashboard] Error syncing title to new chat:', e)
          );
        }
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
          datasetId: apiResult?.dataset_id || apiResult?.transform_plan?.dataset_id || currentConv.datasetId || '',
        }),
        ...(apiResult?.transformation_clarification && {
          transformationClarification: apiResult.transformation_clarification,
        }),
        ...(apiResult?.transformation_update && {
          transformationUpdate: apiResult.transformation_update,
        }),
      };

      // Open Investigation tab if backend flags it (desktop only, don't cover mobile chat view)
      if (apiResult?.trigger_investigation && currentConv.datasetId && typeof window !== 'undefined' && window.innerWidth >= 1024) {
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
        prev.map((c) => {
          if (c.id === targetId || c.id === currentConv.id || (currentConv.chatId && c.chatId === currentConv.chatId)) {
            return {
              ...c,
              messages: [...updatedMessages, aiMessage],
              chartData: nextChartData,
              title: c.title || currentConv.title,
              updatedAt: Date.now(),
            };
          }
          return c;
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
        if (currentConv?.id) next.delete(currentConv.id);
        if (currentConv?.chatId) next.delete(currentConv.chatId);
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
      const resolvedDatasetId = datasetId || activeConversation?.datasetId;
      const result = await PlexisAPI.applyTransformation(planId, resolvedDatasetId);
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
      const resolvedDatasetId = datasetId || activeConversation?.datasetId;
      const result = await PlexisAPI.undoTransformation(resolvedDatasetId);
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
      const resolvedDatasetId = datasetId || activeConversation?.datasetId;
      await PlexisAPI.exportDataset(resolvedDatasetId);
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
        onRenameChat={async (id, title) => {
          setConversations((p) => {
            const next = renameConversation(p, id, title);
            saveConversations(userId, next);
            return next;
          });
          const target = conversations.find(c => c.id === id);
          const dbId = target?.chatId || (isUUID(id) ? id : null);
          if (dbId) {
            try {
              await PlexisAPI.renameChat(dbId, title);
            } catch (err) {
              console.warn('[Dashboard] renameChat error:', err);
            }
          }
        }}
        onDeleteChat={async (id) => {
          const target = conversations.find(c => c.id === id);
          const next = deleteConversation(conversations, id);
          setConversations(next);
          saveConversations(userId, next);
          if (activeId === id) {
            const nextActive = next.length > 0 ? next[0] : null;
            setActiveId(nextActive ? nextActive.id : null);
            if (nextActive?.slug && nextActive.slug.includes('-')) {
              navigate(`/dashboard/${nextActive.slug}`);
            } else {
              navigate('/dashboard');
            }
          }
          const dbId = target?.chatId || (isUUID(id) ? id : null);
          if (dbId) {
            try {
              await PlexisAPI.deleteChat(dbId);
            } catch (err) {
              console.warn('[Dashboard] deleteChat error:', err);
            }
          }
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
          isChatRouteLoading={isChatRouteLoading}
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
