/* Ad blocking page: Pi-hole statistics, verification and rule management. */
(function () {
  "use strict";
  const { $, $$, api, fmt, poll, setText, toast, escapeHtml, reportError, withBusy, confirmAction, decorateTables } = window.NWOS;

  const state = { kind: "allow", status: null };

  function pill(node, tone, text) {
    if (!node) return;
    node.className = `pill pill--${tone}`;
    const label = node.querySelector("span:last-child");
    if (label) label.textContent = text;
    const dot = node.querySelector(".dot");
    if (dot) dot.classList.toggle("dot--pulse", tone === "ok");
  }

  function showBanner(message, kind) {
    const node = $("#adblock-banner");
    if (!node) return;
    if (!message) { node.hidden = true; return; }
    node.hidden = false;
    node.innerHTML = `<div class="banner banner--${kind || "warn"}">
      <svg class="icon"><use href="#i-alert"/></svg>
      <div class="banner__body">${escapeHtml(message)}</div>
    </div>`;
  }

  function bars(items, tone) {
    if (!items || !items.length) return '<div class="text-sm dim">Nothing recorded yet.</div>';
    const max = Math.max(...items.map((item) => item.count), 1);
    return items.slice(0, 6).map((item) => `
      <div class="bar-row">
        <div class="bar-row__top">
          <span class="bar-row__label">${escapeHtml(item.domain)}</span>
          <span class="bar-row__value">${fmt.number(item.count)}</span>
        </div>
        <div class="bar"><div class="bar__fill ${tone === "danger" ? "bar__fill--danger" : ""}" data-width="${((item.count / max) * 100).toFixed(1)}"></div></div>
      </div>`).join("");
  }

  function applyWidths(root) {
    $$("[data-width]", root || document).forEach((bar) => bar.style.setProperty("--value", `${bar.dataset.width}%`));
  }

  function render(status) {
    state.status = status;
    const available = status.available;
    const stats = status.stats || {};

    if (!available) {
      showBanner(`${status.reason || "Pi-hole is not responding"}. Blocking is not being reported as active.`, "bad");
      setText("blocking-headline", "Pi-hole is unavailable");
      setText("blocking-subline", "Ad blocking cannot be confirmed while the resolver is offline");
      pill($("#verification-pill"), "bad", "Unavailable");
      $("#toggle-blocking").disabled = true;
      $("#verify-blocking").disabled = true;
      return;
    }

    showBanner(null);
    $("#toggle-blocking").disabled = false;
    $("#verify-blocking").disabled = false;
    setText("blocking-headline", status.verified ? "Ad blocking is active" : status.enabled ? "Blocking enabled · not verified" : "Ad blocking is paused");
    setText(
      "blocking-subline",
      `${fmt.number(stats.total)} queries today · ${fmt.number(stats.blocked)} blocked · Pi-hole ${status.api_version === 5 ? "v5" : "v6"} API`
    );
    const toggleLabel = $("#toggle-blocking .btn__label");
    if (toggleLabel) toggleLabel.textContent = status.enabled ? "Pause blocking" : "Resume blocking";

    setText("stat-total", fmt.number(stats.total));
    setText("stat-total-meta", `${fmt.number(stats.unique_domains)} unique domains`);
    setText("stat-blocked", fmt.number(stats.blocked));
    setText("stat-blocked-meta", `${fmt.number(stats.forwarded)} forwarded · ${fmt.number(stats.cached)} cached`);
    setText("stat-percent", stats.percent === undefined ? "—" : Number(stats.percent).toFixed(1));
    setText("stat-percent-meta", `${fmt.number(stats.clients_active)} active clients`);
    setText("stat-gravity", fmt.number(stats.gravity_domains));
    setText("stat-gravity-meta", "Domains compiled from blocklists");
    setText("gravity-updated", stats.gravity_updated ? `Updated ${fmt.ago(stats.gravity_updated)}` : "Update time unknown");
    setText("blocked-share-pill", `${stats.percent === undefined ? "—" : Number(stats.percent).toFixed(1)}% blocked`);
    pill($("#verification-pill"), status.verified ? "ok" : status.enabled ? "warn" : "muted",
      status.verified ? "Verified" : status.enabled ? "Unverified" : "Blocking paused");
    setText("verify-pill-text", status.verified ? "Verified by DNS test" : "Not verified");
    pill($("#api-pill"), "info", status.api_version === 5 ? "API v5" : "API v6");

    $("#top-blocked-list").innerHTML = bars(status.top_blocked, "danger");
    $("#top-permitted-list").innerHTML = bars(status.top_permitted, "info");
    applyWidths($("#top-blocked-list"));
    applyWidths($("#top-permitted-list"));

    renderVerification(status.verification);
    renderDomains();

    if (status.api_version === 5) {
      setText("lists-foot", "Blocklist editing needs Pi-hole 6 or newer; this Pi-hole reports the v5 API.");
      $("#list-form").querySelector("button").disabled = true;
      $("#list-rows").innerHTML = '<tr><td colspan="3"><div class="text-sm dim">Blocklist management is unavailable on this Pi-hole version.</div></td></tr>';
    } else {
      setText("lists-foot", "Blocklist management uses the Pi-hole API.");
      $("#list-form").querySelector("button").disabled = false;
      renderLists(status.lists || []);
    }
  }

  function renderVerification(verification) {
    const body = $("#verification-body");
    if (!verification || !verification.checks || !verification.checks.length) {
      body.innerHTML = '<div class="text-sm dim">Verification has not run yet.</div>';
      return;
    }
    const rows = verification.checks.map((check) => {
      const ok = check.result === "blocked";
      const tone = ok ? "ok" : check.result === "allowed" ? "bad" : "muted";
      const label = ok ? "Blocked" : check.result === "allowed" ? "Resolved" : "Unknown";
      return `<tr>
        <td class="mono">${escapeHtml(check.domain)}</td>
        <td class="text-sm">${escapeHtml(check.address || "no answer")}</td>
        <td class="text-sm">${escapeHtml(check.detail || "")}</td>
        <td class="table__actions"><span class="pill pill--${tone}"><span class="dot"></span>${label}</span></td>
      </tr>`;
    }).join("");
    body.innerHTML = `
      <div class="banner banner--${verification.blocking ? "ok" : "warn"} mb-3">
        <svg class="icon"><use href="#i-${verification.blocking ? "check-circle" : "alert"}"/></svg>
        <div class="banner__body">
          ${verification.blocking
            ? `A test query was answered with a blocking response, so filtering is working end to end.`
            : `The test query resolved normally. Blocking is not effective yet — check that clients use this Pi as their DNS server.`}
          <span class="dim"> Last checked ${escapeHtml(fmt.ago(verification.checked_at))}.</span>
        </div>
      </div>
      <div class="table-wrap">
        <table class="table table--stack">
          <thead><tr><th>Test domain</th><th>Answer</th><th>Method</th><th class="table__actions">Result</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
    decorateTables(body);
  }

  function renderDomains() {
    const status = state.status || {};
    const items = (state.kind === "allow" ? status.allow : status.deny) || [];
    setText("allow-count", `(${(status.allow || []).length})`);
    setText("deny-count", `(${(status.deny || []).length})`);
    setText("domain-label", state.kind === "allow" ? "Domain to always allow" : "Domain to always block");
    setText("domain-submit", state.kind === "allow" ? "Allow domain" : "Block domain");

    const rows = $("#domain-rows");
    if (!items.length) {
      rows.innerHTML = `<tr><td colspan="2"><div class="empty">
        <span class="empty__icon"><svg class="icon icon-lg"><use href="#i-list"/></svg></span>
        <strong>No ${state.kind === "allow" ? "allowlisted" : "blocked"} domains</strong>
        <p>Add exact domains above; they take precedence over the subscription lists.</p>
      </div></td></tr>`;
      return;
    }
    rows.innerHTML = items.map((item) => `<tr>
      <td class="td-main mono">${escapeHtml(item.domain)}</td>
      <td class="table__actions">
        <button class="btn btn--sm" data-remove-domain="${escapeHtml(item.domain)}">Remove</button>
      </td>
    </tr>`).join("");
    decorateTables(rows.closest("table"));
  }

  function renderLists(lists) {
    const rows = $("#list-rows");
    if (!lists.length) {
      rows.innerHTML = `<tr><td colspan="3"><div class="text-sm dim">No additional blocklists configured.</div></td></tr>`;
      return;
    }
    rows.innerHTML = lists.map((item) => `<tr>
      <td class="mono text-sm">${escapeHtml(item.address)}</td>
      <td class="text-sm">${item.domains ? fmt.number(item.domains) : "—"}</td>
      <td class="table__actions">
        <span class="pill pill--${item.enabled ? "ok" : "muted"}"><span class="dot"></span>${item.enabled ? "Enabled" : "Disabled"}</span>
        <button class="btn btn--sm" data-remove-list="${escapeHtml(item.address)}">Remove</button>
      </td>
    </tr>`).join("");
    decorateTables(rows.closest("table"));
  }

  async function loadQueries() {
    try {
      const { queries } = await api("/api/adblock/queries?limit=25");
      const rows = $("#queries-rows");
      if (!queries.length) {
        rows.innerHTML = '<tr><td colspan="5"><div class="text-sm dim">No queries logged yet.</div></td></tr>';
        return;
      }
      rows.innerHTML = queries.map((query) => `<tr>
        <td class="mono text-sm">${escapeHtml(fmt.time(query.at))}</td>
        <td class="text-sm">${escapeHtml(query.domain)}</td>
        <td class="mono text-sm">${escapeHtml(query.client_name || query.client || "—")}</td>
        <td class="text-sm">${escapeHtml(query.type || "—")}</td>
        <td class="table__actions">
          <span class="pill pill--${query.blocked ? "bad" : "muted"}"><span class="dot"></span>${escapeHtml(query.status || (query.blocked ? "Blocked" : "Allowed"))}</span>
        </td>
      </tr>`).join("");
      decorateTables(rows.closest("table"));
      setText("queries-foot", `Showing ${queries.length} most recent queries · retention keeps the database small`);
    } catch (error) {
      setText("queries-foot", "The query log could not be read.");
    }
  }

  async function refresh() {
    try {
      const status = await api("/api/adblock", { method: "GET" });
      render(status);
    } catch (error) {
      showBanner(error.message, "bad");
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    poll(refresh, 10000);
    poll(loadQueries, 15000);

    window.refreshPage = () => { refresh(); loadQueries(); };

    $$("[data-action-refresh]").forEach((node) =>
      node.addEventListener("click", (event) => { event.preventDefault(); refresh(); })
    );

    $("#toggle-blocking").addEventListener("click", async () => {
      const status = state.status;
      if (!status || !status.available) return;
      const enabling = !status.enabled;
      if (!enabling) {
        const answer = await confirmAction({
          title: "Pause ad blocking?",
          message: "Pi-hole will answer every query normally until blocking is resumed.",
          confirmLabel: "Pause blocking",
          danger: true,
        });
        if (!answer || !answer.confirmed) return;
      }
      withBusy($("#toggle-blocking"), async () => {
        try {
          const result = await api("/api/adblock/blocking", { method: "POST", body: { enabled: enabling } });
          const verified = result.verification && result.verification.blocking;
          toast({
            title: enabling ? "Blocking enabled" : "Blocking paused",
            text: enabling
              ? verified
                ? "Verified: a test domain is now answered with a blocking response."
                : "Pi-hole accepted the change but the DNS test could not confirm it yet."
              : "All queries will be answered normally.",
            kind: enabling ? (verified ? "ok" : "warn") : "info",
            timeout: 8000,
          });
          await refresh();
        } catch (error) {
          reportError(error);
        }
      });
    });

    $("#verify-blocking").addEventListener("click", () =>
      withBusy($("#verify-blocking"), async () => {
        try {
          const result = await api("/api/adblock/verify", { method: "POST", body: {} });
          toast({
            title: result.blocking ? "Blocking verified" : "Blocking not confirmed",
            text: result.checks && result.checks[0]
              ? `${result.checks[0].domain}: ${result.checks[0].detail}`
              : "No test domain was checked.",
            kind: result.blocking ? "ok" : "warn",
            timeout: 9000,
          });
          renderVerification(result);
        } catch (error) {
          reportError(error);
        }
      })
    );

    $$("[data-kind]").forEach((tab) => {
      tab.addEventListener("click", () => {
        state.kind = tab.dataset.kind;
        $$("[data-kind]").forEach((other) => other.classList.toggle("is-active", other === tab));
        renderDomains();
      });
    });

    $("#domain-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const input = $("#domain-input");
      const value = input.value.trim();
      if (!value) return;
      withBusy($("#domain-submit"), async () => {
        try {
          await api("/api/adblock/domains", { method: "POST", body: { kind: state.kind, action: "add", domain: value } });
          input.value = "";
          toast({ title: state.kind === "allow" ? "Domain allowlisted" : "Domain blocked", kind: "ok" });
          await refresh();
        } catch (error) {
          reportError(error);
        }
      });
    });

    $("#domain-rows").addEventListener("click", async (event) => {
      const button = event.target.closest("[data-remove-domain]");
      if (!button) return;
      const domain = button.dataset.removeDomain;
      await withBusy(button, async () => {
        try {
          await api("/api/adblock/domains", { method: "POST", body: { kind: state.kind, action: "remove", domain } });
          toast({ title: "Domain rule removed", text: domain, kind: "ok" });
          await refresh();
        } catch (error) {
          reportError(error);
        }
      });
    });

    $("#list-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const input = $("#list-input");
      const address = input.value.trim();
      if (!address) return;
      withBusy($("#list-submit"), async () => {
        try {
          await api("/api/adblock/lists", { method: "POST", body: { action: "add", address } });
          input.value = "";
          toast({ title: "Blocklist added", text: "Run “Update lists” to compile it into the gravity database.", kind: "ok", timeout: 8000 });
          await refresh();
        } catch (error) {
          reportError(error);
        }
      });
    });

    $("#list-rows").addEventListener("click", async (event) => {
      const button = event.target.closest("[data-remove-list]");
      if (!button) return;
      const address = button.dataset.removeList;
      const answer = await confirmAction({
        title: "Remove this blocklist?",
        message: address,
        confirmLabel: "Remove list",
        danger: true,
      });
      if (!answer || !answer.confirmed) return;
      await withBusy(button, async () => {
        try {
          await api("/api/adblock/lists", { method: "POST", body: { action: "remove", address } });
          toast({ title: "Blocklist removed", kind: "ok" });
          await refresh();
        } catch (error) {
          reportError(error);
        }
      });
    });

    $("#gravity-btn").addEventListener("click", async () => {
      const answer = await confirmAction({
        title: "Update blocklists now?",
        message: "Pi-hole downloads every list and rebuilds gravity. This can take a couple of minutes on a Pi 3.",
        confirmLabel: "Update now",
      });
      if (!answer || !answer.confirmed) return;
      withBusy($("#gravity-btn"), async () => {
        try {
          const result = await api("/api/adblock/gravity", { method: "POST", body: {} });
          toast({ title: "Blocklists updated", text: (result.output || "").split("\n").slice(-2).join(" "), kind: "ok", timeout: 9000 });
          await refresh();
        } catch (error) {
          reportError(error, "Updating the blocklists failed.");
        }
      });
    });

    $("#refresh-queries").addEventListener("click", () => loadQueries());
  });
})();
