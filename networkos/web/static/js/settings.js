/* Settings page: appearance, naming, retention and exposure. */
(function () {
  "use strict";
  const { $, $$, api, fmt, setText, toast, escapeHtml, reportError, withBusy, confirmAction, applyTheme } = window.NWOS;

  function kvRow(label, value, mono) {
    return `<div class="kv__row"><span class="kv__k">${escapeHtml(label)}</span>
      <span class="kv__v${mono ? " mono" : ""}">${value}</span></div>`;
  }

  function render(data) {
    setText("dashboard_hostname", data.system.dashboard_hostname);
    const about = $("#about-kv");
    about.innerHTML = [
      kvRow("Product", `${escapeHtml(data.ui.product_name)} ${escapeHtml(data.system.version)}`),
      kvRow("Configuration", escapeHtml(data.system.config_path), true),
      kvRow("State directory", escapeHtml(data.system.state_path), true),
      kvRow("Pi-hole API", escapeHtml(data.pihole.api_base), true),
      kvRow("Query retention", `${data.pihole.retention_days} days`),
      kvRow("Dashboard database", fmt.bytes(data.storage.database_bytes)),
      kvRow("Log file", fmt.bytes(data.storage.log_bytes)),
      kvRow("Snapshots", escapeHtml(data.storage.backup_dir), true),
      kvRow("Session idle limit", `${data.security.session_idle_minutes} minutes`),
      kvRow("Login throttling", `${data.security.login_max_attempts} attempts`),
    ].join("");

    const exposure = $("#exposure-pill");
    if (data.security.allow_public_access) {
      exposure.className = "pill pill--bad";
      exposure.querySelector("span:last-child").textContent = "Public";
    } else {
      exposure.className = "pill pill--ok";
      exposure.querySelector("span:last-child").textContent = "LAN only";
    }
  }

  async function refresh() {
    try {
      render(await api("/api/settings"));
    } catch (error) {
      reportError(error, "Could not read the settings.");
    }
  }

  async function save(payload, button) {
    await withBusy(button, async () => {
      try {
        const result = await api("/api/settings", { method: "POST", body: payload });
        toast({ title: "Settings saved", text: (result.changed || []).join(", "), kind: "ok" });
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
  }

  document.addEventListener("DOMContentLoaded", () => {
    refresh();
    window.refreshPage = refresh;

    $("#appearance-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const theme = $("#theme_default").value;
      applyTheme(theme);
      save({ theme_default: theme }, $("#save-appearance"));
    });

    $("#hostname-form").addEventListener("submit", (event) => {
      event.preventDefault();
      save({ dashboard_hostname: $("#dashboard_hostname").value.trim() }, $("#save-hostname"));
    });

    $("#republish-mdns").addEventListener("click", () =>
      withBusy($("#republish-mdns"), async () => {
        try {
          const result = await api("/api/settings/mdns", { method: "POST", body: {} });
          toast({ title: "Name republished", text: `http://${result.hostname}`, kind: "ok" });
        } catch (error) {
          reportError(error);
        }
      })
    );

    $("#retention-form").addEventListener("submit", (event) => {
      event.preventDefault();
      save({ retention_days: Number($("#retention_days").value) }, $("#save-retention"));
    });

    $("#allow_public_access").addEventListener("change", async (event) => {
      const enabled = event.target.checked;
      if (enabled) {
        const answer = await confirmAction({
          title: "Allow public access?",
          message: "Requests from public IP addresses would reach the sign-in page. Only do this behind a TLS reverse proxy you control.",
          confirmLabel: "Allow public access",
          danger: true,
          phrase: "CONFIRM-PUBLIC",
        });
        if (!answer || !answer.confirmed) {
          event.target.checked = false;
          return;
        }
      }
      save({ allow_public_access: enabled }, event.target);
    });
  });
})();
