/* System page: hardware facts, services, maintenance and snapshots. */
(function () {
  "use strict";
  const { $, $$, api, fmt, setText, toast, escapeHtml, reportError, withBusy, confirmAction, decorateTables } = window.NWOS;

  const SERVICE_LABELS = {
    ap: "Access point (hostapd)",
    dhcp: "DHCP server (dnsmasq)",
    net: "Network setup",
    pihole: "Pi-hole FTL",
    web: "Dashboard",
    mdns: "mDNS publisher",
    lighttpd: "Lighttpd (legacy)",
  };

  function kvRow(label, value, mono) {
    return `<div class="kv__row"><span class="kv__k">${escapeHtml(label)}</span>
      <span class="kv__v${mono ? " mono" : ""}">${value}</span></div>`;
  }

  function showBanner(message, kind) {
    const node = $("#system-banner");
    if (!message) { node.hidden = true; return; }
    node.hidden = false;
    node.innerHTML = `<div class="banner banner--${kind || "warn"}">
      <svg class="icon"><use href="#i-alert"/></svg>
      <div class="banner__body">${escapeHtml(message)}</div>
    </div>`;
  }

  function render(data) {
    const model = data.model;
    const firmware = data.firmware;
    setText("hardware-sub", model.name);
    setText("software-sub", `${data.os.name} · kernel ${data.kernel.release}`);

    $("#hardware-kv").innerHTML = [
      kvRow("Model", escapeHtml(model.name)),
      kvRow("Serial", escapeHtml(model.serial || "not available"), true),
      kvRow("CPU cores", String(data.cpu_count)),
      kvRow("Memory", `${fmt.bytes(data.memory.total)} total · ${fmt.bytes(data.memory.used)} used`),
      kvRow("Storage", `${fmt.bytes(data.storage.total)} total · ${fmt.bytes(data.storage.free)} free`),
      kvRow("Temperature", escapeHtml(fmt.temp(data.temperature.celsius))),
      kvRow("Uptime", escapeHtml(data.uptime.human)),
      kvRow("Load average", `${data.load["1m"]} / ${data.load["5m"]} / ${data.load["15m"]}`),
      kvRow("Firmware", escapeHtml(firmware.version || "not reported")),
      kvRow(
        "Throttling",
        firmware.throttled
          ? firmware.throttled.undervoltage_now || firmware.throttled.throttled_now
            ? '<span class="pill pill--bad"><span class="dot"></span>Active now</span>'
            : firmware.throttled.undervoltage_occurred || firmware.throttled.throttled_occurred
            ? '<span class="pill pill--warn"><span class="dot"></span>Occurred</span>'
            : '<span class="pill pill--ok"><span class="dot"></span>None</span>'
          : '<span class="pill pill--muted"><span class="dot"></span>Not reported</span>'
      ),
    ].join("");

    $("#software-kv").innerHTML = [
      kvRow("Product", `${escapeHtml(data.product.name)} ${escapeHtml(data.product.version)}`),
      kvRow("Operating system", escapeHtml(data.os.name)),
      kvRow("Kernel", escapeHtml(`${data.kernel.release} (${data.kernel.arch})`), true),
      kvRow("Hostname", escapeHtml(data.kernel.hostname), true),
      kvRow("Python", escapeHtml(data.kernel.python), true),
      kvRow("System time", escapeHtml(fmt.clock(data.time.utc))),
      kvRow("Install footprint", escapeHtml(data.footprint.human)),
      kvRow("Database size", `${fmt.bytes(data.storage.used)} used on the root filesystem`),
    ].join("");

    const apply = data.last_apply;
    if (apply) {
      setText(
        "apply-foot",
        `Last configuration change: ${apply.reason} at ${fmt.clock(apply.at)} — ${apply.ok ? "applied and verified" : "failed"}` +
          (apply.rollback ? ` (${apply.rollback})` : "")
      );
    }
    if (data.safe_mode) {
      showBanner(`Safe mode is active: ${data.safe_mode}. The wireless configuration was not applied; fix the settings and apply again.`, "bad");
    } else if (apply && apply.ok === false) {
      showBanner(`The last network change failed: ${apply.error || "unknown error"}. ${apply.rollback || ""}`, "warn");
    } else {
      showBanner(null);
    }

    renderServices(data.services);
  }

  function renderServices(services) {
    const rows = $("#service-rows");
    const entries = Object.entries(services || {});
    if (!entries.length) {
      rows.innerHTML = '<tr><td colspan="5"><div class="text-sm dim">systemd is not available on this system.</div></td></tr>';
      return;
    }
    rows.innerHTML = entries
      .filter(([key]) => SERVICE_LABELS[key])
      .map(([key, service]) => {
        const running = service.running;
        return `<tr>
          <td class="td-main">${escapeHtml(SERVICE_LABELS[key] || key)}</td>
          <td class="mono text-sm">${escapeHtml(key === "ap" ? "xpdevs-networkos-ap" : key === "dhcp" ? "xpdevs-networkos-dnsmasq" : key === "web" ? "xpdevs-networkos-web" : key === "pihole" ? "pihole-FTL" : key)}</td>
          <td><span class="pill pill--${running ? "ok" : service.active === "inactive" ? "muted" : "bad"}">
            <span class="dot"></span>${escapeHtml(service.active || "unknown")}</span></td>
          <td class="text-sm">${service.enabled ? "Enabled" : escapeHtml(service.enabled_state || "—")}</td>
          <td class="table__actions">
            <button class="btn btn--sm" data-restart="${escapeHtml(key)}">Restart</button>
          </td>
        </tr>`;
      })
      .join("");
    decorateTables(rows.closest("table"));
  }

  async function refresh() {
    try {
      render(await api("/api/system"));
    } catch (error) {
      reportError(error, "Could not read the system state.");
    }
  }

  async function loadBackups() {
    const rows = $("#backup-rows");
    try {
      const { backups } = await api("/api/system/backups");
      if (!backups.length) {
        rows.innerHTML = '<tr><td colspan="5"><div class="text-sm dim">No snapshots yet.</div></td></tr>';
        return;
      }
      rows.innerHTML = backups.map((backup) => `<tr>
        <td class="mono text-sm">${escapeHtml(backup.name)}</td>
        <td class="text-sm">${escapeHtml(backup.label)}</td>
        <td class="text-sm">${escapeHtml(fmt.clock(backup.created_at))}</td>
        <td class="text-sm">${fmt.bytes(backup.size)}</td>
        <td class="table__actions">
          <button class="btn btn--sm" data-restore="${escapeHtml(backup.name)}">Restore</button>
        </td>
      </tr>`).join("");
      decorateTables(rows.closest("table"));
    } catch (error) {
      rows.innerHTML = '<tr><td colspan="5"><div class="text-sm dim">Snapshots could not be listed.</div></td></tr>';
    }
  }

  async function runAction(action, button, phrase, label, extra) {
    const answer = await confirmAction({
      title: label,
      message: extra || "This action affects the running appliance.",
      confirmLabel: label,
      danger: action !== "update",
      phrase,
    });
    if (!answer || !answer.confirmed) return;
    await withBusy(button, async () => {
      try {
        const result = await api("/api/system/action", { method: "POST", body: { action, confirm: phrase } });
        toast({
          title: `${label} requested`,
          text: action === "update"
            ? (result.steps || []).join(", ")
            : action === "reboot"
            ? "The appliance is rebooting; reconnect in about 45 seconds."
            : "The appliance is shutting down.",
          kind: "ok",
          timeout: 10000,
        });
        if (action === "update") refresh();
      } catch (error) {
        reportError(error);
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    refresh();
    loadBackups();
    window.refreshPage = () => { refresh(); loadBackups(); };

    $("#refresh-services").addEventListener("click", () => refresh());
    $("#refresh-backups").addEventListener("click", () => loadBackups());

    $("#service-rows").addEventListener("click", async (event) => {
      const button = event.target.closest("[data-restart]");
      if (!button) return;
      const key = button.dataset.restart;
      const answer = await confirmAction({
        title: `Restart ${SERVICE_LABELS[key] || key}?`,
        message: "The unit restarts immediately. Clients may reconnect briefly.",
        confirmLabel: "Restart service",
      });
      if (!answer || !answer.confirmed) return;
      await withBusy(button, async () => {
        try {
          await api("/api/system/service-restart", { method: "POST", body: { service: key } });
          toast({ title: "Service restarted", kind: "ok" });
          refresh();
        } catch (error) {
          reportError(error);
        }
      });
    });

    $("#backup-rows").addEventListener("click", async (event) => {
      const button = event.target.closest("[data-restore]");
      if (!button) return;
      const name = button.dataset.restore;
      const answer = await confirmAction({
        title: "Restore this snapshot?",
        message: `The stored configuration replaces the current network settings and is applied immediately.\n${name}`,
        confirmLabel: "Restore snapshot",
        danger: true,
        phrase: "CONFIRM-RESTORE",
      });
      if (!answer || !answer.confirmed) return;
      await withBusy(button, async () => {
        try {
          await api("/api/system/backup", { method: "POST", body: { action: "restore", name, confirm: "CONFIRM-RESTORE" } });
          toast({ title: "Snapshot restored", kind: "ok" });
          refresh();
        } catch (error) {
          reportError(error);
        }
      });
    });

    $("#backup-btn").addEventListener("click", () =>
      withBusy($("#backup-btn"), async () => {
        try {
          await api("/api/system/backup", { method: "POST", body: { action: "create" } });
          toast({ title: "Snapshot created", kind: "ok" });
          loadBackups();
        } catch (error) {
          reportError(error);
        }
      })
    );

    $("#update-btn").addEventListener("click", () =>
      runAction("update", $("#update-btn"), "CONFIRM-UPDATE", "Update software",
        "apt-get update and upgrade run now. Existing configuration files are kept (--force-confold). This can take several minutes.")
    );

    $("#reboot-btn").addEventListener("click", () =>
      runAction("reboot", $("#reboot-btn"), "CONFIRM-REBOOT", "Reboot")
    );

    $("#shutdown-btn").addEventListener("click", () =>
      runAction("shutdown", $("#shutdown-btn"), "CONFIRM-SHUTDOWN", "Shut down")
    );

    $("#load-journal").addEventListener("click", () =>
      withBusy($("#load-journal"), async () => {
        const unit = $("#journal-unit").value;
        const output = $("#journal-output");
        output.textContent = "Loading…";
        try {
          const data = await api(`/api/system/journal?unit=${encodeURIComponent(unit)}&lines=80`);
          output.textContent = data.lines.length ? data.lines.join("\n") : "No log lines available for this unit.";
        } catch (error) {
          output.textContent = error.message || "The journal could not be read.";
        }
      })
    );
  });
})();
