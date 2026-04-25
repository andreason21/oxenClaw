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

  function defaultUrl() {
    if (location.protocol === "file:") return "ws://127.0.0.1:7331";
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}`;
  }

  function pushLog(direction, payload, error) {
    log.unshift({ ts: new Date(), direction, payload, error });
    if (log.length > logCap) log.length = logCap;
  }

  function connect(target) {
    url = target || defaultUrl();
    if (ws) try { ws.close(); } catch {}
    onStateChange("connecting", url);
    ws = new WebSocket(url);
    ws.onopen = () => onStateChange("up", url);
    ws.onclose = () => onStateChange("down", url);
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

  return { connect, call, onEvent, setStateListener, defaultUrl, get url() { return url; }, log, RpcError };
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
      if (/^[-*]\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^[-*]\s+/.test(lines[i])) {
          items.push(`<li>${inline(lines[i].replace(/^[-*]\s+/, ""))}</li>`);
          i++;
        }
        out.push(`<ul>${items.join("")}</ul>`);
        continue;
      }
      if (/^\d+\.\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\d+\.\s+/.test(lines[i])) {
          items.push(`<li>${inline(lines[i].replace(/^\d+\.\s+/, ""))}</li>`);
          i++;
        }
        out.push(`<ol>${items.join("")}</ol>`);
        continue;
      }
      // Paragraph block: collect until blank line.
      const paraLines = [];
      while (i < lines.length && lines[i].trim() !== "" && !lines[i].startsWith("```") && !/^[-*]\s+/.test(lines[i])) {
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
        await safeRpc("chat.send", {
          channel: ChatState.channel,
          account_id: ChatState.accountId,
          chat_id: ChatState.chatId,
          thread_id: ChatState.threadId || null,
          text,
        });
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
        list.append(el("div", { class: "empty" }, "no cron jobs"));
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
        root.append(el("div", { class: "card" },
          el("h3", { class: "card-title" }, "No pending approvals"),
          el("div", { class: "card-meta" },
            "Tools wrapped via gated_tool() raise an approval request that lands here. " +
            "Approve or deny to unblock the agent."),
        ));
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
}

// ─────────────────────────────────────────────────────────────────────────
// Keyboard shortcuts
// ─────────────────────────────────────────────────────────────────────────
function bindShortcuts() {
  let pendingG = null;
  document.addEventListener("keydown", (e) => {
    const tag = (e.target && e.target.tagName) || "";
    const isInput = tag === "INPUT" || tag === "TEXTAREA" || (e.target && e.target.isContentEditable);
    if (e.ctrlKey && e.key === "/") { e.preventDefault(); toggleHelp(); return; }
    if (e.key === "Escape") { hideHelp(); return; }
    if (isInput) return;
    if (pendingG) {
      const target = { c: "chat", a: "agents", k: "channels", r: "cron", p: "approvals", s: "skills", g: "config" }[e.key];
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
// Bootstrap
// ─────────────────────────────────────────────────────────────────────────
function boot() {
  bindConnectionState();

  Router.register("chat", ChatView);
  Router.register("agents", AgentsView);
  Router.register("channels", ChannelsView);
  Router.register("cron", CronView);
  Router.register("approvals", ApprovalsView);
  Router.register("skills", SkillsView);
  Router.register("config", ConfigView);
  Router.register("rpc", RpcLogView);

  $("ws-url").value = Rpc.defaultUrl();
  $("reconnect-btn").onclick = () => Rpc.connect($("ws-url").value);
  $("cmd-help-close").onclick = hideHelp;

  bindShortcuts();

  Rpc.connect($("ws-url").value);
  Router.handleHash();

  // Periodic cheap refreshes for nav badges + approvals event channel.
  setInterval(refreshNavBadges, 4000);
  Rpc.onEvent(() => refreshNavBadges());
}

boot();
