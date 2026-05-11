"use strict";
/* TG Studio frontend — single-file SPA */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const state = {
  token: "",
  me: null,
  chats: [],
  activeChat: null,
  messages: {}, // chatId -> array
  senders: {}, // chatId -> {userId -> user}
  replyTo: null,
  editTarget: null,
  tab: "chats",
  ws: null,
  wsReady: false,
  models: [],
  aiChats: [],
  aiActive: null,
  miniApps: [],
};

// ---------- helpers ----------
function escapeHTML(s) {
  return (s || "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtTime(t) {
  if (!t) return "";
  const d = new Date(t * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
function fmtDate(t) {
  if (!t) return "";
  const d = new Date(t * 1000);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const diff = (now - d) / 86400000;
  if (diff < 7) return d.toLocaleDateString([], { weekday: "short" });
  return d.toLocaleDateString();
}
function fmtDay(t) {
  const d = new Date(t * 1000);
  return d.toLocaleDateString([], { day: "numeric", month: "long", year: "numeric" });
}
function avatarColor(id) {
  const palette = ["#e17076", "#7bc862", "#65aadd", "#a695e7", "#ee7aae", "#6ec9cb", "#faa774"];
  return palette[Math.abs(id || 0) % palette.length];
}
function avatarHTML(name, id, src) {
  const init = (name || "?").trim().split(/\s+/).slice(0, 2).map(x => x[0] || "").join("").toUpperCase();
  const bg = avatarColor(id);
  if (src) return `<div class="avatar" style="background:${bg}"><img src="${escapeHTML(src)}" loading="lazy" onerror="this.remove()" alt=""></div>`;
  return `<div class="avatar" style="background:${bg}">${escapeHTML(init || "?")}</div>`;
}
function toast(msg, kind) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "show " + (kind || "");
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.className = ""), 2400);
}
async function api(path, opts = {}) {
  opts.headers = Object.assign({ "X-Token": state.token, "Content-Type": "application/json" }, opts.headers || {});
  if (opts.body && typeof opts.body !== "string" && !(opts.body instanceof FormData)) {
    opts.body = JSON.stringify(opts.body);
  }
  if (opts.body instanceof FormData) delete opts.headers["Content-Type"];
  const r = await fetch(path, opts);
  if (r.status === 401) {
    showAuth();
    throw new Error("unauthorized");
  }
  if (!r.ok) {
    let err = r.statusText;
    try { err = (await r.json()).error || err; } catch {}
    throw new Error(err);
  }
  const ct = r.headers.get("content-type") || "";
  if (ct.includes("json")) return r.json();
  return r.text();
}

