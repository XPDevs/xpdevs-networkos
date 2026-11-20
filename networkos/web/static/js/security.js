/* Security page: account, sessions, policy and audit trail. */
(function () {
  "use strict";
  const { $, $$, api, fmt, setText, toast, escapeHtml, reportError, withBusy, confirmAction, decorateTables } = window.NWOS;

  function kvRow(label, value, mono) {
    return `<div class="kv__row"><span class="kv__k">${escapeHtml(label)}</span>
      <span class="kv__v${mono ? " mono" : ""}">${value}</span></div>`;
  }

  function render(data) {
    const account = data.account;
    const policy = data.policy;
    setText("hash-pill", account.hash);
    $("#account-kv").innerHTML = [
      kvRow("Username", escapeHtml(account.username), true),
      kvRow("Password hash", escapeHtml(account.hash)),
      kvRow("Account created", escapeHtml(fmt.clock(account.created_at))),
      kvRow("Password changed", escapeHtml(fmt.clock(account.password_changed_at))),
      kvRow("Last sign-in", account.last_login_at ? escapeHtml(fmt.clock(account.last_login_at)) : "—"),
      kvRow("Active sessions", String(data.sessions.length)),
    ].join("");

    $("#policy-kv").innerHTML = [
      kvRow("Minimum password length", `${policy.min_length} characters`),
      kvRow("Idle session limit", `${policy.idle_minutes} minutes`),
      kvRow("Absolute session limit", `${policy.absolute_hours} hours`),
      kvRow("Login throttling", `${policy.max_attempts} attempts / ${policy.window_minutes} min, then lockout`),
      kvRow("Dashboard exposure", policy.allow_public_access ? "Public (not recommended)" : "Local network only"),
      kvRow("Request protection", "CSRF token + same-origin check"),
      kvRow("Content security", "Strict CSP, no inline script or style"),
      kvRow("Password hashing", policy.argon2 ? "Argon2id" : "scrypt (Argon2id unavailable)"),
    ].join("");

    renderSessions(data.sessions);
    renderAudit(data.audit);
  }

  function renderSessions(sessions) {
    const rows = $("#session-rows");
    if (!sessions.length) {
      rows.innerHTML = '<tr><td colspan="5"><div class="text-sm dim">No active sessions.</div></td></tr>';
      return;
    }
    rows.innerHTML = sessions.map((session) => `<tr>
      <td class="mono">${escapeHtml(session.ip || "—")}</td>
      <td class="text-sm">
        <div class="cell-stack">
          <span class="td-main">${escapeHtml((session.user_agent || "Unknown client").slice(0, 60))}</span>
          ${session.current ? '<span class="pill pill--info"><span class="dot"></span>This session</span>' : ""}
        </div>
      </td>
      <td class="text-sm">${escapeHtml(fmt.clock(session.created_at))}</td>
      <td class="text-sm">${escapeHtml(fmt.ago(session.last_seen))}</td>
      <td class="table__actions">
        ${session.current
          ? '<button class="btn btn--sm" data-signout>Sign out</button>'
          : `<button class="btn btn--sm" data-revoke="${escapeHtml(session.id)}">Revoke</button>`}
      </td>
    </tr>`).join("");
    decorateTables(rows.closest("table"));
  }

  function renderAudit(entries) {
    const rows = $("#audit-rows");
    if (!entries.length) {
      rows.innerHTML = '<tr><td colspan="4"><div class="text-sm dim">No audit entries yet.</div></td></tr>';
      return;
    }
    rows.innerHTML = entries.map((entry) => `<tr>
      <td class="text-sm">${escapeHtml(fmt.clock(entry.at))}</td>
      <td class="text-sm mono">${escapeHtml(entry.username || "—")}</td>
      <td class="text-sm">${escapeHtml(entry.action)}</td>
      <td class="text-sm dim">${escapeHtml(entry.detail || entry.target || "")}</td>
    </tr>`).join("");
    decorateTables(rows.closest("table"));
  }

  async function refresh() {
    try {
      render(await api("/api/security"));
    } catch (error) {
      reportError(error, "Could not read the security state.");
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    refresh();
    window.refreshPage = refresh;

    $("#password-form").addEventListener("submit", (event) => {
      event.preventDefault();
      $$(".field.has-error").forEach((field) => field.classList.remove("has-error"));
      const payload = {
        current_password: $("#current_password").value,
        new_password: $("#new_password").value,
        confirm_password: $("#confirm_password").value,
      };
      withBusy($("#save-password"), async () => {
        try {
          await api("/api/security/password", { method: "POST", body: payload });
          $("#password-form").reset();
          toast({ title: "Password updated", text: "Other sessions were signed out.", kind: "ok" });
          refresh();
        } catch (error) {
          const node = $(`[data-error-for="${error.code}"]`);
          if (node) {
            node.textContent = error.message;
            node.closest(".field").classList.add("has-error");
          }
          reportError(error);
        }
      });
    });

    $("#session-rows").addEventListener("click", async (event) => {
      const signout = event.target.closest("[data-signout]");
      if (signout) {
        const form = document.createElement("form");
        form.method = "post";
        form.action = "/logout";
        const input = document.createElement("input");
        input.type = "hidden";
        input.name = "csrf_token";
        input.value = document.body.dataset.csrf;
        form.appendChild(input);
        document.body.appendChild(form);
        form.submit();
        return;
      }
      const button = event.target.closest("[data-revoke]");
      if (!button) return;
      const answer = await confirmAction({
        title: "Revoke this session?",
        message: "That browser is signed out immediately and must sign in again.",
        confirmLabel: "Revoke session",
        danger: true,
      });
      if (!answer || !answer.confirmed) return;
      await withBusy(button, async () => {
        try {
          await api("/api/security/sessions/revoke", { method: "POST", body: { id: button.dataset.revoke } });
          toast({ title: "Session revoked", kind: "ok" });
          refresh();
        } catch (error) {
          reportError(error);
        }
      });
    });
  });
})();
