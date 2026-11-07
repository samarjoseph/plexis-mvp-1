import React, { useRef, useEffect, useState, useCallback, memo } from 'react';
import { useNavigate } from 'react-router-dom';
import { motion, AnimatePresence } from 'framer-motion';
import {
  FileSpreadsheet, Search, LineChart, MessageSquare, Menu, BarChart3,
  Table2, ArrowUpRight,
} from 'lucide-react';
import PlexisLogo from '../brand/PlexisLogo';
import MessageBubbleV2 from './MessageBubbleV2';
import AIThinkingIndicator from './AIThinkingIndicator';
import ConversationLoadingScreen from './ConversationLoadingScreen';

import ChatInputBar from './ChatInputBar';
import { useAuth } from '../../context/AuthContext';
import { LogoToggle } from './SidebarV2';
import Tooltip from '../ui/Tooltip';
import WorkspaceToggleButton from '../workspace/WorkspaceToggleButton.jsx';
import { useWorkspace } from '../../context/WorkspaceContext.jsx';
import { EventBus } from '../../events/EventBus';
import { Events } from '../../events/Events';

/* ─────────────────────────────────────────────────────────────────
   Animation variants for welcome container and cards
   ───────────────────────────────────────────────────────────────── */
const welcomeContainerVariants = {
  hidden: { opacity: 0 },
  visible: {
    opacity: 1,
    transition: {
      staggerChildren: 0.08,
      delayChildren: 0.1,
    },
  },
};

const welcomeCardVariants = {
  hidden: { opacity: 0, y: 16, scale: 0.95 },
  visible: {
    opacity: 1,
    y: 0,
    scale: 1,
    transition: {
      type: 'spring',
      stiffness: 400,
      damping: 26,
    },
  },
};

/* ─────────────────────────────────────────────────────────────────
   useIsDesktop: true when viewport >= 1024px
   ───────────────────────────────────────────────────────────────── */
function useIsDesktop() {
  const [isDesktop, setIsDesktop] = useState(() => window.innerWidth >= 1024);

  useEffect(() => {
    const mq = window.matchMedia('(min-width: 1024px)');
    const handler = (e) => setIsDesktop(e.matches);
    mq.addEventListener('change', handler);
    return () => mq.removeEventListener('change', handler);
  }, []);

  return isDesktop;
}

/* ─────────────────────────────────────────────────────────────────
   ChatMessageList: Memoized message feed to prevent re-rendering
   entire conversation history when input changes or typing occurs.
   ───────────────────────────────────────────────────────────────── */