// ---------- auth ----------
function showAuth() {
  $("#auth").style.display = "flex";
  $("#app").classList.remove("ready");
}
function hideAuth() {
  $("#auth").style.display = "none";
  $("#app").classList.add("ready");
}
async function tryAuth(token) {
  try {
    state.token = token;
    const r = await fetch("/api/auth", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    if (!r.ok) throw new Error("Неверный токен");
    return true;
  } catch (e) {
    toast(e.message, "error");
    return false;
  }
}

// ---------- WebSocket ----------
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws?token=${encodeURIComponent(state.token)}`;
  const ws = new WebSocket(url);
  state.ws = ws;
  ws.onopen = () => {
    state.wsReady = true;
    $("#ws-status").style.color = "var(--ok)";
    $("#ws-status").title = "WebSocket подключён";
  };
  ws.onclose = () => {
    state.wsReady = false;
    $("#ws-status").style.color = "var(--danger)";
    $("#ws-status").title = "WebSocket отключён";
    setTimeout(connectWS, 2500);
  };
  ws.onmessage = ev => {
    try {
      const e = JSON.parse(ev.data);
      handleEvent(e);
    } catch (err) {
      console.error(err);
    }
  };
}

function handleEvent(e) {
  if (e.type === "message" || e.type === "message_edit") {
    const m = e.data;
    if (!state.messages[m.chat_id]) state.messages[m.chat_id] = [];
    const arr = state.messages[m.chat_id];
    const idx = arr.findIndex(x => x.message_id === m.message_id);
    if (idx >= 0) arr[idx] = Object.assign(arr[idx], m);
    else arr.push(m);
    if (state.activeChat === m.chat_id) renderMessages();
    refreshChatList();
  } else if (e.type === "delete") {
    const arr = state.messages[e.chat_id];
    if (arr) {
      const x = arr.find(m => m.message_id === e.message_id);
      if (x) x.deleted = 1;
    }
    if (state.activeChat === e.chat_id) renderMessages();
  } else if (e.type === "reaction") {
    const arr = state.messages[e.chat_id];
    if (arr) {
      const x = arr.find(m => m.message_id === e.message_id);
      if (x) x.reactions = e.reactions;
    }
    if (state.activeChat === e.chat_id) renderMessages();
  } else if (e.type === "moderation") {
    toast(`Модерация: удалено сообщение (${e.reason})`, "ok");
  }
}

// ---------- bootstrap ----------
async function bootstrap() {
  try {
    state.me = await api("/api/me");
    state.models = state.me.models || [];
    $("#me-title").textContent = "@" + (state.me.username || "bot") + " · TG Studio";
  } catch (e) {
    showAuth();
    return;
  }
  hideAuth();
  await loadChats();
  connectWS();
  renderTab();
}

async function loadChats() {
  try {
    state.chats = await api("/api/chats");
    refreshChatList();
  } catch (e) {
    toast(e.message, "error");
  }
}

// ---------- tabs ----------
$$("#tabs .tab").forEach(t => {
  t.addEventListener("click", () => {
    $$("#tabs .tab").forEach(x => x.classList.remove("active"));
    t.classList.add("active");
    state.tab = t.dataset.tab;
    renderTab();
  });
});

function renderTab() {
  const b = $("#body");
  b.innerHTML = "";
  if (state.tab === "chats") renderChats(b);
  else if (state.tab === "profile") renderProfile(b);
  else if (state.tab === "moderation") renderModeration(b);
  else if (state.tab === "ai") renderAI(b);
  else if (state.tab === "apps") renderApps(b);
  else if (state.tab === "audit") renderAudit(b);
  else if (state.tab === "settings") renderSettings(b);
}

// ============== CHATS TAB ==============
function renderChats(root) {
  root.innerHTML = `
    <div class="sidebar" id="sidebar">
      <div class="search"><input id="search" placeholder="🔍 Поиск чатов..."></div>
      <div class="list" id="chat-list"></div>
    </div>
    <div class="main" id="main">
      <div class="empty-state"><div class="e">💬</div><div>Выберите чат, чтобы начать общение.</div>
        <div style="font-size:11px">Только чаты, в которых бот участник или был упомянут — это ограничение Bot API.</div>
      </div>
    </div>
  `;
  refreshChatList();
  $("#search").addEventListener("input", refreshChatList);
  if (state.activeChat) openChat(state.activeChat, true);
}

function refreshChatList() {
  const list = $("#chat-list");
  if (!list) return;
  const q = ($("#search")?.value || "").toLowerCase();
  list.innerHTML = "";
  state.chats
    .filter(c => !q || (c.title || "").toLowerCase().includes(q) || (c.username || "").toLowerCase().includes(q))
    .forEach(c => {
      const lm = c.last_message || {};
      const isOut = lm.sender_id === state.me?.id;
      const preview = (lm.text || lm.caption || (lm.media_type ? `[${lm.media_type}]` : "")).slice(0, 80);
      const type = c.type === "private" ? "private" : (c.type === "channel" ? "channel" : "group");
      const row = document.createElement("div");
      row.className = `chat-row ${type}` + (state.activeChat === c.id ? " active" : "");
      row.innerHTML = `
        ${avatarHTML(c.title, c.id)}
        <div class="meta">
          <div class="name"><b>${escapeHTML(c.title || "")}</b><span class="time">${fmtDate(lm.date)}</span></div>
          <div class="preview">${isOut ? '<span style="color:var(--accent)">Вы: </span>' : ""}${escapeHTML(preview)}</div>
        </div>`;
      row.addEventListener("click", () => openChat(c.id));
      list.appendChild(row);
    });
  if (!state.chats.length) {
    list.innerHTML = `<div class="empty-state"><div>Чаты пока пусты.</div>
      <div style="font-size:11px">Напишите боту в Telegram или добавьте его в группу — они появятся здесь автоматически.</div></div>`;
  }
}

async function openChat(chatId, skipMobileTransition) {
  state.activeChat = chatId;
  state.replyTo = null;
  state.editTarget = null;
  if (!state.messages[chatId]) {
    try {
      const r = await api(`/api/chats/${chatId}/messages`);
      state.messages[chatId] = r.messages || [];
      state.senders[chatId] = r.senders || {};
    } catch (e) {
      toast(e.message, "error");
      return;
    }
  }
  refreshChatList();
  renderMain();
  if (window.innerWidth <= 768 && !skipMobileTransition) {
    $("#sidebar")?.classList.add("hidden");
    $("#main")?.classList.remove("hidden");
  }
}

function renderMain() {
  const main = $("#main");
  if (!main) return;
  const chat = state.chats.find(c => c.id === state.activeChat);
  if (!chat) {
    main.innerHTML = `<div class="empty-state"><div class="e">💬</div><div>Чат не выбран.</div></div>`;
    return;
  }
  main.innerHTML = `
    <div class="chat-head">
      <button class="icon-btn back-btn" title="Назад" style="display:none">←</button>
      ${avatarHTML(chat.title, chat.id)}
      <div class="info">
        <div class="t">${escapeHTML(chat.title || "")}</div>
        <div class="s">${chat.type} · id ${chat.id}</div>
      </div>
      <div class="actions">
        <button class="icon-btn" id="btn-info" title="Информация">ℹ️</button>
      </div>
    </div>
    <div class="messages" id="messages"></div>
    <div class="composer" id="composer">
      <div class="drop-overlay">Бросьте файл сюда</div>
      <div id="reply-bar"></div>
      <div class="row">
        <button class="icon-btn" id="btn-attach" title="Прикрепить">📎</button>
        <button class="icon-btn" id="btn-poll" title="Опрос">📊</button>
        <button class="icon-btn" id="btn-buttons" title="Inline-кнопки">🔘</button>
        <textarea id="msg-input" placeholder="Сообщение..." rows="1"></textarea>
        <button class="send" id="send-btn">➤</button>
      </div>
    </div>
    <input type="file" id="file-picker" accept="image/*,video/*,*/*" multiple style="display:none">
  `;
  renderMessages();
  setupComposer();
  if (window.innerWidth <= 768) {
    const b = $(".back-btn"); b.style.display = "inline-flex";
    b.addEventListener("click", () => {
      $("#sidebar")?.classList.remove("hidden");
      $("#main")?.classList.add("hidden");
    });
  }
}

function entityHTML(text, entities) {
  if (!entities || !entities.length) return escapeHTML(text).replace(/\n/g, "<br>");
  // Sort entities by offset asc
  const ents = entities.slice().sort((a, b) => a.offset - b.offset);
  let out = "";
  let cur = 0;
  const chars = Array.from(text);
  for (const e of ents) {
    if (e.offset > cur) out += escapeHTML(chars.slice(cur, e.offset).join(""));
    const seg = escapeHTML(chars.slice(e.offset, e.offset + e.length).join(""));
    if (e.type === "bold" || e.type === "MessageEntityType.BOLD") out += `<b>${seg}</b>`;
    else if (e.type === "italic" || e.type === "MessageEntityType.ITALIC") out += `<i>${seg}</i>`;
    else if (e.type === "underline" || e.type === "MessageEntityType.UNDERLINE") out += `<u>${seg}</u>`;
    else if (e.type === "strikethrough") out += `<s>${seg}</s>`;
    else if (e.type === "spoiler" || e.type === "MessageEntityType.SPOILER") out += `<span class="spoiler" style="background:rgba(255,255,255,.15);border-radius:4px;cursor:pointer" onclick="this.style.background='transparent'">${seg}</span>`;
    else if (e.type === "code" || e.type === "MessageEntityType.CODE") out += `<code style="background:rgba(0,0,0,.3);padding:0 4px;border-radius:4px;font-family:monospace">${seg}</code>`;
    else if (e.type === "pre" || e.type === "MessageEntityType.PRE") out += `<pre style="background:rgba(0,0,0,.3);padding:8px;border-radius:6px;font-family:monospace;overflow-x:auto;margin:4px 0">${seg}</pre>`;
    else if (e.type === "url" || e.type === "MessageEntityType.URL") out += `<a href="${seg}" target="_blank" rel="noopener" style="color:var(--accent)">${seg}</a>`;
    else if (e.type === "text_link" || e.type === "MessageEntityType.TEXT_LINK") out += `<a href="${escapeHTML(e.url || '#')}" target="_blank" rel="noopener" style="color:var(--accent)">${seg}</a>`;
    else if (e.type === "mention" || e.type === "MessageEntityType.MENTION") out += `<span style="color:var(--accent)">${seg}</span>`;
    else if (e.type === "hashtag" || e.type === "MessageEntityType.HASHTAG") out += `<span style="color:var(--accent)">${seg}</span>`;
    else if (e.type === "custom_emoji" || e.type === "MessageEntityType.CUSTOM_EMOJI") out += `<span class="custom-emoji" data-id="${e.custom_emoji_id}" title="Premium emoji ${e.custom_emoji_id}">${seg}</span>`;
    else out += seg;
    cur = e.offset + e.length;
  }
  if (cur < chars.length) out += escapeHTML(chars.slice(cur).join(""));
  return out.replace(/\n/g, "<br>");
}

function renderMessages() {
  const cont = $("#messages");
  if (!cont) return;
  const msgs = state.messages[state.activeChat] || [];
  const senders = state.senders[state.activeChat] || {};
  cont.innerHTML = "";
  let lastDay = "";
  let lastSender = null;
  for (const m of msgs) {
    if (m.deleted) continue;
    const dayKey = fmtDay(m.date);
    if (dayKey !== lastDay) {
      const d = document.createElement("div");
      d.className = "day";
      d.textContent = dayKey;
      cont.appendChild(d);
      lastDay = dayKey;
      lastSender = null;
    }
    const isOut = m.sender_id === state.me?.id;
    const sender = senders[m.sender_id] || null;
    const row = document.createElement("div");
    row.className = "msg-row " + (isOut ? "out" : "in") + (lastSender === m.sender_id ? " grp" : "");
    row.dataset.id = m.message_id;
    const avSrc = m.sender_id ? `/api/users/${m.sender_id}/photo` : null;
    const name = sender ? ((sender.first_name || "") + " " + (sender.last_name || "")).trim() || sender.username || ("id" + sender.id) : "";
    const replyHTML = m.reply_to_message_id ? renderReplyPreview(m.reply_to_message_id) : "";
    let bodyHTML = "";
    const isStickerOnly = m.media_type === "sticker" && !m.text && !m.caption;
    if (m.media_type === "photo") bodyHTML += `<img class="attach" loading="lazy" src="/file/${m.file_id}" alt="">`;
    else if (m.media_type === "video") bodyHTML += `<video class="attach" src="/file/${m.file_id}" controls></video>`;
    else if (m.media_type === "animation") bodyHTML += `<video class="attach" src="/file/${m.file_id}" autoplay muted loop></video>`;
    else if (m.media_type === "sticker") bodyHTML += `<img class="attach sticker" loading="lazy" src="/file/${m.file_id}" alt="${escapeHTML(m.sticker_emoji || '')}">`;
    else if (m.media_type === "voice" || m.media_type === "audio") bodyHTML += `<audio class="attach" src="/file/${m.file_id}" controls></audio>`;
    else if (m.media_type === "video_note") bodyHTML += `<video class="attach" src="/file/${m.file_id}" controls style="border-radius:50%;max-width:200px"></video>`;
    else if (m.media_type === "document") bodyHTML += `<div class="doc"><div class="ic">📄</div><div><div>${escapeHTML(m.file_id || "")}</div><a href="/file/${m.file_id}" target="_blank" style="color:var(--accent);font-size:12px">Скачать</a></div></div>`;
    if (m.text) bodyHTML += `<div class="text">${entityHTML(m.text, m.entities)}</div>`;
    else if (m.caption) bodyHTML += `<div class="caption">${entityHTML(m.caption, m.entities)}</div>`;
    if (m.poll) bodyHTML += renderPoll(m.poll);
    if (m.reply_markup) bodyHTML += renderInlineKeyboard(m.reply_markup);
    if (m.reactions && m.reactions.length) bodyHTML += renderReactions(m.reactions);

    const showSender = !isOut && (m.sender_id !== lastSender) && (state.chats.find(c=>c.id===state.activeChat)?.type !== "private");
    row.innerHTML = `
      ${!isOut ? avatarHTML(name, m.sender_id, avSrc) : ""}
      <div class="bubble ${isStickerOnly ? 'sticker-only' : ''}" data-id="${m.message_id}">
        ${showSender ? `<div class="sender" style="color:${avatarColor(m.sender_id)}">${escapeHTML(name)}</div>` : ""}
        ${replyHTML}
        ${bodyHTML}
        <div class="ts">${m.edit_date ? '<span class="edited">edited</span>' : ""}${fmtTime(m.date)}</div>
      </div>
      <div class="swipe-icon">↩</div>
    `;
    attachMessageGestures(row, m);
    cont.appendChild(row);
    lastSender = m.sender_id;
  }
  cont.scrollTop = cont.scrollHeight;
}

function renderReplyPreview(replyId) {
  const arr = state.messages[state.activeChat] || [];
  const r = arr.find(x => x.message_id === replyId);
  if (!r) return `<div class="reply">Сообщение #${replyId}</div>`;
  const sender = (state.senders[state.activeChat] || {})[r.sender_id] || {};
  const name = (sender.first_name || sender.username || "сообщение");
  const txt = (r.text || r.caption || `[${r.media_type || 'media'}]`).slice(0, 60);
  return `<div class="reply" data-id="${r.message_id}"><div class="reply-name">${escapeHTML(name)}</div><div>${escapeHTML(txt)}</div></div>`;
}

