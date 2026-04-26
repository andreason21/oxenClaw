// sampyClaw dashboard SPA — vanilla JS, no build step.
// Deliberately small modules in one file: RPC client, router, views, toasts,
// shortcuts, lightweight markdown.

"use strict";

// ─────────────────────────────────────────────────────────────────────────
// RPC client
// ─────────────────────────────────────────────────────────────────────────
const Rpc = (() => {
  let ws = null;
  let nextId = 1;
  const pending = new Map();
  const eventHandlers = new Set();
  const log = [];
  const logCap = 100;

  let url = "";
  let onStateChange = () => {};

  // Token resolution: precedence is (1) ?token=... in the current URL,
  // (2) sampyclaw_token cookie, (3) localStorage["sampyclaw_token"]
  // (set by the in-app login gate), (4) none. The chosen token is
  // forwarded to the WS connect as a query string because browsers
  // can't set Authorization headers on a WS upgrade.
  const TOKEN_KEY = "sampyclaw_token";

  function readToken() {
    const params = new URLSearchParams(location.search);
    const fromQuery = params.get("token");
    if (fromQuery) return fromQuery;
    const m = document.cookie.match(/(?:^|;\s*)sampyclaw_token=([^;]+)/);
    if (m) return decodeURIComponent(m[1]);
    try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
  }

  function storeToken(token, { remember }) {
    if (!token) return;
    if (remember) {
      // 12h cookie. The gateway's own Set-Cookie uses Max-Age=43200; we
      // mirror that so the client TTL doesn't outlive a server-side
      // rotation by accident.
      const ttl = 12 * 3600;
      document.cookie =
        `${TOKEN_KEY}=${encodeURIComponent(token)}; Max-Age=${ttl}; Path=/; SameSite=Strict`;
      try { localStorage.setItem(TOKEN_KEY, token); } catch {}
    }
  }

  function clearStoredToken() {
    document.cookie = `${TOKEN_KEY}=; Max-Age=0; Path=/; SameSite=Strict`;
    try { localStorage.removeItem(TOKEN_KEY); } catch {}
  }

  // Strip the token from the address bar after the page loads so it
  // doesn't sit in browser history / get pasted accidentally.
  function scrubTokenFromUrl() {
    const params = new URLSearchParams(location.search);
    if (params.has("token")) {
      params.delete("token");
      const qs = params.toString();
      const next = location.pathname + (qs ? `?${qs}` : "") + location.hash;
      history.replaceState(null, "", next);
    }
  }

  function defaultUrl() {
    if (location.protocol === "file:") return "ws://127.0.0.1:7331";
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const token = readToken();
    const suffix = token
      ? `/?token=${encodeURIComponent(token)}`
      : "/";
    return `${proto}//${location.host}${suffix}`;
  }

  function urlWithToken(baseUrl, token) {
    if (!token) return baseUrl;
    try {
      const u = new URL(baseUrl);
      u.searchParams.set("token", token);
      return u.toString();
    } catch {
      // Fallback for relative or malformed URLs.
      return `${baseUrl}${baseUrl.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}`;
    }
  }

  function pushLog(direction, payload, error) {
    log.unshift({ ts: new Date(), direction, payload, error });
    if (log.length > logCap) log.length = logCap;
  }

  // Track whether the most recent WS connection actually opened. When
  // `onclose` fires without a preceding `onopen`, the upgrade was
  // rejected (auth failure / connection refused / wrong host) — the UI
  // uses this to surface the login gate instead of silently sitting in
  // the "down" state.
  let lastOpened = false;
  let onAuthFailure = () => {};

  function connect(target) {
    url = target || defaultUrl();
    if (ws) try { ws.close(); } catch {}
    onStateChange("connecting", url);
    lastOpened = false;
    ws = new WebSocket(url);
    ws.onopen = () => { lastOpened = true; onStateChange("up", url); };
    ws.onclose = (ev) => {
      onStateChange("down", url);
      if (!lastOpened) {
        // Upgrade rejected before any frame — most likely auth.
        try { onAuthFailure({ code: ev.code, reason: ev.reason || "" }); } catch {}
      }
    };
    ws.onerror = () => onStateChange("down", url);
    ws.onmessage = (ev) => {
      let frame;
      try { frame = JSON.parse(ev.data); }
      catch { pushLog("in", ev.data, "non-JSON"); return; }
      pushLog("in", frame);
      if (frame.type === "event") {
        for (const h of eventHandlers) { try { h(frame.body); } catch (e) { console.error(e); } }
        return;
      }
      const slot = pending.get(frame.id);
      if (!slot) return;
      pending.delete(frame.id);
      if (frame.error) slot.reject(new RpcError(frame.error));
      else slot.resolve(frame.result);
    };
  }

  function call(method, params = {}) {
    return new Promise((resolve, reject) => {
      if (!ws || ws.readyState !== 1) {
        reject(new RpcError({ code: -1, message: "not connected" }));
        return;
      }
      const id = nextId++;
      const frame = { jsonrpc: "2.0", id, method, params };
      pending.set(id, { resolve, reject });
      ws.send(JSON.stringify(frame));
      pushLog("out", frame);
    });
  }

  class RpcError extends Error {
    constructor(err) {
      super(err.message || "rpc error");
      this.code = err.code;
      this.data = err.data;
    }
  }

  function onEvent(h) { eventHandlers.add(h); return () => eventHandlers.delete(h); }
  function setStateListener(fn) { onStateChange = fn; }
  function setAuthFailureListener(fn) { onAuthFailure = fn || (() => {}); }

  return {
    connect,
    call,
    onEvent,
    setStateListener,
    setAuthFailureListener,
    defaultUrl,
    urlWithToken,
    scrubTokenFromUrl,
    readToken,
    storeToken,
    clearStoredToken,
    get url() { return url; },
    log,
    RpcError,
  };
})();

// ─────────────────────────────────────────────────────────────────────────
// Toast system
// ─────────────────────────────────────────────────────────────────────────
const Toast = (() => {
  const container = () => document.getElementById("toasts");
  function show(kind, title, msg, ttl = 4000) {
    const c = container();
    if (!c) return;
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    const t = document.createElement("div"); t.className = "title"; t.textContent = title;
    el.appendChild(t);
    if (msg) {
      const m = document.createElement("div"); m.className = "msg"; m.textContent = msg;
      el.appendChild(m);
    }
    c.appendChild(el);
    setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 200); }, ttl);
  }
  return {
    info: (t, m) => show("info", t, m),
    success: (t, m) => show("success", t, m),
    warn: (t, m) => show("warn", t, m),
    error: (t, m) => show("error", t, m, 6000),
  };
})();

// ─────────────────────────────────────────────────────────────────────────
// Lightweight markdown — handles paragraphs, fenced code, inline code,
// bold/italic, links, lists. NOT a complete impl; safe for chat content.
// ─────────────────────────────────────────────────────────────────────────
const Markdown = (() => {
  function escape(s) {
    return s
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }
  function inline(s) {
    s = escape(s);
    s = s.replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`);
    s = s.replace(/\*\*([^*]+)\*\*/g, (_, c) => `<strong>${c}</strong>`);
    s = s.replace(/(^|[\s_])\*([^*]+)\*(?=[\s.,!?]|$)/g, (_, pre, c) => `${pre}<em>${c}</em>`);
    s = s.replace(/(^|[\s_])_([^_]+)_(?=[\s.,!?]|$)/g, (_, pre, c) => `${pre}<em>${c}</em>`);
    s = s.replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, (_, t, u) => `<a href="${u}" target="_blank" rel="noopener">${t}</a>`);
    return s;
  }
  function render(text) {
    if (!text) return "";
    const lines = text.split(/\r?\n/);
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (line.startsWith("```")) {
        const lang = line.slice(3).trim();
        const buf = [];
        i++;
        while (i < lines.length && !lines[i].startsWith("```")) buf.push(lines[i++]);
        i++; // closing ```
        out.push(`<pre><code data-lang="${escape(lang)}">${escape(buf.join("\n"))}</code></pre>`);
        continue;
      }
      // Unordered list: require at least 2 sibling items so a stray
      // "- foo" line doesn't get rendered as a 1-item bullet.
      if (
        /^[-*]\s+/.test(line)
        && i + 1 < lines.length
        && /^[-*]\s+/.test(lines[i + 1])
      ) {
        const items = [];
        while (i < lines.length && /^[-*]\s+/.test(lines[i])) {
          items.push(`<li>${inline(lines[i].replace(/^[-*]\s+/, ""))}</li>`);
          i++;
        }
        out.push(`<ul>${items.join("")}</ul>`);
        continue;
      }
      // Ordered list: same — require at least 2 items in a row. This
      // avoids the common LLM/user case where a single line "1. xxx"
      // turns into <ol><li>xxx</li></ol> and the browser auto-prepends
      // a "1." marker, making the message look duplicated.
      if (
        /^\d+\.\s+/.test(line)
        && i + 1 < lines.length
        && /^\d+\.\s+/.test(lines[i + 1])
      ) {
        const items = [];
        while (i < lines.length && /^\d+\.\s+/.test(lines[i])) {
          items.push(`<li>${inline(lines[i].replace(/^\d+\.\s+/, ""))}</li>`);
          i++;
        }
        out.push(`<ol>${items.join("")}</ol>`);
        continue;
      }
      // Paragraph block: collect until blank line, code fence, or the
      // start of a real (multi-item) list. Single-line "1. x" or "- y"
      // stays in the paragraph and renders verbatim.
      const paraLines = [];
      const isMultiList = (idx) =>
        idx + 1 < lines.length
        && (
          (/^[-*]\s+/.test(lines[idx]) && /^[-*]\s+/.test(lines[idx + 1]))
          || (/^\d+\.\s+/.test(lines[idx]) && /^\d+\.\s+/.test(lines[idx + 1]))
        );
      while (
        i < lines.length
        && lines[i].trim() !== ""
        && !lines[i].startsWith("```")
        && !isMultiList(i)
      ) {
        paraLines.push(lines[i++]);
      }
      if (paraLines.length) out.push(`<p>${inline(paraLines.join("\n"))}</p>`);
      else i++;
    }
    return out.join("");
  }
  return { render };
})();

