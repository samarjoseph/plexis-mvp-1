const STORAGE_PREFIX = 'plexis_conversations_';

function storageKey(userId) {
  return `${STORAGE_PREFIX}${userId || 'anonymous'}`;
}

export function loadConversations(userId) {
  try {
    const raw = localStorage.getItem(storageKey(userId));
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

export function saveConversations(userId, conversations) {
  localStorage.setItem(storageKey(userId), JSON.stringify(conversations));
}

/**
 * Extract the true timestamp of the most recent conversation activity.
 * Ranked on actual conversation content (user/assistant messages),
 * NEVER bumped by simply viewing or clicking a chat.
 */
export function getConversationLastActivity(conv) {
  if (!conv) return 0;

  // 1. Check real messages in the conversation (excluding the static welcome placeholder)
  if (conv.messages && Array.isArray(conv.messages) && conv.messages.length > 0) {
    const realMessages = conv.messages.filter(
      (m) => m && m.id !== 'welcome' && (m.sender === 'user' || m.sender === 'ai')
    );
    if (realMessages.length > 0) {
      const lastMsg = realMessages[realMessages.length - 1];
      let msgTime = lastMsg.createdAt;
      if (!msgTime && typeof lastMsg.id === 'string' && lastMsg.id.includes('-')) {
        const parts = lastMsg.id.split('-');
        const parsed = Number(parts[parts.length - 1] || parts[1]);
        if (!isNaN(parsed) && parsed > 1000000000000) {
          msgTime = parsed;
        }
      }
      if (msgTime && !isNaN(msgTime) && msgTime > 0) {
        return Number(msgTime);
      }
    }
  }

  // 2. Check explicit lastMessageAt / last_message_at / updatedAt / createdAt
  const lastAt = conv.lastMessageAt || conv.last_message_at;
  if (lastAt) {
    const parsed = typeof lastAt === 'number' ? lastAt : new Date(lastAt).getTime();
    if (!isNaN(parsed) && parsed > 0) return parsed;
  }

  if (conv.updatedAt) {
    const parsed = typeof conv.updatedAt === 'number' ? conv.updatedAt : new Date(conv.updatedAt).getTime();
    if (!isNaN(parsed) && parsed > 0) return parsed;
  }

  if (conv.createdAt) {
    const parsed = typeof conv.createdAt === 'number' ? conv.createdAt : new Date(conv.createdAt).getTime();
    if (!isNaN(parsed) && parsed > 0) return parsed;
  }

  return 0;
}

export function createConversation({ title = 'New Chat', datasetName = null } = {}) {
  const now = Date.now();
  return {
    id: `conv_${now}_${Math.random().toString(36).slice(2, 9)}`,
    title,
    datasetName,
    pinned: false,
    messages: [
      {
        id: 'welcome',
        sender: 'ai',
        text: '# Welcome to Plexis\nUpload a dataset or ask a question to begin your analysis session.',
        hasChart: false,
      },
    ],
    chartData: { labels: [], datasets: [] },
    activeDatasetName: datasetName || 'No active dataset',
    createdAt: now,
    updatedAt: now,
  };
}

export function updateConversation(conversations, id, patch) {
  return conversations.map((c) => {
    if (c.id !== id && c.chatId !== id) return c;

    // Preserve existing updatedAt unless explicit or derived from new message content.
    // Visiting a chat or background sync does NOT artificially bump its rank to 'now'!
    let nextUpdatedAt = c.updatedAt;
    if (patch.updatedAt !== undefined) {
      nextUpdatedAt = patch.updatedAt;
    } else if (patch.messages !== undefined) {
      const activityTime = getConversationLastActivity({ ...c, ...patch });
      nextUpdatedAt = activityTime || c.updatedAt || Date.now();
    }

    return {
      ...c,
      ...patch,
      updatedAt: nextUpdatedAt,
    };
  });
}

export function deleteConversation(conversations, id) {
  return conversations.filter((c) => c.id !== id && c.chatId !== id);
}

export function renameConversation(conversations, id, title) {
  return updateConversation(conversations, id, { title });
}

export function togglePinConversation(conversations, id) {
  return conversations.map((c) =>
    (c.id === id || c.chatId === id) ? { ...c, pinned: !c.pinned } : c
  );
}

export function searchConversations(conversations, query) {
  const q = query.trim().toLowerCase();
  if (!q) return conversations;
  return conversations.filter(
    (c) =>
      c.title.toLowerCase().includes(q) ||
      (c.datasetName && c.datasetName.toLowerCase().includes(q))
  );
}

export function sortConversations(conversations) {
  return [...conversations].sort((a, b) => {
    if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
    const timeA = getConversationLastActivity(a);
    const timeB = getConversationLastActivity(b);
    return timeB - timeA;
  });
}