function renderPoll(p) {
  const opts = (p.options || []).map(o => `<div class="opt"><span>${escapeHTML(o.text)}</span><span>${o.voter_count}</span></div>`).join("");
  return `<div class="poll"><h4>${escapeHTML(p.question)}</h4>${opts}<div style="font-size:11px;color:var(--muted);margin-top:6px">Голосов: ${p.total_voter_count}${p.is_closed ? " · закрыт" : ""}</div></div>`;
}

function renderInlineKeyboard(rm) {
  const rows = (rm.inline_keyboard || []).map(row => {
    const cells = row.map(b => {
      if (b.url) return `<a href="${escapeHTML(b.url)}" target="_blank" rel="noopener" style="flex:1"><button style="width:100%">${escapeHTML(b.text)}</button></a>`;
      return `<button data-cb="${escapeHTML(b.callback_data || '')}">${escapeHTML(b.text)}</button>`;
    }).join("");
    return `<div class="row">${cells}</div>`;
  }).join("");
  return `<div class="ikb">${rows}</div>`;
}

function renderReactions(rs) {
  // Group by emoji
  const counts = {};
  for (const r of rs) {
    const key = r.type === "custom" ? `custom:${r.custom_emoji_id}` : r.emoji;
    counts[key] = (counts[key] || 0) + 1;
  }
  return `<div class="reactions">` + Object.entries(counts).map(([k, c]) =>
    `<span class="reaction" data-emoji="${escapeHTML(k.startsWith('custom:') ? '✨' : k)}">${k.startsWith('custom:') ? '✨' : escapeHTML(k)} ${c}</span>`
  ).join("") + `</div>`;
}

// ---------- gestures ----------
function attachMessageGestures(row, msg) {
  // swipe-to-reply (touch)
  let startX = 0, startY = 0, dx = 0, dragging = false;
  row.addEventListener("touchstart", e => {
    if (e.touches.length !== 1) return;
    startX = e.touches[0].clientX;
    startY = e.touches[0].clientY;
    dragging = true;
  }, { passive: true });
  row.addEventListener("touchmove", e => {
    if (!dragging || e.touches.length !== 1) return;
    dx = e.touches[0].clientX - startX;
    const dy = e.touches[0].clientY - startY;
    if (Math.abs(dy) > Math.abs(dx)) { dragging = false; return; }
    if (dx > 0 && dx < 100) {
      row.style.transform = `translateX(${dx}px)`;
      row.classList.add("swiping");
    }
  }, { passive: true });
  row.addEventListener("touchend", () => {
    if (!dragging) return;
    if (dx > 60) {
      setReply(msg.message_id);
    }
    row.style.transform = "";
    row.classList.remove("swiping");
    dragging = false;
    dx = 0;
  });

  // long-press / right-click → context menu
  let pressTimer = null;
  const showMenu = (x, y) => {
    showContextMenu(x, y, msg);
  };
  row.addEventListener("contextmenu", e => {
    e.preventDefault();
    showMenu(e.clientX, e.clientY);
  });
  row.addEventListener("touchstart", e => {
    pressTimer = setTimeout(() => {
      const t = e.touches[0];
      if (t) showMenu(t.clientX, t.clientY);
    }, 500);
  }, { passive: true });
  row.addEventListener("touchend", () => clearTimeout(pressTimer));
  row.addEventListener("touchmove", () => clearTimeout(pressTimer));

  // double-tap to react ❤️
  let lastTap = 0;
  row.addEventListener("click", e => {
    const now = Date.now();
    if (now - lastTap < 300) {
      reactQuick(msg, "❤");
    }
    lastTap = now;
    // image zoom
    if (e.target.tagName === "IMG" && e.target.classList.contains("attach") && !e.target.classList.contains("sticker")) {
      openViewer(e.target.src);
    }
  });
}

