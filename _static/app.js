/* =========================================================================
   TG Studio — frontend (Telegram-style messenger UI as a native-app shell)

   Sections:
     0.   Globals + small DOM/format utils
     1.   Auth (gate)
     2.   API client
     3.   WebSocket
     4.   State store (chats, messages, users, ...)
     5.   Sidebar (chat list + search + tabs)
     6.   Chat view (header, messages, composer)
     7.   Message renderers (text/photo/video/sticker/poll/doc/audio/...)
     8.   Context menu + reactions
     9.   Gestures (long-press, swipe-to-reply, edge-swipe back)
     10.  Drawers (right side: chat info / user profile)
     11.  Pages (profile-bot / moderation / AI / mini-apps / settings / audit)
     12.  Image viewer (pinch-zoom)
     13.  Service worker
   ========================================================================= */

'use strict';

// ============= 0. Globals + utils ==========================================

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const TOKEN_KEY = 'tgstudio.token';
let TOKEN = localStorage.getItem(TOKEN_KEY) || '';

function ce(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class' || k === 'className') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k === 'html') el.innerHTML = v;
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null || c === false) continue;
    if (Array.isArray(c)) c.forEach(x => x != null && el.appendChild(typeof x === 'string' ? document.createTextNode(x) : x));
    else el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return el;
}

function esc(s) {
  return (s ?? '').toString()
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function avInitial(name) {
  const n = (name || '?').trim();
  if (!n) return '?';
  const parts = n.split(/\s+/);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return n.slice(0, 2).toUpperCase();
}

function avColor(id) {
  const n = (typeof id === 'number') ? id : (id ? id.toString().split('').reduce((a, c) => a + c.charCodeAt(0), 0) : 0);
  return 'av-c' + (((n % 8) + 8) % 8 + 1);
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const today = new Date();
  if (d.toDateString() === today.toDateString()) {
    return d.getHours().toString().padStart(2, '0') + ':' + d.getMinutes().toString().padStart(2, '0');
  }
  const y = today.getFullYear();
  const sameYear = d.getFullYear() === y;
  if (sameYear) {
    return d.toLocaleDateString('ru-RU', { day: '2-digit', month: 'short' });
  }
  return d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: '2-digit' });
}

function fmtFullTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return d.toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' });
}

function fmtDateHeading(ts) {
  const d = new Date(ts * 1000);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const yest = new Date(today); yest.setDate(today.getDate() - 1);
  const dd = new Date(d); dd.setHours(0, 0, 0, 0);
  if (+dd === +today) return 'Сегодня';
  if (+dd === +yest) return 'Вчера';
  return d.toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: dd.getFullYear() !== today.getFullYear() ? 'numeric' : undefined });
}

function fmtBytes(b) {
  if (!b) return '';
  if (b < 1024) return b + ' Б';
  if (b < 1024 * 1024) return (b / 1024).toFixed(1) + ' КБ';
  if (b < 1024 * 1024 * 1024) return (b / 1024 / 1024).toFixed(1) + ' МБ';
  return (b / 1024 / 1024 / 1024).toFixed(2) + ' ГБ';
}

function fmtDur(s) {
  if (!s) return '0:00';
  const m = Math.floor(s / 60), sec = s % 60;
  return m + ':' + String(sec).padStart(2, '0');
}

function toast(text, ms = 2400) {
  const t = ce('div', { class: 'toast' }, text);
  document.body.appendChild(t);
  setTimeout(() => t.remove(), ms);
}

function debounce(fn, ms) {
  let h; return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); };
}

// SVG icon helper
const SVG = {
  back:    '<svg viewBox="0 0 24 24"><path d="M19 11H7.83l4.88-4.88c.39-.39.39-1.03 0-1.42-.39-.39-1.02-.39-1.41 0L4.71 11.29c-.39.39-.39 1.02 0 1.41l6.59 6.59c.39.39 1.02.39 1.41 0 .39-.39.39-1.02 0-1.41L7.83 13H19c.55 0 1-.45 1-1s-.45-1-1-1z"/></svg>',
  send:    '<svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>',
  more:    '<svg viewBox="0 0 24 24"><path d="M12 8c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zm0 2c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2zm0 6c-1.1 0-2 .9-2 2s.9 2 2 2 2-.9 2-2-.9-2-2-2z"/></svg>',
  search:  '<svg viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27a6.5 6.5 0 1 0-.7.7l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0a4.5 4.5 0 1 1 0-9 4.5 4.5 0 0 1 0 9z"/></svg>',
  attach:  '<svg viewBox="0 0 24 24"><path d="M16.5 6v11.5c0 2.21-1.79 4-4 4s-4-1.79-4-4V5a2.5 2.5 0 1 1 5 0v10.5c0 .55-.45 1-1 1s-1-.45-1-1V6H10v9.5a2.5 2.5 0 0 0 5 0V5a4 4 0 1 0-8 0v12.5c0 3.04 2.46 5.5 5.5 5.5s5.5-2.46 5.5-5.5V6h-1.5z"/></svg>',
  smile:   '<svg viewBox="0 0 24 24"><path d="M11.99 2C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2zM12 20c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8zm3.5-9c.83 0 1.5-.67 1.5-1.5S16.33 8 15.5 8 14 8.67 14 9.5s.67 1.5 1.5 1.5zm-7 0c.83 0 1.5-.67 1.5-1.5S9.33 8 8.5 8 7 8.67 7 9.5 7.67 11 8.5 11zm3.5 6.5c2.33 0 4.31-1.46 5.11-3.5H6.89c.8 2.04 2.78 3.5 5.11 3.5z"/></svg>',
  reply:   '<svg viewBox="0 0 24 24"><path d="M10 9V5l-7 7 7 7v-4.1c5 0 8.5 1.6 11 5.1-1-5-4-10-11-11z"/></svg>',
  edit:    '<svg viewBox="0 0 24 24"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>',
  copy:    '<svg viewBox="0 0 24 24"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg>',
  trash:   '<svg viewBox="0 0 24 24"><path d="M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>',
  pin:     '<svg viewBox="0 0 24 24"><path d="M16 9V4l1-1V2H7v1l1 1v5L6 12v2h4v6l1 1 1-1v-6h4v-2l-2-3z"/></svg>',
  close:   '<svg viewBox="0 0 24 24"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>',
  forward: '<svg viewBox="0 0 24 24"><path d="M12 8V4l8 8-8 8v-4H4V8z"/></svg>',
  download:'<svg viewBox="0 0 24 24"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>',
  doc:     '<svg viewBox="0 0 24 24"><path d="M14 2H6c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V8l-6-6zM6 20V4h7v5h5v11H6z"/></svg>',
  play:    '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>',
  mic:     '<svg viewBox="0 0 24 24"><path d="M12 14c1.66 0 3-1.34 3-3V5a3 3 0 0 0-6 0v6c0 1.66 1.34 3 3 3zm5.3-3c0 3-2.54 5.1-5.3 5.1S6.7 14 6.7 11H5c0 3.41 2.72 6.23 6 6.72V21h2v-3.28c3.28-.49 6-3.31 6-6.72h-1.7z"/></svg>',
  poll:    '<svg viewBox="0 0 24 24"><path d="M19 5v14H5V5h14m0-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zM7 10h2v7H7zm4-3h2v10h-2zm4 6h2v4h-2z"/></svg>',
  photo:   '<svg viewBox="0 0 24 24"><path d="M21 19V5c0-1.1-.9-2-2-2H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2zM8.5 13.5l2.5 3.01L14.5 12l4.5 6H5l3.5-4.5z"/></svg>',
  sticker: '<svg viewBox="0 0 24 24"><path d="M19 2H5c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h11l6-6V4c0-1.1-.9-2-2-2zm-6 14v-3h3l-3 3z"/></svg>',
  user:    '<svg viewBox="0 0 24 24"><path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/></svg>',
  bot:     '<svg viewBox="0 0 24 24"><path d="M20 9V7c0-1.1-.9-2-2-2h-3V3c0-.55-.45-1-1-1h-4c-.55 0-1 .45-1 1v2H6c-1.1 0-2 .9-2 2v2H3c-.55 0-1 .45-1 1v4c0 .55.45 1 1 1h1v2c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2v-2h1c.55 0 1-.45 1-1v-4c0-.55-.45-1-1-1h-1zM7.5 11.5a1.5 1.5 0 1 1 3 0 1.5 1.5 0 0 1-3 0zm9 0a1.5 1.5 0 1 1 3 0 1.5 1.5 0 0 1-3 0zM8 16h8v1H8z"/></svg>',
  settings:'<svg viewBox="0 0 24 24"><path d="M19.43 12.98c.04-.32.07-.64.07-.98s-.03-.66-.07-.98l2.11-1.65a.51.51 0 0 0 .12-.64l-2-3.46a.5.5 0 0 0-.61-.22l-2.49 1c-.52-.4-1.08-.73-1.69-.98l-.38-2.65A.5.5 0 0 0 14 2h-4a.5.5 0 0 0-.5.42l-.38 2.65c-.61.25-1.17.58-1.69.98l-2.49-1a.5.5 0 0 0-.61.22l-2 3.46a.5.5 0 0 0 .12.64L4.57 11c-.04.32-.07.65-.07.98s.03.66.07.98L2.46 14.62a.51.51 0 0 0-.12.64l2 3.46a.5.5 0 0 0 .61.22l2.49-1c.52.4 1.08.73 1.69.98l.38 2.65c.07.25.27.42.5.42h4c.25 0 .45-.17.5-.42l.38-2.65c.61-.25 1.17-.58 1.69-.98l2.49 1c.23.09.49 0 .61-.22l2-3.46a.5.5 0 0 0-.12-.64l-2.11-1.66zM12 15.5c-1.93 0-3.5-1.57-3.5-3.5s1.57-3.5 3.5-3.5 3.5 1.57 3.5 3.5-1.57 3.5-3.5 3.5z"/></svg>',
  shield:  '<svg viewBox="0 0 24 24"><path d="M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4z"/></svg>',
  spark:   '<svg viewBox="0 0 24 24"><path d="M19 5v3l3-3-3-3v3H7c-2.21 0-4 1.79-4 4s1.79 4 4 4h10c1.1 0 2 .9 2 2s-.9 2-2 2H5v-3l-3 3 3 3v-3h12c2.21 0 4-1.79 4-4s-1.79-4-4-4H7c-1.1 0-2-.9-2-2s.9-2 2-2h12z"/></svg>',
  cube:    '<svg viewBox="0 0 24 24"><path d="M12 2 4 6v12l8 4 8-4V6l-8-4zm0 2.3L17.85 7 12 9.7 6.15 7 12 4.3zM6 8.45l5 2.3v8.8l-5-2.5V8.45zm12 0v8.6l-5 2.5v-8.8l5-2.3z"/></svg>',
  bell:    '<svg viewBox="0 0 24 24"><path d="M12 22c1.1 0 2-.9 2-2h-4c0 1.1.9 2 2 2zm6-6V11c0-3.07-1.63-5.64-4.5-6.32V4c0-.83-.67-1.5-1.5-1.5s-1.5.67-1.5 1.5v.68C7.64 5.36 6 7.92 6 11v5l-2 2v1h16v-1l-2-2z"/></svg>',
  ban:     '<svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zM4 12c0-4.42 3.58-8 8-8 1.85 0 3.55.63 4.9 1.69L5.69 16.9C4.63 15.55 4 13.85 4 12zm8 8c-1.85 0-3.55-.63-4.9-1.69L18.31 7.1C19.37 8.45 20 10.15 20 12c0 4.42-3.58 8-8 8z"/></svg>',
  link:    '<svg viewBox="0 0 24 24"><path d="M3.9 12c0-1.71 1.39-3.1 3.1-3.1h4V7H7a5 5 0 0 0 0 10h4v-1.9H7c-1.71 0-3.1-1.39-3.1-3.1zM8 13h8v-2H8v2zm9-6h-4v1.9h4c1.71 0 3.1 1.39 3.1 3.1s-1.39 3.1-3.1 3.1h-4V17h4a5 5 0 0 0 0-10z"/></svg>',
};

// ============= 1. Auth gate ================================================

