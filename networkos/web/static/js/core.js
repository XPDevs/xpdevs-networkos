/* XPDevs NetworkOS - shared dashboard runtime.
   No frameworks, no CDN: everything here runs offline from the appliance. */
(function () {
  "use strict";

  const body = document.body;
  const CSRF = body.dataset.csrf || "";
  const THEME_KEY = "nwos.theme";

  /* --------------------------------------------------------------- helpers */
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const escapeHtml = (value) =>
    String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));

  const fmt = {
    bytes(value, digits) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
      let amount = Number(value);
      const units = ["B", "KB", "MB", "GB", "TB"];
      let index = 0;
      while (Math.abs(amount) >= 1024 && index < units.length - 1) {
        amount /= 1024;
        index += 1;
      }
      const precision = digits === undefined ? (index === 0 ? 0 : 1) : digits;
      return `${amount.toFixed(precision)} ${units[index]}`;
    },
    rate(value) {
      if (value === null || value === undefined) return "—";
      return `${fmt.bytes(value, 1)}/s`;
    },
    number(value) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
      return Number(value).toLocaleString();
    },
    duration(seconds) {
      if (seconds === null || seconds === undefined) return "—";
      let s = Math.max(0, Math.floor(Number(seconds)));
      if (s < 60) return `${s}s`;
      const m = Math.floor(s / 60);
      if (m < 60) return `${m}m`;
      const h = Math.floor(m / 60);
      if (h < 24) return m % 60 ? `${h}h ${m % 60}m` : `${h}h`;
      const d = Math.floor(h / 24);
      return h % 24 ? `${d}d ${h % 24}h` : `${d}d`;
    },
    ago(iso) {
      if (!iso) return "—";
      const when = new Date(iso);
      if (Number.isNaN(when.getTime())) return "—";
      const delta = (Date.now() - when.getTime()) / 1000;
      if (delta < 45) return "just now";
      if (delta < 3600) return `${Math.round(delta / 60)} min ago`;
      if (delta < 86400) return `${Math.round(delta / 3600)} h ago`;
      return `${Math.round(delta / 86400)} d ago`;
    },
    clock(iso) {
      if (!iso) return "—";
      const when = new Date(iso);
      if (Number.isNaN(when.getTime())) return "—";
      return when.toLocaleString(undefined, {
        year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit",
      });
    },
    temp(value) {
      return value === null || value === undefined ? "—" : `${Number(value).toFixed(1)} °C`;
    },
    percent(value, digits) {
      if (value === null || value === undefined) return "—";
      return `${Number(value).toFixed(digits === undefined ? 0 : digits)}%`;
    },
    time(iso) {
      if (!iso) return "—";
      const when = new Date(iso);
      if (Number.isNaN(when.getTime())) return "—";
      return when.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    },
  };

  /* ----------------------------------------------------------------- theme */
  function resolvedTheme() {
    const mode = document.documentElement.dataset.theme || "light";
    if (mode === "system") {
      return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    return mode;
  }

  function applyTheme(mode) {
    document.documentElement.dataset.theme = mode;
    try { localStorage.setItem(THEME_KEY, mode); } catch (e) { /* storage disabled */ }
    const icon = document.getElementById("theme-icon");
    if (icon) icon.setAttribute("href", resolvedTheme() === "dark" ? "#i-sun" : "#i-moon");
  }

  function initTheme() {
    let stored = null;
    try { stored = localStorage.getItem(THEME_KEY); } catch (e) { stored = null; }
    applyTheme(stored || body.dataset.themeDefault || "light");
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      if ((document.documentElement.dataset.theme || "") === "system") applyTheme("system");
    });
  }

  function cycleTheme() {
    const current = document.documentElement.dataset.theme || "light";
    const order = ["light", "dark", "system"];
    applyTheme(order[(order.indexOf(current) + 1) % order.length]);
    toast({ title: "Appearance", text: `Theme set to ${document.documentElement.dataset.theme}.`, kind: "info", timeout: 2200 });
  }

  /* ------------------------------------------------------------------ toasts */
  function toast(options) {
    const opts = typeof options === "string" ? { title: options } : options || {};
    const stack = document.getElementById("toasts");
    if (!stack) return;
    const node = document.createElement("div");
    node.className = `toast toast--${opts.kind || "info"}`;
    const iconFor = { ok: "i-check-circle", bad: "i-x-circle", warn: "i-alert", info: "i-info" };
    node.innerHTML = `
      <svg class="icon" aria-hidden="true"><use href="#${iconFor[opts.kind] || "i-info"}"/></svg>
      <div class="toast__body">
        <div class="toast__title">${escapeHtml(opts.title || "")}</div>
        ${opts.text ? `<div class="toast__text">${escapeHtml(opts.text)}</div>` : ""}
      </div>
      <button class="toast__close" type="button" aria-label="Dismiss">
        <svg class="icon icon-sm" aria-hidden="true"><use href="#i-close"/></svg>
      </button>`;
    const remove = () => {
      node.style.opacity = "0";
      node.style.transform = "translateY(6px)";
      node.style.transition = "opacity .15s ease, transform .15s ease";
      setTimeout(() => node.remove(), 160);
    };
    node.querySelector(".toast__close").addEventListener("click", remove);
    stack.appendChild(node);
    const timeout = opts.timeout === undefined ? 6000 : opts.timeout;
    if (timeout) setTimeout(remove, timeout);
  }

  /* ------------------------------------------------------------------ modal */
  const modal = {
    resolve: null,
    open({ title, message, body: content, footer, onMount }) {
      const root = document.getElementById("modal");
      $(".modal__panel", root).setAttribute("aria-labelledby", "modal-title");
      document.getElementById("modal-title").textContent = title || "Confirm";
      const subEl = document.getElementById("modal-sub");
      subEl.textContent = message || "";
      subEl.style.display = message ? "" : "none";
      const bodyEl = document.getElementById("modal-body");
      bodyEl.innerHTML = content || "";
      const foot = document.getElementById("modal-foot");
      foot.innerHTML = "";
      (footer || []).forEach((button) => {
        const el = document.createElement("button");
        el.type = "button";
        el.className = button.className || "btn";
        el.textContent = button.label;
        el.addEventListener("click", () => button.onClick && button.onClick(el));
        foot.appendChild(el);
      });
      root.hidden = false;
      if (onMount) onMount(root);
      const focusable = root.querySelector("input, select, textarea, button.btn--primary");
      if (focusable) focusable.focus();
      document.addEventListener("keydown", this._esc);
      return new Promise((resolve) => { this.resolve = resolve; });
    },
    _esc(event) {
      if (event.key === "Escape") modal.close(null);
    },
    close(value) {
      const root = document.getElementById("modal");
      root.hidden = true;
      document.removeEventListener("keydown", this._esc);
      document.getElementById("modal-body").innerHTML = "";
      if (this.resolve) { this.resolve(value); this.resolve = null; }
    },
  };

  /**
   * Ask for confirmation. Pass `phrase` to require typed confirmation for
   * destructive actions (the server checks the same phrase).
   */
  function confirmAction({ title, message, confirmLabel = "Confirm", danger = false, phrase = null, extraHtml = "" }) {
    let typing = null;
    const bodyHtml = `${extraHtml}${phrase ? `
      <div class="field">
        <label class="field__label" for="confirm-phrase">Type <span class="mono">${escapeHtml(phrase)}</span> to continue</label>
        <input class="input mono" id="confirm-phrase" autocomplete="off" spellcheck="false">
        <span class="field__hint">This action cannot be undone from the dashboard.</span>
      </div>` : ""}`;
    return modal.open({
      title,
      message,
      body: bodyHtml,
      footer: [
        { label: "Cancel", className: "btn", onClick: () => modal.close(null) },
        {
          label: confirmLabel,
          className: danger ? "btn btn--danger" : "btn btn--primary",
          onClick: () => {
            if (phrase) {
              const input = document.getElementById("confirm-phrase");
              if (!input || input.value.trim().toUpperCase() !== phrase.toUpperCase()) {
                input.closest(".field").classList.add("has-error");
                const hint = input.parentElement.querySelector(".field__hint");
                if (hint) { hint.textContent = `Type ${phrase} exactly to confirm.`; hint.style.color = "var(--danger)"; }
                return;
              }
            }
            modal.close({ confirmed: true, phrase: typing });
          },
        },
      ],
      onMount: () => {
        if (phrase) {
          typing = document.getElementById("confirm-phrase");
        }
      },
    });
  }

  /* -------------------------------------------------------------------- api */
  async function api(path, options) {
    const opts = options || {};
    const headers = { Accept: "application/json" };
    if (opts.body !== undefined && opts.body !== null) headers["Content-Type"] = "application/json";
    if (opts.method && opts.method !== "GET") headers[CSRF_HEADER()] = CSRF;
    let response;
    try {
      response = await fetch(path, {
        method: opts.method || "GET",
        headers,
        credentials: "same-origin",
        body: opts.body === undefined || opts.body === null ? undefined : JSON.stringify(opts.body),
        cache: "no-store",
      });
    } catch (error) {
      throw { status: 0, message: "The dashboard could not reach the appliance. Check your connection.", code: "network" };
    }
    let payload = null;
    try { payload = await response.json(); } catch (e) { payload = null; }
    if (response.status === 401) {
      window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`;
      throw { status: 401, message: "Session expired.", code: "unauthorized" };
    }
    if (!response.ok || !payload || payload.ok === false) {
      const error = (payload && payload.error) || {};
      throw {
        status: response.status,
        code: error.code || "error",
        message: error.message || `Request failed (${response.status}).`,
        details: error.details,
      };
    }
    return payload.data === undefined ? payload : payload.data;
  }

  function CSRF_HEADER() { return "X-CSRF-Token"; }

  function reportError(error, fallback) {
    const message = (error && error.message) || fallback || "Something went wrong.";
    toast({ title: "Action failed", text: message, kind: "bad", timeout: 8000 });
    return message;
  }

  /* ------------------------------------------------------------------ polling */
  function poll(fn, interval, options) {
    const opts = options || {};
    let timer = null;
    let stopped = false;
    const run = async () => {
      if (stopped) return;
      if (document.hidden && !opts.runWhenHidden) return;
      try { await fn(); } catch (error) { opts.onError && opts.onError(error); }
    };
    const start = () => {
      stop();
      timer = setInterval(run, interval);
    };
    const stop = () => { if (timer) { clearInterval(timer); timer = null; } };
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) { run(); start(); }
    });
    run();
    start();
    return {
      refresh: run,
      stop: () => { stopped = true; stop(); },
    };
  }

  /* ------------------------------------------------------------------ canvas */
  function spark(canvas, series, options) {
    if (!canvas) return;
    const opts = options || {};
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const width = canvas.clientWidth || canvas.parentElement.clientWidth || 200;
    const height = canvas.clientHeight || 40;
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (!series || series.length < 2) {
      ctx.strokeStyle = "rgba(120,140,165,.35)";
      ctx.setLineDash([3, 4]);
      ctx.beginPath();
      ctx.moveTo(0, height - 1);
      ctx.lineTo(width, height - 1);
      ctx.stroke();
      return;
    }
    const max = Math.max(...series, opts.min || 1) * 1.15;
    const stepX = width / (series.length - 1);
    const point = (index, value) => [index * stepX, height - (value / max) * (height - 3) - 1.5];
    const color = opts.color || "#1d6ae5";
    const gradient = ctx.createLinearGradient(0, 0, 0, height);
    gradient.addColorStop(0, opts.fill || "rgba(29,106,229,.22)");
    gradient.addColorStop(1, "rgba(29,106,229,0)");

    ctx.beginPath();
    series.forEach((value, index) => {
      const [x, y] = point(index, value);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    const last = point(series.length - 1, series[series.length - 1]);
    ctx.lineTo(last[0], height);
    ctx.lineTo(0, height);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();

    ctx.beginPath();
    series.forEach((value, index) => {
      const [x, y] = point(index, value);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.8;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.stroke();
  }

  /* ------------------------------------------------------------------ ui bits */
  function setText(id, value) {
    const node = typeof id === "string" ? document.getElementById(id) : id;
    if (node) node.textContent = value;
  }

  function setProgress(node, percent, tone) {
    if (!node) return;
    const value = Math.max(0, Math.min(100, Number(percent) || 0));
    node.style.setProperty("--value", `${value}%`);
    node.parentElement && node.parentElement.classList.remove("progress--ok", "progress--warn", "progress--bad");
    if (tone) node.parentElement && node.parentElement.classList.add(`progress--${tone}`);
  }

  function tone(percent) {
    if (percent >= 90) return "bad";
    if (percent >= 75) return "warn";
    return "ok";
  }

  function emptyState(container, { icon = "i-info", title, text }) {
    if (!container) return;
    container.innerHTML = `
      <div class="empty">
        <span class="empty__icon"><svg class="icon icon-lg" aria-hidden="true"><use href="#${icon}"/></svg></span>
        <strong>${escapeHtml(title || "Nothing here yet")}</strong>
        ${text ? `<p>${escapeHtml(text)}</p>` : ""}
      </div>`;
  }

  function withBusy(button, task) {
    if (!button) return task();
    const wasDisabled = button.disabled;
    button.disabled = true;
    button.classList.add("is-busy");
    const promise = Promise.resolve().then(task);
    return promise.finally(() => {
      button.disabled = wasDisabled;
      button.classList.remove("is-busy");
    });
  }

  function signalBars(dbm) {
    if (dbm === null || dbm === undefined) return { level: 0, label: "—" };
    if (dbm >= -55) return { level: 4, label: "Excellent" };
    if (dbm >= -65) return { level: 3, label: "Good" };
    if (dbm >= -75) return { level: 2, label: "Fair" };
    return { level: 1, label: "Weak" };
  }


  /* --------------------------------------------------------- password toggle */
  function initPasswordToggles(root) {
    $$("[data-toggle-password]", root || document).forEach((button) => {
      if (button.dataset.bound) return;
      button.dataset.bound = "1";
      button.addEventListener("click", () => {
        const target = document.getElementById(button.dataset.target);
        if (!target) return;
        const showing = target.type === "text";
        target.type = showing ? "password" : "text";
        button.setAttribute("aria-label", showing ? "Show password" : "Hide password");
        const use = button.querySelector("use");
        if (use) use.setAttribute("href", showing ? "#i-eye" : "#i-eye-off");
        target.focus();
      });
    });
  }

  /* ------------------------------------------------------------- shell wiring */
  function initShell() {
    const app = document.getElementById("app");
    document.addEventListener("click", (event) => {
      const trigger = event.target.closest("[data-action]");
      if (!trigger) return;
      const action = trigger.dataset.action;
      if (action === "open-nav") app.classList.add("is-nav-open");
      if (action === "close-nav") app.classList.remove("is-nav-open");
      if (action === "toggle-theme") cycleTheme();
      if (action === "modal-close") modal.close(null);
      if (action === "refresh") {
        document.dispatchEvent(new CustomEvent("nwos:refresh"));
        trigger.classList.add("is-busy");
        setTimeout(() => trigger.classList.remove("is-busy"), 700);
      }
      if (action === "signout") {
        const form = document.createElement("form");
        form.method = "post";
        form.action = "/logout";
        const input = document.createElement("input");
        input.type = "hidden"; input.name = "csrf_token"; input.value = CSRF;
        form.appendChild(input);
        document.body.appendChild(form);
        form.submit();
      }
    });

    window.addEventListener("keydown", (event) => {
      if (event.key === "Escape") app.classList.remove("is-nav-open");
    });

    decorateTables(document);
    initPasswordToggles(document);
  }

  /** Attach column labels so tables read as cards on narrow screens. */
  function decorateTables(root) {
    $$(".table--stack", root).forEach((table) => {
      const headers = $$("thead th", table).map((th) => th.textContent.trim());
      $$("tbody tr", table).forEach((row) => {
        $$("td", row).forEach((cell, index) => {
          if (!cell.dataset.label && headers[index]) cell.dataset.label = headers[index];
        });
      });
    });
  }

  /* -------------------------------------------------------------------- boot */
  document.addEventListener("DOMContentLoaded", () => {
    initTheme();
    initShell();
    document.addEventListener("nwos:refresh", () => {
      if (typeof window.refreshPage === "function") window.refreshPage();
    });
  });

  window.NWOS = {
    $, $$, api, toast, modal, confirmAction, fmt, poll, spark, setText, setProgress,
    tone, emptyState, withBusy, signalBars, escapeHtml, applyTheme, reportError,
    decorateTables, initPasswordToggles,
  };
})();