function showContextMenu(x, y, msg) {
  closeContextMenu();
  const isMine = msg.sender_id === state.me?.id;
  const menu = document.createElement("div");
  menu.className = "ctx-menu";
  menu.innerHTML = `
    <div class="react-row">
      ${["❤", "👍", "👎", "🔥", "🥰", "👏", "😁", "🤔", "🎉", "💯"].map(e =>
        `<div class="e" data-react="${e}">${e}</div>`).join("")}
    </div>
    <div class="sep"></div>
    <div class="item" data-act="reply">↩ Ответить</div>
    <div class="item" data-act="copy">📋 Копировать</div>
    ${isMine ? '<div class="item" data-act="edit">✏ Редактировать</div>' : ""}
    ${isMine ? '<div class="item danger" data-act="delete">🗑 Удалить</div>' : ""}
    <div class="item" data-act="id">🔗 Скопировать ID</div>
  `;
  document.body.appendChild(menu);
  // Position to fit viewport
  const w = 200, h = menu.offsetHeight;
  menu.style.left = Math.min(x, window.innerWidth - w - 8) + "px";
  menu.style.top = Math.min(y, window.innerHeight - h - 8) + "px";

  menu.addEventListener("click", async ev => {
    const t = ev.target.closest("[data-act],[data-react]");
    if (!t) return;
    if (t.dataset.react) {
      await reactQuick(msg, t.dataset.react);
    } else if (t.dataset.act === "reply") setReply(msg.message_id);
    else if (t.dataset.act === "copy") {
      navigator.clipboard.writeText(msg.text || msg.caption || "");
      toast("Скопировано", "ok");
    } else if (t.dataset.act === "edit") startEdit(msg);
    else if (t.dataset.act === "delete") deleteMessage(msg);
    else if (t.dataset.act === "id") {
      navigator.clipboard.writeText(String(msg.message_id));
      toast("ID скопирован", "ok");
    }
    closeContextMenu();
  });
  setTimeout(() => document.addEventListener("click", closeContextMenu, { once: true }), 0);
}
function closeContextMenu() {
  $$(".ctx-menu").forEach(m => m.remove());
}

async function reactQuick(msg, emoji) {
  try {
    await api("/api/reaction", { method: "POST", body: { chat_id: msg.chat_id, message_id: msg.message_id, emoji } });
  } catch (e) { toast(e.message, "error"); }
}
async function deleteMessage(msg) {
  if (!confirm("Удалить сообщение?")) return;
  try {
    await api("/api/delete", { method: "POST", body: { chat_id: msg.chat_id, message_id: msg.message_id } });
    msg.deleted = 1;
    renderMessages();
  } catch (e) { toast(e.message, "error"); }
}

function setReply(messageId) {
  state.replyTo = messageId;
  state.editTarget = null;
  drawReplyBar();
  $("#msg-input")?.focus();
}
function startEdit(msg) {
  state.editTarget = msg;
  state.replyTo = null;
  $("#msg-input").value = msg.text || msg.caption || "";
  drawReplyBar();
  $("#msg-input")?.focus();
}
function drawReplyBar() {
  const bar = $("#reply-bar");
  if (!bar) return;
  if (state.editTarget) {
    bar.innerHTML = `<div class="reply-bar"><span>✏</span><div class="name">Редактируем</div><div style="flex:1;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHTML((state.editTarget.text || state.editTarget.caption || "").slice(0,80))}</div><button onclick="cancelReplyEdit()">✕</button></div>`;
  } else if (state.replyTo) {
    const m = (state.messages[state.activeChat] || []).find(x => x.message_id === state.replyTo);
    bar.innerHTML = `<div class="reply-bar"><span>↩</span><div class="name">Ответ</div><div style="flex:1;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHTML((m?.text || m?.caption || "...").slice(0,80))}</div><button onclick="cancelReplyEdit()">✕</button></div>`;
  } else bar.innerHTML = "";
}
window.cancelReplyEdit = function() { state.replyTo = null; state.editTarget = null; $("#msg-input").value = ""; drawReplyBar(); };

// ---------- composer ----------
function setupComposer() {
  const ta = $("#msg-input");
  const send = $("#send-btn");
  ta.addEventListener("input", () => {
    ta.style.height = "auto";
    ta.style.height = Math.min(160, ta.scrollHeight) + "px";
  });
  ta.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      doSend();
    }
  });
  send.addEventListener("click", doSend);

  $("#btn-attach").addEventListener("click", () => $("#file-picker").click());
  $("#file-picker").addEventListener("change", e => {
    for (const f of e.target.files) uploadFile(f);
    e.target.value = "";
  });
  $("#btn-poll").addEventListener("click", openPollModal);
  $("#btn-buttons").addEventListener("click", openButtonsModal);

  // drag-and-drop
  const comp = $("#composer");
  ["dragenter", "dragover"].forEach(ev => comp.addEventListener(ev, e => { e.preventDefault(); comp.classList.add("drag"); }));
  ["dragleave", "drop"].forEach(ev => comp.addEventListener(ev, e => { e.preventDefault(); if (ev === "drop") for (const f of e.dataTransfer.files) uploadFile(f); comp.classList.remove("drag"); }));
}

async function doSend() {
  const ta = $("#msg-input");
  const text = ta.value.trim();
  if (!text || !state.activeChat) return;
  ta.disabled = true;
  try {
    if (state.editTarget) {
      await api("/api/edit", { method: "POST", body: { chat_id: state.activeChat, message_id: state.editTarget.message_id, text } });
    } else {
      const body = { chat_id: state.activeChat, text };
      if (state.replyTo) body.reply_to_message_id = state.replyTo;
      if (window._pendingButtons) {
        body.inline_keyboard = window._pendingButtons;
        window._pendingButtons = null;
      }
      await api("/api/send", { method: "POST", body });
    }
    ta.value = "";
    state.replyTo = null;
    state.editTarget = null;
    drawReplyBar();
  } catch (e) {
    toast(e.message, "error");
  } finally {
    ta.disabled = false;
    ta.style.height = "auto";
    ta.focus();
  }
}

async function uploadFile(f) {
  const fd = new FormData();
  fd.append("chat_id", String(state.activeChat));
  fd.append("file", f, f.name);
  if (state.replyTo) fd.append("reply_to_message_id", String(state.replyTo));
  try {
    await fetch("/api/send_photo", { method: "POST", headers: { "X-Token": state.token }, body: fd });
    toast("Отправлено", "ok");
    state.replyTo = null;
    drawReplyBar();
  } catch (e) { toast(e.message, "error"); }
}

function openPollModal() {
  modal({
    title: "Создать опрос",
    body: `
      <input id="poll-q" placeholder="Вопрос">
      <textarea id="poll-opts" placeholder="Варианты (по одному в строке)"></textarea>
      <label class="row-h"><input type="checkbox" id="poll-anon" checked> Анонимный</label>
      <label class="row-h"><input type="checkbox" id="poll-multi"> Несколько ответов</label>
    `,
    onOk: async () => {
      const q = $("#poll-q").value.trim();
      const opts = $("#poll-opts").value.split("\n").map(s => s.trim()).filter(Boolean);
      if (!q || opts.length < 2) { toast("Минимум 2 варианта", "error"); return false; }
      try {
        await api("/api/send_poll", {
          method: "POST",
          body: { chat_id: state.activeChat, question: q, options: opts, anonymous: $("#poll-anon").checked, multiple: $("#poll-multi").checked }
        });
        return true;
      } catch (e) { toast(e.message, "error"); return false; }
    }
  });
}