const ChatMessageList = memo(function ChatMessageList({
  messages,
  isLoading,
  showWelcome,
  greeting,
  firstName,
  activeDatasetId,
  activeSessionId,
  onUploadClick,
  onPrefill,
  onRetry,
  onApplyTransform,
  onCancelTransform,
  onClarificationAction,
  onUndoTransform,
  onExportDataset,
  isApplyingTransform,
  chatBodyRef,
  scrollRef,
  isGuest = false,
  onLockedFeature = null,
}) {
  const { openWorkspace } = useWorkspace();

  const handleSpreadsheetClick = useCallback(() => {
    if (isGuest) {
      onLockedFeature?.('spreadsheet');
      return;
    }
    if (activeDatasetId) {
      openWorkspace(activeDatasetId, activeSessionId);
    } else {
      onUploadClick?.();
    }
  }, [isGuest, onLockedFeature, activeDatasetId, activeSessionId, openWorkspace, onUploadClick]);

  return (
    <div className="v2-chat-messages" ref={chatBodyRef}>
      {showWelcome && (
        <motion.div
          className="v2-welcome"
          initial={{ opacity: 0, y: 22 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.45, ease: [0.16, 1, 0.3, 1] }}
        >
          <div className="v2-welcome-icon">
            <PlexisLogo width={26} height={26} />
          </div>
          <h2>
            {greeting}{firstName ? `, ${firstName}` : ''}
          </h2>
          <p>What would you like to explore today?</p>

          <motion.div
            className="v2-welcome-cards"
            variants={welcomeContainerVariants}
            initial="hidden"
            animate="visible"
          >
            <motion.button
              type="button"
              variants={welcomeCardVariants}
              whileHover={{ y: -4, scale: 1.02 }}
              whileTap={{ scale: 0.96 }}
              className="v2-welcome-card v2-welcome-card--upload"
              onClick={() => {
                if (isGuest) {
                  onStageFile?.({
                    name: 'sample_customer.csv',
                    size: 15420,
                    type: 'text/csv',
                    isSampleGuest: true,
                  });
                  return;
                }
                onUploadClick?.();
              }}
            >
              <div className="v2-welcome-card-icon">
                <FileSpreadsheet size={19} />
              </div>
              <div className="v2-welcome-card-content">
                <span className="v2-welcome-card-title">Upload Dataset</span>
                <span className="v2-welcome-card-desc">Analyze CSV & Excel with AI</span>
              </div>
              <ArrowUpRight className="v2-welcome-card-arrow" size={15} />
            </motion.button>

            <motion.button
              type="button"
              variants={welcomeCardVariants}
              whileHover={{ y: -4, scale: 1.02 }}
              whileTap={{ scale: 0.96 }}
              className="v2-welcome-card v2-welcome-card--spreadsheet"
              onClick={handleSpreadsheetClick}
            >
              <div className="v2-welcome-card-icon">
                <Table2 size={19} />
              </div>
              <div className="v2-welcome-card-content">
                <span className="v2-welcome-card-title">View Live Spreadsheet</span>
                <span className="v2-welcome-card-desc">
                  {activeDatasetId ? 'Inspect & filter active data' : 'Open interactive data table'}
                </span>
              </div>
              <ArrowUpRight className="v2-welcome-card-arrow" size={15} />
            </motion.button>

            <motion.button
              type="button"
              variants={welcomeCardVariants}
              whileHover={{ y: -4, scale: 1.02 }}
              whileTap={{ scale: 0.96 }}
              className="v2-welcome-card v2-welcome-card--insights"
              onClick={() => onPrefill('Generate insights from the current dataset.')}
            >
              <div className="v2-welcome-card-icon">
                <LineChart size={19} />
              </div>
              <div className="v2-welcome-card-content">
                <span className="v2-welcome-card-title">Generate Insights</span>
                <span className="v2-welcome-card-desc">Extract patterns & statistics</span>
              </div>
              <ArrowUpRight className="v2-welcome-card-arrow" size={15} />
            </motion.button>

            <motion.button
              type="button"
              variants={welcomeCardVariants}
              whileHover={{ y: -4, scale: 1.02 }}
              whileTap={{ scale: 0.96 }}
              className="v2-welcome-card v2-welcome-card--ask"
              onClick={() => onPrefill('Ask Plexis anything.')}
            >
              <div className="v2-welcome-card-icon">
                <MessageSquare size={19} />
              </div>
              <div className="v2-welcome-card-content">
                <span className="v2-welcome-card-title">Ask Anything</span>
                <span className="v2-welcome-card-desc">Chat with Plexis freely</span>
              </div>
              <ArrowUpRight className="v2-welcome-card-arrow" size={15} />
            </motion.button>
          </motion.div>
        </motion.div>
      )}

      {messages.map((msg) => (
        <div
          key={msg.id}
          className={`v2-msg-row v2-msg-row--${msg.sender}`}
        >
          <MessageBubbleV2
            message={msg}
            onRetry={onRetry}
            onApplyTransform={onApplyTransform}
            onCancelTransform={onCancelTransform}
            onClarificationAction={onClarificationAction}
            onUndoTransform={onUndoTransform}
            onExportDataset={onExportDataset}
            isApplyingTransform={isApplyingTransform}
          />
        </div>
      ))}

      <AnimatePresence>
        {isLoading && (
          <motion.div
            className="v2-msg-row v2-msg-row--ai"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
          >
            <AIThinkingIndicator />
          </motion.div>
        )}
      </AnimatePresence>

      <div ref={scrollRef} />
    </div>
  );
});