// ─────────────────────────────────────────────────────────────────────────
// DOM helpers
// ─────────────────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, ...children) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "dataset") Object.assign(e.dataset, v);
    else if (v === false || v == null) {} else if (v === true) e.setAttribute(k, "");
    else e.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    e.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return e;
};
const fmtTime = (ts) => {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const now = new Date();
  const diff = (now - d) / 1000;
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff/60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff/3600)}h ago`;
  return d.toLocaleString();
};
const fmtFuture = (ts) => {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const diff = (d - new Date()) / 1000;
  if (diff < 60) return `in ${Math.max(0, Math.floor(diff))}s`;
  if (diff < 3600) return `in ${Math.floor(diff/60)}m`;
  if (diff < 86400) return `in ${Math.floor(diff/3600)}h`;
  return d.toLocaleString();
};

async function safeRpc(method, params, { quiet = false } = {}) {
  try { return await Rpc.call(method, params); }
  catch (e) {
    if (!quiet) Toast.error(`${method} failed`, e.message);
    throw e;
  }
}

// Friendly empty-state with icon + title + body + optional copyable example
// + optional action buttons. `example` is rendered in a monospace box —
// good for sample chat prompts the user can copy.
function emptyState({ icon = "✨", title, body, example, actions }) {
  const root = el("div", { class: "empty-state" });
  if (icon) root.append(el("div", { class: "empty-state__icon" }, icon));
  if (title) root.append(el("div", { class: "empty-state__title" }, title));
  if (body) root.append(el("div", { class: "empty-state__body", html: body }));
  if (example) root.append(el("div", { class: "empty-state__example" }, example));
  if (actions && actions.length) {
    const row = el("div", { class: "empty-state__actions" });
    for (const a of actions) row.append(a);
    root.append(row);
  }
  return root;
}

// ─────────────────────────────────────────────────────────────────────────
// Router
// ─────────────────────────────────────────────────────────────────────────
const Router = (() => {
  const routes = {};
  let active = null;
  let activeCleanup = null;

  function register(name, view) { routes[name] = view; }
  function go(name) { if (location.hash !== `#${name}`) location.hash = `#${name}`; else handleHash(); }
  async function handleHash() {
    const name = (location.hash || "#chat").slice(1).split("?")[0] || "chat";
    const view = routes[name] || routes.chat;
    if (active === name && activeCleanup) return;
    if (activeCleanup) try { activeCleanup(); } catch {}
    active = name;
    document.querySelectorAll(".nav-item").forEach((n) => {
      n.classList.toggle("active", n.dataset.route === name);
    });
    $("view-title").textContent = view.title;
    $("topbar-actions").innerHTML = "";
    const root = $("view");
    root.innerHTML = "";
    activeCleanup = await view.render(root, $("topbar-actions")) || null;
  }
  window.addEventListener("hashchange", handleHash);
  return { register, go, handleHash, get active() { return active; } };
})();

// ─────────────────────────────────────────────────────────────────────────
// Chat view
// ─────────────────────────────────────────────────────────────────────────
const ChatState = {
  agentId: localStorage.getItem("samp.agentId") || "assistant",
  channel: localStorage.getItem("samp.channel") || "telegram",
  accountId: localStorage.getItem("samp.accountId") || "main",
  chatId: localStorage.getItem("samp.chatId") || "demo",
  threadId: localStorage.getItem("samp.threadId") || "",
  sessionKey() {
    const parts = [this.channel, this.accountId, this.chatId];
    if (this.threadId) parts.push(this.threadId);
    return parts.join(":");
  },
  save() {
    localStorage.setItem("samp.agentId", this.agentId);
    localStorage.setItem("samp.channel", this.channel);
    localStorage.setItem("samp.accountId", this.accountId);
    localStorage.setItem("samp.chatId", this.chatId);
    localStorage.setItem("samp.threadId", this.threadId);
  },
};

const ChatView = {
  title: "Chat",
  async render(root, actions) {
    const layout = el("div", { class: "chat-layout" });
    const left = el("aside", { class: "session-panel card" });
    const right = el("div", { class: "chat-pane" });
    layout.append(left, right);
    root.append(layout);

    const targetCard = el("div", { class: "chat-target" });
    const stream = el("div", { class: "chat-stream" });
    const compose = el("div", { class: "chat-compose" });
    right.append(targetCard, stream, compose);

    const fields = ["agentId", "channel", "accountId", "chatId", "threadId"];
    const inputs = {};
    for (const f of fields) {
      inputs[f] = el("input", { type: "text", value: ChatState[f], placeholder: f });
      inputs[f].addEventListener("change", () => { ChatState[f] = inputs[f].value.trim(); ChatState.save(); refresh(); });
    }
    targetCard.append(
      el("div", { class: "row" },
        labelled("agent_id", inputs.agentId),
        labelled("channel", inputs.channel),
        labelled("account_id", inputs.accountId),
        labelled("chat_id", inputs.chatId),
        labelled("thread_id (opt)", inputs.threadId),
      ),
    );

    const textarea = el("textarea", { placeholder: "type a message…\nCtrl+Enter to send" });
    const sendBtn = el("button", { class: "btn btn-primary" }, "Send");
    const clearBtn = el("button", { class: "btn btn-danger btn-sm", onclick: () => clearHistory() }, "Clear");
    compose.append(textarea, sendBtn);
    actions.append(clearBtn);

    function labelled(name, input) {
      const wrap = el("div", { class: "field", style: "margin: 0; flex: 1;" });
      wrap.append(el("label", {}, name), input);
      return wrap;
    }

    let polling = null;
    let alive = true;

    async function send() {
      const text = textarea.value.trim();
      if (!text || !ChatState.chatId) return;
      sendBtn.disabled = true;
      textarea.value = "";
      try {
        const result = await safeRpc("chat.send", {
          channel: ChatState.channel,
          account_id: ChatState.accountId,
          chat_id: ChatState.chatId,
          thread_id: ChatState.threadId || null,
          text,
        });
        if (result && result.status === "dropped") {
          // Real drop: no agent ran. Restore text so user can retry.
          Toast.error(
            "message dropped",
            result.reason || "no agent matched the channel",
            6000,
          );
          textarea.value = text;
        } else if (result && result.message_id === "local" && result.reason) {
          // Agent replied to history (chat.history poll renders it) but
          // wire delivery failed — informational only.
          Toast.info("delivery note", result.reason);
        }
        await refresh();
        startPolling();
      } finally {
        sendBtn.disabled = false;
        textarea.focus();
      }
    }
    sendBtn.onclick = send;
    textarea.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); }
    });

    async function clearHistory() {
      if (!confirm(`clear history for ${ChatState.agentId}:${ChatState.sessionKey()}?`)) return;
      await safeRpc("chat.clear", { agent_id: ChatState.agentId, session_key: ChatState.sessionKey() });
      await refresh();
      Toast.success("History cleared");
    }

    async function loadSessions() {
      try {
        const data = await Rpc.call("chat.list_sessions", { agent_id: ChatState.agentId });
        renderSessions(data.sessions || []);
      } catch {
        renderSessions([]);
      }
    }

    function renderSessions(sessions) {
      left.innerHTML = "";
      left.append(el("h3", {}, `${ChatState.agentId} sessions`));
      if (!sessions.length) {
        left.append(el("div", { class: "empty" }, "no sessions yet"));
        return;
      }
      const current = ChatState.sessionKey();
      for (const s of sessions) {
        const item = el("div", {
          class: "session-item" + (s.session_key === current ? " active" : ""),
          onclick: () => {
            const parts = s.session_key.split(":");
            if (parts.length >= 3) {
              ChatState.channel = parts[0];
              ChatState.accountId = parts[1];
              ChatState.chatId = parts[2];
              ChatState.threadId = parts[3] || "";
              ChatState.save();
              for (const f of fields) inputs[f].value = ChatState[f];
              refresh();
            }
          },
        });
        item.append(
          el("div", { class: "key" }, s.session_key),
          el("div", { class: "meta" }, `${s.size}b • ${fmtTime(s.modified_at)}`),
        );
        left.append(item);
      }
    }

    async function refresh() {
      await loadSessions();
      try {
        const data = await Rpc.call("chat.history", {
          agent_id: ChatState.agentId,
          session_key: ChatState.sessionKey(),
        });
        renderStream(data.messages || []);
      } catch (e) {
        stream.innerHTML = "";
        stream.append(el("div", { class: "empty" }, e.message));
      }
    }

    function renderStream(messages) {
      stream.innerHTML = "";
      for (const m of messages) {
        if (m.role === "system") continue; // hide system prompt from chat UI
        const wrap = el("div", { class: `chat-msg ${m.role || "system"}` });
        wrap.append(el("div", { class: "role" }, m.role || "?"));
        const body = el("div", { class: "body" });
        const text = textOf(m.content);
        if (m.role === "assistant" || m.role === "user") {
          body.innerHTML = Markdown.render(text);
        } else {
          body.textContent = text;
        }
        wrap.append(body);
        // Tool calls inline summary.
        if (Array.isArray(m.content)) {
          for (const b of m.content) {
            if (b.type === "tool_use") {
              wrap.append(el("div", { class: "tool-call" }, `🔧 ${b.name}(${JSON.stringify(b.input)})`));
            }
          }
        }
        stream.append(wrap);
      }
      stream.scrollTop = stream.scrollHeight;
    }

    function textOf(content) {
      if (typeof content === "string") return content;
      if (Array.isArray(content)) {
        return content
          .map((b) => {
            if (b.type === "text") return b.text || "";
            if (b.type === "tool_result") return `↩ ${b.content || ""}`;
            return "";
          })
          .filter(Boolean)
          .join("\n");
      }
      return JSON.stringify(content);
    }

    let pollCount = 0;
    function startPolling() {
      if (polling) return;
      pollCount = 0;
      polling = setInterval(async () => {
        if (!alive) return;
        pollCount++;
        await refresh();
        if (pollCount > 30) stopPolling(); // ~60s cap
      }, 2000);
    }
    function stopPolling() {
      if (polling) { clearInterval(polling); polling = null; }
    }

    await refresh();

    return () => { alive = false; stopPolling(); };
  },
};