function openButtonsModal() {
  const cur = window._pendingButtons || [[{ text: "", url: "" }]];
  const draw = () => {
    return cur.map((row, ri) => row.map((b, ci) =>
      `<div class="row-h" style="gap:4px">
        <input data-r="${ri}" data-c="${ci}" data-f="text" placeholder="Текст" value="${escapeHTML(b.text || '')}">
        <input data-r="${ri}" data-c="${ci}" data-f="url" placeholder="URL (опц.)" value="${escapeHTML(b.url || '')}">
        <input data-r="${ri}" data-c="${ci}" data-f="callback_data" placeholder="callback" value="${escapeHTML(b.callback_data || '')}">
      </div>`).join("")).join("<hr style='border:0;border-top:1px solid var(--line)'>");
  };
  modal({
    title: "Inline-кнопки",
    body: `<div id="btns-area">${draw()}</div>
      <div class="row-h">
        <button onclick="window._addBtnRow()">+ Ряд</button>
        <button onclick="window._addBtnInRow()">+ Кнопка в ряд</button>
        <button class="danger" onclick="window._clearBtns()">Очистить</button>
      </div>
      <div class="hint" style="font-size:11px;color:var(--muted)">Кнопки будут прикреплены к следующему отправленному сообщению.</div>`,
    onOk: () => {
      const buttons = [];
      const rows = $$(".row-h", $("#btns-area")).length ? cur : cur;
      $$('#btns-area input').forEach(inp => {
        const r = +inp.dataset.r, c = +inp.dataset.c, f = inp.dataset.f;
        if (!cur[r]) cur[r] = [];
        if (!cur[r][c]) cur[r][c] = {};
        cur[r][c][f] = inp.value;
      });
      const cleaned = cur.map(r => r.filter(b => b.text && b.text.trim()).map(b => {
        const out = { text: b.text.trim() };
        if (b.url) out.url = b.url;
        if (b.callback_data) out.callback_data = b.callback_data;
        return out;
      })).filter(r => r.length);
      window._pendingButtons = cleaned.length ? cleaned : null;
      if (cleaned.length) toast(`Прикреплено ${cleaned.flat().length} кнопок`, "ok");
      return true;
    }
  });
  window._addBtnRow = () => { cur.push([{ text: "" }]); $("#btns-area").innerHTML = draw(); };
  window._addBtnInRow = () => { cur[cur.length - 1].push({ text: "" }); $("#btns-area").innerHTML = draw(); };
  window._clearBtns = () => { cur.length = 0; cur.push([{ text: "" }]); $("#btns-area").innerHTML = draw(); };
}

// ---------- modal ----------
function modal({ title, body, onOk, okText = "OK", cancelText = "Отмена" }) {
  const back = document.createElement("div");
  back.className = "modal-back";
  back.innerHTML = `<div class="modal">
    <header><h3>${escapeHTML(title)}</h3><button onclick="this.closest('.modal-back').remove()">✕</button></header>
    <div class="content">${body}</div>
    <footer>
      <button onclick="this.closest('.modal-back').remove()">${cancelText}</button>
      <button class="primary" id="modal-ok">${okText}</button>
    </footer>
  </div>`;
  document.body.appendChild(back);
  $("#modal-ok", back).addEventListener("click", async () => {
    const ok = onOk ? await onOk() : true;
    if (ok !== false) back.remove();
  });
  back.addEventListener("click", e => { if (e.target === back) back.remove(); });
}

// ---------- viewer (pinch zoom) ----------
function openViewer(src) {
  const v = document.createElement("div");
  v.className = "viewer";
  v.innerHTML = `<span class="close">✕</span><img src="${escapeHTML(src)}">`;
  document.body.appendChild(v);
  v.querySelector(".close").addEventListener("click", () => v.remove());
  v.addEventListener("click", e => { if (e.target === v) v.remove(); });
  const img = v.querySelector("img");
  let scale = 1, tx = 0, ty = 0;
  let initialDist = 0, initialScale = 1;
  let lastX = 0, lastY = 0, panning = false;
  img.addEventListener("touchstart", e => {
    if (e.touches.length === 2) {
      const dx = e.touches[0].clientX - e.touches[1].clientX;
      const dy = e.touches[0].clientY - e.touches[1].clientY;
      initialDist = Math.hypot(dx, dy);
      initialScale = scale;
    } else if (e.touches.length === 1) {
      panning = true; lastX = e.touches[0].clientX; lastY = e.touches[0].clientY;
    }
  });
  img.addEventListener("touchmove", e => {
    if (e.touches.length === 2) {
      const dx = e.touches[0].clientX - e.touches[1].clientX;
      const dy = e.touches[0].clientY - e.touches[1].clientY;
      const d = Math.hypot(dx, dy);
      scale = Math.max(1, Math.min(5, initialScale * (d / initialDist)));
    } else if (e.touches.length === 1 && panning && scale > 1) {
      const x = e.touches[0].clientX, y = e.touches[0].clientY;
      tx += x - lastX; ty += y - lastY;
      lastX = x; lastY = y;
    }
    img.style.transform = `translate(${tx}px,${ty}px) scale(${scale})`;
  });
  img.addEventListener("touchend", () => { panning = false; if (scale === 1) { tx = ty = 0; img.style.transform = ""; } });
  img.addEventListener("wheel", e => {
    e.preventDefault();
    scale = Math.max(1, Math.min(5, scale + (e.deltaY < 0 ? 0.2 : -0.2)));
    img.style.transform = `translate(${tx}px,${ty}px) scale(${scale})`;
  });
}

