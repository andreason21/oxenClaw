// oxenClaw dashboard SPA — vanilla JS, no build step.
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
  // (2) oxenclaw_token cookie, (3) localStorage["oxenclaw_token"]
  // (set by the in-app login gate), (4) none. The chosen token is
  // forwarded to the WS connect as a query string because browsers
  // can't set Authorization headers on a WS upgrade.
  const TOKEN_KEY = "oxenclaw_token";

  function readToken() {
    const params = new URLSearchParams(location.search);
    const fromQuery = params.get("token");
    if (fromQuery) return fromQuery;
    const m = document.cookie.match(/(?:^|;\s*)oxenclaw_token=([^;]+)/);
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
  channel: localStorage.getItem("samp.channel") || "dashboard",
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
  // Generate a fresh `chat_id` and clear `thread_id`, leaving agent/
  // channel/account untouched. PiAgent's `_ensure_session` creates a
  // brand-new ConversationHistory the first time a message is sent
  // against this chat-id, so this is all that's needed to "start a
  // new chat" — no backend RPC required.
  newChat() {
    const stamp = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 14);
    const rand = Math.random().toString(16).slice(2, 6);
    this.chatId = `chat-${stamp}-${rand}`;
    this.threadId = "";
    this.save();
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

    // ── Compact target bar (default) ──────────────────────────────────
    // 5-input row was a Telegram-era artefact (multi-channel + multi-
    // account + thread routing). For dashboard chat 99% of users only
    // care about agent + chat-id. Channel/account_id/thread_id are
    // hidden behind an Advanced toggle.
    const fields = ["agentId", "channel", "accountId", "chatId", "threadId"];
    const requiredFields = new Set(["agentId", "channel", "accountId", "chatId"]);
    const defaults = { agentId: "default", channel: "dashboard", accountId: "main", chatId: "demo", threadId: "" };
    const inputs = {};
    for (const f of fields) {
      inputs[f] = el("input", { type: "text", value: ChatState[f] ?? defaults[f], placeholder: f });
      inputs[f].addEventListener("change", () => {
        const next = inputs[f].value.trim();
        if (!next && requiredFields.has(f)) {
          inputs[f].value = ChatState[f] || defaults[f];
          ChatState[f] = inputs[f].value;
        } else {
          ChatState[f] = next;
        }
        ChatState.save();
        refresh();
      });
    }

    // Agent dropdown (populated from agents.list at render time).
    const agentSelect = el("select", { class: "chat-target__agent" });
    async function reloadAgentChoices() {
      try {
        const ids = await Rpc.call("agents.list", {});
        const list = Array.isArray(ids) && ids.length ? ids : [ChatState.agentId || "assistant"];
        agentSelect.innerHTML = "";
        if (!list.includes(ChatState.agentId)) list.push(ChatState.agentId);
        for (const id of list) {
          const opt = el("option", { value: id }, id);
          if (id === ChatState.agentId) opt.selected = true;
          agentSelect.append(opt);
        }
      } catch (e) {
        agentSelect.innerHTML = "";
        agentSelect.append(el("option", { value: ChatState.agentId }, ChatState.agentId));
      }
    }
    agentSelect.addEventListener("change", () => {
      ChatState.agentId = agentSelect.value;
      ChatState.save();
      // Keep the hidden advanced input synced so toggling Advanced shows
      // a consistent value.
      inputs.agentId.value = ChatState.agentId;
      refresh();
    });

    // chat-id chip + popover (rename / new chat / copy session-key).
    const chatChipLabel = el("span", { class: "chat-target__chat-id" });
    const updateChip = () => {
      chatChipLabel.textContent = ChatState.chatId || "(no chat-id)";
    };
    updateChip();
    const chatChipBtn = el("button", {
      class: "chat-target__chip",
      type: "button",
      title: "Click for chat actions",
      onclick: async () => {
        const next = prompt(
          "Chat-id (used as conversation key). Leave blank for a fresh one.",
          ChatState.chatId || "",
        );
        if (next === null) return;
        if (!next.trim()) {
          ChatState.newChat();
        } else {
          ChatState.chatId = next.trim();
          ChatState.threadId = "";
          ChatState.save();
        }
        inputs.chatId.value = ChatState.chatId;
        inputs.threadId.value = ChatState.threadId;
        updateChip();
        await refresh();
      },
    });
    chatChipBtn.append(el("span", { class: "chat-target__chip-icon" }, "💬"), chatChipLabel);

    const advBtn = el("button", {
      class: "chat-target__adv",
      type: "button",
      title: "Toggle advanced channel / account / thread fields (debug only)",
    }, "⚙️ Advanced");
    let advancedVisible = false;
    const advRow = el("div", {
      class: "chat-target__advanced",
      style: "display:none",
    });
    advRow.append(
      labelled("agent_id", inputs.agentId),
      labelled("channel", inputs.channel),
      labelled("account_id", inputs.accountId),
      labelled("chat_id", inputs.chatId),
      labelled("thread_id (opt)", inputs.threadId),
    );
    advBtn.onclick = () => {
      advancedVisible = !advancedVisible;
      advRow.style.display = advancedVisible ? "" : "none";
      advBtn.textContent = advancedVisible ? "⚙️ Advanced (hide)" : "⚙️ Advanced";
    };

    const compactRow = el("div", { class: "chat-target__compact" },
      el("label", { class: "chat-target__label" }, "Agent"),
      agentSelect,
      chatChipBtn,
      advBtn,
    );
    targetCard.append(compactRow, advRow);
    reloadAgentChoices();

    const textarea = el("textarea", { placeholder: "type a message…\nCtrl+Enter to send" });
    const sendBtn = el("button", { class: "btn btn-primary" }, "Send");
    const clearBtn = el("button", { class: "btn btn-danger btn-sm", onclick: () => clearHistory() }, "Clear");
    // "+ New chat" — fresh chat-id, fresh ConversationHistory on first send.
    // Listed first in the topbar actions row so it's the most prominent
    // affordance for "start a new conversation" (operator's request).
    const newChatBtn = el("button", {
      class: "btn btn-sm",
      title: "Start a new chat (Ctrl+Shift+N)",
      onclick: () => startNewChat({ inputs }),
    }, "+ New chat");

    // Image attach: hidden file input + 📎 button + thumbnail strip.
    // Files are read as data URIs (`data:image/...;base64,...`) and shipped
    // verbatim as MediaItem.source — `multimodal/inbound.py` accepts that
    // shape with a 10 MiB cap. Keeping it client-side with no upload step
    // means the gateway doesn't need an /upload route and we don't add a
    // new attack surface.
    const MAX_FILE_BYTES = 10 * 1024 * 1024;   // matches server-side cap
    let pendingMedia = [];                     // [{kind, source, mime_type, filename}]
    const attachInput = el("input", {
      type: "file",
      accept: "image/jpeg,image/png,image/gif,image/webp",
      multiple: true,
      style: "display:none",
    });
    const attachBtn = el("button", {
      type: "button",
      class: "btn btn-ghost",
      title: "Attach image(s)",
      onclick: () => attachInput.click(),
    }, "📎");
    const thumbs = el("div", { class: "chat-thumbs" });
    function renderThumbs() {
      thumbs.innerHTML = "";
      if (!pendingMedia.length) { thumbs.style.display = "none"; return; }
      thumbs.style.display = "flex";
      pendingMedia.forEach((m, idx) => {
        const t = el("div", { class: "chat-thumb" });
        t.append(
          el("img", { src: m.source, alt: m.filename || "" }),
          el("button", {
            type: "button",
            class: "chat-thumb__remove",
            title: "Remove",
            onclick: () => { pendingMedia.splice(idx, 1); renderThumbs(); },
          }, "×"),
        );
        thumbs.append(t);
      });
    }
    attachInput.addEventListener("change", async () => {
      const files = Array.from(attachInput.files || []);
      attachInput.value = "";
      for (const f of files) {
        if (f.size > MAX_FILE_BYTES) {
          Toast.error("file too large", `${f.name}: ${(f.size/1024/1024).toFixed(1)} MiB > 10 MiB`);
          continue;
        }
        if (!/^image\//.test(f.type)) {
          Toast.error("not an image", `${f.name}: ${f.type || "unknown"}`);
          continue;
        }
        const dataUri = await new Promise((res, rej) => {
          const fr = new FileReader();
          fr.onload = () => res(fr.result);
          fr.onerror = () => rej(fr.error);
          fr.readAsDataURL(f);
        }).catch((e) => { Toast.error("read failed", String(e)); return null; });
        if (!dataUri) continue;
        pendingMedia.push({
          kind: "photo",
          source: dataUri,
          mime_type: f.type || null,
          filename: f.name || null,
        });
      }
      renderThumbs();
    });

    compose.append(attachInput, attachBtn, textarea, sendBtn);
    right.insertBefore(thumbs, compose);
    renderThumbs();

    // 🔍 Debug-prompt — operator-only diagnostic that shows exactly
    // what the model will be told for a given query (recalled memories,
    // skill block, base playbook). Helps debug "agent doesn't remember"
    // by separating "recall didn't fire" from "model ignored recall".
    const debugBtn = el("button", {
      class: "btn btn-sm btn-ghost",
      title: "Show the assembled system prompt for the current agent",
      onclick: () => openDebugDialog(),
    }, "🔍 Debug prompt");

    async function openDebugDialog() {
      const seed = (textarea.value || "").trim();
      const query = seed || prompt(
        "Query to recall against (the assembled prompt depends on the user message):",
        "",
      );
      if (!query) return;
      let payload;
      try {
        payload = await Rpc.call("chat.debug_prompt", {
          agent_id: ChatState.agentId,
          query,
        });
      } catch (e) {
        Toast.error("debug_prompt failed", e.message);
        return;
      }
      if (payload && payload.ok === false) {
        Toast.error("debug_prompt error", payload.error || "(no detail)");
        return;
      }
      const dlg = el("dialog", { class: "debug-prompt-dialog" });
      const header = el("div", { class: "debug-prompt__header" },
        el("strong", {}, `model: ${payload.model_id} · agent: ${payload.agent_id}`),
        el("button", { class: "btn btn-sm btn-ghost", onclick: () => dlg.close() }, "✕"),
      );
      const stats = el("div", { class: "debug-prompt__stats" },
        `system=${payload.system_prompt_chars}c · base=${payload.base_prompt_chars}c · ` +
        `memory=${payload.memory_block_chars}c · skills=${payload.skills_block_chars}c · ` +
        `weak<${payload.memory_weak_threshold}`,
      );
      const hitsTbl = el("table", { class: "debug-prompt__hits" });
      hitsTbl.append(el("thead", {}, el("tr", {},
        el("th", {}, "score"), el("th", {}, "citation"), el("th", {}, "preview"),
      )));
      const tbody = el("tbody");
      for (const h of (payload.memory_hits || [])) {
        const row = el("tr", {});
        row.append(
          el("td", {}, h.score.toFixed(3)),
          el("td", {}, h.citation || `${h.path || "?"}:${h.start_line}-${h.end_line}`),
          el("td", {}, h.text_preview || ""),
        );
        tbody.append(row);
      }
      hitsTbl.append(tbody);
      const promptPre = el("pre", { class: "debug-prompt__pre" }, payload.system_prompt);
      const copyBtn = el("button", {
        class: "btn btn-sm",
        onclick: async () => {
          try {
            await navigator.clipboard.writeText(payload.system_prompt);
            Toast.success("copied", "system prompt → clipboard");
          } catch (e) { Toast.error("copy failed", e.message); }
        },
      }, "Copy prompt");
      dlg.append(header, stats,
        el("h4", {}, `Memory hits (${(payload.memory_hits || []).length})`), hitsTbl,
        el("h4", {}, "Assembled system prompt"), promptPre, copyBtn,
      );
      document.body.append(dlg);
      dlg.addEventListener("close", () => dlg.remove());
      dlg.showModal();
    }

    actions.append(newChatBtn, clearBtn, debugBtn);

    function labelled(name, input) {
      const wrap = el("div", { class: "field", style: "margin: 0; flex: 1;" });
      wrap.append(el("label", {}, name), input);
      return wrap;
    }

    let polling = null;
    let alive = true;

    function appendOptimistic(role, bodyHtml, opts = {}) {
      const wrap = el("div", { class: `chat-msg ${role} pending-optimistic` });
      wrap.append(el("div", { class: "role" }, role));
      const body = el("div", {
        class: "body",
        style: opts.muted ? "opacity:0.6; font-style:italic;" : "",
      });
      body.innerHTML = bodyHtml;
      wrap.append(body);
      if (opts.attachments) {
        wrap.append(el("div", { class: "tool-call" }, `📎 ${opts.attachments} attachment(s)`));
      }
      stream.append(wrap);
      stream.scrollTop = stream.scrollHeight;
      return wrap;
    }

    async function send() {
      const text = textarea.value.trim();
      const media = pendingMedia.slice();
      // A turn is valid if there's text OR at least one attachment.
      if ((!text && !media.length) || !ChatState.chatId) return;
      sendBtn.disabled = true;
      textarea.value = "";
      // Clear thumbs immediately so the user sees the send happen.
      pendingMedia = [];
      renderThumbs();
      // Stop the post-previous-turn polling cycle. If polling fires
      // its 2 s tick mid-flight against the new turn, `refresh()` runs
      // BEFORE PiAgent has persisted the new user message to
      // dashboard_history (active-memory + model dispatch can take
      // 5–20 s). renderStream() does `stream.innerHTML = ""` and
      // rebuilds from the server's stale view → the optimistic user
      // bubble gets wiped. The next poll tick (once the server-side
      // save has landed) re-paints it, which the operator perceives
      // as "message appears, disappears, reappears." Pausing here
      // and restarting after our own refresh below closes the race.
      stopPolling();
      // Optimistic render: the user's bubble + a "thinking…" placeholder
      // appear immediately so the user can see their message landed and
      // the agent is being asked. Both get replaced by `refresh()` once
      // chat.send returns (renderStream rebuilds the whole stream from
      // chat.history). On error we keep them and append an inline error
      // bubble so failure is visible without the user chasing a toast.
      const pendingUserBubble = text
        ? appendOptimistic("user", Markdown.render(text), { attachments: media.length })
        : null;
      const pendingBubble = appendOptimistic("assistant", "Thinking…", { muted: true });
      try {
        const result = await safeRpc("chat.send", {
          channel: ChatState.channel,
          account_id: ChatState.accountId,
          chat_id: ChatState.chatId,
          thread_id: ChatState.threadId || null,
          agent_id: ChatState.agentId || null,
          text,
          media,
        });
        if (result && result.status === "dropped") {
          // Real drop: no agent ran. Restore text + attachments so user can retry.
          const reason = result.reason || "no agent matched the channel";
          Toast.error("message dropped", reason, 6000);
          pendingBubble.querySelector(".body").textContent = `⚠ dropped — ${reason}`;
          pendingBubble.querySelector(".body").style.cssText = "color:#e15c5c;";
          textarea.value = text;
          pendingMedia = media;
          renderThumbs();
          return;
        }
        if (result && result.message_id === "local" && result.reason) {
          // Agent replied to history (chat.history poll renders it) but
          // wire delivery failed — informational only.
          Toast.info("delivery note", result.reason);
        }
        // chat.send succeeded → server has the user message + assistant
        // reply persisted. Drop the optimistic placeholders BEFORE refresh
        // so renderStream's carry-forward (which keeps unmatched
        // .pending-optimistic nodes through innerHTML="") doesn't leave
        // an orphaned "Thinking…" bubble — those would otherwise
        // accumulate one per turn and surface as the duplicate-thinking
        // bug operators see.
        if (pendingUserBubble) pendingUserBubble.remove();
        pendingBubble.remove();
        await refresh();
        startPolling();
      } catch (e) {
        // safeRpc already surfaced a toast; keep an inline error bubble so
        // it stays visible after the toast fades, and restore the textarea
        // so the user doesn't lose what they typed.
        pendingBubble.querySelector(".body").textContent = `⚠ ${e.message || e}`;
        pendingBubble.querySelector(".body").style.cssText = "color:#e15c5c;";
        textarea.value = text;
        pendingMedia = media;
        renderThumbs();
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

    // Start a fresh chat: pick a new chat-id, sync the chat-target
    // inputs, refresh the stream pane, focus the compose box. PiAgent
    // creates the matching ConversationHistory file lazily on first
    // chat.send so there's no setup RPC needed.
    async function startNewChat({ inputs }) {
      ChatState.newChat();
      if (inputs && inputs.chatId) inputs.chatId.value = ChatState.chatId;
      if (inputs && inputs.threadId) inputs.threadId.value = ChatState.threadId;
      updateChip();
      await refresh();
      try { await loadSessions(); } catch { /* sessions panel optional */ }
      const ta = document.querySelector(".chat-compose textarea");
      if (ta) ta.focus();
      Toast.info("New chat started", `chat_id = ${ChatState.chatId}`);
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
              updateChip();
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

    // Listen for "new chat" events triggered from outside the ChatView
    // (Ctrl+Shift+N global shortcut, command palette). The shortcut
    // already mutates ChatState; we just need to re-sync the inputs
    // and refresh. ChatView's render is re-run on hashchange so this
    // listener naturally lives until the next view swap; bindShortcuts
    // dispatches the event AFTER potentially navigating to chat, so
    // the freshly-rendered handler always sees it.
    const onExternalNewChat = () => {
      for (const f of fields) inputs[f].value = ChatState[f] ?? "";
      updateChip();
      reloadAgentChoices();
      refresh();
      const ta = document.querySelector(".chat-compose textarea");
      if (ta) ta.focus();
    };
    window.addEventListener("samp:new-chat", onExternalNewChat);

    function renderStream(messages) {
      // Preserve any optimistic bubbles the user appended via send()
      // that haven't yet shown up in the server-returned `messages`.
      // Without this, a polling refresh() that lands BEFORE PiAgent
      // has flushed the user's new message to dashboard_history wipes
      // the optimistic bubble; the operator sees a flicker (visible
      // → disappear → reappear-on-next-poll). We only carry forward
      // .pending-optimistic nodes whose textual body isn't already
      // represented in the server response — once the server catches
      // up the optimistic copy is dropped to avoid duplicate rendering.
      const carry = Array.from(stream.querySelectorAll(".pending-optimistic"));
      const serverTexts = new Set(
        (messages || [])
          .map((m) => (typeof m.content === "string" ? m.content.trim() : ""))
          .filter(Boolean)
      );
      stream.innerHTML = "";
      for (const m of messages) {
        if (m.role === "system") continue; // hide system prompt from chat UI
        const wrap = el("div", { class: `chat-msg ${m.role || "system"}` });
        wrap.append(el("div", { class: "role" }, m.role || "?"));
        // Render image blocks inline. Three shapes seen in history:
        //  - OpenAI shim:    {type:"image_url", image_url:{url:"data:..."}}
        //  - Anthropic:      {type:"image",     source:{type:"base64", media_type, data}}
        //  - pi ImageContent: {type:"image",    image:{url:"data:..."}}
        const imgs = imagesIn(m.content);
        if (imgs.length) {
          const row = el("div", { class: "chat-msg__images" });
          for (const url of imgs) {
            row.append(el("img", {
              class: "chat-msg__img",
              src: url,
              alt: "image",
              onclick: () => window.open(url, "_blank", "noopener"),
            }));
          }
          wrap.append(row);
        }
        const body = el("div", { class: "body" });
        const text = textOf(m.content);
        if (m.role === "assistant" || m.role === "user") {
          body.innerHTML = Markdown.render(text);
        } else {
          body.textContent = text;
        }
        if (text) wrap.append(body);
        // PiAgent persists tool calls with timing as `m.tool_calls`:
        //   [{ id, name, args, started_at, ended_at, status, output_preview }]
        // Render each as an expandable card with tool name + elapsed ms.
        if (Array.isArray(m.tool_calls) && m.tool_calls.length) {
          for (const t of m.tool_calls) {
            wrap.append(renderToolCallCard(t));
          }
        }
        // Older schema (LocalAgent / inline content blocks) keeps using
        // the simple summary form below.
        if (Array.isArray(m.content)) {
          for (const b of m.content) {
            if (b.type === "tool_use") {
              wrap.append(el("div", { class: "tool-call" }, `🔧 ${b.name}(${JSON.stringify(b.input)})`));
            }
          }
        }
        stream.append(wrap);
      }
      // Re-attach optimistic bubbles whose text isn't represented yet
      // in the server's view. They'll be dropped on a subsequent
      // render once the server-side save catches up.
      for (const node of carry) {
        const body = node.querySelector(".body");
        const txt = body ? body.textContent.trim() : "";
        if (!txt) continue;
        if (serverTexts.has(txt)) continue;
        stream.append(node);
      }
      stream.scrollTop = stream.scrollHeight;
    }

    function imagesIn(content) {
      if (!Array.isArray(content)) return [];
      const out = [];
      for (const b of content) {
        if (!b || typeof b !== "object") continue;
        if (b.type === "image_url" && b.image_url && b.image_url.url) {
          out.push(b.image_url.url);
        } else if (b.type === "image" && b.source && b.source.type === "base64" &&
                   b.source.data && b.source.media_type) {
          out.push(`data:${b.source.media_type};base64,${b.source.data}`);
        } else if (b.type === "image" && b.image && b.image.url) {
          out.push(b.image.url);
        }
      }
      return out;
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

    // Render an expandable tool-call card for PiAgent's tool_calls schema.
    // Shape: { id, name, args, started_at, ended_at, status, output_preview }
    // The summary line shows tool name + elapsed (ms or s) + status icon;
    // clicking the row toggles a details panel with the args JSON and the
    // output preview. Mirrors openclaw's tool-cards.ts visual idiom.
    function renderToolCallCard(t) {
      const elapsedMs = (typeof t.started_at === "number" && typeof t.ended_at === "number")
        ? Math.max(0, (t.ended_at - t.started_at) * 1000) : null;
      const elapsedTxt = elapsedMs == null
        ? "—"
        : (elapsedMs >= 1000 ? `${(elapsedMs / 1000).toFixed(2)}s` : `${Math.round(elapsedMs)}ms`);
      const statusIcon = t.status === "error" ? "⚠" : t.status === "ok" ? "✓" : "•";
      const card = el("details", { class: `tool-call-card status-${t.status || "unknown"}` });
      const summary = el("summary", { class: "tool-call-card__summary" });
      summary.append(
        el("span", { class: "tool-call-card__icon" }, "🔧"),
        el("span", { class: "tool-call-card__name" }, t.name || "(unnamed tool)"),
        el("span", { class: "tool-call-card__elapsed" }, elapsedTxt),
        el("span", { class: "tool-call-card__status" }, statusIcon),
      );
      card.append(summary);
      const body = el("div", { class: "tool-call-card__body" });
      if (t.args !== undefined) {
        body.append(
          el("div", { class: "tool-call-card__label" }, "args"),
          el("pre", { class: "tool-call-card__pre" }, JSON.stringify(t.args, null, 2)),
        );
      }
      if (t.output_preview) {
        body.append(
          el("div", { class: "tool-call-card__label" }, "output preview"),
          el("pre", { class: "tool-call-card__pre" }, t.output_preview),
        );
      }
      card.append(body);
      return card;
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

    return () => {
      alive = false;
      stopPolling();
      window.removeEventListener("samp:new-chat", onExternalNewChat);
    };
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

    // openclaw-style: provider IS the model's catalog provider id, not
    // an agent-class selector. The model field auto-suggests a sensible
    // default per provider; users can type anything (custom vLLM models
    // etc. work via the factory's synthetic-registry path).
    // On-host catalog only as of 2026-04-29.
    const PROVIDER_MODEL_HINTS = {
      ollama: "qwen3.5:9b",
      "llamacpp-direct": "local-gguf (uses $OXENCLAW_LLAMACPP_GGUF)",
      llamacpp: "qwen3.5:9b",
      vllm: "meta-llama/Llama-3.1-8B-Instruct",
      lmstudio: "qwen3.5:9b",
      echo: "(no model — echoes input)",
    };

    function renderCreateForm(providers) {
      const card = el("div", { class: "card", style: "margin-top:12px" });
      card.append(el("h3", { class: "card-title" }, "Create new"));

      const idIn = el("input", { type: "text", placeholder: "agent id" });
      const provSel = el("select", {});
      const sortedProviders = [...providers].sort();
      for (const p of sortedProviders) provSel.append(el("option", { value: p }, p));
      const modelIn = el("input", {
        type: "text",
        placeholder: PROVIDER_MODEL_HINTS[provSel.value] || "model id (catalog or custom)",
      });
      provSel.addEventListener("change", () => {
        const hint = PROVIDER_MODEL_HINTS[provSel.value];
        modelIn.placeholder = hint || "model id (catalog or custom)";
      });
      const baseIn = el("input", {
        type: "text",
        placeholder: "base_url (override transport URL — Ollama/vLLM/etc.)",
      });
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
            "Add Slack credentials at ~/.oxenclaw/credentials/slack/<account>.json " +
            "and declare the account in config.yaml under channels.slack.accounts, then call config.reload."),
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

// CronViewState — all ephemeral UI state for the Cron tab.
// Persisted for the tab's lifetime only (reset on each mount).
const CronViewState = {
  // job list
  allJobs: [],           // raw cron.list result
  jobLastRun: {},        // { [job_id]: CronRunEntry | null }
  // filter / sort
  query: "",
  enabledFilter: "all",  // "all" | "enabled" | "disabled"
  scheduleKindFilter: "all", // "all" | "cron" | "every" | "at"
  lastStatusFilter: "all",   // "all" | "ok" | "error" | "skipped" | "never"
  sortBy: "name",        // "name" | "schedule" | "next_run" | "last_run"
  sortDir: "asc",        // "asc" | "desc"
  // top-level tab
  activeTab: "jobs",     // "jobs" | "runs"
  // edit modal
  modalOpen: false,
  modalMode: "new",      // "new" | "edit" | "clone"
  editingJobId: null,
  // full-edit form draft
  draft: {},
  draftErrors: {},
  // run-log
  runsJobId: null,       // null = all jobs
  runs: [],
  runsTotal: 0,
  runsHasMore: false,
  runsOffset: 0,
  runsLimit: 50,
  runsStatusFilter: [],  // [] = all; subset of ["ok","error","skipped","running"]
  runsDeliveryFilter: [],// [] = all; subset of ["delivered","failed","skipped","not-set"]
  runsQuery: "",
  runsSortDir: "desc",
  runsScope: "all",      // "all" | job_id
  reset() {
    Object.assign(this, {
      allJobs: [], jobLastRun: {}, query: "", enabledFilter: "all",
      scheduleKindFilter: "all", lastStatusFilter: "all",
      sortBy: "name", sortDir: "asc", activeTab: "jobs",
      modalOpen: false, modalMode: "new", editingJobId: null,
      draft: {}, draftErrors: {},
      runsJobId: null, runs: [], runsTotal: 0, runsHasMore: false,
      runsOffset: 0, runsLimit: 50, runsStatusFilter: [], runsDeliveryFilter: [],
      runsQuery: "", runsSortDir: "desc", runsScope: "all",
    });
  },
};

// Detect the kind of a schedule string.
function cronScheduleKind(schedule) {
  if (!schedule) return "cron";
  if (/^every\s/i.test(schedule)) return "every";
  if (/^at\s/i.test(schedule) || /^\d{4}-\d{2}-\d{2}T/.test(schedule)) return "at";
  return "cron";
}

// ─── Schedule builder helpers ───────────────────────────────────────────
// A small DSL on top of 5-field cron that covers the patterns most users
// actually want: daily at a time, weekly on selected days, monthly on a
// given day, hourly at a minute, every-N-minutes. The builder produces
// a cron expression; parseBuilderCron tries to recover the builder state
// from an expression so we can re-open the builder for existing jobs.

const DOW_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function pad2(n) { return String(n).padStart(2, "0"); }

function buildCron(b) {
  const m = Math.max(0, Math.min(59, parseInt(b.minute, 10) || 0));
  const h = Math.max(0, Math.min(23, parseInt(b.hour, 10) || 0));
  const dom = Math.max(1, Math.min(31, parseInt(b.dayOfMonth, 10) || 1));
  const everyN = Math.max(1, Math.min(59, parseInt(b.everyMinutes, 10) || 5));

  if (b.frequency === "daily")   return `${m} ${h} * * *`;
  if (b.frequency === "weekly")  {
    const days = (b.daysOfWeek && b.daysOfWeek.length) ? b.daysOfWeek.slice().sort((a,b)=>a-b).join(",") : "1";
    return `${m} ${h} * * ${days}`;
  }
  if (b.frequency === "monthly") return `${m} ${h} ${dom} * *`;
  if (b.frequency === "hourly")  return `${m} * * * *`;
  if (b.frequency === "minutes") return `*/${everyN} * * * *`;
  return `${m} ${h} * * *`;
}

// Try to recover a builder state from a cron expression. Returns null
// when the expression doesn't fit one of the supported patterns; callers
// fall back to raw cron mode in that case.
function parseBuilderCron(expr) {
  if (!expr) return null;
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [mn, hr, dom, mo, dw] = parts;

  // Reject anything outside the supported subset.
  if (mo !== "*") return null;

  // every-N-minutes: */N * * * *
  if (/^\*\/\d+$/.test(mn) && hr === "*" && dom === "*" && dw === "*") {
    return { frequency: "minutes", everyMinutes: Number(mn.slice(2)), hour: 0, minute: 0, daysOfWeek: [], dayOfMonth: 1 };
  }
  if (!/^\d+$/.test(mn)) return null;
  const minute = Number(mn);

  // hourly: M * * * *
  if (hr === "*" && dom === "*" && dw === "*") {
    return { frequency: "hourly", everyMinutes: 5, hour: 0, minute, daysOfWeek: [], dayOfMonth: 1 };
  }
  if (!/^\d+$/.test(hr)) return null;
  const hour = Number(hr);

  // monthly: M H D * *
  if (/^\d+$/.test(dom) && dw === "*") {
    return { frequency: "monthly", everyMinutes: 5, hour, minute, daysOfWeek: [], dayOfMonth: Number(dom) };
  }
  // daily / weekly: M H * * dw
  if (dom === "*") {
    if (dw === "*") {
      return { frequency: "daily", everyMinutes: 5, hour, minute, daysOfWeek: [], dayOfMonth: 1 };
    }
    // Accept "1-5" → [1,2,3,4,5] and "1,3,5" → [1,3,5]; reject "*/N"
    let days = [];
    if (/^\d+(-\d+)?(,\d+(-\d+)?)*$/.test(dw)) {
      for (const part of dw.split(",")) {
        if (part.includes("-")) {
          const [a, b] = part.split("-").map(Number);
          for (let i = a; i <= b; i++) days.push(i);
        } else {
          days.push(Number(part));
        }
      }
      days = days.filter((d) => d >= 0 && d <= 6);
      return { frequency: "weekly", everyMinutes: 5, hour, minute, daysOfWeek: days, dayOfMonth: 1 };
    }
  }
  return null;
}

// Human-readable summary of a builder state.
function describeBuilder(b) {
  const t = `${pad2(b.hour)}:${pad2(b.minute)}`;
  if (b.frequency === "daily")   return `Every day at ${t}`;
  if (b.frequency === "weekly")  {
    const days = (b.daysOfWeek||[]).slice().sort((a,b)=>a-b).map(d => DOW_LABELS[d]);
    return days.length ? `Every ${days.join(", ")} at ${t}` : `Pick at least one day`;
  }
  if (b.frequency === "monthly") return `Day ${b.dayOfMonth} of every month at ${t}`;
  if (b.frequency === "hourly")  return `Every hour at :${pad2(b.minute)}`;
  if (b.frequency === "minutes") return `Every ${b.everyMinutes} minute${b.everyMinutes === 1 ? "" : "s"}`;
  return "";
}

function defaultBuilder() {
  return { frequency: "daily", hour: 9, minute: 0, daysOfWeek: [1,2,3,4,5], dayOfMonth: 1, everyMinutes: 5 };
}

// Render the schedule builder into a container. `state` is the mutable
// builder state object (stored on whatever larger draft owns it). Calls
// `onChange()` after every interaction so the parent can re-render
// previews/cron strings.
function renderScheduleBuilder(container, state, onChange) {
  container.innerHTML = "";
  container.classList.add("cron-builder");

  // Frequency segmented control
  const freqRow = el("div", { class: "cron-builder__freq" });
  const FREQS = [
    ["daily",   "Daily"],
    ["weekly",  "Weekly"],
    ["monthly", "Monthly"],
    ["hourly",  "Hourly"],
    ["minutes", "Every N min"],
  ];
  for (const [v, l] of FREQS) {
    const btn = el("button", {
      type: "button",
      class: "cron-builder__freq-btn" + (state.frequency === v ? " active" : ""),
      onclick: () => { state.frequency = v; renderScheduleBuilder(container, state, onChange); onChange(); },
    }, l);
    freqRow.append(btn);
  }
  container.append(freqRow);

  const detail = el("div", { class: "cron-builder__detail" });
  container.append(detail);

  if (state.frequency === "daily" || state.frequency === "weekly" || state.frequency === "monthly") {
    // Time picker (HH:MM) — single input, native control, easy to use.
    const timeIn = el("input", {
      type: "time",
      class: "cron-builder__time",
      value: `${pad2(state.hour)}:${pad2(state.minute)}`,
    });
    timeIn.addEventListener("change", () => {
      const [h, m] = timeIn.value.split(":").map(Number);
      state.hour = isNaN(h) ? 0 : h;
      state.minute = isNaN(m) ? 0 : m;
      onChange();
    });
    detail.append(el("div", { class: "field cron-builder__field" },
      el("label", {}, "Time"), timeIn));
  }

  if (state.frequency === "weekly") {
    const chips = el("div", { class: "cron-builder__chips" });
    for (let i = 0; i < 7; i++) {
      const active = state.daysOfWeek.includes(i);
      const chip = el("button", {
        type: "button",
        class: "cron-builder__chip" + (active ? " active" : ""),
        onclick: () => {
          if (state.daysOfWeek.includes(i)) state.daysOfWeek = state.daysOfWeek.filter((d) => d !== i);
          else state.daysOfWeek = state.daysOfWeek.concat(i);
          renderScheduleBuilder(container, state, onChange); onChange();
        },
      }, DOW_LABELS[i]);
      chips.append(chip);
    }
    const presetRow = el("div", { class: "cron-builder__chip-presets" });
    presetRow.append(
      el("button", { type: "button", class: "btn btn-sm btn-ghost",
        onclick: () => { state.daysOfWeek = [1,2,3,4,5]; renderScheduleBuilder(container, state, onChange); onChange(); }
      }, "Weekdays"),
      el("button", { type: "button", class: "btn btn-sm btn-ghost",
        onclick: () => { state.daysOfWeek = [0,6]; renderScheduleBuilder(container, state, onChange); onChange(); }
      }, "Weekends"),
      el("button", { type: "button", class: "btn btn-sm btn-ghost",
        onclick: () => { state.daysOfWeek = [0,1,2,3,4,5,6]; renderScheduleBuilder(container, state, onChange); onChange(); }
      }, "Every day"),
    );
    detail.append(el("div", { class: "field cron-builder__field" },
      el("label", {}, "Days of week"), chips, presetRow));
  }

  if (state.frequency === "monthly") {
    const domIn = el("input", {
      type: "number", min: "1", max: "31", value: String(state.dayOfMonth),
      class: "cron-builder__dom",
    });
    domIn.addEventListener("input", () => {
      const v = parseInt(domIn.value, 10);
      state.dayOfMonth = isNaN(v) ? 1 : Math.max(1, Math.min(31, v));
      onChange();
    });
    detail.append(el("div", { class: "field cron-builder__field" },
      el("label", {}, "Day of month (1–31)"), domIn));
  }

  if (state.frequency === "hourly") {
    const minIn = el("input", {
      type: "number", min: "0", max: "59", value: String(state.minute),
      class: "cron-builder__dom",
    });
    minIn.addEventListener("input", () => {
      const v = parseInt(minIn.value, 10);
      state.minute = isNaN(v) ? 0 : Math.max(0, Math.min(59, v));
      onChange();
    });
    detail.append(el("div", { class: "field cron-builder__field" },
      el("label", {}, "Minute offset (0–59)"), minIn));
  }

  if (state.frequency === "minutes") {
    const nIn = el("input", {
      type: "number", min: "1", max: "59", value: String(state.everyMinutes),
      class: "cron-builder__dom",
    });
    nIn.addEventListener("input", () => {
      const v = parseInt(nIn.value, 10);
      state.everyMinutes = isNaN(v) ? 5 : Math.max(1, Math.min(59, v));
      onChange();
    });
    detail.append(el("div", { class: "field cron-builder__field" },
      el("label", {}, "Every N minutes"), nIn));
  }
}

const CronView = {
  title: "Cron",
  async render(root, actions) {
    CronViewState.reset();

    // ── Topbar buttons ──────────────────────────────────────────────────
    const refreshBtn = el("button", { class: "btn btn-ghost btn-sm", onclick: () => loadJobs() }, "↻ Refresh");
    const newJobBtn = el("button", {
      class: "btn btn-primary btn-sm",
      onclick: () => {
        quickCard.scrollIntoView({ behavior: "smooth", block: "start" });
        const ta = quickCard.querySelector("textarea");
        if (ta) ta.focus();
      },
    }, "+ New job");
    actions.append(newJobBtn, refreshBtn);

    // ── Tab bar: Jobs / Run log ─────────────────────────────────────────
    const tabBar = el("div", { class: "cron-tab-bar" });
    const tabJobs = el("button", { class: "cron-tab active", onclick: () => switchTab("jobs") }, "Jobs");
    const tabRuns = el("button", { class: "cron-tab", onclick: () => switchTab("runs") }, "Run log");
    tabBar.append(tabJobs, tabRuns);
    root.append(tabBar);

    function switchTab(tab) {
      CronViewState.activeTab = tab;
      tabJobs.classList.toggle("active", tab === "jobs");
      tabRuns.classList.toggle("active", tab === "runs");
      jobsPane.style.display = tab === "jobs" ? "" : "none";
      runsPane.style.display = tab === "runs" ? "" : "none";
      if (tab === "runs") loadRuns();
    }

    // ── Jobs pane ───────────────────────────────────────────────────────
    const jobsPane = el("div");
    root.append(jobsPane);

    // Quick-add wizard
    const quickCard = el("div", { class: "card cron-quick" });
    jobsPane.append(quickCard);

    quickCard.append(el("h3", { class: "card-title" }, "+ New job"));
    quickCard.append(el("div", { class: "card-meta" },
      "Build any schedule below — daily at a chosen time, weekly on selected " +
      "days, monthly on a date, or every N minutes. Optionally limit it to a " +
      "date range."
    ));

    // Templates that pre-fill the builder. One click = sensible default,
    // user can still tweak time/days afterwards.
    const TEMPLATES = [
      { label: "Daily 09:00",          state: { frequency: "daily",   hour: 9,  minute: 0,  daysOfWeek: [], dayOfMonth: 1, everyMinutes: 5 } },
      { label: "Weekdays 09:00",       state: { frequency: "weekly",  hour: 9,  minute: 0,  daysOfWeek: [1,2,3,4,5], dayOfMonth: 1, everyMinutes: 5 } },
      { label: "Weekly Mon 09:00",     state: { frequency: "weekly",  hour: 9,  minute: 0,  daysOfWeek: [1], dayOfMonth: 1, everyMinutes: 5 } },
      { label: "Monthly 1st 08:00",    state: { frequency: "monthly", hour: 8,  minute: 0,  daysOfWeek: [], dayOfMonth: 1, everyMinutes: 5 } },
      { label: "Hourly",               state: { frequency: "hourly",  hour: 0,  minute: 0,  daysOfWeek: [], dayOfMonth: 1, everyMinutes: 5 } },
      { label: "Every 5 min",          state: { frequency: "minutes", hour: 0,  minute: 0,  daysOfWeek: [], dayOfMonth: 1, everyMinutes: 5 } },
    ];
    const tplRow = el("div", { class: "cron-builder__templates" });
    for (const t of TEMPLATES) {
      tplRow.append(el("button", {
        type: "button", class: "btn btn-sm btn-ghost",
        onclick: () => { Object.assign(quickBuilder, JSON.parse(JSON.stringify(t.state))); refresh(); },
      }, t.label));
    }
    quickCard.append(tplRow);

    const quickBuilder = defaultBuilder();
    const builderHost = el("div", {});
    quickCard.append(builderHost);

    const previewWrap = el("div", { class: "cron-builder__preview" });
    const previewHuman = el("div", { class: "cron-builder__preview-human" });
    const previewCron = el("code", { class: "cron-builder__preview-cron" });
    previewWrap.append(previewHuman, previewCron);
    quickCard.append(previewWrap);

    // Optional active period (collapsed by default).
    const dateRow = el("div", { class: "cron-builder__date-range" });
    const startIn = el("input", { type: "date", placeholder: "" });
    const endIn = el("input", { type: "date", placeholder: "" });
    dateRow.append(
      el("div", { class: "field" }, el("label", {}, "Start (optional)"), startIn),
      el("div", { class: "field" }, el("label", {}, "End (optional)"), endIn),
    );
    const datesToggle = el("button", {
      type: "button", class: "btn btn-sm btn-ghost cron-builder__dates-toggle",
      onclick: () => {
        const open = dateRow.style.display !== "none";
        dateRow.style.display = open ? "none" : "";
        datesToggle.textContent = open ? "+ Active period (optional)" : "− Active period";
      },
    }, "+ Active period (optional)");
    dateRow.style.display = "none";
    quickCard.append(datesToggle, dateRow);

    function refresh() {
      renderScheduleBuilder(builderHost, quickBuilder, refresh);
      previewHuman.textContent = describeBuilder(quickBuilder);
      previewCron.textContent = buildCron(quickBuilder);
    }
    refresh();

    const quickPrompt = el("textarea", {
      class: "cron-quick__prompt",
      placeholder: "What should the agent do? e.g. 'Summarise overnight Slack DMs'",
    });
    const quickAgent = el("input", { type: "text", value: ChatState.agentId || "assistant", placeholder: "agent_id" });
    const quickChatId = el("input", { type: "text", value: ChatState.chatId || "demo", placeholder: "chat_id" });
    const quickRow = el("div", { class: "row" },
      el("div", { class: "field" }, el("label", {}, "agent_id"), quickAgent),
      el("div", { class: "field" }, el("label", {}, "chat_id (where to deliver)"), quickChatId),
    );
    const quickCreate = el("button", { class: "btn btn-primary cron-quick__submit" }, "Create");
    quickCreate.onclick = async () => {
      const prompt = quickPrompt.value.trim();
      const agent_id = quickAgent.value.trim() || "assistant";
      const chat_id = quickChatId.value.trim() || "demo";
      if (!prompt) { Toast.warn("Enter what the agent should do."); quickPrompt.focus(); return; }
      if (quickBuilder.frequency === "weekly" && !quickBuilder.daysOfWeek.length) {
        Toast.warn("Pick at least one day of the week."); return;
      }
      const cron = buildCron(quickBuilder);
      const params = {
        schedule: cron,
        agent_id, channel: "dashboard", account_id: "main", chat_id,
        prompt,
      };
      if (startIn.value) params.start_date = startIn.value;
      if (endIn.value)   params.end_date = endIn.value;
      try {
        const res = await Rpc.call("cron.create", params);
        Toast.success(`created ${res.id.slice(0, 8)}`, `${describeBuilder(quickBuilder)} · ${cron}`);
        quickPrompt.value = "";
        await loadJobs();
      } catch (e) {
        Toast.error("create failed", e.message);
      }
    };
    const advLink = el("button", {
      type: "button",
      class: "btn btn-sm btn-ghost cron-quick__advanced",
      onclick: () => openModal("new", null),
    }, "Advanced… (raw cron / every-N / one-shot datetime)");
    quickCard.append(quickPrompt, quickRow, quickCreate, advLink);

    // ── Filter + sort bar ───────────────────────────────────────────────
    const filterBar = el("div", { class: "cron-filter-bar" });
    jobsPane.append(filterBar);

    const searchInput = el("input", {
      type: "search",
      class: "cron-filter-bar__search",
      placeholder: "Search jobs…",
    });
    searchInput.addEventListener("input", () => { CronViewState.query = searchInput.value; renderJobList(); });

    const enabledSel = el("select", { class: "cron-filter-bar__sel" });
    for (const [v, l] of [["all","All"], ["enabled","Enabled"], ["disabled","Disabled"]]) {
      enabledSel.append(el("option", { value: v }, l));
    }
    enabledSel.addEventListener("change", () => { CronViewState.enabledFilter = enabledSel.value; renderJobList(); });

    const kindSel = el("select", { class: "cron-filter-bar__sel" });
    for (const [v, l] of [["all","All kinds"], ["cron","cron"], ["every","every"], ["at","at"]]) {
      kindSel.append(el("option", { value: v }, l));
    }
    kindSel.addEventListener("change", () => { CronViewState.scheduleKindFilter = kindSel.value; renderJobList(); });

    const statusSel = el("select", { class: "cron-filter-bar__sel" });
    for (const [v, l] of [["all","Any status"], ["ok","OK"], ["error","Error"], ["skipped","Skipped"], ["never","Never run"]]) {
      statusSel.append(el("option", { value: v }, l));
    }
    statusSel.addEventListener("change", () => { CronViewState.lastStatusFilter = statusSel.value; renderJobList(); });

    const sortBySel = el("select", { class: "cron-filter-bar__sel" });
    for (const [v, l] of [["name","Sort: Name"], ["schedule","Sort: Schedule"], ["next_run","Sort: Next run"], ["last_run","Sort: Last run"]]) {
      sortBySel.append(el("option", { value: v }, l));
    }
    sortBySel.addEventListener("change", () => { CronViewState.sortBy = sortBySel.value; renderJobList(); });

    const sortDirBtn = el("button", { class: "btn btn-sm cron-filter-bar__dir", onclick: () => {
      CronViewState.sortDir = CronViewState.sortDir === "asc" ? "desc" : "asc";
      sortDirBtn.textContent = CronViewState.sortDir === "asc" ? "▲" : "▼";
      renderJobList();
    } }, "▲");

    const resetBtn = el("button", { class: "btn btn-sm btn-ghost", onclick: () => {
      CronViewState.query = "";
      CronViewState.enabledFilter = "all";
      CronViewState.scheduleKindFilter = "all";
      CronViewState.lastStatusFilter = "all";
      CronViewState.sortBy = "name";
      CronViewState.sortDir = "asc";
      searchInput.value = "";
      enabledSel.value = "all";
      kindSel.value = "all";
      statusSel.value = "all";
      sortBySel.value = "name";
      sortDirBtn.textContent = "▲";
      renderJobList();
    } }, "Reset");

    filterBar.append(searchInput, enabledSel, kindSel, statusSel, sortBySel, sortDirBtn, resetBtn);

    // ── Job list container ──────────────────────────────────────────────
    const jobList = el("div", { class: "cron-job-list" });
    jobsPane.append(jobList);

    // ── Runs pane ───────────────────────────────────────────────────────
    const runsPane = el("div", { style: "display:none" });
    root.append(runsPane);

    const runsFilterBar = el("div", { class: "cron-filter-bar" });
    const runsScopeEl = el("select", { class: "cron-filter-bar__sel" });
    runsScopeEl.append(el("option", { value: "all" }, "All jobs"));
    runsScopeEl.addEventListener("change", () => { CronViewState.runsScope = runsScopeEl.value; CronViewState.runsOffset = 0; loadRuns(); });

    const runsSearchEl = el("input", {
      type: "search",
      class: "cron-filter-bar__search",
      placeholder: "Search runs…",
    });
    runsSearchEl.addEventListener("input", () => { CronViewState.runsQuery = runsSearchEl.value; CronViewState.runsOffset = 0; loadRuns(); });

    const runsSortDirBtn = el("button", { class: "btn btn-sm cron-filter-bar__dir", onclick: () => {
      CronViewState.runsSortDir = CronViewState.runsSortDir === "asc" ? "desc" : "asc";
      runsSortDirBtn.textContent = CronViewState.runsSortDir === "asc" ? "▲" : "▼";
      CronViewState.runsOffset = 0;
      loadRuns();
    } }, "▼");

    const runsRefreshBtn = el("button", { class: "btn btn-sm btn-ghost", onclick: () => { CronViewState.runsOffset = 0; loadRuns(); } }, "↻");

    runsFilterBar.append(runsScopeEl, runsSearchEl, runsSortDirBtn, runsRefreshBtn);
    runsPane.append(runsFilterBar);

    const runsList = el("div", { class: "cron-runs-list" });
    runsPane.append(runsList);

    const runsLoadMoreBtn = el("button", {
      class: "btn btn-sm",
      style: "display:none; margin-top:8px",
      onclick: () => loadMoreRuns(),
    }, "Load more");
    runsPane.append(runsLoadMoreBtn);

    // ── Edit modal (full-edit form) ─────────────────────────────────────
    const modalOverlay = el("div", { class: "cron-modal-overlay", style: "display:none", onclick: (e) => { if (e.target === modalOverlay) closeModal(); } });
    const modalCard = el("div", { class: "cron-modal card" });
    modalOverlay.append(modalCard);
    document.body.append(modalOverlay);

    function closeModal() {
      modalOverlay.style.display = "none";
      CronViewState.modalOpen = false;
    }

    // Esc closes the modal
    const escHandler = (e) => { if (e.key === "Escape" && CronViewState.modalOpen) closeModal(); };
    document.addEventListener("keydown", escHandler);

    function openModal(mode, job) {
      CronViewState.modalMode = mode;
      CronViewState.editingJobId = job ? job.id : null;
      CronViewState.draftErrors = {};
      if (mode === "new") {
        CronViewState.draft = {
          name: "", description: "", enabled: true,
          agent_id: ChatState.agentId || "assistant",
          scheduleKind: "builder", cronExpr: "", everyAmount: "", everyUnit: "hours",
          scheduleAt: "", timezone: "", builder: defaultBuilder(),
          start_date: "", end_date: "",
          chat_id: ChatState.chatId || "", channel: "dashboard",
          account_id: "main", thread_id: "", prompt: "",
          model: "",
        };
      } else if (mode === "edit" && job) {
        const parsedKind = cronScheduleKind(job.schedule);
        const parsedBuilder = parsedKind === "cron" ? parseBuilderCron(job.schedule) : null;
        CronViewState.draft = {
          name: job.name || "", description: job.description || "",
          enabled: job.enabled !== false,
          agent_id: job.agent_id || "",
          scheduleKind: parsedBuilder ? "builder" : parsedKind,
          cronExpr: job.schedule || "", everyAmount: "", everyUnit: "hours",
          scheduleAt: "", timezone: job.timezone || "",
          builder: parsedBuilder || defaultBuilder(),
          start_date: job.start_date || "", end_date: job.end_date || "",
          chat_id: job.chat_id || "", channel: job.channel || "dashboard",
          account_id: job.account_id || "main", thread_id: job.thread_id || "",
          prompt: job.prompt || "", model: job.model || "",
        };
      } else if (mode === "clone" && job) {
        const parsedKind = cronScheduleKind(job.schedule);
        const parsedBuilder = parsedKind === "cron" ? parseBuilderCron(job.schedule) : null;
        CronViewState.draft = {
          name: (job.name || "") + " (copy)", description: job.description || "",
          enabled: job.enabled !== false,
          agent_id: job.agent_id || "",
          scheduleKind: parsedBuilder ? "builder" : parsedKind,
          cronExpr: job.schedule || "", everyAmount: "", everyUnit: "hours",
          scheduleAt: "", timezone: job.timezone || "",
          builder: parsedBuilder || defaultBuilder(),
          start_date: job.start_date || "", end_date: job.end_date || "",
          chat_id: job.chat_id || "", channel: job.channel || "dashboard",
          account_id: job.account_id || "main", thread_id: job.thread_id || "",
          prompt: job.prompt || "", model: job.model || "",
        };
        CronViewState.editingJobId = null; // will create
      }
      CronViewState.modalOpen = true;
      renderModal();
      modalOverlay.style.display = "flex";
    }

    function validateDraft(draft) {
      const errors = {};
      if (!draft.prompt.trim()) errors.prompt = "Prompt is required";
      if (draft.scheduleKind === "cron" && !draft.cronExpr.trim()) errors.cronExpr = "Cron expression is required";
      if (draft.scheduleKind === "every" && !draft.everyAmount.trim()) errors.everyAmount = "Amount is required";
      if (draft.scheduleKind === "at" && !draft.scheduleAt.trim()) errors.scheduleAt = "Run-at time is required";
      if (draft.scheduleKind === "builder" && draft.builder.frequency === "weekly" && !draft.builder.daysOfWeek.length) {
        errors.builder = "Pick at least one day of the week";
      }
      if (draft.start_date && draft.end_date && draft.start_date > draft.end_date) {
        errors.end_date = "End must be on or after start";
      }
      if (!draft.chat_id.trim()) errors.chat_id = "chat_id is required";
      return errors;
    }

    function renderModal() {
      modalCard.innerHTML = "";
      const d = CronViewState.draft;
      const isEdit = CronViewState.modalMode === "edit";
      const titleText = CronViewState.modalMode === "edit" ? "Edit job" :
                        CronViewState.modalMode === "clone" ? "Clone job" : "New job";

      // Header
      const header = el("div", { class: "cron-modal__header" });
      header.append(
        el("h3", { class: "cron-modal__title" }, titleText),
        el("button", { class: "btn btn-sm btn-ghost cron-modal__close", onclick: closeModal }, "✕"),
      );
      modalCard.append(header);

      // Helper: labelled field with optional aria-invalid
      function mfield(key, labelText, input) {
        const err = CronViewState.draftErrors[key];
        if (err) input.setAttribute("aria-invalid", "true");
        else input.removeAttribute("aria-invalid");
        const wrap = el("div", { class: "field cron-modal__field" + (err ? " has-error" : "") });
        wrap.append(el("label", {}, labelText), input);
        if (err) wrap.append(el("div", { class: "cron-modal__field-error" }, err));
        return wrap;
      }

      // Section: Basics
      const secBasics = el("div", { class: "cron-modal__section" });
      secBasics.append(el("div", { class: "cron-modal__section-title" }, "Basics"));
      const nameIn = el("input", { type: "text", value: d.name, placeholder: "e.g. Morning digest", id: "cme-name" });
      nameIn.addEventListener("input", () => { CronViewState.draft.name = nameIn.value; });
      const descIn = el("textarea", { placeholder: "Optional description", style: "min-height:48px", id: "cme-description" });
      descIn.textContent = d.description;
      descIn.addEventListener("input", () => { CronViewState.draft.description = descIn.value; });
      const enabledLabel = el("label", { style: "display:flex;align-items:center;gap:6px;cursor:pointer" });
      const enabledChk = el("input", { type: "checkbox", id: "cme-enabled" });
      enabledChk.checked = d.enabled;
      enabledChk.addEventListener("change", () => { CronViewState.draft.enabled = enabledChk.checked; });
      enabledLabel.append(enabledChk, "Enabled");
      const agentIn = el("input", { type: "text", value: d.agent_id, placeholder: "assistant", id: "cme-agent-id" });
      agentIn.addEventListener("input", () => { CronViewState.draft.agent_id = agentIn.value; });
      secBasics.append(
        mfield("name", "Name", nameIn),
        mfield("description", "Description (optional)", descIn),
        el("div", { class: "field cron-modal__field" }, enabledLabel),
        mfield("agent_id", "Agent ID", agentIn),
      );
      modalCard.append(secBasics);

      // Section: Schedule
      const secSched = el("div", { class: "cron-modal__section" });
      secSched.append(el("div", { class: "cron-modal__section-title" }, "Schedule"));
      const kindSel = el("select", { id: "cme-kind" });
      for (const [v, l] of [
        ["builder","builder (recommended)"],
        ["cron","cron expression"],
        ["every","every N units"],
        ["at","run at datetime"],
      ]) {
        const opt = el("option", { value: v }, l);
        if (v === d.scheduleKind) opt.selected = true;
        kindSel.append(opt);
      }
      const schedFields = el("div", { class: "cron-modal__sched-fields" });
      function renderSchedFields() {
        schedFields.innerHTML = "";
        const kind = CronViewState.draft.scheduleKind;
        if (kind === "builder") {
          const builderHost = el("div", {});
          const preview = el("div", { class: "cron-builder__preview" });
          const previewHuman = el("div", { class: "cron-builder__preview-human" });
          const previewCron = el("code", { class: "cron-builder__preview-cron" });
          preview.append(previewHuman, previewCron);
          const refresh = () => {
            renderScheduleBuilder(builderHost, CronViewState.draft.builder, refresh);
            previewHuman.textContent = describeBuilder(CronViewState.draft.builder);
            previewCron.textContent = buildCron(CronViewState.draft.builder);
          };
          refresh();
          schedFields.append(builderHost, preview);
          if (CronViewState.draftErrors.builder) {
            schedFields.append(el("div", { class: "cron-modal__field-error" }, CronViewState.draftErrors.builder));
          }
        } else if (kind === "cron") {
          const cronIn = el("input", { type: "text", value: d.cronExpr, placeholder: "*/5 * * * *", id: "cme-cron-expr" });
          cronIn.addEventListener("input", () => { CronViewState.draft.cronExpr = cronIn.value; });
          schedFields.append(mfield("cronExpr", "Cron expression (5-field)", cronIn));
        } else if (kind === "every") {
          const amtIn = el("input", { type: "number", value: d.everyAmount, placeholder: "1", id: "cme-every-amount", style: "width:80px;flex:0 0 80px" });
          amtIn.addEventListener("input", () => { CronViewState.draft.everyAmount = amtIn.value; });
          const unitSel = el("select", { id: "cme-every-unit", style: "flex:1" });
          for (const u of ["minutes","hours","days"]) {
            const opt = el("option", { value: u }, u);
            if (u === d.everyUnit) opt.selected = true;
            unitSel.append(opt);
          }
          unitSel.addEventListener("change", () => { CronViewState.draft.everyUnit = unitSel.value; });
          schedFields.append(
            el("div", { class: "field cron-modal__field" },
              el("label", {}, "Every"),
              el("div", { class: "row", style: "gap:6px" }, amtIn, unitSel),
            ),
          );
        } else {
          const atIn = el("input", { type: "datetime-local", value: d.scheduleAt, id: "cme-schedule-at" });
          atIn.addEventListener("change", () => { CronViewState.draft.scheduleAt = atIn.value; });
          schedFields.append(mfield("scheduleAt", "Run at", atIn));
        }
        const tzIn = el("input", { type: "text", value: d.timezone, placeholder: "UTC", id: "cme-timezone" });
        tzIn.addEventListener("input", () => { CronViewState.draft.timezone = tzIn.value; });
        schedFields.append(mfield("timezone", "Timezone (optional, e.g. America/New_York)", tzIn));

        // Date-range fields — apply to all kinds. APScheduler honours
        // start_date/end_date on top of the cron trigger.
        const startIn = el("input", { type: "date", value: d.start_date || "", id: "cme-start-date" });
        startIn.addEventListener("change", () => { CronViewState.draft.start_date = startIn.value; });
        const endIn = el("input", { type: "date", value: d.end_date || "", id: "cme-end-date" });
        endIn.addEventListener("change", () => { CronViewState.draft.end_date = endIn.value; });
        schedFields.append(
          el("div", { class: "row" },
            mfield("start_date", "Start (optional)", startIn),
            mfield("end_date", "End (optional)", endIn),
          ),
        );
      }
      kindSel.addEventListener("change", () => {
        CronViewState.draft.scheduleKind = kindSel.value;
        renderSchedFields();
      });
      secSched.append(mfield("scheduleKind", "Schedule kind", kindSel));
      secSched.append(schedFields);
      renderSchedFields();
      modalCard.append(secSched);

      // Section: Execution
      const secExec = el("div", { class: "cron-modal__section" });
      secExec.append(el("div", { class: "cron-modal__section-title" }, "Execution"));
      const promptIn = el("textarea", { placeholder: "Prompt sent to the agent each run", style: "min-height:80px", id: "cme-prompt" });
      promptIn.textContent = d.prompt;
      promptIn.addEventListener("input", () => { CronViewState.draft.prompt = promptIn.value; });
      const chatIn = el("input", { type: "text", value: d.chat_id, placeholder: "chat_id", id: "cme-chat-id" });
      chatIn.addEventListener("input", () => { CronViewState.draft.chat_id = chatIn.value; });
      const channelIn = el("input", { type: "text", value: d.channel, placeholder: "dashboard", id: "cme-channel" });
      channelIn.addEventListener("input", () => { CronViewState.draft.channel = channelIn.value; });
      const accountIn = el("input", { type: "text", value: d.account_id, placeholder: "main", id: "cme-account-id" });
      accountIn.addEventListener("input", () => { CronViewState.draft.account_id = accountIn.value; });
      const threadIn = el("input", { type: "text", value: d.thread_id, placeholder: "optional", id: "cme-thread-id" });
      threadIn.addEventListener("input", () => { CronViewState.draft.thread_id = threadIn.value; });
      secExec.append(
        mfield("prompt", "Prompt", promptIn),
        el("div", { class: "row" },
          mfield("chat_id", "chat_id", chatIn),
          mfield("channel", "channel", channelIn),
        ),
        el("div", { class: "row" },
          mfield("account_id", "account_id", accountIn),
          mfield("thread_id", "thread_id (optional)", threadIn),
        ),
      );
      modalCard.append(secExec);

      // Section: Advanced
      const secAdv = el("div", { class: "cron-modal__section" });
      secAdv.append(el("div", { class: "cron-modal__section-title" }, "Advanced"));
      const modelIn = el("input", { type: "text", value: d.model, placeholder: "model override (leave blank for default)", id: "cme-model" });
      modelIn.addEventListener("input", () => { CronViewState.draft.model = modelIn.value; });
      secAdv.append(mfield("model", "Model (optional)", modelIn));
      modalCard.append(secAdv);

      // Error list (blocking fields summary)
      const errListWrap = el("div", { class: "cron-modal__error-list", style: "display:none" });
      modalCard.append(errListWrap);

      function showErrors(errors) {
        errListWrap.innerHTML = "";
        const keys = Object.keys(errors);
        if (!keys.length) { errListWrap.style.display = "none"; return; }
        errListWrap.style.display = "";
        errListWrap.append(el("div", { class: "cron-modal__error-list-title" }, "Fix these to submit:"));
        const ul = el("ul", { class: "cron-modal__error-list-ul" });
        for (const k of keys) {
          const li = el("li", {});
          const inputId = "cme-" + k.replace(/([A-Z])/g, "-$1").toLowerCase();
          const a = el("a", { href: "#", onclick: (e) => {
            e.preventDefault();
            const target = document.getElementById(inputId);
            if (target) { target.scrollIntoView({ block: "center", behavior: "smooth" }); target.focus(); }
          } }, errors[k]);
          li.append(a);
          ul.append(li);
        }
        errListWrap.append(ul);
      }

      // Footer buttons
      const footer = el("div", { class: "cron-modal__footer" });
      const cancelBtn = el("button", { class: "btn", onclick: closeModal }, "Cancel");
      const submitBtn = el("button", { class: "btn btn-primary", onclick: async () => {
        const draft = CronViewState.draft;
        const errors = validateDraft(draft);
        CronViewState.draftErrors = errors;
        renderModal();
        showErrors(errors);
        if (Object.keys(errors).length) return;

        let schedule = draft.cronExpr;
        if (draft.scheduleKind === "builder") schedule = buildCron(draft.builder);
        else if (draft.scheduleKind === "every") schedule = `every ${draft.everyAmount} ${draft.everyUnit}`;
        else if (draft.scheduleKind === "at") schedule = draft.scheduleAt;

        const params = {
          schedule,
          agent_id: draft.agent_id || "assistant",
          channel: draft.channel || "dashboard",
          account_id: draft.account_id || "main",
          chat_id: draft.chat_id,
          prompt: draft.prompt,
          name: draft.name || undefined,
          description: draft.description || undefined,
          enabled: draft.enabled,
          timezone: draft.timezone || undefined,
          thread_id: draft.thread_id || undefined,
          model: draft.model || undefined,
          // Send empty string so cron.update can clear an existing date.
          start_date: isEdit ? (draft.start_date || "") : (draft.start_date || undefined),
          end_date:   isEdit ? (draft.end_date   || "") : (draft.end_date   || undefined),
        };

        try {
          if (isEdit && CronViewState.editingJobId) {
            await Rpc.call("cron.update", { id: CronViewState.editingJobId, ...params });
            Toast.success("Job updated");
          } else {
            const res = await Rpc.call("cron.create", params);
            Toast.success(`Created ${(res.id || "").slice(0, 8)}`);
          }
          closeModal();
          await loadJobs();
        } catch (e) {
          Toast.error("Save failed", e.message);
        }
      } }, isEdit ? "Save" : "Create");
      footer.append(cancelBtn, submitBtn);
      modalCard.append(footer);
    }

    // ── Load + render job list ──────────────────────────────────────────
    async function loadJobs() {
      let jobs;
      try { jobs = await Rpc.call("cron.list", {}); }
      catch { jobs = []; }
      CronViewState.allJobs = jobs || [];
      // Fetch last run for each job (limit 1).
      await Promise.all(CronViewState.allJobs.map(async (j) => {
        try {
          const r = await Rpc.call("cron.runs", { job_id: j.id, limit: 1, sort_dir: "desc" });
          CronViewState.jobLastRun[j.id] = (r.runs && r.runs.length) ? r.runs[0] : null;
        } catch {
          CronViewState.jobLastRun[j.id] = null;
        }
      }));
      renderJobList();
    }

    function filteredSortedJobs() {
      const s = CronViewState;
      const q = s.query.toLowerCase();
      let jobs = s.allJobs.filter((j) => {
        if (q) {
          const haystack = `${j.name||""} ${j.prompt||""} ${j.agent_id||""} ${j.schedule||""}`.toLowerCase();
          if (!haystack.includes(q)) return false;
        }
        if (s.enabledFilter === "enabled" && !j.enabled) return false;
        if (s.enabledFilter === "disabled" && j.enabled) return false;
        if (s.scheduleKindFilter !== "all" && cronScheduleKind(j.schedule) !== s.scheduleKindFilter) return false;
        if (s.lastStatusFilter !== "all") {
          const last = s.jobLastRun[j.id];
          if (s.lastStatusFilter === "never" && last) return false;
          if (s.lastStatusFilter !== "never" && (!last || last.status !== s.lastStatusFilter)) return false;
        }
        return true;
      });
      // Sort
      jobs.sort((a, b) => {
        let va, vb;
        if (s.sortBy === "name") { va = (a.name||a.id||"").toLowerCase(); vb = (b.name||b.id||"").toLowerCase(); }
        else if (s.sortBy === "schedule") { va = a.schedule||""; vb = b.schedule||""; }
        else if (s.sortBy === "next_run") { va = a.next_run_at||0; vb = b.next_run_at||0; }
        else { // last_run
          va = (s.jobLastRun[a.id]||{}).started_at||0;
          vb = (s.jobLastRun[b.id]||{}).started_at||0;
        }
        if (va < vb) return s.sortDir === "asc" ? -1 : 1;
        if (va > vb) return s.sortDir === "asc" ? 1 : -1;
        return 0;
      });
      return jobs;
    }

    function renderStatusPill(status) {
      const map = { ok: ["pill-ok","✓ ok"], error: ["pill-error","✗ error"], skipped: ["pill-skipped","⏭ skipped"], running: ["pill-running","▶ running"] };
      const [cls, text] = map[status] || ["pill-muted","· never"];
      return el("span", { class: `cron-pill ${cls}` }, text);
    }

    function renderJobList() {
      jobList.innerHTML = "";
      const jobs = filteredSortedJobs();
      if (!jobs.length && !CronViewState.allJobs.length) {
        jobList.append(emptyState({
          icon: "⏱",
          title: "No scheduled jobs yet",
          body: "Cron jobs let an agent run a prompt on a schedule. " +
                "The easiest way to add one is to <strong>ask the agent</strong> from " +
                "the Chat tab — the cron tool will register it for you.",
          example: 'e.g. "Every weekday at 9am summarise overnight Slack DMs"',
        }));
        return;
      }
      if (!jobs.length) {
        jobList.append(emptyState({ icon: "🔍", title: "No jobs match the current filters" }));
        return;
      }
      for (const j of jobs) {
        const lastRun = CronViewState.jobLastRun[j.id];
        const row = el("div", { class: "cron-row" + (j.enabled ? "" : " disabled"), dataset: { jobId: j.id } });

        // Left: schedule + next run
        const schedCol = el("div", {});
        schedCol.append(
          el("div", { class: "schedule" }, j.schedule || "(no schedule)"),
          el("div", { class: "next" }, "next: " + (j.next_run_at ? fmtFuture(j.next_run_at) : "not scheduled")),
        );

        // Middle: name + prompt + meta + pills
        const infoCol = el("div", {});
        const nameEl = el("div", { class: "cron-row__name" }, j.name || j.id || "");
        const promptEl = el("div", { class: "prompt" }, j.prompt || "");
        const metaEl = el("div", { class: "meta" }, `${j.agent_id} via ${j.channel}:${j.account_id}:${j.chat_id}`);
        const pills = el("div", { class: "cron-row__pills" });
        pills.append(
          el("span", { class: `cron-pill ${j.enabled ? "pill-enabled" : "pill-disabled"}` }, j.enabled ? "enabled" : "disabled"),
          renderStatusPill(lastRun ? lastRun.status : null),
        );
        infoCol.append(nameEl, promptEl, metaEl, pills);

        // Right: action buttons
        const actionsCol = el("div", { class: "actions" });
        actionsCol.append(
          el("button", { class: "btn btn-sm", title: "Edit", onclick: () => openModal("edit", j) }, "Edit"),
          el("button", { class: "btn btn-sm", title: "Clone", onclick: () => openModal("clone", j) }, "Clone"),
          el("button", { class: "btn btn-sm", title: j.enabled ? "Disable" : "Enable",
            onclick: async () => {
              await safeRpc("cron.toggle", { id: j.id, enabled: !j.enabled });
              await loadJobs();
            },
          }, j.enabled ? "Disable" : "Enable"),
          el("button", { class: "btn btn-sm", title: "Force run now",
            onclick: async () => {
              // cron.fire with no mode = force run regardless of schedule.
              const r = await safeRpc("cron.fire", { id: j.id });
              if (r && r.fired) Toast.success(`fired ${j.id.slice(0, 8)}`);
              await loadJobs();
            },
          }, "Run"),
          el("button", { class: "btn btn-sm btn-ghost", title: "Run only if due (skip if next_run is in future)",
            onclick: async () => {
              // For v1: same cron.fire RPC. Documented difference: this is
              // conceptually a "run if due" — the server honors next_run_at
              // if the job is not yet past its next-run window. Visually
              // distinct from the force-run button to give operators clarity.
              const r = await safeRpc("cron.fire", { id: j.id });
              if (r && r.fired) Toast.info(`fired (if due) ${j.id.slice(0, 8)}`);
              await loadJobs();
            },
          }, "Run if due"),
          el("button", { class: "btn btn-sm", title: "View run history",
            onclick: () => {
              CronViewState.runsJobId = j.id;
              CronViewState.runsScope = j.id;
              CronViewState.runsOffset = 0;
              // Rebuild scope selector options
              runsScopeEl.innerHTML = "";
              runsScopeEl.append(el("option", { value: "all" }, "All jobs"));
              runsScopeEl.append(el("option", { value: j.id, selected: true }, j.name || j.id));
              runsScopeEl.value = j.id;
              switchTab("runs");
            },
          }, "History"),
          el("button", { class: "btn btn-sm btn-danger", title: "Remove job",
            onclick: async () => {
              if (!confirm(`Remove job ${j.name || j.id}?`)) return;
              await safeRpc("cron.remove", { id: j.id });
              await loadJobs();
            },
          }, "Remove"),
        );

        row.append(schedCol, infoCol, actionsCol);
        jobList.append(row);
      }
    }

    // ── Run log ─────────────────────────────────────────────────────────
    async function loadRuns() {
      const s = CronViewState;
      const params = {
        limit: s.runsLimit,
        offset: 0,
        sort_dir: s.runsSortDir,
      };
      if (s.runsScope && s.runsScope !== "all") params.job_id = s.runsScope;
      if (s.runsQuery) params.query = s.runsQuery;
      if (s.runsStatusFilter.length) params.status = s.runsStatusFilter;
      if (s.runsDeliveryFilter.length) params.delivery = s.runsDeliveryFilter;

      let result;
      try { result = await Rpc.call("cron.runs", params); }
      catch { result = { runs: [], total: 0, has_more: false }; }

      s.runs = result.runs || [];
      s.runsTotal = result.total || 0;
      s.runsHasMore = result.has_more || false;
      s.runsOffset = s.runs.length;
      renderRunsList();
    }

    async function loadMoreRuns() {
      const s = CronViewState;
      const params = {
        limit: s.runsLimit,
        offset: s.runsOffset,
        sort_dir: s.runsSortDir,
      };
      if (s.runsScope && s.runsScope !== "all") params.job_id = s.runsScope;
      if (s.runsQuery) params.query = s.runsQuery;
      if (s.runsStatusFilter.length) params.status = s.runsStatusFilter;

      let result;
      try { result = await Rpc.call("cron.runs", params); }
      catch { return; }

      const more = result.runs || [];
      s.runs = s.runs.concat(more);
      s.runsHasMore = result.has_more || false;
      s.runsOffset += more.length;
      renderRunsList();
    }

    function renderRunsList() {
      runsList.innerHTML = "";
      const runs = CronViewState.runs;
      if (!runs.length) {
        runsList.append(emptyState({
          icon: "📋",
          title: "No runs yet — fire a job to see history.",
        }));
        runsLoadMoreBtn.style.display = "none";
        return;
      }
      for (const run of runs) {
        const jobName = (() => {
          const j = CronViewState.allJobs.find((x) => x.id === run.job_id);
          return j ? (j.name || j.id) : run.job_id;
        })();
        const startedFmt = run.started_at ? fmtTime(run.started_at) : "—";
        const runCard = el("div", { class: "cron-run-card", dataset: { runId: run.run_id } });

        const titleBar = el("div", { class: "cron-run-card__title" });
        titleBar.append(
          el("span", { class: "cron-run-card__name" }, `${jobName} run @ ${startedFmt}`),
          renderStatusPill(run.status),
        );

        const summary = el("div", { class: "cron-run-card__summary" }, run.summary || "(no summary)");

        const chips = el("div", { class: "cron-run-card__chips" });
        if (run.model) chips.append(el("span", { class: "cron-chip" }, `model: ${run.model}`));
        if (run.provider) chips.append(el("span", { class: "cron-chip" }, `provider: ${run.provider}`));
        if (run.token_usage) {
          const u = run.token_usage;
          chips.append(el("span", { class: "cron-chip" }, `tokens: ${u.input}/${u.output}/${u.total}`));
        }
        if (run.delivery_status) chips.append(el("span", { class: `cron-chip cron-chip--${run.delivery_status}` }, `delivery: ${run.delivery_status}`));

        const expandRow = el("div", { class: "cron-run-card__expand" });
        if (run.output_preview) {
          const detOut = el("details", { class: "cron-run-card__details" });
          detOut.append(el("summary", {}, "view output"));
          detOut.append(el("pre", { class: "cron-run-card__pre" }, run.output_preview));
          expandRow.append(detOut);
        }
        if (run.error) {
          const detErr = el("details", { class: "cron-run-card__details" });
          detErr.append(el("summary", {}, "view error"));
          detErr.append(el("pre", { class: "cron-run-card__pre cron-run-card__pre--error" }, run.error));
          expandRow.append(detErr);
        }

        runCard.append(titleBar, summary, chips);
        if (expandRow.children.length) runCard.append(expandRow);
        runsList.append(runCard);
      }
      runsLoadMoreBtn.style.display = CronViewState.runsHasMore ? "" : "none";
    }

    // ── Boot ────────────────────────────────────────────────────────────
    await loadJobs();
    const interval = setInterval(loadJobs, 5000);
    return () => {
      clearInterval(interval);
      document.removeEventListener("keydown", escHandler);
      if (modalOverlay.parentNode) modalOverlay.remove();
    };
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

    function renderGatedToolsPanel(tools) {
      const panel = el("section", { class: "gated-tools" });
      panel.append(el("h3", {}, `Gated tools (${tools ? tools.length : 0})`));
      if (!tools || !tools.length) {
        panel.append(el("div", { class: "ctx" },
          "No tools are currently approval-gated. Wire an ApprovalManager " +
          "into the agent factory to require human confirmation for " +
          "destructive tools."));
        return panel;
      }
      const tbl = el("table", { class: "gated-tools-table" });
      const thead = el("thead", {},
        el("tr", {},
          el("th", {}, "Tool"),
          el("th", {}, "Agents"),
          el("th", {}, "Description"),
        ),
      );
      const tbody = el("tbody");
      for (const t of tools) {
        tbody.append(el("tr", {},
          el("td", { class: "mono" }, t.name),
          el("td", {}, (t.agents || []).join(", ")),
          el("td", {}, t.description || ""),
        ));
      }
      tbl.append(thead, tbody);
      panel.append(tbl);
      return panel;
    }

    async function refresh() {
      const [list, tools] = await Promise.all([
        safeRpc("exec-approvals.list", {}, { quiet: true }),
        safeRpc("exec-approvals.tools", {}, { quiet: true }),
      ]);
      root.innerHTML = "";
      if (!list || !list.length) {
        root.append(emptyState({
          icon: "✅",
          title: "No pending approvals",
          body: "Tools wrapped via <code>gated_tool()</code> pause and ask " +
                "for human confirmation before running. Pending requests appear " +
                "here — Approve or Deny to unblock the agent.",
        }));
      } else {
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
      root.append(renderGatedToolsPanel(tools));
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
            "Live config viewer. Edit ~/.oxenclaw/config.yaml on disk and click Reload above. " +
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
            "Switch to the Browse tab or run `oxenclaw skills search` from a terminal."),
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
// Memory view — tabbed: Search / Browse / Stats
// ─────────────────────────────────────────────────────────────────────────
const MemoryView = {
  title: "Memory",
  async render(root, actions) {
    let activeTab = sessionStorage.getItem("samp.memory.tab") || "search";
    // Shared state for Browse tab navigation from Search.
    let browseTarget = null; // { path, start_line } set by "Open" button

    root.className = (root.className || "") + " memory-view";

    const tabBar = el("div", { class: "memory-tabs" });
    const searchTabEl  = el("div", { class: "memory-tab", onclick: () => switchTab("search") },  "Search");
    const browseTabEl  = el("div", { class: "memory-tab", onclick: () => switchTab("browse") },  "Browse");
    const statsTabEl   = el("div", { class: "memory-tab", onclick: () => switchTab("stats") },   "Stats");
    tabBar.append(searchTabEl, browseTabEl, statsTabEl);

    const body = el("div", { class: "memory-body" });
    root.append(tabBar, body);

    function switchTab(next, opts) {
      activeTab = next;
      sessionStorage.setItem("samp.memory.tab", next);
      searchTabEl.classList.toggle("active", next === "search");
      browseTabEl.classList.toggle("active", next === "browse");
      statsTabEl.classList.toggle("active",  next === "stats");
      body.innerHTML = "";
      if (next === "search")  renderSearch();
      else if (next === "browse") renderBrowse(opts);
      else renderStats();
    }

    // ── Search tab ──────────────────────────────────────────────────────
    function renderSearch() {
      const wrap = el("div", { class: "memory-search" });

      // Row 1: search input + toggles
      const bar = el("div", { class: "memory-search__bar search-bar" });
      const qInput = el("input", {
        type: "search",
        class: "memory-search__input",
        placeholder: "Search memory… (sqlite-vec + FTS5)",
      });

      // Toggle switches: Hybrid / MMR / Decay
      function mkToggle(label, id) {
        const tog = el("label", { class: "memory-toggle", title: label });
        const chk = el("input", { type: "checkbox", id, checked: true });
        const span = el("span", { class: "memory-toggle__track" });
        const lbl  = el("span", { class: "memory-toggle__label" }, label);
        tog.append(chk, span, lbl);
        return { toggle: tog, chk };
      }
      const { toggle: hybridToggle, chk: hybridChk } = mkToggle("Hybrid", "mem-hybrid");
      const { toggle: mmrToggle,    chk: mmrChk }    = mkToggle("MMR",    "mem-mmr");
      const { toggle: decayToggle,  chk: decayChk }  = mkToggle("Decay",  "mem-decay");

      const goBtn = el("button", { class: "btn btn-primary" }, "Search");
      bar.append(qInput, hybridToggle, mmrToggle, decayToggle, goBtn);

      // Row 2: source filter + k slider
      const controls = el("div", { class: "memory-search__controls" });
      const srcSel = el("select", { class: "memory-search__source" },
        el("option", { value: "" }, "All sources"),
        el("option", { value: "memory" }, "memory"),
        el("option", { value: "sessions" }, "sessions"),
        el("option", { value: "wiki" }, "wiki"),
      );
      const kLabel = el("label", { class: "memory-search__k-label" }, "k=");
      const kVal   = el("span",  { class: "memory-search__k-val" }, "5");
      const kRange = el("input", {
        type: "range", min: "1", max: "20", value: "5",
        class: "memory-search__k-range",
        oninput: () => { kVal.textContent = kRange.value; },
      });
      controls.append(srcSel, kLabel, kRange, kVal);

      const results = el("div", { class: "memory-search__results" });
      wrap.append(bar, controls, results);
      body.append(wrap);

      async function doSearch() {
        const q   = qInput.value.trim();
        const k   = Math.max(1, Math.min(20, parseInt(kRange.value, 10) || 5));
        const src = srcSel.value;
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
        // Backend's _SearchParams expects nested objects, not bare
        // booleans, and the key is `temporal_decay` (not `decay`).
        // `extra="forbid"` rejects the wrong shape, which is why every
        // call returned a ValidationError before. Send object form,
        // omit the sub-block entirely when the toggle is off so we
        // pass the backend's None-default.
        const params = { query: q, k };
        if (hybridChk.checked) params.hybrid = { enabled: true };
        if (mmrChk.checked)    params.mmr    = { enabled: true };
        if (decayChk.checked)  params.temporal_decay = { enabled: true };
        if (src) params.source = src;
        let res;
        try {
          res = await Rpc.call("memory.search", params);
        } catch (e) {
          results.append(el("div", { class: "empty" }, e.message));
          return;
        }
        if (res && res.ok === false) {
          results.append(el("div", { class: "empty" }, res.error || "search failed"));
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
        const list = el("ul", { class: "list memory-hits" });
        for (const h of hits) {
          // Backend hit shape: {chunk: {id, path, source, start_line,
          // end_line, text, hash}, score, distance, citation}. Earlier
          // versions of this view read `h.text` / `h.path` at the top
          // level, which produced empty previews and "(untitled)"
          // citations. Read from h.chunk first.
          const c = h.chunk || h;
          const preview = (c.text || c.content || "").slice(0, 200);
          const full    = (c.text || c.content || "");
          const startLine = c.start_line != null ? c.start_line : 1;
          const endLine   = c.end_line   != null ? c.end_line   : "";
          const path = c.path || h.path;
          const citation  = h.citation || (
            path
              ? (endLine ? `${path}:${startLine}-${endLine}` : `${path}:${startLine}`)
              : (c.id || h.id || "(untitled)")
          );
          let expanded = false;

          const scorePill = el("span", { class: "memory-hit__score" },
            h.score != null ? Number(h.score).toFixed(2) : "—",
          );
          const citEl   = el("div",  { class: "memory-hit__citation" }, citation);
          const prevEl  = el("div",  { class: "memory-hit__preview" }, preview);
          const openBtn = el("button", { class: "btn btn-sm btn-ghost memory-hit__open" }, "Open");

          openBtn.onclick = (ev) => {
            ev.stopPropagation();
            browseTarget = { path: path || c.id || h.id, start_line: startLine };
            switchTab("browse", browseTarget);
          };

          const item = el("li", { class: "list-item memory-hit" });
          item.append(
            el("div", { class: "memory-hit__row" }, scorePill, citEl, openBtn),
            prevEl,
          );
          item.onclick = () => {
            expanded = !expanded;
            prevEl.textContent = expanded ? full : preview;
            item.classList.toggle("memory-hit--expanded", expanded);
          };
          list.append(item);
        }
        results.append(list);
      }

      goBtn.onclick = doSearch;
      qInput.addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });
      doSearch();
    }

    // ── Browse tab ──────────────────────────────────────────────────────
    function renderBrowse(opts) {
      const wrap = el("div", { class: "memory-browse" });
      const left  = el("div", { class: "memory-browse__tree" });
      const right = el("div", { class: "memory-browse__viewer" });
      wrap.append(left, right);
      body.append(wrap);

      // Right pane: show file content
      let viewerPath = null;
      let viewerOffset = 1;
      const viewerContent = el("pre", { class: "memory-browse__content code-block" });
      const loadMoreBtn = el("button", { class: "btn btn-sm memory-browse__load-more" }, "Load more");
      const deleteBtn   = el("button", { class: "btn btn-sm btn-danger memory-browse__delete" }, "Delete this file");

      async function loadFile(path, fromLine, reset) {
        if (reset) {
          viewerPath = path;
          viewerOffset = fromLine || 1;
          viewerContent.textContent = "loading…";
        }
        let res;
        try {
          res = await Rpc.call("memory.get", { path: viewerPath, from_line: viewerOffset, lines: 200 });
        } catch (e) {
          viewerContent.textContent = `error: ${e.message}`;
          return;
        }
        // Backend shape: {ok: true, read: {path, text, start_line,
        // end_line, truncated, next_from}}. The legacy fallbacks
        // (res.text / res.content) stayed in place from an earlier
        // gateway version and silently produced empty viewers — the
        // server hasn't returned content at the top level for a long
        // time. Read from res.read first, then fall back.
        const read = (res && res.read) || res || {};
        const text = read.text || res.text || res.content || "";
        if (reset) {
          viewerContent.textContent = text || "(empty)";
        } else {
          viewerContent.textContent += text;
        }
        // Advance the offset using the backend's authoritative
        // next_from when available; fall back to counting newlines for
        // older gateways that don't include it.
        if (typeof read.next_from === "number" && read.next_from > 0) {
          viewerOffset = read.next_from;
        } else {
          viewerOffset += (text.match(/\n/g) || []).length + 1;
        }
        const hasMore = read.truncated === true
          || (typeof read.next_from === "number" && read.next_from > 0);
        loadMoreBtn.style.display = hasMore ? "" : "none";
      }

      loadMoreBtn.onclick = () => loadFile(viewerPath, viewerOffset, false);
      deleteBtn.onclick   = async () => {
        if (!viewerPath) return;
        if (!confirm(`Delete ${viewerPath} from memory?`)) return;
        try {
          await Rpc.call("memory.delete", { path: viewerPath });
          Toast.success("Deleted", viewerPath);
          viewerContent.textContent = "";
          loadMoreBtn.style.display = "none";
          deleteBtn.style.display   = "none";
          await loadTree(); // refresh tree
        } catch (e) {
          Toast.error("Delete failed", e.message);
        }
      };

      right.append(
        el("div", { class: "memory-browse__viewer-header" },
          el("span", { class: "memory-browse__viewer-path" }, ""),
        ),
        viewerContent,
        el("div", { class: "memory-browse__viewer-actions" }, loadMoreBtn, deleteBtn),
      );
      loadMoreBtn.style.display = "none";
      deleteBtn.style.display   = "none";

      function showFile(path, fromLine) {
        right.querySelector(".memory-browse__viewer-path").textContent = path;
        deleteBtn.style.display = "";
        loadMoreBtn.style.display = "none";
        viewerOffset = fromLine || 1;
        loadFile(path, viewerOffset, true);
      }

      // Left pane: tree
      async function loadTree() {
        left.innerHTML = "";
        left.append(el("div", { class: "empty" }, "loading…"));
        let res;
        try {
          res = await Rpc.call("memory.list", {});
        } catch (e) {
          left.innerHTML = "";
          left.append(emptyState({
            icon: "📂",
            title: "No files indexed",
            body: "The memory index is empty. Chat with an agent to populate it.",
          }));
          return;
        }
        const files = (res && res.files) || [];
        left.innerHTML = "";
        if (!files.length) {
          left.append(emptyState({
            icon: "📂",
            title: "No files indexed",
            body: "The memory index is empty. Chat with an agent to populate it.",
          }));
          return;
        }
        // Group by source
        const groups = {};
        for (const f of files) {
          const src = f.source || "memory";
          if (!groups[src]) groups[src] = [];
          groups[src].push(f);
        }
        for (const [src, items] of Object.entries(groups)) {
          const grpEl = el("details", { class: "memory-tree__group", open: true });
          grpEl.append(el("summary", { class: "memory-tree__group-label" }, src));
          for (const f of items) {
            const path = f.path || f.id || "?";
            const chunks = f.chunk_count != null ? ` (${f.chunk_count})` : "";
            const leaf = el("div", { class: "memory-tree__leaf", onclick: () => showFile(path, 1) }, path + chunks);
            grpEl.append(leaf);
          }
          left.append(grpEl);
        }
      }
      loadTree().then(() => {
        // If navigated from Search "Open", show that file
        if (opts && opts.path) showFile(opts.path, opts.start_line || 1);
      });
    }

    // ── Stats tab ───────────────────────────────────────────────────────
    async function renderStats() {
      const wrap = el("div", { class: "memory-stats" });
      body.append(wrap);
      wrap.append(el("div", { class: "empty" }, "loading…"));

      let res;
      try {
        res = await Rpc.call("memory.stats", {});
      } catch (e) {
        wrap.innerHTML = "";
        wrap.append(emptyState({
          icon: "📊",
          title: "Stats unavailable",
          body: e.message,
        }));
        return;
      }

      wrap.innerHTML = "";
      // Backend shape: {ok, total_files, total_chunks, dimensions,
      // path, meta: {provider, model, last_sync, ...}}.
      const stats = res || {};
      const meta = (stats.meta || {});

      const cards = [
        ["Total chunks",     stats.total_chunks    ?? "—"],
        ["Total files",      stats.total_files     ?? "—"],
        ["Embedding dims",   stats.dimensions ?? stats.embedding_dims ?? "—"],
        ["Provider",         meta.provider ?? stats.provider ?? "—"],
        ["Model",            meta.model    ?? stats.model    ?? "—"],
        ["Last sync",        (meta.last_sync ?? stats.last_sync)
          ? fmtTime(meta.last_sync ?? stats.last_sync) : "—"],
        ["Cache hit rate",   stats.cache_hit_rate != null
          ? (Number(stats.cache_hit_rate) * 100).toFixed(1) + "%" : "—"],
      ];

      const grid = el("div", { class: "memory-stats__grid" });
      for (const [label, value] of cards) {
        grid.append(
          el("div", { class: "memory-stats__card" },
            el("div", { class: "memory-stats__card-val" }, String(value)),
            el("div", { class: "memory-stats__card-label" }, label),
          ),
        );
      }
      wrap.append(grid);

      // Action row: Sync / Export / Import
      const actRow = el("div", { class: "memory-stats__actions" });

      const syncBtn = el("button", { class: "btn btn-primary", onclick: async () => {
        syncBtn.disabled = true;
        Toast.info("Syncing memory…", "");
        try {
          await Rpc.call("memory.sync", {});
          Toast.success("Sync complete", "");
          await renderStats();
        } catch (e) {
          Toast.error("Sync failed", e.message);
        } finally {
          syncBtn.disabled = false;
        }
      } }, "Sync now");

      const exportBtn = el("button", { class: "btn", onclick: async () => {
        exportBtn.disabled = true;
        try {
          const r = await Rpc.call("memory.export", {});
          const blob = new Blob([JSON.stringify(r, null, 2)], { type: "application/json" });
          const url  = URL.createObjectURL(blob);
          const a    = document.createElement("a");
          a.href     = url;
          a.download = "memory-export.json";
          a.click();
          URL.revokeObjectURL(url);
          Toast.success("Export ready", "Download started");
        } catch (e) {
          Toast.error("Export failed", e.message);
        } finally {
          exportBtn.disabled = false;
        }
      } }, "Export");

      const importInput = el("input", {
        type: "file", accept: ".json",
        style: "display:none",
      });
      const importBtn = el("button", { class: "btn", onclick: () => importInput.click() }, "Import");
      importInput.onchange = async () => {
        const file = importInput.files && importInput.files[0];
        if (!file) return;
        importBtn.disabled = true;
        try {
          const text = await file.text();
          const data = JSON.parse(text);
          const r = await Rpc.call("memory.import", data);
          Toast.success("Import done", r && r.message ? r.message : "Loaded");
        } catch (e) {
          Toast.error("Import failed", e.message);
        } finally {
          importBtn.disabled = false;
          importInput.value = "";
        }
      };

      actRow.append(syncBtn, exportBtn, importBtn, importInput);
      wrap.append(actRow);
    }

    // Boot the active tab
    switchTab(activeTab);
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
              el("span", { class: "key" }, `${s.agent_id || "?"} · ${s.title || shortId(s)}`),
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
            el("button", { class: "btn btn-sm", onclick: () => compact(s) }, "Compact"),
            el("button", { class: "btn btn-sm", onclick: () => reset(s) }, "Reset"),
            el("button", { class: "btn btn-sm", onclick: () => fork(s) }, "Fork"),
            el("button", { class: "btn btn-sm", onclick: () => archive(s) }, "Archive"),
            el("button", { class: "btn btn-sm btn-danger", onclick: () => del(s) }, "Delete"),
          ),
        );
        list.append(card);
      }
    }

    function shortId(s) {
      const k = s.session_key || s.id || "";
      return k.length > 12 ? k.slice(0, 8) + "…" : k;
    }
    async function preview(s) {
      try {
        const res = await safeRpc("sessions.preview", { id: s.id });
        if (!res) {
          Toast.info("preview", "session not found");
          return;
        }
        const lines = [
          res.title ? `title: ${res.title}` : null,
          res.first_user ? `first user: ${res.first_user}` : null,
          res.last_assistant ? `last assistant: ${res.last_assistant}` : null,
          `messages: ${res.message_count ?? "?"} · compactions: ${res.compaction_count ?? 0}`,
        ].filter(Boolean);
        Toast.info("preview", lines.join("\n"));
      } catch (e) {
        Toast.error(`preview failed: ${e.message}`);
      }
    }
    async function compact(s) {
      if (!confirm(`Compact session ${shortId(s)}? Old turns will be summarised.`)) return;
      try {
        const res = await Rpc.call("sessions.compact", { id: s.id, keep_tail_turns: 6 });
        if (res && res.compacted) {
          Toast.success(`compacted: ${res.tokens_before} → ${res.tokens_after} tokens`);
        } else {
          Toast.info("compact", "below threshold — nothing to do");
        }
      } catch (e) {
        Toast.error(`compact failed: ${e.message}`);
      }
      refresh();
    }
    async function reset(s) {
      if (!confirm(`Reset session ${shortId(s)}? Messages will be cleared.`)) return;
      await safeRpc("sessions.reset", { id: s.id });
      Toast.success("session reset");
      refresh();
    }
    async function fork(s) {
      const newTitle = window.prompt("Title for the forked session:", (s.title || shortId(s)) + " (fork)");
      if (newTitle === null) return;
      const res = await safeRpc("sessions.fork", { id: s.id, title: newTitle || null });
      if (res && res.id) Toast.success(`forked → ${res.id.slice(0, 8)}…`);
      refresh();
    }
    async function archive(s) {
      await safeRpc("sessions.archive", { id: s.id });
      Toast.success("archived");
      refresh();
    }
    async function del(s) {
      if (!confirm(`Delete session ${shortId(s)}? This cannot be undone.`)) return;
      await safeRpc("sessions.delete", { id: s.id });
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
// MCP view — manage external MCP (Model Context Protocol) servers
//
// Reads and writes ~/.oxenclaw/mcp.json via the mcp.* RPC surface. Lets
// the operator add, edit, delete, and connection-test stdio (subprocess)
// or HTTP/SSE servers without leaving the dashboard. Restart the gateway
// after edits — MCP servers are wired into the agent at startup.
// ─────────────────────────────────────────────────────────────────────────
const MCPView = {
  title: "MCP",
  async render(root, actions) {
    let alive = true;
    let configPath = "";

    root.className = (root.className || "") + " mcp-view";

    const introCard = el("div", { class: "card" },
      el("div", { class: "card-meta" },
        "Configure external MCP (Model Context Protocol) servers. Their tools become " +
        "available to the agent at gateway startup. ",
        el("strong", {}, "Restart the gateway after edits."),
      ),
    );
    const pathLine = el("div", { class: "card-meta", style: "margin-top:8px" });
    introCard.append(pathLine);

    const listBox = el("div", { class: "mcp-list", style: "display:flex;flex-direction:column;gap:12px;margin-top:12px" });

    const addBtn = el("button", { class: "btn btn-primary btn-sm", onclick: () => openModal({ mode: "add" }) }, "+ Add server");
    const refreshBtn = el("button", { class: "btn btn-ghost btn-sm", onclick: () => refresh() }, "↻ Refresh");
    actions.append(addBtn, refreshBtn);

    root.append(introCard, listBox);

    async function refresh() {
      if (!alive) return;
      listBox.innerHTML = "";
      const r = await safeRpc("mcp.list", {}, { quiet: true });
      configPath = (r && r.config_path) || "~/.oxenclaw/mcp.json";
      pathLine.innerHTML = "";
      pathLine.append(
        document.createTextNode("Config file: "),
        el("code", {}, configPath),
        document.createTextNode(r && r.exists ? "" : " (not yet created)"),
      );
      const servers = (r && r.servers) || [];
      if (!servers.length) {
        listBox.append(emptyState({
          icon: "🔌",
          title: "No MCP servers configured",
          body:
            "Add a <strong>stdio</strong> server (subprocess like " +
            "<code>npx @modelcontextprotocol/server-filesystem</code>) or an " +
            "<strong>HTTP / SSE</strong> server to expose its tools to the agent.",
          actions: [
            el("button", { class: "btn btn-primary btn-sm", onclick: () => openModal({ mode: "add" }) },
              "+ Add your first server"),
          ],
        }));
        return;
      }
      for (const s of servers) listBox.append(renderCard(s));
    }

    function renderCard(s) {
      const card = el("div", { class: "skill-card mcp-card" });
      const head = el("div", { class: "head" });
      head.append(
        el("span", { class: "emoji" }, s.kind === "http" ? "🌐" : "⚙"),
        el("span", { class: "slug" }, s.name),
        el("span", { class: "name" }, s.kind ? `· ${s.kind}` : ""),
      );
      if (!s.valid) head.append(el("span", { class: "tag req", style: "background:#a33;color:#fff" }, "invalid"));
      card.append(head);

      if (s.description) card.append(el("div", { class: "summary" }, s.description));
      if (!s.valid && s.reason) card.append(el("div", { class: "summary", style: "color:#c44" }, `⚠ ${s.reason}`));

      if (s.kind === "stdio" && s.dropped_env_keys && s.dropped_env_keys.length) {
        const meta = el("div", { class: "meta" }, "stripped env: ");
        for (const k of s.dropped_env_keys) meta.append(el("span", { class: "tag req" }, k));
        card.append(meta);
      }
      if (s.connection_timeout_ms) {
        card.append(el("div", { class: "meta" }, `timeout ${s.connection_timeout_ms} ms`));
      }

      card.append(el("div", { class: "actions" },
        el("button", { class: "btn btn-sm", onclick: () => openModal({ mode: "edit", server: s }) }, "Edit"),
        el("button", { class: "btn btn-sm btn-ghost", onclick: () => testServer(s.name) }, "Test"),
        el("button", { class: "btn btn-sm btn-danger", onclick: async () => {
          if (!confirm(`Delete MCP server "${s.name}"?`)) return;
          const r = await safeRpc("mcp.delete", { name: s.name });
          if (r.ok) Toast.success(`removed ${s.name}`);
          else Toast.error("delete failed", r.error || "");
          await refresh();
        } }, "Delete"),
      ));
      return card;
    }

    async function testServer(name) {
      Toast.info(`testing ${name}…`, "connecting");
      const r = await safeRpc("mcp.test", { name }, { quiet: true });
      if (r.ok) {
        const tools = (r.tools || []).map((t) => t.name).filter(Boolean);
        Toast.success(`${name} reachable`, `${tools.length} tool(s)${tools.length ? ": " + tools.slice(0, 6).join(", ") : ""}`);
      } else {
        Toast.error(`${name} test failed`, r.error || "unknown error");
      }
    }

    // ── Modal: add / edit form ────────────────────────────────────────
    function openModal({ mode, server }) {
      const overlay = el("div", { class: "cmd-help" });
      const card = el("div", { class: "cmd-help-card", style: "min-width:520px;max-width:720px;text-align:left" });

      const title = el("h3", {}, mode === "add" ? "Add MCP server" : `Edit ${server?.name || ""}`);
      card.append(title);

      const initial = server?.raw || {};
      const initialKind = server?.kind || (initial.url ? "http" : "stdio");

      const form = el("div", { style: "display:flex;flex-direction:column;gap:10px" });

      // Name (locked when editing — used as the dict key on disk)
      const nameField = field("Name", el("input", {
        type: "text",
        value: server?.name || "",
        placeholder: "e.g. filesystem",
        spellcheck: "false",
        readonly: mode === "edit" ? true : false,
      }));
      form.append(nameField.row);

      // Kind selector
      const kindSel = el("select", {},
        el("option", { value: "stdio" }, "stdio (subprocess)"),
        el("option", { value: "http" }, "http (SSE / streamable-http)"),
      );
      kindSel.value = initialKind;
      form.append(field("Transport", kindSel).row);

      // ── stdio fields ──
      const cmdInput = el("input", { type: "text", value: initial.command || "", placeholder: "npx", spellcheck: "false" });
      const argsInput = el("textarea", { rows: "2", placeholder: "one arg per line" });
      argsInput.value = (initial.args || []).join("\n");
      const cwdInput = el("input", { type: "text", value: initial.cwd || "", placeholder: "(optional) working directory", spellcheck: "false" });
      const envInput = el("textarea", { rows: "3", placeholder: "KEY=VALUE per line\nFOO=bar" });
      envInput.value = Object.entries(initial.env || {}).map(([k, v]) => `${k}=${v}`).join("\n");

      const stdioBox = el("div", { style: "display:flex;flex-direction:column;gap:10px" },
        field("Command", cmdInput).row,
        field("Args", argsInput).row,
        field("Working dir", cwdInput).row,
        field("Env", envInput).row,
      );

      // ── http fields ──
      const urlInput = el("input", { type: "text", value: initial.url || "", placeholder: "https://mcp.example.com/sse", spellcheck: "false" });
      const transportSel = el("select", {},
        el("option", { value: "sse" }, "sse"),
        el("option", { value: "streamable-http" }, "streamable-http"),
      );
      transportSel.value = initial.transport === "streamable-http" ? "streamable-http" : "sse";
      const headersInput = el("textarea", { rows: "3", placeholder: "Header-Name: value per line\nAuthorization: Bearer xxx" });
      headersInput.value = Object.entries(initial.headers || {}).map(([k, v]) => `${k}: ${v}`).join("\n");

      const httpBox = el("div", { style: "display:flex;flex-direction:column;gap:10px" },
        field("URL", urlInput).row,
        field("Transport type", transportSel).row,
        field("Headers", headersInput).row,
      );

      // Shared: connection timeout
      const timeoutInput = el("input", { type: "number", min: "0", step: "500",
        value: server?.connection_timeout_ms || "", placeholder: "30000" });
      const timeoutBox = field("Connection timeout (ms)", timeoutInput).row;

      const dynBox = el("div");
      function syncKind() {
        dynBox.innerHTML = "";
        if (kindSel.value === "stdio") dynBox.append(stdioBox);
        else dynBox.append(httpBox);
      }
      kindSel.addEventListener("change", syncKind);
      syncKind();
      form.append(dynBox, timeoutBox);

      const errBox = el("div", { class: "login-gate__error", hidden: true, style: "margin-top:8px" });

      const saveBtn = el("button", { class: "btn primary" }, mode === "add" ? "Create" : "Save");
      const cancelBtn = el("button", { class: "btn btn-ghost btn-sm", onclick: () => overlay.remove() }, "Cancel");
      const btnRow = el("div", { style: "margin-top:12px;display:flex;gap:8px;justify-content:flex-end" }, cancelBtn, saveBtn);

      saveBtn.addEventListener("click", async () => {
        errBox.hidden = true;
        const name = (nameField.input.value || "").trim();
        if (!name) {
          errBox.textContent = "Name is required.";
          errBox.hidden = false;
          return;
        }
        const payload = {
          name,
          kind: kindSel.value,
          args: [],
          env: {},
          headers: {},
        };
        if (kindSel.value === "stdio") {
          payload.command = cmdInput.value.trim();
          payload.args = argsInput.value.split("\n").map((s) => s.trim()).filter(Boolean);
          payload.cwd = cwdInput.value.trim() || null;
          payload.env = parseKvBlock(envInput.value, "=");
        } else {
          payload.url = urlInput.value.trim();
          payload.transport = transportSel.value;
          payload.headers = parseKvBlock(headersInput.value, ":");
        }
        const t = parseInt(timeoutInput.value, 10);
        if (Number.isFinite(t) && t > 0) payload.connection_timeout_ms = t;

        // Strip null/empty fields the backend's pydantic spec rejects (extra="forbid").
        if (payload.cwd == null) delete payload.cwd;

        saveBtn.disabled = true;
        const method = mode === "add" ? "mcp.add" : "mcp.update";
        const r = await safeRpc(method, payload, { quiet: true });
        saveBtn.disabled = false;
        if (!r.ok) {
          errBox.textContent = r.error || "save failed";
          errBox.hidden = false;
          return;
        }
        Toast.success(mode === "add" ? `added ${name}` : `updated ${name}`);
        overlay.remove();
        await refresh();
      });

      card.append(form, errBox, btnRow);
      overlay.append(card);
      document.body.append(overlay);
      setTimeout(() => nameField.input.focus(), 50);

      function field(label, input) {
        const row = el("label", { class: "field" },
          el("span", {}, label),
          input,
        );
        return { row, input };
      }
    }

    function parseKvBlock(text, sep) {
      const out = {};
      for (const line of (text || "").split("\n")) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        const idx = trimmed.indexOf(sep);
        if (idx <= 0) continue;
        const k = trimmed.slice(0, idx).trim();
        const v = trimmed.slice(idx + 1).trim();
        if (k) out[k] = v;
      }
      return out;
    }

    await refresh();
    const interval = setInterval(refresh, 8000);
    return () => { alive = false; clearInterval(interval); };
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
  try {
    const r = await Rpc.call("mcp.list", {});
    const badge = $("nav-mcp-badge");
    const n = (r && r.servers && r.servers.length) || 0;
    if (badge && n > 0) { badge.hidden = false; badge.textContent = String(n); }
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
    // Ctrl+Shift+N — start a fresh chat from anywhere. The handler
    // dispatches a custom event so the active ChatView (if rendered)
    // picks it up and can sync its inputs; if Chat isn't current we
    // navigate to it first.
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === "n" || e.key === "N")) {
      e.preventDefault();
      ChatState.newChat();
      if (Router.active !== "chat") Router.go("chat");
      else window.dispatchEvent(new CustomEvent("samp:new-chat"));
      Toast.info("New chat started", `chat_id = ${ChatState.chatId}`);
      return;
    }
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
  const KEY = "oxenclaw_theme";
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
    { id: "go-mcp",        group: "Go to", icon: "🔌", title: "MCP",        hint: "",     run: () => Router.go("mcp") },
    { id: "go-config",     group: "Go to", icon: "⚙",  title: "Config",     hint: "g g", run: () => Router.go("config") },
    { id: "go-rpc",        group: "Go to", icon: "📜", title: "RPC log",    hint: "",     run: () => Router.go("rpc") },
    { id: "theme-cycle",   group: "Action", icon: "🌓", title: "Cycle theme (system → light → dark)", hint: "",
      run: () => { /* call inside Theme module */ document.getElementById("theme-toggle").click(); } },
    { id: "new-chat", group: "Action", icon: "💬", title: "Start a new chat", hint: "Ctrl+Shift+N",
      run: () => {
        ChatState.newChat();
        if (Router.active !== "chat") Router.go("chat");
        else window.dispatchEvent(new CustomEvent("samp:new-chat"));
        Toast.info("New chat started", `chat_id = ${ChatState.chatId}`);
      } },
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
// ─────────────────────────────────────────────────────────────────────────
// Native notifications — Tauri (Action Center) when running inside the
// desktop app, Web Notifications API as the browser fallback. Both
// paths require user permission once; Tauri grants it at install time
// so there's no popup.
// ─────────────────────────────────────────────────────────────────────────
const Notify = (() => {
  const isTauri = !!(globalThis.__TAURI__ && globalThis.__TAURI__.core);
  let webPermAsked = false;

  // Dedup: don't re-notify the same agent reply / approval twice if
  // the dashboard is already focused.
  const seen = new Set();
  function key(kind, id) { return `${kind}:${id}`; }

  async function ensureWebPermission() {
    if (typeof Notification === "undefined") return false;
    if (Notification.permission === "granted") return true;
    if (Notification.permission === "denied") return false;
    if (webPermAsked) return Notification.permission === "granted";
    webPermAsked = true;
    try {
      const r = await Notification.requestPermission();
      return r === "granted";
    } catch { return false; }
  }

  async function show({ kind, title, body, correlation_id, actions }) {
    const k = correlation_id ? key(kind, correlation_id) : null;
    if (k && seen.has(k)) return;
    if (k) seen.add(k);

    if (isTauri) {
      try {
        await globalThis.__TAURI__.core.invoke("show_notification", {
          payload: { kind, title, body, correlation_id, actions: actions || [] },
        });
        return;
      } catch (e) { /* fall back to web */ }
    }
    if (!(await ensureWebPermission())) return;
    try {
      const n = new Notification(title, { body, tag: k || undefined });
      n.onclick = () => { window.focus(); n.close(); };
    } catch { /* swallow — notifications are advisory */ }
  }

  return { show, isTauri };
})();

// ─────────────────────────────────────────────────────────────────────────
// Tauri auto-updater status — only meaningful inside the desktop app.
// The Rust side emits `updater_status` events with shape
// { status, version?, notes?, error?, progress?, total? } as it
// progresses through check → download → install. We surface them as
// toasts so the user knows when a restart is needed.
// ─────────────────────────────────────────────────────────────────────────
function bindUpdaterStatus() {
  if (!(globalThis.__TAURI__ && globalThis.__TAURI__.event)) return;
  const { listen } = globalThis.__TAURI__.event;
  listen("updater_status", (evt) => {
    const p = (evt && evt.payload) || {};
    switch (p.status) {
      case "available":
        Toast.info("Update available",
          `oxenClaw ${p.version} — downloading in the background.`);
        break;
      case "installed":
        Toast.success("Update installed",
          `Restart oxenClaw to use ${p.version || "the new version"}.`,
          12000);
        break;
      case "no-update":
        Toast.info("Up to date", "You're on the latest version.");
        break;
      case "error":
        Toast.error("Updater error", p.error || "(no detail)");
        break;
      // "downloading" status fires per chunk; suppress to avoid spam.
    }
  });
}

function bindEventNotifications() {
  // Don't notify when the user is actively looking at the dashboard.
  function visible() { return document.visibilityState === "visible" && document.hasFocus(); }

  Rpc.onEvent((evt) => {
    if (!evt || typeof evt !== "object") return;
    const body = evt.body || {};
    const kind = body.kind || evt.kind;
    if (!kind) return;

    // Per-event-kind notification mapping. The Rpc bus delivers
    // every server-pushed EventFrame; we filter to the ones a
    // human alert is useful for.
    if (kind === "approval_requested" || kind === "approval") {
      // Always notify — explicit human approval is required.
      Notify.show({
        kind: "approval",
        title: "Approval needed",
        body: body.prompt || body.message || "Tool execution awaiting approval",
        correlation_id: body.id || body.request_id,
        actions: ["Approve", "Deny"],
      });
      return;
    }
    if (kind === "reply" || kind === "reply_complete") {
      if (visible()) return;        // user is already looking
      Notify.show({
        kind: "reply",
        title: `Reply from ${body.agent_id || "agent"}`,
        body: (body.text || body.summary || "").slice(0, 200) || "(empty reply)",
        correlation_id: body.message_id,
      });
      return;
    }
    if (kind === "cron_fired") {
      if (visible()) return;
      Notify.show({
        kind: "cron",
        title: "Cron job fired",
        body: body.prompt || body.id || "",
        correlation_id: body.id,
      });
    }
  });
}

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
  Router.register("mcp", MCPView);
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
  bindEventNotifications();
  bindUpdaterStatus();

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
      { type: "oxenclaw.canvas.eval", expression: expr },
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