// ─────────────────────────────────────────────────────────────────────────
// Agents view
// ─────────────────────────────────────────────────────────────────────────
const AgentsView = {
  title: "Agents",
  async render(root, actions) {
    const split = el("div", { class: "split" });
    const left = el("div", { class: "left" });
    const right = el("div", { class: "right" });
    split.append(left, right);
    root.append(split);

    let selected = null;
    let alive = true;

    const refreshBtn = el("button", { class: "btn btn-ghost btn-sm", onclick: () => refresh() }, "↻ Refresh");
    actions.append(refreshBtn);

    async function refresh() {
      const ids = await safeRpc("agents.list", {}, { quiet: true });
      const providers = await safeRpc("agents.providers", {}, { quiet: true });
      renderList(ids || [], providers || []);
      if (selected) await loadDetail(selected);
      else if (ids && ids.length) await loadDetail(ids[0]);
    }

    function renderList(ids, providers) {
      left.innerHTML = "";
      const card = el("div", { class: "card" });
      card.append(el("h3", { class: "card-title" }, "Registered"));
      if (!ids.length) card.append(el("div", { class: "empty" }, "no agents"));
      else {
        const ul = el("ul", { class: "list" });
        for (const id of ids) {
          ul.append(el("li", {
            class: "list-item" + (id === selected ? " active" : ""),
            onclick: () => { selected = id; refresh(); },
          },
            el("div", { class: "title" }, id),
          ));
        }
        card.append(ul);
      }
      left.append(card);
      left.append(renderCreateForm(providers));
    }

    function renderCreateForm(providers) {
      const card = el("div", { class: "card", style: "margin-top:12px" });
      card.append(el("h3", { class: "card-title" }, "Create new"));

      const idIn = el("input", { type: "text", placeholder: "agent id" });
      const provSel = el("select", {});
      for (const p of providers) provSel.append(el("option", { value: p }, p));
      const modelIn = el("input", { type: "text", placeholder: "model (provider-specific)" });
      const baseIn = el("input", { type: "text", placeholder: "base_url (local only)" });
      const sysIn = el("textarea", { placeholder: "system_prompt (optional)" });

      const create = el("button", { class: "btn btn-primary", onclick: async () => {
        if (!idIn.value.trim()) { Toast.warn("agent id required"); return; }
        const params = { id: idIn.value.trim(), provider: provSel.value };
        if (modelIn.value.trim()) params.model = modelIn.value.trim();
        if (baseIn.value.trim()) params.base_url = baseIn.value.trim();
        if (sysIn.value.trim()) params.system_prompt = sysIn.value.trim();
        const res = await safeRpc("agents.create", params);
        if (res.created) {
          Toast.success(`agent ${res.id} created`);
          idIn.value = ""; modelIn.value = ""; baseIn.value = ""; sysIn.value = "";
          await refresh();
        } else {
          Toast.error("create failed", res.error);
        }
      } }, "Create");

      card.append(
        el("div", { class: "field" }, el("label", {}, "id"), idIn),
        el("div", { class: "field" }, el("label", {}, "provider"), provSel),
        el("div", { class: "field" }, el("label", {}, "model"), modelIn),
        el("div", { class: "field" }, el("label", {}, "base_url"), baseIn),
        el("div", { class: "field" }, el("label", {}, "system_prompt"), sysIn),
        create,
      );
      return card;
    }

    async function loadDetail(id) {
      try {
        const d = await Rpc.call("agents.get", { id });
        if (!d.found) { right.innerHTML = ""; right.append(el("div", { class: "empty" }, `agent ${id} not registered`)); return; }
        right.innerHTML = "";
        const card = el("div", { class: "card" });
        card.append(el("h3", { class: "card-title" }, d.id));
        const kv = el("dl", { class: "kv" });
        kv.append(
          el("dt", {}, "provider"), el("dd", {}, el("span", { class: "tag" }, d.provider)),
          el("dt", {}, "model"), el("dd", {}, d.model || "—"),
          el("dt", {}, "base_url"), el("dd", {}, d.base_url || "—"),
          el("dt", {}, "tools"), el("dd", {}, ...(d.tools || []).map((t) => el("span", { class: "tag" }, t))),
        );
        card.append(kv);
        if (d.system_prompt) {
          card.append(
            el("div", { class: "card-section" },
              el("label", {}, "system_prompt"),
              el("pre", { class: "code-block" }, d.system_prompt),
            ),
          );
        }
        const del = el("button", {
          class: "btn btn-danger", style: "margin-top:12px",
          onclick: async () => {
            if (!confirm(`delete agent ${id}?`)) return;
            const r = await safeRpc("agents.delete", { id });
            if (r.deleted) Toast.success(`deleted ${id}`);
            selected = null;
            await refresh();
          },
        }, "Delete agent");
        card.append(del);
        right.append(card);
      } catch (e) {
        right.innerHTML = "";
        right.append(el("div", { class: "empty" }, e.message));
      }
    }

    await refresh();
    return () => { alive = false; };
  },
};