// ============== PROFILE TAB ==============
async function renderProfile(root) {
  root.innerHTML = `<div class="page" id="prof"></div>`;
  const p = $("#prof");
  p.innerHTML = `<div class="spinner"></div>`;
  const me = state.me;
  p.innerHTML = `
    <h2>Профиль бота @${escapeHTML(me.username || "")}</h2>
    <div class="section">
      <h3>Имя и описание</h3>
      <div class="field"><label>Отображаемое имя (set_my_name)</label>
        <input id="p-name" value="${escapeHTML(me.first_name || '')}"></div>
      <div class="field"><label>Описание (видно до старта в боте, set_my_description)</label>
        <textarea id="p-desc" rows="3">${escapeHTML(me.description || '')}</textarea></div>
      <div class="field"><label>Краткое описание (в карточке бота, set_my_short_description)</label>
        <textarea id="p-short" rows="2">${escapeHTML(me.short_description || '')}</textarea></div>
      <button class="primary" id="p-save">Сохранить</button>
    </div>

    <div class="section">
      <h3>Команды (/help, /id ...)</h3>
      <div id="cmd-list"></div>
      <button onclick="window._addCmd()">+ Команда</button>
      <button class="primary" onclick="window._saveCmds()">Сохранить команды</button>
    </div>

    <div class="section">
      <h3>Аватарка бота</h3>
      <div style="color:var(--muted);font-size:13px">
        ⚠️ Bot API не позволяет менять аватарку самого бота напрямую — это можно сделать только через
        <a href="https://t.me/BotFather" target="_blank" style="color:var(--accent)">@BotFather</a> → /mybots → выбрать бота → Bot Settings → Edit Bot → Edit Botpic.
        Если бот — админ канала <code>${TG_CHANNEL_ID || '...'}</code>, можно менять аватарку канала:
      </div>
      <input type="file" id="ch-avatar" accept="image/*">
      <button class="primary" id="ch-avatar-go" disabled>Заменить аватарку канала</button>
    </div>

    <div class="section">
      <h3>Возможности</h3>
      <div>can_join_groups: ${me.can_join_groups ? "✅" : "❌"}</div>
      <div>can_read_all_group_messages: ${me.can_read_all_group_messages ? "✅" : "❌ (включите в BotFather → Group Privacy → Disable)"}</div>
      <div>supports_inline_queries: ${me.supports_inline_queries ? "✅" : "❌"}</div>
    </div>
  `;
  $("#p-save").addEventListener("click", async () => {
    try {
      await api("/api/profile", { method: "POST", body: {
        name: $("#p-name").value, description: $("#p-desc").value, short_description: $("#p-short").value
      }});
      toast("Сохранено", "ok");
    } catch (e) { toast(e.message, "error"); }
  });
  const commands = []; // load existing? Not available via API easily; user starts from scratch
  const drawCmds = () => {
    $("#cmd-list").innerHTML = commands.map((c, i) => `<div class="row-h" style="margin-bottom:6px">
      <input style="flex:1" value="${escapeHTML(c.command)}" oninput="window._cmd(${i}, 'command', this.value)">
      <input style="flex:2" value="${escapeHTML(c.description)}" oninput="window._cmd(${i}, 'description', this.value)">
      <button class="danger" onclick="window._delCmd(${i})">✕</button>
    </div>`).join("");
  };
  window._cmd = (i, k, v) => { commands[i][k] = v; };
  window._addCmd = () => { commands.push({ command: "start", description: "Запуск" }); drawCmds(); };
  window._delCmd = (i) => { commands.splice(i, 1); drawCmds(); };
  window._saveCmds = async () => {
    try {
      await api("/api/profile", { method: "POST", body: { commands: commands.map(c => ({ command: c.command.replace(/^\//, ''), description: c.description })) } });
      toast("Команды сохранены", "ok");
    } catch (e) { toast(e.message, "error"); }
  };
  drawCmds();
}

// ============== MODERATION TAB ==============
async function renderModeration(root) {
  root.innerHTML = `
    <div class="sidebar">
      <div class="search"><input id="mod-search" placeholder="🔍 Поиск чатов..."></div>
      <div class="list" id="mod-list"></div>
    </div>
    <div class="main"><div id="mod-page" class="page"></div></div>
  `;
  const list = $("#mod-list");
  const draw = () => {
    const q = ($("#mod-search").value || "").toLowerCase();
    list.innerHTML = "";
    state.chats.filter(c => c.type !== "private")
      .filter(c => !q || (c.title || "").toLowerCase().includes(q))
      .forEach(c => {
        const row = document.createElement("div");
        row.className = "chat-row";
        row.innerHTML = `${avatarHTML(c.title, c.id)}<div class="meta"><div class="name"><b>${escapeHTML(c.title || '')}</b></div><div class="preview">${c.type}</div></div>`;
        row.addEventListener("click", () => loadMod(c.id));
        list.appendChild(row);
      });
  };
  draw();
  $("#mod-search").addEventListener("input", draw);
  $("#mod-page").innerHTML = `<div class="empty-state"><div class="e">🛡️</div><div>Выберите чат для настройки авто-модерации.</div></div>`;
}

async function loadMod(chatId) {
  const p = $("#mod-page");
  p.innerHTML = `<div class="spinner"></div>`;
  try {
    const m = await api(`/api/moderation/${chatId}`);
    const chat = state.chats.find(c => c.id === chatId);
    p.innerHTML = `
      <h2>🛡 ${escapeHTML(chat?.title || chatId)}</h2>
      <div class="section">
        <div class="row-h between">
          <div><b>Включить авто-модерацию</b><div style="font-size:11px;color:var(--muted)">Бот должен быть админом, чтобы удалять/банить</div></div>
          <label class="switch"><input type="checkbox" id="m-enabled" ${m.enabled?"checked":""}><span class="slider"></span></label>
        </div>
        <div class="field"><label>Действие при срабатывании</label>
          <select id="m-action">
            <option value="delete" ${m.action==="delete"?"selected":""}>Удалить сообщение</option>
            <option value="warn" ${m.action==="warn"?"selected":""}>Удалить + предупреждение</option>
            <option value="mute" ${m.action==="mute"?"selected":""}>Удалить + замутить</option>
            <option value="ban" ${m.action==="ban"?"selected":""}>Удалить + бан</option>
          </select></div>
        <div class="row-h"><div class="field" style="flex:1"><label>Мут (минут)</label><input type="number" id="m-mute" value="${m.mute_minutes}"></div>
          <div class="field" style="flex:1"><label>Лимит предупреждений</label><input type="number" id="m-warn" value="${m.warn_threshold}"></div></div>
      </div>

      <div class="section"><h3>Бан-слова</h3>
        <div id="banwords">${(m.banwords || []).map(w => `<span class="chip">${escapeHTML(w)} <span class="x" onclick="this.parentElement.remove()">✕</span></span>`).join(" ")}</div>
        <div class="row-h"><input id="bw-input" placeholder="новое слово или фраза"><button onclick="window._addBw()">+</button></div>
      </div>

      <div class="section">
        <div class="row-h between"><div><b>Анти-флуд</b></div><label class="switch"><input type="checkbox" id="m-flood" ${m.antiflood?"checked":""}><span class="slider"></span></label></div>
        <div class="row-h"><div class="field" style="flex:1"><label>Макс. сообщений</label><input type="number" id="m-flood-c" value="${m.antiflood_count}"></div>
          <div class="field" style="flex:1"><label>За секунд</label><input type="number" id="m-flood-s" value="${m.antiflood_seconds}"></div></div>
      </div>

      <div class="section">
        <div class="row-h between"><div><b>Анти-ссылки</b><div style="font-size:11px;color:var(--muted)">Блок http(s)://, t.me/, @username</div></div><label class="switch"><input type="checkbox" id="m-links" ${m.antilinks?"checked":""}><span class="slider"></span></label></div>
      </div>
      <div class="section">
        <div class="row-h between"><div><b>Анти-капс</b></div><label class="switch"><input type="checkbox" id="m-caps" ${m.anticaps?"checked":""}><span class="slider"></span></label></div>
        <div class="field"><label>Порог % заглавных</label><input type="number" id="m-caps-t" value="${m.caps_threshold}"></div>
      </div>

      <div class="section">
        <div class="row-h between"><div><b>AI-модерация</b><div style="font-size:11px;color:var(--muted)">Подозрительные сообщения проверяются через OpenRouter</div></div><label class="switch"><input type="checkbox" id="m-ai" ${m.ai_moderation?"checked":""}><span class="slider"></span></label></div>
        <div class="field"><label>Модель</label>
          <select id="m-ai-model">
            ${state.models.filter(x => x.kind==="chat").map(x => `<option value="${escapeHTML(x.id)}" ${m.ai_model===x.id?"selected":""}>${escapeHTML(x.id)}</option>`).join("")}
          </select></div>
        <div class="field"><label>Порог токсичности (0..100)</label><input type="number" id="m-ai-t" value="${m.ai_threshold}"></div>
      </div>

      <div class="row-h"><button class="primary" id="m-save">Сохранить</button>
        <span style="color:var(--muted);font-size:12px">Изменения применяются мгновенно к новым сообщениям.</span></div>
    `;
    window._addBw = () => {
      const v = $("#bw-input").value.trim();
      if (!v) return;
      const el = document.createElement("span");
      el.className = "chip";
      el.innerHTML = `${escapeHTML(v)} <span class="x" onclick="this.parentElement.remove()">✕</span>`;
      $("#banwords").appendChild(el);
      $("#banwords").appendChild(document.createTextNode(" "));
      $("#bw-input").value = "";
    };
    $("#m-save").addEventListener("click", async () => {
      const banwords = $$("#banwords .chip").map(c => c.firstChild.textContent.trim());
      try {
        await api("/api/moderation", { method: "POST", body: {
          chat_id: chatId,
          enabled: $("#m-enabled").checked, action: $("#m-action").value,
          mute_minutes: +$("#m-mute").value, warn_threshold: +$("#m-warn").value,
          banwords, antiflood: $("#m-flood").checked,
          antiflood_count: +$("#m-flood-c").value, antiflood_seconds: +$("#m-flood-s").value,
          antilinks: $("#m-links").checked, anticaps: $("#m-caps").checked,
          caps_threshold: +$("#m-caps-t").value,
          ai_moderation: $("#m-ai").checked, ai_threshold: +$("#m-ai-t").value, ai_model: $("#m-ai-model").value
        }});
        toast("Сохранено", "ok");
      } catch (e) { toast(e.message, "error"); }
    });
  } catch (e) {
    p.innerHTML = `<div class="empty-state">${escapeHTML(e.message)}</div>`;
  }
}

// ============== AI TAB ==============
function renderAI(root) {
  root.innerHTML = `<div class="ai-pane">
    <div class="ai-side" id="ai-side">
      <button class="primary" id="ai-new">+ Новый чат</button>
      <div id="ai-list"></div>
    </div>
    <div class="ai-main">
      <div class="ai-messages" id="ai-msgs"></div>
      <div class="ai-compose">
        <select id="ai-model">
          ${state.models.map(m => `<option value="${escapeHTML(m.id)}">[${m.kind}] ${escapeHTML(m.id)}</option>`).join("")}
        </select>
        <label class="row-h" style="white-space:nowrap"><input type="checkbox" id="ai-reasoning"> reasoning</label>
        <textarea id="ai-input" placeholder="Спросите что угодно..."></textarea>
        <button class="primary" id="ai-send">➤</button>
      </div>
    </div>
  </div>`;
  loadAiChats();
  $("#ai-new").addEventListener("click", () => {
    state.aiActive = { id: "tmp" + Date.now(), title: "Новый чат", model: state.models[0]?.id, messages: [] };
    state.aiChats.unshift(state.aiActive);
    drawAiList();
    drawAiMsgs();
  });
  $("#ai-send").addEventListener("click", aiSend);
  $("#ai-input").addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); aiSend(); }});
  $("#ai-model").addEventListener("change", () => { if (state.aiActive) state.aiActive.model = $("#ai-model").value; });
}

function loadAiChats() {
  try {
    const raw = localStorage.getItem("ai_chats");
    state.aiChats = raw ? JSON.parse(raw) : [];
  } catch { state.aiChats = []; }
  drawAiList();
}
function saveAiChats() {
  localStorage.setItem("ai_chats", JSON.stringify(state.aiChats.slice(0, 40)));
}
function drawAiList() {
  const list = $("#ai-list");
  if (!list) return;
  list.innerHTML = state.aiChats.map(c =>
    `<div class="ai-chat ${state.aiActive?.id === c.id ? 'active' : ''}" data-id="${c.id}">
      <span>${escapeHTML(c.title || 'Чат')}</span>
      <span class="danger" data-del="${c.id}">✕</span>
    </div>`).join("");
  list.addEventListener("click", e => {
    const del = e.target.closest("[data-del]");
    if (del) { state.aiChats = state.aiChats.filter(c => c.id !== del.dataset.del); if (state.aiActive?.id === del.dataset.del) state.aiActive = null; saveAiChats(); drawAiList(); drawAiMsgs(); return; }
    const it = e.target.closest("[data-id]");
    if (it) { state.aiActive = state.aiChats.find(c => c.id === it.dataset.id); drawAiList(); drawAiMsgs(); }
  });
}
function drawAiMsgs() {
  const c = $("#ai-msgs"); if (!c) return;
  c.innerHTML = "";
  if (!state.aiActive) { c.innerHTML = `<div class="empty-state"><div class="e">✨</div><div>Создайте новый чат с AI.</div></div>`; return; }
  $("#ai-model").value = state.aiActive.model || state.models[0]?.id;
  for (const m of state.aiActive.messages) {
    const el = document.createElement("div");
    el.className = "ai-msg " + m.role;
    if (typeof m.content === "string") el.innerHTML = escapeHTML(m.content).replace(/\n/g, "<br>");
    else if (Array.isArray(m.content)) {
      el.innerHTML = m.content.map(part => {
        if (part.type === "text") return escapeHTML(part.text).replace(/\n/g, "<br>");
        if (part.type === "image_url") return `<img src="${escapeHTML(part.image_url.url)}">`;
        return "";
      }).join("");
    }
    c.appendChild(el);
  }
  c.scrollTop = c.scrollHeight;
}

async function aiSend() {
  if (!state.aiActive) {
    state.aiActive = { id: "tmp" + Date.now(), title: "Новый чат", model: $("#ai-model").value, messages: [] };
    state.aiChats.unshift(state.aiActive);
  }
  const text = $("#ai-input").value.trim();
  if (!text) return;
  state.aiActive.messages.push({ role: "user", content: text });
  if (state.aiActive.title === "Новый чат") state.aiActive.title = text.slice(0, 30);
  $("#ai-input").value = "";
  drawAiMsgs();
  state.aiActive.messages.push({ role: "assistant", content: "..." });
  drawAiMsgs();
  const model = $("#ai-model").value;
  const isImage = (state.models.find(m => m.id === model) || {}).kind === "image";
  try {
    let resp;
    if (isImage) {
      resp = await api("/api/ai/image", { method: "POST", body: { model, prompt: text } });
    } else {
      resp = await api("/api/ai/chat", { method: "POST", body: { model, messages: state.aiActive.messages.slice(0, -1), reasoning: $("#ai-reasoning").checked } });
    }
    state.aiActive.messages.pop(); // remove placeholder
    if (resp.error) {
      state.aiActive.messages.push({ role: "assistant", content: "Ошибка: " + (resp.error.message || JSON.stringify(resp.error)) });
    } else if (resp.choices && resp.choices[0]) {
      const m = resp.choices[0].message || {};
      if (m.images && m.images.length) {
        state.aiActive.messages.push({ role: "assistant", content: [{ type: "image_url", image_url: { url: m.images[0].image_url.url } }, { type: "text", text: m.content || "" }] });
      } else {
        state.aiActive.messages.push({ role: "assistant", content: m.content || "(пусто)" });
      }
    } else {
      state.aiActive.messages.push({ role: "assistant", content: JSON.stringify(resp).slice(0, 400) });
    }
  } catch (e) {
    state.aiActive.messages.pop();
    state.aiActive.messages.push({ role: "assistant", content: "Ошибка: " + e.message });
  }
  saveAiChats();
  drawAiMsgs();
}

// ============== MINI APPS TAB ==============
async function renderApps(root) {
  root.innerHTML = `<div class="page">
    <h2>🧩 Мини-апы <button class="primary" style="margin-left:8px" id="new-app">+ Создать</button></h2>
    <div class="apps-grid" id="apps-grid"></div>
  </div>`;
  $("#new-app").addEventListener("click", editApp);
  try {
    state.miniApps = await api("/api/mini_apps");
  } catch { state.miniApps = []; }
  drawApps();
}

function drawApps() {
  const g = $("#apps-grid");
  if (!g) return;
  g.innerHTML = state.miniApps.map(a => `
    <div class="app-tile" data-id="${a.id}">
      <div class="icon">${escapeHTML(a.icon || "🧩")}</div>
      <div class="name">${escapeHTML(a.name || "—")}</div>
      <div class="desc">${escapeHTML(a.description || "")}</div>
    </div>`).join("");
  const starterApps = [
    { id: "starter-calc", name: "Калькулятор", icon: "🧮", desc: "Базовый" },
    { id: "starter-notes", name: "Заметки", icon: "📝", desc: "Локальные" },
    { id: "starter-canvas", name: "Канвас", icon: "🎨", desc: "Рисование" },
  ];
  for (const sa of starterApps) {
    if (state.miniApps.find(x => x.id === sa.id)) continue;
    const div = document.createElement("div");
    div.className = "app-tile";
    div.innerHTML = `<div class="icon">${sa.icon}</div><div class="name">${sa.name}</div><div class="desc">${sa.desc} (шаблон)</div>`;
    div.addEventListener("click", () => editApp({ id: sa.id, name: sa.name, icon: sa.icon, description: sa.desc, html: STARTERS[sa.id] }));
    g.appendChild(div);
  }
  g.addEventListener("click", e => {
    const t = e.target.closest("[data-id]"); if (!t) return;
    const app = state.miniApps.find(a => a.id === t.dataset.id);
    if (!app) return;
    if (e.shiftKey || e.altKey) return editApp(app);
    openMiniApp(app.id, app.name);
  });
}

function openMiniApp(id, name) {
  const f = document.createElement("div");
  f.className = "app-frame";
  f.innerHTML = `<div class="app-head"><button onclick="this.closest('.app-frame').remove()">←</button><b>${escapeHTML(name)}</b><span style="flex:1"></span><button onclick="window.editAppById('${id}')">✏</button></div>
    <iframe sandbox="allow-scripts allow-forms allow-modals" src="/mini/${id}"></iframe>`;
  document.body.appendChild(f);
}
window.editAppById = id => editApp(state.miniApps.find(a => a.id === id));

function editApp(app) {
  app = app || { id: "", name: "Новое приложение", icon: "🧩", description: "", html: "<h1>Привет!</h1>" };
  modal({
    title: "Редактор мини-апа",
    body: `
      <div class="field"><label>Название</label><input id="ma-name" value="${escapeHTML(app.name || '')}"></div>
      <div class="row-h"><div class="field" style="flex:1"><label>Иконка (emoji)</label><input id="ma-icon" value="${escapeHTML(app.icon || '🧩')}"></div>
        <div class="field" style="flex:3"><label>Описание</label><input id="ma-desc" value="${escapeHTML(app.description || '')}"></div></div>
      <div class="field"><label>HTML (полный документ или фрагмент)</label>
        <textarea id="ma-html" rows="14" style="font-family:monospace;font-size:12px">${escapeHTML(app.html || '')}</textarea></div>
      <button class="danger" onclick="window._delMa('${app.id}')" ${app.id ? '' : 'style="display:none"'}>🗑 Удалить</button>
    `,
    onOk: async () => {
      try {
        await api("/api/mini_apps", { method: "POST", body: {
          id: app.id || undefined,
          name: $("#ma-name").value, icon: $("#ma-icon").value,
          description: $("#ma-desc").value, html: $("#ma-html").value
        }});
        state.miniApps = await api("/api/mini_apps");
        drawApps();
        return true;
      } catch (e) { toast(e.message, "error"); return false; }
    }
  });
  window._delMa = async id => {
    if (!id) return;
    if (!confirm("Удалить мини-ап?")) return;
    await api("/api/mini_apps/" + id, { method: "DELETE" });
    state.miniApps = await api("/api/mini_apps");
    drawApps();
    $$(".modal-back").forEach(m => m.remove());
  };
}

const STARTERS = {
  "starter-calc": `<!doctype html><html><body style="background:#222;color:#fff;font-family:sans-serif;padding:20px"><h2>Калькулятор</h2><input id=x style="padding:8px;font-size:18px;width:60%"><button onclick="document.getElementById('y').textContent=eval(document.getElementById('x').value)">=</button><div id=y style="font-size:32px;margin-top:14px">0</div></body></html>`,
  "starter-notes": `<!doctype html><html><body style="background:#222;color:#fff;font-family:sans-serif;padding:20px"><h2>Заметки</h2><textarea id=n style="width:100%;height:60vh;background:#333;color:#fff;border:0;padding:8px;font-family:inherit"></textarea><script>const n=document.getElementById('n');n.value=localStorage.getItem('notes')||'';n.oninput=()=>localStorage.setItem('notes',n.value);<\\/script></body></html>`,
  "starter-canvas": `<!doctype html><html><body style="margin:0"><canvas id=c style="display:block;background:#fff;width:100vw;height:100vh"></canvas><script>const c=document.getElementById('c');c.width=innerWidth;c.height=innerHeight;const x=c.getContext('2d');let d=false;c.onpointerdown=e=>{d=true;x.beginPath();x.moveTo(e.offsetX,e.offsetY)};c.onpointermove=e=>{if(d){x.lineTo(e.offsetX,e.offsetY);x.stroke()}};c.onpointerup=()=>d=false;<\\/script></body></html>`
};

// ============== AUDIT TAB ==============
async function renderAudit(root) {
  root.innerHTML = `<div class="page"><h2>📜 Лог событий</h2><div id="audit-list" style="display:flex;flex-direction:column;gap:6px"></div></div>`;
  try {
    const rows = await api("/api/audit");
    $("#audit-list").innerHTML = rows.map(r =>
      `<div class="list-row">
        <div><b>${escapeHTML(r.kind)}</b> ${r.chat_id ? `chat ${r.chat_id}` : ""} ${r.user_id ? `user ${r.user_id}` : ""}
          <div style="font-size:11px;color:var(--muted)">${escapeHTML(r.message || '')}</div></div>
        <div style="color:var(--muted);font-size:11px">${new Date(r.ts*1000).toLocaleString()}</div>
      </div>`).join("");
  } catch (e) { $("#audit-list").innerHTML = `<div>Ошибка: ${escapeHTML(e.message)}</div>`; }
}

// ============== SETTINGS TAB ==============
function renderSettings(root) {
  const me = state.me;
  root.innerHTML = `<div class="page">
    <h2>⚙️ Настройки</h2>
    <div class="section">
      <h3>Сводка</h3>
      <div>Бот: <b>@${escapeHTML(me?.username || '')}</b> (id ${me?.id})</div>
      <div>Владелец: @${escapeHTML(me?.owner_username || '')} (id ${me?.owner_id})</div>
      <div>Канал: ${me?.channel_id}</div>
      <div>Известных моделей: ${state.models.length}</div>
      <div>Открытых WebSocket: <span id="ws-info"></span></div>
    </div>
    <div class="section">
      <h3>PWA / Установка</h3>
      <div>Можно поставить TG Studio как приложение: меню браузера → «Установить приложение».</div>
      <button class="primary" id="pwa-prompt">Попробовать установить</button>
    </div>
    <div class="section">
      <h3>Сессия</h3>
      <button class="danger" id="logout">Выйти</button>
    </div>
    <div class="section" style="color:var(--muted);font-size:12px">
      <h3>Ограничения Telegram Bot API</h3>
      <ul>
        <li>Бот видит только сообщения, адресованные ему, или (в группах) — все, если Group Privacy отключён в @BotFather.</li>
        <li>Нельзя загрузить историю чата до момента, когда бот её получил через webhook/polling.</li>
        <li>Нельзя видеть список «всех личных чатов» — только тех, кто написал боту.</li>
        <li>Менять аватарку бота можно только через @BotFather.</li>
      </ul>
    </div>
  </div>`;
  $("#logout").addEventListener("click", () => { document.cookie = "token=; path=/; max-age=0"; location.reload(); });
  $("#pwa-prompt").addEventListener("click", () => {
    if (window._deferredPrompt) window._deferredPrompt.prompt();
    else toast("Браузер не предлагает установку (или уже установлено)", "ok");
  });
}

// ---------- main ----------
window.addEventListener("beforeinstallprompt", e => { e.preventDefault(); window._deferredPrompt = e; });

document.addEventListener("DOMContentLoaded", async () => {
  // Try token from URL or cookie
  const params = new URLSearchParams(location.search);
  const urlToken = params.get("token");
  const cookieToken = (document.cookie.split("; ").find(x => x.startsWith("token=")) || "").slice(6);
  let token = urlToken || cookieToken;
  if (token) {
    if (await tryAuth(token)) {
      state.token = token;
      bootstrap();
      return;
    }
  }
  showAuth();
});

$("#auth-go").addEventListener("click", async () => {
  const t = $("#auth-token").value.trim();
  if (await tryAuth(t)) { state.token = t; bootstrap(); }
});
$("#auth-token").addEventListener("keydown", e => { if (e.key === "Enter") $("#auth-go").click(); });

// PWA service worker
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