async function showGate() {
  $('#gate').hidden = false;
  $('#app').hidden = true;
  $('#tokInp').value = '';
  $('#tokInp').focus();
  return new Promise(resolve => {
    const submit = async () => {
      const v = $('#tokInp').value.trim();
      if (!v) return;
      try {
        const r = await fetch('/api/auth', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ token: v }),
        });
        if (!r.ok) throw new Error('invalid');
        TOKEN = v;
        localStorage.setItem(TOKEN_KEY, TOKEN);
        $('#gate').hidden = true;
        $('#app').hidden = false;
        resolve();
      } catch (e) {
        toast('Неверный токен');
      }
    };
    $('#tokBtn').onclick = submit;
    $('#tokInp').onkeydown = e => { if (e.key === 'Enter') submit(); };
  });
}

// ============= 2. API client ==============================================

async function api(path, opts = {}) {
  const url = new URL(path, location.href);
  url.searchParams.set('token', TOKEN);
  const init = { ...opts };
  if (init.body && typeof init.body === 'object' && !(init.body instanceof FormData)) {
    init.headers = { 'Content-Type': 'application/json', ...(init.headers || {}) };
    init.body = JSON.stringify(init.body);
  }
  const r = await fetch(url, init);
  if (r.status === 401) {
    localStorage.removeItem(TOKEN_KEY);
    location.reload();
    throw new Error('unauthorized');
  }
  if (!r.ok) {
    let msg = await r.text().catch(() => '');
    try { msg = JSON.parse(msg).error || msg; } catch {}
    throw new Error(msg || ('HTTP ' + r.status));
  }
  return r.json();
}

// ============= 3. WebSocket ===============================================