// ─────────────────────────────────────────────────────────────────────────
// Channels view
// ─────────────────────────────────────────────────────────────────────────
const ChannelsView = {
  title: "Channels",
  async render(root, actions) {
    const refreshBtn = el("button", { class: "btn btn-ghost btn-sm", onclick: () => render() }, "↻ Refresh");
    actions.append(refreshBtn);

    async function render() {
      root.innerHTML = "";
      const data = await safeRpc("channels.list", {}, { quiet: true });
      const entries = Object.entries(data || {});
      if (!entries.length) {
        root.append(el("div", { class: "card" },
          el("h3", { class: "card-title" }, "No channels loaded"),
          el("div", { class: "card-meta" },
            "Add Telegram credentials at ~/.sampyclaw/credentials/telegram/<account>.json " +
            "and declare the account in config.yaml under channels.telegram.accounts, then call config.reload."),
        ));
        return;
      }
      const grid = el("div", { class: "grid-2" });
      for (const [chan, accts] of entries) {
        const card = el("div", { class: "card" });
        card.append(el("h3", { class: "card-title" }, chan));
        if (!accts.length) {
          card.append(el("div", { class: "empty" }, "no accounts"));
        } else {
          const ul = el("ul", { class: "list" });
          for (const acct of accts) {
            const item = el("li", { class: "list-item" });
            item.append(el("div", { class: "title" }, acct));
            const status = el("div", { class: "meta" }, "—");
            const probe = el("button", {
              class: "btn btn-ghost btn-sm",
              style: "margin-top:6px",
              onclick: async () => {
                status.textContent = "probing…";
                try {
                  const r = await Rpc.call("channels.probe", { channel: chan, account_id: acct });
                  if (r.ok) {
                    item.querySelector(".meta").innerHTML = "";
                    item.querySelector(".meta").append(
                      el("span", { class: "tag good" }, "ok"),
                      ` ${r.display_name || r.account_id}`,
                    );
                  } else {
                    item.querySelector(".meta").innerHTML = "";
                    item.querySelector(".meta").append(
                      el("span", { class: "tag bad" }, "fail"),
                      ` ${r.error || ""}`,
                    );
                  }
                } catch (e) {
                  status.textContent = `err: ${e.message}`;
                }
              },
            }, "Probe");
            item.append(status, probe);
            ul.append(item);
          }
          card.append(ul);
        }
        grid.append(card);
      }
      root.append(grid);
    }

    await render();
  },
};

// ─────────────────────────────────────────────────────────────────────────
// Cron view
// ─────────────────────────────────────────────────────────────────────────
const CronView = {
  title: "Cron",
  async render(root, actions) {
    const list = el("div");
    const formCard = el("div", { class: "card", style: "margin-top:16px" });
    root.append(list, formCard);

    const refreshBtn = el("button", { class: "btn btn-ghost btn-sm", onclick: () => refresh() }, "↻ Refresh");
    actions.append(refreshBtn);

    async function refresh() {
      const jobs = await safeRpc("cron.list", {}, { quiet: true });
      list.innerHTML = "";
      if (!jobs || !jobs.length) {
        list.append(emptyState({
          icon: "⏱",
          title: "No scheduled jobs yet",
          body: "Cron jobs let an agent run a prompt on a schedule. " +
                "The easiest way to add one is to <strong>ask the agent</strong> from " +
                "the Chat tab — the cron tool will register it for you.",
          example: 'e.g. "Every weekday at 9am summarise overnight Slack DMs"',
        }));
      } else {
        for (const j of jobs) {
          const row = el("div", { class: "cron-row" + (j.enabled ? "" : " disabled") });
          row.append(
            el("div", {},
              el("div", { class: "schedule" }, j.schedule),
              el("div", { class: "next" }, "next: " + (j.next_run_at ? fmtFuture(j.next_run_at) : "not scheduled")),
            ),
            el("div", {},
              el("div", { class: "prompt" }, j.prompt),
              el("div", { class: "meta" }, `→ ${j.agent_id} via ${j.channel}:${j.account_id}:${j.chat_id}`),
            ),
            el("div", { class: "actions" },
              el("button", { class: "btn btn-sm", onclick: async () => {
                const r = await safeRpc("cron.fire", { id: j.id });
                if (r.fired) Toast.success(`fired ${j.id.slice(0,8)}`);
                await refresh();
              } }, "fire"),
              el("button", { class: "btn btn-sm", onclick: async () => {
                await safeRpc("cron.toggle", { id: j.id, enabled: !j.enabled });
                await refresh();
              } }, j.enabled ? "disable" : "enable"),
              el("button", { class: "btn btn-sm btn-danger", onclick: async () => {
                if (!confirm(`remove ${j.id}?`)) return;
                await safeRpc("cron.remove", { id: j.id });
                await refresh();
              } }, "remove"),
            ),
          );
          list.append(row);
        }
      }
    }

    formCard.append(el("h3", { class: "card-title" }, "New cron job"));
    const inputs = {
      schedule: el("input", { type: "text", placeholder: "*/5 * * * *" }),
      agent_id: el("input", { type: "text", placeholder: "assistant", value: "assistant" }),
      channel: el("input", { type: "text", placeholder: "telegram", value: "telegram" }),
      account_id: el("input", { type: "text", placeholder: "main", value: "main" }),
      chat_id: el("input", { type: "text", placeholder: "chat_id" }),
      thread_id: el("input", { type: "text", placeholder: "thread_id (optional)" }),
      prompt: el("textarea", { placeholder: "prompt sent to agent on each fire" }),
    };
    const labelOf = (k, label) => el("div", { class: "field" }, el("label", {}, label || k), inputs[k]);
    formCard.append(
      el("div", { class: "row" }, labelOf("schedule", "schedule (5-field cron)"), labelOf("agent_id", "agent_id")),
      el("div", { class: "row" }, labelOf("channel", "channel"), labelOf("account_id", "account_id"), labelOf("chat_id", "chat_id"), labelOf("thread_id", "thread_id")),
      labelOf("prompt", "prompt"),
      el("button", { class: "btn btn-primary", onclick: async () => {
        const params = {
          schedule: inputs.schedule.value.trim(),
          agent_id: inputs.agent_id.value.trim(),
          channel: inputs.channel.value.trim(),
          account_id: inputs.account_id.value.trim(),
          chat_id: inputs.chat_id.value.trim(),
          prompt: inputs.prompt.value.trim(),
        };
        if (inputs.thread_id.value.trim()) params.thread_id = inputs.thread_id.value.trim();
        if (!params.schedule || !params.chat_id || !params.prompt) {
          Toast.warn("schedule + chat_id + prompt required");
          return;
        }
        try {
          const res = await Rpc.call("cron.create", params);
          Toast.success(`created ${res.id.slice(0,8)}`);
          inputs.prompt.value = "";
          await refresh();
        } catch (e) {
          Toast.error("create failed", e.message);
        }
      } }, "Create"),
    );

    await refresh();
    const interval = setInterval(refresh, 5000);
    return () => clearInterval(interval);
  },
};

// ─────────────────────────────────────────────────────────────────────────
// Approvals view
// ─────────────────────────────────────────────────────────────────────────
const ApprovalsView = {
  title: "Approvals",
  async render(root, actions) {
    const refreshBtn = el("button", { class: "btn btn-ghost btn-sm", onclick: () => refresh() }, "↻ Refresh");
    actions.append(refreshBtn);

    async function refresh() {
      const list = await safeRpc("exec-approvals.list", {}, { quiet: true });
      root.innerHTML = "";
      if (!list || !list.length) {
        root.append(emptyState({
          icon: "✅",
          title: "No pending approvals",
          body: "Tools wrapped via <code>gated_tool()</code> pause and ask " +
                "for human confirmation before running. Pending requests appear " +
                "here — Approve or Deny to unblock the agent.",
        }));
        return;
      }
      for (const a of list) {
        const item = el("div", { class: "approval" });
        item.append(
          el("div", { class: "prompt" }, a.prompt),
          el("div", { class: "ctx" }, `id=${a.id} • ${JSON.stringify(a.context || {})}`),
          el("div", { class: "actions" },
            el("button", { class: "btn btn-good", onclick: async () => {
              await safeRpc("exec-approvals.resolve", { id: a.id, approved: true });
              Toast.success("approved");
              await refresh();
            } }, "Approve"),
            el("button", { class: "btn btn-danger", onclick: async () => {
              const reason = window.prompt("reason (optional):") || null;
              await safeRpc("exec-approvals.resolve", { id: a.id, approved: false, reason });
              Toast.warn("denied");
              await refresh();
            } }, "Deny"),
            el("button", { class: "btn btn-ghost", onclick: async () => {
              await safeRpc("exec-approvals.cancel", { id: a.id });
              await refresh();
            } }, "Cancel"),
          ),
        );
        root.append(item);
      }
    }

    await refresh();
    const interval = setInterval(refresh, 3000);
    return () => clearInterval(interval);
  },
};

