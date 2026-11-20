/* Devices page: inventory, search, details and firewall blocking. */
(function () {
  "use strict";
  const { $, $$, api, fmt, poll, setText, toast, escapeHtml, reportError, withBusy, confirmAction, modal, decorateTables, signalBars } = window.NWOS;

  let devices = [];
  let query = "";

  function signalMarkup(dbm) {
    const quality = signalBars(dbm);
    if (!dbm) return '<span class="text-xs dim">Wired</span>';
    return `<span class="signal" data-level="${quality.level}" title="${quality.label}"><i></i><i></i><i></i><i></i></span>
      <span class="text-xs dim">${dbm} dBm</span>`;
  }

  function renderSummary(summary) {
    setText("total-value", fmt.number(summary.total));
    setText("total-meta", `${summary.wireless} wireless · ${summary.wired} wired`);
    setText("online-value", fmt.number(summary.online));
    setText("online-meta", `${summary.total ? Math.round((summary.online / summary.total) * 100) : 0}% of known devices`);
    setText("wireless-value", fmt.number(summary.wireless));
    setText("wireless-meta", summary.stations_available ? "Live from the station table" : (summary.stations_reason || "Station table unavailable"));
    setText("blocked-value", fmt.number(summary.blocked));
    setText("blocked-meta", summary.blocked ? "Denied at the firewall" : "No devices blocked");
    const pill = $("#blocked-pill");
    if (pill) pill.hidden = !summary.blocked;
  }

  function matches(device) {
    if (!query) return true;
    const haystack = [device.display_name, device.hostname, device.ip, device.mac].join(" ").toLowerCase();
    return haystack.includes(query);
  }

  function renderTable() {
    const tbody = $("#device-rows");
    const visible = devices.filter(matches);
    if (!devices.length) {
      tbody.innerHTML = `<tr><td colspan="7"><div class="empty">
        <span class="empty__icon"><svg class="icon icon-lg"><use href="#i-devices"/></svg></span>
        <strong>No devices have connected yet</strong>
        <p>Devices appear as soon as they take a DHCP lease or associate with the access point.</p>
      </div></td></tr>`;
      return;
    }
    if (!visible.length) {
      tbody.innerHTML = `<tr><td colspan="7"><div class="empty">
        <span class="empty__icon"><svg class="icon icon-lg"><use href="#i-search"/></svg></span>
        <strong>No device matches “${escapeHtml(query)}”</strong>
      </div></td></tr>`;
      return;
    }
    tbody.innerHTML = visible
      .map((device) => {
        const status = device.blocked
          ? '<span class="pill pill--bad"><span class="dot"></span>Blocked</span>'
          : device.online
          ? '<span class="pill pill--ok"><span class="dot"></span>Online</span>'
          : '<span class="pill pill--muted"><span class="dot"></span>Idle</span>';
        const session = device.connected_seconds
          ? fmt.duration(device.connected_seconds)
          : device.lease_remaining
          ? `Lease ${fmt.duration(device.lease_remaining)}`
          : "—";
        const traffic = device.rx_bytes || device.tx_bytes
          ? `↓ ${fmt.bytes(device.rx_bytes)} · ↑ ${fmt.bytes(device.tx_bytes)}`
          : '<span class="dim">—</span>';
        return `<tr data-mac="${escapeHtml(device.mac)}">
          <td>
            <div class="td-main">
              <span class="device-avatar"><svg class="icon icon-sm"><use href="#i-devices"/></svg></span>
              <span class="cell-stack">
                <span class="td-main">${escapeHtml(device.display_name)} ${status}</span>
                <span class="td-sub mono">${escapeHtml(device.mac)}</span>
              </span>
            </div>
          </td>
          <td class="mono">${escapeHtml(device.ip || "—")}</td>
          <td class="text-sm">${escapeHtml((device.connection || "unknown").replace(/^./, (c) => c.toUpperCase()))}</td>
          <td>${signalMarkup(device.signal_dbm)}</td>
          <td class="text-sm">${escapeHtml(session)}</td>
          <td class="text-sm">${traffic}</td>
          <td class="table__actions">
            <button class="btn btn--sm" data-detail="${escapeHtml(device.mac)}">Details</button>
            ${
              device.blocked
                ? `<button class="btn btn--sm" data-unblock="${escapeHtml(device.mac)}">Unblock</button>`
                : `<button class="btn btn--sm btn--soft" data-block="${escapeHtml(device.mac)}">Block</button>`
            }
          </td>
        </tr>`;
      })
      .join("");
    decorateTables(tbody.closest("table"));
  }

  async function refresh(silent) {
    try {
      const data = await api("/api/devices");
      devices = data.devices || [];
      renderSummary(data.summary || {});
      renderTable();
      const foot = $("#devices-foot");
      const stale = data.snapshot && data.snapshot.stale;
      foot.textContent = stale
        ? "The privileged snapshot is stale; values may lag behind the live system."
        : `Updated ${new Date().toLocaleTimeString()} · ${devices.length} device(s) known`;
      if (!silent) decorateTables(document);
    } catch (error) {
      if (!silent) reportError(error, "Could not read the device list.");
    }
  }

  function detailRow(label, value) {
    return `<div class="kv__row"><span class="kv__k">${escapeHtml(label)}</span><span class="kv__v">${value}</span></div>`;
  }

  async function showDetail(mac) {
    try {
      const { device } = await api(`/api/devices/${encodeURIComponent(mac)}`);
      const body = `
        <div class="kv">
          ${detailRow("Hostname", escapeHtml(device.hostname || "Unknown"))}
          ${detailRow("Description", escapeHtml(device.display_name))}
          ${detailRow("IP address", `<span class="mono">${escapeHtml(device.ip || "—")}</span>`)}
          ${detailRow("MAC address", `<span class="mono">${escapeHtml(device.mac)}</span>`)}
          ${detailRow("Connection", escapeHtml(device.connection || "unknown"))}
          ${detailRow("State", device.blocked ? "Blocked" : device.online ? "Online" : "Idle")}
          ${detailRow("Signal", device.signal_dbm ? `${signalBars(device.signal_dbm).label} (${device.signal_dbm} dBm)` : "Wired")}
          ${detailRow("Session", device.connected_seconds ? fmt.duration(device.connected_seconds) : "—")}
          ${detailRow("Traffic", device.rx_bytes || device.tx_bytes ? `↓ ${fmt.bytes(device.rx_bytes)} · ↑ ${fmt.bytes(device.tx_bytes)}` : "—")}
          ${detailRow("Lease remaining", device.lease_remaining ? fmt.duration(device.lease_remaining) : "—")}
          ${detailRow("First seen", escapeHtml(device.first_seen ? fmt.clock(device.first_seen) : "—"))}
          ${detailRow("Last seen", escapeHtml(device.last_seen ? fmt.ago(device.last_seen) : "—"))}
        </div>`;
      modal.open({
        title: device.display_name,
        message: device.mac,
        body,
        footer: [{ label: "Close", className: "btn", onClick: () => modal.close(null) }],
      });
    } catch (error) {
      reportError(error, "Could not load the device details.");
    }
  }

  async function toggleBlock(mac, blocking, button) {
    if (blocking) {
      const answer = await confirmAction({
        title: "Block this device?",
        message: "All traffic from this device is dropped at the firewall and any wireless session is ended.",
        confirmLabel: "Block device",
        danger: true,
      });
      if (!answer || !answer.confirmed) return;
    }
    await withBusy(button, async () => {
      try {
        await api(`/api/devices/${encodeURIComponent(mac)}/${blocking ? "block" : "unblock"}`, { method: "POST", body: {} });
        toast({ title: blocking ? "Device blocked" : "Device unblocked", kind: "ok" });
        refresh(true);
      } catch (error) {
        reportError(error);
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("#device-search").addEventListener("input", (event) => {
      query = event.target.value.trim().toLowerCase();
      renderTable();
    });
    $("#refresh-devices").addEventListener("click", () => refresh());

    $("#device-rows").addEventListener("click", (event) => {
      const detail = event.target.closest("[data-detail]");
      if (detail) return showDetail(detail.dataset.detail);
      const block = event.target.closest("[data-block]");
      if (block) return toggleBlock(block.dataset.block, true, block);
      const unblock = event.target.closest("[data-unblock]");
      if (unblock) return toggleBlock(unblock.dataset.unblock, false, unblock);
      return undefined;
    });

    poll(() => refresh(true), 6000);
    window.refreshPage = () => refresh();
  });
})();
