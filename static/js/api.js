/* LocalCode API client — all server calls live here. */
(function () {
  "use strict";

  class ApiError extends Error {
    constructor(message, status, payload) {
      super(message || "Request failed");
      this.name = "ApiError";
      this.status = status || 0;
      this.payload = payload || null;
      this.code = payload && payload.code;
    }
  }

  async function request(path, opts) {
    opts = opts || {};
    const init = {
      method: opts.method || "GET",
      headers: Object.assign({}, opts.headers || {}),
      signal: opts.signal,
      cache: "no-store",
    };
    if (opts.body !== undefined && opts.body !== null) {
      if (opts.body instanceof FormData) {
        init.body = opts.body;
      } else {
        init.headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(opts.body);
      }
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch (err) {
      if (err && err.name === "AbortError") throw err;
      throw new ApiError("Cannot reach the LocalCode server. Is it still running?", 0);
    }
    let data = null;
    const text = await res.text();
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (_) {
        data = { error: text.slice(0, 500) };
      }
    }
    if (!res.ok) {
      const message = (data && (data.error || data.message)) || `HTTP ${res.status}`;
      throw new ApiError(message, res.status, data);
    }
    return data;
  }

  function json(method, path, body, opts) {
    return request(path, Object.assign({}, opts || {}, { method, body }));
  }

  async function streamChat(payload, onEvent, signal) {
    let res;
    try {
      res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal,
      });
    } catch (err) {
      if (err && err.name === "AbortError") throw err;
      throw new ApiError("Cannot reach the LocalCode server. Is it still running?", 0);
    }
    if (!res.ok) {
      let data = null;
      try {
        data = await res.json();
      } catch (_) {
        try {
          data = { error: await res.text() };
        } catch (__) {
          data = null;
        }
      }
      const message = (data && (data.error || data.message)) || `HTTP ${res.status}`;
      throw new ApiError(message, res.status, data);
    }
    if (!res.body || !res.body.getReader) {
      throw new ApiError("This browser cannot read streaming responses.", 0);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    const dispatch = (line) => {
      line = line.trim();
      if (!line) return;
      let event;
      try {
        event = JSON.parse(line);
      } catch (_) {
        return;
      }
      try {
        onEvent(event);
      } catch (err) {
        console.error("LocalCode stream handler failed", err);
      }
    };
    try {
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        let index;
        while ((index = buffer.indexOf("\n")) >= 0) {
          dispatch(buffer.slice(0, index));
          buffer = buffer.slice(index + 1);
        }
      }
      buffer += decoder.decode();
      if (buffer.trim()) dispatch(buffer);
    } finally {
      try {
        reader.releaseLock();
      } catch (_) {
        /* ignore */
      }
    }
  }

  const API = {
    ApiError,
    request,

    health: () => request("/api/health"),

    sessions: {
      list: (q) => request("/api/sessions" + (q ? "?q=" + encodeURIComponent(q) : "")),
      create: (data) => json("POST", "/api/sessions", data || {}),
      get: (id) => request("/api/sessions/" + encodeURIComponent(id)),
      patch: (id, data) => json("PATCH", "/api/sessions/" + encodeURIComponent(id), data),
      remove: (id) => json("DELETE", "/api/sessions/" + encodeURIComponent(id)),
      compact: (id) => json("POST", "/api/sessions/" + encodeURIComponent(id) + "/compact"),
      exportUrl: (id, format) =>
        "/api/sessions/" + encodeURIComponent(id) + "/export?format=" + encodeURIComponent(format || "md"),
    },

    chat: {
      stream: streamChat,
      queue: (data) => json("POST", "/api/chat/queue", data),
      unqueue: (data) => json("POST", "/api/chat/unqueue", data),
      stop: (sessionId) => json("POST", "/api/chat/stop", { session_id: sessionId }),
      status: (sessionId) =>
        request("/api/chat/status?session_id=" + encodeURIComponent(sessionId || "")),
      approve: (id, approved, reason) =>
        json("POST", "/api/approval", { id, approved, reason: reason || "" }),
      answer: (id, answer, reason) =>
        json("POST", "/api/ask", { id, answer: answer || "", reason: reason || "" }),
    },

    attachments: {
      upload: (file) => {
        const fd = new FormData();
        fd.append("file", file);
        return request("/api/attachments", { method: "POST", body: fd });
      },
      remove: (id) => json("DELETE", "/api/attachments/" + encodeURIComponent(id)),
      url: (id) => "/api/attachments/" + encodeURIComponent(id) + "/raw",
    },

    models: {
      list: () => request("/api/models"),
      repo: (repo) => request("/api/models/repo?repo=" + encodeURIComponent(repo)),
      download: (data) => json("POST", "/api/models/download", data),
      downloadUrl: (data) => json("POST", "/api/models/download-url", data),
      downloads: () => request("/api/downloads"),
      cancel: (jobId) => json("POST", "/api/models/cancel", { job_id: jobId }),
      removeJob: (jobId) => json("POST", "/api/models/remove-job", { job_id: jobId }),
      search: (q, limit) =>
        request("/api/models/search?q=" + encodeURIComponent(q || "") + "&limit=" + (limit || 24)),
      load: (id) => json("POST", "/api/models/load", { id }),
      unload: () => json("POST", "/api/models/unload"),
      remove: (id) => json("POST", "/api/models/delete", { id }),
    },

    runtime: {
      status: (logs) => request("/api/runtime" + (logs ? "?logs=1" : "")),
      install: (flavor) => json("POST", "/api/runtime/install", { flavor: flavor || "auto" }),
      stop: () => json("POST", "/api/runtime/stop"),
      logs: (lines) => request("/api/runtime/logs?lines=" + (lines || 200)),
    },

    provider: {
      models: () => request("/api/provider/models"),
      probe: () => json("POST", "/api/provider/probe"),
    },

    mcp: {
      status: () => request("/api/mcp"),
      tools: () => request("/api/mcp/tools"),
      addServer: (data) => json("POST", "/api/mcp/servers", data),
      addPreset: (data) => json("POST", "/api/mcp/presets", data),
      remove: (name) => json("DELETE", "/api/mcp/servers/" + encodeURIComponent(name)),
      start: (name) => json("POST", "/api/mcp/servers/" + encodeURIComponent(name) + "/start"),
      stop: (name) => json("POST", "/api/mcp/servers/" + encodeURIComponent(name) + "/stop"),
      restart: (name) => json("POST", "/api/mcp/servers/" + encodeURIComponent(name) + "/restart"),
      logs: (name) => request("/api/mcp/servers/" + encodeURIComponent(name) + "/logs"),
      node: () => request("/api/mcp/node"),
      installNode: () => json("POST", "/api/mcp/node/install"),
    },

    settings: {
      get: () => request("/api/settings"),
      patch: (data) => json("PATCH", "/api/settings", data),
    },

    usage: {
      get: (sessionId) =>
        request("/api/usage" + (sessionId ? "?session_id=" + encodeURIComponent(sessionId) : "")),
    },

    capabilities: {
      get: (modelId) =>
        request("/api/capabilities" + (modelId ? "?model_id=" + encodeURIComponent(modelId) : "")),
    },

    tools: {
      list: () => request("/api/tools"),
    },

    memories: {
      list: (workspace, q) => {
        const params = new URLSearchParams();
        if (workspace) params.set("workspace", workspace);
        if (q) params.set("q", q);
        return request("/api/memories" + (params.toString() ? "?" + params.toString() : ""));
      },
      add: (data) => json("POST", "/api/memories", data),
      remove: (id) => json("DELETE", "/api/memories/" + encodeURIComponent(id)),
    },

    workspace: {
      tree: (path) => request("/api/workspace/tree?path=" + encodeURIComponent(path || ".")),
      file: (path) => request("/api/workspace/file?path=" + encodeURIComponent(path)),
    },

    system: {
      info: () => request("/api/system/info"),
      pickFolder: () => json("POST", "/api/system/pick-folder", {}),
      openFolder: (kind) => json("POST", "/api/system/open-folder", { kind }),
      quit: () => json("POST", "/api/system/quit", {}),
      reset: (payload) => json("POST", "/api/system/reset", payload || {}),
      exportUrl: () => "/api/system/export",
    },
  };

  window.API = API;
  window.ApiError = ApiError;
})();
