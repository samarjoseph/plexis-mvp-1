import React, { useState, useRef, useEffect, useCallback, useMemo } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import {
  Plus,
  Search,
  Pin,
  PinOff,
  MessageSquare,
  FileSpreadsheet,
  MoreHorizontal,
  Pencil,
  Trash2,
  LogOut,
  Sparkles,
  Settings,
  PanelLeft,
  X,
} from 'lucide-react';
import { useAuth } from '../../context/AuthContext';
import { sortConversations, getConversationLastActivity } from '../../utils/conversationStorage';
import PlexisLogo from '../brand/PlexisLogo';
import Tooltip from '../ui/Tooltip';

/* ─────────────────────────────────────────────────────────────────
   AI TITLE GENERATION
   ───────────────────────────────────────────────────────────────── */
export async function generateChatTitle(firstUserMessage) {
  try {
    const res = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: 'claude-haiku-4-5-20251001',
        max_tokens: 30,
        messages: [
          {
            role: 'user',
            content: `Generate a concise chat title (3-5 words max) for this message. Respond ONLY with a JSON object like: {"chat_title":"Your Title Here"}
No punctuation at end. No quotes around the title. No emojis. Title case only.

Message: "${firstUserMessage.slice(0, 300)}"`,
          },
        ],
      }),
    });

    if (!res.ok) return null;
    const data = await res.json();
    const text = data?.content?.[0]?.text?.trim() ?? '';
    const clean = text.replace(/```json|```/g, '').trim();
    const parsed = JSON.parse(clean);
    return parsed?.chat_title ?? null;
  } catch {
    return null;
  }
}

/* ─────────────────────────────────────────────────────────────────
   HELPERS
   ───────────────────────────────────────────────────────────────── */