// ─────────────────────────────────────────────────────────────────────────
// Config view
// ─────────────────────────────────────────────────────────────────────────
const ConfigView = {
  title: "Config",
  async render(root, actions) {
    const reload = el("button", { class: "btn btn-ghost btn-sm", onclick: async () => {
      const r = await safeRpc("config.reload", {});
      Toast.success("reloaded", `channels=${r.channels.join(", ") || "—"}; agents=${r.agents.join(", ") || "—"}`);
      await render();
    } }, "↻ Reload from disk");
    actions.append(reload);

    async function render() {
      root.innerHTML = "";
      const cfg = await safeRpc("config.get", {}, { quiet: true });
      root.append(
        el("div", { class: "card" },
          el("div", { class: "card-meta" },
            "Live config viewer. Edit ~/.sampyclaw/config.yaml on disk and click Reload above. " +
            "RPC writes are intentionally not exposed."),
        ),
        el("pre", { class: "code-block", style: "margin-top:12px" }, JSON.stringify(cfg, null, 2)),
      );
    }
    await render();
  },
};

// ─────────────────────────────────────────────────────────────────────────
// Skills view — search ClawHub, install/update/uninstall, browse installed
// ─────────────────────────────────────────────────────────────────────────
const SkillsView = {
  title: "Skills",
  async render(root, actions) {
    let mode = sessionStorage.getItem("samp.skills.mode") || "installed";
    let lastQuery = "";
    let alive = true;

    const tabs = el("div", { class: "skills-tabs" });
    const browseTab = el("div", { class: "skills-tab", onclick: () => switchMode("browse") }, "Browse");
    const installedTab = el("div", { class: "skills-tab", onclick: () => switchMode("installed") }, "Installed");
    tabs.append(installedTab, browseTab);

    const body = el("div");
    root.append(tabs, body);

    function switchMode(next) {
      mode = next;
      sessionStorage.setItem("samp.skills.mode", next);
      installedTab.classList.toggle("active", mode === "installed");
      browseTab.classList.toggle("active", mode === "browse");
      render();
    }
    switchMode(mode);

    async function render() {
      body.innerHTML = "";
      if (mode === "installed") await renderInstalled();
      else await renderBrowse();
    }

    async function renderInstalled() {
      const r = await safeRpc("skills.list_installed", {}, { quiet: true });
      const skills = (r && r.skills) || [];
      if (!skills.length) {
        body.append(el("div", { class: "card" },
          el("h3", { class: "card-title" }, "No skills installed"),
          el("div", { class: "card-meta" },
            "Switch to the Browse tab or run `sampyclaw skills search` from a terminal."),
        ));
        return;
      }
      for (const s of skills) {
        body.append(renderInstalledCard(s));
      }
    }

    function renderInstalledCard(s) {
      const card = el("div", { class: "skill-card" });
      const head = el("div", { class: "head" });
      if (s.emoji) head.append(el("span", { class: "emoji" }, s.emoji));
      head.append(
        el("span", { class: "slug" }, s.slug),
        el("span", { class: "name" }, s.name === s.slug ? "" : `· ${s.name}`),
        el("span", { class: "ver" }, s.version ? `v${s.version}` : ""),
      );
      card.append(head);
      if (s.description) card.append(el("div", { class: "summary" }, s.description));
      const reqBins = [...(s.requires?.bins || []), ...(s.requires?.anyBins || [])];
      if (reqBins.length) {
        const req = el("div", { class: "meta" }, "requires: ");
        for (const b of reqBins) req.append(el("span", { class: "tag req" }, b));
        card.append(req);
      }
      const meta = el("div", { class: "meta" });
      const ts = s.installed_at ? fmtTime(s.installed_at) : "?";
      meta.textContent = `installed ${ts}${s.registry ? " · " + s.registry : ""}`;
      card.append(meta);
      card.append(el("div", { class: "actions" },
        el("button", { class: "btn btn-sm", onclick: async () => {
          const r2 = await safeRpc("skills.update", { slug: s.slug });
          if (r2.ok) Toast.success(`updated ${s.slug}`, `v${r2.version}`);
          await render();
        } }, "Update"),
        el("button", { class: "btn btn-sm btn-danger", onclick: async () => {
          if (!confirm(`uninstall ${s.slug}?`)) return;
          const r2 = await safeRpc("skills.uninstall", { slug: s.slug });
          if (r2.ok && r2.removed) Toast.success(`uninstalled ${s.slug}`);
          await render();
        } }, "Uninstall"),
        el("button", { class: "btn btn-sm btn-ghost", onclick: () => showDetail(s.slug) }, "Detail"),
      ));
      return card;
    }

    async function renderBrowse() {
      const bar = el("div", { class: "search-bar" });
      const input = el("input", { type: "search", placeholder: "search clawhub… (empty = list all)", value: lastQuery });
      const go = el("button", { class: "btn btn-primary", onclick: () => doSearch(input.value) }, "Search");
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(input.value); });
      bar.append(input, go);
      body.append(bar);

      const results = el("div");
      body.append(results);

      async function doSearch(q) {
        lastQuery = q || "";
        results.innerHTML = "";
        const loading = el("div", { class: "empty" }, "loading…");
        results.append(loading);
        try {
          let list;
          if (q && q.trim()) {
            const r = await Rpc.call("skills.search", { query: q.trim(), limit: 50 });
            list = r.ok ? r.results : [];
            if (!r.ok) throw new Error(r.error || "search failed");
          } else {
            const r = await Rpc.call("skills.list_remote", { limit: 50 });
            if (!r.ok) throw new Error(r.error || "list failed");
            list = r.results || r.skills || [];
          }
          results.innerHTML = "";
          if (!list.length) {
            results.append(el("div", { class: "empty" }, q ? `no matches for "${q}"` : "registry returned empty list"));
            return;
          }
          const installed = await Rpc.call("skills.list_installed", {});
          const installedSlugs = new Set((installed.skills || []).map((s) => s.slug));
          for (const r of list) results.append(renderRemoteCard(r, installedSlugs));
        } catch (e) {
          results.innerHTML = "";
          results.append(el("div", { class: "empty" }, `error: ${e.message}`));
        }
      }

      // Initial load.
      await doSearch(lastQuery);
    }

    function renderRemoteCard(r, installedSlugs) {
      const slug = r.slug || r.id || "?";
      const card = el("div", { class: "skill-card" });
      const head = el("div", { class: "head" });
      head.append(
        el("span", { class: "slug" }, slug),
        el("span", { class: "name" }, r.displayName && r.displayName !== slug ? `· ${r.displayName}` : ""),
        el("span", { class: "ver" }, r.version ? `v${r.version}` : ""),
      );
      card.append(head);
      if (r.summary) card.append(el("div", { class: "summary" }, r.summary));
      if (r.updatedAt) card.append(el("div", { class: "meta" }, `updated ${fmtTime(r.updatedAt / 1000)}`));

      const installed = installedSlugs.has(slug);
      const actions = el("div", { class: "actions" });
      actions.append(
        el("button", {
          class: "btn btn-primary btn-sm",
          onclick: async () => {
            actions.querySelectorAll("button").forEach((b) => (b.disabled = true));
            const res = await safeRpc("skills.install", { slug, force: installed });
            if (res.ok) {
              Toast.success(`installed ${res.slug}`, `v${res.version}`);
              const reqBins = [...(res.manifest?.requires?.bins || []), ...(res.manifest?.requires?.anyBins || [])];
              if (reqBins.length) Toast.warn("requires on PATH", reqBins.join(", "));
            }
            actions.querySelectorAll("button").forEach((b) => (b.disabled = false));
            await render();
          },
        }, installed ? "Reinstall" : "Install"),
        el("button", { class: "btn btn-sm btn-ghost", onclick: () => showDetail(slug) }, "Detail"),
      );
      card.append(actions);
      return card;
    }

    async function showDetail(slug) {
      const overlay = el("div", { class: "cmd-help" });
      const card = el("div", { class: "cmd-help-card", style: "min-width: 520px; max-width: 700px;" });
      card.append(el("h3", {}, slug));
      const body2 = el("pre", { class: "code-block", style: "max-height: 60vh" }, "loading…");
      const close = el("button", { class: "btn btn-ghost btn-sm", onclick: () => overlay.remove() }, "close");
      card.append(body2, el("div", { style: "margin-top:12px" }, close));
      overlay.append(card);
      document.body.append(overlay);
      try {
        const r = await Rpc.call("skills.detail", { slug });
        body2.textContent = JSON.stringify(r.detail || r, null, 2);
      } catch (e) {
        body2.textContent = `error: ${e.message}`;
      }
    }

    const refreshBtn = el("button", { class: "btn btn-ghost btn-sm", onclick: () => render() }, "↻ Refresh");
    actions.append(refreshBtn);

    return () => { alive = false; };
  },
};

