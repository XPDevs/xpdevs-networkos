/* Overview page: live status, throughput chart and resource meters. */
(function () {
  "use strict";
  const { $, $$, api, fmt, poll, setText, setProgress, tone, toast, escapeHtml, reportError, withBusy, decorateTables } = window.NWOS;

  const state = { history: [] };

  function pill(node, tone, text) {
    if (!node) return;
    node.className = `pill pill--${tone}`;
    const textNode = node.querySelector("span:last-child");
    if (textNode) textNode.textContent = text;
    const dot = node.querySelector(".dot");
    if (dot) dot.classList.toggle("dot--pulse", tone === "ok");
  }

  function drawTraffic(canvas, history) {
    if (!canvas) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const width = canvas.clientWidth || 400;
    const height = canvas.clientHeight || 120;
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const points = history.slice(-40);
    if (points.length < 2) {
      ctx.strokeStyle = "rgba(125,145,170,.4)";
      ctx.setLineDash([4, 5]);
      ctx.beginPath();
      ctx.moveTo(0, height - 12);
      ctx.lineTo(width, height - 12);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "rgba(125,145,170,.85)";
      ctx.font = "12px InterVariable, system-ui, sans-serif";
      ctx.fillText("Collecting traffic samples…", 8, height - 22);
      return;
    }

    const peak = Math.max(1024, ...points.map((p) => Math.max(p.rx, p.tx))) * 1.2;
    const stepX = width / Math.max(1, points.length - 1);
    const yOf = (value) => height - (value / peak) * (height - 8) - 4;

    // horizontal guides
    ctx.strokeStyle = "rgba(125,145,170,.16)";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 2; i += 1) {
      const y = 4 + ((height - 8) / 2) * i;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(width, y);
      ctx.stroke();
    }

    const seriesFor = (key, color, fill) => {
      ctx.beginPath();
      points.forEach((point, index) => {
        const x = index * stepX;
        const y = yOf(point[key]);
        if (index === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.9;
      ctx.lineJoin = "round";
      ctx.stroke();

      ctx.lineTo((points.length - 1) * stepX, height);
      ctx.lineTo(0, height);
      ctx.closePath();
      ctx.fillStyle = fill;
      ctx.fill();
    };

    seriesFor("rx", "#1d6ae5", "rgba(29,106,229,.14)");
    seriesFor("tx", "#0e9f6e", "rgba(14,159,110,.10)");
  }

  function renderMetrics(metrics) {
    const cpu = metrics.cpu.percent;
    setText("cpu-value", cpu === null || cpu === undefined ? "—" : `${cpu.toFixed(0)}%`);
    setProgress($("#cpu-bar"), cpu || 0, tone(cpu || 0));
    if (cpu !== null && cpu !== undefined) {
      $("#cpu-progress").classList.remove("progress--ok", "progress--warn", "progress--bad");
      $("#cpu-progress").classList.add(`progress--${tone(cpu)}`);
    }

    const mem = metrics.memory;
    setText("memory-value", `${fmt.bytes(mem.used)} / ${fmt.bytes(mem.total)}`);
    setProgress($("#memory-bar"), mem.percent, tone(mem.percent));
    $("#memory-progress").className = `progress progress--${tone(mem.percent)}`;

    const disk = metrics.storage;
    setText("storage-value", `${fmt.bytes(disk.used)} / ${fmt.bytes(disk.total)}`);
    setProgress($("#storage-bar"), disk.percent, tone(disk.percent));
    $("#storage-progress").className = `progress progress--${tone(disk.percent)}`;

    const temp = metrics.temperature.celsius;
    setText("temp-value", fmt.temp(temp));
    const tempPercent = temp === null || temp === undefined ? 0 : Math.min(100, (temp / 90) * 100);
    setProgress($("#temp-bar"), tempPercent, temp >= 75 ? "bad" : temp >= 65 ? "warn" : "ok");
    $("#temp-progress").className = `progress progress--${temp >= 75 ? "bad" : temp >= 65 ? "warn" : "ok"}`;

    setText("uptime-pill", `Up ${metrics.uptime.human}`);

    const traffic = metrics.traffic || {};
    const wan = document.body.dataset.wanInterface;
    const lan = document.body.dataset.lanInterface;
    const entry = traffic[wan] || traffic[lan] || {};
    setText("rx-rate", fmt.rate(entry.rx_rate));
    setText("tx-rate", fmt.rate(entry.tx_rate));
    if (entry.rx_bytes) {
      setText("traffic-totals", `${wan}: ${fmt.bytes(entry.rx_bytes)} received · ${fmt.bytes(entry.tx_bytes)} sent`);
    }

    state.history = metrics.history || state.history;
    drawTraffic($("#traffic-chart"), state.history);
  }

  function renderOverview(data) {
    const wan = data.wan;
    setText("wan-value", wan.online ? "Online" : "Offline");
    setText(
      "wan-meta",
      [wan.ipv4 || "No address", wan.latency_ms !== null && wan.latency_ms !== undefined ? `${wan.latency_ms} ms` : null]
        .filter(Boolean)
        .join(" · ")
    );
    pill($("#wan-pill"), wan.online ? "ok" : "bad", wan.online ? "Connected" : "No internet");

    const ap = data.ap;
    setText("ap-clients", ap.clients_online ?? ap.clients ?? 0);
    setText("ap-meta", `Channel ${ap.channel} · ${ap.band}`);
    pill($("#ap-pill"), ap.broadcasting ? "ok" : "warn", ap.broadcasting ? "Broadcasting" : ap.running ? "Starting" : "Not broadcasting");

    const pihole = data.pihole;
    if (pihole && pihole.available) {
      setText("block-percent", pihole.stats.percent ?? 0);
      setText("queries-total", fmt.number(pihole.stats.total));
      setText("queries-blocked", fmt.number(pihole.stats.blocked));
      setText("gravity-domains", fmt.number(pihole.stats.gravity_domains));
      setText("block-meta", `${fmt.number(pihole.stats.blocked)} of ${fmt.number(pihole.stats.total)} queries`);
      if (pihole.verified) pill($("#block-pill"), "ok", "Blocking verified");
      else if (pihole.enabled) pill($("#block-pill"), "warn", "Enabled, unverified");
      else pill($("#block-pill"), "muted", "Paused");
      renderTopBlocked(pihole.top_blocked || []);
      $("#overview-banner").hidden = true;
    } else {
      setText("block-percent", "—");
      setText("queries-total", "—");
      setText("queries-blocked", "—");
      setText("gravity-domains", "—");
      setText("block-meta", (pihole && pihole.reason) || "Pi-hole unavailable");
      pill($("#block-pill"), "bad", "Unavailable");
      showBanner(pihole && pihole.reason ? pihole.reason : "Pi-hole is not responding.");
    }

    const devices = data.devices || {};
    setText("devices-online", devices.online ?? 0);
    const unit = $(".stat__unit", $("#devices-online").closest(".stat"));
    if (unit) unit.textContent = `/ ${devices.total ?? 0}`;
    setText("devices-meta", `${devices.wireless ?? 0} wireless · ${devices.blocked ?? 0} blocked`);
    renderDevices(devices.recent || []);

    if (data.events) renderEvents(data.events);
    if (data.services) renderServices(data.services);
    if (data.health) renderHealth(data.health);

    setText("hero-meta", `${data.lan.configured_address} · ${data.system.model} · up ${data.resources.uptime.human}`);

    const badge = document.getElementById("nav-device-badge");
    if (badge && devices.total) {
      badge.hidden = false;
      badge.textContent = devices.total;
    }
  }

  function renderTopBlocked(items) {
    const wrap = $("#top-blocked");
    if (!wrap) return;
    if (!items.length) {
      wrap.innerHTML = '<div class="text-sm dim">No blocked queries recorded yet.</div>';
      return;
    }
    const max = Math.max(...items.map((item) => item.count), 1);
    wrap.innerHTML = items
      .slice(0, 4)
      .map(
        (item) => `
      <div class="bar-row">
        <div class="bar-row__top">
          <span class="bar-row__label">${escapeHtml(item.domain)}</span>
          <span class="bar-row__value">${fmt.number(item.count)}</span>
        </div>
        <div class="bar"><div class="bar__fill bar__fill--danger" data-width="${((item.count / max) * 100).toFixed(1)}"></div></div>
      </div>`
      )
      .join("");
    applyBarWidths(wrap);
  }

  function applyBarWidths(root) {
    $$("[data-width]", root || document).forEach((bar) => {
      bar.style.setProperty("--value", `${bar.dataset.width}%`);
    });
  }

  function renderDevices(devices) {
    const tbody = $("#device-rows");
    if (!tbody) return;
    if (!devices.length) {
      tbody.innerHTML = `<tr><td colspan="4"><div class="empty">
        <span class="empty__icon"><svg class="icon icon-lg"><use href="#i-devices"/></svg></span>
        <strong>No devices have connected yet</strong>
        <p>Join the wireless network and the device will appear here.</p></div></td></tr>`;
      return;
    }
    tbody.innerHTML = devices
      .map((device) => {
        const status = device.blocked
          ? '<span class="pill pill--bad"><span class="dot"></span>Blocked</span>'
          : device.online
          ? '<span class="pill pill--ok"><span class="dot"></span>Online</span>'
          : '<span class="pill pill--muted"><span class="dot"></span>Idle</span>';
        return `<tr>
          <td><div class="td-main">
            <span class="device-avatar"><svg class="icon icon-sm"><use href="#i-devices"/></svg></span>
            <span class="cell-stack">
              <span class="td-main">${escapeHtml(device.display_name)}</span>
              <span class="td-sub mono">${escapeHtml(device.mac)}</span>
            </span>
          </div></td>
          <td class="mono">${escapeHtml(device.ip || "—")}</td>
          <td class="text-sm">${escapeHtml((device.connection || "unknown").replace(/^./, (c) => c.toUpperCase()))}</td>
          <td class="table__actions">${status}</td>
        </tr>`;
      })
      .join("");
    decorateTables(tbody.closest("table"));
  }

  function renderEvents(events) {
    const list = $("#event-list");
    if (!list) return;
    if (!events.length) {
      list.innerHTML = '<div class="text-sm dim">No events recorded yet.</div>';
      return;
    }
    list.innerHTML = events
      .map(
        (event) => `<div class="event-row">
        <span class="event-dot event-dot--${escapeHtml(event.level)}"></span>
        <div class="event-body">
          <div class="event-text">${escapeHtml(event.message)}</div>
          <div class="event-time">${escapeHtml(fmt.ago(event.at))} · ${escapeHtml(event.category)}</div>
        </div>
      </div>`
      )
      .join("");
  }

  function renderServices(services) {
    const rows = $$("#service-list .list__item");
    const order = ["ap", "dhcp", "pihole", "web"];
    order.forEach((key, index) => {
      const row = rows[index];
      if (!row || !services[key]) return;
      const dot = row.querySelector(".event-dot");
      dot.className = `event-dot ${services[key].running ? "event-dot--success" : "event-dot--error"}`;
      const meta = row.querySelector(".list__meta");
      if (meta) meta.textContent = `${services[key].state} · ${services[key].enabled_state}`;
      const value = row.querySelector(".list__value");
      if (value) {
        value.textContent = services[key].running ? "Running" : "Stopped";
        value.classList.toggle("muted", !services[key].running);
      }
    });
  }

  function renderHealth(health) {
    const failing = (health.checks || []).filter((check) => check.status === "fail");
    if (failing.length) {
      showBanner(`${failing[0].label}: ${failing[0].detail}`, "warn");
    }
  }

  function showBanner(message, kind) {
    const node = $("#overview-banner");
    if (!node) return;
    node.hidden = false;
    node.innerHTML = `<div class="banner banner--${kind || "bad"}">
      <svg class="icon"><use href="#i-alert"/></svg>
      <div class="banner__body">${escapeHtml(message)}</div>
      <a class="btn btn--sm" href="/system">Diagnose</a>
    </div>`;
  }

  async function refreshOverview() {
    try {
      const data = await api("/api/overview");
      renderOverview(data);
      document.getElementById("connection-pill").className = `pill pill--${data.wan.online ? "ok" : "bad"}`;
      setText("connection-text", data.wan.online ? "Internet connected" : "No internet");
    } catch (error) {
      setText("connection-text", "Unreachable");
    }
  }

  async function refreshMetrics() {
    try {
      renderMetrics(await api("/api/metrics"));
    } catch (error) {
      /* the next poll will retry */
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    applyBarWidths(document);
    document.querySelector('[data-legend="rx"]').style.backgroundColor = "#1d6ae5";
    document.querySelector('[data-legend="tx"]').style.backgroundColor = "#0e9f6e";
    drawTraffic($("#traffic-chart"), []);

    poll(refreshOverview, 8000);
    poll(refreshMetrics, 3000);

    window.refreshPage = () => {
      refreshOverview();
      refreshMetrics();
    };

    const diagnostics = $("#diagnostics-btn");
    diagnostics.addEventListener("click", () =>
      withBusy(diagnostics, async () => {
        try {
          const report = await api("/api/health/run", { method: "POST", body: {} });
          const failing = (report.checks || []).filter((check) => check.status === "fail");
          toast({
            title: failing.length ? "Diagnostics finished with issues" : "Diagnostics passed",
            text: failing.length ? `${failing.length} check(s) need attention.` : "All checks passed.",
            kind: failing.length ? "warn" : "ok",
            timeout: 8000,
          });
          if (report.remediated && report.remediated.length) {
            toast({ title: "Automatically repaired", text: report.remediated.join(", "), kind: "info", timeout: 8000 });
          }
          refreshOverview();
        } catch (error) {
          reportError(error, "The diagnostic run failed.");
        }
      })
    );
  });
})();
