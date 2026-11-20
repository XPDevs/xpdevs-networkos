/* Wireless page: access point state, configuration form and client table. */
(function () {
  "use strict";
  const { $, $$, api, fmt, poll, setText, toast, escapeHtml, reportError, withBusy, confirmAction, decorateTables, signalBars } = window.NWOS;

  let lastState = null;

  function pill(node, tone, text) {
    if (!node) return;
    node.className = `pill pill--${tone}`;
    const label = node.querySelector("span:last-child");
    if (label) label.textContent = text;
    const dot = node.querySelector(".dot");
    if (dot) dot.classList.toggle("dot--pulse", tone === "ok");
  }

  function signalMarkup(dbm) {
    const quality = signalBars(dbm);
    return `<span class="signal" data-level="${quality.level}" title="${quality.label}">
      <i></i><i></i><i></i><i></i>
    </span> <span class="text-xs dim">${dbm === null || dbm === undefined ? "—" : `${dbm} dBm`}</span>`;
  }

  function render(state) {
    lastState = state;
    const ap = state.ap;

    setText("ap-headline", ap.ssid);
    setText("ap-subline", `${ap.broadcasting ? "Broadcasting" : ap.running ? "Starting" : "Stopped"} on ${ap.interface} · ${ap.band} · channel ${ap.channel}`);
    pill($("#ap-state-pill"), ap.broadcasting ? "ok" : ap.running ? "warn" : "bad",
      ap.broadcasting ? "Broadcasting" : ap.running ? "Starting up" : "Stopped");

    setText("stat-ssid", ap.ssid);
    setText("stat-ssid-meta", ap.hidden ? "Hidden network" : "Visible to nearby devices");
    setText("stat-channel", ap.channel);
    setText("stat-channel-meta", `${ap.band} · ${ap.security}`);
    setText("stat-clients", ap.clients_online ?? ap.clients ?? 0);
    setText("stat-clients-meta", `${ap.clients ?? 0} associated in total`);
    pill($("#client-pill"), (ap.clients_online || 0) > 0 ? "info" : "muted", `${ap.clients ?? 0} associated`);

    const capability = state.capabilities || {};
    if (capability.supported) {
      pill($("#capability-pill"), "ok", "AP mode supported");
    } else {
      pill($("#capability-pill"), "bad", capability.reason || "AP mode unavailable");
    }

    setText("security-label", ap.security);
    pill($("#security-pill"), ap.security && ap.security.indexOf("WPA2") === 0 ? "ok" : "warn",
      ap.security && ap.security.indexOf("WPA2") === 0 ? "Protected" : "Check settings");

    const toggle = $("#toggle-wifi-label");
    if (toggle) toggle.textContent = ap.enabled ? "Disable Wi-Fi" : "Enable Wi-Fi";

    renderClients(state.clients || []);
    const foot = $("#clients-foot");
    if (foot) {
      foot.textContent = state.stations_reason
        ? `Station table unavailable: ${state.stations_reason}`
        : `${(state.clients || []).length} client(s) in the station table.`;
    }
  }

  function renderClients(clients) {
    const tbody = $("#client-rows");
    if (!tbody) return;
    if (!clients.length) {
      tbody.innerHTML = `<tr><td colspan="6"><div class="empty">
        <span class="empty__icon"><svg class="icon icon-lg"><use href="#i-wifi"/></svg></span>
        <strong>No wireless clients connected</strong>
        <p>Devices that join “${escapeHtml(document.body.dataset.ssid)}” appear here with live signal and traffic.</p>
      </div></td></tr>`;
      return;
    }
    tbody.innerHTML = clients.map((client) => {
      const action = client.blocked
        ? `<button class="btn btn--sm" data-unblock="${escapeHtml(client.mac)}">Unblock</button>`
        : `<button class="btn btn--sm" data-block="${escapeHtml(client.mac)}">Block</button>`;
      return `<tr>
        <td>
          <div class="td-main">
            <span class="device-avatar"><svg class="icon icon-sm"><use href="#i-devices"/></svg></span>
            <span class="cell-stack">
              <span class="td-main">${escapeHtml(client.display_name)}</span>
              <span class="td-sub mono">${escapeHtml(client.mac)}</span>
            </span>
          </div>
        </td>
        <td class="mono">${escapeHtml(client.ip || "—")}</td>
        <td>${signalMarkup(client.signal_dbm)}</td>
        <td class="text-sm">${client.connected_seconds ? fmt.duration(client.connected_seconds) : "—"}</td>
        <td class="text-sm">↓ ${fmt.bytes(client.rx_bytes)} · ↑ ${fmt.bytes(client.tx_bytes)}</td>
        <td class="table__actions">${action}</td>
      </tr>`;
    }).join("");
    decorateTables(tbody.closest("table"));
  }

  async function refresh() {
    try {
      render(await api("/api/wireless"));
    } catch (error) {
      reportError(error, "Could not read the wireless state.");
    }
  }

  function clearFieldErrors() {
    $$(".field.has-error").forEach((field) => field.classList.remove("has-error"));
  }

  function showFieldError(code, message) {
    const target = $(`[data-error-for="${code}"]`);
    if (target) {
      target.textContent = message;
      target.closest(".field").classList.add("has-error");
      target.closest(".field").scrollIntoView({ block: "center", behavior: "smooth" });
    }
    toast({ title: "Not applied", text: message, kind: "bad", timeout: 8000 });
  }

  document.addEventListener("DOMContentLoaded", () => {
    poll(refresh, 7000);
    window.refreshPage = refresh;

    $("#wifi-form").addEventListener("submit", (event) => {
      event.preventDefault();
      clearFieldErrors();
      const button = $("#save-wifi");
      const payload = {
        ssid: $("#ssid").value.trim(),
        password: $("#password").value,
        channel: Number($("#channel").value),
        country: $("#country").value.trim().toUpperCase(),
        hidden: $("#hidden").checked,
        isolate: $("#isolate").checked,
        max_clients: Number($("#max_clients").value || 32),
      };
      const apply = async () => {
        const result = await api("/api/wireless/config", { method: "POST", body: payload });
        $("#password").value = "";
        toast({
          title: "Wireless settings applied",
          text: `${result.ssid} on channel ${result.channel}. Clients reconnect within a few seconds.`,
          kind: "ok",
          timeout: 9000,
        });
        refresh();
      };
      const run = () => withBusy(button, apply).catch((error) => {
        if (error.code) showFieldError(error.code, error.message);
        else reportError(error);
      });
      if (payload.ssid !== document.body.dataset.ssid || payload.password) {
        confirmAction({
          title: "Apply wireless changes?",
          message: "The access point restarts and every connected device reconnects. The wired management connection is not affected.",
          confirmLabel: "Apply and restart AP",
        }).then((result) => { if (result && result.confirmed) run(); });
      } else {
        run();
      }
    });

    $("#toggle-wifi").addEventListener("click", async () => {
      const enabled = lastState ? !lastState.ap.enabled : true;
      const answer = await confirmAction({
        title: enabled ? "Enable the access point?" : "Disable the access point?",
        message: enabled
          ? "The wireless network starts broadcasting again."
          : "All wireless clients disconnect. The dashboard stays reachable over Ethernet.",
        confirmLabel: enabled ? "Enable Wi-Fi" : "Disable Wi-Fi",
        danger: !enabled,
      });
      if (!answer || !answer.confirmed) return;
      withBusy($("#toggle-wifi"), async () => {
        try {
          await api("/api/wireless/power", { method: "POST", body: { enabled } });
          toast({ title: enabled ? "Wi-Fi enabled" : "Wi-Fi disabled", kind: "ok" });
          refresh();
        } catch (error) {
          reportError(error);
        }
      });
    });

    $("#restart-ap").addEventListener("click", async () => {
      const answer = await confirmAction({
        title: "Restart the access point?",
        message: "hostapd restarts and wireless clients reconnect. Takes about ten seconds.",
        confirmLabel: "Restart now",
      });
      if (!answer || !answer.confirmed) return;
      withBusy($("#restart-ap"), async () => {
        try {
          await api("/api/wireless/restart", { method: "POST", body: {} });
          toast({ title: "Access point restarted", kind: "ok" });
          refresh();
        } catch (error) {
          reportError(error);
        }
      });
    });

    $("#scan-btn").addEventListener("click", async () => {
      const answer = await confirmAction({
        title: "Scan for nearby networks?",
        message: "The access point pauses for a few seconds while the radio scans. Clients reconnect afterwards.",
        confirmLabel: "Scan now",
      });
      if (!answer || !answer.confirmed) return;
      withBusy($("#scan-btn"), async () => {
        try {
          const report = await api("/api/wireless/scan", { method: "POST", body: { pause_ap: true } });
          renderScan(report);
          toast({ title: "Scan complete", text: `Recommended channel: ${report.recommended_channel}.`, kind: "ok" });
          refresh();
        } catch (error) {
          reportError(error, "The channel scan failed.");
        }
      });
    });

    $("#client-rows").addEventListener("click", async (event) => {
      const blockButton = event.target.closest("[data-block]");
      const unblockButton = event.target.closest("[data-unblock]");
      if (!blockButton && !unblockButton) return;
      const mac = (blockButton || unblockButton).dataset.block || (blockButton || unblockButton).dataset.unblock;
      const blocking = Boolean(blockButton);
      if (blocking) {
        const answer = await confirmAction({
          title: "Block this device?",
          message: "The firewall drops all traffic from this MAC address and the device is disconnected.",
          confirmLabel: "Block device",
          danger: true,
        });
        if (!answer || !answer.confirmed) return;
      }
      withBusy(blockButton || unblockButton, async () => {
        try {
          await api(`/api/devices/${mac}/${blocking ? "block" : "unblock"}`, { method: "POST", body: {} });
          toast({ title: blocking ? "Device blocked" : "Device unblocked", kind: "ok" });
          refresh();
        } catch (error) {
          reportError(error);
        }
      });
    });
  });

  function renderScan(report) {
    const wrap = $("#scan-results");
    const networks = (report.networks || []).filter((network) => network.ssid);
    const usage = report.channel_usage || {};
    const usageText = Object.keys(usage).length
      ? Object.entries(usage).map(([channel, count]) => `ch ${channel}: ${count}`).join(" · ")
      : "No other networks detected";
    if (!networks.length) {
      wrap.innerHTML = `<div class="empty">
        <span class="empty__icon"><svg class="icon icon-lg"><use href="#i-signal"/></svg></span>
        <strong>No neighbouring networks found</strong>
        <p>${escapeHtml(usageText)}</p>
      </div>`;
      return;
    }
    wrap.innerHTML = `
      <div class="banner banner--ok mb-3">
        <svg class="icon"><use href="#i-check-circle"/></svg>
        <div class="banner__body">Recommended channel <strong>${report.recommended_channel}</strong> — ${escapeHtml(usageText)}</div>
      </div>
      <div class="table-wrap">
        <table class="table table--stack">
          <thead><tr><th>Network</th><th>Channel</th><th>Signal</th></tr></thead>
          <tbody>
            ${networks.slice(0, 12).map((network) => `<tr>
              <td class="td-main">${escapeHtml(network.ssid)}</td>
              <td class="mono">${network.channel ?? "—"}</td>
              <td>${signalMarkup(network.signal === null ? null : Math.round(network.signal))}</td>
            </tr>`).join("")}
          </tbody>
        </table>
      </div>`;
    decorateTables(wrap);
  }
})();