// ─────────────────────────────────────────────────────────────────────────
// Memory view — search the long-term knowledge index
// ─────────────────────────────────────────────────────────────────────────
const MemoryView = {
  title: "Memory",
  async render(root, actions) {
    const card = el("div", { class: "card" });
    const bar = el("div", { class: "search-bar" });
    const input = el("input", {
      type: "search",
      placeholder: "Search memory… (sqlite-vec + FTS5 + MMR rerank)",
    });
    const kInput = el("input", { type: "number", value: "10", style: "max-width:80px" });
    const goBtn = el("button", { class: "btn btn-primary" }, "Search");
    bar.append(input, kInput, goBtn);
    const results = el("div");
    card.append(bar, results);
    root.append(card);

    async function search() {
      const q = input.value.trim();
      const k = Math.max(1, Math.min(50, parseInt(kInput.value, 10) || 10));
      results.innerHTML = "";
      if (!q) {
        results.append(emptyState({
          icon: "🧠",
          title: "Search the long-term memory",
          body: "Sessions, ingested docs, and explicit notes the agent saved " +
                "are indexed here. Memory is enriched as you chat — empty for now " +
                "is normal.",
          example: 'try: "what did we decide about the Gerrit MCP plan?"',
        }));
        return;
      }
      let res;
      try {
        res = await Rpc.call("memory.search", { query: q, k });
      } catch (e) {
        results.append(el("div", { class: "empty" }, e.message));
        return;
      }
      const hits = (res && res.hits) || [];
      if (!hits.length) {
        results.append(emptyState({
          icon: "🔍",
          title: "No matches",
          body: `Nothing in memory matches <code>${q.replace(/[<>&]/g, "")}</code> right now.`,
        }));
        return;
      }
      const list = el("ul", { class: "list" });
      for (const h of hits) {
        const item = el("li", { class: "list-item" });
        item.append(
          el("div", { class: "title" }, h.path || h.id || "(untitled)"),
          el("div", { class: "meta" },
            (h.score != null ? `score=${Number(h.score).toFixed(3)} · ` : "") +
            (h.source || "") +
            (h.session_key ? ` · ${h.session_key}` : ""),
          ),
          el("div", {
            style: "margin-top:6px;font-size:12px;color:var(--fg-2);" +
                   "white-space:pre-wrap;word-break:break-word;",
          }, h.text || h.content || ""),
        );
        list.append(item);
      }
      results.append(list);
    }
    goBtn.onclick = search;
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") search(); });
    search();
  },
};

// ─────────────────────────────────────────────────────────────────────────
// Sessions view — list / preview / reset / fork / archive / delete
// (Phase 11–15 backend, sessions.* RPCs)
// ─────────────────────────────────────────────────────────────────────────
const SessionsView = {
  title: "Sessions",
  async render(root, actions) {
    const refreshBtn = el("button", { class: "btn btn-ghost btn-sm",
      onclick: () => refresh() }, "↻ Refresh");
    actions.append(refreshBtn);

    const filterBar = el("div", { class: "search-bar" });
    const agentFilter = el("input", {
      type: "text",
      placeholder: "filter by agent_id (empty = all)",
    });
    filterBar.append(agentFilter);
    root.append(filterBar);
    const list = el("div");
    root.append(list);

    async function refresh() {
      const params = {};
      if (agentFilter.value.trim()) params.agent_id = agentFilter.value.trim();
      let res;
      try {
        res = await Rpc.call("sessions.list", params);
      } catch (e) {
        list.innerHTML = "";
        list.append(emptyState({
          icon: "🗂",
          title: "Sessions RPC unavailable",
          body: `<code>sessions.*</code> isn't registered on this gateway. ` +
                `That's only wired when the operator passes a SessionManager + LifecycleBus to the gateway. ` +
                `Error: ${e.message}`,
        }));
        return;
      }
      const sessions = (res && (res.sessions || res)) || [];
      list.innerHTML = "";
      if (!sessions.length) {
        list.append(emptyState({
          icon: "🗂",
          title: "No sessions yet",
          body: "Sessions accumulate as channels deliver inbound messages " +
                "to agents. Open the <strong>Chat</strong> tab and send a turn " +
                "to seed one.",
          actions: [
            el("button", {
              class: "btn btn-primary",
              onclick: () => Router.go("chat"),
            }, "Go to Chat"),
          ],
        }));
        return;
      }
      const badge = $("nav-sessions-badge");
      if (badge) { badge.hidden = false; badge.textContent = String(sessions.length); }
      for (const s of sessions) {
        const card = el("div", { class: "session-card" });
        card.append(
          el("div", {},
            el("div", { class: "head" },
              el("span", { class: "key" }, `${s.agent_id || "?"} · ${s.session_key}`),
              el("span", { class: "tag" }, `${s.message_count ?? "?"} msgs`),
              s.archived ? el("span", { class: "tag warn" }, "archived") : null,
            ),
            el("div", { class: "meta" },
              `updated ${fmtTime(s.updated_at)} · created ${fmtTime(s.created_at)}`,
            ),
            (s.first_preview || s.last_preview) ? el("div", { class: "preview" },
              [s.first_preview, s.last_preview].filter(Boolean).join(" · "),
            ) : null,
          ),
          el("div", { class: "actions" },
            el("button", { class: "btn btn-sm", onclick: () => preview(s) }, "Preview"),
            el("button", { class: "btn btn-sm", onclick: () => reset(s) }, "Reset"),
            el("button", { class: "btn btn-sm", onclick: () => fork(s) }, "Fork"),
            el("button", { class: "btn btn-sm", onclick: () => archive(s) }, "Archive"),
            el("button", { class: "btn btn-sm btn-danger", onclick: () => del(s) }, "Delete"),
          ),
        );
        list.append(card);
      }
    }

    async function preview(s) {
      try {
        const res = await safeRpc("sessions.preview", {
          agent_id: s.agent_id, session_key: s.session_key,
        });
        const text = (res && res.preview) || JSON.stringify(res, null, 2);
        Toast.info("preview", text.slice(0, 400));
      } catch {}
    }
    async function reset(s) {
      if (!confirm(`Reset session ${s.session_key}? Messages will be cleared.`)) return;
      await safeRpc("sessions.reset", {
        agent_id: s.agent_id, session_key: s.session_key,
      });
      Toast.success("session reset");
      refresh();
    }
    async function fork(s) {
      const newKey = window.prompt("New session_key for the fork:", s.session_key + "-fork");
      if (!newKey) return;
      await safeRpc("sessions.fork", {
        agent_id: s.agent_id, source_session_key: s.session_key, new_session_key: newKey,
      });
      Toast.success(`forked → ${newKey}`);
      refresh();
    }
    async function archive(s) {
      await safeRpc("sessions.archive", {
        agent_id: s.agent_id, session_key: s.session_key,
      });
      Toast.success("archived");
      refresh();
    }
    async function del(s) {
      if (!confirm(`Delete session ${s.session_key}? This cannot be undone.`)) return;
      await safeRpc("sessions.delete", {
        agent_id: s.agent_id, session_key: s.session_key,
      });
      Toast.warn("session deleted");
      refresh();
    }

    agentFilter.addEventListener("change", refresh);
    await refresh();
    const interval = setInterval(refresh, 5000);
    return () => clearInterval(interval);
  },
};

// ─────────────────────────────────────────────────────────────────────────
// RPC log view
// ─────────────────────────────────────────────────────────────────────────
const RpcLogView = {
  title: "RPC log",
  async render(root) {
    const pre = el("div", { class: "rpc-log" });
    root.append(pre);
    function refresh() {
      pre.innerHTML = "";
      for (const entry of Rpc.log) {
        const line = el("div");
        const ts = entry.ts.toISOString().slice(11, 23);
        const cls = entry.error ? "err" : entry.direction;
        line.append(
          el("span", { class: "ts" }, `[${ts}] `),
          el("span", { class: cls }, entry.direction === "out" ? "→ " : "← "),
          document.createTextNode(JSON.stringify(entry.payload)),
        );
        pre.append(line);
      }
      if (!Rpc.log.length) pre.append(el("div", { class: "empty" }, "no frames yet"));
    }
    refresh();
    const interval = setInterval(refresh, 1000);
    return () => clearInterval(interval);
  },
};

// ─────────────────────────────────────────────────────────────────────────
// Connection state UI + nav badges
// ─────────────────────────────────────────────────────────────────────────
function bindConnectionState() {
  Rpc.setStateListener((kind, url) => {
    const dot = $("status-dot");
    const text = $("status-text");
    dot.className = `dot ${kind === "up" ? "up" : kind === "down" ? "down" : "busy"}`;
    text.textContent = kind === "up" ? `up · ${url}` : kind;
  });
}

