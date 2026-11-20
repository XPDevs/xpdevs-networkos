/* Network page: WAN/LAN state, firewall, DHCP form and connectivity probes. */
(function () {
  "use strict";
  const { $, $$, api, fmt, poll, setText, toast, escapeHtml, reportError, withBusy, confirmAction, decorateTables } = window.NWOS;

  function pill(node, tone, text) {
    if (!node) return;
    node.className = `pill pill--${tone}`;
    const label = node.querySelector("span:last-child");
    if (label) label.textContent = text;
    const dot = node.querySelector(".dot");
    if (dot) dot.classList.toggle("dot--pulse", tone === "ok");
  }

  function kv(rows) {
    return rows
      .map(([key, value, mono]) => `<div class="kv__row"><span class="kv__k">${escapeHtml(key)}</span>
        <span class="kv__v${mono ? " mono" : ""}">${value}</span></div>`)
      .join("");
  }

  function render(data) {
    const wan = data.wan;
    const lan = data.lan;
    const firewall = data.firewall;
    const dns = data.dns;
    const dhcp = data.dhcp;

    setText("wan-state", wan.link_up ? (wan.online ? "Online" : "No route") : "Disconnected");
    setText("wan-address", `${wan.interface} · ${wan.ipv4 || "no address"}`);
    pill($("#wan-pill"), wan.online ? "ok" : wan.link_up ? "warn" : "bad",
      wan.online ? `Internet ${wan.latency_ms ? `${wan.latency_ms} ms` : ""}` : wan.link_up ? "Link only" : "Link down");

    setText("lan-state", lan.configured_address);
    setText("lan-meta", `${lan.interface} · ${lan.address_applied ? "address applied" : "address missing"}`);
    pill($("#lan-pill"), lan.address_applied ? "ok" : "bad", lan.address_applied ? "Active" : "Not applied");

    setText("fw-state", firewall.enabled ? "Active" : "Off");
    setText("fw-meta", firewall.enabled ? "nftables · default drop" : "Rules not loaded");
    pill($("#fw-pill"), firewall.enabled ? "ok" : "bad", firewall.enabled ? `${firewall.blocked_macs.length} blocked` : "Disabled");

    setText("dns-state", dns.listening ? "Listening" : "Down");
    setText("dns-meta", dns.listening ? `UDP 53 · ${dns.addresses.join(", ")}` : "Nothing bound to port 53");
    pill($("#dns-pill"), dns.listening ? "ok" : "bad", dns.listening ? "Answering" : "Not answering");

    $("#wan-kv").innerHTML = kv([
      ["Interface", escapeHtml(wan.interface), true],
      ["Link state", escapeHtml(wan.state)],
      ["IPv4 address", escapeHtml(wan.ipv4 || "—"), true],
      ["MAC address", escapeHtml(wan.mac || "—"), true],
      ["Default gateway", escapeHtml(wan.gateway || "—"), true],
      ["Internet", wan.online ? `Reachable (${wan.latency_ms} ms)` : "Unreachable"],
      ["Checked", escapeHtml(fmt.ago(wan.checked_at))],
    ]);

    $("#lan-kv").innerHTML = kv([
      ["Interface", escapeHtml(lan.interface), true],
      ["Address", escapeHtml(lan.configured_address), true],
      ["Uplink interface", escapeHtml(wan.interface), true],
      ["DHCP range", `${escapeHtml(data.config.dhcp_start)} – ${escapeHtml(data.config.dhcp_end)}`, true],
      ["Lease time", escapeHtml(data.config.lease_time), true],
      ["DNS advertised", escapeHtml((data.config.dns_servers || []).join(", ")), true],
      ["DHCP service", dhcp.listening ? "Listening on UDP 67" : "Not listening"],
      ["NAT", firewall.nat ? "Enabled" : "Disabled"],
    ]);

    $("#firewall-kv").innerHTML = kv([
      ["Table", firewall.table_present ? "inet networkos loaded" : "not loaded"],
      ["Input policy", escapeHtml(firewall.input_policy || "—")],
      ["IPv4 forwarding", firewall.forwarding === null ? "unknown" : firewall.forwarding ? "enabled" : "disabled"],
      ["Blocked MAC addresses", String(firewall.blocked_macs.length)],
      ["Dropped packets", fmt.number((firewall.counters || {}).packets)],
    ]);

    if (data.snapshot && data.snapshot.stale) {
      setText("lan-foot", "The privileged snapshot is stale; firewall counters may lag.");
    }
  }

  function renderConnectivity(report) {
    const list = $("#connectivity-list");
    list.innerHTML = report.checks
      .map((check) => {
        const tone = check.ok ? "ok" : "bad";
        return `<div class="list__item">
          <span class="event-dot ${check.ok ? "event-dot--success" : "event-dot--error"}"></span>
          <div class="list__main">
            <div class="list__title">${escapeHtml(check.label)}</div>
            <div class="list__meta">${escapeHtml(check.detail)}</div>
          </div>
          <span class="pill pill--${tone}"><span class="dot"></span>${check.ok ? "Pass" : "Fail"}</span>
        </div>`;
      })
      .join("");
    const summary = document.createElement("div");
    summary.className = "card__foot";
    summary.textContent = `${report.ok ? "All connectivity checks passed" : "Some checks failed"} · ${fmt.ago(report.checked_at)}`;
    list.parentElement.appendChild(summary);
  }

  async function refresh() {
    try {
      render(await api("/api/network"));
    } catch (error) {
      reportError(error, "Could not read the network state.");
    }
  }

  function clearErrors() {
    $$(".field.has-error").forEach((field) => field.classList.remove("has-error"));
  }

  function fieldError(code, message) {
    const node = $(`[data-error-for="${code}"]`);
    if (node) {
      node.textContent = message;
      node.closest(".field").classList.add("has-error");
    }
    toast({ title: "Not applied", text: message, kind: "bad", timeout: 8000 });
  }

  document.addEventListener("DOMContentLoaded", () => {
    poll(refresh, 8000);
    window.refreshPage = refresh;

    api("/api/network/test", { method: "POST", body: {} }).then(renderConnectivity).catch(() => {
      $("#connectivity-list").innerHTML =
        '<div class="list__item"><div class="list__main"><div class="list__title">The connectivity test could not run</div><div class="list__meta">Use “Run test” to retry.</div></div></div>';
    });

    $("#run-test").addEventListener("click", () =>
      withBusy($("#run-test"), async () => {
        try {
          const report = await api("/api/network/test", { method: "POST", body: {} });
          renderConnectivity(report);
          toast({
            title: report.ok ? "Connectivity verified" : "Connectivity issues found",
            text: report.ok ? "Uplink, gateway, internet and DNS all responded." : "See the failing checks below.",
            kind: report.ok ? "ok" : "warn",
            timeout: 8000,
          });
        } catch (error) {
          reportError(error);
        }
      })
    );

    $("#lan-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      clearErrors();
      const payload = {
        lan_address: $("#lan_address").value.trim(),
        lan_prefix: Number($("#lan_prefix").value),
        dhcp_start: $("#dhcp_start").value.trim(),
        dhcp_end: $("#dhcp_end").value.trim(),
        lease_time: $("#lease_time").value.trim(),
        domain: $("#domain").value.trim(),
        dns_servers: $("#dns_servers").value.trim(),
      };
      const answer = await confirmAction({
        title: "Apply LAN settings?",
        message: `The access point moves to ${payload.lan_address}/${payload.lan_prefix} and DHCP restarts. ` +
          "If you are connected over Wi-Fi you will be disconnected and must rejoin.",
        confirmLabel: "Apply and restart",
        danger: true,
        phrase: "CONFIRM-LAN",
      });
      if (!answer || !answer.confirmed) return;
      withBusy($("#save-lan"), async () => {
        try {
          await api("/api/network/lan", { method: "POST", body: payload });
          toast({ title: "Network settings applied", text: `The access point now uses ${payload.lan_address}.`, kind: "ok", timeout: 9000 });
          refresh();
        } catch (error) {
          if (error.code) fieldError(error.code, error.message);
          else reportError(error);
        }
      });
    });

    $("#firewall-toggle").addEventListener("change", async (event) => {
      const enabled = event.target.checked;
      if (!enabled) {
        const answer = await confirmAction({
          title: "Disable the firewall?",
          message: "Wireless clients would no longer be isolated from the uplink by NetworkOS. NAT stays as configured.",
          confirmLabel: "Disable firewall",
          danger: true,
        });
        if (!answer || !answer.confirmed) {
          event.target.checked = true;
          return;
        }
      }
      try {
        await api("/api/network/firewall", { method: "POST", body: { enabled } });
        toast({ title: enabled ? "Firewall enabled" : "Firewall disabled", kind: enabled ? "ok" : "warn" });
        refresh();
      } catch (error) {
        event.target.checked = !enabled;
        reportError(error);
      }
    });

    $("#nat-toggle").addEventListener("change", async (event) => {
      const enabled = event.target.checked;
      try {
        await api("/api/network/nat", { method: "POST", body: { enabled } });
        toast({ title: enabled ? "NAT enabled" : "NAT disabled", text: enabled ? "Wireless clients can reach the internet." : "Wireless clients lose internet access.", kind: enabled ? "ok" : "warn" });
        refresh();
      } catch (error) {
        event.target.checked = !enabled;
        reportError(error);
      }
    });
  });
})();
