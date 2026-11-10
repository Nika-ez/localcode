/* LocalCode — frontend application.
   Vanilla JS, no build step. Talks to the Flask API in server/api.py. */
(function () {
  "use strict";

  // ------------------------------------------------------------------ helpers
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function esc(value) {
    return String(value === null || value === undefined ? "" : value).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function fmtNum(value) {
    const n = Number(value || 0);
    if (!isFinite(n)) return "0";
    if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(1) + "B";
    if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (Math.abs(n) >= 1e4) return (n / 1e3).toFixed(1) + "k";
    return n.toLocaleString();
  }

  function fmtBytes(value) {
    let n = Number(value || 0);
    if (!isFinite(n) || n <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    return (i === 0 ? Math.round(n) : n.toFixed(n >= 100 ? 0 : 1)) + " " + units[i];
  }

  function fmtUsd(value) {
    const n = Number(value || 0);
    if (!isFinite(n) || n === 0) return "$0.00";
    if (n < 0.0001) return "$" + n.toFixed(6);
    if (n < 0.01) return "$" + n.toFixed(4);
    if (n < 1) return "$" + n.toFixed(3);
    return "$" + n.toFixed(2);
  }

  function fmtDuration(ms) {
    const sec = Math.max(0, Math.round(Number(ms || 0) / 1000));
    if (sec < 60) return sec + "s";
    const min = Math.floor(sec / 60);
    const rest = sec % 60;
    if (min < 60) return min + "m " + rest + "s";
    const hours = Math.floor(min / 60);
    return hours + "h " + (min % 60) + "m";
  }

  function parseDate(value) {
    if (!value) return null;
    const d = new Date(value);
    return isNaN(d.getTime()) ? null : d;
  }

  function timeAgo(value) {
    const d = parseDate(value);
    if (!d) return "";
    const diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 60) return "just now";
    if (diff < 3600) return Math.floor(diff / 60) + "m ago";
    if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
    if (diff < 86400 * 7) return Math.floor(diff / 86400) + "d ago";
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }

  function fmtClock(value) {
    const d = parseDate(value);
    if (!d) return "";
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }

  function debounce(fn, ms) {
    let timer = null;
    return function (...args) {
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(this, args), ms);
    };
  }

  function truncate(value, max) {
    const text = String(value === null || value === undefined ? "" : value);
    return text.length > max ? text.slice(0, max) + "…" : text;
  }

  function safeJson(text, fallback) {
    try { return JSON.parse(text); } catch (_) { return fallback; }
  }

  async function copyText(text) {
    const value = String(text === null || text === undefined ? "" : text);
    try {
      await navigator.clipboard.writeText(value);
      return true;
    } catch (_) {
      const area = document.createElement("textarea");
      area.value = value;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch (__) { ok = false; }
      area.remove();
      return ok;
    }
  }

  function errorMessage(err) {
    if (!err) return "Something went wrong.";
    if (err.message) return err.message;
    return String(err);
  }

  function shortModel(name) {
    const text = String(name || "No model");
    return truncate(text.replace(/\s*\(.*?\)\s*/g, " ").trim(), 42);
  }

  // -------------------------------------------------------------------- state
  const state = {
    booted: false,
    health: null,
    settings: null,
    system: null,
    provider: null,
    runtime: null,
    models: null,
    mcp: null,
    globalUsage: null,
    sessions: [],
    session: null,
    currentSessionId: null,
    messages: [],
    capabilities: null,
    price: null,
    usage: null,
    running: false,
    run: null,
    queue: [],
    approvals: [],
    questions: [],
    pendingAttachments: [],
    editTarget: null,
    regenerate: false,
    streamAbort: null,
    live: null,
    watchTimer: null,
    watchBusy: false,
    pollTimer: null,
    filesOpen: false,
    filesPath: ".",
    filesBusy: false,
    modal: null,
    palette: null,
    stickToBottom: true,
    lastSessionSignature: "",
    setupDismissed: false,
    downloadsTimer: null,
    approvalSignature: "",
    answerDrafts: {},
    sessionSearch: "",
    onboardingShown: false,
    downloadStates: {},
    downloadCards: {},
    downloadDismissed: {},
    downloadPanelTimer: null,
    engineInstallState: null,
    engineCard: null,
    slash: null,
    builtinTools: null,
  };

  const dom = {};

  // ------------------------------------------------------------------ markdown
  function renderMarkdown(text) {
    const source = String(text || "");
    if (!window.marked) {
      return "<p>" + esc(source).replace(/\n/g, "<br>") + "</p>";
    }
    try {
      window.marked.setOptions({ gfm: true, breaks: true });
      const html = window.marked.parse ? window.marked.parse(source) : window.marked(source);
      if (window.DOMPurify) {
        return window.DOMPurify.sanitize(html, { ADD_ATTR: ["target", "rel"] });
      }
      return html;
    } catch (_) {
      return "<p>" + esc(source).replace(/\n/g, "<br>") + "</p>";
    }
  }

  function enhanceMarkdown(root, withHighlight) {
    if (!root) return;
    $$("a", root).forEach((a) => {
      a.target = "_blank";
      a.rel = "noopener noreferrer";
    });
    $$("pre", root).forEach((pre) => {
      if (!pre.querySelector(":scope > .copy-code")) {
        const btn = document.createElement("button");
        btn.className = "copy-code";
        btn.type = "button";
        btn.textContent = "Copy";
        btn.addEventListener("click", async (event) => {
          event.preventDefault();
          event.stopPropagation();
          const code = pre.querySelector("code");
          const ok = await copyText((code || pre).innerText);
          btn.textContent = ok ? "Copied" : "Failed";
          setTimeout(() => { btn.textContent = "Copy"; }, 1300);
        });
        pre.appendChild(btn);
      }
    });
    if (withHighlight && window.hljs) {
      $$("pre code", root).forEach((code) => {
        if (code.dataset.highlighted) return;
        try {
          window.hljs.highlightElement(code);
          code.dataset.highlighted = "1";
        } catch (_) { /* ignore */ }
      });
    }
  }

  function fillMarkdown(el, text, withHighlight) {
    if (!el) return;
    el.innerHTML = renderMarkdown(text);
    enhanceMarkdown(el, withHighlight);
  }

  // -------------------------------------------------------------------- toast
  function ensureToastStack() {
    let stack = $("#toastRoot .toast-stack");
    if (!stack) {
      stack = document.createElement("div");
      stack.className = "toast-stack";
      $("#toastRoot").appendChild(stack);
    }
    return stack;
  }

  function toast(message, kind, timeout) {
    const stack = ensureToastStack();
    const el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.innerHTML = '<button class="close" type="button" aria-label="Dismiss">×</button><span></span>';
    el.querySelector("span").textContent = String(message || "");
    el.querySelector("button").addEventListener("click", () => el.remove());
    stack.appendChild(el);
    const life = timeout === undefined ? 4200 : timeout;
    if (life > 0) setTimeout(() => { if (el.isConnected) el.remove(); }, life);
    return el;
  }

  // --------------------------------------------------------- download popup
  // Bottom-right cards for model downloads and engine installs. Active items
  // show live progress; finished items stay on screen until dismissed.
  function downloadPanel() {
    let panel = $("#downloadRoot .download-panel");
    if (!panel) {
      panel = document.createElement("div");
      panel.className = "download-panel";
      $("#downloadRoot").appendChild(panel);
    }
    return panel;
  }

  function downloadDismiss(key) {
    state.downloadDismissed[key] = true;
    if (key === "engine") { state.engineCard = null; }
    else { delete state.downloadCards[key]; }
    renderDownloadPanel();
  }

  function engineInstallCard(install) {
    const status = install && install.status ? install.status : "idle";
    if (status === "idle") return null;
    const active = status === "starting" || status === "working";
    return {
      key: "engine",
      kind: status === "complete" ? "done" : (status === "error" ? "bad" : "live"),
      name: "LocalCode engine (llama.cpp)",
      status,
      sub: install.error ? install.error : (install.message || ""),
      percent: install.percent || 0,
      active,
    };
  }

  function jobCard(job) {
    const status = job.status || "queued";
    const active = status === "queued" || status === "downloading";
    const kind = status === "complete" ? "done" : (status === "error" ? "bad" : "live");
    const speed = job.speed ? fmtBytes(job.speed) + "/s" : "";
    const eta = job.eta ? " · " + fmtDuration(job.eta * 1000) + " left" : "";
    const sub = status === "complete"
      ? "Downloaded and ready to load."
      : (status === "error"
        ? (job.error || "Download failed.")
        : (status === "cancelled"
          ? "Cancelled."
          : fmtBytes(job.downloaded) + " / " + fmtBytes(job.total) + " · " + speed + eta));
    return {
      key: job.id, kind, name: job.name || "Model", status, sub,
      percent: Number(job.percent || 0), active, job,
    };
  }

  function downloadFootHtml(card) {
    if (card.active) {
      return '<div class="dc-foot"><span class="dc-pct">' + esc(Math.round(Number(card.percent || 0)) + "%") + "</span>" +
        '<div class="dc-btns">' +
        (card.job
          ? '<button class="ghost-btn small" type="button" data-dl-cancel="' + esc(card.key) + '">Cancel</button>'
          : "") +
        '<button class="ghost-btn small" type="button" data-dl-dismiss="' + esc(card.key) + '">Hide</button>' +
        "</div></div>";
    }
    return '<div class="dc-foot"><span></span><div class="dc-btns">' +
      (card.kind === "done" && card.job
        ? '<button class="primary-btn small" type="button" data-dl-load="' + esc(card.key) + '">Load</button>'
        : "") +
      '<button class="ghost-btn small" type="button" data-dl-dismiss="' + esc(card.key) + '">Dismiss</button>' +
      "</div></div>";
  }

  function downloadCardHtml(card) {
    const kindClass = card.kind === "done" ? " done" : (card.kind === "bad" ? " bad" : "");
    const percent = Math.max(0, Math.min(100, Number(card.percent || 0)));
    const progress = card.active || card.kind === "bad"
      ? '<div class="progress"><div style="width:' + percent + '%"></div></div>'
      : "";
    return '<div class="download-card' + kindClass + '" data-dl-card="' + esc(card.key) + '" data-mode="' +
      esc(card.active ? "active" : card.kind) + '">' +
      '<div class="dc-head"><span class="dc-name">' + esc(card.name) + "</span>" +
      '<span class="dc-status">' + esc(card.status) + "</span>" +
      '<button class="dc-close" type="button" data-dl-dismiss="' + esc(card.key) + '" aria-label="Dismiss">×</button></div>' +
      '<div class="dc-sub' + (card.kind === "bad" ? " bad" : "") + '">' + esc(card.sub) + "</div>" +
      progress + downloadFootHtml(card) + "</div>";
  }

  function bindDownloadCardButtons(el, key) {
    el.querySelectorAll("[data-dl-dismiss]").forEach((button) => {
      button.addEventListener("click", () => downloadDismiss(key));
    });
    el.querySelectorAll("[data-dl-cancel]").forEach((button) => {
      button.addEventListener("click", async () => {
        try {
          await API.models.cancel(key);
          toast("Download cancelled", "ok");
        } catch (err) {
          toast(errorMessage(err), "error");
        }
      });
    });
    el.querySelectorAll("[data-dl-load]").forEach((button) => {
      button.addEventListener("click", () => openModels("installed"));
    });
  }

  function updateDownloadCardElement(el, card) {
    el.classList.toggle("done", card.kind === "done");
    el.classList.toggle("bad", card.kind === "bad");
    const mode = card.active ? "active" : card.kind;
    if (el.dataset.mode !== mode) {
      el.dataset.mode = mode;
      const foot = el.querySelector(".dc-foot");
      if (foot) {
        foot.innerHTML = downloadFootHtml(card);
        bindDownloadCardButtons(el, card.key);
      }
    }
    const status = el.querySelector(".dc-status");
    if (status) status.textContent = card.status;
    const sub = el.querySelector(".dc-sub");
    if (sub) {
      sub.textContent = card.sub;
      sub.classList.toggle("bad", card.kind === "bad");
    }
    const pct = el.querySelector(".dc-pct");
    if (pct) pct.textContent = Math.round(Number(card.percent || 0)) + "%";
    const bar = el.querySelector(".progress > div");
    if (bar) bar.style.width = Math.max(0, Math.min(100, Number(card.percent || 0))) + "%";
    const progress = el.querySelector(".progress");
    if (progress) progress.style.display = (card.active || card.kind === "bad") ? "" : "none";
  }

  function renderDownloadPanel() {
    const panel = downloadPanel();
    const cards = [];
    if (state.engineCard) cards.push(state.engineCard);
    for (const key of Object.keys(state.downloadCards)) cards.push(state.downloadCards[key]);
    if (!cards.length) { panel.innerHTML = ""; return; }
    const byKey = {};
    for (const card of cards) byKey[card.key] = card;
    const seen = new Set();
    panel.querySelectorAll("[data-dl-card]").forEach((el) => {
      const card = byKey[el.dataset.dlCard];
      if (!card) { el.remove(); return; }
      seen.add(card.key);
      updateDownloadCardElement(el, card);
    });
    for (const card of cards) {
      if (seen.has(card.key)) continue;
      const holder = document.createElement("div");
      holder.innerHTML = downloadCardHtml(card);
      const node = holder.firstElementChild;
      panel.appendChild(node);
      bindDownloadCardButtons(node, card.key);
    }
  }

  function updateDownloadPanel(jobs) {
    const terminal = { complete: true, error: true, cancelled: true };
    const seen = {};
    for (const job of jobs || []) {
      seen[job.id] = true;
      if (state.downloadDismissed[job.id]) continue;
      const prev = state.downloadStates[job.id];
      state.downloadStates[job.id] = job.status;
      if (prev === undefined && terminal[job.status]) continue; // old history
      state.downloadCards[job.id] = jobCard(job);
    }
    for (const key of Object.keys(state.downloadCards)) {
      if (!seen[key]) delete state.downloadCards[key];
    }
    for (const key of Object.keys(state.downloadDismissed)) {
      if (key !== "engine" && !seen[key]) delete state.downloadDismissed[key];
    }
    renderDownloadPanel();
  }

  function updateEngineInstallPanel(install) {
    const status = install && install.status ? install.status : "idle";
    const prev = state.engineInstallState;
    state.engineInstallState = status;
    if (status === "idle") {
      state.engineCard = null;
    } else if (!state.downloadDismissed.engine) {
      if ((status === "complete" || status === "error") &&
          prev !== "starting" && prev !== "working") {
        state.engineCard = null; // finished before this session
      } else {
        state.engineCard = engineInstallCard(install);
      }
    }
    renderDownloadPanel();
  }

  async function pollDownloads() {
    if (document.hidden) return;
    try {
      const data = await API.models.downloads();
      updateDownloadPanel(data.downloads || []);
    } catch (_) { /* server may be busy */ }
    try {
      const runtime = await API.runtime.status();
      updateEngineInstallPanel((runtime && runtime.install) || {});
    } catch (_) { /* ignore */ }
  }

  function startDownloadPolling() {
    if (state.downloadPanelTimer) clearInterval(state.downloadPanelTimer);
    pollDownloads();
    state.downloadPanelTimer = setInterval(pollDownloads, 1600);
  }

  // ------------------------------------------------------------------- modals
  function openModal(options) {
    const opts = options || {};
    const root = $("#modalRoot");
    root.innerHTML = "";
    const backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop";
    backdrop.innerHTML =
      '<div class="modal ' + (opts.size || "") + '" role="dialog" aria-modal="true">' +
        '<div class="modal-head">' +
          '<div><div class="modal-title"></div><div class="modal-sub"></div></div>' +
          '<button class="icon-btn" type="button" data-modal-close aria-label="Close">×</button>' +
        "</div>" +
        '<div class="modal-tabs" data-modal-tabs hidden></div>' +
        '<div class="modal-body"></div>' +
        '<div class="modal-foot" hidden></div>' +
      "</div>";
    root.appendChild(backdrop);
    root.classList.add("open");
    const modal = $(".modal", backdrop);
    $(".modal-title", modal).textContent = opts.title || "";
    const subEl = $(".modal-sub", modal);
    if (opts.sub) subEl.textContent = opts.sub; else subEl.hidden = true;
    const body = $(".modal-body", modal);
    const foot = $(".modal-foot", modal);
    if (opts.body) body.innerHTML = opts.body;
    if (opts.foot) { foot.innerHTML = opts.foot; foot.hidden = false; }
    const tabs = $(".modal-tabs", modal);
    if (opts.tabs && opts.tabs.length) {
      tabs.hidden = false;
      tabs.innerHTML = opts.tabs.map((tab) =>
        '<button class="tab-btn' + (tab.active ? " active" : "") + '" type="button" data-tab="' +
        esc(tab.id) + '">' + esc(tab.label) + "</button>"
      ).join("");
    }
    const api = { root, backdrop, modal, body, foot, tabs, opts, close: () => closeModal() };
    state.modal = api;

    $("[data-modal-close]", modal).addEventListener("click", () => closeModal());
    backdrop.addEventListener("mousedown", (event) => {
      if (event.target === backdrop) closeModal();
    });
    if (opts.onMount) {
      try { opts.onMount(api); } catch (err) { console.error(err); }
    }
    return api;
  }

  function closeModal() {
    const root = $("#modalRoot");
    const previous = state.modal;
    root.innerHTML = "";
    root.classList.remove("open");
    state.modal = null;
    if (previous && previous.opts && previous.opts.onClose) {
      try { previous.opts.onClose(); } catch (_) { /* ignore */ }
    }
  }

  function modalBody(html) {
    if (state.modal) state.modal.body.innerHTML = html;
  }

  function modalFoot(html) {
    if (!state.modal) return;
    state.modal.foot.innerHTML = html || "";
    state.modal.foot.hidden = !html;
  }

  function miniDialog(options) {
    const opts = options || {};
    return new Promise((resolve) => {
      const wrap = document.createElement("div");
      wrap.className = "modal-backdrop";
      wrap.style.zIndex = "300";
      wrap.innerHTML =
        '<div class="modal small" role="dialog" aria-modal="true">' +
          '<div class="modal-head"><div><div class="modal-title"></div></div>' +
          '<button class="icon-btn" type="button" data-x aria-label="Close">×</button></div>' +
          '<div class="modal-body"><div class="mini-dialog-body">' + (opts.body || "") + "</div></div>" +
          '<div class="modal-foot"></div>' +
        "</div>";
      document.body.appendChild(wrap);
      $(".modal-title", wrap).textContent = opts.title || "";
      const foot = $(".modal-foot", wrap);
      let settled = false;
      const done = (value) => {
        if (settled) return;
        settled = true;
        wrap.remove();
        resolve(value);
      };
      (opts.buttons || []).forEach((button) => {
        const el = document.createElement("button");
        el.type = "button";
        el.className = button.kind === "primary" ? "primary-btn" :
          (button.kind === "danger" ? "danger-btn" : "ghost-btn");
        el.textContent = button.label;
        el.addEventListener("click", () => {
          if (button.collect) {
            const input = $("[data-dialog-input]", wrap);
            done(input ? input.value : "");
          } else {
            done(button.value);
          }
        });
        foot.appendChild(el);
      });
      $("[data-x]", wrap).addEventListener("click", () => done(null));
      wrap.addEventListener("mousedown", (event) => { if (event.target === wrap) done(null); });
      const first = $("[data-dialog-input]", wrap) || $("button", foot);
      if (first) setTimeout(() => { try { first.focus(); } catch (_) {} }, 30);
      wrap.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && opts.submitOnEnter !== false) {
          const input = $("[data-dialog-input]", wrap);
          if (input && (input.tagName !== "TEXTAREA" || event.ctrlKey)) {
            event.preventDefault();
            const btn = $$("button", foot).find((b) => b.classList.contains("primary-btn"));
            if (btn) btn.click();
          }
        }
      });
    });
  }

  function confirmDialog(title, message, confirmText, danger) {
    return miniDialog({
      title,
      body: '<p>' + esc(message) + "</p>",
      buttons: [
        { label: "Cancel", value: false, kind: "ghost" },
        { label: confirmText || "Confirm", value: true, kind: danger ? "danger" : "primary" },
      ],
    }).then((value) => value === true);
  }

  function promptValue(title, label, value, multiline) {
    return new Promise((resolve) => {
      const body = '<label class="tiny muted">' + esc(label || "") + "</label>" +
        (multiline
          ? '<textarea class="textarea" data-dialog-input rows="5">' + esc(value || "") + "</textarea>"
          : '<input class="input" data-dialog-input type="text" value="' + esc(value || "") + '">');
      miniDialog({
        title,
        body,
        submitOnEnter: !multiline,
        buttons: [
          { label: "Cancel", value: null, kind: "ghost" },
          { label: "Save", value: "__collect__", kind: "primary", collect: true },
        ],
      }).then((result) => {
        if (result === null || result === undefined) resolve(null);
        else resolve(String(result));
      });
    });
  }

  // --------------------------------------------------------------------- boot
  function cacheDom() {
    const ids = [
      "sidebar", "collapseSidebar", "showSidebar", "newChatBtn", "sessionSearch", "sessionList",
      "openModelsBtn", "openMcpBtn", "openSettingsBtn", "engineDot", "engineMiniDot", "engineMiniText",
      "mobileMenu", "sessionTitle", "sessionSub", "modelPill", "modelDot", "modelPillText",
      "filesToggleBtn", "toolsToggleBtn", "paletteBtn", "sessionMenuBtn", "chat", "chatScroll", "welcome", "setupCard",
      "setupCardText", "setupCardBtn", "suggestions", "welcomeWorkspace", "welcomeModelsBtn",
      "messages", "queueStrip", "contextStrip", "contextText", "contextPct", "contextFill",
      "composer", "composerInput", "attachFileBtn", "approvalModeBtn", "approvalModeLabel",
      "composerModel", "stopBtn", "steerBtn", "sendBtn", "composerHint", "hintRight",
      "composerArea", "filesPanel", "refreshFiles", "closeFiles", "filesPath", "fileList", "workspacePath",
      "openWorkspaceFolder", "modalRoot", "approvalRoot", "toastRoot", "slashMenu",
    ];
    ids.forEach((id) => { dom[id] = document.getElementById(id); });
  }

  async function boot() {
    cacheDom();
    bindEvents();
    try {
      state.health = await API.health();
    } catch (err) {
      toast("Cannot reach the LocalCode server. Is it still running?", "error", 0);
    }
    await Promise.allSettled([
      loadSettings(),
      loadSystem(),
      loadModels(),
      loadMCP(),
      loadSessions(""),
      loadUsage(),
    ]);
    applyTheme();
    renderSessions();
    renderEngine();
    updateSetupCard();
    updateWelcomeWorkspace();
    updateApprovalMode();
    const last = localStorage.getItem("localcode.lastSession");
    if (last && state.sessions.some((s) => s.id === last)) {
      try { await selectSession(last, { initial: true }); }
      catch (_) { showWelcome(); }
    } else {
      showWelcome();
    }
    startPolling();
    startDownloadPolling();
    loadBuiltinTools().catch(() => {});
    state.booted = true;
    setTimeout(() => maybeOnboard(), 700);
    if (dom.composerInput) dom.composerInput.focus();
  }

  async function loadSettings() {
    try {
      const data = await API.settings.get();
      state.settings = data.settings || {};
      state.system = data.system || state.system;
      state.runtime = data.runtime || state.runtime;
      state.provider = data.provider || state.provider;
      applyTheme();
      updateApprovalMode();
      if (dom.workspacePath) dom.workspacePath.textContent = state.settings.workspace || "";
      updateWelcomeWorkspace();
    } catch (err) {
      console.error("Settings failed", err);
      if (!state.settings) state.settings = {};
    }
  }

  async function loadSystem() {
    try {
      state.system = await API.system.info();
      if (dom.workspacePath) dom.workspacePath.textContent = state.system.workspace || "";
      updateWelcomeWorkspace();
    } catch (err) { console.error("System info failed", err); }
  }

  async function loadModels() {
    try {
      const data = await API.models.list();
      state.models = data;
      state.runtime = data.runtime || state.runtime;
      state.provider = data.provider || state.provider;
      state.capabilities = data.capabilities || state.capabilities;
      renderEngine();
      updateSetupCard();
    } catch (err) {
      console.error("Models failed", err);
    }
  }

  async function loadMCP() {
    try {
      state.mcp = await API.mcp.status();
    } catch (err) { console.error("MCP status failed", err); }
  }

  async function loadSessions(query) {
    try {
      const data = await API.sessions.list(query === undefined ? state.sessionSearch : query);
      state.sessions = data.sessions || [];
      renderSessions();
    } catch (err) {
      console.error("Sessions failed", err);
    }
  }

  async function loadUsage() {
    try {
      state.globalUsage = await API.usage.get();
      updateHint();
    } catch (err) { console.error("Usage failed", err); }
  }

  function startPolling() {
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = setInterval(async () => {
      if (document.hidden) return;
      try {
        const runtime = await API.runtime.status();
        const changed = !state.runtime ||
          runtime.ready !== state.runtime.ready ||
          runtime.loading !== state.runtime.loading ||
          runtime.model_id !== state.runtime.model_id ||
          runtime.installed !== state.runtime.installed;
        state.runtime = runtime;
        if (state.models) state.models.runtime = runtime;
        if (changed) {
          renderEngine();
          updateSetupCard();
          if (runtime.ready) loadModels();
        }
      } catch (_) { /* server may be busy */ }
    }, 9000);
  }

  // ------------------------------------------------------------------ engine
  function renderEngine() {
    const runtime = state.runtime || (state.models && state.models.runtime) || {};
    const installed = (state.models && state.models.installed) || [];
    let text = "No model loaded";
    let dotClass = "";
    let mini = "Engine not installed";
    if (runtime.ready) {
      text = shortModel(runtime.model_name);
      dotClass = "ok";
      const ctx = runtime.context_size ? Math.round(runtime.context_size / 1024) + "k ctx" : "";
      mini = shortModel(runtime.model_name) + (ctx ? " · " + ctx : "");
    } else if (runtime.loading) {
      text = "Loading model…";
      dotClass = "busy";
      mini = "Loading " + shortModel(runtime.model_name || "model") + "…";
    } else if (runtime.error) {
      text = "Engine error";
      dotClass = "bad";
      mini = truncate(runtime.error, 70);
    } else if (runtime.installed) {
      text = installed.length ? "Load a model" : "Download a model";
      dotClass = installed.length ? "busy" : "";
      mini = installed.length ? "No model loaded" : "No models installed";
    }
    if (dom.modelPillText) dom.modelPillText.textContent = text;
    if (dom.modelDot) dom.modelDot.className = "dot " + dotClass;
    if (dom.engineDot) dom.engineDot.className = "dot " + dotClass;
    if (dom.engineMiniDot) dom.engineMiniDot.className = "dot " + dotClass;
    if (dom.engineMiniText) dom.engineMiniText.textContent = mini;
    if (dom.composerModel) {
      dom.composerModel.textContent = runtime.ready
        ? shortModel(runtime.model_name) + (runtime.vision ? " · vision" : "")
        : (installed.length ? "No model loaded" : "No model");
    }
    if (dom.approvalModeBtn) dom.approvalModeBtn.title = "Tool approval mode: " +
      ((state.settings && state.settings.approval_mode) === "auto" ? "auto-run safe + write tools" : "ask first");
  }

  function updateApprovalMode() {
    const mode = (state.settings && state.settings.approval_mode) || "ask";
    if (dom.approvalModeLabel) dom.approvalModeLabel.textContent = mode === "auto" ? "Auto-run" : "Ask first";
    if (dom.approvalModeBtn) dom.approvalModeBtn.classList.toggle("active", mode === "auto");
  }

  function updateSetupCard() {
    if (!dom.setupCard) return;
    const runtime = state.runtime || (state.models && state.models.runtime) || {};
    const installed = (state.models && state.models.installed) || [];
    const welcomeVisible = dom.welcome && !dom.welcome.hidden;
    let show = welcomeVisible && !state.setupDismissed;
    let text = "";
    if (!runtime.installed) {
      text = "Install the local llama.cpp engine to run models on this machine.";
    } else if (!installed.length) {
      text = "The engine is ready. Download a coding model to start your first chat.";
    } else if (!runtime.ready && !runtime.loading) {
      text = "A model is downloaded but not loaded. Load it to start chatting.";
    } else {
      show = false;
    }
    dom.setupCard.hidden = !show;
    if (text && dom.setupCardText) dom.setupCardText.textContent = text;
  }

  function updateWelcomeWorkspace() {
    const ws = (state.settings && state.settings.workspace) ||
      (state.system && state.system.workspace) || "";
    if (dom.welcomeWorkspace) dom.welcomeWorkspace.textContent = ws ? "Workspace: " + ws : "Workspace: —";
    if (dom.workspacePath) dom.workspacePath.textContent = ws;
  }

  function applyTheme() {
    const theme = (state.settings && state.settings.theme) || "dark";
    document.documentElement.dataset.theme = theme === "light" ? "light" : "dark";
    const meta = $('meta[name="color-scheme"]');
    if (meta) meta.setAttribute("content", theme === "light" ? "light" : "dark");
  }

  // ----------------------------------------------------------------- sessions
  function sessionGroupLabel(iso) {
    const d = parseDate(iso);
    if (!d) return "Older";
    const now = new Date();
    const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const t = d.getTime();
    if (t >= startToday) return "Today";
    if (t >= startToday - 86400000) return "Yesterday";
    if (t >= startToday - 7 * 86400000) return "Previous 7 days";
    return "Older";
  }

  function renderSessions() {
    if (!dom.sessionList) return;
    if (!state.sessions.length) {
      dom.sessionList.innerHTML = '<div class="session-empty">' +
        (state.sessionSearch ? "No chats match your search." : "No chats yet. Start one below.") + "</div>";
      return;
    }
    const groups = new Map();
    state.sessions.forEach((session) => {
      const label = sessionGroupLabel(session.updated_at);
      if (!groups.has(label)) groups.set(label, []);
      groups.get(label).push(session);
    });
    const order = ["Today", "Yesterday", "Previous 7 days", "Older"];
    let html = "";
    order.forEach((label) => {
      const items = groups.get(label);
      if (!items || !items.length) return;
      html += '<div class="session-group-label">' + esc(label) + "</div>";
      items.forEach((session) => {
        const active = session.id === state.currentSessionId;
        const count = Number(session.message_count || 0);
        const meta = timeAgo(session.updated_at) + (count ? " · " + count + " message" + (count === 1 ? "" : "s") : "");
        html += '<div class="session-item' + (active ? " active" : "") + '" data-session-id="' + esc(session.id) +
          '" title="' + esc(session.preview || session.title || "") + '">' +
          '<div class="session-title">' + esc(session.title || "New chat") + "</div>" +
          '<div class="session-meta">' + esc(meta) + "</div>" +
          (session.pinned ? '<span class="session-pin" title="Pinned">★</span>' : "") +
          "</div>";
      });
    });
    dom.sessionList.innerHTML = html;
  }

  function showWelcome() {
    if (dom.welcome) dom.welcome.hidden = false;
    if (dom.messages) dom.messages.innerHTML = "";
    if (dom.sessionTitle) dom.sessionTitle.textContent = "New chat";
    if (dom.sessionSub) dom.sessionSub.textContent = "Your machine. Your model. Your code.";
    if (dom.queueStrip) dom.queueStrip.innerHTML = "";
    if (dom.contextStrip) dom.contextStrip.hidden = true;
    state.stickToBottom = true;
    updateSetupCard();
    updateWelcomeWorkspace();
    if (dom.chatScroll) dom.chatScroll.scrollTop = 0;
  }

  function newChat() {
    if (state.running) {
      detachStream();
      toast("The agent keeps working in the previous chat.", "ok");
    }
    state.currentSessionId = null;
    state.session = null;
    state.messages = [];
    state.capabilities = null;
    state.price = null;
    state.usage = null;
    state.run = null;
    state.queue = [];
    state.approvals = [];
    state.questions = [];
    state.editTarget = null;
    state.regenerate = false;
    state.pendingAttachments = [];
    state.live = null;
    state.running = false;
    localStorage.removeItem("localcode.lastSession");
    stopWatch();
    closeApprovals();
    updateRunningUI();
    renderQueue();
    renderPendingAttachments();
    showWelcome();
    renderSessions();
    updateHint();
    if (dom.composerInput) { dom.composerInput.value = ""; dom.composerInput.focus(); }
    autoSizeComposer();
  }

  async function selectSession(sessionId, options) {
    const opts = options || {};
    if (!sessionId) return;
    if (state.running && state.live && state.currentSessionId !== sessionId) {
      detachStream();
    }
    try {
      const data = await API.sessions.get(sessionId);
      applySessionPayload(data, opts);
      localStorage.setItem("localcode.lastSession", sessionId);
    } catch (err) {
      toast(errorMessage(err), "error");
      throw err;
    }
  }

  async function refreshCurrentSession(options) {
    if (!state.currentSessionId) return;
    try {
      const data = await API.sessions.get(state.currentSessionId);
      applySessionPayload(data, options || { quiet: true });
    } catch (_) { /* ignore transient errors */ }
  }

  function applySessionPayload(data, options) {
    const opts = options || {};
    const session = data.session || {};
    const previousDistance = dom.chatScroll
      ? dom.chatScroll.scrollHeight - dom.chatScroll.scrollTop - dom.chatScroll.clientHeight
      : 0;
    state.session = session;
    state.currentSessionId = session.id;
    state.messages = data.messages || [];
    state.capabilities = data.capabilities || null;
    state.price = data.price || null;
    state.usage = data.usage || null;
    state.approvals = data.approvals || [];
    state.questions = data.questions || [];
    state.run = data.run || { active: false };
    state.running = !!state.run.active;
    state.queue = state.run.queued || [];
    state.editTarget = null;
    state.regenerate = false;
    state.lastSessionSignature = JSON.stringify({
      id: session.id, title: session.title, count: state.messages.length,
    });
    if (state.live && state.live.sessionId !== session.id) state.live = null;
    if (dom.welcome) dom.welcome.hidden = state.messages.length > 0;
    if (dom.sessionTitle) dom.sessionTitle.textContent = session.title || "New chat";
    if (dom.sessionSub) {
      const count = state.messages.length;
      dom.sessionSub.textContent = count
        ? count + " message" + (count === 1 ? "" : "s") + " · " + timeAgo(session.updated_at)
        : "Your machine. Your model. Your code.";
    }
    renderMessages();
    renderSessions();
    renderApprovals();
    renderQueue();
    updateRunningUI();
    updateHint();
    updateSetupCard();
    if (!opts.quiet) {
      if (state.running) startWatch();
      else stopWatch();
    } else if (state.running) {
      startWatch();
    }
    if (dom.chatScroll) {
      if (opts.preserveScroll) {
        dom.chatScroll.scrollTop = dom.chatScroll.scrollHeight - dom.chatScroll.clientHeight - previousDistance;
      } else if (state.stickToBottom) {
        dom.chatScroll.scrollTop = dom.chatScroll.scrollHeight;
      }
    }
  }

  function updateRunningUI() {
    if (!dom.sendBtn) return;
    const running = state.running;
    if (dom.stopBtn) dom.stopBtn.hidden = !running;
    if (dom.steerBtn) dom.steerBtn.hidden = !running;
    dom.sendBtn.title = running ? "Queue message (Ctrl+Enter to interrupt)" : "Send (Enter)";
    dom.sendBtn.disabled = false;
    if (dom.composerInput) {
      dom.composerInput.placeholder = running
        ? "The agent is working — type to queue a message…"
        : "Ask LocalCode to build something…";
    }
    if (dom.sessionSub && running && state.run) {
      const step = state.run.step ? " · step " + state.run.step : "";
      const elapsed = state.run.elapsed ? " · " + fmtDuration(state.run.elapsed * 1000) : "";
      dom.sessionSub.textContent = (state.run.phase || "working") + step + elapsed;
    }
  }

  // ------------------------------------------------------------------ hint
  function updateHint() {
    if (!dom.hintRight) return;
    const usage = (state.session && {
      total_tokens: state.session.total_tokens,
      cost_usd: state.session.total_cost_usd,
    }) || state.usage || { total_tokens: 0, cost_usd: 0 };
    const tokens = Number(usage.total_tokens || 0);
    const showCosts = !state.settings || state.settings.show_costs !== false;
    const bits = [fmtNum(tokens) + " tokens"];
    if (showCosts) bits.push(fmtUsd(usage.cost_usd || 0) + " hosted");
    if (state.running) bits.push("working…");
    dom.hintRight.textContent = bits.join(" · ");
  }

  // ----------------------------------------------------------------- context
  function updateContext(event) {
    if (!dom.contextStrip) return;
    const pct = Math.max(0, Math.min(100, Number(event.percent || 0)));
    dom.contextStrip.hidden = false;
    dom.contextText.textContent = "Context " + fmtNum(event.tokens || 0) + " / " +
      fmtNum(event.context_size || 0) + " tokens" + (event.unlimited ? " · auto-compacts" : "");
    dom.contextPct.textContent = pct.toFixed(1) + "%";
    dom.contextFill.style.width = pct + "%";
    dom.contextFill.classList.toggle("warn", pct >= 80);
  }

  function resetContext() {
    if (dom.contextStrip) dom.contextStrip.hidden = true;
    if (dom.contextFill) dom.contextFill.style.width = "0%";
  }

  // ------------------------------------------------------------------- events
  function bindEvents() {
    ensureComposerExtras();
    renderPendingAttachments();
    setupDragDrop();
    if (dom.approvalRoot) dom.approvalRoot.addEventListener("click", handleApprovalClick);
    if (dom.newChatBtn) dom.newChatBtn.addEventListener("click", newChat);
    if (dom.collapseSidebar) dom.collapseSidebar.addEventListener("click", () => {
      if (window.innerWidth <= 900) {
        document.body.classList.remove("sidebar-open");
      } else {
        document.body.classList.toggle("sidebar-collapsed");
        localStorage.setItem("localcode.sidebarCollapsed",
          document.body.classList.contains("sidebar-collapsed") ? "1" : "0");
      }
    });
    if (dom.showSidebar) dom.showSidebar.addEventListener("click", () => {
      document.body.classList.remove("sidebar-collapsed");
      localStorage.removeItem("localcode.sidebarCollapsed");
    });
    if (dom.mobileMenu) dom.mobileMenu.addEventListener("click", () => {
      document.body.classList.toggle("sidebar-open");
    });
    if (localStorage.getItem("localcode.sidebarCollapsed") === "1") {
      document.body.classList.add("sidebar-collapsed");
    }

    const search = debounce(() => {
      state.sessionSearch = dom.sessionSearch.value.trim();
      loadSessions(state.sessionSearch);
    }, 220);
    if (dom.sessionSearch) dom.sessionSearch.addEventListener("input", search);

    if (dom.sessionList) {
      dom.sessionList.addEventListener("click", (event) => {
        const item = event.target.closest("[data-session-id]");
        if (!item) return;
        document.body.classList.remove("sidebar-open");
        selectSession(item.dataset.sessionId).catch(() => {});
      });
      dom.sessionList.addEventListener("contextmenu", (event) => {
        const item = event.target.closest("[data-session-id]");
        if (!item) return;
        event.preventDefault();
        selectSession(item.dataset.sessionId)
          .then(() => openSessionMenu())
          .catch(() => {});
      });
    }

    if (dom.openModelsBtn) dom.openModelsBtn.addEventListener("click", () => openModels("browse"));
    if (dom.openMcpBtn) dom.openMcpBtn.addEventListener("click", () => openMCP());
    if (dom.openSettingsBtn) dom.openSettingsBtn.addEventListener("click", () => openSettings());
    if (dom.modelPill) dom.modelPill.addEventListener("click", () => openModels("installed"));
    if (dom.welcomeModelsBtn) dom.welcomeModelsBtn.addEventListener("click", () => openModels("browse"));
    if (dom.setupCardBtn) dom.setupCardBtn.addEventListener("click", () => openModels(
      (state.runtime && state.runtime.installed) ? "browse" : "runtime"
    ));

    if (dom.sessionTitle) {
      dom.sessionTitle.addEventListener("click", () => {
        if (!state.currentSessionId) return;
        promptValue("Rename chat", "Chat title", state.session ? state.session.title : "")
          .then(async (value) => {
            if (value === null) return;
            const title = value.trim();
            if (!title) return;
            await API.sessions.patch(state.currentSessionId, { title });
            if (state.session) state.session.title = title;
            if (dom.sessionTitle) dom.sessionTitle.textContent = title;
            await loadSessions();
            toast("Chat renamed", "ok");
          })
          .catch((err) => toast(errorMessage(err), "error"));
      });
    }

    if (dom.filesToggleBtn) dom.filesToggleBtn.addEventListener("click", () => toggleFiles());
    if (dom.closeFiles) dom.closeFiles.addEventListener("click", () => toggleFiles(false));
    if (dom.refreshFiles) dom.refreshFiles.addEventListener("click", () => loadFiles(state.filesPath));
    if (dom.openWorkspaceFolder) dom.openWorkspaceFolder.addEventListener("click", () => {
      API.system.openFolder("workspace").catch((err) => toast(errorMessage(err), "error"));
    });
    if (dom.sessionMenuBtn) dom.sessionMenuBtn.addEventListener("click", openSessionMenu);

    if (dom.composerInput) {
      dom.composerInput.addEventListener("input", () => {
        autoSizeComposer();
        if (dom.composerInput.value.trim()) state.regenerate = false;
        updateSendAvailability();
        renderSlash();
      });
      dom.composerInput.addEventListener("keydown", (event) => {
        if (state.slash) {
          if (event.key === "ArrowDown") {
            event.preventDefault();
            state.slash._index = Math.min(state.slash._items.length - 1, state.slash._index + 1);
            highlightSlash();
            return;
          }
          if (event.key === "ArrowUp") {
            event.preventDefault();
            state.slash._index = Math.max(0, state.slash._index - 1);
            highlightSlash();
            return;
          }
          if (event.key === "Enter" || event.key === "Tab") {
            event.preventDefault();
            applySlash();
            return;
          }
          if (event.key === "Escape") {
            event.preventDefault();
            closeSlash();
            return;
          }
        }
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          sendMessage(event.ctrlKey || event.metaKey);
        }
      });
      dom.composerInput.addEventListener("paste", handlePaste);
    }
    if (dom.toolsToggleBtn) dom.toolsToggleBtn.addEventListener("click", () => openTools());
    if (dom.paletteBtn) dom.paletteBtn.addEventListener("click", () => openPalette());
    if (dom.sendBtn) dom.sendBtn.addEventListener("click", () => sendMessage(false));
    if (dom.steerBtn) dom.steerBtn.addEventListener("click", () => sendMessage(true));
    if (dom.stopBtn) dom.stopBtn.addEventListener("click", stopRun);
    if (dom.approvalModeBtn) dom.approvalModeBtn.addEventListener("click", toggleApprovalMode);
    if (dom.attachFileBtn) dom.attachFileBtn.addEventListener("click", openAttachMenu);
    if (dom.suggestions) {
      dom.suggestions.addEventListener("click", (event) => {
        const button = event.target.closest("[data-prompt]");
        if (!button) return;
        if (dom.composerInput) {
          dom.composerInput.value = button.dataset.prompt || "";
          autoSizeComposer();
          dom.composerInput.focus();
        }
      });
    }
    if (dom.chatScroll) {
      dom.chatScroll.addEventListener("scroll", () => {
        const el = dom.chatScroll;
        state.stickToBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
      });
    }
    if (dom.messages) {
      dom.messages.addEventListener("click", handleMessageAction);
    }
    if (dom.queueStrip) {
      dom.queueStrip.addEventListener("click", (event) => {
        const button = event.target.closest("[data-unqueue]");
        if (!button) return;
        removeQueued(button.dataset.unqueue);
      });
    }

    document.addEventListener("keydown", handleGlobalKey);
    window.addEventListener("beforeunload", () => {
      if (state.streamAbort) {
        try { state.streamAbort.abort(); } catch (_) { /* ignore */ }
      }
    });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && state.currentSessionId && state.running && !state.live) {
        refreshCurrentSession({ quiet: true, preserveScroll: true });
      }
    });
  }

  function autoSizeComposer() {
    const input = dom.composerInput;
    if (!input) return;
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 220) + "px";
  }

  function handleGlobalKey(event) {
    const mod = event.ctrlKey || event.metaKey;
    if (mod && event.key.toLowerCase() === "k") {
      event.preventDefault();
      openPalette();
      return;
    }
    if (mod && event.key.toLowerCase() === "n") {
      event.preventDefault();
      newChat();
      return;
    }
    if (mod && event.key === ",") {
      event.preventDefault();
      openSettings();
      return;
    }
    if (mod && event.shiftKey && event.key.toLowerCase() === "f") {
      event.preventDefault();
      toggleFiles();
      return;
    }
    if (event.key === "Escape") {
      if (state.palette) { closePalette(); return; }
      if (state.modal) { closeModal(); return; }
      const overlay = $("#approvalRoot .approval-backdrop");
      if (overlay) return;
    }
    if (event.key === "/" && document.activeElement !== dom.composerInput &&
        document.activeElement !== dom.sessionSearch &&
        !(document.activeElement && document.activeElement.tagName === "INPUT") &&
        !(document.activeElement && document.activeElement.tagName === "TEXTAREA")) {
      event.preventDefault();
      if (dom.sessionSearch) dom.sessionSearch.focus();
    }
  }

  // ----------------------------------------------------------------- messages
  function isLastAssistant(index) {
    for (let i = state.messages.length - 1; i >= 0; i -= 1) {
      if (state.messages[i].role === "assistant") return i === index;
    }
    return false;
  }

  function attachmentsHtml(list, removable) {
    if (!list || !list.length) return "";
    return '<div class="attachments">' + list.map((att) => {
      const kind = att.kind || att.ext || "file";
      const size = att.size ? fmtBytes(att.size) : "";
      const name = att.name || "attachment";
      const inner = (att.kind === "image" && att.url
        ? '<img src="' + esc(att.url) + '" alt="">'
        : "") +
        '<span class="kind">' + esc(String(kind).slice(0, 8)) + "</span>" +
        '<a href="' + esc(att.url || "#") + '" target="_blank" rel="noopener">' + esc(name) + "</a>" +
        (size ? '<span class="muted tiny">' + esc(size) + "</span>" : "");
      if (removable) {
        return '<span class="attach-chip" data-attachment-id="' + esc(att.id) + '">' + inner +
          '<button type="button" data-remove-attachment="' + esc(att.id) + '" title="Remove">×</button></span>';
      }
      return '<span class="attach-chip">' + inner + "</span>";
    }).join("") + "</div>";
  }

  function reasoningHtml(message) {
    if (!message.reasoning) return "";
    if (state.settings && state.settings.show_reasoning === false) return "";
    return '<details class="reasoning"><summary>Thinking · ' +
      fmtNum((message.reasoning || "").length) + " chars</summary>" +
      '<div class="reasoning-body">' + esc(message.reasoning) + "</div></details>";
  }

  function messageActionsHtml(message, lastAssistant) {
    const actions = [];
    actions.push('<button class="msg-action" type="button" data-copy-message="' + esc(message.id) + '">Copy</button>');
    if (message.role === "user") {
      actions.push('<button class="msg-action" type="button" data-edit-message="' + esc(message.id) + '">Edit</button>');
    }
    if (message.role === "assistant" && lastAssistant && !state.running) {
      actions.push('<button class="msg-action" type="button" data-regenerate="1">Regenerate</button>');
    }
    return '<div class="msg-actions">' + actions.join("") + "</div>";
  }

  function toolStatusLabel(status) {
    return ({
      ok: "done", error: "error", denied: "denied", running: "running",
      waiting: "waiting", streaming: "running", stopped: "stopped",
    })[status] || status || "done";
  }

  function toolArgSummary(name, args, raw) {
    if (!args || typeof args !== "object") {
      return truncate(raw || "", 180);
    }
    const keys = ["path", "command", "pattern", "query", "url", "question", "content", "old_string"];
    const bits = [];
    keys.forEach((key) => {
      if (args[key] !== undefined && args[key] !== null && String(args[key]).trim()) {
        let value = String(args[key]).replace(/\s+/g, " ").trim();
        if (key === "content" || key === "old_string") value = truncate(value, 70);
        bits.push(key + ": " + truncate(value, 120));
      }
    });
    return bits.length ? bits.join("  ·  ") : truncate(raw || "", 180);
  }

  function diffHtml(diff) {
    return '<div class="diff">' + String(diff || "").split("\n").map((line) => {
      if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("@@")) {
        return '<span class="meta">' + esc(line) + "</span>";
      }
      if (line.startsWith("+")) return '<span class="add">' + esc(line) + "</span>";
      if (line.startsWith("-")) return '<span class="del">' + esc(line) + "</span>";
      return esc(line);
    }).join("\n") + "</div>";
  }

  function toolCardHtml(message) {
    const status = message.status || "ok";
    const args = safeJson(message.tool_args, null);
    const summary = toolArgSummary(message.tool_name, args, message.tool_args);
    const meta = message.meta || {};
    const output = String(message.content || "");
    const diff = meta && meta.diff ? String(meta.diff) : "";
    let body = "";
    if (diff) {
      body = diffHtml(diff);
      if (output) {
        body += '<details class="mt-8"><summary>Tool output</summary><pre>' + esc(truncate(output, 20000)) + "</pre></details>";
      }
    } else if (output) {
      body = "<pre>" + esc(truncate(output, 24000)) + "</pre>";
    } else {
      body = '<span class="muted tiny">' + (status === "running" || status === "waiting"
        ? "Waiting for the tool…" : "No output") + "</span>";
    }
    const duration = meta && meta.duration_ms ? " · " + fmtDuration(meta.duration_ms) : "";
    return '<div class="tool-card">' +
      '<div class="tool-head">' +
        '<span class="tool-icon">⚙</span>' +
        '<span class="tool-name">' + esc(message.tool_name || "tool") + "</span>" +
        '<span class="tool-status ' + esc(status) + '">' + esc(toolStatusLabel(status)) + duration + "</span>" +
      "</div>" +
      (summary ? '<div class="tool-args">' + esc(summary) + "</div>" : "") +
      '<div class="tool-body">' + body + "</div>" +
      "</div>";
  }

  function messageHtml(message, index, lastAssistant) {
    const role = message.role;
    if (role === "tool") {
      return '<div class="msg tool" data-msg-id="' + esc(message.id) + '">' +
        '<div class="msg-avatar">⚙</div>' +
        '<div class="msg-main">' + toolCardHtml(message) + "</div>" +
        "</div>";
    }
    const isUser = role === "user";
    const time = fmtClock(message.created_at);
    const tokens = Number(message.tokens || 0);
    const cost = Number(message.cost_usd || 0);
    const showCosts = !state.settings || state.settings.show_costs !== false;
    const tokenBits = tokens ? fmtNum(tokens) + " tok" + (showCosts && cost ? " · " + fmtUsd(cost) : "") : "";
    let body = "";
    if (isUser) {
      body = '<div class="msg-body">' + esc(message.content || "") + "</div>";
    } else {
      const reasoning = reasoningHtml(message);
      let content = "";
      if (message.content) content = renderMarkdown(message.content);
      else if (message.status === "streaming") content = '<span class="typing"><i></i><i></i><i></i></span>';
      else content = '<span class="muted">No response.</span>';
      body = reasoning + '<div class="msg-body">' + content + "</div>";
    }
    return '<div class="msg ' + (isUser ? "user" : "assistant") + '" data-msg-id="' + esc(message.id) + '">' +
      '<div class="msg-avatar">' + (isUser ? "You" : "LC") + "</div>" +
      '<div class="msg-main">' +
        '<div class="msg-head">' +
          '<span class="msg-role">' + (isUser ? "You" : "LocalCode") + "</span>" +
          '<span class="msg-time" title="' + esc(message.model || "") + '">' + esc(time) + "</span>" +
          (tokenBits ? '<span class="msg-tokens">' + esc(tokenBits) + "</span>" : "") +
          (message.status === "stopped" ? '<span class="msg-tokens">stopped</span>' : "") +
          (message.status === "error" ? '<span class="msg-tokens" style="color:var(--danger)">error</span>' : "") +
          messageActionsHtml(message, lastAssistant) +
        "</div>" +
        body +
        attachmentsHtml(message.attachments, false) +
      "</div>" +
      "</div>";
  }

  function enhanceMessages() {
    if (!dom.messages) return;
    $$(".msg-body", dom.messages).forEach((el) => enhanceMarkdown(el, true));
    $$(".reasoning-body", dom.messages).forEach((el) => enhanceMarkdown(el, false));
  }

  function renderMessages() {
    if (!dom.messages) return;
    if (!state.messages.length) {
      dom.messages.innerHTML = "";
      if (dom.welcome) dom.welcome.hidden = false;
      return;
    }
    if (dom.welcome) dom.welcome.hidden = true;
    dom.messages.innerHTML = state.messages.map((message, index) =>
      messageHtml(message, index, isLastAssistant(index))
    ).join("");
    enhanceMessages();
  }

  function appendMessageElement(message) {
    if (!dom.messages) return null;
    if (dom.welcome) dom.welcome.hidden = true;
    const index = state.messages.findIndex((m) => m.id === message.id);
    const holder = document.createElement("div");
    holder.innerHTML = messageHtml(message, index, index === -1 ? false : isLastAssistant(index));
    const el = holder.firstElementChild;
    dom.messages.appendChild(el);
    enhanceMessages();
    if (state.stickToBottom) scrollChatBottom();
    return el;
  }

  function replaceMessageElement(message) {
    if (!dom.messages) return null;
    let el = dom.messages.querySelector('[data-msg-id="' + cssEscape(message.id) + '"]');
    const index = state.messages.findIndex((m) => m.id === message.id);
    const holder = document.createElement("div");
    holder.innerHTML = messageHtml(message, index, index === -1 ? false : isLastAssistant(index));
    const next = holder.firstElementChild;
    if (el) {
      el.replaceWith(next);
    } else {
      dom.messages.appendChild(next);
    }
    enhanceMessages();
    return next;
  }

  function cssEscape(value) {
    if (window.CSS && window.CSS.escape) return window.CSS.escape(String(value));
    return String(value).replace(/["\\]/g, "\\$&");
  }

  function scrollChatBottom(force) {
    if (!dom.chatScroll) return;
    if (!force && !state.stickToBottom) return;
    dom.chatScroll.scrollTop = dom.chatScroll.scrollHeight;
  }

  function findMessage(messageId) {
    return state.messages.find((m) => m.id === messageId) || null;
  }

  function inlineNotice(message, kind) {
    if (!dom.messages) return;
    const el = document.createElement("div");
    el.className = "notice" + (kind ? " " + kind : "");
    el.textContent = String(message || "");
    dom.messages.appendChild(el);
    if (state.stickToBottom) scrollChatBottom();
  }

  function handleMessageAction(event) {
    const copyButton = event.target.closest("[data-copy-message]");
    if (copyButton) {
      const message = findMessage(copyButton.dataset.copyMessage);
      if (message) copyText(message.content || "").then((ok) => toast(ok ? "Copied" : "Copy failed", ok ? "ok" : "error"));
      return;
    }
    const editButton = event.target.closest("[data-edit-message]");
    if (editButton) {
      startEdit(editButton.dataset.editMessage);
      return;
    }
    const regen = event.target.closest("[data-regenerate]");
    if (regen) {
      regenerateLast();
      return;
    }
    const removeAtt = event.target.closest("[data-remove-attachment]");
    if (removeAtt) {
      state.pendingAttachments = state.pendingAttachments.filter((a) => a.id !== removeAtt.dataset.removeAttachment);
      renderPendingAttachments();
    }
  }

  // -------------------------------------------------------------------- edits
  function ensureComposerExtras() {
    if (!dom.composer) return;
    if (!dom.editBanner) {
      dom.editBanner = document.createElement("div");
      dom.editBanner.className = "edit-banner";
      dom.editBanner.hidden = true;
      dom.composer.parentNode.insertBefore(dom.editBanner, dom.composer);
    }
    if (!dom.pendingAttachments) {
      dom.pendingAttachments = document.createElement("div");
      dom.pendingAttachments.className = "pending-attachments";
      dom.composer.parentNode.insertBefore(dom.pendingAttachments, dom.composer);
    }
  }

  function renderEditBanner() {
    ensureComposerExtras();
    if (!dom.editBanner) return;
    if (!state.editTarget) {
      dom.editBanner.hidden = true;
      dom.editBanner.innerHTML = "";
      return;
    }
    const message = findMessage(state.editTarget);
    dom.editBanner.hidden = false;
    dom.editBanner.innerHTML = "<strong>Editing message</strong>" +
      '<span class="grow">' + esc(truncate(message ? message.content : "", 90)) + "</span>" +
      '<button type="button" data-cancel-edit>Cancel</button>';
    const cancel = $("[data-cancel-edit]", dom.editBanner);
    if (cancel) cancel.addEventListener("click", cancelEdit);
  }

  function startEdit(messageId) {
    if (state.running) {
      toast("Wait for the current run to finish before editing.", "error");
      return;
    }
    const message = findMessage(messageId);
    if (!message || message.role !== "user") return;
    state.editTarget = messageId;
    state.regenerate = false;
    state.pendingAttachments = (message.attachments || []).slice();
    if (dom.composerInput) {
      dom.composerInput.value = message.content || "";
      autoSizeComposer();
      dom.composerInput.focus();
      dom.composerInput.setSelectionRange(dom.composerInput.value.length, dom.composerInput.value.length);
    }
    renderEditBanner();
    renderPendingAttachments();
    updateSendAvailability();
  }

  function cancelEdit() {
    state.editTarget = null;
    state.pendingAttachments = [];
    if (dom.composerInput) dom.composerInput.value = "";
    renderEditBanner();
    renderPendingAttachments();
    autoSizeComposer();
  }

  function updateSendAvailability() {
    if (!dom.sendBtn) return;
    const hasText = !!(dom.composerInput && dom.composerInput.value.trim());
    const hasFiles = state.pendingAttachments.length > 0;
    dom.sendBtn.disabled = !(hasText || hasFiles || state.regenerate);
  }

  // ------------------------------------------------------------------ sending
  async function sendMessage(interrupt) {
    closeSlash();
    if (state.running) {
      await queueMessage(interrupt);
      return;
    }
    const text = dom.composerInput ? dom.composerInput.value.trim() : "";
    if (!text && !state.pendingAttachments.length) {
      if (dom.composerInput) dom.composerInput.focus();
      return;
    }
    const payload = {
      message: text,
      attachments: state.pendingAttachments.map((a) => a.id),
    };
    if (state.currentSessionId) payload.session_id = state.currentSessionId;
    if (state.editTarget) payload.edit_message_id = state.editTarget;
    if (state.regenerate) payload.regenerate = true;
    await startRun(payload, {
      optimisticText: text,
      optimisticAttachments: state.pendingAttachments.slice(),
      edit: !!state.editTarget,
      regenerate: !!state.regenerate,
    });
  }

  async function startRun(payload, options) {
    const opts = options || {};
    if (state.running) return;
    const restore = {
      text: dom.composerInput ? dom.composerInput.value : "",
      attachments: state.pendingAttachments.slice(),
      editTarget: state.editTarget,
    };
    if (opts.edit && state.editTarget) {
      const index = state.messages.findIndex((m) => m.id === state.editTarget);
      if (index >= 0) {
        state.messages[index] = {
          ...state.messages[index],
          content: opts.optimisticText,
          attachments: opts.optimisticAttachments,
          status: "complete",
        };
        state.messages = state.messages.slice(0, index + 1);
      }
    } else if (opts.regenerate) {
      let lastUser = -1;
      for (let i = state.messages.length - 1; i >= 0; i -= 1) {
        if (state.messages[i].role === "user") { lastUser = i; break; }
      }
      if (lastUser >= 0) state.messages = state.messages.slice(0, lastUser + 1);
    } else if (opts.optimisticText || opts.optimisticAttachments.length) {
      state.messages.push({
        id: "tmp_user_" + Date.now(),
        session_id: state.currentSessionId,
        role: "user",
        content: opts.optimisticText || "",
        attachments: opts.optimisticAttachments,
        status: "complete",
        created_at: new Date().toISOString(),
      });
    }
    state.editTarget = null;
    state.regenerate = false;
    state.pendingAttachments = [];
    if (dom.composerInput) dom.composerInput.value = "";
    renderEditBanner();
    renderPendingAttachments();
    autoSizeComposer();
    renderMessages();
    scrollChatBottom(true);

    state.running = true;
    state.run = { active: true, phase: "starting", step: 0, queued: [] };
    state.live = {
      sessionId: payload.session_id || null,
      assistantId: null,
      assistantEl: null,
      bodyEl: null,
      reasoningEl: null,
      thinkingEl: null,
      content: "",
      reasoning: "",
      toolEls: new Map(),
      renderTimer: null,
    };
    updateRunningUI();
    updateHint();
    renderQueue();
    state.streamAbort = new AbortController();
    const signal = state.streamAbort.signal;
    try {
      await API.chat.stream(payload, handleStreamEvent, signal);
    } catch (err) {
      if (err && err.name === "AbortError") {
        // The run continues on the server; the UI detached deliberately.
      } else {
        toast(errorMessage(err), "error");
        inlineNotice(errorMessage(err), "error");
        state.running = false;
        state.run = null;
        state.live = null;
        updateRunningUI();
        if (state.currentSessionId) refreshCurrentSession({ quiet: true, preserveScroll: true });
      }
    } finally {
      state.streamAbort = null;
      if (state.live && state.live.renderTimer) {
        clearTimeout(state.live.renderTimer);
        state.live.renderTimer = null;
      }
      if (state.running && state.currentSessionId && !state.live) {
        // The stream ended without a done event; check whether the run is still alive.
        try {
          const status = await API.chat.status(state.currentSessionId);
          if (status.active) startWatch();
          else {
            state.running = false;
            state.run = null;
            updateRunningUI();
          }
        } catch (_) { /* ignore */ }
      }
    }
    if (state.currentSessionId) {
      localStorage.setItem("localcode.lastSession", state.currentSessionId);
      if (!restore.editTarget) {
        setTimeout(() => loadSessions(), 300);
      }
    }
  }

  function handleStreamEvent(event) {
    if (!event || !event.type) return;
    const eventSession = event.session_id || (state.live && state.live.sessionId);
    if (eventSession && state.currentSessionId && eventSession !== state.currentSessionId) return;
    if (eventSession && !state.currentSessionId && event.type === "run_start") {
      state.currentSessionId = eventSession;
      if (state.live) state.live.sessionId = eventSession;
      localStorage.setItem("localcode.lastSession", eventSession);
      loadSessions();
    }
    switch (event.type) {
      case "run_start":
        if (state.live) state.live.sessionId = event.session_id;
        showThinkingBubble();
        if (dom.sessionSub) dom.sessionSub.textContent = "Working · " + shortModel(event.model || "");
        if (dom.sessionTitle && dom.sessionTitle.textContent === "New chat") {
          dom.sessionTitle.textContent = "New chat";
        }
        break;
      case "user_message":
        if (event.queue_id) {
          state.queue = state.queue.filter((item) => item.id !== event.queue_id);
          renderQueue();
        }
        if (event.message) {
          state.messages.push(event.message);
          appendMessageElement(event.message);
        }
        break;
      case "assistant_start":
        startLiveAssistant(event.message_id);
        break;
      case "delta":
        appendLiveText(event.text);
        break;
      case "reasoning":
        appendLiveReasoning(event.text);
        break;
      case "assistant_end":
        finishLiveAssistant(event);
        break;
      case "tool_call":
        createLiveToolCard(event);
        break;
      case "tool_running":
        setLiveToolStatus(event.id, "running");
        break;
      case "tool_result":
        finishLiveTool(event);
        break;
      case "approval":
        if (!state.approvals.some((a) => a.id === event.id)) {
          state.approvals.push({
            id: event.id, tool: event.tool, args: event.args,
            risk: event.risk, preview: event.preview,
          });
        }
        setLiveToolStatus(event.id, "waiting");
        renderApprovals();
        break;
      case "ask_user":
        if (!state.questions.some((q) => q.id === event.id)) {
          state.questions.push({
            id: event.id, question: event.question,
            options: event.options || [], default: event.default || "",
          });
        }
        renderApprovals();
        break;
      case "context":
        updateContext(event);
        break;
      case "step":
        state.run = state.run || { active: true };
        state.run.step = event.step;
        updateRunningUI();
        break;
      case "mcp":
        if (event.status === "connecting") {
          if (dom.hintRight) dom.hintRight.textContent = "Connecting MCP servers…";
        } else if (event.status === "ready") {
          if (dom.hintRight) dom.hintRight.textContent = event.tools + " MCP tools ready";
        } else if (event.status === "error") {
          toast("MCP: " + (event.error || "connection failed"), "error");
        }
        break;
      case "compacting":
        inlineNotice("Compacting older messages into persistent memory…", "ok");
        break;
      case "compacted":
        inlineNotice("Context compacted. The full transcript is still saved; the agent keeps working.", "ok");
        break;
      case "notice":
        inlineNotice(event.message || "", "");
        break;
      case "error":
        clearThinkingBubble();
        toast(event.message || "Agent error", "error");
        inlineNotice(event.message || "Agent error", "error");
        break;
      case "queue_cancelled":
        state.queue = [];
        renderQueue();
        if (event.messages && event.messages.length) {
          toast("Queued messages were cancelled because the run ended.", "error");
        }
        break;
      case "done":
        finishRun(event);
        break;
      default:
        break;
    }
  }

  function startLiveAssistant(messageId) {
    if (!state.live) return;
    clearThinkingBubble();
    state.live.assistantId = messageId;
    state.live.content = "";
    state.live.reasoning = "";
    const message = {
      id: messageId, role: "assistant", content: "", reasoning: "",
      status: "streaming", created_at: new Date().toISOString(),
    };
    const index = state.messages.length;
    state.messages.push(message);
    const el = appendMessageElement(message);
    if (!el) return;
    state.live.assistantEl = el;
    state.live.bodyEl = $(".msg-body", el);
    state.live.reasoningEl = null;
    scrollChatBottom(true);
  }

  function showThinkingBubble() {
    if (!state.live || !dom.messages) return;
    clearThinkingBubble();
    const holder = document.createElement("div");
    holder.innerHTML = '<div class="msg assistant thinking-msg" data-thinking="1">' +
      '<div class="msg-avatar">LC</div>' +
      '<div class="msg-main"><div class="msg-body">' +
        '<span class="thinking-label">Thinking</span><span class="typing"><i></i><i></i><i></i></span>' +
      "</div></div></div>";
    const el = holder.firstElementChild;
    dom.messages.appendChild(el);
    state.live.thinkingEl = el;
    scrollChatBottom(true);
  }

  function clearThinkingBubble() {
    if (state.live && state.live.thinkingEl) {
      state.live.thinkingEl.remove();
      state.live.thinkingEl = null;
    }
  }

  function appendLiveText(text) {
    if (!state.live || !text) return;
    state.live.content += text;
    if (dom.sessionSub) dom.sessionSub.textContent = "Responding…";
    if (!state.live.renderTimer) {
      state.live.renderTimer = setTimeout(() => {
        if (!state.live) return;
        state.live.renderTimer = null;
        if (state.live.bodyEl) {
          fillMarkdown(state.live.bodyEl, state.live.content, false);
          const caret = document.createElement("span");
          caret.className = "stream-caret";
          state.live.bodyEl.appendChild(caret);
          if (state.stickToBottom) scrollChatBottom();
        }
      }, 30);
    }
  }

  function appendLiveReasoning(text) {
    if (!state.live || !text) return;
    state.live.reasoning += text;
    if (!state.live.reasoningEl && state.live.assistantEl) {
      const details = document.createElement("details");
      details.className = "reasoning";
      details.open = true;
      details.innerHTML = "<summary>Thinking…</summary><div class=\"reasoning-body\"></div>";
      state.live.assistantEl.querySelector(".msg-main").insertBefore(
        details, state.live.assistantEl.querySelector(".msg-body")
      );
      state.live.reasoningEl = details;
    }
    if (state.live.reasoningEl) {
      const body = $(".reasoning-body", state.live.reasoningEl);
      if (body) body.textContent = state.live.reasoning;
      if (state.stickToBottom) scrollChatBottom();
    }
  }

  function finishLiveAssistant(event) {
    if (!state.live) return;
    if (state.live.renderTimer) {
      clearTimeout(state.live.renderTimer);
      state.live.renderTimer = null;
    }
    const message = event.message || {
      id: state.live.assistantId,
      role: "assistant",
      content: state.live.content,
      reasoning: state.live.reasoning,
      status: "complete",
      created_at: new Date().toISOString(),
    };
    const index = state.messages.findIndex((m) => m.id === message.id);
    if (index >= 0) state.messages[index] = message;
    else state.messages.push(message);
    const el = replaceMessageElement(message);
    state.live.assistantEl = el;
    state.live.bodyEl = el ? $(".msg-body", el) : null;
    state.live.reasoningEl = el ? $(".reasoning", el) : null;
    state.live.content = message.content || "";
    state.live.reasoning = message.reasoning || "";
    if (event.tokens && event.tokens.session && state.session) {
      state.session.total_prompt_tokens = event.tokens.session.prompt_tokens;
      state.session.total_completion_tokens = event.tokens.session.completion_tokens;
      state.session.total_tokens = event.tokens.session.total_tokens;
      state.session.total_cost_usd = event.tokens.session.cost_usd;
      updateHint();
    }
    if (state.stickToBottom) scrollChatBottom();
  }

  function createLiveToolCard(event) {
    if (!state.live) return;
    const message = {
      id: "tmp_tool_" + event.id,
      role: "tool",
      content: "",
      tool_name: event.name,
      tool_args: JSON.stringify(event.args || {}),
      meta: null,
      status: event.risk === "safe" ? "running" : "waiting",
      created_at: new Date().toISOString(),
    };
    const holder = document.createElement("div");
    holder.innerHTML = messageHtml(message, state.messages.length, false);
    const el = holder.firstElementChild;
    el.dataset.toolEventId = event.id;
    if (dom.messages) dom.messages.appendChild(el);
    state.live.toolEls.set(event.id, el);
    if (state.stickToBottom) scrollChatBottom();
  }

  function setLiveToolStatus(toolEventId, status) {
    if (!state.live) return;
    const el = state.live.toolEls.get(toolEventId);
    if (!el) return;
    const badge = $(".tool-status", el);
    if (badge) {
      badge.className = "tool-status " + status;
      badge.textContent = toolStatusLabel(status);
    }
  }

  function finishLiveTool(event) {
    if (!state.live) return;
    const old = state.live.toolEls.get(event.id);
    const message = event.message;
    if (message) {
      state.messages.push(message);
      if (old) {
        const holder = document.createElement("div");
        holder.innerHTML = messageHtml(message, state.messages.length - 1, false);
        old.replaceWith(holder.firstElementChild);
        enhanceMessages();
      } else {
        appendMessageElement(message);
      }
    }
    state.live.toolEls.delete(event.id);
    if (state.stickToBottom) scrollChatBottom();
  }

  function finishRun(event) {
    state.running = false;
    state.run = null;
    state.queue = [];
    clearThinkingBubble();
    if (event && event.session_usage && state.session) {
      state.session.total_prompt_tokens = event.session_usage.prompt_tokens;
      state.session.total_completion_tokens = event.session_usage.completion_tokens;
      state.session.total_tokens = event.session_usage.total_tokens;
      state.session.total_cost_usd = event.session_usage.cost_usd;
    }
    updateRunningUI();
    renderQueue();
    updateHint();
    const sessionId = (event && event.session_id) || state.currentSessionId;
    setTimeout(() => afterRun(sessionId), 80);
  }

  async function afterRun(sessionId) {
    if (!sessionId || state.currentSessionId !== sessionId) return;
    state.live = null;
    state.running = false;
    state.run = null;
    updateRunningUI();
    stopWatch();
    await refreshCurrentSession({ quiet: true, preserveScroll: true });
    await Promise.allSettled([loadSessions(), loadUsage()]);
    if (dom.composerInput) dom.composerInput.focus();
  }

  function detachStream() {
    if (state.streamAbort) {
      try { state.streamAbort.abort(); } catch (_) { /* ignore */ }
    }
    state.streamAbort = null;
    state.live = null;
    state.running = false;
    state.run = null;
    stopWatch();
    updateRunningUI();
  }

  // -------------------------------------------------------------------- queue
  function renderQueue() {
    if (!dom.queueStrip) return;
    if (!state.queue.length) {
      dom.queueStrip.innerHTML = "";
      return;
    }
    dom.queueStrip.innerHTML = state.queue.map((item) => {
      const mode = item.mode === "steer" ? "steer" : "queued";
      return '<span class="queue-chip" data-queue-id="' + esc(item.id) + '">' +
        '<span class="mode">' + esc(mode) + "</span>" +
        '<span class="text">' + esc(item.content || "Attachment") + "</span>" +
        '<button type="button" data-unqueue="' + esc(item.id) + '" title="Remove">×</button>' +
        "</span>";
    }).join("");
  }

  async function queueMessage(interrupt) {
    const text = dom.composerInput ? dom.composerInput.value.trim() : "";
    if (!text && !state.pendingAttachments.length) return;
    if (!state.currentSessionId) {
      await sendMessage(false);
      return;
    }
    const attachments = state.pendingAttachments.map((a) => a.id);
    try {
      const result = await API.chat.queue({
        session_id: state.currentSessionId,
        message: text,
        attachments,
        interrupt: !!interrupt,
      });
      if (!result.active) {
        // The run finished between the click and the queue call; send normally.
        state.running = false;
        state.run = null;
        updateRunningUI();
        await sendMessage(false);
        return;
      }
      state.queue.push(result.item);
      renderQueue();
      if (dom.composerInput) dom.composerInput.value = "";
      state.pendingAttachments = [];
      renderPendingAttachments();
      renderEditBanner();
      autoSizeComposer();
      updateSendAvailability();
      toast(interrupt ? "Interrupting — your message will be sent next." : "Message queued.", "ok");
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  }

  async function removeQueued(queueId) {
    if (!state.currentSessionId) return;
    try {
      await API.chat.unqueue({ session_id: state.currentSessionId, queue_id: queueId });
      state.queue = state.queue.filter((item) => item.id !== queueId);
      renderQueue();
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  }

  async function stopRun() {
    if (!state.currentSessionId) return;
    try {
      const result = await API.chat.stop(state.currentSessionId);
      if (!result.stopped) {
        state.running = false;
        state.run = null;
        updateRunningUI();
      } else {
        if (state.run) state.run.phase = "stopping";
        updateRunningUI();
        toast("Stopping the agent…", "ok");
      }
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  }

  async function toggleApprovalMode() {
    const current = (state.settings && state.settings.approval_mode) || "ask";
    const next = current === "auto" ? "ask" : "auto";
    try {
      const data = await API.settings.patch({ approval_mode: next });
      state.settings = data.settings || state.settings;
      updateApprovalMode();
      toast(next === "auto"
        ? "Auto-run is on: write and shell tools run without asking."
        : "Ask-first is on: risky tools wait for approval.", "ok");
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  }

  // ------------------------------------------------------------------- watcher
  function startWatch() {
    if (!state.currentSessionId) return;
    if (state.watchTimer) return;
    state.watchTimer = setInterval(watchTick, 1600);
  }

  function stopWatch() {
    if (state.watchTimer) clearInterval(state.watchTimer);
    state.watchTimer = null;
    state.watchBusy = false;
  }

  async function watchTick() {
    if (!state.currentSessionId || state.watchBusy) return;
    state.watchBusy = true;
    try {
      const status = await API.chat.status(state.currentSessionId);
      if (!status.active) {
        stopWatch();
        if (state.running) {
          state.running = false;
          state.run = null;
          updateRunningUI();
          await refreshCurrentSession({ quiet: true, preserveScroll: true });
          await loadSessions();
        }
        return;
      }
      state.running = true;
      state.run = status;
      state.queue = status.queued || [];
      state.approvals = status.approvals || [];
      state.questions = status.questions || [];
      renderQueue();
      renderApprovals();
      updateRunningUI();
      if (!state.live) {
        await refreshCurrentSession({ quiet: true, preserveScroll: true });
      }
    } catch (_) {
      // A transient failure while the server is busy; retry on the next tick.
    } finally {
      state.watchBusy = false;
    }
  }

  // --------------------------------------------------------------- attachments
  function renderPendingAttachments() {
    ensureComposerExtras();
    if (!dom.pendingAttachments) return;
    if (!state.pendingAttachments.length) {
      dom.pendingAttachments.innerHTML = "";
      updateSendAvailability();
      return;
    }
    dom.pendingAttachments.innerHTML = state.pendingAttachments.map((att) => {
      const kind = att.kind || att.ext || "file";
      return '<span class="attach-chip" data-attachment-id="' + esc(att.id) + '">' +
        (att.kind === "image" && att.url ? '<img src="' + esc(att.url) + '" alt="">' : "") +
        '<span class="kind">' + esc(String(kind).slice(0, 8)) + "</span>" +
        '<span class="nowrap">' + esc(att.name || "attachment") + "</span>" +
        '<span class="muted tiny">' + esc(fmtBytes(att.size || 0)) + "</span>" +
        '<button type="button" data-remove-attachment="' + esc(att.id) + '" title="Remove">×</button>' +
        "</span>";
    }).join("");
    $$("[data-remove-attachment]", dom.pendingAttachments).forEach((button) => {
      button.addEventListener("click", () => {
        state.pendingAttachments = state.pendingAttachments.filter(
          (a) => a.id !== button.dataset.removeAttachment
        );
        renderPendingAttachments();
      });
    });
    updateSendAvailability();
  }

  function ensureFileInput() {
    if (dom.fileInput) return dom.fileInput;
    const input = document.createElement("input");
    input.type = "file";
    input.multiple = true;
    input.hidden = true;
    input.addEventListener("change", () => {
      const files = Array.from(input.files || []);
      input.value = "";
      if (files.length) uploadFiles(files);
    });
    document.body.appendChild(input);
    dom.fileInput = input;
    return input;
  }

  async function uploadFiles(files) {
    const list = Array.from(files || []);
    if (!list.length) return;
    let okCount = 0;
    for (const file of list) {
      try {
        const result = await API.attachments.upload(file);
        if (result && result.attachment) {
          state.pendingAttachments.push(result.attachment);
          okCount += 1;
          renderPendingAttachments();
        }
      } catch (err) {
        toast(file.name + ": " + errorMessage(err), "error");
      }
    }
    if (okCount) toast(okCount + " file" + (okCount === 1 ? "" : "s") + " attached", "ok", 1800);
  }

  function openAttachMenu() {
    openModal({
      title: "Add context",
      sub: "Attach files or pull a file from the workspace",
      size: "small",
      body:
        '<div class="btn-row" style="flex-direction:column;align-items:stretch;gap:8px">' +
          '<button class="soft-btn" type="button" data-attach-upload style="justify-content:flex-start">' +
            '<span class="icon-text">📎 Upload files from this computer</span></button>' +
          '<button class="soft-btn" type="button" data-attach-workspace style="justify-content:flex-start">' +
            '<span class="icon-text">🗂 Insert a workspace file</span></button>' +
        "</div>" +
        '<div class="section-note mt-12">Text, code, PDF, images, audio and video are accepted. Images need a vision model to be understood.</div>',
      onMount(modal) {
        $("[data-attach-upload]", modal.body).addEventListener("click", () => {
          closeModal();
          ensureFileInput().click();
        });
        $("[data-attach-workspace]", modal.body).addEventListener("click", () => {
          closeModal();
          openWorkspacePicker();
        });
      },
    });
  }

  function handlePaste(event) {
    const items = event.clipboardData && event.clipboardData.items;
    if (!items) return;
    const files = [];
    for (const item of Array.from(items)) {
      if (item.kind === "file") {
        const file = item.getAsFile();
        if (file) files.push(file);
      }
    }
    if (files.length) {
      event.preventDefault();
      uploadFiles(files);
    }
  }

  function setupDragDrop() {
    if (!dom.composer) return;
    let depth = 0;
    dom.composer.addEventListener("dragenter", (event) => {
      event.preventDefault();
      depth += 1;
      dom.composer.classList.add("drop-active");
    });
    dom.composer.addEventListener("dragover", (event) => {
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    });
    dom.composer.addEventListener("dragleave", (event) => {
      event.preventDefault();
      depth = Math.max(0, depth - 1);
      if (depth === 0) dom.composer.classList.remove("drop-active");
    });
    dom.composer.addEventListener("drop", (event) => {
      event.preventDefault();
      depth = 0;
      dom.composer.classList.remove("drop-active");
      const files = event.dataTransfer ? Array.from(event.dataTransfer.files || []) : [];
      if (files.length) uploadFiles(files);
    });
  }

  async function openWorkspacePicker() {
    const modal = openModal({
      title: "Workspace files",
      sub: "Pick a file to attach to this message",
      size: "medium",
      body: '<div class="files-path" data-picker-path>/</div><div class="file-list" data-picker-list>' +
        '<div class="empty-state"><span class="spinner"></span></div></div>',
      foot: '<button class="ghost-btn" type="button" data-picker-close>Close</button>',
      onMount(api) {
        const list = $("[data-picker-list]", api.body);
        const pathEl = $("[data-picker-path]", api.body);
        $("[data-picker-close]", api.foot).addEventListener("click", closeModal);
        async function load(path) {
          list.innerHTML = '<div class="empty-state"><span class="spinner"></span></div>';
          try {
            const data = await API.workspace.tree(path);
            pathEl.textContent = "/" + (data.path === "." ? "" : data.path);
            const entries = data.entries || [];
            let html = "";
            if (data.path && data.path !== ".") {
              const parent = data.path.split("/").slice(0, -1).join("/") || ".";
              html += '<div class="file-row dir" data-picker-dir="' + esc(parent) + '">' +
                '<span class="f-icon">↩</span><span>..</span></div>';
            }
            html += entries.map((entry) =>
              '<div class="file-row ' + (entry.type === "dir" ? "dir" : "") + '" data-picker-' +
              (entry.type === "dir" ? "dir" : "file") + '="' + esc(entry.path) + '">' +
                '<span class="f-icon">' + (entry.type === "dir" ? "▸" : "·") + "</span>" +
                '<span>' + esc(entry.name) + "</span>" +
                '<span class="f-size">' + (entry.type === "file" ? fmtBytes(entry.size) : "") + "</span>" +
              "</div>"
            ).join("");
            list.innerHTML = html || '<div class="empty-state">This folder is empty.</div>';
            $$("[data-picker-dir]", list).forEach((row) => {
              row.addEventListener("click", () => load(row.dataset.pickerDir));
            });
            $$("[data-picker-file]", list).forEach((row) => {
              row.addEventListener("click", () => {
                closeModal();
                openFilePreview(row.dataset.pickerFile, { attach: true });
              });
            });
          } catch (err) {
            list.innerHTML = '<div class="empty-state">' + esc(errorMessage(err)) + "</div>";
          }
        }
        load(".");
      },
    });
    return modal;
  }

  async function attachWorkspaceFile(path) {
    try {
      const data = await API.workspace.file(path);
      const name = String(path || "file").split("/").pop() || "file";
      const blob = new Blob([data.content || ""], { type: "text/plain" });
      const file = new File([blob], name, { type: "text/plain" });
      const result = await API.attachments.upload(file);
      if (result && result.attachment) {
        state.pendingAttachments.push(result.attachment);
        renderPendingAttachments();
        toast("Attached " + name, "ok", 1800);
      }
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  }

  function insertPathIntoComposer(path) {
    if (!dom.composerInput) return;
    const current = dom.composerInput.value;
    dom.composerInput.value = current + (current && !current.endsWith(" ") ? " " : "") + "@" + path + " ";
    autoSizeComposer();
    dom.composerInput.focus();
    updateSendAvailability();
  }

  // ----------------------------------------------------------------- approvals
  function closeApprovals() {
    if (dom.approvalRoot) dom.approvalRoot.innerHTML = "";
    state.approvalSignature = "";
  }

  function renderApprovals() {
    if (!dom.approvalRoot) return;
    const approvals = state.approvals || [];
    const questions = state.questions || [];
    if (!approvals.length && !questions.length) {
      closeApprovals();
      return;
    }
    const signature = JSON.stringify({
      approvals: approvals.map((a) => a.id),
      questions: questions.map((q) => q.id),
      mode: state.settings && state.settings.approval_mode,
    });
    if (signature === state.approvalSignature && $("#approvalRoot .approval-backdrop")) return;
    state.approvalSignature = signature;
    let html = '<div class="approval-backdrop"><div class="approval-stack">';
    approvals.forEach((approval) => {
      const risk = approval.risk || "write";
      html += '<div class="approval-card" data-approval-card="' + esc(approval.id) + '">' +
        '<div class="approval-head">' +
          '<span class="tool-icon">⚠</span>' +
          '<div class="title">Approve tool: ' + esc(approval.tool || "tool") + "</div>" +
          '<span class="risk">' + esc(risk) + "</span>" +
        "</div>" +
        '<div class="approval-body">' +
          (approval.preview
            ? '<div class="approval-preview">' + esc(approval.preview) + "</div>"
            : '<div class="muted tiny">No preview available.</div>') +
          '<div class="btn-row mt-8">' +
            '<button class="primary-btn" type="button" data-approve="' + esc(approval.id) + '">Allow once</button>' +
            '<button class="ghost-btn" type="button" data-always="' + esc(approval.id) + '">Always allow</button>' +
            '<button class="danger-btn" type="button" data-deny="' + esc(approval.id) + '">Deny</button>' +
          "</div>" +
        "</div>" +
      "</div>";
    });
    questions.forEach((question) => {
      const draft = state.answerDrafts[question.id] || "";
      html += '<div class="approval-card question-card" data-question-card="' + esc(question.id) + '">' +
        '<div class="approval-head">' +
          '<span class="tool-icon">?</span>' +
          '<div class="title">LocalCode has a question</div>' +
        "</div>" +
        '<div class="approval-body">' +
          '<div class="question-text">' + esc(question.question || "") + "</div>" +
          (question.options && question.options.length
            ? '<div class="option-row">' + question.options.map((option) =>
                '<button class="option-btn" type="button" data-answer="' + esc(question.id) +
                '" data-value="' + esc(option) + '">' + esc(option) + "</button>"
              ).join("") + "</div>"
            : "") +
          '<div class="ask-input">' +
            '<input class="input" type="text" data-answer-input="' + esc(question.id) + '" placeholder="Type your answer…" value="' + esc(draft) + '">' +
            '<button class="primary-btn" type="button" data-send-answer="' + esc(question.id) + '">Send</button>' +
          "</div>" +
          '<div class="btn-row mt-8"><button class="ghost-btn small" type="button" data-skip-question="' +
            esc(question.id) + '">Skip and let the agent decide</button></div>' +
        "</div>" +
      "</div>";
    });
    html += "</div></div>";
    dom.approvalRoot.innerHTML = html;
    $$("[data-answer-input]", dom.approvalRoot).forEach((input) => {
      input.addEventListener("input", () => {
        state.answerDrafts[input.dataset.answerInput] = input.value;
      });
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          resolveQuestion(input.dataset.answerInput, input.value);
        }
      });
    });
  }

  async function resolveApprovalUI(approvalId, approved, always) {
    try {
      if (always) {
        try {
          const data = await API.settings.patch({ approval_mode: "auto" });
          state.settings = data.settings || state.settings;
          updateApprovalMode();
        } catch (_) { /* still allow this one */ }
      }
      await API.chat.approve(approvalId, approved, approved ? "Approved by user" : "Denied by user");
      state.approvals = state.approvals.filter((a) => a.id !== approvalId);
      renderApprovals();
      toast(approved ? "Tool approved" : "Tool denied", approved ? "ok" : "");
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  }

  async function resolveQuestion(questionId, answer) {
    try {
      await API.chat.answer(questionId, answer || "", answer ? "Answered by user" : "Skipped by user");
      state.questions = state.questions.filter((q) => q.id !== questionId);
      delete state.answerDrafts[questionId];
      renderApprovals();
      if (answer) toast("Answer sent", "ok");
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  }

  function handleApprovalClick(event) {
    const approve = event.target.closest("[data-approve]");
    if (approve) { resolveApprovalUI(approve.dataset.approve, true, false); return; }
    const always = event.target.closest("[data-always]");
    if (always) { resolveApprovalUI(always.dataset.always, true, true); return; }
    const deny = event.target.closest("[data-deny]");
    if (deny) { resolveApprovalUI(deny.dataset.deny, false, false); return; }
    const option = event.target.closest("[data-answer]");
    if (option) { resolveQuestion(option.dataset.answer, option.dataset.value); return; }
    const send = event.target.closest("[data-send-answer]");
    if (send) {
      const input = $('[data-answer-input="' + cssEscape(send.dataset.sendAnswer) + '"]');
      resolveQuestion(send.dataset.sendAnswer, input ? input.value : "");
      return;
    }
    const skip = event.target.closest("[data-skip-question]");
    if (skip) { resolveQuestion(skip.dataset.skipQuestion, ""); return; }
  }

  // ------------------------------------------------------------- session menu
  async function openSessionMenu() {
    if (!state.currentSessionId) {
      toast("Start a chat first.", "error");
      return;
    }
    const session = state.session || {};
    const pinned = !!session.pinned;
    openModal({
      title: session.title || "Chat",
      sub: session.workspace || "",
      size: "small",
      body:
        '<div class="btn-row" style="flex-direction:column;align-items:stretch;gap:6px">' +
          '<button class="soft-btn" type="button" data-act="rename" style="justify-content:flex-start">Rename chat</button>' +
          '<button class="soft-btn" type="button" data-act="pin" style="justify-content:flex-start">' + (pinned ? "Unpin chat" : "Pin chat") + "</button>" +
          '<button class="soft-btn" type="button" data-act="compact" style="justify-content:flex-start">Compact context now</button>' +
          '<button class="soft-btn" type="button" data-act="usage" style="justify-content:flex-start">Usage &amp; hosted cost</button>' +
          '<button class="soft-btn" type="button" data-act="caps" style="justify-content:flex-start">Model capabilities</button>' +
          '<button class="soft-btn" type="button" data-act="export-md" style="justify-content:flex-start">Export as Markdown</button>' +
          '<button class="soft-btn" type="button" data-act="export-json" style="justify-content:flex-start">Export as JSON</button>' +
          '<button class="danger-btn" type="button" data-act="delete" style="justify-content:flex-start">Delete chat</button>' +
        "</div>" +
        '<div class="section-note mt-12">Compaction keeps the full transcript in the database and replaces older context with a durable summary.</div>',
      onMount(api) {
        $$("[data-act]", api.body).forEach((button) => {
          button.addEventListener("click", async () => {
            const action = button.dataset.act;
            if (action === "rename") {
              closeModal();
              const value = await promptValue("Rename chat", "Chat title", session.title || "");
              if (value === null) return;
              const title = value.trim();
              if (!title) return;
              try {
                await API.sessions.patch(state.currentSessionId, { title });
                if (state.session) state.session.title = title;
                if (dom.sessionTitle) dom.sessionTitle.textContent = title;
                await loadSessions();
                toast("Chat renamed", "ok");
              } catch (err) { toast(errorMessage(err), "error"); }
            } else if (action === "pin") {
              try {
                await API.sessions.patch(state.currentSessionId, { pinned: !pinned });
                if (state.session) state.session.pinned = pinned ? 0 : 1;
                closeModal();
                await loadSessions();
              } catch (err) { toast(errorMessage(err), "error"); }
            } else if (action === "compact") {
              closeModal();
              try {
                const result = await API.sessions.compact(state.currentSessionId);
                toast("Context compacted" + (result && result.compacted ? " (" + result.compacted + " messages)" : ""), "ok");
                await refreshCurrentSession({ quiet: true, preserveScroll: true });
              } catch (err) { toast(errorMessage(err), "error"); }
            } else if (action === "usage") {
              closeModal();
              openUsage(state.currentSessionId);
            } else if (action === "caps") {
              closeModal();
              openCapabilities(state.capabilities);
            } else if (action === "export-md") {
              downloadUrl(API.sessions.exportUrl(state.currentSessionId, "md"));
              closeModal();
            } else if (action === "export-json") {
              downloadUrl(API.sessions.exportUrl(state.currentSessionId, "json"));
              closeModal();
            } else if (action === "delete") {
              closeModal();
              const ok = await confirmDialog(
                "Delete chat?",
                "This permanently deletes this chat, its messages and its usage history.",
                "Delete", true
              );
              if (!ok) return;
              try {
                await API.sessions.remove(state.currentSessionId);
                toast("Chat deleted", "ok");
                newChat();
                await loadSessions();
              } catch (err) { toast(errorMessage(err), "error"); }
            }
          });
        });
      },
    });
  }

  function downloadUrl(url) {
    const a = document.createElement("a");
    a.href = url;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  // -------------------------------------------------------------------- files
  function toggleFiles(force) {
    const open = force === undefined ? !state.filesOpen : !!force;
    state.filesOpen = open;
    if (dom.filesPanel) dom.filesPanel.hidden = !open;
    if (dom.filesToggleBtn) dom.filesToggleBtn.classList.toggle("active", open);
    if (open) loadFiles(state.filesPath || ".");
  }

  async function loadFiles(path) {
    if (!dom.fileList) return;
    state.filesBusy = true;
    state.filesPath = path || ".";
    if (dom.filesPath) dom.filesPath.textContent = "/" + (state.filesPath === "." ? "" : state.filesPath);
    dom.fileList.innerHTML = '<div class="empty-state"><span class="spinner"></span></div>';
    try {
      const data = await API.workspace.tree(state.filesPath);
      state.filesPath = data.path || ".";
      if (dom.filesPath) dom.filesPath.textContent = "/" + (state.filesPath === "." ? "" : state.filesPath);
      const entries = data.entries || [];
      let html = "";
      if (state.filesPath && state.filesPath !== ".") {
        const parent = state.filesPath.split("/").slice(0, -1).join("/") || ".";
        html += '<div class="file-row dir" data-file-dir="' + esc(parent) + '">' +
          '<span class="f-icon">↩</span><span>..</span></div>';
      }
      html += entries.map((entry) =>
        '<div class="file-row ' + (entry.type === "dir" ? "dir" : "") + '" data-file-' +
        (entry.type === "dir" ? "dir" : "file") + '="' + esc(entry.path) + '">' +
          '<span class="f-icon">' + (entry.type === "dir" ? "▸" : "·") + "</span>" +
          '<span>' + esc(entry.name) + "</span>" +
          '<span class="f-size">' + (entry.type === "file" ? fmtBytes(entry.size) : "") + "</span>" +
        "</div>"
      ).join("");
      dom.fileList.innerHTML = html || '<div class="empty-state">This folder is empty.</div>';
      $$("[data-file-dir]", dom.fileList).forEach((row) => {
        row.addEventListener("click", () => loadFiles(row.dataset.fileDir));
      });
      $$("[data-file-file]", dom.fileList).forEach((row) => {
        row.addEventListener("click", () => openFilePreview(row.dataset.fileFile, { attach: true }));
      });
    } catch (err) {
      dom.fileList.innerHTML = '<div class="empty-state">' + esc(errorMessage(err)) + "</div>";
    } finally {
      state.filesBusy = false;
    }
  }

  async function openFilePreview(path, options) {
    const opts = options || {};
    const modal = openModal({
      title: path || "File",
      sub: "Workspace file",
      body: '<div class="empty-state"><span class="spinner"></span> Loading…</div>',
      foot: '<button class="ghost-btn" type="button" data-file-copy>Copy</button>' +
        '<button class="ghost-btn" type="button" data-file-insert>Insert path</button>' +
        (opts.attach ? '<button class="primary-btn" type="button" data-file-attach>Attach to chat</button>' : "") +
        '<button class="ghost-btn" type="button" data-file-close>Close</button>',
      onMount(api) {
        $("[data-file-close]", api.foot).addEventListener("click", closeModal);
        $("[data-file-copy]", api.foot).addEventListener("click", () => {
          const code = $("code", api.body);
          copyText(code ? code.textContent : "").then((ok) => toast(ok ? "Copied" : "Copy failed", ok ? "ok" : "error"));
        });
        $("[data-file-insert]", api.foot).addEventListener("click", () => {
          insertPathIntoComposer(path);
          closeModal();
        });
        const attachBtn = $("[data-file-attach]", api.foot);
        if (attachBtn) {
          attachBtn.addEventListener("click", async () => {
            attachBtn.disabled = true;
            attachBtn.textContent = "Attaching…";
            await attachWorkspaceFile(path);
            closeModal();
          });
        }
      },
    });
    try {
      const data = await API.workspace.file(path);
      if (state.modal !== modal) return;
      modal.opts.sub = fmtBytes(data.size || 0) + (data.truncated ? " · preview truncated" : "");
      const sub = $(".modal-sub", modal.modal);
      if (sub) sub.textContent = modal.opts.sub;
      modalBody('<div class="code-preview"><pre><code></code></pre></div>');
      const code = $("code", modal.body);
      if (code) {
        code.textContent = data.content || "";
        const ext = String(path).split(".").pop();
        if (ext && window.hljs) {
          try {
            code.className = "language-" + ext;
            window.hljs.highlightElement(code);
          } catch (_) { /* ignore */ }
        }
      }
    } catch (err) {
      if (state.modal === modal) modalBody('<div class="empty-state">' + esc(errorMessage(err)) + "</div>");
    }
  }

  // -------------------------------------------------------------------- models
  function capabilityBadges(cap) {
    if (!cap || !cap.badges) return "";
    return '<div class="cap-badges">' + cap.badges.map((badge) =>
      '<span class="cap-badge ' + (badge.on ? "on" : "off") + '" title="' +
      esc(badge.label) + (badge.on ? " supported" : " not available") + '">' + esc(badge.label) + "</span>"
    ).join("") + "</div>";
  }

  function fitBadge(fit, gpuFit) {
    let html = "";
    if (fit && fit !== "unknown") {
      const labels = { great: "fits RAM", tight: "tight RAM", no: "too large for RAM" };
      html += '<span class="fit ' + esc(fit) + '">' + esc(labels[fit] || fit) + "</span>";
    }
    if (gpuFit && gpuFit !== "none") {
      const gl = { full: "fits GPU", partial: "GPU partial" };
      const cls = gpuFit === "full" ? "great" : "tight";
      html += '<span class="fit gpu ' + cls + '">' + esc(gl[gpuFit] || gpuFit) + "</span>";
    }
    return html;
  }

  function openModels(tab) {
    const ui = {
      tab: tab || "browse",
      category: "all",
      search: "",
      timer: null,
      api: null,
      loading: false,
    };
    const tabs = [
      { id: "installed", label: "Installed" },
      { id: "browse", label: "Browse" },
      { id: "downloads", label: "Downloads" },
      { id: "runtime", label: "Runtime" },
    ].map((item) => ({ ...item, active: item.id === ui.tab }));
    const api = openModal({
      title: "Models",
      sub: "Download GGUF models and run them fully offline",
      tabs,
      body: '<div class="empty-state"><span class="spinner"></span> Loading models…</div>',
      onMount(modal) {
        $$("[data-tab]", modal.tabs).forEach((button) => {
          button.addEventListener("click", () => {
            ui.tab = button.dataset.tab;
            $$("[data-tab]", modal.tabs).forEach((b) => b.classList.toggle("active", b.dataset.tab === ui.tab));
            renderModelsTab(ui);
          });
        });
      },
      onClose() {
        if (ui.timer) clearInterval(ui.timer);
      },
    });
    ui.api = api;
    renderModelsTab(ui);
    ui.timer = setInterval(() => {
      if (state.modal !== api) { clearInterval(ui.timer); return; }
      if (ui.tab === "downloads" || ui.tab === "runtime" || ui.tab === "installed") {
        renderModelsTab(ui, true);
      }
    }, 2200);
    return api;
  }

  async function renderModelsTab(ui, quiet) {
    const api = ui.api;
    if (!api || state.modal !== api) return;
    if (ui.loading) return;
    ui.loading = true;
    try {
      if (ui.tab === "installed" || ui.tab === "browse") {
        const data = await API.models.list();
        if (state.modal !== api) return;
        state.models = data;
        state.runtime = data.runtime || state.runtime;
        state.capabilities = data.capabilities || state.capabilities;
        renderEngine();
        updateSetupCard();
        if (ui.tab === "installed") renderInstalledTab(ui, data);
        else renderBrowseTab(ui, data);
      } else if (ui.tab === "downloads") {
        const data = await API.models.downloads();
        if (state.modal !== api) return;
        renderDownloadsTab(ui, data.downloads || []);
      } else if (ui.tab === "runtime") {
        const runtime = await API.runtime.status(true);
        if (state.modal !== api) return;
        state.runtime = runtime;
        renderRuntimeTab(ui, runtime);
      }
    } catch (err) {
      if (state.modal === api) modalBody('<div class="empty-state">' + esc(errorMessage(err)) + "</div>");
    } finally {
      ui.loading = false;
    }
  }

  function modelFoot(left) {
    return '<div class="left">' + (left || "") + "</div>" +
      '<button class="ghost-btn" type="button" data-refresh-models>Refresh</button>';
  }

  function bindModelFoot(ui) {
    if (!state.modal) return;
    const refresh = $("[data-refresh-models]", state.modal.foot);
    if (refresh) refresh.addEventListener("click", () => renderModelsTab(ui));
  }

  function renderInstalledTab(ui, data) {
    const installed = data.installed || [];
    const runtime = data.runtime || {};
    modalFoot(modelFoot("Models folder: " + esc(data.disk ? fmtBytes(data.disk.models) : "0 B") +
      " · " + esc(fmtBytes(data.disk ? data.disk.free : 0)) + " free"));
    bindModelFoot(ui);
    if (!installed.length) {
      modalBody('<div class="empty-state"><strong>No models installed yet</strong>' +
        "Download a small coding model to get started. The Browse tab has models for every RAM size." +
        '<div class="btn-row mt-12" style="justify-content:center">' +
        '<button class="primary-btn" type="button" data-goto-browse>Browse models</button></div></div>');
      const go = $("[data-goto-browse]", state.modal.body);
      if (go) go.addEventListener("click", () => {
        ui.tab = "browse";
        $$("[data-tab]", state.modal.tabs).forEach((b) => b.classList.toggle("active", b.dataset.tab === "browse"));
        renderModelsTab(ui);
      });
      return;
    }
    const rows = installed.map((item) => {
      const cap = item.capabilities || {};
      const size = fmtBytes(item.size || 0);
      const ctx = item.context ? Math.round(item.context / 1024) + "k ctx" : "context auto";
      const loaded = item.loaded || runtime.model_id === item.id;
      return '<div class="installed-row" data-model-id="' + esc(item.id) + '">' +
        '<div class="grow">' +
          '<div class="title">' + esc(item.name) + " " +
            (loaded ? '<span class="model-badge accent">loaded</span>' : "") +
            (item.vision_ready ? '<span class="model-badge">vision</span>' : "") +
          "</div>" +
          '<div class="sub">' + esc(item.quant || "GGUF") + " · " + esc(size) + " · " + esc(ctx) +
            (item.params ? " · " + esc(item.params) : "") + " · " + esc(item.source || "download") + "</div>" +
          capabilityBadges(cap) +
        "</div>" +
        '<div class="btn-row">' +
          (loaded
            ? '<button class="ghost-btn small" type="button" data-unload-model>Unload</button>'
            : '<button class="primary-btn small" type="button" data-load-model="' + esc(item.id) + '">Load</button>') +
          '<button class="ghost-btn small" type="button" data-model-caps="' + esc(item.id) + '">Capabilities</button>' +
          '<button class="danger-btn small" type="button" data-delete-model="' + esc(item.id) + '">Delete</button>' +
        "</div>" +
      "</div>";
    }).join("");
    modalBody(
      '<div class="section-note">' + (runtime.ready
        ? "Loaded: " + esc(runtime.model_name || "model") + " · " + esc(runtime.context_size || 0) + " tokens context"
        : "No model is loaded. Pick one and press Load.") + "</div>" +
      rows +
      '<div class="section-note mt-12">Delete removes the GGUF files from disk. Downloaded models can be re-downloaded at any time.</div>'
    );
    $$("[data-load-model]", state.modal.body).forEach((button) => {
      button.addEventListener("click", () => loadModel(button.dataset.loadModel, ui));
    });
    const unload = $("[data-unload-model]", state.modal.body);
    if (unload) unload.addEventListener("click", async () => {
      try {
        await API.models.unload();
        toast("Model unloaded", "ok");
        await loadModels();
        renderModelsTab(ui);
      } catch (err) { toast(errorMessage(err), "error"); }
    });
    $$("[data-model-caps]", state.modal.body).forEach((button) => {
      button.addEventListener("click", () => {
        const item = installed.find((m) => m.id === button.dataset.modelCaps);
        openCapabilities(item ? item.capabilities : null);
      });
    });
    $$("[data-delete-model]", state.modal.body).forEach((button) => {
      button.addEventListener("click", async () => {
        const item = installed.find((m) => m.id === button.dataset.deleteModel);
        const ok = await confirmDialog("Delete model?",
          "This deletes " + (item ? item.name : "this model") + " from disk. You can download it again later.",
          "Delete", true);
        if (!ok) return;
        try {
          await API.models.remove(button.dataset.deleteModel);
          toast("Model deleted", "ok");
          await loadModels();
          renderModelsTab(ui);
        } catch (err) { toast(errorMessage(err), "error"); }
      });
    });
  }

  async function loadModel(modelId, ui) {
    try {
      toast("Loading model… this can take a few seconds", "ok", 2500);
      await API.models.load(modelId);
      await sleep(900);
      await loadModels();
      if (ui && state.modal === ui.api) renderModelsTab(ui);
      renderEngine();
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  }

  function renderBrowseTab(ui, data) {
    const categories = data.categories || {};
    const catalog = data.catalog || [];
    const installed = data.installed || [];
    const runtime = data.runtime || {};
    modalFoot(modelFoot("Downloads go to " + esc(data.disk ? fmtBytes(data.disk.free) : "?") + " free disk space"));
    bindModelFoot(ui);
    const catTabs = '<button class="cat-tab ' + (ui.category === "all" ? "active" : "") +
      '" type="button" data-cat="all">All</button>' +
      Object.keys(categories).map((key) =>
        '<button class="cat-tab ' + (ui.category === key ? "active" : "") + '" type="button" data-cat="' +
        esc(key) + '">' + esc(categories[key]) + "</button>"
      ).join("");
    const query = ui.search.toLowerCase();
    const filtered = catalog.filter((entry) => {
      if (ui.category !== "all" && entry.category !== ui.category) return false;
      if (!query) return true;
      const hay = [entry.name, entry.blurb, entry.repo, entry.params, (entry.tags || []).join(" ")]
        .join(" ").toLowerCase();
      return hay.includes(query);
    });
    const cards = filtered.map((entry) => {
      const inst = (entry.installed || [])[0];
      const loaded = inst && (inst.loaded || runtime.model_id === inst.id);
      const cap = entry.capabilities || (inst && inst.capabilities) || {};
      return '<div class="model-card">' +
        '<div class="model-card-top"><div class="model-name">' + esc(entry.name) + "</div>" +
          (entry.params ? '<span class="model-badge">' + esc(entry.params) + "</span>" : "") +
          fitBadge(entry.fit, entry.gpu_fit) +
        "</div>" +
        '<div class="model-blurb">' + esc(entry.blurb || "") + "</div>" +
        capabilityBadges(cap) +
        '<div class="model-meta">' +
          "<span>" + esc(fmtBytes(entry.size || 0)) + "</span>" +
          "<span>" + esc(entry.quant || "GGUF") + "</span>" +
          "<span>" + (entry.context ? Math.round(entry.context / 1024) + "k ctx" : "auto ctx") + "</span>" +
          "<span>" + esc(entry.license || "") + "</span>" +
        "</div>" +
        '<div class="model-actions">' +
          (inst
            ? (loaded
              ? '<button class="ghost-btn small" type="button" data-unload-model>Unload</button>'
              : '<button class="primary-btn small" type="button" data-load-model="' + esc(inst.id) + '">Load</button>') +
              '<span class="model-badge accent">installed</span>'
            : '<button class="primary-btn small" type="button" data-download-repo="' + esc(entry.repo) + '">Download</button>') +
          '<button class="ghost-btn small" type="button" data-repo-details="' + esc(entry.repo) + '">Details</button>' +
        "</div>" +
      "</div>";
    }).join("");
    modalBody(
      '<div class="model-toolbar">' +
        '<input class="input" type="search" data-model-search placeholder="Filter models…" value="' + esc(ui.search) + '">' +
        '<button class="ghost-btn" type="button" data-hf-search>Search Hugging Face…</button>' +
        '<button class="ghost-btn" type="button" data-url-download>Add from URL</button>' +
      "</div>" +
      '<div class="cat-tabs">' + catTabs + "</div>" +
      '<div class="model-grid mt-12">' + (cards || '<div class="empty-state" style="grid-column:1/-1">No models match this filter.</div>') + "</div>" +
      '<div class="section-note mt-12">RAM fit is an estimate. Larger quants are slower and use more memory. Vision models also need an mmproj projector.</div>'
    );
    const search = $("[data-model-search]", state.modal.body);
    if (search) {
      search.addEventListener("input", debounce(() => {
        ui.search = search.value.trim();
        renderBrowseTab(ui, data);
        const again = $("[data-model-search]", state.modal.body);
        if (again) { again.focus(); again.setSelectionRange(again.value.length, again.value.length); }
      }, 220));
    }
    $$("[data-cat]", state.modal.body).forEach((button) => {
      button.addEventListener("click", () => {
        ui.category = button.dataset.cat;
        renderBrowseTab(ui, data);
      });
    });
    $$("[data-download-repo]", state.modal.body).forEach((button) => {
      button.addEventListener("click", () => {
        const entry = catalog.find((e) => e.repo === button.dataset.downloadRepo) ||
          { repo: button.dataset.downloadRepo, name: button.dataset.downloadRepo };
        openRepoPicker(entry);
      });
    });
    $$("[data-repo-details]", state.modal.body).forEach((button) => {
      button.addEventListener("click", () => {
        const entry = catalog.find((e) => e.repo === button.dataset.repoDetails) ||
          { repo: button.dataset.repoDetails, name: button.dataset.repoDetails };
        openRepoPicker(entry);
      });
    });
    $$("[data-load-model]", state.modal.body).forEach((button) => {
      button.addEventListener("click", () => loadModel(button.dataset.loadModel, ui));
    });
    const unload = $("[data-unload-model]", state.modal.body);
    if (unload) unload.addEventListener("click", async () => {
      try {
        await API.models.unload();
        toast("Model unloaded", "ok");
        await loadModels();
        renderModelsTab(ui);
      } catch (err) { toast(errorMessage(err), "error"); }
    });
    const hf = $("[data-hf-search]", state.modal.body);
    if (hf) hf.addEventListener("click", openHfSearch);
    const url = $("[data-url-download]", state.modal.body);
    if (url) url.addEventListener("click", addFromUrl);
  }

  async function openRepoPicker(entry) {
    const repo = entry.repo;
    const modal = openModal({
      title: entry.name || repo,
      sub: repo,
      body: '<div class="empty-state"><span class="spinner"></span> Reading the repository…</div>',
      foot: '<button class="ghost-btn" type="button" data-picker-cancel>Cancel</button>',
    });
    $("[data-picker-cancel]", modal.foot).addEventListener("click", closeModal);
    try {
      const data = await API.models.repo(repo);
      if (state.modal !== modal) return;
      const quants = data.quants || [];
      const mmproj = data.mmproj || [];
      const recommended = quants.find((q) => q.recommended) || quants[0];
      if (!quants.length) {
        modalBody('<div class="empty-state">No GGUF files were found in this repository.</div>');
        return;
      }
      const rows = quants.map((q, index) => {
        const selected = recommended && q.key === recommended.key;
        return '<label class="quant-row" style="cursor:pointer">' +
          '<input type="radio" name="quant" value="' + esc(q.key) + '" ' + (selected ? "checked" : "") + ">" +
          '<span class="qname">' + esc(q.quant || q.name) + "</span>" +
          (q.parts > 1 ? '<span class="model-badge">' + q.parts + " parts</span>" : "") +
          fitBadge(q.fit, q.gpu_fit) +
          '<span class="qsize">' + esc(fmtBytes(q.size)) + "</span>" +
        "</label>";
      }).join("");
      const mm = mmproj.length ? '<div class="section-title">Vision projector (optional)</div>' +
        '<div class="section-note">Required for images and scanned PDFs. Download the mmproj file with the model.</div>' +
        mmproj.map((file, index) =>
          '<label class="check-row"><input type="checkbox" name="mmproj" value="' + index + '" ' +
          (index === 0 ? "checked" : "") + "> " + esc(file.path) +
          ' <span class="muted tiny">' + esc(fmtBytes(file.size)) + "</span></label>"
        ).join("") : "";
      modalBody(
        '<div class="form-grid one">' +
          '<div class="field"><label>Quantization</label>' + rows + "</div>" +
        "</div>" + mm +
        '<div class="section-note mt-12" data-fit-warning></div>' +
        '<div class="section-note mt-12">Context: ' + esc(data.entry && data.entry.context ? data.entry.context : "model default") +
        " tokens. LocalCode compacts automatically, so long chats never hit a hard limit.</div>" +
        (data.capabilities ? capabilityBadges(data.capabilities) : "")
      );
      const fitWarning = $("[data-fit-warning]", modal.body);
      const updateFitWarning = () => {
        if (!fitWarning) return;
        const chosen = quants.find((q) => {
          const box = $('input[name="quant"]:checked', modal.body);
          return box && q.key === box.value;
        });
        if (chosen && chosen.fit === "no") {
          fitWarning.style.color = "var(--danger)";
          fitWarning.textContent = "This quantization is too large for this PC's RAM (" +
            fmtBytes(chosen.size) + " model). It will probably fail to load — pick a smaller quant, or download it for another computer.";
        } else {
          fitWarning.style.color = "";
          fitWarning.textContent = "Fit badges are a guide: “fits RAM” leaves room for Windows and the app; “GPU partial” means some layers run on the GPU and the rest in RAM.";
        }
      };
      $$('input[name="quant"]', modal.body).forEach((box) =>
        box.addEventListener("change", updateFitWarning));
      updateFitWarning();
      modalFoot('<div class="left">' + esc(fmtBytes(quants[0].size)) + " recommended</div>" +
        '<button class="ghost-btn" type="button" data-picker-cancel>Cancel</button>' +
        '<button class="primary-btn" type="button" data-picker-download>Download &amp; load</button>');
      $("[data-picker-cancel]", modal.foot).addEventListener("click", closeModal);
      $("[data-picker-download]", modal.foot).addEventListener("click", async () => {
        const selected = $('input[name="quant"]:checked', modal.body);
        if (!selected) { toast("Choose a quantization first.", "error"); return; }
        const quant = quants.find((q) => q.key === selected.value);
        if (!quant) return;
        const mmFiles = $$('input[name="mmproj"]:checked', modal.body).map((box) =>
          mmproj[Number(box.value)]
        ).filter(Boolean);
        const button = $("[data-picker-download]", modal.foot);
        button.disabled = true;
        button.textContent = "Starting…";
        try {
          await API.models.download({
            repo: data.repo,
            files: quant.files,
            mmproj: mmFiles,
            name: (data.entry && data.entry.name) || entry.name || repo.split("/").pop(),
            quant: quant.quant,
            category: (data.entry && data.entry.category) || entry.category || "local",
            params: (data.entry && data.entry.params) || entry.params || "",
            context: (data.entry && data.entry.context) || entry.context || null,
            autoload: true,
          });
          closeModal();
          toast("Download started. The model will load automatically when it finishes.", "ok", 6000);
          openModels("downloads");
        } catch (err) {
          button.disabled = false;
          button.textContent = "Download & load";
          toast(errorMessage(err), "error");
        }
      });
    } catch (err) {
      if (state.modal === modal) {
        modalBody('<div class="empty-state">' + esc(errorMessage(err)) + "</div>");
      }
    }
  }

  function openHfSearch() {
    const modal = openModal({
      title: "Hugging Face",
      sub: "Search public GGUF repositories",
      body:
        '<div class="model-toolbar"><input class="input" type="search" data-hf-query placeholder="e.g. qwen coder, deepseek, llama">' +
        '<button class="primary-btn" type="button" data-hf-go>Search</button></div>' +
        '<div data-hf-results><div class="empty-state">Search for a model family or paste a repo name.</div></div>',
      foot: '<button class="ghost-btn" type="button" data-hf-close>Close</button>',
    });
    $("[data-hf-close]", modal.foot).addEventListener("click", closeModal);
    const input = $("[data-hf-query]", modal.body);
    const results = $("[data-hf-results]", modal.body);
    async function search() {
      const query = input.value.trim();
      results.innerHTML = '<div class="empty-state"><span class="spinner"></span> Searching…</div>';
      try {
        const data = await API.models.search(query, 24);
        const items = data.results || [];
        results.innerHTML = items.length ? items.map((item) =>
          '<div class="installed-row">' +
            '<div class="grow"><div class="title">' + esc(item.id) + "</div>" +
            '<div class="sub">' + fmtNum(item.downloads) + " downloads · " + fmtNum(item.likes) + " likes · " +
            esc((item.tags || []).slice(0, 4).join(", ")) + "</div></div>" +
            '<button class="soft-btn small" type="button" data-hf-open="' + esc(item.id) + '">View quants</button>' +
          "</div>"
        ).join("") : '<div class="empty-state">No GGUF repositories found.</div>';
        $$("[data-hf-open]", results).forEach((button) => {
          button.addEventListener("click", () => {
            const id = button.dataset.hfOpen;
            closeModal();
            openRepoPicker({ repo: id, name: id.split("/").pop(), category: "local" });
          });
        });
      } catch (err) {
        results.innerHTML = '<div class="empty-state">' + esc(errorMessage(err)) + "</div>";
      }
    }
    $("[data-hf-go]", modal.body).addEventListener("click", search);
    input.addEventListener("keydown", (event) => { if (event.key === "Enter") search(); });
    input.focus();
  }

  async function addFromUrl() {
    const url = await promptValue("Add a GGUF from URL", "Direct .gguf download URL", "");
    if (url === null || !url.trim()) return;
    try {
      await API.models.downloadUrl({ url: url.trim(), category: "local" });
      toast("Download started", "ok");
      openModels("downloads");
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  }

  function renderDownloadsTab(ui, jobs) {
    modalFoot(modelFoot("Downloads continue in the background, even if you close this window."));
    bindModelFoot(ui);
    const active = jobs.filter((job) => job.status === "downloading" || job.status === "queued");
    if (!jobs.length) {
      modalBody('<div class="empty-state"><strong>No downloads</strong>' +
        "Choose a model from the Browse tab, or add a direct GGUF URL." +
        '<div class="btn-row mt-12" style="justify-content:center">' +
        '<button class="primary-btn" type="button" data-url-download>Add from URL</button>' +
        '<button class="ghost-btn" type="button" data-goto-browse>Browse models</button></div></div>');
      const browse = $("[data-goto-browse]", state.modal.body);
      if (browse) browse.addEventListener("click", () => {
        ui.tab = "browse";
        $$("[data-tab]", state.modal.tabs).forEach((b) => b.classList.toggle("active", b.dataset.tab === "browse"));
        renderModelsTab(ui);
      });
      const url = $("[data-url-download]", state.modal.body);
      if (url) url.addEventListener("click", addFromUrl);
      return;
    }
    const rows = jobs.map((job) => {
      const percent = Number(job.percent || 0);
      const status = job.status || "queued";
      const statusClass = status === "error" ? "danger" : (status === "complete" ? "accent" : "");
      const speed = job.speed ? fmtBytes(job.speed) + "/s" : "";
      const eta = job.eta ? " · " + fmtDuration(job.eta * 1000) + " left" : "";
      return '<div class="download-row" data-job-id="' + esc(job.id) + '">' +
        '<div class="download-top"><span class="name">' + esc(job.name) + "</span>" +
          '<span class="model-badge ' + statusClass + '">' + esc(status) + "</span>" +
          '<span class="muted tiny">' + percent.toFixed(0) + "%</span></div>" +
        '<div class="progress"><div style="width:' + Math.max(0, Math.min(100, percent)) + '%"></div></div>' +
        '<div class="download-sub"><span>' + esc(fmtBytes(job.downloaded)) + " / " + esc(fmtBytes(job.total)) +
          " · " + esc(job.quant || "GGUF") + " · " + esc(job.repo) + "</span>" +
          '<span>' + esc(speed + eta) + "</span></div>" +
        (job.error ? '<div class="section-note" style="color:var(--danger)">' + esc(job.error) + "</div>" : "") +
        '<div class="btn-row mt-8">' +
          (status === "downloading" || status === "queued"
            ? '<button class="ghost-btn small" type="button" data-cancel-job="' + esc(job.id) + '">Cancel</button>'
            : '<button class="ghost-btn small" type="button" data-remove-job="' + esc(job.id) + '">Remove</button>') +
          (status === "complete" ? '<button class="primary-btn small" type="button" data-goto-installed>Load model</button>' : "") +
        "</div>" +
      "</div>";
    }).join("");
    modalBody(
      '<div class="section-note">' + (active.length
        ? active.length + " active download" + (active.length === 1 ? "" : "s") + ". Progress updates automatically."
        : "No active downloads.") + "</div>" + rows
    );
    $$("[data-cancel-job]", state.modal.body).forEach((button) => {
      button.addEventListener("click", async () => {
        try { await API.models.cancel(button.dataset.cancelJob); toast("Download cancelled", "ok"); renderModelsTab(ui); }
        catch (err) { toast(errorMessage(err), "error"); }
      });
    });
    $$("[data-remove-job]", state.modal.body).forEach((button) => {
      button.addEventListener("click", async () => {
        try { await API.models.removeJob(button.dataset.removeJob); renderModelsTab(ui); }
        catch (err) { toast(errorMessage(err), "error"); }
      });
    });
    const go = $("[data-goto-installed]", state.modal.body);
    if (go) go.addEventListener("click", () => {
      ui.tab = "installed";
      $$("[data-tab]", state.modal.tabs).forEach((b) => b.classList.toggle("active", b.dataset.tab === "installed"));
      renderModelsTab(ui);
    });
  }

  function renderRuntimeTab(ui, runtime) {
    const install = runtime.install || {};
    const installing = install.status === "working" || install.status === "starting";
    const flavor = runtime.flavor || (install && install.flavor) ||
      (runtime.installed ? "installed" : "not installed");
    const ngl = Number(runtime.gpu_layers);
    let gpuLayersLabel = "—";
    if (flavor === "cpu") gpuLayersLabel = "CPU only";
    else if (flavor === "cuda" || flavor === "vulkan") {
      gpuLayersLabel = !Number.isFinite(ngl) || ngl < 0
        ? "Auto (fit to VRAM)"
        : ngl >= 999 ? "All layers"
          : ngl > 0 ? ngl + " layers"
            : "CPU only";
    }
    const statusBits = [];
    statusBits.push('<div class="status-line"><span class="dot ' +
      (runtime.ready ? "ok" : (runtime.loading || installing ? "busy" : (runtime.error ? "bad" : ""))) +
      '"></span>' + (runtime.ready ? "Engine ready" : (installing ? "Installing…" : (runtime.loading ? "Loading model…" : "Stopped"))) + "</div>");
    let html =
      '<div class="runtime-card">' +
        '<div class="top"><strong>llama.cpp runtime</strong><span class="model-badge">' + esc(flavor) + "</span></div>" +
        statusBits.join("") +
        '<div class="kv-row"><span class="k">Installed</span><span class="v">' + (runtime.installed ? "yes" : "no") + "</span></div>" +
        '<div class="kv-row"><span class="k">Version</span><span class="v">' + esc(runtime.version || "—") + "</span></div>" +
        '<div class="kv-row"><span class="k">Port</span><span class="v">' + esc(runtime.port || "—") + "</span></div>" +
        '<div class="kv-row"><span class="k">Model</span><span class="v">' + esc(runtime.model_name || "none") + "</span></div>" +
        '<div class="kv-row"><span class="k">Context</span><span class="v">' + esc(runtime.context_size || 0) + " tokens</span></div>" +
        '<div class="kv-row"><span class="k">GPU layers</span><span class="v">' +
          esc(gpuLayersLabel) + "</span></div>" +
        (runtime.vision ? '<div class="kv-row"><span class="k">Vision projector</span><span class="v">loaded</span></div>' : "") +
        (runtime.error ? '<div class="section-note" style="color:var(--danger)">' + esc(runtime.error) + "</div>" : "") +
      "</div>";
    if (installing) {
      html += '<div class="runtime-card">' +
        '<div class="download-top"><span class="name">' + esc(install.message || "Installing runtime…") + "</span>" +
        '<span class="muted tiny">' + Number(install.percent || 0).toFixed(0) + "%</span></div>" +
        '<div class="progress"><div style="width:' + Math.max(0, Math.min(100, Number(install.percent || 0))) + '%"></div></div>' +
        '<div class="download-sub"><span>' + esc(fmtBytes(install.bytes || 0)) + " / " + esc(fmtBytes(install.total || 0)) + "</span></div>" +
      "</div>";
    }
    html += '<div class="section-title">Install or update</div>' +
      '<div class="section-note">Auto detects the best backend for this machine. CUDA uses an NVIDIA GPU, Vulkan works with most GPUs (including Intel integrated graphics). With GPU layers set to Auto, llama.cpp offloads as many layers as fit in VRAM and keeps the rest on the CPU — both work on the same model at once.</div>' +
      '<div class="btn-row">' +
        '<button class="primary-btn" type="button" data-install-flavor="auto" ' + (installing ? "disabled" : "") + ">Auto (recommended)</button>" +
        '<button class="soft-btn" type="button" data-install-flavor="cpu" ' + (installing ? "disabled" : "") + ">CPU</button>" +
        '<button class="soft-btn" type="button" data-install-flavor="vulkan" ' + (installing ? "disabled" : "") + ">Vulkan</button>" +
        '<button class="soft-btn" type="button" data-install-flavor="cuda" ' + (installing ? "disabled" : "") + ">CUDA</button>" +
        (runtime.running ? '<button class="danger-btn" type="button" data-stop-runtime>Stop engine</button>' : "") +
      "</div>" +
      '<div class="section-title">Runtime log</div>' +
      '<div class="mcp-logs">' + esc((runtime.logs || "No runtime log yet.").slice(-12000)) + "</div>";
    modalFoot(modelFoot("Runtime files live in the LocalCode runtime folder."));
    bindModelFoot(ui);
    modalBody(html);
    $$("[data-install-flavor]", state.modal.body).forEach((button) => {
      button.addEventListener("click", async () => {
        try {
          await API.runtime.install(button.dataset.installFlavor);
          if (button.dataset.installFlavor === "cpu") {
            API.settings.patch({ gpu_layers: 0, runtime_flavor: "cpu" }).catch(() => {});
            toast("CPU runtime installation started", "ok");
          } else {
            // GPU builds default to Auto: llama.cpp offloads as many layers
            // as actually fit in VRAM (CPU+GPU together) instead of guessing.
            API.settings.patch({ gpu_layers: -1, runtime_flavor: button.dataset.installFlavor })
              .then(() => toast("GPU engine install started — GPU layers set to Auto.", "ok", 5000))
              .catch(() => {});
          }
          setTimeout(() => renderModelsTab(ui), 700);
        } catch (err) { toast(errorMessage(err), "error"); }
      });
    });
    const stop = $("[data-stop-runtime]", state.modal.body);
    if (stop) stop.addEventListener("click", async () => {
      try {
        await API.runtime.stop();
        toast("Engine stopped", "ok");
        renderModelsTab(ui);
      } catch (err) { toast(errorMessage(err), "error"); }
    });
  }

  // ------------------------------------------------------------------ settings
  function fieldHtml(label, inner, hint) {
    return '<div class="field"><label>' + esc(label) + "</label>" + inner +
      (hint ? '<span class="hint">' + esc(hint) + "</span>" : "") + "</div>";
  }

  function inputSetting(key, label, value, options) {
    const opts = options || {};
    return fieldHtml(label,
      '<input class="input" data-setting="' + esc(key) + '" type="' + (opts.type || "text") + '"' +
      (opts.min !== undefined ? ' min="' + opts.min + '"' : "") +
      (opts.max !== undefined ? ' max="' + opts.max + '"' : "") +
      (opts.step !== undefined ? ' step="' + opts.step + '"' : "") +
      ' value="' + esc(value === null || value === undefined ? "" : value) + '">',
      opts.hint);
  }

  function selectSetting(key, label, value, choices, hint) {
    return fieldHtml(label,
      '<select class="select" data-setting="' + esc(key) + '">' + choices.map((choice) =>
        '<option value="' + esc(choice[0]) + '"' + (String(choice[0]) === String(value) ? " selected" : "") +
        ">" + esc(choice[1]) + "</option>"
      ).join("") + "</select>", hint);
  }

  function checkSetting(key, label, value, hint) {
    return '<label class="check-row"><input type="checkbox" data-setting="' + esc(key) + '" ' +
      (value ? "checked" : "") + "> <span>" + esc(label) +
      (hint ? ' <span class="muted tiny">' + esc(hint) + "</span>" : "") + "</span></label>";
  }

  function openSettings(tab) {
    const ui = { tab: tab || "general", settings: null, system: null, api: null, usage: null };
    const api = openModal({
      title: "Settings",
      sub: "Everything stays on this machine",
      body: '<div class="empty-state"><span class="spinner"></span> Loading settings…</div>',
      foot: '<button class="ghost-btn" type="button" data-settings-reset ' +
        'style="margin-right:auto;color:var(--danger)">Start from scratch</button>' +
        '<button class="ghost-btn" type="button" data-settings-quit ' +
        'style="color:var(--danger)">Quit LocalCode</button>' +
        '<button class="ghost-btn" type="button" data-settings-close>Close</button>' +
        '<button class="primary-btn" type="button" data-settings-save>Save changes</button>',
      onMount(modal) {
        $("[data-settings-close]", modal.foot).addEventListener("click", closeModal);
        $("[data-settings-save]", modal.foot).addEventListener("click", () => saveSettings(ui));
        $("[data-settings-reset]", modal.foot).addEventListener("click", startFromScratch);
        $("[data-settings-quit]", modal.foot).addEventListener("click", async () => {
          const ok = await confirmDialog(
            "Quit LocalCode?",
            "The local model will be unloaded and the app will close. Your chats are already saved.",
            "Quit", true
          );
          if (!ok) return;
          try {
            await API.system.quit();
          } catch (err) {
            // The process may exit before the response is read.
          }
          // Close the dedicated app window; harmless in a normal browser tab.
          setTimeout(() => { try { window.close(); } catch (_) { /* ignore */ } }, 500);
        });
      },
    });
    ui.api = api;
    Promise.allSettled([API.settings.get(), API.usage.get()]).then(([settingsResult, usageResult]) => {
      if (state.modal !== api) return;
      if (settingsResult.status === "fulfilled") {
        ui.settings = settingsResult.value.settings || {};
        ui.system = settingsResult.value.system || {};
        state.settings = ui.settings;
        applyTheme();
      } else {
        ui.settings = state.settings || {};
        ui.system = state.system || {};
      }
      if (usageResult.status === "fulfilled") ui.usage = usageResult.value.usage || {};
      renderSettingsTabs(ui);
      renderSettingsTab(ui);
    });
  }

  function renderSettingsTabs(ui) {
    const tabs = [
      ["general", "General"], ["model", "Model & context"],
      ["connection", "Connection"], ["data", "Data & privacy"],
    ];
    const tabsEl = ui.api.tabs;
    tabsEl.hidden = false;
    tabsEl.innerHTML = tabs.map(([id, label]) =>
      '<button class="tab-btn' + (ui.tab === id ? " active" : "") + '" type="button" data-tab="' +
      esc(id) + '">' + esc(label) + "</button>"
    ).join("");
    $$("[data-tab]", tabsEl).forEach((button) => {
      button.addEventListener("click", () => {
        collectSettingsForm(ui);
        ui.tab = button.dataset.tab;
        renderSettingsTabs(ui);
        renderSettingsTab(ui);
      });
    });
  }

  function collectSettingsForm(ui) {
    if (!ui.api || !ui.api.body) return;
    $$("[data-setting]", ui.api.body).forEach((input) => {
      const key = input.dataset.setting;
      if (!key) return;
      ui.settings[key] = input.type === "checkbox" ? input.checked : input.value;
    });
  }

  function renderSettingsTab(ui) {
    const s = ui.settings || {};
    const system = ui.system || {};
    const api = ui.api;
    if (state.modal !== api) return;
    let html = "";
    if (ui.tab === "general") {
      const ram = system.ram ? fmtBytes(system.ram) : "unknown";
      const gpus = (system.gpus || []).map((g) => g.name).join(", ") || "none detected";
      html =
        '<div class="section-title">Workspace</div>' +
        '<div class="form-grid one">' +
          fieldHtml("Project folder",
            '<div class="btn-row"><input class="input" data-setting="workspace" type="text" value="' +
            esc(s.workspace || "") + '"><button class="soft-btn" type="button" data-pick-folder>Browse…</button></div>',
            "The agent can only read and write inside this folder.") +
        "</div>" +
        '<div class="section-title">Appearance & behaviour</div>' +
        '<div class="form-grid">' +
          selectSetting("theme", "Theme", s.theme || "dark", [["dark", "Dark"], ["light", "Light"]]) +
          selectSetting("approval_mode", "Tool approvals", s.approval_mode || "ask",
            [["ask", "Ask first (recommended)"], ["auto", "Auto-run write & shell tools"]]) +
        "</div>" +
        '<div class="mt-8">' +
          checkSetting("show_reasoning", "Show model thinking", s.show_reasoning !== false) +
          checkSetting("show_costs", "Show token usage and hosted-cost estimates", s.show_costs !== false) +
          checkSetting("auto_compact", "Automatically compact long chats", s.auto_compact !== false,
            "Keeps every chat running forever; full transcripts stay saved.") +
          checkSetting("keep_alive", "Keep the model loaded when idle", s.keep_alive !== false) +
          checkSetting("mcp_auto_start", "Connect enabled MCP servers on startup", s.mcp_auto_start !== false) +
          checkSetting("auto_load", "Load the last model on startup", !!s.auto_load) +
        "</div>" +
        '<div class="section-title">This machine</div>' +
        '<div class="kv-row"><span class="k">Operating system</span><span class="v">' + esc(system.os || "—") + "</span></div>" +
        '<div class="kv-row"><span class="k">CPU</span><span class="v">' + esc(system.cpu || "—") + " · " + esc(system.cpu_count || "?") + " threads</span></div>" +
        '<div class="kv-row"><span class="k">RAM</span><span class="v">' + esc(ram) + "</span></div>" +
        '<div class="kv-row"><span class="k">GPU</span><span class="v">' + esc(gpus) + "</span></div>" +
        '<div class="kv-row"><span class="k">Models folder</span><span class="v">' + esc(system.models_dir || "—") + "</span></div>" +
        '<div class="kv-row"><span class="k">Data folder</span><span class="v">' + esc(system.data_dir || "—") + "</span></div>" +
        '<div class="btn-row mt-12"><button class="soft-btn" type="button" data-open-folder="workspace">Open workspace</button>' +
        '<button class="soft-btn" type="button" data-open-folder="models">Open models</button>' +
        '<button class="soft-btn" type="button" data-open-folder="data">Open data</button>' +
        '<button class="soft-btn" type="button" data-open-folder="logs">Open logs</button></div>';
    } else if (ui.tab === "model") {
      html =
        '<div class="section-note">Context size 0 means automatic: LocalCode picks the largest safe window for this machine and compacts before it overflows.</div>' +
        '<div class="form-grid">' +
          inputSetting("context_size", "Context size (tokens)", s.context_size || 0,
            { type: "number", min: 0, max: 131072, step: 1024, hint: "0 = automatic" }) +
          inputSetting("max_steps", "Maximum agent steps", s.max_steps || 0,
            { type: "number", min: 0, max: 10000, hint: "0 = unlimited" }) +
          inputSetting("threads", "CPU threads", s.threads || 0,
            { type: "number", min: 0, max: 64, hint: "0 = automatic" }) +
          inputSetting("gpu_layers", "GPU layers", s.gpu_layers === undefined ? -1 : s.gpu_layers,
            { type: "number", min: -1, max: 999, hint: "-1 = Auto (offload as many layers as fit in VRAM) · 0 = CPU only · 999 = all layers (needs CUDA or Vulkan)" }) +
          inputSetting("temperature", "Temperature", s.temperature === undefined ? 0.2 : s.temperature,
            { type: "number", min: 0, max: 2, step: 0.05 }) +
          inputSetting("max_tokens", "Max response tokens", s.max_tokens || 4096,
            { type: "number", min: 128, max: 32768, step: 128 }) +
          inputSetting("compact_threshold", "Compact at context %", s.compact_threshold || 0.72,
            { type: "number", min: 0.3, max: 0.95, step: 0.01, hint: "0.72 = 72%" }) +
          selectSetting("runtime_flavor", "Runtime build", s.runtime_flavor || "auto",
            [["auto", "Auto-detect"], ["cpu", "CPU"], ["vulkan", "Vulkan"], ["cuda", "CUDA"]]) +
        "</div>" +
        '<div class="section-note mt-12">Changing context, threads or GPU layers requires reloading the model. Chats themselves have no fixed message limit: older context is summarized into persistent session memory.</div>' +
        '<div class="btn-row"><button class="soft-btn" type="button" data-open-runtime>Open runtime manager</button>' +
        '<button class="soft-btn" type="button" data-open-models>Open model manager</button></div>';
    } else if (ui.tab === "connection") {
      html =
        '<div class="section-note">The default provider is the managed local llama.cpp engine. Ollama and any OpenAI-compatible server can be used instead.</div>' +
        '<div class="form-grid">' +
          selectSetting("provider", "Provider", s.provider || "managed",
            [["managed", "Managed local llama.cpp"], ["ollama", "Ollama"], ["openai", "OpenAI-compatible API"]]) +
          inputSetting("external_model", "External model name", s.external_model || "",
            { hint: "For Ollama: e.g. qwen2.5-coder:7b" }) +
          inputSetting("external_base_url", "External base URL", s.external_base_url || "http://127.0.0.1:11434/v1") +
          inputSetting("external_api_key", "External API key", s.external_api_key || "",
            { type: "password", hint: "Stored locally. Leave blank for Ollama." }) +
        "</div>" +
        '<div class="btn-row mt-12"><button class="soft-btn" type="button" data-probe-provider>Test connection</button>' +
        '<button class="soft-btn" type="button" data-provider-models>List provider models</button></div>' +
        '<div data-provider-result class="section-note mt-8"></div>' +
        '<div class="section-title">Hugging Face</div>' +
        inputSetting("hf_token", "Access token (optional)", s.hf_token || "",
          { type: "password", hint: "Only needed for gated model downloads." }) +
        '<div class="section-note mt-8">Downloads use huggingface.co. After a model is downloaded, chats are fully offline.</div>';
    } else {
      const usage = ui.usage || {};
      html =
        '<div class="section-title">Usage on this machine</div>' +
        '<div class="stat-grid">' +
          '<div class="stat"><div class="label">Chats</div><div class="value">' + fmtNum(usage.sessions || 0) + '</div><div class="sub">stored locally</div></div>' +
          '<div class="stat"><div class="label">Total tokens</div><div class="value">' + fmtNum(usage.total_tokens || 0) + '</div><div class="sub">all chats</div></div>' +
          '<div class="stat"><div class="label">Responses</div><div class="value">' + fmtNum(usage.responses || 0) + '</div><div class="sub">assistant turns</div></div>' +
          '<div class="stat"><div class="label">Hosted estimate</div><div class="value">' + fmtUsd(usage.cost_usd || 0) + '</div><div class="sub">local cost $0.00</div></div>' +
        "</div>" +
        '<div class="section-title">Your data</div>' +
        '<div class="section-note">Everything is stored in SQLite on this machine. There are no accounts, telemetry or cloud sync.</div>' +
        '<div class="btn-row">' +
          '<button class="soft-btn" type="button" data-export-all>Export all chats (.zip)</button>' +
          '<button class="soft-btn" type="button" data-open-usage>Open usage report</button>' +
          '<button class="soft-btn" type="button" data-open-memories>Persistent memories</button>' +
          '<button class="soft-btn" type="button" data-open-folder="data">Open data folder</button>' +
        "</div>" +
        '<div class="section-title">Session memory</div>' +
        '<div class="section-note">When a chat grows beyond the model context, older messages are summarized and kept as durable memory. The complete original transcript remains in the database and can be exported at any time.</div>';
    }
    modalBody(html);
    const pick = $("[data-pick-folder]", api.body);
    if (pick) pick.addEventListener("click", async () => {
      try {
        const result = await API.system.pickFolder();
        if (result && result.path) {
          const input = $('[data-setting="workspace"]', api.body);
          if (input) input.value = result.path;
        }
      } catch (err) { toast(errorMessage(err), "error"); }
    });
    $$("[data-open-folder]", api.body).forEach((button) => {
      button.addEventListener("click", () => {
        API.system.openFolder(button.dataset.openFolder).catch((err) => toast(errorMessage(err), "error"));
      });
    });
    const runtimeBtn = $("[data-open-runtime]", api.body);
    if (runtimeBtn) runtimeBtn.addEventListener("click", () => { closeModal(); openModels("runtime"); });
    const modelsBtn = $("[data-open-models]", api.body);
    if (modelsBtn) modelsBtn.addEventListener("click", () => { closeModal(); openModels("installed"); });
    const probe = $("[data-probe-provider]", api.body);
    if (probe) probe.addEventListener("click", async () => {
      collectSettingsForm(ui);
      await saveSettings(ui, true);
      try {
        const result = await API.provider.probe();
        const box = $("[data-provider-result]", api.body);
        if (box) box.textContent = result.ok
          ? "Connected in " + (result.ms || 0) + " ms · " + (result.label || "provider")
          : "Connection failed: " + (result.error || "unknown error");
        toast(result.ok ? "Provider connected" : "Provider failed", result.ok ? "ok" : "error");
      } catch (err) { toast(errorMessage(err), "error"); }
    });
    const listModels = $("[data-provider-models]", api.body);
    if (listModels) listModels.addEventListener("click", async () => {
      collectSettingsForm(ui);
      await saveSettings(ui, true);
      try {
        const result = await API.provider.models();
        const box = $("[data-provider-result]", api.body);
        if (box) box.textContent = "Models: " + (result.models || []).join(", ");
      } catch (err) { toast(errorMessage(err), "error"); }
    });
    const exportAll = $("[data-export-all]", api.body);
    if (exportAll) exportAll.addEventListener("click", () => downloadUrl(API.system.exportUrl()));
    const usageBtn = $("[data-open-usage]", api.body);
    if (usageBtn) usageBtn.addEventListener("click", () => { closeModal(); openUsage(); });
    const memoriesBtn = $("[data-open-memories]", api.body);
    if (memoriesBtn) memoriesBtn.addEventListener("click", () => { closeModal(); openMemories(); });
  }

  async function saveSettings(ui, quiet) {
    collectSettingsForm(ui);
    const payload = {};
    ["workspace", "provider", "theme", "approval_mode", "runtime_flavor", "external_base_url",
      "external_api_key", "external_model", "hf_token"].forEach((key) => {
      if (ui.settings[key] !== undefined) payload[key] = ui.settings[key];
    });
    ["context_size", "threads", "gpu_layers", "max_steps", "max_tokens"].forEach((key) => {
      if (ui.settings[key] !== undefined) payload[key] = Number(ui.settings[key] || 0);
    });
    ["temperature", "compact_threshold"].forEach((key) => {
      if (ui.settings[key] !== undefined) payload[key] = Number(ui.settings[key]);
    });
    ["auto_load", "show_reasoning", "auto_compact", "mcp_auto_start", "show_costs", "keep_alive"].forEach((key) => {
      if (ui.settings[key] !== undefined) payload[key] = !!ui.settings[key];
    });
    try {
      const data = await API.settings.patch(payload);
      state.settings = data.settings || state.settings;
      ui.settings = state.settings;
      applyTheme();
      updateApprovalMode();
      updateWelcomeWorkspace();
      updateHint();
      if (!quiet) toast("Settings saved", "ok");
      if (data.needs_reload) toast("Reload the model to apply context/thread changes.", "error", 6000);
      if (ui.tab === "general" || ui.tab === "connection") {
        // keep the current tab and refresh system values
        if (state.modal === ui.api) renderSettingsTab(ui);
      }
      await loadModels();
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  }

  // ------------------------------------------------------------------ memories
  async function openMemories() {
    const modal = openModal({
      title: "Persistent memories",
      sub: "Facts the agent should remember across chats",
      size: "medium",
      body: '<div class="empty-state"><span class="spinner"></span> Loading memories…</div>',
      foot: '<button class="ghost-btn" type="button" data-memory-close>Close</button>',
    });
    $("[data-memory-close]", modal.foot).addEventListener("click", closeModal);
    async function refresh() {
      if (state.modal !== modal) return;
      try {
        const data = await API.memories.list(state.settings && state.settings.workspace);
        const memories = data.memories || [];
        const rows = memories.length ? memories.map((memory) =>
          '<div class="memory-row" data-memory-id="' + esc(memory.id) + '">' +
            '<span class="scope">' + (memory.scope === "global" ? "global" : "workspace") + "</span>" +
            '<div class="grow">' + esc(memory.content) + "</div>" +
            '<button class="icon-btn small" type="button" data-delete-memory="' + esc(memory.id) + '" title="Delete">×</button>' +
          "</div>"
        ).join("") : '<div class="empty-state">No memories yet. Add a fact below.</div>';
        modalBody(
          '<div class="section-note">Memories are injected into every chat in this workspace. Use them for durable preferences, project conventions and facts the agent should not forget.</div>' +
          rows +
          '<div class="section-title">Add a memory</div>' +
          '<textarea class="textarea" data-memory-text rows="3" placeholder="e.g. This project uses tabs and Python 3.12. Never edit files in vendor/."></textarea>' +
          '<div class="btn-row mt-8"><select class="select" data-memory-scope style="max-width:220px">' +
            '<option value="workspace">This workspace</option><option value="global">All workspaces</option></select>' +
            '<button class="primary-btn" type="button" data-add-memory>Add memory</button></div>'
        );
        $$("[data-delete-memory]", modal.body).forEach((button) => {
          button.addEventListener("click", async () => {
            try { await API.memories.remove(button.dataset.deleteMemory); await refresh(); }
            catch (err) { toast(errorMessage(err), "error"); }
          });
        });
        const add = $("[data-add-memory]", modal.body);
        if (add) add.addEventListener("click", async () => {
          const text = $("[data-memory-text]", modal.body).value.trim();
          if (!text) { toast("Write the memory first.", "error"); return; }
          try {
            await API.memories.add({ content: text, scope: $("[data-memory-scope]", modal.body).value });
            toast("Memory saved", "ok");
            await refresh();
          } catch (err) { toast(errorMessage(err), "error"); }
        });
      } catch (err) {
        modalBody('<div class="empty-state">' + esc(errorMessage(err)) + "</div>");
      }
    }
    refresh();
  }

  // --------------------------------------------------------------------- usage
  function usageStatsHtml(usage) {
    return '<div class="stat-grid">' +
      '<div class="stat"><div class="label">Total tokens</div><div class="value">' + fmtNum(usage.total_tokens || 0) + '</div><div class="sub">' + fmtNum(usage.responses || 0) + " responses</div></div>" +
      '<div class="stat"><div class="label">Prompt tokens</div><div class="value">' + fmtNum(usage.prompt_tokens || 0) + '</div><div class="sub">input context</div></div>' +
      '<div class="stat"><div class="label">Completion tokens</div><div class="value">' + fmtNum(usage.completion_tokens || 0) + '</div><div class="sub">generated</div></div>' +
      '<div class="stat"><div class="label">Hosted estimate</div><div class="value">' + fmtUsd(usage.cost_usd || 0) + '</div><div class="sub">local cost $0.00</div></div>' +
    "</div>";
  }

  function usageTableHtml(usage) {
    const rows = (usage.by_model || []).map((row) =>
      '<tr><td>' + esc(row.model || "unknown") + "</td><td class=\"num\">" + fmtNum(row.prompt_tokens) +
      '</td><td class="num">' + fmtNum(row.completion_tokens) + '</td><td class="num">' + fmtNum(row.total_tokens) +
      '</td><td class="num">' + fmtUsd(row.cost_usd) + '</td><td class="num">' + fmtNum(row.responses) + "</td></tr>"
    ).join("");
    if (!rows) return '<div class="empty-state">No responses yet.</div>';
    return '<div class="table-wrap"><table class="data-table"><thead><tr><th>Model</th><th class="num">Prompt</th>' +
      '<th class="num">Completion</th><th class="num">Total</th><th class="num">Hosted cost</th><th class="num">Responses</th>' +
      "</tr></thead><tbody>" + rows + "</tbody></table></div>";
  }

  async function openUsage(sessionId) {
    const modal = openModal({
      title: "Usage & cost",
      sub: "Local inference is free; hosted estimates show what the same tokens would typically cost",
      body: '<div class="empty-state"><span class="spinner"></span> Calculating…</div>',
      foot: '<button class="ghost-btn" type="button" data-usage-close>Close</button>',
    });
    $("[data-usage-close]", modal.foot).addEventListener("click", closeModal);
    try {
      const results = await Promise.all([
        sessionId ? API.usage.get(sessionId) : Promise.resolve(null),
        API.usage.get(),
      ]);
      if (state.modal !== modal) return;
      const sessionData = results[0];
      const allData = results[1];
      let html = "";
      if (sessionData) {
        html += '<div class="section-title">This chat</div>' + usageStatsHtml(sessionData.usage || {}) +
          '<div class="mt-12">' + usageTableHtml(sessionData.usage || {}) + "</div>";
      }
      html += '<div class="section-title">All chats</div>' + usageStatsHtml(allData.usage || {}) +
        '<div class="mt-12">' + usageTableHtml(allData.usage || {}) + "</div>" +
        '<div class="section-note mt-12">' + esc((allData.pricing && allData.pricing.note) || "") + "</div>";
      modalBody(html);
    } catch (err) {
      modalBody('<div class="empty-state">' + esc(errorMessage(err)) + "</div>");
    }
  }

  // -------------------------------------------------------------- capabilities
  function openCapabilities(cap) {
    const data = cap || state.capabilities;
    if (!data) {
      openModal({
        title: "Model capabilities",
        size: "small",
        body: '<div class="empty-state"><strong>No model loaded</strong>Download and load a model to see its real capabilities.</div>',
        foot: '<button class="primary-btn" type="button" data-open-models>Open models</button>',
        onMount(modal) {
          $("[data-open-models]", modal.foot).addEventListener("click", () => { closeModal(); openModels("browse"); });
        },
      });
      return;
    }
    const rows = [
      ["Text", data.text, "Chat, code and tool calls"],
      ["Images", data.image, data.image ? "Vision input is ready" : (data.image_family ? "Vision family, but no mmproj installed" : "Not supported by this model")],
      ["PDF text", data.pdf, "Text is extracted and sent as context"],
      ["Scanned PDFs", data.pdf_scanned, data.pdf_scanned ? "Page images can be read" : "Needs a vision model + mmproj"],
      ["Audio", data.audio, data.audio ? "Audio input family detected" : "Not enabled in this build"],
      ["Video", data.video, data.video ? "Video family detected" : "Not enabled in this build"],
      ["Tools", data.tools, "Read, write, edit, run, search, fetch and ask"],
      ["MCP", data.mcp, "External MCP servers and their tools"],
      ["Reasoning", data.reasoning, data.reasoning ? "Thinking/reasoning traces supported" : "Standard responses"],
    ];
    openModal({
      title: "Model capabilities",
      sub: data.model || "Current model",
      size: "medium",
      body:
        '<div class="cap-badges">' + (data.badges || []).map((badge) =>
          '<span class="cap-badge ' + (badge.on ? "on" : "off") + '">' + esc(badge.label) + "</span>"
        ).join("") + "</div>" +
        '<div class="kv-row"><span class="k">Context window</span><span class="v">' +
          (data.context ? fmtNum(data.context) + " tokens" : "automatic") + "</span></div>" +
        '<div class="kv-row"><span class="k">Input</span><span class="v">' + esc((data.input || []).join(", ")) + "</span></div>" +
        '<div class="kv-row"><span class="k">Output</span><span class="v">' + esc((data.output || []).join(", ")) + "</span></div>" +
        '<div class="section-title">What this model can do</div>' +
        rows.map(([label, on, note]) =>
          '<div class="kv-row"><span class="k">' + esc(label) + '</span><span class="v">' +
          (on ? '<span style="color:var(--ok)">yes</span>' : '<span class="muted">no</span>') +
          ' · <span class="muted tiny">' + esc(note) + "</span></span></div>"
        ).join("") +
        ((data.notes || []).length ? '<div class="section-title">Notes</div>' +
          (data.notes || []).map((note) => '<div class="section-note">' + esc(note) + "</div>").join("") : ""),
      foot: '<button class="ghost-btn" type="button" data-caps-close>Close</button>' +
        '<button class="soft-btn" type="button" data-caps-models>Manage models</button>',
      onMount(modal) {
        $("[data-caps-close]", modal.foot).addEventListener("click", closeModal);
        $("[data-caps-models]", modal.foot).addEventListener("click", () => { closeModal(); openModels("installed"); });
      },
    });
  }

  // ---------------------------------------------------------------------- MCP
  function openMCP() {
    const ui = { api: null, timer: null, sig: "", loading: false };
    const api = openModal({
      title: "MCP servers",
      sub: "Connect external tools — including Roblox Studio",
      body: '<div class="empty-state"><span class="spinner"></span> Reading MCP configuration…</div>',
      foot: '<button class="ghost-btn" type="button" data-mcp-close>Close</button>' +
        '<button class="ghost-btn" type="button" data-mcp-refresh>Refresh</button>',
      onClose() { if (ui.timer) clearInterval(ui.timer); },
    });
    ui.api = api;
    $("[data-mcp-close]", api.foot).addEventListener("click", closeModal);
    $("[data-mcp-refresh]", api.foot).addEventListener("click", () => renderMCP(ui));
    renderMCP(ui);
    ui.timer = setInterval(() => {
      if (state.modal !== api) { clearInterval(ui.timer); return; }
      const active = document.activeElement;
      if (active && api.body.contains(active) && (active.tagName === "INPUT" || active.tagName === "TEXTAREA")) return;
      renderMCP(ui, true);
    }, 2200);
    return api;
  }

  async function renderMCP(ui, quiet) {
    const api = ui.api;
    if (!api || state.modal !== api || ui.loading) return;
    ui.loading = true;
    try {
      const data = await API.mcp.status();
      if (state.modal !== api) return;
      state.mcp = data;
      const signature = JSON.stringify({
        node: data.node && [data.node.status, data.node.installed, data.node.percent],
        servers: (data.servers || []).map((s) => [s.name, s.status, s.tool_count, s.error]),
      });
      if (quiet && signature === ui.sig) return;
      ui.sig = signature;
      renderMCPBody(ui, data);
    } catch (err) {
      if (state.modal === api) modalBody('<div class="empty-state">' + esc(errorMessage(err)) + "</div>");
    } finally {
      ui.loading = false;
    }
  }

  function renderMCPBody(ui, data) {
    const node = data.node || {};
    const servers = data.servers || [];
    const presets = data.presets || [];
    const nodeWorking = node.status === "working";
    let html =
      '<div class="runtime-card">' +
        '<div class="top"><strong>Node.js runtime</strong>' +
          '<span class="model-badge ' + (node.installed ? "accent" : "") + '">' +
          (node.installed ? "installed" : "not installed") + "</span></div>" +
        '<div class="status-line"><span class="dot ' + (node.installed ? "ok" : (nodeWorking ? "busy" : "")) + '"></span>' +
          (nodeWorking ? esc(node.message || "Installing Node.js…") :
            (node.installed ? "Node.js " + esc(node.version || "") + " ready for npx servers" : "Required for npx-based MCP servers")) +
        "</div>" +
        (node.node_path ? '<div class="mcp-cmd">' + esc(node.node_path) + "</div>" : "") +
        (nodeWorking ? '<div class="progress mt-8"><div style="width:' + Math.max(0, Math.min(100, Number(node.percent || 0))) + '%"></div></div>' : "") +
        (node.error ? '<div class="section-note" style="color:var(--danger)">' + esc(node.error) + "</div>" : "") +
        '<div class="btn-row mt-8">' +
          '<button class="primary-btn small" type="button" data-node-install ' + (nodeWorking ? "disabled" : "") + ">" +
            (node.installed ? "Update portable Node.js" : "Install portable Node.js") + "</button>" +
          '<span class="muted tiny">Downloads into the LocalCode runtime folder. No system-wide install.</span>' +
        "</div>" +
      "</div>";

    const roblox = presets.find((p) => p.id === "roblox-studio");
    if (roblox) {
      const installed = servers.find((s) => s.preset === "roblox-studio");
      html += '<div class="mcp-server" style="border-color:var(--accent-line);background:linear-gradient(135deg,var(--accent-dim),var(--panel-2))">' +
        '<div class="mcp-top"><span class="name">🎮 Roblox Studio</span>' +
          '<span class="model-badge ' + (installed ? (installed.status === "ready" ? "accent" : "") : "") + '">' +
          (installed ? esc(installed.status) : "recommended") + "</span></div>" +
        '<div class="section-note mt-8">Build, inspect and edit Roblox places from LocalCode. Requires Roblox Studio and the free MCP plugin.</div>' +
        '<div class="mcp-notes">Install the Studio plugin once:\n' +
          '<code>npx -y @chrrxs/robloxstudio-mcp@latest --install-plugin</code>\n\n' +
          'Then open Roblox Studio, open a place, add the server below and start it. The plugin connects to the local bridge automatically.</div>' +
        '<div class="btn-row mt-8">' +
          '<button class="soft-btn small" type="button" data-copy-text="npx -y @chrrxs/robloxstudio-mcp@latest --install-plugin">Copy plugin command</button>' +
          (installed
            ? '<button class="ghost-btn small" type="button" data-mcp-start="' + esc(installed.name) + '">Start</button>' +
              '<button class="ghost-btn small" type="button" data-mcp-logs="' + esc(installed.name) + '">Logs</button>'
            : '<button class="primary-btn small" type="button" data-mcp-preset="roblox-studio">Add &amp; start Roblox MCP</button>') +
        "</div>" +
      "</div>";
    }

    html += '<div class="section-title">Configured servers</div>';
    if (!servers.length) {
      html += '<div class="empty-state">No MCP servers yet. Add the Roblox preset above or pick one below.</div>';
    } else {
      html += servers.map((server) => {
        const ready = server.status === "ready";
        const tools = server.tools || [];
        return '<div class="mcp-server">' +
          '<div class="mcp-top"><span class="name">' + esc(server.name) + "</span>" +
            '<span class="model-badge ' + (ready ? "accent" : (server.status === "error" ? "danger" : "")) + '">' +
            esc(server.status) + "</span>" +
            (server.enabled ? "" : '<span class="model-badge">disabled</span>') +
          "</div>" +
          '<div class="mcp-cmd">' + esc(server.command || "") + " " + esc((server.args || []).join(" ")) + "</div>" +
          (server.error ? '<div class="section-note" style="color:var(--danger)">' + esc(server.error) + "</div>" : "") +
          (tools.length ? '<div class="mcp-tools">' + tools.slice(0, 40).map((tool) =>
            '<span class="mcp-tool" title="' + esc(tool.description || "") + '">' + esc(tool.name) + "</span>"
          ).join("") + (tools.length > 40 ? '<span class="mcp-tool">+' + (tools.length - 40) + " more</span>" : "") + "</div>" : "") +
          '<div class="btn-row mt-8">' +
            (ready
              ? '<button class="ghost-btn small" type="button" data-mcp-stop="' + esc(server.name) + '">Stop</button>' +
                '<button class="ghost-btn small" type="button" data-mcp-restart="' + esc(server.name) + '">Restart</button>'
              : '<button class="primary-btn small" type="button" data-mcp-start="' + esc(server.name) + '">Start</button>') +
            '<button class="ghost-btn small" type="button" data-mcp-logs="' + esc(server.name) + '">Logs</button>' +
            '<button class="danger-btn small" type="button" data-mcp-remove="' + esc(server.name) + '">Remove</button>' +
          "</div>" +
          ((server.logs || []).length ? '<details class="mt-8"><summary class="muted tiny">Last server output</summary>' +
            '<div class="mcp-logs">' + esc((server.logs || []).slice(-40).join("\n")) + "</div></details>" : "") +
        "</div>";
      }).join("");
    }

    html += '<div class="section-title">Add a preset</div>' +
      '<div class="preset-grid">' + presets.filter((p) => p.id !== "roblox-studio").map((preset) =>
        '<button class="preset-card" type="button" data-mcp-preset="' + esc(preset.id) + '">' +
          "<strong>" + esc(preset.name) + "</strong>" +
          "<span>" + esc(preset.description || "") + "</span>" +
        "</button>"
      ).join("") + "</div>";

    html += '<details class="mt-12"><summary class="section-title" style="cursor:pointer">Add a custom MCP server</summary>' +
      '<div class="form-grid mt-8">' +
        fieldHtml("Name", '<input class="input" data-mcp-name placeholder="my-server">') +
        fieldHtml("Command", '<input class="input" data-mcp-command placeholder="npx">') +
      "</div>" +
      fieldHtml("Arguments", '<input class="input" data-mcp-args placeholder="-y @scope/server@latest">', "Space-separated. Use quotes for arguments with spaces.") +
      fieldHtml("Environment", '<textarea class="textarea" data-mcp-env rows="3" placeholder="KEY=value&#10;ANOTHER_KEY=value"></textarea>', "One KEY=value per line.") +
      fieldHtml("Working directory (optional)", '<input class="input" data-mcp-cwd placeholder="C:\\path\\to\\folder">') +
      '<div class="btn-row mt-8"><button class="primary-btn" type="button" data-mcp-add>Add &amp; start</button></div>' +
    "</details>";
    modalBody(html);

    const installNode = $("[data-node-install]", ui.api.body);
    if (installNode) installNode.addEventListener("click", async () => {
      try {
        await API.mcp.installNode();
        toast("Installing portable Node.js…", "ok");
        setTimeout(() => renderMCP(ui), 600);
      } catch (err) { toast(errorMessage(err), "error"); }
    });
    $$("[data-mcp-preset]", ui.api.body).forEach((button) => {
      button.addEventListener("click", async () => {
        try {
          await API.mcp.addPreset({ preset: button.dataset.mcpPreset, start: true });
          toast("MCP server added", "ok");
          ui.sig = "";
          renderMCP(ui);
        } catch (err) { toast(errorMessage(err), "error"); }
      });
    });
    $$("[data-mcp-start]", ui.api.body).forEach((button) => {
      button.addEventListener("click", () => mcpAction(ui, button.dataset.mcpStart, "start"));
    });
    $$("[data-mcp-stop]", ui.api.body).forEach((button) => {
      button.addEventListener("click", () => mcpAction(ui, button.dataset.mcpStop, "stop"));
    });
    $$("[data-mcp-restart]", ui.api.body).forEach((button) => {
      button.addEventListener("click", () => mcpAction(ui, button.dataset.mcpRestart, "restart"));
    });
    $$("[data-mcp-remove]", ui.api.body).forEach((button) => {
      button.addEventListener("click", async () => {
        const ok = await confirmDialog("Remove MCP server?",
          "This removes " + button.dataset.mcpRemove + " from LocalCode. Its files and packages stay on disk.",
          "Remove", true);
        if (!ok) return;
        try {
          await API.mcp.remove(button.dataset.mcpRemove);
          toast("MCP server removed", "ok");
          ui.sig = "";
          renderMCP(ui);
        } catch (err) { toast(errorMessage(err), "error"); }
      });
    });
    $$("[data-mcp-logs]", ui.api.body).forEach((button) => {
      button.addEventListener("click", () => openMcpLogs(button.dataset.mcpLogs));
    });
    $$("[data-copy-text]", ui.api.body).forEach((button) => {
      button.addEventListener("click", () => {
        copyText(button.dataset.copyText).then((ok) => toast(ok ? "Copied" : "Copy failed", ok ? "ok" : "error"));
      });
    });
    const add = $("[data-mcp-add]", ui.api.body);
    if (add) add.addEventListener("click", async () => {
      const name = $("[data-mcp-name]", ui.api.body).value.trim();
      const command = $("[data-mcp-command]", ui.api.body).value.trim();
      if (!name || !command) { toast("Name and command are required.", "error"); return; }
      const args = $("[data-mcp-args]", ui.api.body).value.trim();
      const env = {};
      $("[data-mcp-env]", ui.api.body).value.split("\n").forEach((line) => {
        const index = line.indexOf("=");
        if (index > 0) env[line.slice(0, index).trim()] = line.slice(index + 1).trim();
      });
      try {
        await API.mcp.addServer({
          name, command,
          args: args ? args.match(/(?:[^\s"]+|"[^"]*")+/g).map((a) => a.replace(/^"|"$/g, "")) : [],
          env,
          cwd: $("[data-mcp-cwd]", ui.api.body).value.trim() || null,
          start: true,
        });
        toast("MCP server added", "ok");
        ui.sig = "";
        renderMCP(ui);
      } catch (err) { toast(errorMessage(err), "error"); }
    });
  }

  async function mcpAction(ui, name, action) {
    try {
      if (action === "start") await API.mcp.start(name);
      else if (action === "stop") await API.mcp.stop(name);
      else await API.mcp.restart(name);
      toast("MCP " + action + " requested", "ok", 1800);
      ui.sig = "";
      setTimeout(() => renderMCP(ui), 400);
    } catch (err) {
      toast(errorMessage(err), "error");
    }
  }

  function startFromScratch() {
    openModal({
      title: "Start from scratch",
      sub: "Wipe everything and set LocalCode up again like a brand-new install",
      size: "small",
      body:
        '<div class="section-note">This deletes every chat, memory, MCP tool, attachment and setting. ' +
        "The setup welcome screen will appear again afterwards.</div>" +
        '<label class="check-row"><input type="checkbox" data-scratch-models> ' +
        "Also delete downloaded models (" + (state.system && state.system.models_dir
          ? fmtBytes(state.system.model_disk || 0) : "") + " of files)</label>" +
        '<label class="check-row"><input type="checkbox" data-scratch-runtime> ' +
        "Also delete the llama.cpp engine and portable Node (re-downloads on next use)</label>",
      foot:
        '<button class="ghost-btn" type="button" data-scratch-cancel>Cancel</button>' +
        '<button class="danger-btn" type="button" data-scratch-confirm>Delete everything &amp; restart setup</button>',
      onMount(modal) {
        $("[data-scratch-cancel]", modal.foot).addEventListener("click", closeModal);
        $("[data-scratch-confirm]", modal.foot).addEventListener("click", async () => {
          const button = $("[data-scratch-confirm]", modal.foot);
          button.disabled = true;
          button.textContent = "Resetting…";
          try {
            await API.system.reset({
              models: !!$("[data-scratch-models]", modal.body).checked,
              runtime_files: !!$("[data-scratch-runtime]", modal.body).checked,
            });
            toast("LocalCode was reset. Welcome back!", "ok", 4000);
            setTimeout(() => window.location.reload(), 600);
          } catch (err) {
            button.disabled = false;
            button.textContent = "Delete everything & restart setup";
            toast(errorMessage(err), "error", 6000);
          }
        });
      },
    });
  }

  async function openMcpLogs(name) {
    const modal = openModal({
      title: "MCP logs · " + name,
      size: "medium",
      body: '<div class="empty-state"><span class="spinner"></span></div>',
      foot: '<button class="ghost-btn" type="button" data-log-close>Close</button>',
    });
    $("[data-log-close]", modal.foot).addEventListener("click", closeModal);
    try {
      const data = await API.mcp.logs(name);
      if (state.modal !== modal) return;
      modalBody('<div class="mcp-logs" style="max-height:60vh">' +
        esc((data.logs || []).join("\n") || "No output yet.") + "</div>");
    } catch (err) {
      if (state.modal === modal) modalBody('<div class="empty-state">' + esc(errorMessage(err)) + "</div>");
    }
  }

  // --------------------------------------------------------------- command palette
  function paletteItems() {
    const items = [
      { label: "New chat", desc: "Start a fresh conversation", key: "Ctrl N", run: newChat },
      { label: "Search chats", desc: "Filter the sidebar", key: "/", run: () => dom.sessionSearch && dom.sessionSearch.focus() },
      { label: "Models", desc: "Download, load and manage local models", key: "", run: () => openModels("installed") },
      { label: "Settings", desc: "Workspace, context, provider and data", key: "Ctrl ,", run: () => openSettings() },
      { label: "MCP servers", desc: "Roblox Studio and external tools", key: "", run: () => openMCP() },
      { label: "Usage & cost", desc: "Tokens and hosted-API estimates", key: "", run: () => openUsage(state.currentSessionId) },
      { label: "Toggle workspace files", desc: "Browse and preview project files", key: "Ctrl Shift F", run: () => toggleFiles() },
      { label: "Persistent memories", desc: "Facts remembered across chats", key: "", run: () => openMemories() },
      { label: "Toggle theme", desc: "Switch between dark and light", key: "", run: toggleTheme },
      { label: "Welcome tour", desc: "Show setup steps again", key: "", run: () => { state.onboardingShown = false; maybeOnboard(true); } },
    ];
    if (state.currentSessionId) {
      items.push({ label: "Compact this chat", desc: "Summarize older context now", key: "", run: () => {
        API.sessions.compact(state.currentSessionId)
          .then(() => { toast("Context compacted", "ok"); refreshCurrentSession({ quiet: true, preserveScroll: true }); })
          .catch((err) => toast(errorMessage(err), "error"));
      }});
      items.push({ label: "Export this chat", desc: "Download as Markdown", key: "", run: () => downloadUrl(API.sessions.exportUrl(state.currentSessionId, "md")) });
    }
    state.sessions.slice(0, 8).forEach((session) => {
      items.push({
        label: session.title || "New chat",
        desc: "Open chat · " + timeAgo(session.updated_at),
        key: "",
        run: () => selectSession(session.id),
      });
    });
    return items;
  }

  function openPalette() {
    closePalette();
    const root = $("#modalRoot");
    root.innerHTML = '<div class="modal-backdrop palette-backdrop">' +
      '<div class="palette" role="dialog" aria-modal="true">' +
        '<input type="text" placeholder="Search commands and chats…" autocomplete="off" spellcheck="false">' +
        '<div class="palette-list"></div>' +
      "</div></div>";
    root.classList.add("open");
    const backdrop = $(".modal-backdrop", root);
    const input = $("input", root);
    const list = $(".palette-list", root);
    const palette = { root, backdrop, input, list, items: paletteItems(), filtered: [], index: 0 };
    state.palette = palette;
    function render() {
      const query = input.value.trim().toLowerCase();
      palette.filtered = palette.items.filter((item) =>
        !query || (item.label + " " + item.desc).toLowerCase().includes(query)
      ).slice(0, 30);
      palette.index = Math.max(0, Math.min(palette.index, palette.filtered.length - 1));
      list.innerHTML = palette.filtered.length ? palette.filtered.map((item, index) =>
        '<div class="palette-item' + (index === palette.index ? " active" : "") + '" data-index="' + index + '">' +
          '<div><div>' + esc(item.label) + '</div><div class="desc">' + esc(item.desc) + "</div></div>" +
          (item.key ? '<span class="key">' + esc(item.key) + "</span>" : "") +
        "</div>"
      ).join("") : '<div class="palette-empty">No matches.</div>';
      $$("[data-index]", list).forEach((el) => {
        el.addEventListener("mouseenter", () => { palette.index = Number(el.dataset.index); render(); });
        el.addEventListener("click", () => execute(Number(el.dataset.index)));
      });
    }
    function execute(index) {
      const item = palette.filtered[index];
      if (!item) return;
      closePalette();
      try { item.run(); } catch (err) { toast(errorMessage(err), "error"); }
    }
    input.addEventListener("input", () => { palette.index = 0; render(); });
    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        palette.index = Math.min(palette.filtered.length - 1, palette.index + 1);
        render();
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        palette.index = Math.max(0, palette.index - 1);
        render();
      } else if (event.key === "Enter") {
        event.preventDefault();
        execute(palette.index);
      } else if (event.key === "Escape") {
        event.preventDefault();
        closePalette();
      }
    });
    backdrop.addEventListener("mousedown", (event) => {
      if (event.target === backdrop) closePalette();
    });
    render();
    input.focus();
  }

  function closePalette() {
    if (!state.palette) return;
    const root = $("#modalRoot");
    root.innerHTML = "";
    root.classList.remove("open");
    state.palette = null;
  }

  // ----------------------------------------------------------------- tools panel
  async function loadBuiltinTools() {
    if (state.builtinTools) return state.builtinTools;
    try {
      const data = await API.tools.list();
      state.builtinTools = data.tools || [];
    } catch (_) {
      state.builtinTools = [];
    }
    return state.builtinTools;
  }

  function riskBadge(risk) {
    const label = { safe: "Safe", write: "Writes", delete: "Deletes", shell: "Shell", network: "Network" }[risk] || risk || "Tool";
    const cls = { safe: "ok", write: "warn", delete: "bad", shell: "bad", network: "warn" }[risk] || "";
    return '<span class="tool-risk ' + cls + '">' + esc(label) + "</span>";
  }

  function openTools() {
    const modal = openModal({
      title: "Tools",
      sub: "Everything LocalCode can do on your machine.",
      body: '<div class="empty-state"><span class="spinner"></span>Loading tools…</div>',
      size: "medium",
    });
    Promise.all([loadBuiltinTools(), API.mcp.tools().catch(() => ({ tools: [] }))])
      .then(([builtin, mcpData]) => {
        if (state.modal !== modal) return;
        const mcpTools = (mcpData && mcpData.tools) || [];
        const builtinHtml = builtin.map((t) =>
          '<div class="tool-row"><div class="tool-row-main">' +
            '<div class="tool-name"><code>' + esc(t.name) + "</code>" + riskBadge(t.risk) + "</div>" +
            '<div class="tool-desc">' + esc(t.description) + "</div>" +
          "</div></div>"
        ).join("");
        const mcpHtml = mcpTools.length
          ? mcpTools.map((t) =>
              '<div class="tool-row mcp"><div class="tool-row-main">' +
                '<div class="tool-name"><code>' + esc(t.qualified || t.name) + '</code><span class="tool-risk">MCP</span></div>' +
                '<div class="tool-desc">' + esc(t.description || "External tool") + "</div>" +
              "</div></div>"
            ).join("")
          : '<div class="empty-state">No MCP tools connected. Add a server in MCP.</div>';
        modalBody(
          '<div class="tools-note">Type <b>/</b> in the chat input to trigger a tool with a slash command.</div>' +
          '<div class="tools-section-title">Built-in tools (' + builtin.length + ")</div>" + builtinHtml +
          '<div class="tools-section-title">MCP tools (' + mcpTools.length + ")</div>" + mcpHtml
        );
      })
      .catch((err) => {
        if (state.modal === modal) modalBody('<div class="empty-state">' + esc(errorMessage(err)) + "</div>");
      });
  }

  // ----------------------------------------------------------------- slash commands
  function slashToolCommands() {
    const defs = [
      { label: "read", desc: "Read a file and explain it", insert: "Read the file  and explain what it does.", caretAfter: "Read the file " },
      { label: "write", desc: "Create or overwrite a file", insert: "Create or overwrite the file  with this content:\n", caretAfter: "Create or overwrite the file " },
      { label: "edit", desc: "Replace text in a file", insert: "Edit the file : replace the old text with the new text.", caretAfter: "Edit the file " },
      { label: "run", desc: "Run a shell command", insert: "Run this command in the workspace:\n```\n\n```", caretAfter: "Run this command in the workspace:\n```\n" },
      { label: "list", desc: "List files and folders", insert: "List the files and folders in .", caretAfter: "List the files and folders in " },
      { label: "grep", desc: "Search file contents", insert: "Search the codebase for the pattern .", caretAfter: "Search the codebase for the pattern " },
      { label: "glob", desc: "Find files by pattern", insert: "Find files matching the pattern  (e.g. **/*.py).", caretAfter: "Find files matching the pattern " },
      { label: "mkdir", desc: "Create a directory", insert: "Create the directory .", caretAfter: "Create the directory " },
      { label: "delete", desc: "Delete a file or folder", insert: "Delete the file or folder .", caretAfter: "Delete the file or folder " },
      { label: "remember", desc: "Save a fact to memory", insert: "Remember this fact: ", caretAfter: "Remember this fact: " },
      { label: "recall", desc: "Search your memory", insert: "Search your memory for: ", caretAfter: "Search your memory for: " },
      { label: "todo", desc: "Make a task plan", insert: "Make a step-by-step task plan for: ", caretAfter: "Make a step-by-step task plan for: " },
      { label: "fetch", desc: "Fetch and summarize a URL", insert: "Fetch this URL and summarize it: ", caretAfter: "Fetch this URL and summarize it: " },
    ];
    return defs.map((d) => ({
      cmd: "/" + d.label, label: d.label, desc: d.desc, insert: d.insert, caretAfter: d.caretAfter, kind: "tool",
    }));
  }

  function slashAppCommands() {
    return [
      { cmd: "/new", label: "New chat", desc: "Start a fresh conversation", kind: "app", run: newChat },
      { cmd: "/model", label: "Choose model", desc: "Open the Models panel", kind: "app", run: () => openModels("installed") },
      { cmd: "/settings", label: "Settings", desc: "Workspace, context, provider", kind: "app", run: () => openSettings() },
      { cmd: "/mcp", label: "MCP servers", desc: "Roblox Studio and external tools", kind: "app", run: () => openMCP() },
      { cmd: "/usage", label: "Usage & cost", desc: "Tokens and hosted-API estimates", kind: "app", run: () => openUsage(state.currentSessionId) },
      { cmd: "/files", label: "Workspace files", desc: "Toggle the file browser", kind: "app", run: () => toggleFiles() },
      { cmd: "/memory", label: "Memories", desc: "Facts remembered across chats", kind: "app", run: () => openMemories() },
      { cmd: "/export", label: "Export chat", desc: "Download as Markdown", kind: "app", run: () => {
        if (state.currentSessionId) downloadUrl(API.sessions.exportUrl(state.currentSessionId, "md"));
        else toast("No chat to export yet", "error");
      } },
      { cmd: "/theme", label: "Toggle theme", desc: "Dark / light", kind: "app", run: toggleTheme },
      { cmd: "/tools", label: "List tools", desc: "See every tool the agent can use", kind: "app", run: () => openTools() },
    ];
  }

  function slashToken() {
    const input = dom.composerInput;
    if (!input) return null;
    const value = input.value;
    const caret = input.selectionStart != null ? input.selectionStart : value.length;
    const before = value.slice(0, caret);
    const idx = before.lastIndexOf("/");
    if (idx === -1) return null;
    if (idx > 0 && !/\s/.test(before[idx - 1])) return null;
    const token = before.slice(idx + 1);
    if (/\s/.test(token)) return null;
    return { start: idx, text: token, caret };
  }

  function closeSlash() {
    if (state.slash) {
      state.slash.remove();
      state.slash = null;
    }
  }

  function renderSlash() {
    const token = slashToken();
    if (!token) { closeSlash(); return; }
    const query = token.text.toLowerCase();
    const items = slashAppCommands().concat(slashToolCommands()).filter((c) =>
      !query || c.label.toLowerCase().includes(query) || c.cmd.includes(query)
    ).slice(0, 12);
    if (!items.length) { closeSlash(); return; }
    if (!state.slash) {
      const menu = document.createElement("div");
      menu.className = "slash-menu";
      menu.id = "slashMenu";
      (dom.composerArea || dom.composer.parentElement).appendChild(menu);
      state.slash = menu;
    }
    state.slash.innerHTML = items.map((item, index) =>
      '<div class="slash-item' + (index === 0 ? " active" : "") + '" data-slash-index="' + index + '">' +
        '<span class="slash-cmd">' + esc(item.cmd) + "</span>" +
        '<span class="slash-label">' + esc(item.label) + "</span>" +
        '<span class="slash-kind ' + esc(item.kind) + '">' + (item.kind === "app" ? "action" : "tool") + "</span>" +
        '<span class="slash-desc">' + esc(item.desc || "") + "</span>" +
      "</div>"
    ).join("");
    state.slash._items = items;
    state.slash._index = 0;
    $$(".slash-item", state.slash).forEach((el) => {
      el.addEventListener("mouseenter", () => {
        state.slash._index = Number(el.dataset.slashIndex);
        highlightSlash();
      });
      el.addEventListener("mousedown", (event) => {
        event.preventDefault();
        state.slash._index = Number(el.dataset.slashIndex);
        applySlash();
      });
    });
  }

  function highlightSlash() {
    if (!state.slash) return;
    $$(".slash-item", state.slash).forEach((el, i) => {
      el.classList.toggle("active", i === state.slash._index);
    });
  }

  function applySlash() {
    if (!state.slash) return;
    const item = state.slash._items[state.slash._index];
    const token = slashToken();
    closeSlash();
    if (!item || !token) return;
    const input = dom.composerInput;
    if (item.kind === "app") {
      input.value = input.value.slice(0, token.start) + input.value.slice(token.caret);
      autoSizeComposer();
      try { item.run(); } catch (err) { toast(errorMessage(err), "error"); }
      return;
    }
    const replaced = input.value.slice(0, token.start) + item.insert + input.value.slice(token.caret);
    input.value = replaced;
    autoSizeComposer();
    const caretPos = token.start + (item.caretAfter ? item.caretAfter.length : item.insert.length);
    input.focus();
    input.setSelectionRange(caretPos, caretPos);
    updateSendAvailability();
  }

  function toggleTheme() {
    const next = ((state.settings && state.settings.theme) || "dark") === "dark" ? "light" : "dark";
    API.settings.patch({ theme: next }).then((data) => {
      state.settings = data.settings || state.settings;
      applyTheme();
      toast(next === "light" ? "Light theme" : "Dark theme", "ok", 1500);
    }).catch((err) => toast(errorMessage(err), "error"));
  }

  // ---------------------------------------------------------------- onboarding
  function maybeOnboard(force) {
    if (state.onboardingShown && !force) return;
    if (state.settings && state.settings.onboarded && !force) return;
    if (!state.booted && !force) return;
    state.onboardingShown = true;
    const runtime = state.runtime || {};
    const installed = (state.models && state.models.installed) || [];
    const mcpConnected = state.mcp && state.mcp.connected;
    const steps = [
      {
        ok: !!runtime.installed,
        title: "Install the local engine",
        desc: runtime.installed ? "llama.cpp is installed" : "Download the llama.cpp runtime for this machine",
      },
      {
        ok: installed.length > 0,
        title: "Download a model",
        desc: installed.length ? installed.length + " model" + (installed.length === 1 ? "" : "s") + " installed" : "Start with a small coding model (1–3 GB)",
      },
      {
        ok: !!mcpConnected,
        title: "Connect MCP (optional)",
        desc: mcpConnected ? mcpConnected + " server" + (mcpConnected === 1 ? "" : "s") + " connected" : "Roblox Studio and other external tools",
      },
    ];
    openModal({
      title: "Welcome to LocalCode",
      sub: (state.settings && state.settings.onboarded)
        ? "Setup walkthrough"
        : "New here? Let's set everything up — it only takes a minute",
      size: "medium",
      body:
        '<div class="hero-badges" style="justify-content:flex-start;margin-top:0">' +
          "<span><i></i> 100% local</span><span><i></i> No API keys</span><span><i></i> Unlimited chats</span></div>" +
        '<div class="onboard-steps">' + steps.map((step, index) =>
          '<div class="onboard-step"><span class="num">' + (step.ok ? "✓" : index + 1) + "</span>" +
            '<div class="txt"><strong>' + esc(step.title) + "</strong><span>" + esc(step.desc) + "</span></div>" +
            '<span class="state ' + (step.ok ? "ok" : "") + '">' + (step.ok ? "ready" : "needed") + "</span></div>"
        ).join("") + "</div>" +
        '<div class="section-note">Everything you type, every file the agent reads and every model stays on this computer. LocalCode never sends your code to a cloud service.</div>',
      foot:
        '<button class="ghost-btn" type="button" data-onboard-models>Open models</button>' +
        '<button class="ghost-btn" type="button" data-onboard-mcp>Open MCP</button>' +
        '<button class="primary-btn" type="button" data-onboard-done>Start chatting</button>',
      onMount(modal) {
        $("[data-onboard-models]", modal.foot).addEventListener("click", () => {
          closeModal();
          openModels(runtime.installed ? "browse" : "runtime");
        });
        $("[data-onboard-mcp]", modal.foot).addEventListener("click", () => {
          closeModal();
          openMCP();
        });
        $("[data-onboard-done]", modal.foot).addEventListener("click", async () => {
          closeModal();
          try {
            const data = await API.settings.patch({ onboarded: true });
            state.settings = data.settings || state.settings;
          } catch (_) { /* ignore */ }
          if (dom.composerInput) dom.composerInput.focus();
        });
      },
    });
  }

  // ------------------------------------------------------------------- start
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