export default function ChatAreaV2({
  messages,
  inputText,
  onInputChange,
  onSend,
  onRetry,
  stagedFile,
  onStageFile,
  onCancelFile,
  onFileSizeError,
  isLoading,
  isChatRouteLoading = false,
  activeDatasetName,
  activeDatasetId,
  activeSessionId,
  onOpenSidebar,
  sidebarCollapsed,
  onToggleSidebar,
  analyticsCollapsed,
  onToggleAnalytics,
  // Transform handlers
  onApplyTransform,
  onCancelTransform,
  onClarificationAction,
  onUndoTransform,
  onExportDataset,
  isApplyingTransform,
  userPlan = 'free',
  isGuest = false,
  onLockedFeature = null,
  onOpenCustomUploadModal = null,
}) {
  const navigate = useNavigate();
  const { user } = useAuth();
  const isDesktop = useIsDesktop();
  const scrollRef = useRef(null);
  const chatBodyRef = useRef(null);
  const [greeting, setGreeting] = useState('');
  // True when the user has scrolled up manually — halts auto-scroll until they return to bottom
  const [userScrolledUp, setUserScrolledUp] = useState(false);

  useEffect(() => {
    const hour = new Date().getHours();
    if (hour >= 5 && hour < 12) setGreeting('Good morning');
    else if (hour >= 12 && hour < 17) setGreeting('Good afternoon');
    else if (hour >= 17 && hour < 22) setGreeting('Good evening');
    else setGreeting('Good night');
  }, []);

  // Intelligent auto-scroll: follows streamed content but pauses if user scrolls up
  useEffect(() => {
    const el = chatBodyRef.current;
    if (!el) return;
    const handleScroll = () => {
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 100;
      setUserScrolledUp(!atBottom);
    };
    el.addEventListener('scroll', handleScroll, { passive: true });
    return () => el.removeEventListener('scroll', handleScroll);
  }, []);

  const activeSessionRef = useRef(activeSessionId);
  const lastMsgCountRef = useRef(messages.length);

  // Synchronously snap directly to bottom on mount or when switching chats (never animate from top)
  useEffect(() => {
    if (activeSessionRef.current !== activeSessionId) {
      activeSessionRef.current = activeSessionId;
      lastMsgCountRef.current = messages.length;
    }
    if (chatBodyRef.current) {
      chatBodyRef.current.scrollTop = chatBodyRef.current.scrollHeight;
    }
  }, [activeSessionId, messages.length]);

  // Keep scroll at bottom without animating entire history
  useEffect(() => {
    const el = chatBodyRef.current;
    if (!el || userScrolledUp) return;

    // While route is loading or initial chat display: snap directly to bottom without animation
    if (isChatRouteLoading || messages.length <= lastMsgCountRef.current) {
      el.scrollTop = el.scrollHeight;
      lastMsgCountRef.current = messages.length;
      return;
    }

    // Only when new message arrives in current chat during generation: smooth scroll
    if (isLoading || messages.length > lastMsgCountRef.current) {
      scrollRef.current?.scrollIntoView({ behavior: 'smooth' });
      lastMsgCountRef.current = messages.length;
    }
  }, [messages, isLoading, isChatRouteLoading, userScrolledUp]);

  const handleUploadClick = useCallback(() => {
    document.getElementById('dataset-upload-input')?.click();
  }, []);

  const handlePrefill = useCallback((text) => {
    EventBus.emit(Events.PREFILL_CHAT_INPUT, text);
    if (onInputChange) {
      onInputChange(text);
    }
  }, [onInputChange]);

  const firstName = user?.name?.split(' ')[0] || '';
  const showWelcome = (activeSessionId === null || !activeSessionId) && messages.length === 0 && !isLoading && !isChatRouteLoading;

  return (
    <main className="v2-chat">
      {/* ── Header ── */}
      <header className="v2-chat-header">
        {!isDesktop && (
          <button
            type="button"
            className="v2-mobile-menu-btn"
            onClick={onOpenSidebar}
            aria-label="Open sidebar"
          >
            <Menu size={20} />
          </button>
        )}

        {isDesktop && sidebarCollapsed && (
          <LogoToggle collapsed={true} onToggle={onToggleSidebar} />
        )}

        <div className={`v2-chat-header-brand ${!isDesktop ? 'v2-chat-header-brand--mobile' : ''}`}>
          <span>Plexis</span>
        </div>

        <div className="v2-chat-header-actions" style={{ marginLeft: 'auto', display: 'flex', gap: '8px', alignItems: 'center' }}>
          {isGuest ? (
            <div className="v2-header-guest-wrap" style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <button
                type="button"
                className="v2-header-plan-btn v2-header-plan-btn--guest"
                onClick={() => onLockedFeature?.('quota')}
                title="Guest Mode (Max 2 transformations/month)"
                aria-label="Guest Mode"
              >
                <span className="v2-header-plan-text">Mode</span>
                <span className="v2-header-plan-guest-text">Guest</span>
              </button>
              <button
                type="button"
                className="v2-header-guest-signin-btn"
                onClick={() => onLockedFeature?.('spreadsheet')}
              >
                Sign In
              </button>
            </div>
          ) : (
            <button
              type="button"
              className="v2-header-plan-btn"
              onClick={() => navigate('/billing')}
              title="Subscription plan & limits"
              aria-label="Subscription plan"
            >
              <span className="v2-header-plan-text">Plan</span>
              <span className={userPlan === 'plus' ? 'v2-header-plan-plus-text' : 'v2-header-plan-free-text'}>
                {userPlan === 'plus' ? 'Plus' : 'Free'}
              </span>
            </button>
          )}
          {activeDatasetId && (
            <Tooltip label={isGuest ? "Live Spreadsheet (Locked in Guest Mode)" : "Open data table"} side="left">
              <WorkspaceToggleButton
                datasetId={activeDatasetId}
                sessionId={activeSessionId}
                onClick={isGuest ? () => onLockedFeature?.('spreadsheet') : undefined}
              />
            </Tooltip>
          )}
          <AnimatePresence>
            {analyticsCollapsed && (
              <motion.div
                initial={{ opacity: 0, scale: 0.9 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.9 }}
                transition={{ duration: 0.2 }}
              >
                <Tooltip label={isGuest ? "Analytics (Locked in Guest Mode)" : "Open Analytics"} shortcut="Ctrl+Shift+A" side="left">
                  <button
                    type="button"
                    className="v2-analytics-btn"
                    onClick={isGuest ? () => onLockedFeature?.('analytics') : onToggleAnalytics}
                    aria-label="Open Analytics"
                  >
                    <BarChart3 size={18} />
                  </button>
                </Tooltip>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </header>

      {/* ── AI-ish Loading Transition ── */}
      <AnimatePresence>
        {isChatRouteLoading && <ConversationLoadingScreen />}
      </AnimatePresence>

      {/* ── Messages feed (memoized) ── */}
      <ChatMessageList
        messages={messages}
        isLoading={isLoading}
        showWelcome={showWelcome}
        greeting={greeting}
        firstName={firstName}
        activeDatasetId={activeDatasetId}
        activeSessionId={activeSessionId}
        onUploadClick={handleUploadClick}
        onPrefill={handlePrefill}
        onRetry={onRetry}
        onApplyTransform={onApplyTransform}
        onCancelTransform={onCancelTransform}
        onClarificationAction={onClarificationAction}
        onUndoTransform={onUndoTransform}
        onExportDataset={onExportDataset}
        isApplyingTransform={isApplyingTransform}
        chatBodyRef={chatBodyRef}
        scrollRef={scrollRef}
        isGuest={isGuest}
        onLockedFeature={onLockedFeature}
      />

      {/* ── Isolated Chat Input Bar ── */}
      <ChatInputBar
        onSend={onSend}
        stagedFile={stagedFile}
        onStageFile={onStageFile}
        onCancelFile={onCancelFile}
        onFileSizeError={onFileSizeError}
        isLoading={isLoading}
        initialText={inputText || ''}
        isGuest={isGuest}
        onLockedFeature={onLockedFeature}
        onOpenCustomUploadModal={onOpenCustomUploadModal}
      />
    </main>
  );
}