async function refreshNavBadges() {
  try {
    const list = await Rpc.call("exec-approvals.list");
    const badge = $("nav-approvals-badge");
    if (list && list.length) { badge.hidden = false; badge.textContent = String(list.length); }
    else badge.hidden = true;
  } catch {}
  try {
    const r = await Rpc.call("skills.list_installed");
    const badge = $("nav-skills-badge");
    const n = (r && r.skills && r.skills.length) || 0;
    if (n > 0) { badge.hidden = false; badge.textContent = String(n); }
    else badge.hidden = true;
  } catch {}
  // Sessions badge: only updates when sessions.* RPCs are wired. Failure
  // is silent so the dashboard works on gateways without that surface.
  try {
    const r = await Rpc.call("sessions.list", {});
    const sessions = (r && (r.sessions || r)) || [];
    const badge = $("nav-sessions-badge");
    if (badge && sessions.length) { badge.hidden = false; badge.textContent = String(sessions.length); }
    else if (badge) badge.hidden = true;
  } catch {}
}

// ─────────────────────────────────────────────────────────────────────────
// Keyboard shortcuts
// ─────────────────────────────────────────────────────────────────────────
function bindShortcuts() {
  let pendingG = null;
  document.addEventListener("keydown", (e) => {
    const tag = (e.target && e.target.tagName) || "";
    const isInput = tag === "INPUT" || tag === "TEXTAREA" || (e.target && e.target.isContentEditable);
    // Command palette opens regardless of input focus.
    if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      Palette.toggle();
      return;
    }
    if (e.ctrlKey && e.key === "/") { e.preventDefault(); toggleHelp(); return; }
    if (e.key === "Escape") { hideHelp(); Palette.hide(); return; }
    if (isInput) return;
    if (pendingG) {
      const target = {
        c: "chat", a: "agents", k: "channels", x: "sessions", r: "cron",
        p: "approvals", s: "skills", m: "memory", g: "config",
      }[e.key];
      pendingG = null;
      if (target) Router.go(target);
      return;
    }
    if (e.key === "g") { pendingG = setTimeout(() => { pendingG = null; }, 800); }
  });
}
function toggleHelp() {
  const h = $("cmd-help"); h.hidden = !h.hidden;
}
function hideHelp() { $("cmd-help").hidden = true; }

// ─────────────────────────────────────────────────────────────────────────
// Theme toggle (light / dark / system) — cycles on click; saved preference
// is read by the boot script in app.html before paint.
// ─────────────────────────────────────────────────────────────────────────
const Theme = (() => {
  const KEY = "sampyclaw_theme";
  const ORDER = ["system", "light", "dark"];
  function pref() {
    return localStorage.getItem(KEY) || "system";
  }
  function resolved(p) {
    if (p === "system") {
      return window.matchMedia("(prefers-color-scheme: light)").matches
        ? "light" : "dark";
    }
    return p;
  }
  function apply(p) {
    document.documentElement.setAttribute("data-theme", resolved(p));
    document.documentElement.setAttribute("data-theme-pref", p);
    try { localStorage.setItem(KEY, p); } catch {}
    const btn = $("theme-toggle");
    if (btn) {
      btn.textContent = p === "light" ? "☀" : p === "dark" ? "☾" : "🌓";
      btn.title = `Theme: ${p} (click to cycle)`;
    }
  }
  function cycle() {
    const cur = pref();
    const next = ORDER[(ORDER.indexOf(cur) + 1) % ORDER.length];
    apply(next);
    Toast.info("theme", `→ ${next}`);
  }
  function bind() {
    apply(pref());
    const btn = $("theme-toggle");
    if (btn) btn.onclick = cycle;
    // React to system theme changes when "system" is chosen.
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    mq.addEventListener?.("change", () => {
      if (pref() === "system") apply("system");
    });
  }
  return { bind };
})();

// ─────────────────────────────────────────────────────────────────────────
// Mobile nav drawer toggle
// ─────────────────────────────────────────────────────────────────────────
function bindNavToggle() {
  const app = document.getElementById("app");
  const btn = $("nav-toggle");
  const back = $("nav-backdrop");
  function open()  { app.classList.add("nav-open"); }
  function close() { app.classList.remove("nav-open"); }
  if (btn) btn.onclick = () => app.classList.toggle("nav-open");
  if (back) back.onclick = close;
  // Close drawer on route change.
  window.addEventListener("hashchange", close);
}

// ─────────────────────────────────────────────────────────────────────────
// Command palette — Ctrl/Cmd+K opens; type to filter; Enter runs.
// Items are pure-JS objects with id, title, hint, group, run().
// ─────────────────────────────────────────────────────────────────────────
const Palette = (() => {
  const ITEMS = [
    { id: "go-chat",       group: "Go to", icon: "💬", title: "Chat",       hint: "g c", run: () => Router.go("chat") },
    { id: "go-agents",     group: "Go to", icon: "🤖", title: "Agents",     hint: "g a", run: () => Router.go("agents") },
    { id: "go-channels",   group: "Go to", icon: "📡", title: "Channels",   hint: "g k", run: () => Router.go("channels") },
    { id: "go-sessions",   group: "Go to", icon: "🗂", title: "Sessions",   hint: "g x", run: () => Router.go("sessions") },
    { id: "go-cron",       group: "Go to", icon: "⏱",  title: "Cron",       hint: "g r", run: () => Router.go("cron") },
    { id: "go-approvals",  group: "Go to", icon: "✅", title: "Approvals",  hint: "g p", run: () => Router.go("approvals") },
    { id: "go-skills",     group: "Go to", icon: "🧰", title: "Skills",     hint: "g s", run: () => Router.go("skills") },
    { id: "go-memory",     group: "Go to", icon: "🧠", title: "Memory",     hint: "g m", run: () => Router.go("memory") },
    { id: "go-config",     group: "Go to", icon: "⚙",  title: "Config",     hint: "g g", run: () => Router.go("config") },
    { id: "go-rpc",        group: "Go to", icon: "📜", title: "RPC log",    hint: "",     run: () => Router.go("rpc") },
    { id: "theme-cycle",   group: "Action", icon: "🌓", title: "Cycle theme (system → light → dark)", hint: "",
      run: () => { /* call inside Theme module */ document.getElementById("theme-toggle").click(); } },
    { id: "config-reload", group: "Action", icon: "↻", title: "Reload config from disk", hint: "",
      run: async () => {
        try { const r = await Rpc.call("config.reload", {});
          Toast.success("reloaded", `channels=${(r.channels || []).join(", ") || "—"}`);
        } catch (e) { Toast.error("reload failed", e.message); }
      } },
    { id: "approvals-refresh", group: "Action", icon: "✅", title: "Open Approvals queue", hint: "",
      run: () => Router.go("approvals") },
    { id: "help",          group: "Action", icon: "?", title: "Show keyboard shortcuts", hint: "Ctrl+/",
      run: () => toggleHelp() },
  ];

  let listEl, inputEl, paletteEl, activeIdx = 0, filtered = [];

  function render() {
    listEl.innerHTML = "";
    if (!filtered.length) {
      listEl.append(el("div", { class: "cmd-palette__empty" }, "No matches"));
      return;
    }
    let lastGroup = null;
    filtered.forEach((it, i) => {
      if (it.group !== lastGroup) {
        listEl.append(el("div", { class: "cmd-palette__group" }, it.group));
        lastGroup = it.group;
      }
      const node = el("div", {
        class: "cmd-palette__item" + (i === activeIdx ? " active" : ""),
        onclick: () => choose(i),
      },
        el("span", { class: "cmd-palette__icon" }, it.icon || "•"),
        el("span", { class: "cmd-palette__title" }, it.title),
        it.hint ? el("span", { class: "cmd-palette__hint" }, it.hint) : null,
      );
      listEl.append(node);
    });
  }

  function filter(q) {
    q = q.trim().toLowerCase();
    if (!q) filtered = ITEMS.slice();
    else filtered = ITEMS.filter((it) =>
      it.title.toLowerCase().includes(q) ||
      (it.group || "").toLowerCase().includes(q));
    activeIdx = 0;
    render();
  }

  function choose(i) {
    const it = filtered[i];
    if (!it) return;
    hide();
    try { it.run(); } catch (e) { Toast.error("command failed", e.message); }
  }

  function show() {
    paletteEl.hidden = false;
    inputEl.value = "";
    filter("");
    requestAnimationFrame(() => inputEl.focus());
  }
  function hide() { paletteEl.hidden = true; }
  function toggle() { paletteEl.hidden ? show() : hide(); }

  function bind() {
    paletteEl = $("cmd-palette");
    inputEl = $("cmd-palette-input");
    listEl = $("cmd-palette-list");
    paletteEl.addEventListener("click", (e) => {
      if (e.target === paletteEl) hide();
    });
    inputEl.addEventListener("input", () => filter(inputEl.value));
    inputEl.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") { e.preventDefault(); activeIdx = Math.min(activeIdx + 1, filtered.length - 1); render(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); activeIdx = Math.max(activeIdx - 1, 0); render(); }
      else if (e.key === "Enter") { e.preventDefault(); choose(activeIdx); }
      else if (e.key === "Escape") { e.preventDefault(); hide(); }
    });
    const btn = $("cmd-palette-btn");
    if (btn) btn.onclick = toggle;
  }
  return { bind, show, hide, toggle };
})();

