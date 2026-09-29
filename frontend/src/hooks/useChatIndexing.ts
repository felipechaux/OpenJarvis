import { useEffect, useRef } from 'react';
import { getBase } from '../lib/api';
import { useAppStore } from '../lib/store';
import type { Conversation } from '../types';

/// Wait this long after the last change before indexing a conversation, so
/// a streaming reply is sent once it has settled, not token by token.
const SETTLE_MS = 5000;

function payload(conv: Conversation) {
  return {
    id: conv.id,
    title: conv.title,
    updatedAt: conv.updatedAt,
    messages: conv.messages.map((m) => ({ role: m.role, content: m.content })),
  };
}

async function postIndex(convs: Conversation[]): Promise<boolean> {
  try {
    const res = await fetch(`${getBase()}/v1/chat-history/index`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversations: convs.map(payload) }),
    });
    return res.ok;
  } catch {
    return false;
  }
}

/// Mirrors chat conversations (which live only in localStorage) into the
/// server's knowledge store, so JARVIS's knowledge_search can find what the
/// user talked about with it.  Everything is sent once on startup, then each
/// conversation after it settles; deleted conversations are removed.
/// Mount once (App).
export function useChatIndexing(enabled: boolean) {
  const conversations = useAppStore((s) => s.conversations);
  // id → updatedAt last indexed; null until the startup backfill succeeds.
  const indexed = useRef<Map<string, number> | null>(null);

  useEffect(() => {
    if (!enabled) return;
    const current = useAppStore.getState().conversations;
    const timer = setTimeout(async () => {
      if (indexed.current !== null) return;
      if (await postIndex(current)) {
        indexed.current = new Map(current.map((c) => [c.id, c.updatedAt]));
      }
    }, 1000);
    return () => clearTimeout(timer);
  }, [enabled]);

  useEffect(() => {
    if (!enabled) return;
    const timer = setTimeout(() => {
      const seen = indexed.current;
      if (seen === null) return; // the startup backfill covers it
      const changed = conversations.filter(
        (c) => c.messages.length > 0 && seen.get(c.id) !== c.updatedAt,
      );
      if (changed.length) {
        postIndex(changed).then((ok) => {
          if (ok) changed.forEach((c) => seen.set(c.id, c.updatedAt));
        });
      }
      const live = new Set(conversations.map((c) => c.id));
      for (const id of [...seen.keys()]) {
        if (live.has(id)) continue;
        seen.delete(id);
        fetch(`${getBase()}/v1/chat-history/${encodeURIComponent(id)}`, {
          method: 'DELETE',
        }).catch(() => {});
      }
    }, SETTLE_MS);
    return () => clearTimeout(timer);
  }, [conversations, enabled]);
}