function relativeTime(timestamp) {
  const diff = Date.now() - timestamp;
  const mins = Math.floor(diff / 60000);
  if (mins < 60) return `${Math.max(1, mins)}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const d = new Date(timestamp);
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function groupConversations(conversations) {
  const pinned = [];
  const datasets = [];
  const recent = [];
  for (const c of conversations) {
    if (c.pinned) pinned.push(c);
    else if (c.datasetName) datasets.push(c);
    else recent.push(c);
  }
  return { pinned, datasets, recent };
}

/* ─────────────────────────────────────────────────────────────────
   CUSTOM HOOK: useIsDesktop
   Desktop = width >= 1024px. Recalculates on resize.
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
   LOGO TOGGLE BUTTON (Desktop only)
   - Shows Sparkles logo by default
   - On hover: morphs into PanelLeft (sidebar) icon
   - Click: toggles sidebar collapsed state
   - Tooltip shows action + shortcut
   ───────────────────────────────────────────────────────────────── */
export function LogoToggle({ collapsed, onToggle }) {
  const [hovered, setHovered] = useState(false);
  const label = collapsed ? 'Open Sidebar' : 'Close Sidebar';
  const shortcut = 'Ctrl+B';

  return (
    <Tooltip label={label} shortcut={shortcut} side={collapsed ? 'right' : 'right'}>
      <motion.button
        layoutId="v2-logo-toggle"
        className="v2-logo-toggle"
        onClick={onToggle}
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
        whileTap={{ scale: 0.92 }}
        aria-label={label}
      >
        {/* Sparkles logo — default state */}
        <motion.span
          className="v2-logo-icon"
          animate={{ opacity: hovered ? 0 : 1, scale: hovered ? 0.55 : 1 }}
          transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
          style={{ position: 'absolute', display: 'flex' }}
        >
          <PlexisLogo width={18} height={18} />
        </motion.span>

        {/* PanelLeft — appears on hover */}
        <motion.span
          className="v2-logo-arrow"
          animate={{ opacity: hovered ? 1 : 0, scale: hovered ? 1 : 0.55 }}
          transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
          style={{ position: 'absolute' }}
        >
          <PanelLeft size={16} />
        </motion.span>

        {/* Invisible spacer keeps button size stable */}
        <span style={{ visibility: 'hidden', display: 'flex' }}>
          <PlexisLogo width={18} height={18} />
        </span>
      </motion.button>
    </Tooltip>
  );
}

/* ─────────────────────────────────────────────────────────────────
   CHAT ITEM
   ───────────────────────────────────────────────────────────────── */
function ChatItem({ chat, isActive, onSelect, onRename, onDelete, onTogglePin }) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [renameVal, setRenameVal] = useState(chat.title);
  const menuRef = useRef(null);

  useEffect(() => {
    if (!menuOpen) return;
    const handler = (e) => {
      if (menuRef.current && !menuRef.current.contains(e.target)) setMenuOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [menuOpen]);

  const commitRename = () => {
    const trimmed = renameVal.trim();
    if (trimmed && trimmed !== chat.title) onRename(chat.id, trimmed);
    setRenaming(false);
  };

  return (
    <div
      className={`v2-chat-item${isActive ? ' active' : ''}`}
      onClick={() => !renaming && onSelect(chat.id)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => e.key === 'Enter' && !renaming && onSelect(chat.id)}
    >
      <MessageSquare size={14} className="v2-chat-item-icon" />

      <div className="v2-chat-item-content">
        {renaming ? (
          <input
            className="v2-rename-input"
            value={renameVal}
            autoFocus
            onChange={(e) => setRenameVal(e.target.value)}
            onBlur={commitRename}
            onKeyDown={(e) => {
              if (e.key === 'Enter') commitRename();
              if (e.key === 'Escape') setRenaming(false);
            }}
            onClick={(e) => e.stopPropagation()}
          />
        ) : (
          <span className="v2-chat-item-title">{chat.title || 'New Chat'}</span>
        )}
        <span className="v2-chat-item-time">{relativeTime(getConversationLastActivity(chat))}</span>
      </div>

      <button
        className="v2-chat-item-menu-btn"
        onClick={(e) => {
          e.stopPropagation();
          setMenuOpen((v) => !v);
        }}
        aria-label="Chat options"
      >
        <MoreHorizontal size={13} />
      </button>

      <AnimatePresence>
        {menuOpen && (
          <motion.div
            ref={menuRef}
            className="v2-context-menu"
            initial={{ opacity: 0, scale: 0.92, y: -4 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.92, y: -4 }}
            transition={{ duration: 0.1, ease: [0.16, 1, 0.3, 1] }}
            onClick={(e) => e.stopPropagation()}
          >
            <button
              onClick={() => {
                setRenaming(true);
                setRenameVal(chat.title);
                setMenuOpen(false);
              }}
            >
              <Pencil size={13} /> Rename
            </button>
            <button
              onClick={() => {
                onTogglePin(chat.id);
                setMenuOpen(false);
              }}
            >
              {chat.pinned ? <PinOff size={13} /> : <Pin size={13} />}
              {chat.pinned ? 'Unpin' : 'Pin'}
            </button>
            <div className="v2-context-divider" />
            <button
              className="danger"
              onClick={() => {
                onDelete(chat.id);
                setMenuOpen(false);
              }}
            >
              <Trash2 size={13} /> Delete
            </button>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────────
   VIRTUALIZED CHAT LIST
   ───────────────────────────────────────────────────────────────── */
const ITEM_H = 54;
const SECTION_H = 34;
const OVERSCAN = 8;

function VirtualList({ groups, activeId, onSelect, onRename, onDelete, onTogglePin, activeSearch }) {
  const containerRef = useRef(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportH, setViewportH] = useState(400);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setViewportH(el.clientHeight));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const { items, totalH } = useMemo(() => {
    const flat = [];
    let y = 0;

    const addSection = (label, icon, chats) => {
      if (!chats.length) return;
      flat.push({ type: 'section', label, icon, y, id: `section-${label}` });
      y += SECTION_H;
      for (const c of chats) {
        flat.push({ type: 'chat', chat: c, y, id: c.id });
        y += ITEM_H;
      }
      y += 8;
    };

    addSection('Pinned', <Pin size={11} />, groups.pinned);
    addSection('Datasets', <FileSpreadsheet size={11} />, groups.datasets);
    addSection('Recent', <MessageSquare size={11} />, groups.recent);

    if (!flat.length) {
      flat.push({ type: activeSearch ? 'empty-search' : 'empty', y: 0, id: 'empty' });
      y = 80;
    }

    return { items: flat, totalH: y };
  }, [groups]);

  const handleScroll = useCallback((e) => setScrollTop(e.currentTarget.scrollTop), []);

  const topBound = Math.max(0, scrollTop - OVERSCAN * ITEM_H);
  const botBound = scrollTop + viewportH + OVERSCAN * ITEM_H;
  const visible = items.filter((item) => item.y + ITEM_H > topBound && item.y < botBound);

  return (
    <div
      ref={containerRef}
      className="v2-sidebar-list v2-virtual-list"
      onScroll={handleScroll}
    >
      <div style={{ position: 'relative', height: totalH, minHeight: '100%' }}>
        {visible.map((item) => {
          if (item.type === 'empty' || item.type === 'empty-search') {
            return (
              <div
                key="empty"
                className={item.type === 'empty-search' ? 'v2-sidebar-empty-search' : 'v2-sidebar-empty'}
                style={{ position: 'absolute', top: item.y, left: 0, right: 0, textAlign: 'center', color: 'var(--dash-dim)', padding: '20px 0' }}
              >
                {item.type === 'empty-search' ? 'No matching conversations' : 'No conversations yet'}
              </div>
            );
          }
          if (item.type === 'section') {
            return (
              <div
                key={item.id}
                className="v2-sidebar-section-label"
                style={{ position: 'absolute', top: item.y, left: 0, right: 0, height: SECTION_H }}
              >
                {item.icon} {item.label}
              </div>
            );
          }
          return (
            <div
              key={item.id}
              style={{ position: 'absolute', top: item.y, left: 0, right: 0, height: ITEM_H }}
            >
              <ChatItem
                chat={item.chat}
                isActive={item.chat.id === activeId}
                onSelect={onSelect}
                onRename={onRename}
                onDelete={onDelete}
                onTogglePin={onTogglePin}
              />
            </div>
          );
        })}
      </div>
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────────
   SIDEBARV2
   ───────────────────────────────────────────────────────────────── */
export default function SidebarV2({
  collapsed,
  mobileOpen,
  onToggleCollapse,
  onCloseMobile,
  conversations,
  activeId,
  searchQuery,
  onSearchChange,
  onNewChat,
  onSelectChat,
  onRenameChat,
  onDeleteChat,
  onTogglePin,
  onOpenSettings,
}) {
  const { user, logout } = useAuth();
  const isDesktop = useIsDesktop();
  const [profileOpen, setProfileOpen] = useState(false);
  const [searchLocal, setSearchLocal] = useState('');

  // Resizing state
  const [expandedW, setExpandedW] = useState(280);
  const [isDragging, setIsDragging] = useState(false);

  // Reset to default width when collapsed changes
  useEffect(() => {
    setExpandedW(280);
  }, [collapsed]);

  const handleDragStart = useCallback((e) => {
    if (!isDesktop) return;
    e.preventDefault();
    setIsDragging(true);
    document.body.classList.add('is-dragging');

    const handleMouseMove = (eMove) => {
      let newW = eMove.clientX;
      if (newW < 220) newW = 220;
      if (newW > 420) newW = 420;
      setExpandedW(newW);
    };

    const handleMouseUp = () => {
      setIsDragging(false);
      document.body.classList.remove('is-dragging');
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };

    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);
  }, [isDesktop]);

  const activeSearch = searchQuery || searchLocal;

  const filteredConversations = useMemo(() => {
    let list = Object.values(conversations);
    if (activeSearch) {
      const lowerQuery = activeSearch.toLowerCase();
      list = list.filter((chat) => {
        const matchTitle = chat.title?.toLowerCase().includes(lowerQuery);
        const matchDataset = chat.activeDatasetName?.toLowerCase().includes(lowerQuery);
        const matchLastGenTitle = chat.messages?.some(m => m.generatedTitle?.toLowerCase().includes(lowerQuery));
        return matchTitle || matchDataset || matchLastGenTitle;
      });
    }
    return sortConversations(list);
  }, [conversations, activeSearch]);
  
  const groups = useMemo(() => groupConversations(filteredConversations), [filteredConversations]);

  const COLLAPSED_W = 0;

  const sidebarClasses = [
    'v2-sidebar',
    collapsed ? 'v2-sidebar--collapsed' : '',
    mobileOpen ? 'mobile-open' : '',
  ]
    .filter(Boolean)
    .join(' ');

  const sidebarWidth = isDesktop
    ? collapsed ? COLLAPSED_W : expandedW
    : expandedW;

  return (
    <>
      <motion.aside
        className={sidebarClasses}
        animate={isDesktop ? { width: sidebarWidth } : {}}
        transition={isDragging ? { duration: 0 } : { duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
        style={{ overflow: 'hidden', flexShrink: 0, position: 'relative' }}
      >
        <motion.div
          className="v2-sidebar-inner"
          animate={
            isDesktop
              ? { opacity: collapsed ? 0 : 1, pointerEvents: collapsed ? 'none' : 'auto' }
              : { opacity: 1, pointerEvents: 'auto' }
          }
          transition={{ duration: collapsed ? 0.1 : 0.18, ease: 'easeOut' }}
          style={{ width: expandedW, height: '100%', display: 'flex', flexDirection: 'column' }}
        >
          <div className="v2-sidebar-header">
            <div className="v2-sidebar-logo">
              {isDesktop ? (
                <Tooltip label={collapsed ? "Expand" : "Collapse"} side="right">
                    <LogoToggle collapsed={collapsed} onToggle={onToggleCollapse} />
                </Tooltip>
              ) : (
                <div className="v2-sidebar-logo-mobile" style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                  <PlexisLogo width={18} height={18} />
                </div>
              )}
              <motion.span
                className="v2-sidebar-logo-text"
                initial={{ opacity: 0, x: -6 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ duration: 0.2, delay: 0.05 }}
              >
                Plexis
              </motion.span>
            </div>
            {!isDesktop && (
              <button
                type="button"
                className="v2-sidebar-mobile-close"
                onClick={onCloseMobile}
                aria-label="Close sidebar"
              >
                <X size={18} />
              </button>
            )}
          </div>

          <Tooltip label="New Chat" shortcut="Ctrl+N" side="right" fullWidth={true}>
            <motion.button
              className="v2-new-chat-btn"
              onClick={onNewChat}
              whileHover={{ scale: 1.01 }}
              whileTap={{ scale: 0.97 }}
            >
              <Plus size={15} />
              <span>New Chat</span>
            </motion.button>
          </Tooltip>

          <Tooltip label="Search Chats" shortcut="Ctrl+K" side="right" fullWidth={true}>
            <div className="v2-sidebar-search">
              <Search size={14} />
              <input
                id="chat-search-input"
                type="text"
                placeholder="Search chats…"
                value={searchQuery}
                onChange={(e) => onSearchChange(e.target.value)}
              />
            {searchQuery && (
              <Tooltip label="Clear search">
                <button
                  className="v2-search-clear"
                  onClick={() => onSearchChange('')}
                  aria-label="Clear search"
                >
                  ×
                </button>
              </Tooltip>
            )}
          </div>
          </Tooltip>

          <VirtualList
            groups={groups}
            activeId={activeId}
            onSelect={onSelectChat}
            onRename={onRenameChat}
            onDelete={onDeleteChat}
            onTogglePin={onTogglePin}
            activeSearch={activeSearch}
          />

          <div className="v2-sidebar-bottom">
            <div className="v2-sidebar-profile">
              <button
                type="button"
                className="v2-profile-btn"
                onClick={() => setProfileOpen((v) => !v)}
              >
                {user?.avatar ? (
                  <img
                    className="v2-profile-avatar"
                    src={user.avatar}
                    alt={user.name}
                    referrerPolicy="no-referrer"
                  />
                ) : (
                  <div className="v2-profile-avatar-fallback">
                    {user?.name?.charAt(0) ?? '?'}
                  </div>
                )}
                <div className="v2-profile-info">
                  <span className="v2-profile-name">{user?.name}</span>
                  <span className="v2-profile-email">{user?.email}</span>
                </div>
              </button>
            </div>

            <AnimatePresence>
              {profileOpen && (
                <motion.div
                  className="v2-profile-dropdown"
                  initial={{ opacity: 0, y: 8, scale: 0.95 }}
                  animate={{ opacity: 1, y: 0, scale: 1 }}
                  exit={{ opacity: 0, y: 8, scale: 0.95 }}
                  transition={{ duration: 0.14, ease: [0.16, 1, 0.3, 1] }}
                >
                  <div className="v2-profile-dropdown-header">
                    {user?.avatar ? (
                      <img
                        className="v2-profile-avatar"
                        src={user.avatar}
                        alt={user.name}
                        referrerPolicy="no-referrer"
                      />
                    ) : (
                      <div className="v2-profile-avatar-fallback">
                        {user?.name?.charAt(0) ?? '?'}
                      </div>
                    )}
                    <div>
                      <span className="v2-profile-name">{user?.name}</span>
                      <span className="v2-profile-email">{user?.email}</span>
                    </div>
                  </div>

                  <div className="v2-profile-dropdown-divider" />

                  <button
                    onClick={() => {
                      setProfileOpen(false);
                      onOpenSettings();
                    }}
                  >
                    <Settings size={14} /> Settings
                  </button>
                  <button
                    className="danger"
                    onClick={() => {
                      setProfileOpen(false);
                      logout();
                    }}
                  >
                    <LogOut size={14} /> Sign out
                  </button>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        </motion.div>

        {/* Drag Handle */}
        {isDesktop && !collapsed && (
          <div
            className={`v2-resize-handle v2-resize-handle-right ${isDragging ? 'active' : ''}`}
            onMouseDown={handleDragStart}
          />
        )}
      </motion.aside>
    </>
  );
}