// ─────────────────────────────────────────────────────────────────────────
// Bootstrap
// ─────────────────────────────────────────────────────────────────────────
function boot() {
  bindConnectionState();

  Router.register("chat", ChatView);
  Router.register("agents", AgentsView);
  Router.register("channels", ChannelsView);
  Router.register("sessions", SessionsView);
  Router.register("cron", CronView);
  Router.register("approvals", ApprovalsView);
  Router.register("skills", SkillsView);
  Router.register("memory", MemoryView);
  Router.register("config", ConfigView);
  Router.register("rpc", RpcLogView);

  $("ws-url").value = Rpc.defaultUrl();
  $("reconnect-btn").onclick = () => Rpc.connect($("ws-url").value);
  $("cmd-help-close").onclick = hideHelp;

  Theme.bind();
  bindNavToggle();
  Palette.bind();
  bindShortcuts();
  bindLoginGate();
  bindCanvasPanel();

  Rpc.connect($("ws-url").value);
  // Token has been captured into the WS URL + (server-set) cookie; clean
  // it out of the address bar so it doesn't leak via shared screenshots
  // or browser history.
  Rpc.scrubTokenFromUrl();
  Router.handleHash();

  // Periodic cheap refreshes for nav badges + approvals event channel.
  setInterval(refreshNavBadges, 4000);
  Rpc.onEvent(() => refreshNavBadges());
}

// ─────────────────────────────────────────────────────────────────────────
// Canvas panel
//
// Subscribes to server-pushed `canvas` event frames and routes the four
// kinds (`present`, `navigate`, `hide`, `eval`) to the right-side canvas
// drawer. The iframe is `sandbox="allow-scripts ..."` and rendered via
// srcdoc, so the agent's HTML cannot reach parent state, cookies, or
// storage. `eval` opens a one-shot MessageChannel so the agent's JS can
// reply with a structured-clone-safe value, which we forward back to the
// gateway via canvas.eval_result.
// ─────────────────────────────────────────────────────────────────────────
function bindCanvasPanel() {
  const panel = $("canvas-panel");
  const frame = $("canvas-frame");
  const titleEl = $("canvas-panel-title");
  const metaEl = $("canvas-panel-meta");
  const hideBtn = $("canvas-panel-hide");
  if (!panel || !frame) return;

  const evalChannels = new Map();   // request_id -> { port, timer }

  function show({ html, title, version }) {
    titleEl.textContent = title || "Canvas";
    metaEl.textContent = version != null ? `v${version}` : "";
    frame.srcdoc = html || "";
    panel.hidden = false;
  }
  function hide() {
    panel.hidden = true;
    frame.srcdoc = "<!doctype html><title>canvas</title>";
    metaEl.textContent = "";
  }
  function navigate({ url }) {
    if (!url) return;
    if (url === "about:blank") {
      frame.srcdoc = "<!doctype html><title>canvas</title>";
      return;
    }
    if (url.startsWith("data:")) {
      // data: URLs render in the sandboxed iframe without leaving the
      // dashboard origin. Anything else was refused server-side.
      frame.src = url;
      panel.hidden = false;
    }
  }
  function runEval({ request_id, payload }) {
    const expr = payload && payload.expression;
    if (!request_id || !expr) return;
    // Wrap the expression so the iframe-side eval returns a value that we
    // can JSON-clone back. Five-second hard timeout matching the server
    // default; we also reply on timeout so the gateway future doesn't hang.
    const channel = new MessageChannel();
    const timer = setTimeout(() => {
      try { Rpc.call("canvas.eval_result", { request_id, ok: false, error: "client timeout" }); } catch {}
      evalChannels.delete(request_id);
    }, 5500);
    channel.port1.onmessage = (ev) => {
      clearTimeout(timer);
      evalChannels.delete(request_id);
      const data = ev.data || {};
      Rpc.call("canvas.eval_result", {
        request_id,
        ok: !!data.ok,
        value: data.ok ? data.value : null,
        error: data.ok ? null : (data.error || "eval failed"),
      }).catch(() => {});
    };
    evalChannels.set(request_id, { port: channel.port1, timer });
    // The iframe needs a bootstrap that listens for the port and runs the
    // expression. We re-srcdoc the existing HTML with a tiny appended
    // <script> only if the iframe doesn't already expose one — easiest
    // path is to inject via postMessage to a known message handler the
    // skill-author wrote, OR fall back to wrapping the expression server-
    // side. For v1 we adopt the latter: skills opt into eval by including
    // `<script>window.addEventListener('message',e=>{...})</script>` in
    // their HTML. If they didn't, the eval will time out cleanly.
    frame.contentWindow?.postMessage(
      { type: "sampyclaw.canvas.eval", expression: expr },
      "*",
      [channel.port2],
    );
  }

  hideBtn.onclick = () => {
    hide();
    // Best-effort server-side hide; agent_id unknown from the panel,
    // so this is intentionally a UI-only hide. The agent can reissue
    // canvas.present to bring the panel back.
  };

  Rpc.onEvent((evt) => {
    if (!evt || evt.kind !== "canvas") return;
    const body = evt.body || {};
    const payload = body.payload || {};
    switch (body.kind) {
      case "present":  return show(payload);
      case "navigate": return navigate(payload);
      case "hide":     return hide();
      case "eval":     return runEval({ request_id: body.request_id, payload });
      default: return;
    }
  });
}


// ─────────────────────────────────────────────────────────────────────────
// Login gate
//
// Shown when the WS upgrade is refused (almost always: missing/wrong
// token) or when the server actively closes early. Lets the user paste
// the gateway token, optionally remember it for 12 hours, and retry the
// connect — exactly the same UX as openclaw's control-ui.
// ─────────────────────────────────────────────────────────────────────────
function bindLoginGate() {
  const gate = $("login-gate");
  const form = $("login-gate-form");
  const urlInput = $("login-gate-url");
  const tokenInput = $("login-gate-token");
  const remember = $("login-gate-remember");
  const toggle = $("login-gate-toggle");
  const errorBox = $("login-gate-error");
  const sub = $("login-gate-sub");

  if (!gate || !form) return;

  let armed = false; // suppress an early-close blip during the very
                     // first connect when no token is configured.
  // Wait one tick before treating early-close as "show login" so the
  // anonymous server-with-no-auth case doesn't flash the gate.
  setTimeout(() => { armed = true; }, 250);

  function show(reason) {
    if (!armed) return;
    urlInput.value = $("ws-url").value || Rpc.defaultUrl();
    tokenInput.value = Rpc.readToken();
    errorBox.hidden = !reason;
    if (reason) errorBox.textContent = reason;
    sub.textContent = reason
      ? "Connection rejected — check the gateway URL and token."
      : "Enter the gateway token to continue.";
    gate.hidden = false;
    setTimeout(() => tokenInput.focus(), 50);
  }

  function hide() { gate.hidden = true; errorBox.hidden = true; }

  Rpc.setAuthFailureListener(({ code, reason }) => {
    show(`WS upgrade failed (code ${code || "?"}${reason ? ": " + reason : ""}).`);
  });

  toggle.addEventListener("click", () => {
    const showing = tokenInput.type === "text";
    tokenInput.type = showing ? "password" : "text";
    toggle.setAttribute("aria-pressed", String(!showing));
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const token = tokenInput.value.trim();
    const target = urlInput.value.trim() || Rpc.defaultUrl();
    if (token) Rpc.storeToken(token, { remember: remember.checked });
    else Rpc.clearStoredToken();
    const wsTarget = token ? Rpc.urlWithToken(target, token) : target;
    $("ws-url").value = wsTarget;
    hide();
    Rpc.connect(wsTarget);
  });

  // If the page boots with no stored token AND we can't tell whether
  // the server requires one, let the WS connect attempt fire first;
  // the auth-failure listener will surface the gate if needed. If the
  // user explicitly pressed "Logout" via cmd-help in the future, we'd
  // call show() here directly.
}

boot();