let ws = null;
let wsBackoff = 1000;
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(TOKEN)}`);
  ws.onopen = () => { wsBackoff = 1000; };
  ws.onmessage = ev => {
    let data; try { data = JSON.parse(ev.data); } catch { return; }
    handleEvent(data);
  };
  ws.onclose = () => {
    setTimeout(connectWS, wsBackoff);
    wsBackoff = Math.min(wsBackoff * 2, 15000);
  };
  ws.onerror = () => { try { ws.close(); } catch {} };
}

// ============= 4. State store =============================================

const state = {
  me: null,           // bot info
  chats: new Map(),   // chat_id -> chat dict
  messages: new Map(),// chat_id -> Map(message_id -> msg)
  users: new Map(),   // user_id -> user dict
  current: null,      // current chat id
  tab: 'all',         // 'all' | 'private' | 'group' | 'channel'
  search: '',
  reply: null,        // {chat_id, message_id, text, name}
  edit: null,         // {chat_id, message_id, text}
  customEmoji: new Map(), // emoji_id -> meta
  page: null,         // open settings page
  drawerKind: null,   // 'chat' | 'user'
};

function getMsgMap(chatId) {
  if (!state.messages.has(chatId)) state.messages.set(chatId, new Map());
  return state.messages.get(chatId);
}

// ============= Event handling =============================================

function handleEvent(data) {
  switch (data.type) {
    case 'message':
    case 'message_edit': {
      const p = data.data || data;
      if (p && p.chat_id) {
        const map = getMsgMap(p.chat_id);
        // Pre-parse json fields if present
        for (const f of ['entities_json', 'poll_json', 'reply_markup_json', 'reactions_json']) {
          if (p[f] && typeof p[f] === 'string') {
            try { p[f.replace('_json', '')] = JSON.parse(p[f]); } catch {}
          }
        }
        map.set(p.message_id, { ...map.get(p.message_id), ...p });
        const c = state.chats.get(p.chat_id);
        if (c) {
          c.last_message = { text: p.text, caption: p.caption, media_type: p.media_type, date: p.date, sender_id: p.sender_id, message_id: p.message_id, deleted: p.deleted };
          c.last_message_at = p.date;
          renderChatList();
        } else {
          loadChats();
        }
        if (state.current === p.chat_id) renderMessages();
      }
      break;
    }
    case 'delete': {
      const m = getMsgMap(data.chat_id).get(data.message_id);
      if (m) m.deleted = 1;
      if (state.current === data.chat_id) renderMessages();
      break;
    }
    case 'reaction': {
      const m = getMsgMap(data.chat_id).get(data.message_id);
      if (m) {
        m.reactions = data.reactions;
        if (state.current === data.chat_id) renderMessages();
      }
      break;
    }
    case 'poll': {
      // Update poll inside all messages with this poll_id
      for (const [chatId, msgs] of state.messages) {
        for (const m of msgs.values()) {
          if (m.poll && m.poll.id === data.poll_id) {
            m.poll = data.poll;
            if (chatId === state.current) renderMessages();
          }
        }
      }
      break;
    }
    case 'poll_vote': {
      // Refresh voters of current open poll modal
      if (state._pollVoterModal && state._pollVoterModal.poll_id === data.poll_id) {
        loadPollVoters(data.poll_id);
      }
      break;
    }
    case 'chat_member':
    case 'member': {
      loadChats();
      break;
    }
  }
}

// ============= 5. Sidebar =================================================

async function loadChats() {
  try {
    const chats = await api('/api/chats');
    state.chats.clear();
    for (const c of chats) state.chats.set(c.id, c);
    renderChatList();
  } catch (e) {
    console.error(e);
  }
}

function chatRowPreview(c) {
  const lm = c.last_message;
  if (!lm) return ce('span', { class: 'muted' }, 'Нет сообщений');
  if (lm.deleted) return ce('span', {}, ce('i', {}, 'Удалено'));
  const mt = lm.media_type;
  if (mt === 'photo')     return '📷 ' + (lm.caption || 'Фото');
  if (mt === 'video')     return '🎬 ' + (lm.caption || 'Видео');
  if (mt === 'animation') return '🎞 ' + (lm.caption || 'GIF');
  if (mt === 'sticker')   return '🎟 Стикер';
  if (mt === 'voice')     return '🎤 Голосовое';
  if (mt === 'audio')     return '🎵 ' + (lm.caption || 'Аудио');
  if (mt === 'document')  return '📎 ' + (lm.caption || 'Файл');
  if (mt === 'video_note')return '🟢 Кружок';
  if (mt === 'poll')      return '📊 Опрос';
  return (lm.text || lm.caption || '').replace(/\s+/g, ' ').slice(0, 80);
}

function chatTags(c) {
  const out = [];
  if (c.type === 'private' && state.users.get(c.id)?.is_bot) out.push(ce('span', { class: 'tag-bot' }, 'BOT'));
  if (c.type === 'private' && state.users.get(c.id)?.is_premium) out.push(ce('span', { class: 'badge-star', title: 'Premium' }, '★'));
  if (c.type === 'channel') out.push(ce('span', { class: 'tag-channel' }, 'КАНАЛ'));
  if (c.type === 'supergroup' || c.type === 'group') out.push(ce('span', { class: 'tag-group' }, 'ГРУППА'));
  return out;
}

function renderChatList() {
  const list = $('#chatList');
  list.innerHTML = '';
  const arr = Array.from(state.chats.values());
  const filtered = arr.filter(c => {
    if (state.tab === 'private' && c.type !== 'private') return false;
    if (state.tab === 'group' && c.type !== 'group' && c.type !== 'supergroup') return false;
    if (state.tab === 'channel' && c.type !== 'channel') return false;
    if (state.search) {
      const q = state.search.toLowerCase();
      const t = (c.title || '').toLowerCase();
      const u = (c.username || '').toLowerCase();
      if (!t.includes(q) && !u.includes(q)) return false;
    }
    return true;
  });
  filtered.sort((a, b) => (b.last_message_at || 0) - (a.last_message_at || 0));
  for (const c of filtered) {
    const row = ce('div', {
      class: 'chat-row' + (state.current === c.id ? ' active' : ''),
      onclick: () => openChat(c.id),
    });
    row.appendChild(ce('div', {
      class: 'avatar ' + avColor(c.id),
    }, avInitial(c.title || c.username || String(c.id))));
    const top = ce('div', { class: 'chat-row-top' });
    top.appendChild(ce('div', { class: 'chat-row-title' }, c.title || c.username || String(c.id), ...chatTags(c)));
    row.appendChild(top);
    if (c.last_message_at) row.appendChild(ce('div', { class: 'chat-row-time' }, fmtTime(c.last_message_at)));
    const prev = ce('div', { class: 'chat-row-preview' });
    const pv = chatRowPreview(c);
    if (typeof pv === 'string') prev.textContent = pv;
    else prev.appendChild(pv);
    row.appendChild(prev);
    list.appendChild(row);
  }
  if (!filtered.length) {
    list.appendChild(ce('div', { style: 'padding:24px; text-align:center; color:var(--tg-text-3); font-size:13px' },
      state.search ? 'Ничего не найдено' : 'Бот ещё не получал сообщений — добавьте его в группу или напишите ему в личку.'));
  }
}

function setupSidebar() {
  $$('.side-tab').forEach(t => t.onclick = () => {
    state.tab = t.dataset.tab;
    $$('.side-tab').forEach(x => x.classList.toggle('active', x === t));
    renderChatList();
  });
  $('#searchInp').oninput = debounce(e => {
    state.search = e.target.value;
    renderChatList();
  }, 120);
  $('#menuBtn').onclick = openSideMenu;
}

function openSideMenu(ev) {
  closeContextMenu();
  const menu = ce('div', { class: 'ctx-menu', style: { left: '10px', top: '60px' } });
  const item = (icon, label, fn) => ce('button', { onclick: () => { menu.remove(); fn(); } }, ce('span', { class: 'icon', html: icon }), label);
  menu.append(
    item(SVG.bot, 'Профиль бота', () => openPage('profile')),
    item(SVG.shield, 'Авто-модерация', () => openPage('moderation')),
    item(SVG.spark, 'AI-ассистент', () => openPage('ai')),
    item(SVG.cube, 'Мини-апы', () => openPage('mini')),
    item(SVG.settings, 'Настройки', () => openPage('settings')),
    document.createElement('hr'),
    item(SVG.doc, 'Журнал событий', () => openPage('audit')),
    item(SVG.close, 'Выйти', () => { localStorage.removeItem(TOKEN_KEY); location.reload(); }),
  );
  document.body.appendChild(menu);
  const closeNow = ev2 => { if (!menu.contains(ev2.target)) { menu.remove(); document.removeEventListener('click', closeNow, true); } };
  setTimeout(() => document.addEventListener('click', closeNow, true), 0);
}

// ============= 6. Chat view ===============================================

async function openChat(chatId) {
  state.current = chatId;
  document.body.classList.add('in-chat');
  renderChatList();
  await loadMessages(chatId);
  renderChat();
}

function closeChat() {
  state.current = null;
  document.body.classList.remove('in-chat');
  state.reply = null;
  state.edit = null;
  $('#main').className = 'main placeholder';
  $('#main').innerHTML = `<div class="empty-state">
    <div style="font-size:48px;margin-bottom:12px;">💬</div>
    <div style="font-size:15px;color:var(--tg-text);">Выберите чат, чтобы начать общение</div>
  </div>`;
  renderChatList();
}

async function loadMessages(chatId) {
  try {
    const r = await api(`/api/chats/${chatId}/messages?limit=200`);
    const map = getMsgMap(chatId);
    map.clear();
    for (const m of r.messages || []) map.set(m.message_id, m);
    for (const [uid, u] of Object.entries(r.senders || {})) state.users.set(+uid, u);
  } catch (e) { console.error(e); }
}

function renderChat() {
  const chat = state.chats.get(state.current);
  if (!chat) return;
  const main = $('#main');
  main.className = 'main';
  main.innerHTML = '';

  // Header
  const header = ce('header', { class: 'chat-header' });
  const backBtn = ce('button', { class: 'icon-btn', html: SVG.back, onclick: e => { e.stopPropagation(); closeChat(); } });
  if (window.innerWidth <= 768) header.appendChild(backBtn);
  header.appendChild(ce('div', { class: 'avatar s40 ' + avColor(chat.id) }, avInitial(chat.title)));
  const meta = ce('div', { class: 'chat-header-meta' });
  meta.appendChild(ce('div', { class: 'chat-header-title' }, chat.title || String(chat.id), ...chatTags(chat)));
  const statusText = chatStatus(chat);
  meta.appendChild(ce('div', { class: 'chat-header-status' }, statusText));
  header.appendChild(meta);
  const actions = ce('div', { class: 'chat-header-actions' });
  actions.appendChild(ce('button', { class: 'icon-btn', html: SVG.search, onclick: e => { e.stopPropagation(); /* TODO: in-chat search */ } }));
  actions.appendChild(ce('button', { class: 'icon-btn', html: SVG.more, onclick: e => { e.stopPropagation(); openChatMenu(e); } }));
  header.appendChild(actions);
  header.onclick = () => openChatDrawer(chat);
  main.appendChild(header);

  // Messages
  const scroll = ce('div', { class: 'msgs-scroll', id: 'msgsScroll' });
  const inner = ce('div', { class: 'msgs-inner', id: 'msgsInner' });
  scroll.appendChild(inner);
  main.appendChild(scroll);

  // Composer
  main.appendChild(renderComposer());

  renderMessages();
  setTimeout(() => { scroll.scrollTop = scroll.scrollHeight; }, 0);
}

function chatStatus(c) {
  if (c.type === 'private') {
    const u = state.users.get(c.id);
    if (u && u.is_bot) return 'бот';
    return 'был(а) недавно';
  }
  if (c.members_count) return c.members_count + ' участников';
  if (c.type === 'channel') return 'канал';
  if (c.type === 'supergroup' || c.type === 'group') return 'группа';
  return '';
}

function openChatMenu(ev) {
  closeContextMenu();
  const rect = ev.currentTarget.getBoundingClientRect();
  const menu = ce('div', { class: 'ctx-menu', style: { right: '10px', top: (rect.bottom + 4) + 'px' } });
  const it = (icon, label, fn, danger) => ce('button', { class: danger ? 'danger' : '', onclick: () => { menu.remove(); fn(); } }, ce('span', { html: icon }), label);
  const c = state.chats.get(state.current);
  menu.append(
    it(SVG.user, 'Информация о чате', () => openChatDrawer(c)),
    it(SVG.shield, 'Авто-модерация', () => openModerationFor(c.id)),
    it(SVG.copy, 'Скопировать ID', () => { navigator.clipboard.writeText(String(c.id)); toast('ID скопирован'); }),
    it(SVG.ban, 'Выйти из чата', () => leaveChat(c.id), true),
  );
  document.body.appendChild(menu);
  const closeNow = ev2 => { if (!menu.contains(ev2.target)) { menu.remove(); document.removeEventListener('click', closeNow, true); } };
  setTimeout(() => document.addEventListener('click', closeNow, true), 0);
}

async function leaveChat(chatId) {
  if (!confirm('Бот выйдет из этого чата. Подтвердить?')) return;
  try {
    await api('/api/bot/settings', { method: 'POST', body: { op: 'leave_chat', chat_id: chatId } });
    state.chats.delete(chatId);
    closeChat();
    renderChatList();
    toast('Бот покинул чат');
  } catch (e) { toast('Ошибка: ' + e.message); }
}

// ============= 7. Message renderers =======================================

function renderMessages() {
  const inner = $('#msgsInner'); if (!inner) return;
  inner.innerHTML = '';
  const map = getMsgMap(state.current);
  // Ordered ascending by date
  const arr = Array.from(map.values()).sort((a, b) => a.date - b.date);

  // Group by media_group_id
  const groups = new Map();
  for (const m of arr) {
    if (m.media_group_id) {
      if (!groups.has(m.media_group_id)) groups.set(m.media_group_id, []);
      groups.get(m.media_group_id).push(m);
    }
  }
  const renderedGroup = new Set();

  // Walk in reverse (newest first), DOM goes column-reverse so first-pushed is bottom
  let prevDay = null;
  for (let i = arr.length - 1; i >= 0; i--) {
    const m = arr[i];
    if (m.media_group_id && renderedGroup.has(m.media_group_id)) continue;
    if (m.media_group_id) {
      renderedGroup.add(m.media_group_id);
      const all = groups.get(m.media_group_id).sort((a, b) => a.message_id - b.message_id);
      inner.appendChild(renderMessageGroup(all));
    } else {
      const next = arr[i - 1];
      const prev = arr[i + 1];
      inner.appendChild(renderSingleMessage(m, prev, next));
    }
    // Day separator
    const d = new Date(m.date * 1000).toDateString();
    const nextEarlier = i > 0 ? arr[i - 1] : null;
    if (!nextEarlier || new Date(nextEarlier.date * 1000).toDateString() !== d) {
      inner.appendChild(ce('div', { class: 'date-sep' }, fmtDateHeading(m.date)));
    }
  }
}

function renderSingleMessage(m, prev, next) {
  if (m.deleted) {
    return ce('div', { class: 'svc' }, 'Сообщение удалено');
  }
  const me = state.me?.id;
  const out = m.sender_id === me;
  const sameSender = prev && prev.sender_id === m.sender_id && !prev.media_group_id && (m.date - prev.date) < 300;
  const sameSenderNext = next && next.sender_id === m.sender_id && !next.media_group_id && (next.date - m.date) < 300;

  const row = ce('div', { class: 'msg ' + (out ? 'out' : 'in'), dataset: { mid: m.message_id } });
  row._msg = m;

  if (!out && !sameSenderNext) {
    const sender = state.users.get(m.sender_id) || {};
    row.appendChild(ce('div', {
      class: 'avatar s32 ' + avColor(m.sender_id),
      onclick: e => { e.stopPropagation(); openUserDrawer(m.sender_id); }
    }, avInitial(sender.first_name || sender.username || '?')));
  } else if (!out) {
    row.appendChild(ce('div', { class: 'avatar s32', style: { visibility: 'hidden' } }));
  }

  const stack = ce('div', { class: 'msg-stack' });
  stack.appendChild(renderBubble(m, { out, sameSender, sameSenderNext }));
  row.appendChild(stack);

  // Swipe-to-reply indicator
  row.appendChild(ce('div', { class: 'swipe-reply-indicator', html: SVG.reply }));

  attachGestures(row, m);
  return row;
}

function renderMessageGroup(msgs) {
  const m = msgs[0];
  const me = state.me?.id;
  const out = m.sender_id === me;
  const row = ce('div', { class: 'msg ' + (out ? 'out' : 'in') });
  if (!out) {
    const sender = state.users.get(m.sender_id) || {};
    row.appendChild(ce('div', {
      class: 'avatar s32 ' + avColor(m.sender_id),
      onclick: e => { e.stopPropagation(); openUserDrawer(m.sender_id); }
    }, avInitial(sender.first_name || sender.username || '?')));
  }
  const stack = ce('div', { class: 'msg-stack' });
  const bubble = ce('div', { class: 'bubble' + (msgs.some(x => x.media_type === 'document') ? '' : ' media-only') });
  if (!out && (msgs[0].caption || msgs[0].text)) {
    // sender label
    const sender = state.users.get(m.sender_id) || {};
    const nameRow = ce('div', { class: 'bubble-name' }, sender.first_name || sender.username || '');
    if (sender.is_premium) nameRow.appendChild(ce('span', { class: 'badge-star' }, '★'));
    bubble.appendChild(nameRow);
  }

  // Media grid
  const n = msgs.length;
  const cols = n === 1 ? 1 : n === 2 ? 2 : n <= 4 ? 2 : 3;
  const grid = ce('div', { class: 'media-group cols-' + cols });
  for (const mm of msgs) grid.appendChild(renderMediaGridItem(mm));
  bubble.appendChild(grid);

  // Caption from first message that has one
  const caption = msgs.map(x => x.caption || '').find(Boolean) || '';
  if (caption) bubble.appendChild(ce('div', { class: 'bubble-caption', html: linkify(esc(caption)) }));

  bubble.appendChild(renderBubbleMeta(m, out));
  stack.appendChild(bubble);
  row.appendChild(stack);
  row.appendChild(ce('div', { class: 'swipe-reply-indicator', html: SVG.reply }));
  attachGestures(row, m);
  return row;
}

function renderMediaGridItem(m) {
  const item = ce('div', { class: 'mg-item' });
  if (m.media_type === 'photo') {
    item.appendChild(ce('img', { src: '/file/' + m.file_id + '?token=' + encodeURIComponent(TOKEN), loading: 'lazy', onclick: () => openImageViewer(m.file_id) }));
  } else if (m.media_type === 'video' || m.media_type === 'animation') {
    item.appendChild(ce('video', {
      src: '/file/' + m.file_id + '?token=' + encodeURIComponent(TOKEN),
      muted: true, loop: true, playsinline: true, preload: 'metadata',
      onclick: () => openImageViewer(m.file_id, 'video'),
    }));
  } else if (m.media_type === 'document') {
    item.appendChild(renderDocBlock(m));
  }
  return item;
}

function renderBubble(m, { out, sameSender, sameSenderNext }) {
  const isSticker = m.media_type === 'sticker';
  const isMedia = ['photo', 'video', 'animation'].includes(m.media_type);
  const bubble = ce('div', { class: 'bubble' });
  if (isSticker) bubble.classList.add('sticker-only');
  if (isMedia && !(m.text || m.caption)) bubble.classList.add('media-only');
  if (sameSender) bubble.classList.add('same-prev');
  if (sameSenderNext) bubble.classList.add('same-next');
  if (!sameSenderNext) bubble.classList.add(out ? 'last-out' : 'last-in');

  // Sender name (groups only, not for own messages, only on top of stack)
  if (!out && !sameSender) {
    const c = state.chats.get(state.current);
    if (c && c.type !== 'private') {
      const sender = state.users.get(m.sender_id) || {};
      const nameRow = ce('div', { class: 'bubble-name' }, sender.first_name || sender.username || '');
      if (sender.is_premium) nameRow.appendChild(ce('span', { class: 'badge-star' }, '★'));
      bubble.appendChild(nameRow);
    }
  }

  // Reply
  if (m.reply_to_message_id) {
    const rep = getMsgMap(state.current).get(m.reply_to_message_id);
    const repSender = rep ? state.users.get(rep.sender_id) : null;
    bubble.appendChild(ce('div', {
      class: 'bubble-reply',
      onclick: e => { e.stopPropagation(); scrollToMessage(m.reply_to_message_id); }
    },
      ce('div', { class: 'bubble-reply-name' }, repSender ? (repSender.first_name || repSender.username || '') : 'Сообщение'),
      ce('div', { class: 'bubble-reply-text' }, rep ? ((rep.text || rep.caption || mediaLabel(rep) || '').slice(0, 80)) : '...'),
    ));
  }

  // Media (single, not group)
  if (!m.media_group_id && m.media_type) {
    const mediaEl = renderMedia(m);
    if (mediaEl) bubble.appendChild(mediaEl);
  }

  // Text
  const txt = m.text || m.caption || '';
  if (txt && !isSticker) {
    const div = ce('div', { class: (m.media_type ? 'bubble-caption' : 'bubble-text'), html: linkify(applyEntities(txt, m.entities)) });
    bubble.appendChild(div);
    bindCustomEmojiObserver(div);
    bindSpoilers(div);
  }

  // Poll
  if (m.poll) bubble.appendChild(renderPoll(m));

  // Inline keyboard
  if (m.reply_markup && m.reply_markup.inline_keyboard) {
    bubble.appendChild(renderInlineKeyboard(m));
  }

  // Reactions
  if (m.reactions && m.reactions.length) {
    bubble.appendChild(renderReactions(m));
  }

  bubble.appendChild(renderBubbleMeta(m, out));
  bubble.classList.add('has-meta');
  return bubble;
}

function renderBubbleMeta(m, out) {
  const meta = ce('span', { class: 'bubble-meta' });
  if (m.edit_date) meta.appendChild(ce('span', { class: 'edited' }, 'изм.'));
  meta.appendChild(document.createTextNode(fmtTime(m.date)));
  if (out) {
    const tick = ce('span', { class: 'tick tick-2', title: 'Доставлено' });
    meta.appendChild(tick);
  }
  return meta;
}

function mediaLabel(m) {
  const map = { photo: '📷 Фото', video: '🎬 Видео', animation: '🎞 GIF', sticker: '🎟 Стикер',
                voice: '🎤 Голосовое', audio: '🎵 Аудио', document: '📎 Файл',
                video_note: '🟢 Видео-кружок', poll: '📊 Опрос' };
  return map[m.media_type] || '';
}

function renderMedia(m) {
  const url = '/file/' + m.file_id + '?token=' + encodeURIComponent(TOKEN);
  if (m.media_type === 'photo') {
    return ce('img', {
      class: 'media-photo', src: url, loading: 'lazy',
      onclick: () => openImageViewer(m.file_id),
    });
  }
  if (m.media_type === 'video' || m.media_type === 'animation') {
    return ce('video', {
      class: 'media-video', src: url, controls: m.media_type === 'video' ? 'controls' : false,
      autoplay: m.media_type === 'animation', loop: m.media_type === 'animation',
      muted: m.media_type === 'animation', playsinline: true, preload: 'metadata',
      onclick: () => { if (m.media_type === 'animation') openImageViewer(m.file_id, 'video'); },
    });
  }
  if (m.media_type === 'sticker') {
    // .webp / .webm / .tgs
    if (m.sticker_is_video) {
      return ce('video', {
        class: 'sticker', src: url, autoplay: true, loop: true, muted: true, playsinline: true,
        onclick: () => openImageViewer(m.file_id, 'video'),
      });
    }
    if (m.sticker_is_animated) {
      // Lottie .tgs — we can't easily decode; fall back to thumbnail
      const thumb = m.thumb_file_id ? '/file/' + m.thumb_file_id + '?token=' + encodeURIComponent(TOKEN) : url;
      return ce('img', { class: 'sticker', src: thumb, alt: m.sticker_emoji || '' });
    }
    return ce('img', { class: 'sticker', src: url, alt: m.sticker_emoji || '' });
  }
  if (m.media_type === 'document') return renderDocBlock(m);
  if (m.media_type === 'voice' || m.media_type === 'audio') return renderAudioBlock(m, url);
  if (m.media_type === 'video_note') {
    return ce('video', {
      class: 'sticker',
      style: { borderRadius: '50%', width: '200px', height: '200px', objectFit: 'cover' },
      src: url, controls: 'controls', playsinline: true, preload: 'metadata',
    });
  }
  return null;
}

function renderDocBlock(m) {
  const url = '/file/' + m.file_id + '?token=' + encodeURIComponent(TOKEN);
  const wrap = ce('div', { class: 'media-doc', onclick: e => { e.stopPropagation(); window.open(url, '_blank'); } });
  wrap.appendChild(ce('div', { class: 'media-doc-icon', html: SVG.doc }));
  const info = ce('div');
  info.appendChild(ce('div', { class: 'media-doc-name' }, m.file_name || 'Файл'));
  info.appendChild(ce('div', { class: 'media-doc-meta' }, fmtBytes(m.file_size)));
  wrap.appendChild(info);
  return wrap;
}

function renderAudioBlock(m, url) {
  const wrap = ce('div', { class: 'media-audio' });
  wrap.appendChild(ce('div', { class: 'media-doc-icon', html: m.media_type === 'voice' ? SVG.mic : SVG.play }));
  const info = ce('div');
  info.appendChild(ce('audio', { controls: 'controls', src: url, preload: 'metadata', style: { width: '220px' } }));
  if (m.media_type === 'audio' && m.file_name) info.appendChild(ce('div', { class: 'media-doc-meta' }, m.file_name));
  wrap.appendChild(info);
  return wrap;
}

function renderPoll(m) {
  const p = m.poll;
  const wrap = ce('div', { class: 'poll' });
  wrap.appendChild(ce('div', { class: 'poll-q' }, p.question));
  const total = (p.options || []).reduce((a, o) => a + (o.voter_count || 0), 0);
  wrap.appendChild(ce('div', { class: 'poll-meta' },
    p.is_anonymous ? 'Анонимный опрос · ' : 'Открытый опрос · ',
    total + ' голос' + (total % 10 === 1 && total % 100 !== 11 ? '' : total % 10 >= 2 && total % 10 <= 4 && (total % 100 < 12 || total % 100 > 14) ? 'а' : 'ов'),
    p.is_closed ? ' · завершён' : '',
  ));
  for (let i = 0; i < (p.options || []).length; i++) {
    const o = p.options[i];
    const pct = total ? Math.round((o.voter_count || 0) * 100 / total) : 0;
    const opt = ce('div', { class: 'poll-opt' + (o.is_chosen ? ' checked' : '') });
    opt.appendChild(ce('div', { class: 'opt-marker' }));
    opt.appendChild(ce('div', { class: 'opt-text' }, o.text));
    opt.appendChild(ce('div', { class: 'opt-pct' }, pct + '%'));
    const bar = ce('div', { class: 'opt-bar' });
    bar.appendChild(ce('div', { class: 'opt-bar-fill', style: { width: pct + '%' } }));
    opt.appendChild(bar);
    wrap.appendChild(opt);
  }
  if (!p.is_anonymous) {
    wrap.appendChild(ce('div', {
      class: 'poll-voters-btn',
      onclick: e => { e.stopPropagation(); openPollVoters(p.id); },
    }, 'Кто голосовал →'));
  }
  return wrap;
}

function renderInlineKeyboard(m) {
  const ikb = ce('div', { class: 'ikb' });
  for (const row of m.reply_markup.inline_keyboard) {
    const rowEl = ce('div', { class: 'ikb-row' });
    for (const btn of row) {
      const b = ce('div', {
        class: 'ikb-btn',
        onclick: e => {
          e.stopPropagation();
          if (btn.url) window.open(btn.url, '_blank');
          else if (btn.callback_data) toast('Это кнопка callback_data — нажатие исполнит обработчик в боте');
          else if (btn.switch_inline_query) toast('Inline-кнопка');
        },
      }, btn.text);
      if (btn.url) b.appendChild(ce('span', { class: 'ext' }, '↗'));
      rowEl.appendChild(b);
    }
    ikb.appendChild(rowEl);
  }
  return ikb;
}

function renderReactions(m) {
  const wrap = ce('div', { class: 'reactions' });
  // Aggregate by emoji
  const counts = {};
  let myEmoji = null;
  for (const r of m.reactions || []) {
    const em = r.type === 'emoji' ? r.emoji : (r.type === 'custom_emoji' ? '★' : '?');
    counts[em] = (counts[em] || 0) + 1;
    if (r.user_id === state.me?.id) myEmoji = em;
  }
  for (const [em, n] of Object.entries(counts)) {
    wrap.appendChild(ce('div', {
      class: 'reaction' + (em === myEmoji ? ' me' : ''),
      onclick: e => { e.stopPropagation(); setReaction(m, em === myEmoji ? null : em); },
    }, ce('span', { class: 'em' }, em), n > 1 ? String(n) : ''));
  }
  return wrap;
}

// Text entities (links, bold, italic, mentions, code, spoilers, custom emoji)
function applyEntities(text, entities) {
  if (!entities || !entities.length) return esc(text);
  // entities are byte-offsets in UTF-16, that's how Telegram returns them
  const arr = Array.from(text);
  const events = [];
  for (const e of entities) {
    events.push({ pos: e.offset, type: 'open', e });
    events.push({ pos: e.offset + e.length, type: 'close', e });
  }
  events.sort((a, b) => a.pos - b.pos || (a.type === 'open' ? 1 : -1));
  let out = '';
  let cursor = 0;
  const open = (e) => {
    switch (e.type) {
      case 'bold': return '<b>';
      case 'italic': return '<i>';
      case 'underline': return '<u>';
      case 'strikethrough': return '<s>';
      case 'code': return '<code>';
      case 'pre': return '<pre>';
      case 'spoiler': return '<span class="spoiler">';
      case 'url': return `<a href="${esc(text.substr(e.offset, e.length))}" target="_blank">`;
      case 'text_link': return `<a href="${esc(e.url)}" target="_blank">`;
      case 'mention': return `<span class="mention">`;
      case 'text_mention': return `<span class="mention" data-uid="${e.user?.id || ''}">`;
      case 'custom_emoji': return `<span class="cemoji" data-eid="${esc(e.custom_emoji_id || '')}">`;
      case 'hashtag': case 'cashtag': case 'bot_command': case 'email': case 'phone_number':
        return '<span class="mention">';
      default: return '';
    }
  };
  const close = (e) => {
    switch (e.type) {
      case 'bold': return '</b>';
      case 'italic': return '</i>';
      case 'underline': return '</u>';
      case 'strikethrough': return '</s>';
      case 'code': return '</code>';
      case 'pre': return '</pre>';
      case 'spoiler': return '</span>';
      case 'url': case 'text_link': return '</a>';
      case 'mention': case 'text_mention': return '</span>';
      case 'custom_emoji': return '</span>';
      case 'hashtag': case 'cashtag': case 'bot_command': case 'email': case 'phone_number':
        return '</span>';
      default: return '';
    }
  };
  for (const ev of events) {
    out += esc(arr.slice(cursor, ev.pos).join(''));
    cursor = ev.pos;
    out += ev.type === 'open' ? open(ev.e) : close(ev.e);
  }
  out += esc(arr.slice(cursor).join(''));
  return out;
}

function linkify(html) {
  // Cheap fallback for plain URLs that weren't in entities
  return html.replace(/(^|[\s>])(https?:\/\/[^\s<]+)/g, (m, p, url) =>
    p + `<a href="${esc(url)}" target="_blank">${esc(url)}</a>`);
}

function bindSpoilers(root) {
  $$('.spoiler', root).forEach(s => s.onclick = () => s.classList.add('revealed'));
}

// Custom emoji rendering — fetch their file_ids lazily
const emojiObserver = new IntersectionObserver(async entries => {
  const ids = entries.filter(e => e.isIntersecting).map(e => e.target.dataset.eid).filter(Boolean);
  if (!ids.length) return;
  for (const e of entries) if (e.isIntersecting) emojiObserver.unobserve(e.target);
  const missing = ids.filter(id => !state.customEmoji.has(id));
  if (missing.length) {
    try {
      const r = await api('/api/custom_emoji', { method: 'POST', body: { ids: missing } });
      for (const [id, meta] of Object.entries(r)) state.customEmoji.set(id, meta);
    } catch {}
  }
  for (const e of entries) {
    if (!e.isIntersecting) continue;
    const eid = e.target.dataset.eid;
    const meta = state.customEmoji.get(eid);
    if (!meta) continue;
    const url = '/file/' + meta.file_id + '?token=' + encodeURIComponent(TOKEN);
    if (meta.is_video) {
      e.target.innerHTML = '';
      e.target.appendChild(ce('video', { src: url, autoplay: true, loop: true, muted: true, playsinline: true }));
    } else if (!meta.is_animated) {
      e.target.style.backgroundImage = `url('${url}')`;
    } else {
      // Static thumb fallback for lottie
      e.target.style.backgroundImage = `url('${url}')`;
    }
  }
}, { rootMargin: '200px' });

function bindCustomEmojiObserver(root) {
  $$('.cemoji', root).forEach(el => emojiObserver.observe(el));
}

function scrollToMessage(mid) {
  const el = $$('.msg').find(x => +x.dataset.mid === +mid);
  if (el) {
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    el.style.transition = 'background .3s';
    el.style.background = 'rgba(100,186,240,.18)';
    setTimeout(() => { el.style.background = ''; }, 1500);
  }
}

// ============= 8. Context menu + reactions ================================

let _ctxMenuEl = null;
function closeContextMenu() {
  if (_ctxMenuEl) { _ctxMenuEl.remove(); _ctxMenuEl = null; }
}

const QUICK_REACTS = ['👍','❤️','🔥','🥰','👏','😁','🤔','😢','😱','🎉','💯','🤯'];

function showMessageContextMenu(ev, m) {
  closeContextMenu();
  ev.preventDefault();
  const wrap = ce('div', { style: { position: 'fixed', left: 0, top: 0, zIndex: 200 } });
  const bar = ce('div', { class: 'ctx-react-bar' });
  for (const em of QUICK_REACTS) {
    bar.appendChild(ce('button', { onclick: () => { closeContextMenu(); setReaction(m, em); } }, em));
  }
  wrap.appendChild(bar);

  const menu = ce('div', { class: 'ctx-menu', style: { position: 'static' } });
  const it = (icon, label, fn, danger) => ce('button', { class: danger ? 'danger' : '', onclick: () => { closeContextMenu(); fn(); } }, ce('span', { html: icon }), label);
  menu.append(
    it(SVG.reply, 'Ответить', () => startReply(m)),
    it(SVG.copy, 'Скопировать текст', () => { navigator.clipboard.writeText(m.text || m.caption || ''); toast('Скопировано'); }),
    it(SVG.forward, 'Переслать', () => openForwardModal(m)),
    it(SVG.edit, 'Редактировать', () => startEdit(m)),
    it(SVG.pin, 'Закрепить', () => pinMessage(m)),
    document.createElement('hr'),
    it(SVG.trash, 'Удалить', () => deleteMessage(m), true),
  );
  wrap.appendChild(menu);
  document.body.appendChild(wrap);
  _ctxMenuEl = wrap;

  // Position
  const x = ev.clientX || (ev.touches && ev.touches[0].clientX) || 100;
  const y = ev.clientY || (ev.touches && ev.touches[0].clientY) || 100;
  const w = wrap.getBoundingClientRect().width;
  const h = wrap.getBoundingClientRect().height;
  wrap.style.left = Math.max(8, Math.min(x, window.innerWidth - w - 8)) + 'px';
  wrap.style.top  = Math.max(8, Math.min(y, window.innerHeight - h - 8)) + 'px';

  const closeNow = ev2 => { if (!wrap.contains(ev2.target)) closeContextMenu(); };
  setTimeout(() => document.addEventListener('click', closeNow, { capture: true, once: true }), 0);
}

async function setReaction(m, emoji) {
  try {
    await api('/api/reaction', { method: 'POST', body: {
      chat_id: m.chat_id, message_id: m.message_id, emoji: emoji,
    }});
  } catch (e) { toast('Ошибка реакции: ' + e.message); }
}

async function deleteMessage(m) {
  if (!confirm('Удалить сообщение?')) return;
  try {
    await api('/api/delete', { method: 'POST', body: { chat_id: m.chat_id, message_id: m.message_id } });
  } catch (e) { toast('Не получилось удалить: ' + e.message); }
}

async function pinMessage(m) {
  try {
    await api('/api/bot/settings', { method: 'POST', body: { op: 'pin', chat_id: m.chat_id, message_id: m.message_id, silent: false } });
    toast('Закреплено');
  } catch (e) { toast('Ошибка: ' + e.message); }
}

function startReply(m) {
  state.reply = m;
  state.edit = null;
  renderComposerBars();
  $('#composerInp')?.focus();
}

function startEdit(m) {
  state.edit = m;
  state.reply = null;
  $('#composerInp').textContent = m.text || m.caption || '';
  renderComposerBars();
  $('#composerInp')?.focus();
}

function openForwardModal(m) {
  const back = ce('div', { class: 'modal-back' });
  const modal = ce('div', { class: 'modal' });
  modal.appendChild(ce('h3', {}, 'Переслать сообщение'));
  const list = ce('div', { class: 'user-list' });
  const arr = Array.from(state.chats.values()).sort((a, b) => (b.last_message_at || 0) - (a.last_message_at || 0));
  for (const c of arr) {
    const item = ce('div', { class: 'item', onclick: async () => {
      try {
        await api('/api/forward', { method: 'POST', body: { from_chat_id: m.chat_id, to_chat_id: c.id, message_id: m.message_id } });
        toast('Переслано');
      } catch (e) { toast('Ошибка: ' + e.message); }
      back.remove();
    }});
    item.appendChild(ce('div', { class: 'avatar s40 ' + avColor(c.id) }, avInitial(c.title)));
    item.appendChild(ce('div', {}, ce('div', { style: { fontSize: '14px' } }, c.title || String(c.id)), ce('div', { style: { fontSize: '12px', color: 'var(--tg-text-3)' } }, chatStatus(c))));
    list.appendChild(item);
  }
  modal.appendChild(list);
  modal.appendChild(ce('div', { class: 'actions' }, ce('button', { onclick: () => back.remove() }, 'Отмена')));
  back.appendChild(modal);
  back.onclick = e => { if (e.target === back) back.remove(); };
  document.body.appendChild(back);
}

// ============= 9. Gestures ================================================

function attachGestures(row, m) {
  let startX = 0, startY = 0, dx = 0, dragging = false, longPress = null, longPressed = false;

  const pdown = e => {
    longPressed = false;
    if (e.button === 2) return;
    const p = e.touches ? e.touches[0] : e;
    startX = p.clientX; startY = p.clientY; dx = 0; dragging = false;
    longPress = setTimeout(() => {
      longPressed = true;
      if (navigator.vibrate) try { navigator.vibrate(20); } catch {}
      showMessageContextMenu(p, m);
    }, 500);
  };
  const pmove = e => {
    const p = e.touches ? e.touches[0] : e;
    const ddx = p.clientX - startX;
    const ddy = p.clientY - startY;
    if (Math.abs(ddx) > 8 || Math.abs(ddy) > 8) {
      if (longPress) { clearTimeout(longPress); longPress = null; }
    }
    if (!dragging && Math.abs(ddx) > 16 && Math.abs(ddx) > Math.abs(ddy)) dragging = true;
    if (dragging) {
      const me = state.me?.id;
      const out = m.sender_id === me;
      // Swipe direction: in messages, swipe-left for own, swipe-right for incoming
      const allowDir = out ? -1 : 1;
      dx = Math.max(-90, Math.min(90, ddx));
      if (Math.sign(dx) !== allowDir && Math.abs(dx) > 4) dx = 0;
      row.style.transform = `translateX(${dx}px)`;
      const ind = row.querySelector('.swipe-reply-indicator');
      if (ind) {
        const k = Math.min(1, Math.abs(dx) / 60);
        ind.style.transform = `translateY(-50%) scale(${k})`;
        ind.style.opacity = k;
      }
    }
  };
  const pup = () => {
    if (longPress) { clearTimeout(longPress); longPress = null; }
    if (dragging && Math.abs(dx) > 60) startReply(m);
    row.style.transform = '';
    const ind = row.querySelector('.swipe-reply-indicator');
    if (ind) { ind.style.transform = ''; ind.style.opacity = ''; }
    dragging = false;
  };
  row.addEventListener('pointerdown', pdown);
  row.addEventListener('pointermove', pmove);
  row.addEventListener('pointerup',   pup);
  row.addEventListener('pointercancel', pup);
  row.addEventListener('contextmenu', e => showMessageContextMenu(e, m));
  row.addEventListener('dblclick', e => {
    e.preventDefault();
    setReaction(m, '❤️');
  });
}

// Edge-swipe-back (mobile)
let edgeStartX = 0, edgeDragging = false;
document.addEventListener('touchstart', e => {
  if (window.innerWidth > 768) return;
  if (!state.current && !state.page) return;
  const t = e.touches[0];
  if (t.clientX < 16) {
    edgeStartX = t.clientX;
    edgeDragging = true;
  }
}, { passive: true });
document.addEventListener('touchmove', e => {
  if (!edgeDragging) return;
  const t = e.touches[0];
  const dx = t.clientX - edgeStartX;
  if (dx > 60) {
    edgeDragging = false;
    if (state.page) closePage();
    else closeChat();
  }
}, { passive: true });
document.addEventListener('touchend', () => { edgeDragging = false; }, { passive: true });

// Prevent native context menu globally except on selectable surfaces
document.addEventListener('contextmenu', e => {
  let t = e.target;
  while (t && t !== document.body) {
    const tag = t.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || t.isContentEditable) return;
    if (t.classList && (t.classList.contains('bubble-text') || t.classList.contains('bubble-caption') || t.classList.contains('selectable'))) return;
    t = t.parentNode;
  }
  e.preventDefault();
});

// Suppress double-tap zoom on iOS Safari
let _lastTouch = 0;
document.addEventListener('touchend', e => {
  const now = Date.now();
  if (now - _lastTouch < 320) {
    let t = e.target;
    while (t && t !== document.body) {
      if (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable) return;
      t = t.parentNode;
    }
    e.preventDefault();
  }
  _lastTouch = now;
}, { passive: false });

// Suppress pinch zoom via gesture events
document.addEventListener('gesturestart', e => e.preventDefault());
document.addEventListener('gesturechange', e => e.preventDefault());
document.addEventListener('gestureend', e => e.preventDefault());

// ============= 10. Drawers (chat info / user profile) =====================

function openChatDrawer(chat) {
  state.drawerKind = 'chat';
  const d = $('#drawer');
  d.innerHTML = '';
  d.appendChild(buildDrawerHeader('Информация о чате'));
  const body = ce('div', { class: 'drawer-body' });
  const cover = ce('div', { class: 'profile-cover ' + avColor(chat.id) });
  const ident = ce('div', { class: 'ident' });
  const nameRow = ce('div', { class: 'name' }, chat.title || String(chat.id), ...chatTags(chat));
  ident.appendChild(nameRow);
  ident.appendChild(ce('div', { class: 'status' }, chatStatus(chat)));
  cover.appendChild(ce('div', { class: 'gradient' }));
  cover.appendChild(ident);
  body.appendChild(cover);
  if (chat.username) body.appendChild(profileRow(SVG.link, '@' + chat.username, 'Юзернейм'));
  body.appendChild(profileRow(SVG.copy, String(chat.id), 'ID чата'));
  body.appendChild(profileDivider());
  body.appendChild(ce('div', { class: 'profile-section-title' }, 'Действия'));
  body.appendChild(profileRow(SVG.shield, 'Авто-модерация', '', () => openModerationFor(chat.id)));
  if (chat.type !== 'private') {
    body.appendChild(profileRow(SVG.user, 'Администраторы', '', () => openAdminsList(chat.id)));
  }
  body.appendChild(profileRow(SVG.photo, 'Сменить аватар', 'Канал', () => uploadChannelPhoto(chat.id)));
  body.appendChild(profileRow(SVG.ban, 'Бот выходит из чата', '', () => leaveChat(chat.id)));
  d.appendChild(body);
  d.classList.add('open');
}

async function openUserDrawer(userId) {
  state.drawerKind = 'user';
  const d = $('#drawer');
  d.innerHTML = '';
  d.appendChild(buildDrawerHeader('Профиль пользователя'));
  const body = ce('div', { class: 'drawer-body' });
  body.innerHTML = '<div style="padding:24px;text-align:center"><div class="spinner" style="margin:auto"></div></div>';
  d.appendChild(body);
  d.classList.add('open');

  try {
    const u = await api(`/api/users/${userId}/profile`);
    state.users.set(userId, u);
    body.innerHTML = '';
    const cover = ce('div', { class: 'profile-cover ' + avColor(u.id) });
    // Photos carousel
    if (u.photos && u.photos.length) {
      cover.appendChild(ce('img', { class: 'full', src: '/file/' + u.photos[0].file_id + '?token=' + encodeURIComponent(TOKEN) }));
      if (u.photos.length > 1) {
        const segs = ce('div', { class: 'profile-photos-dots' });
        for (let i = 0; i < u.photos.length; i++) segs.appendChild(ce('div', { class: 'seg' + (i === 0 ? ' active' : '') }));
        cover.appendChild(segs);
        // Tap left/right
        cover.addEventListener('click', ev => {
          const rect = cover.getBoundingClientRect();
          const x = (ev.clientX - rect.left) / rect.width;
          const dir = x < 0.5 ? -1 : 1;
          const segArr = $$('.profile-photos-dots .seg', cover);
          let idx = segArr.findIndex(s => s.classList.contains('active'));
          idx = (idx + dir + u.photos.length) % u.photos.length;
          segArr.forEach((s, i) => s.classList.toggle('active', i === idx));
          cover.querySelector('img.full').src = '/file/' + u.photos[idx].file_id + '?token=' + encodeURIComponent(TOKEN);
        });
      }
    }
    cover.appendChild(ce('div', { class: 'gradient' }));
    const ident = ce('div', { class: 'ident' });
    const nameRow = ce('div', { class: 'name' }, (u.first_name || '') + ' ' + (u.last_name || ''));
    if (u.is_premium) nameRow.appendChild(ce('span', { class: 'badge-star' }, '★'));
    if (u.is_bot) nameRow.appendChild(ce('span', { class: 'tag-bot' }, 'BOT'));
    ident.appendChild(nameRow);
    ident.appendChild(ce('div', { class: 'status' }, u.username ? '@' + u.username : 'ID ' + u.id));
    cover.appendChild(ident);
    body.appendChild(cover);
    if (u.username) body.appendChild(profileRow(SVG.link, '@' + u.username, 'Юзернейм'));
    body.appendChild(profileRow(SVG.copy, String(u.id), 'ID пользователя'));
    if (u.is_premium) body.appendChild(profileRow('<svg viewBox="0 0 24 24"><path fill="var(--tg-premium)" d="M12 2l3.09 6.26L22 9.27l-5 4.87L18.18 22 12 18.27 5.82 22 7 14.14l-5-4.87 6.91-1.01z"/></svg>', 'Telegram Premium', 'Премиум-аккаунт'));
    body.appendChild(profileDivider());
    body.appendChild(ce('div', { class: 'profile-section-title' }, 'Действия'));
    body.appendChild(profileRow(SVG.send, 'Написать в личку', '', () => {
      const exists = state.chats.get(userId);
      if (exists) { closeDrawer(); openChat(userId); }
      else toast('Бот не может сам инициировать диалог. Попросите пользователя написать боту первым.');
    }));
  } catch (e) {
    body.innerHTML = '<div style="padding:24px;color:var(--tg-text-3)">Ошибка: ' + esc(e.message) + '</div>';
  }
}

function buildDrawerHeader(title) {
  const h = ce('header', { class: 'drawer-header' });
  h.appendChild(ce('button', { class: 'icon-btn', html: SVG.back, onclick: closeDrawer }));
  h.appendChild(ce('div', { class: 'title' }, title));
  return h;
}

function closeDrawer() {
  $('#drawer').classList.remove('open');
}

function profileRow(iconHtml, primary, secondary, onclick) {
  const row = ce('div', { class: 'profile-row' });
  row.appendChild(ce('div', { html: iconHtml }));
  const txt = ce('div');
  txt.appendChild(ce('div', { class: 'pri selectable' }, primary));
  if (secondary) txt.appendChild(ce('div', { class: 'sec' }, secondary));
  row.appendChild(txt);
  if (onclick) row.onclick = onclick;
  return row;
}

function profileDivider() { return ce('div', { class: 'profile-divider' }); }

// ============= Composer (send / edit / reply) =============================

function renderComposer() {
  const wrap = ce('div', { class: 'composer-wrap' });
  wrap.appendChild(ce('div', { id: 'composerBars' }));
  const comp = ce('div', { class: 'composer' });

  const attachBtn = ce('button', { class: 'icon-btn', html: SVG.attach, onclick: () => openAttachPopover() });
  const inp = ce('div', {
    class: 'composer-input',
    id: 'composerInp',
    contenteditable: 'true',
    'data-placeholder': 'Сообщение',
    onkeydown: e => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        sendComposer();
      }
    },
  });
  // Send chat_action typing as the user types
  let typingT = 0;
  inp.oninput = () => {
    const now = Date.now();
    if (now - typingT > 4500) {
      typingT = now;
      api('/api/bot/settings', { method: 'POST', body: { op: 'send_chat_action', chat_id: state.current, action: 'typing' } }).catch(() => {});
    }
  };
  const sendBtn = ce('button', { class: 'composer-send', html: SVG.send, onclick: sendComposer });
  comp.appendChild(attachBtn);
  comp.appendChild(inp);
  comp.appendChild(sendBtn);
  wrap.appendChild(comp);
  return wrap;
}

function renderComposerBars() {
  const bars = $('#composerBars');
  if (!bars) return;
  bars.innerHTML = '';
  if (state.reply) {
    const r = state.reply;
    const sender = state.users.get(r.sender_id) || {};
    const bar = ce('div', { class: 'composer-reply-bar' });
    bar.appendChild(ce('div', { class: 'stripe' }));
    bar.appendChild(ce('div', { class: 'meta' },
      ce('div', { class: 'title' }, sender.first_name || sender.username || 'Сообщение'),
      ce('div', { class: 'text' }, r.text || r.caption || mediaLabel(r) || '...')));
    bar.appendChild(ce('button', { class: 'icon-btn small', html: SVG.close, onclick: () => { state.reply = null; renderComposerBars(); } }));
    bars.appendChild(bar);
  }
  if (state.edit) {
    const e = state.edit;
    const bar = ce('div', { class: 'composer-reply-bar' });
    bar.appendChild(ce('div', { class: 'stripe' }));
    bar.appendChild(ce('div', { class: 'meta' },
      ce('div', { class: 'title' }, 'Редактирование'),
      ce('div', { class: 'text' }, e.text || e.caption || '...')));
    bar.appendChild(ce('button', { class: 'icon-btn small', html: SVG.close, onclick: () => { state.edit = null; $('#composerInp').textContent = ''; renderComposerBars(); } }));
    bars.appendChild(bar);
  }
}

async function sendComposer() {
  const inp = $('#composerInp');
  const text = (inp.textContent || '').trim();
  if (!text) return;
  if (state.edit) {
    try {
      await api('/api/edit', { method: 'POST', body: { chat_id: state.edit.chat_id, message_id: state.edit.message_id, text } });
      state.edit = null;
      inp.textContent = '';
      renderComposerBars();
    } catch (err) { toast('Ошибка: ' + err.message); }
    return;
  }
  try {
    await api('/api/send', {
      method: 'POST', body: {
        chat_id: state.current, text,
        reply_to_message_id: state.reply?.message_id || null,
      },
    });
    inp.textContent = '';
    state.reply = null;
    renderComposerBars();
    setTimeout(() => { const s = $('#msgsScroll'); if (s) s.scrollTop = s.scrollHeight; }, 50);
  } catch (e) { toast('Ошибка: ' + e.message); }
}

function openAttachPopover() {
  closeAttachPopover();
  const pop = ce('div', { class: 'attach-pop', id: 'attachPop' });
  const item = (icon, label, fn) => ce('button', { onclick: () => { closeAttachPopover(); fn(); } }, ce('span', { html: icon }), label);
  pop.append(
    item(SVG.photo, 'Фото / Видео', () => pickFiles('image/*,video/*', true)),
    item(SVG.doc, 'Документ', () => pickFiles('*/*', false)),
    item(SVG.poll, 'Опрос', () => openPollComposer()),
    item(SVG.sticker, 'Стикер', () => toast('Из бота нельзя отправить произвольный стикер — нужен file_id. (TODO: загрузка из набора)')),
  );
  document.body.appendChild(pop);
  const closeNow = ev2 => { if (!pop.contains(ev2.target)) closeAttachPopover(); };
  setTimeout(() => document.addEventListener('click', closeNow, { capture: true, once: true }), 0);
}
function closeAttachPopover() { $('#attachPop')?.remove(); }

function pickFiles(accept, asMedia) {
  const f = ce('input', { type: 'file', accept, multiple: 'multiple', style: { display: 'none' } });
  f.onchange = async () => {
    const files = Array.from(f.files || []);
    if (!files.length) return;
    const fd = new FormData();
    fd.append('chat_id', state.current);
    if (state.reply?.message_id) fd.append('reply_to_message_id', state.reply.message_id);
    files.forEach((file, i) => fd.append('file' + i, file, file.name));
    try {
      if (files.length === 1) {
        fd.set('file', files[0], files[0].name);
        await api('/api/send_photo', { method: 'POST', body: fd });
      } else {
        await api('/api/send_media_group', { method: 'POST', body: fd });
      }
      state.reply = null; renderComposerBars();
    } catch (e) { toast('Ошибка отправки: ' + e.message); }
  };
  document.body.appendChild(f);
  f.click();
  setTimeout(() => f.remove(), 1000);
}

function openPollComposer() {
  const back = ce('div', { class: 'modal-back' });
  const modal = ce('div', { class: 'modal' });
  modal.appendChild(ce('h3', {}, 'Новый опрос'));
  modal.appendChild(ce('label', {}, 'Вопрос'));
  const q = ce('input', { type: 'text', placeholder: 'Ваш вопрос' });
  modal.appendChild(q);
  modal.appendChild(ce('label', {}, 'Варианты (с новой строки)'));
  const opts = ce('textarea', { placeholder: 'Вариант 1\nВариант 2' });
  modal.appendChild(opts);
  const anon = ce('div', { class: 'modal-row' });
  anon.appendChild(ce('div', { class: 'flex' }, 'Анонимный'));
  const anonSw = ce('div', { class: 'switch on', onclick: () => anonSw.classList.toggle('on') });
  anon.appendChild(anonSw);
  modal.appendChild(anon);
  const multi = ce('div', { class: 'modal-row' });
  multi.appendChild(ce('div', { class: 'flex' }, 'Можно выбрать несколько'));
  const multiSw = ce('div', { class: 'switch', onclick: () => multiSw.classList.toggle('on') });
  multi.appendChild(multiSw);
  modal.appendChild(multi);
  modal.appendChild(ce('div', { class: 'actions' },
    ce('button', { onclick: () => back.remove() }, 'Отмена'),
    ce('button', {
      class: 'primary',
      onclick: async () => {
        const question = q.value.trim();
        const optionList = opts.value.split('\n').map(s => s.trim()).filter(Boolean);
        if (!question || optionList.length < 2) { toast('Минимум 2 варианта'); return; }
        try {
          await api('/api/send_poll', { method: 'POST', body: {
            chat_id: state.current, question, options: optionList,
            is_anonymous: anonSw.classList.contains('on'),
            allows_multiple_answers: multiSw.classList.contains('on'),
          }});
          back.remove();
        } catch (e) { toast('Ошибка: ' + e.message); }
      },
    }, 'Создать'),
  ));
  back.appendChild(modal);
  back.onclick = e => { if (e.target === back) back.remove(); };
  document.body.appendChild(back);
}

// ============= 11. Pages ==================================================

function openPage(kind) {
  closePage();
  state.page = kind;
  let page = $('#page-' + kind);
  if (!page) return;
  page.innerHTML = '';
  page.appendChild(buildPageShell(kind));
  page.classList.add('active');
  if (kind === 'profile') loadProfilePage(page);
  else if (kind === 'moderation') loadModerationPage(page);
  else if (kind === 'ai') loadAIPage(page);
  else if (kind === 'mini') loadMiniPage(page);
  else if (kind === 'settings') loadSettingsPage(page);
  else if (kind === 'audit') loadAuditPage(page);
}

function closePage() {
  if (!state.page) return;
  const page = $('#page-' + state.page);
  if (page) page.classList.remove('active');
  state.page = null;
}

function buildPageShell(kind) {
  const titles = { profile: 'Профиль бота', moderation: 'Авто-модерация', ai: 'AI-ассистент', mini: 'Мини-апы', settings: 'Настройки', audit: 'Журнал событий' };
  const h = ce('header', { class: 'page-header' });
  h.appendChild(ce('button', { class: 'icon-btn', html: SVG.back, onclick: closePage }));
  h.appendChild(ce('div', { class: 'title' }, titles[kind] || kind));
  return h;
}

async function loadProfilePage(page) {
  const body = ce('div', { class: 'page-body', id: 'profileBody' });
  body.innerHTML = '<div style="padding:24px;text-align:center"><div class="spinner" style="margin:auto"></div></div>';
  page.appendChild(body);
  try {
    const me = await api('/api/me');
    state.me = me;
    body.innerHTML = '';
    if (!me.bot_online) {
      body.appendChild(ce('div', { class: 'page-section' },
        ce('div', { style: 'color:var(--tg-text-2)' }, me.error || 'Бот не подключён.'),
        ce('div', { class: 'desc', style: 'margin-top:8px' }, 'Заполните TG_BOT_TOKEN в .env и перезапустите приложение.')));
      return;
    }
    body.appendChild(ce('div', { class: 'page-section' },
      ce('div', { style: 'display:flex; align-items:center; gap:14px; padding:8px 0' },
        ce('div', { class: 'avatar s96 ' + avColor(me.id) }, avInitial(me.first_name)),
        ce('div', {},
          ce('div', { style: 'font-size:18px; font-weight:600' }, me.first_name + ' ' + (me.last_name || ''),
            ce('span', { class: 'tag-bot' }, 'BOT')),
          ce('div', { class: 'desc', style: 'margin-top:2px' }, '@' + me.username + ' · ID ' + me.id),
        ),
      )));

    const profileBlock = ce('div', { class: 'page-section' });
    profileBlock.appendChild(ce('h4', {}, 'Профиль'));
    const nameInp = ce('input', { type: 'text', value: me.first_name || '', placeholder: 'Имя бота' });
    profileBlock.appendChild(ce('div', { class: 'page-row' }, ce('label', {}, 'Имя'), nameInp));
    const descTa = ce('textarea', { placeholder: 'Видно при входе в чат с ботом' });
    profileBlock.appendChild(ce('div', { class: 'page-row' }, ce('label', {}, 'Описание'), descTa));
    const sdescTa = ce('textarea', { placeholder: 'Видно в подсказках' });
    profileBlock.appendChild(ce('div', { class: 'page-row' }, ce('label', {}, 'Краткое описание'), sdescTa));
    const cmdsTa = ce('textarea', { placeholder: 'start - Начать\\nhelp - Справка' });
    profileBlock.appendChild(ce('div', { class: 'page-row' }, ce('label', {}, 'Команды'), cmdsTa));
    profileBlock.appendChild(ce('div', { class: 'btn-row', style: 'margin-top:12px' },
      ce('button', { class: 'btn primary', onclick: async () => {
        const cmds = (cmdsTa.value || '').split('\n').map(l => l.trim()).filter(Boolean).map(l => {
          const i = l.indexOf('-');
          return { command: l.slice(0, i).trim().replace(/^\//, ''), description: l.slice(i + 1).trim() };
        });
        try {
          await api('/api/profile', { method: 'POST', body: {
            name: nameInp.value, description: descTa.value, short_description: sdescTa.value, commands: cmds,
          }});
          toast('Сохранено');
        } catch (e) { toast('Ошибка: ' + e.message); }
      }}, 'Сохранить'),
    ));
    body.appendChild(profileBlock);

    body.appendChild(ce('div', { class: 'page-section' },
      ce('h4', {}, 'Меню-кнопка (Menu Button)'),
      ce('div', { class: 'desc', style: 'margin-bottom:10px' }, 'Кнопка слева от поля ввода в личке с ботом.'),
      ce('div', { class: 'btn-row' },
        ce('button', { class: 'btn', onclick: () => api('/api/bot/settings', { method: 'POST', body: { op: 'set_menu_button', kind: 'commands' }}).then(() => toast('Команды')) }, 'Команды'),
        ce('button', { class: 'btn', onclick: () => api('/api/bot/settings', { method: 'POST', body: { op: 'set_menu_button', kind: 'default' }}).then(() => toast('По умолчанию')) }, 'Скрыть'),
        ce('button', { class: 'btn', onclick: () => {
          const url = prompt('URL веб-приложения (https://...)');
          if (!url) return;
          api('/api/bot/settings', { method: 'POST', body: { op: 'set_menu_button', kind: 'webapp', url, text: 'Открыть' }}).then(() => toast('WebApp кнопка установлена'));
        }}, 'WebApp...'),
      ),
    ));

    body.appendChild(ce('div', { class: 'page-section' },
      ce('h4', {}, 'Права администратора по умолчанию (для добавления в группы)'),
      buildAdminRightsForm(false),
    ));
    body.appendChild(ce('div', { class: 'page-section' },
      ce('h4', {}, 'Права администратора по умолчанию (для каналов)'),
      buildAdminRightsForm(true),
    ));

    body.appendChild(ce('div', { class: 'page-section' },
      ce('h4', {}, 'Что нельзя через Bot API'),
      ce('div', { class: 'desc' },
        '• Аватарка бота — меняется только через @BotFather (BotFather → /mybots → выбрать бота → Edit Bot → Edit Botpic).',
        ce('br'),
        '• Запретить пользователю писать боту нельзя — бот может только перестать отвечать.',
        ce('br'),
        '• Чтобы бот видел ВСЕ сообщения в группе, выключите Privacy Mode: BotFather → /mybots → Bot Settings → Group Privacy → Turn off.',
      ),
    ));
  } catch (e) {
    body.innerHTML = '<div style="padding:24px;color:var(--tg-error)">' + esc(e.message) + '</div>';
  }
}

function buildAdminRightsForm(forChannels) {
  const f = ce('div');
  const checks = forChannels ? [
    ['can_manage_chat', 'Управление чатом'],
    ['can_delete_messages', 'Удалять сообщения'],
    ['can_restrict_members', 'Ограничивать участников'],
    ['can_promote_members', 'Назначать админов'],
    ['can_change_info', 'Менять инфо'],
    ['can_invite_users', 'Добавлять'],
    ['can_post_messages', 'Публиковать'],
    ['can_edit_messages', 'Редактировать'],
  ] : [
    ['can_manage_chat', 'Управление чатом'],
    ['can_delete_messages', 'Удалять сообщения'],
    ['can_restrict_members', 'Ограничивать участников'],
    ['can_promote_members', 'Назначать админов'],
    ['can_change_info', 'Менять инфо'],
    ['can_invite_users', 'Добавлять'],
    ['can_pin_messages', 'Закреплять'],
    ['can_manage_topics', 'Темы'],
  ];
  const switches = {};
  for (const [k, label] of checks) {
    const row = ce('div', { class: 'modal-row' });
    row.appendChild(ce('div', { class: 'flex' }, label));
    const sw = ce('div', { class: 'switch', onclick: () => sw.classList.toggle('on') });
    row.appendChild(sw);
    switches[k] = sw;
    f.appendChild(row);
  }
  f.appendChild(ce('div', { class: 'btn-row', style: 'margin-top:12px' },
    ce('button', { class: 'btn primary', onclick: async () => {
      const body = { op: 'set_default_admin_rights', for_channels: forChannels };
      for (const [k, sw] of Object.entries(switches)) body[k] = sw.classList.contains('on');
      try { await api('/api/bot/settings', { method: 'POST', body }); toast('Сохранено'); }
      catch (e) { toast('Ошибка: ' + e.message); }
    }}, 'Применить'),
  ));
  return f;
}

// Moderation
function openModerationFor(chatId) {
  openPage('moderation');
  setTimeout(() => loadModerationPage($('#page-moderation'), chatId), 30);
}

async function loadModerationPage(page, focusChatId) {
  const body = ce('div', { class: 'page-body' });
  body.appendChild(ce('div', { class: 'page-section' },
    ce('div', { class: 'desc' }, 'Включите модерацию для каждого чата отдельно. Бот должен быть админом с правом удалять/ограничивать.'),
  ));
  const chatsArr = Array.from(state.chats.values()).filter(c => c.type !== 'private');
  for (const c of chatsArr) {
    const sect = ce('div', { class: 'page-section' });
    sect.appendChild(ce('h4', {}, c.title));
    sect.appendChild(ce('div', { id: 'mod-' + c.id }, '...'));
    body.appendChild(sect);
    loadModForChat(c.id);
  }
  if (!chatsArr.length) body.appendChild(ce('div', { class: 'page-section' }, ce('div', { class: 'desc' }, 'Бот ещё не в группах/каналах.')));
  page.appendChild(body);
}

async function loadModForChat(chatId) {
  try {
    const conf = await api('/api/moderation/' + chatId);
    const cont = $('#mod-' + chatId);
    if (!cont) return;
    cont.innerHTML = '';
    const rows = [
      ['enabled', 'Включить модерацию', 'switch'],
      ['antiflood', 'Антифлуд', 'switch'],
      ['antilinks', 'Антиссылки', 'switch'],
      ['anticaps', 'Антикапс', 'switch'],
      ['ai_moderation', 'AI модерация', 'switch'],
    ];
    for (const [k, label, kind] of rows) {
      const r = ce('div', { class: 'modal-row' });
      r.appendChild(ce('div', { class: 'flex' }, label));
      const sw = ce('div', { class: 'switch' + (conf[k] ? ' on' : ''), onclick: () => sw.classList.toggle('on') });
      sw.dataset.key = k;
      r.appendChild(sw);
      cont.appendChild(r);
    }
    const banwordsRow = ce('div');
    banwordsRow.appendChild(ce('label', {}, 'Бан-слова (через запятую)'));
    const bw = ce('input', { type: 'text', value: (JSON.parse(conf.banwords_json || '[]')).join(', ') });
    banwordsRow.appendChild(bw);
    cont.appendChild(banwordsRow);
    const actionRow = ce('div');
    actionRow.appendChild(ce('label', {}, 'Действие при нарушении'));
    const act = ce('select');
    for (const o of ['delete', 'warn', 'mute', 'ban']) {
      const opt = ce('option', { value: o }, o);
      if (conf.action === o) opt.selected = true;
      act.appendChild(opt);
    }
    actionRow.appendChild(act);
    cont.appendChild(actionRow);

    cont.appendChild(ce('div', { class: 'btn-row', style: 'margin-top:8px' },
      ce('button', { class: 'btn primary', onclick: async () => {
        const body = { chat_id: chatId };
        $$('.switch[data-key]', cont).forEach(s => body[s.dataset.key] = s.classList.contains('on') ? 1 : 0);
        body.banwords = bw.value.split(',').map(s => s.trim()).filter(Boolean);
        body.action = act.value;
        try { await api('/api/moderation', { method: 'POST', body }); toast('Сохранено'); }
        catch (e) { toast('Ошибка: ' + e.message); }
      }}, 'Сохранить'),
    ));
  } catch (e) {
    $('#mod-' + chatId).textContent = 'Ошибка: ' + e.message;
  }
}

// AI page
async function loadAIPage(page) {
  const body = ce('div', { class: 'page-body' });
  body.style.display = 'flex';
  body.style.flexDirection = 'column';
  const topBar = ce('div', { style: 'display:flex; gap:8px; margin-bottom:10px' });
  const sel = ce('select', { id: 'aiModelSel', style: 'flex:1; padding:8px; background:var(--tg-bg-2); color:white; border:1px solid var(--tg-divider-2); border-radius:8px' });
  body.appendChild(topBar);
  topBar.appendChild(sel);
  const msgs = ce('div', { class: 'ai-msgs', id: 'aiMsgs' });
  body.appendChild(msgs);
  const composer = ce('div', { style: 'display:flex; gap:6px; padding-top:10px; border-top:1px solid var(--tg-divider)' });
  const inp = ce('textarea', { placeholder: 'Сообщение модели...', style: 'flex:1; background:var(--tg-bg-2); color:white; border:1px solid var(--tg-divider-2); border-radius:10px; padding:10px; resize:none; min-height:40px; max-height:140px' });
  const sendBtn = ce('button', { class: 'btn primary', onclick: async () => {
    const text = inp.value.trim(); if (!text) return;
    inp.value = '';
    msgs.appendChild(ce('div', { class: 'ai-msg-user' }, text));
    const loading = ce('div', { class: 'ai-msg-asst' }, ce('div', { class: 'spinner' }));
    msgs.appendChild(loading);
    try {
      const r = await api('/api/ai/chat', { method: 'POST', body: { model: sel.value, messages: [{ role: 'user', content: text }] } });
      loading.remove();
      msgs.appendChild(ce('div', { class: 'ai-msg-asst', html: linkify(esc(r.content || JSON.stringify(r))) }));
    } catch (e) { loading.remove(); msgs.appendChild(ce('div', { class: 'ai-msg-asst', style: 'color:var(--tg-error)' }, 'Ошибка: ' + e.message)); }
  }}, 'Отправить');
  composer.appendChild(inp); composer.appendChild(sendBtn);
  body.appendChild(composer);
  page.appendChild(body);

  try {
    const me = await api('/api/me');
    const models = me.models || [];
    if (!models.length) sel.innerHTML = '<option>(нет OPENROUTER ключей в .env)</option>';
    for (const m of models) sel.appendChild(ce('option', { value: m.id }, m.id));
  } catch {}
}

// Mini apps page (basic CRUD)
async function loadMiniPage(page) {
  const body = ce('div', { class: 'page-body' });
  body.appendChild(ce('div', { class: 'page-section' },
    ce('h4', {}, 'Ваши мини-апы'),
    ce('div', { id: 'miniList' }, '...'),
    ce('div', { class: 'btn-row', style: 'margin-top:12px' },
      ce('button', { class: 'btn primary', onclick: () => openMiniEditor() }, '＋ Создать'),
    ),
  ));
  page.appendChild(body);
  refreshMiniList();
}
async function refreshMiniList() {
  const list = $('#miniList');
  if (!list) return;
  try {
    const apps = await api('/api/mini_apps');
    list.innerHTML = '';
    if (!apps.length) { list.appendChild(ce('div', { class: 'desc' }, 'Пока пусто.')); return; }
    for (const a of apps) {
      const row = ce('div', { class: 'page-row' });
      row.appendChild(ce('div', {},
        ce('div', { style: 'font-size:14.5px' }, a.name),
        ce('div', { class: 'desc' }, a.description || ''),
      ));
      const actions = ce('div', { class: 'btn-row' });
      actions.appendChild(ce('button', { class: 'btn', onclick: () => window.open('/mini/' + a.id + '?token=' + encodeURIComponent(TOKEN), '_blank') }, 'Открыть'));
      actions.appendChild(ce('button', { class: 'btn', onclick: () => openMiniEditor(a) }, 'Редактор'));
      actions.appendChild(ce('button', { class: 'btn danger', onclick: async () => {
        if (!confirm('Удалить ' + a.name + '?')) return;
        await api('/api/mini_apps/' + a.id, { method: 'DELETE' });
        refreshMiniList();
      }}, 'Удалить'));
      row.appendChild(actions);
      list.appendChild(row);
    }
  } catch (e) { list.textContent = 'Ошибка: ' + e.message; }
}
function openMiniEditor(app) {
  const back = ce('div', { class: 'modal-back' });
  const modal = ce('div', { class: 'modal' });
  modal.appendChild(ce('h3', {}, app ? 'Редактировать мини-ап' : 'Новый мини-ап'));
  modal.appendChild(ce('label', {}, 'Имя'));
  const name = ce('input', { type: 'text', value: app?.name || '' }); modal.appendChild(name);
  modal.appendChild(ce('label', {}, 'Описание'));
  const desc = ce('input', { type: 'text', value: app?.description || '' }); modal.appendChild(desc);
  modal.appendChild(ce('label', {}, 'HTML (полная страница)'));
  const html = ce('textarea', { style: 'min-height:240px; font-family:monospace; font-size:12px' });
  html.value = app?.html || '<!doctype html>\n<html>\n<body style="background:#0e1621;color:white;font-family:system-ui;padding:20px">\n<h1>Привет!</h1>\n<p>Это новый мини-ап.</p>\n</body>\n</html>';
  modal.appendChild(html);
  modal.appendChild(ce('div', { class: 'actions' },
    ce('button', { onclick: () => back.remove() }, 'Отмена'),
    ce('button', { class: 'primary', onclick: async () => {
      try {
        await api('/api/mini_apps', { method: 'POST', body: { id: app?.id, name: name.value, description: desc.value, html: html.value }});
        back.remove(); refreshMiniList();
      } catch (e) { toast('Ошибка: ' + e.message); }
    }}, 'Сохранить'),
  ));
  back.appendChild(modal);
  back.onclick = e => { if (e.target === back) back.remove(); };
  document.body.appendChild(back);
}

async function loadSettingsPage(page) {
  const body = ce('div', { class: 'page-body' });
  body.appendChild(ce('div', { class: 'page-section' },
    ce('h4', {}, 'Веб-приложение'),
    ce('div', { class: 'page-row' },
      ce('div', { class: 'flex' }, ce('label', {}, 'Сменить токен доступа'), ce('div', { class: 'desc' }, 'Выйдет, и придётся войти заново')),
      ce('button', { class: 'btn danger', onclick: () => { localStorage.removeItem(TOKEN_KEY); location.reload(); } }, 'Выйти'),
    ),
  ));
  body.appendChild(ce('div', { class: 'page-section' },
    ce('h4', {}, 'Версия'),
    ce('div', { class: 'desc' }, 'TG Studio. Все настройки бота — на странице «Профиль бота».'),
  ));
  page.appendChild(body);
}

async function loadAuditPage(page) {
  const body = ce('div', { class: 'page-body' });
  body.innerHTML = '<div style="padding:24px;text-align:center"><div class="spinner" style="margin:auto"></div></div>';
  page.appendChild(body);
  try {
    const list = await api('/api/audit');
    body.innerHTML = '';
    const sect = ce('div', { class: 'page-section' });
    for (const r of list) {
      const item = ce('div', { class: 'page-row' });
      item.appendChild(ce('div', {},
        ce('div', { style: 'font-size:13.5px' }, r.kind + (r.message ? ': ' + r.message : '')),
        ce('div', { class: 'desc' }, fmtFullTime(r.ts) + ' · chat=' + (r.chat_id || '-') + ' · user=' + (r.user_id || '-')),
      ));
      sect.appendChild(item);
    }
    body.appendChild(sect);
  } catch (e) { body.innerHTML = 'Ошибка: ' + esc(e.message); }
}

// ============= 12. Image viewer (pinch-zoom) ==============================

function openImageViewer(fileId, kind) {
  const back = ce('div', { class: 'viewer' });
  const isVid = kind === 'video';
  let el;
  if (isVid) {
    el = ce('video', {
      class: 'viewer-img',
      src: '/file/' + fileId + '?token=' + encodeURIComponent(TOKEN),
      controls: 'controls', autoplay: 'autoplay', loop: 'loop', playsinline: 'playsinline',
    });
  } else {
    el = ce('img', { class: 'viewer-img', src: '/file/' + fileId + '?token=' + encodeURIComponent(TOKEN) });
  }
  back.appendChild(el);
  const close = ce('div', { class: 'viewer-close', html: SVG.close, onclick: () => back.remove() });
  back.appendChild(close);
  back.onclick = e => { if (e.target === back) back.remove(); };
  // Pinch zoom
  let scale = 1, lastDist = 0, tx = 0, ty = 0;
  back.addEventListener('touchstart', e => {
    if (e.touches.length === 2) {
      lastDist = Math.hypot(e.touches[0].clientX - e.touches[1].clientX, e.touches[0].clientY - e.touches[1].clientY);
    }
  });
  back.addEventListener('touchmove', e => {
    if (e.touches.length === 2 && lastDist) {
      e.preventDefault();
      const d = Math.hypot(e.touches[0].clientX - e.touches[1].clientX, e.touches[0].clientY - e.touches[1].clientY);
      scale = Math.max(1, Math.min(5, scale * (d / lastDist)));
      lastDist = d;
      el.style.transform = `translate(${tx}px,${ty}px) scale(${scale})`;
    }
  }, { passive: false });
  back.addEventListener('touchend', () => { lastDist = 0; });
  // Mouse wheel
  back.addEventListener('wheel', e => {
    e.preventDefault();
    scale = Math.max(1, Math.min(5, scale + (e.deltaY < 0 ? 0.15 : -0.15)));
    el.style.transform = `translate(${tx}px,${ty}px) scale(${scale})`;
  });
  document.body.appendChild(back);
}

// ============= Poll voters modal ==========================================

let _voterModal = null;
function openPollVoters(pollId) {
  const back = ce('div', { class: 'modal-back' });
  const modal = ce('div', { class: 'modal' });
  modal.appendChild(ce('h3', {}, 'Голосовавшие'));
  const list = ce('div', { class: 'user-list', id: 'voterList' }, ce('div', { class: 'spinner', style: 'margin:24px auto' }));
  modal.appendChild(list);
  modal.appendChild(ce('div', { class: 'actions' }, ce('button', { class: 'primary', onclick: () => { back.remove(); state._pollVoterModal = null; } }, 'Закрыть')));
  back.appendChild(modal);
  back.onclick = e => { if (e.target === back) { back.remove(); state._pollVoterModal = null; } };
  document.body.appendChild(back);
  state._pollVoterModal = { poll_id: pollId, modal: back };
  loadPollVoters(pollId);
}
async function loadPollVoters(pollId) {
  try {
    const voters = await api('/api/poll/' + pollId + '/voters');
    const list = $('#voterList'); if (!list) return;
    list.innerHTML = '';
    if (!voters.length) { list.appendChild(ce('div', { class: 'desc', style: 'padding:14px' }, 'Пока никто не голосовал, либо опрос анонимный и API не отдаёт имена.')); return; }
    for (const v of voters) {
      const it = ce('div', { class: 'item' });
      it.appendChild(ce('div', { class: 'avatar s40 ' + avColor(v.user_id) }, avInitial(v.first_name || v.username || '?')));
      const meta = ce('div', { class: 'flex' });
      meta.appendChild(ce('div', { style: 'font-size:14.5px; display:flex; align-items:center; gap:4px' },
        (v.first_name || '') + ' ' + (v.last_name || ''),
        v.is_premium ? ce('span', { class: 'badge-star' }, '★') : null,
        v.is_bot ? ce('span', { class: 'tag-bot' }, 'BOT') : null,
      ));
      meta.appendChild(ce('div', { class: 'desc' }, (v.username ? '@' + v.username + ' · ' : '') + 'вариант' + (v.option_ids.length > 1 ? 'ы' : '') + ' ' + v.option_ids.map(x => x + 1).join(', ')));
      it.appendChild(meta);
      list.appendChild(it);
    }
  } catch (e) { $('#voterList').innerHTML = 'Ошибка: ' + esc(e.message); }
}

// ============= Channel photo upload =======================================

async function uploadChannelPhoto(chatId) {
  const f = ce('input', { type: 'file', accept: 'image/*', style: { display: 'none' } });
  f.onchange = async () => {
    const file = f.files?.[0]; if (!file) return;
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        await api('/api/bot/settings', { method: 'POST', body: { op: 'set_channel_photo', chat_id: chatId, image_base64: reader.result } });
        toast('Аватарка обновлена');
      } catch (e) { toast('Ошибка: ' + e.message); }
    };
    reader.readAsDataURL(file);
  };
  document.body.appendChild(f);
  f.click();
  setTimeout(() => f.remove(), 1000);
}

async function openAdminsList(chatId) {
  try {
    const admins = await api('/api/chats/' + chatId + '/admins');
    const back = ce('div', { class: 'modal-back' });
    const modal = ce('div', { class: 'modal' });
    modal.appendChild(ce('h3', {}, 'Администраторы'));
    const list = ce('div', { class: 'user-list' });
    for (const a of admins) {
      const it = ce('div', { class: 'item', onclick: () => { back.remove(); openUserDrawer(a.user_id); } });
      it.appendChild(ce('div', { class: 'avatar s40 ' + avColor(a.user_id) }, avInitial(a.first_name || a.username || '?')));
      const meta = ce('div', {});
      meta.appendChild(ce('div', { style: 'font-size:14.5px; display:flex; align-items:center; gap:4px' },
        (a.first_name || '') + ' ' + (a.last_name || ''),
        a.is_premium ? ce('span', { class: 'badge-star' }, '★') : null,
        a.is_bot ? ce('span', { class: 'tag-bot' }, 'BOT') : null,
        a.custom_title ? ce('span', { class: 'tag-verified' }, a.custom_title) : null,
      ));
      meta.appendChild(ce('div', { class: 'desc' }, a.status + (a.username ? ' · @' + a.username : '')));
      it.appendChild(meta);
      list.appendChild(it);
    }
    modal.appendChild(list);
    modal.appendChild(ce('div', { class: 'actions' }, ce('button', { class: 'primary', onclick: () => back.remove() }, 'OK')));
    back.appendChild(modal);
    back.onclick = e => { if (e.target === back) back.remove(); };
    document.body.appendChild(back);
  } catch (e) { toast('Ошибка: ' + e.message); }
}

// ============= 13. Service worker =========================================

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

// ============= Boot =======================================================

async function boot() {
  if (!TOKEN) await showGate();
  else {
    try { await api('/api/me'); }
    catch { localStorage.removeItem(TOKEN_KEY); TOKEN = ''; await showGate(); }
  }
  document.title = 'TG Studio';
  $('#gate').hidden = true;
  $('#app').hidden = false;
  setupSidebar();
  try { state.me = await api('/api/me'); } catch {}
  await loadChats();
  connectWS();
}

document.addEventListener('DOMContentLoaded', boot);
