(function () {
  "use strict";
  // Flota dashboard tab: a live map of your machines around the Hermes host, their details, and
  // wake / sleep / power off / add. Plain JS (no build step) against window.__HERMES_PLUGIN_SDK__.
  //
  // Demo mode: add ?flota_demo=1 to the URL (or ?flota_demo=N for N fake machines, 0..24) to render
  // made-up machines instead of calling the API. Actions in demo mode are simulated locally and
  // never touch a real machine or the config.
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const h = React.createElement;
  const { useState, useEffect, useCallback, useRef } = SDK.hooks;
  const C = SDK.components;
  const cn = (SDK.utils && SDK.utils.cn) || function () {
    return Array.prototype.filter.call(arguments, Boolean).join(" ");
  };
  const BASE = "/api/plugins/flota";
  const POLL_MS = 5000;
  const WAKE_METHODS = ["none", "wol", "wol_via", "command"];
  const SLEEP_METHODS = ["", "suspend", "hibernate", "poweroff", "none"];
  const SETUP_TEMPLATE = "hermes flota setup <name> --host <ip> --user <user>";

  // --- demo data ------------------------------------------------------------------------------

  function demoCount() {
    const text = (window.location.search || "") + "&" + (window.location.hash || "");
    const match = /[?&#]flota_demo(?:=(\d+))?/.exec(text);
    if (!match) return -1;
    if (match[1] === undefined) return 5;
    const n = parseInt(match[1], 10);
    return n === 1 ? 5 : Math.max(0, Math.min(24, n));  // "=1" reads as "on"
  }
  const DEMO_N = demoCount();
  const DEMO = DEMO_N >= 0;

  const DEMO_BASE = [
    { name: "torre", description: "GPU desktop (RTX 4090): heavy builds, local models", host: "192.0.2.20",
      os: "linux", wake: "wol", up: true, busy: 2, sleep_after: 600 },
    { name: "portatil", description: "ThinkPad laptop, usually asleep in the drawer", host: "192.0.2.31",
      os: "linux", wake: "wol", up: false, busy: 0, sleep_after: 300 },
    { name: "servidor", description: "Always-on home server (Docker host)", host: "192.0.2.10",
      os: "linux", wake: "none", up: true, busy: 0, sleep_after: 0 },
    { name: "nas", description: "Storage server; backups and media", host: "192.0.2.40",
      os: "linux", wake: "wol_via", up: false, waking: true, busy: 0, sleep_after: 1800 },
    { name: "mac-mini", description: "Mac mini for iOS builds", host: "192.0.2.52",
      os: "macos", wake: "wol", up: false, busy: 0, sleep_after: 900 },
  ];

  function demoMachine(m) {
    return Object.assign({ port: 22, user: "alice", key: "", profile: "flota-" + m.name, profile_exists: true,
      setup_command: null, mac: "aa:bb:cc:dd:ee:" + (10 + m.name.length), broadcast: "", via: null,
      sleep: m.wake === "none" ? "none" : "suspend", custom_sleep: false, fallback: null, waking: false,
      wake_error: null }, m);
  }

  const demo = { machines: [], timers: {} };
  (function seedDemo() {
    if (!DEMO) return;
    const extra = ["estudio", "gpu-2", "raspi", "build-1", "build-2", "media", "lab", "vm-host", "backup",
      "render", "arm-box", "edge", "lab-2", "nuc", "sbc", "spare", "old-pc", "test", "ci", "vault"];
    for (let i = 0; i < DEMO_N; i++) {
      if (i < DEMO_BASE.length) { demo.machines.push(demoMachine(DEMO_BASE[i])); continue; }
      const name = extra[(i - DEMO_BASE.length) % extra.length];
      demo.machines.push(demoMachine({ name: name, host: "192.0.2." + (60 + i), os: "linux",
        wake: "wol", up: i % 3 === 0, busy: i % 5 === 0 ? 1 : 0, sleep_after: 600,
        description: /raspi|sbc|arm/.test(name) ? "Raspberry Pi server" : "Spare desktop" }));
    }
  })();

  function demoFind(name) {
    return demo.machines.filter(function (m) { return m.name === name; })[0];
  }

  function demoApi(path, options) {
    const method = (options && options.method) || "GET";
    const delay = function (value) {
      return new Promise(function (resolve) { setTimeout(function () { resolve(value); }, 350); });
    };
    const fail = function (status, message) {
      const err = new Error(status + ": " + JSON.stringify({ detail: message }));
      err.status = status; err.body = JSON.stringify({ detail: message });
      return Promise.reject(err);
    };
    if (path === "/status") {
      return delay({ machines: demo.machines.map(function (m) { return Object.assign({}, m); }), problems: [],
        allow_power_off: true, default: null, busy_known: true, checked_at: Date.now() / 1000,
        hub: { name: "atalaya", os: "linux" } });
    }
    const parts = path.split("?")[0].split("/");
    const name = decodeURIComponent(parts[2] || "");
    const force = path.indexOf("force=1") >= 0 || /"force":\s*true/.test((options && options.body) || "");
    if (path === "/machines" && method === "POST") {
      const body = JSON.parse(options.body || "{}");
      const existing = demoFind(body.name);
      const entry = demoMachine(Object.assign({ os: "linux", wake: "none", up: false, busy: 0, description: "" },
        existing || {}, body, { port: body.port || 22, sleep_after: 600 }));
      if (existing) demo.machines[demo.machines.indexOf(existing)] = entry;
      else demo.machines.push(entry);
      return delay({ ok: true, machine: body.name, profile: "flota-" + body.name, profile_exists: true,
        setup_command: null });
    }
    const m = demoFind(name);
    if (!m) return fail(404, "no machine named '" + name + "'");
    if (method === "DELETE") {
      demo.machines.splice(demo.machines.indexOf(m), 1);
      return delay({ ok: true, machine: name, profile_kept: "(demo: nothing was changed)" });
    }
    if (parts[1] === "wake") {
      m.waking = true;
      clearTimeout(demo.timers[name]);
      demo.timers[name] = setTimeout(function () { m.waking = false; m.up = true; }, 6000);
      return delay({ ok: true, state: "waking", machine: name });
    }
    if (parts[1] === "sleep" || parts[1] === "off") {
      if (m.busy && !force) return fail(409, name + " is busy with " + m.busy + " subagent(s); add --force to sleep it anyway");
      m.up = false; m.busy = 0;
      return delay({ ok: true, result: name + ": " + (parts[1] === "off" ? "powered off" : "suspended") + " (demo)" });
    }
    return fail(400, "unknown demo request " + path);
  }

  // The host's fetchJSON adds the session header (loopback) or cookie (gated OAuth) and throws on
  // non-2xx with the server's `detail` as the message.
  function api(path, options) {
    if (DEMO) return demoApi(path, options);
    return SDK.fetchJSON(BASE + path, options);
  }
  function post(path, body, method) {
    return api(path, {
      method: method || "POST",
      headers: { "Content-Type": "application/json" },
      // Always a JSON body on POST: the API refuses a POST that is not application/json.
      body: body === undefined ? (method && method !== "POST" ? undefined : "{}") : JSON.stringify(body),
    });
  }

  // { message, problems } from a thrown fetchJSON error.
  function explain(err) {
    let detail = null;
    const raw = err && (err.body || (err.details && err.details.body));
    if (typeof raw === "string") {
      try { detail = JSON.parse(raw).detail; } catch (_) { /* not JSON */ }
    }
    if (detail && typeof detail === "object" && !Array.isArray(detail)) {
      return { message: detail.message || "Request failed", problems: detail.problems || [] };
    }
    if (Array.isArray(detail)) {
      return { message: detail.map(function (d) { return d && d.msg; }).filter(Boolean).join("; "), problems: [] };
    }
    if (typeof detail === "string") return { message: detail, problems: [] };
    return { message: (err && err.message) || String(err), problems: [] };
  }

  // HTTP status of a thrown fetchJSON error ("409: <body>").
  function httpStatus(err) {
    if (err && err.status) return err.status;
    const match = /^(\d{3}):/.exec((err && err.message) || "");
    return match ? parseInt(match[1], 10) : 0;
  }

  function duration(seconds) {
    if (!seconds) return "never";
    if (seconds % 3600 === 0) return seconds / 3600 + "h";
    if (seconds % 60 === 0) return seconds / 60 + "m";
    return seconds + "s";
  }

  function ago(ms) {
    const s = Math.max(0, Math.round(ms / 1000));
    if (s < 2) return "just now";
    if (s < 60) return s + "s ago";
    return Math.floor(s / 60) + "m ago";
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve, reject) {
      const area = document.createElement("textarea");
      area.value = text; area.setAttribute("readonly", ""); area.style.position = "fixed"; area.style.opacity = "0";
      document.body.appendChild(area); area.select();
      try { document.execCommand("copy") ? resolve() : reject(new Error("copy failed")); }
      catch (e) { reject(e); } finally { document.body.removeChild(area); }
    });
  }

  const WAKE_LABELS = { none: "none", wol: "Wake-on-LAN", wol_via: "Wake-on-LAN via relay", command: "command" };

  // up / waking / down; down reads "asleep" when Flota can wake it, "offline" when it cannot.
  function stateOf(m) {
    if (m.up) return { key: "up", label: "up" };
    if (m.waking) return { key: "waking", label: "waking…" };
    return { key: "down", label: m.wake === "none" ? "offline" : "asleep" };
  }

  // desktop / laptop / server, from the description, name and OS.
  function kindOf(m) {
    const text = ((m.description || "") + " " + (m.name || "")).toLowerCase();
    if (/laptop|port[aá]til|notebook|macbook|thinkpad|ultrabook/.test(text)) return "laptop";
    if (/server|servidor|\bnas\b|docker|rack|homelab|always[- ]on|raspberry|\bpi\b|raspi|proxmox|vm[- ]host/.test(text)) {
      return "server";
    }
    if (/desktop|tower|torre|workstation|\bpc\b|gpu|mini/.test(text)) return "desktop";
    const os = (m.os || "").toLowerCase();
    if (/mac|darwin/.test(os)) return "laptop";
    return "desktop";
  }

  // 24x24 stroke glyphs (lucide-style), drawn centred on (0, 0).
  function Glyph({ kind, size }) {
    const s = size || 24;
    const attrs = { className: "flota-glyph", transform: "translate(" + (-s / 2) + "," + (-s / 2) + ") scale(" + (s / 24) + ")" };
    if (kind === "server") {
      return h("g", attrs,
        h("rect", { x: 3, y: 3, width: 18, height: 7.5, rx: 2 }),
        h("rect", { x: 3, y: 13.5, width: 18, height: 7.5, rx: 2 }),
        h("path", { d: "M7 6.75h.01M7 17.25h.01M11 6.75h6M11 17.25h6" }));
    }
    if (kind === "laptop") {
      return h("g", attrs,
        h("path", { d: "M5 16V7.5A1.5 1.5 0 0 1 6.5 6h11A1.5 1.5 0 0 1 19 7.5V16" }),
        h("path", { d: "M2.5 16h19l-1 2.2a1.5 1.5 0 0 1-1.4.8H4.9a1.5 1.5 0 0 1-1.4-.8z" }));
    }
    if (kind === "hermes") {  // ☿, the planetary sign of Mercury / Hermes
      return h("g", attrs,
        h("path", { d: "M7.5 2.5a4.5 4.5 0 0 0 9 0" }),
        h("circle", { cx: 12, cy: 10.5, r: 4.5 }),
        h("path", { d: "M12 15v7M8.5 18.5h7" }));
    }
    return h("g", attrs,
      h("rect", { x: 2.5, y: 3.5, width: 19, height: 13, rx: 2 }),
      h("path", { d: "M8 21h8M12 16.5V21" }));
  }

  function Dot({ state }) {
    return h("span", { className: cn("flota-dot", "flota-dot-" + state), "aria-hidden": true });
  }

  function Field({ label, children }) {
    return h("div", { className: "flota-field" },
      h("span", { className: "flota-field-label" }, label),
      h("span", { className: "flota-field-value" }, children));
  }

  // --- map ------------------------------------------------------------------------------------

  // Positions on one ellipse (<= 12 machines) or two (> 12). Even counts are rotated half a step so
  // the ring is balanced left/right; one machine sits to the right of the hub.
  function ring(count, rx, ry, cx, cy, offset) {
    const out = [];
    const step = (2 * Math.PI) / count;
    let start = -Math.PI / 2 + (count % 2 === 0 ? step / 2 : 0) + (offset || 0);
    if (count === 1) start = 0;
    for (let i = 0; i < count; i++) {
      const a = start + i * step;
      out.push({ x: cx + rx * Math.cos(a), y: cy + ry * Math.sin(a) });
    }
    return out;
  }

  function layout(n, wide) {
    const two = n > 12;
    const W = wide ? 820 : 480;
    let H, rings, pos;
    if (wide) H = two ? 670 : n > 6 ? 540 : n > 2 ? 460 : 330;
    else H = two ? 800 : n > 6 ? 700 : n > 2 ? 600 : 520;
    const cx = W / 2, cy = H / 2 - 8;
    if (!two) {
      const r = wide ? { rx: 300, ry: n > 6 ? 196 : n > 2 ? 156 : 105 } : { rx: 170, ry: n > 6 ? 280 : 225 };
      rings = [r];
      pos = n ? ring(n, r.rx, r.ry, cx, cy) : [];
    } else {
      // Inner and outer rings interleave so outer links pass between inner nodes, not through them.
      const inner = Math.min(Math.floor(n / 2), 9), outer = n - inner;
      const ri = wide ? { rx: 185, ry: 138 } : { rx: 110, ry: 180 };
      const ro = wide ? { rx: 345, ry: 262 } : { rx: 195, ry: 330 };
      rings = [ri, ro];
      const innerPos = ring(inner, ri.rx, ri.ry, cx, cy);
      const innerStart = -Math.PI / 2 + (inner % 2 === 0 ? Math.PI / inner : 0);
      const step = (2 * Math.PI) / outer;
      // Rotate the outer ring so its spokes keep as far as possible from the inner nodes.
      let best = 0, bestGap = -1;
      for (let k = 0; k < 48; k++) {
        const off = (k / 48) * step;
        let gap = Infinity;
        for (let i = 0; i < outer; i++) {
          for (let j = 0; j < inner; j++) {
            let d = Math.abs(((-Math.PI / 2 + off + i * step) - (innerStart + j * 2 * Math.PI / inner)) % (2 * Math.PI));
            d = Math.min(d, 2 * Math.PI - d);
            if (d < gap) gap = d;
          }
        }
        if (gap > bestGap + 1e-6) { bestGap = gap; best = off; }
      }
      pos = innerPos.concat(Array.from({ length: outer }, function (_, i) {
        const a = -Math.PI / 2 + best + i * step;
        return { x: cx + ro.rx * Math.cos(a), y: cy + ro.ry * Math.sin(a) };
      }));
    }
    return { W: W, H: H, cx: cx, cy: cy, rings: rings, pos: pos, compact: two };
  }

  function short(text, max) {
    return text.length > max ? text.slice(0, max - 1) + "…" : text;
  }

  function statusText(m) {
    const st = stateOf(m);
    if (st.key === "up" && m.busy) return m.busy + " subagent" + (m.busy === 1 ? "" : "s");
    return st.label;
  }

  function MapNode({ m, p, r, compact, selected, hovered, fresh, onSelect, onHover }) {
    const st = stateOf(m);
    const busy = m.up && m.busy > 0;
    function activate() { onSelect(m.name); }
    return h("g", { transform: "translate(" + p.x.toFixed(1) + "," + p.y.toFixed(1) + ")" },
      h("g", {
        className: cn("flota-node", "flota-node-" + st.key, busy && "flota-node-busy",
          selected && "flota-node-selected", hovered && "flota-node-hover"),
        role: "button", tabIndex: 0,
        "aria-label": m.name + ": " + statusText(m) + ". Show its details.",
        onClick: function (e) { e.stopPropagation(); activate(); },
        onKeyDown: function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activate(); } },
        onMouseEnter: function () { onHover(m.name); }, onMouseLeave: function () { onHover(null); },
        onFocus: function () { onHover(m.name); }, onBlur: function () { onHover(null); },
      },
      fresh ? h("circle", { className: "flota-ripple", r: r }) : null,
      st.key === "waking" ? h("circle", { className: "flota-pulse", r: r + 2 }) : null,
      busy ? h("circle", { className: "flota-busy-ring", r: r + 7 }) : null,
      h("circle", { className: "flota-select-ring", r: r + (busy ? 12 : 6) }),
      h("circle", { className: "flota-node-disc", r: r }),
      h(Glyph, { kind: kindOf(m), size: compact ? 18 : 22 }),
      busy ? h("g", { className: "flota-busy-badge", transform: "translate(" + (r * 0.74) + "," + (-r * 0.74) + ")" },
        h("circle", { r: 9.5 }),
        h("text", { y: 0.5, dominantBaseline: "middle", textAnchor: "middle" }, String(m.busy))) : null,
      h("text", { className: "flota-node-name", y: r + 18, textAnchor: "middle" }, short(m.name, compact ? 11 : 16)),
      h("text", { className: "flota-node-status", y: r + 33, textAnchor: "middle" }, statusText(m)),
      h("circle", { className: "flota-hit", r: r + 14 })));
  }

  function linkPath(L, p, r, hubR) {
    const dx = p.x - L.cx, dy = p.y - L.cy;
    const d = Math.sqrt(dx * dx + dy * dy) || 1;
    const ux = dx / d, uy = dy / d;
    const a = { x: L.cx + ux * (hubR + 6), y: L.cy + uy * (hubR + 6) };
    const b = { x: p.x - ux * (r + 6), y: p.y - uy * (r + 6) };
    return "M" + a.x.toFixed(1) + " " + a.y.toFixed(1) + "L" + b.x.toFixed(1) + " " + b.y.toFixed(1);
  }

  function Tooltip({ m, p, L, r }) {
    const st = stateOf(m);
    const below = p.y < L.H * 0.5;
    // Anchor under the node's status line, or just above its busy ring, in map coordinates.
    const left = (p.x / L.W) * 100, top = ((below ? p.y + r + 44 : p.y - r - 16) / L.H) * 100;
    const side = p.x > L.W * 0.7 ? "flota-tip-left" : p.x < L.W * 0.3 ? "flota-tip-right" : "";
    return h("div", { className: cn("flota-tip", below ? "flota-tip-below" : "flota-tip-above", side), role: "tooltip",
      style: { left: left + "%", top: top + "%" } },
      h("div", { className: "flota-tip-title" }, h(Dot, { state: st.key }), h("strong", null, m.name),
        h("span", { className: "flota-tip-state" }, statusText(m))),
      m.description ? h("p", { className: "flota-tip-desc" }, m.description) : null,
      h("div", { className: "flota-fields" },
        h(Field, { label: "Host" }, (m.user ? m.user + "@" : "") + m.host + (m.port && m.port !== 22 ? ":" + m.port : "")),
        h(Field, { label: "OS" }, m.os || "linux"),
        h(Field, { label: "Wake" }, WAKE_LABELS[m.wake] || m.wake),
        h(Field, { label: "Sleeps after" }, m.sleep_after ? duration(m.sleep_after) + " idle" : "never")),
      h("p", { className: "flota-tip-hint" }, "Click for actions"));
  }

  function useWide(ref) {
    const [wide, setWide] = useState(true);
    useEffect(function () {
      const el = ref.current;
      if (!el || typeof ResizeObserver === "undefined") return undefined;
      const ro = new ResizeObserver(function (entries) {
        const w = entries[0].contentRect.width;
        setWide(w === 0 || w >= 620);
      });
      ro.observe(el);
      return function () { ro.disconnect(); };
    }, [ref]);
    return wide;
  }

  function FleetMap({ machines, hub, selected, hovered, fresh, onSelect, onHover }) {
    const box = useRef(null);
    const wide = useWide(box);
    const L = layout(machines.length, wide);
    const r = L.compact ? 21 : 26;
    const hubR = L.compact ? 34 : 40;
    const up = machines.filter(function (m) { return m.up; }).length;
    const hubName = hub && hub.name ? "Hermes · " + short(String(hub.name), 22) : "Hermes";
    const hoverM = machines.filter(function (m) { return m.name === hovered; })[0];
    const hoverP = hoverM ? L.pos[machines.indexOf(hoverM)] : null;
    return h("div", { className: "flota-map", ref: box },
      h("svg", { className: "flota-map-svg", viewBox: "0 0 " + L.W + " " + L.H, role: "group",
        "aria-label": "Network map: Hermes and " + machines.length + " machine(s), " + up + " up",
        preserveAspectRatio: "xMidYMid meet", onClick: function () { onSelect(null); } },
        h("defs", null,
          h("radialGradient", { id: "flota-hub-glow" },
            h("stop", { offset: "0%", className: "flota-hub-glow-stop", stopOpacity: 0.38 }),
            h("stop", { offset: "100%", className: "flota-hub-glow-stop", stopOpacity: 0 }))),
        L.rings.map(function (o, i) {
          return h("ellipse", { key: "orbit" + i, className: "flota-orbit", cx: L.cx, cy: L.cy, rx: o.rx, ry: o.ry });
        }),
        machines.map(function (m, i) {
          const st = stateOf(m);
          const d = linkPath(L, L.pos[i], r, hubR);
          const hot = selected === m.name || hovered === m.name;
          return h("g", { key: "link-" + m.name, className: cn("flota-link", "flota-link-" + st.key, hot && "flota-link-hot") },
            h("path", { className: "flota-link-base", d: d }),
            st.key !== "down" ? h("path", { className: "flota-link-flow", d: d }) : null);
        }),
        h("g", { className: "flota-hub", transform: "translate(" + L.cx + "," + L.cy + ")" },
          h("circle", { className: "flota-hub-halo", r: hubR * 2.1, fill: "url(#flota-hub-glow)" }),
          h("circle", { className: "flota-hub-ring", r: hubR + 8 }),
          h("circle", { className: "flota-hub-disc", r: hubR }),
          h(Glyph, { kind: "hermes", size: L.compact ? 28 : 34 }),
          h("text", { className: "flota-hub-name", y: hubR + 22, textAnchor: "middle" }, hubName),
          h("text", { className: "flota-hub-sub", y: hubR + 38, textAnchor: "middle" },
            machines.length ? up + " of " + machines.length + " up" : "no machines yet")),
        machines.map(function (m, i) {
          return h(MapNode, { key: m.name, m: m, p: L.pos[i], r: r, compact: L.compact, selected: selected === m.name,
            hovered: hovered === m.name, fresh: !!fresh[m.name], onSelect: onSelect, onHover: onHover });
        })),
      hoverM && hoverP ? h(Tooltip, { m: hoverM, p: hoverP, L: L, r: r }) : null);
  }

  function Legend({ updated, now, busyKnown }) {
    function item(cls, label) {
      return h("span", { className: "flota-legend-item" }, h("span", { className: cn("flota-swatch", cls), "aria-hidden": true }), label);
    }
    return h("div", { className: "flota-legend" },
      h("div", { className: "flota-legend-items" },
        item("flota-swatch-up", "up"),
        item("flota-swatch-down", "asleep / off"),
        item("flota-swatch-waking", "waking"),
        busyKnown === false ? null : item("flota-swatch-busy", "busy (subagents)")),
      h("span", { className: "flota-updated", title: "Status refreshes every 5 s (SSH port probe)" },
        h("span", { className: "flota-live", "aria-hidden": true }),
        updated ? "updated " + ago(now - updated) : "checking…"));
  }

  function CopyCommand({ text }) {
    const [copied, setCopied] = useState(false);
    return h("div", { className: "flota-copy" },
      h("code", null, text),
      h(C.Button, { size: "sm", outlined: true, type: "button",
        onClick: function () {
          copyText(text).then(function () { setCopied(true); setTimeout(function () { setCopied(false); }, 1800); })
            .catch(function () { setCopied(false); });
        } }, copied ? "Copied ✓" : "Copy"));
  }

  function EmptyState({ onAdd }) {
    function step(n, title, body) {
      return h("li", { className: "flota-step" },
        h("span", { className: "flota-step-n", "aria-hidden": true }, n),
        h("div", null, h("strong", null, title), body));
    }
    return h(C.Card, { className: "flota-card flota-empty" },
      h(C.CardHeader, null, h(C.CardTitle, null, "Add your first machine")),
      h(C.CardContent, null,
        h("ol", { className: "flota-steps" },
          step("1", "Make it reachable",
            h("p", null, "Check that this computer can ", h("code", null, "ssh <user>@<ip>"),
              " into it. For waking from sleep, turn on Wake-on-LAN in its BIOS / network settings.")),
          step("2", "Register it with Flota",
            h("div", null, h("p", null, "Run this in a terminal (it also creates the SSH profile subagents use):"),
              h(CopyCommand, { text: SETUP_TEMPLATE }))),
          step("3", "Watch it join the map",
            h("p", null, "It appears around Hermes within seconds. Hermes wakes it when a subagent needs it and puts it back to sleep when idle."))),
        h("div", { className: "flota-actions" },
          h(C.Button, { size: "sm", onClick: onAdd }, "Or add it here"))));
  }

  // --- cards ----------------------------------------------------------------------------------

  function MachineCard({ m, allowOff, onAction, pending, selected, onHover }) {
    const st = stateOf(m);
    const busy = !!pending;
    const sleepText = m.custom_sleep ? "custom command" : (m.sleep === "none" ? "suspend (manual only)" : m.sleep);
    // Per machine since the API sends can_power_off; older APIs only had the panel switch.
    const canOff = typeof m.can_power_off === "boolean" ? m.can_power_off : allowOff && m.wake !== "none";
    const offReason = canOff ? null : (m.power_off_reason || (allowOff
      ? m.name + " has no wake method, so Flota would not be able to turn it back on; it can only be suspended."
      : "Powering off is disabled. Turn on \"Allow power off\" in Plugins > Flota."));
    const offButton = h(C.Button, {
      size: "sm", destructive: true, outlined: true, disabled: !canOff || busy || !m.up,
      onClick: function () {
        if (window.confirm("Power off " + m.name + "? It needs Wake-on-LAN from power-off to come back.")) {
          onAction("off", m.name);
        }
      },
    }, "Power off");
    return h("div", { id: "flota-card-" + m.name, className: cn("flota-card-wrap", "flota-card-" + st.key, selected && "flota-card-selected"),
      onMouseEnter: function () { onHover(m.name); }, onMouseLeave: function () { onHover(null); } },
      h(C.Card, { className: "flota-card" },
        h(C.CardHeader, { className: "flota-card-header" },
          h("div", { className: "flota-card-title-row" },
            h("svg", { className: cn("flota-card-icon", "flota-card-icon-" + st.key), viewBox: "-14 -14 28 28", "aria-hidden": true },
              h(Glyph, { kind: kindOf(m), size: 20 })),
            h(C.CardTitle, { className: "flota-card-title" }, m.name),
            h(C.Badge, { tone: st.key === "up" ? "success" : st.key === "waking" ? "warning" : "outline" }, st.label),
            m.busy ? h(C.Badge, { tone: "warning", title: "Subagents or routes active in the gateway" },
              "busy (" + m.busy + ")") : null),
          m.description ? h("p", { className: "flota-desc" }, m.description) : null),
        h(C.CardContent, { className: "flota-card-body" },
          h("div", { className: "flota-fields" },
            h(Field, { label: "Host" }, (m.user ? m.user + "@" : "") + m.host + (m.port !== 22 ? ":" + m.port : "")),
            h(Field, { label: "OS" }, m.os),
            h(Field, { label: "Wake" }, WAKE_LABELS[m.wake] || m.wake),
            h(Field, { label: "Sleep" }, sleepText),
            h(Field, { label: "Auto-sleep after" }, m.sleep_after ? duration(m.sleep_after) + " idle" : "never"),
            h(Field, { label: "Profile" }, m.profile + (m.profile_exists ? "" : " (missing)"))),
          m.wake_error ? h("p", { className: "flota-error", role: "alert" }, "Wake failed: " + m.wake_error) : null,
          !m.profile_exists && m.setup_command ? h("p", { className: "flota-hint" },
            "Subagents need its SSH profile. Run in a terminal: ", h("code", null, m.setup_command)) : null,
          h("div", { className: "flota-actions" },
            h(C.Button, { size: "sm", disabled: busy || m.up || m.waking || m.wake === "none",
              title: m.wake === "none" ? "This machine has no wake method" : undefined,
              onClick: function () { onAction("wake", m.name); } }, m.waking ? "Waking…" : "Wake"),
            h(C.Button, { size: "sm", outlined: true, disabled: busy || !m.up,
              onClick: function () { onAction("sleep", m.name); } }, pending === "sleep" ? "Sleeping…" : "Sleep"),
            canOff ? offButton : h("span", { className: "flota-tooltip-wrap", title: offReason }, offButton),
            h(C.Button, { size: "sm", ghost: true, disabled: busy, className: "flota-remove",
              onClick: function () {
                if (window.confirm("Remove " + m.name + " from Flota? Its Hermes profile is kept.")) {
                  onAction("delete", m.name);
                }
              } }, "Remove")))));
  }

  const EMPTY_FORM = { name: "", host: "", user: "", key: "", port: "", mac: "", broadcast: "",
    wake: "none", sleep: "", sleep_after: "", description: "" };

  function TextInput({ id, label, value, onChange, placeholder, required }) {
    return h("div", { className: "flota-form-field" },
      h(C.Label, { htmlFor: id }, label + (required ? " *" : "")),
      h(C.Input, { id: id, value: value, placeholder: placeholder, required: !!required,
        onChange: function (e) { onChange(e.target.value); } }));
  }

  function Choice({ id, label, value, options, labels, onChange }) {
    return h("div", { className: "flota-form-field" },
      h(C.Label, { htmlFor: id }, label),
      h(C.Select, { id: id, value: value, onValueChange: function (v) { onChange(v == null ? "" : v); } },
        options.map(function (o) { return h(C.SelectOption, { key: o || "default", value: o }, labels[o] || o); })));
  }

  function AddMachineForm({ onSaved, onCancel }) {
    const [form, setForm] = useState(EMPTY_FORM);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState(null);
    const [saved, setSaved] = useState(null);
    function set(key) { return function (v) { setForm(Object.assign({}, form, { [key]: v })); }; }

    function submit(e) {
      e.preventDefault();
      setSaving(true); setError(null); setSaved(null);
      const body = {};
      Object.keys(form).forEach(function (k) {
        const v = String(form[k]).trim();
        if (v) body[k] = k === "port" && /^\d+$/.test(v) ? parseInt(v, 10) : v;
      });
      post("/machines", body).then(function (res) {
        setSaved(res); setForm(EMPTY_FORM); onSaved();
      }).catch(function (err) { setError(explain(err)); }).finally(function () { setSaving(false); });
    }

    const wol = form.wake === "wol" || form.wake === "wol_via";
    return h(C.Card, { className: "flota-card flota-form-card" },
      h(C.CardHeader, null, h(C.CardTitle, null, "Add or update a machine")),
      h(C.CardContent, null,
        h("form", { className: "flota-form", onSubmit: submit },
          h("div", { className: "flota-form-grid" },
            h(TextInput, { id: "flota-name", label: "Name", value: form.name, onChange: set("name"),
              placeholder: "torre", required: true }),
            h(TextInput, { id: "flota-host", label: "Host", value: form.host, onChange: set("host"),
              placeholder: "192.0.2.20", required: true }),
            h(TextInput, { id: "flota-user", label: "SSH user", value: form.user, onChange: set("user") }),
            h(TextInput, { id: "flota-key", label: "SSH key path", value: form.key, onChange: set("key"),
              placeholder: "optional: private key path" }),
            h(TextInput, { id: "flota-port", label: "SSH port", value: form.port, onChange: set("port"),
              placeholder: "22" }),
            h(TextInput, { id: "flota-desc", label: "Description (the agent reads it)", value: form.description,
              onChange: set("description"), placeholder: "GPU box; heavy builds" }),
            h(Choice, { id: "flota-wake", label: "Wake", value: form.wake, options: WAKE_METHODS,
              labels: WAKE_LABELS, onChange: set("wake") }),
            h(TextInput, { id: "flota-mac", label: "MAC" + (wol ? " *" : ""), value: form.mac,
              onChange: set("mac"), placeholder: "aa:bb:cc:dd:ee:ff" }),
            h(TextInput, { id: "flota-bcast", label: "Broadcast", value: form.broadcast, onChange: set("broadcast"),
              placeholder: "255.255.255.255" }),
            h(Choice, { id: "flota-sleep", label: "Sleep", value: form.sleep, options: SLEEP_METHODS,
              labels: { "": "panel default", none: "never auto-sleep" }, onChange: set("sleep") }),
            h(TextInput, { id: "flota-after", label: "Sleep after", value: form.sleep_after,
              onChange: set("sleep_after"), placeholder: "10m" })),
          form.wake === "wol_via" || form.wake === "command" ? h("p", { className: "flota-hint" },
            "This wake method needs '" + (form.wake === "wol_via" ? "via" : "wake_command") +
            "', which is set in config.yaml (plugins.entries.flota.settings.machines), not here.") : null,
          error ? h("div", { className: "flota-error", role: "alert" },
            h("strong", null, error.message),
            error.problems.length ? h("ul", null, error.problems.map(function (p, i) {
              return h("li", { key: i }, p);
            })) : null) : null,
          saved ? h("div", { className: "flota-ok", role: "status" },
            "Saved " + saved.machine + " (profile " + saved.profile + ").",
            saved.setup_command ? h("span", null, " Create its SSH profile in a terminal: ",
              h("code", null, saved.setup_command)) : null) : null,
          h("div", { className: "flota-actions" },
            h(C.Button, { type: "submit", size: "sm", disabled: saving || !form.name || !form.host },
              saving ? "Saving…" : "Save machine"),
            h(C.Button, { type: "button", size: "sm", ghost: true, onClick: onCancel }, "Close")))));
  }

  // --- page -----------------------------------------------------------------------------------

  function FlotaPage() {
    const [data, setData] = useState(null);
    const [loadError, setLoadError] = useState(null);
    const [actionMsg, setActionMsg] = useState(null);
    const [pending, setPending] = useState({});
    const [showForm, setShowForm] = useState(false);
    const [selected, setSelected] = useState(null);
    const [hovered, setHovered] = useState(null);
    const [updated, setUpdated] = useState(0);
    const [now, setNow] = useState(Date.now());
    const [fresh, setFresh] = useState({});
    const alive = useRef(true);
    const known = useRef(null);  // machine names seen on the previous poll

    const loading = useRef(false);  // one /status at a time: a slow server must not stack requests
    const refresh = useCallback(function () {
      if (loading.current) return Promise.resolve();
      loading.current = true;
      return api("/status").then(function (res) {
        if (!alive.current) return;
        const names = (res.machines || []).map(function (m) { return m.name; });
        if (known.current) {
          const added = names.filter(function (n) { return !known.current.has(n); });
          if (added.length) {
            setFresh(function (f) {
              const next = Object.assign({}, f);
              added.forEach(function (n) { next[n] = true; });
              return next;
            });
            setTimeout(function () {
              if (!alive.current) return;
              setFresh(function (f) {
                const next = Object.assign({}, f);
                added.forEach(function (n) { delete next[n]; });
                return next;
              });
            }, 2600);
          }
        }
        known.current = new Set(names);
        setData(res); setLoadError(null); setUpdated(Date.now());
      }).catch(function (err) {
        if (alive.current) setLoadError(explain(err).message);
      }).finally(function () { loading.current = false; });
    }, []);

    useEffect(function () {
      alive.current = true;
      refresh();
      // A hidden tab does not poll (it would keep an idle gateway awake); it catches up when shown.
      const tick = function () { if (!document.hidden) refresh(); };
      const timer = setInterval(tick, POLL_MS);
      const clock = setInterval(function () { setNow(Date.now()); }, 1000);
      document.addEventListener("visibilitychange", tick);
      return function () {
        alive.current = false; clearInterval(timer); clearInterval(clock);
        document.removeEventListener("visibilitychange", tick);
      };
    }, [refresh]);

    function select(name) {
      setSelected(name);
      if (!name) return;
      setTimeout(function () {
        const el = document.getElementById("flota-card-" + name);
        if (el && el.scrollIntoView) el.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }, 30);
    }

    function onAction(action, name) {
      setPending(function (p) { return Object.assign({}, p, { [name]: action }); });
      setActionMsg(null);
      const path = "/" + action + "/" + encodeURIComponent(name);
      const req = action === "delete"
        ? post("/machines/" + encodeURIComponent(name), undefined, "DELETE")
        : post(path).catch(function (err) {
          // 409: the gateway reports it busy. Ask, then send it again with force.
          const what = action === "off" ? "Busy — power off anyway?" : "Busy — sleep anyway?";
          if ((action === "sleep" || action === "off") && httpStatus(err) === 409 &&
              window.confirm(what + "\n\n" + explain(err).message)) {
            return post(path + "?force=1", { force: true });
          }
          throw err;
        });
      req.then(function (res) {
        const text = action === "wake" ? name + ": waking; the map shows it coming up."
          : action === "delete" ? "Removed " + name + ". " + (res.profile_kept || "")
          : res.result;
        setActionMsg({ ok: true, text: text });
        if (action === "delete" && selected === name) setSelected(null);
      }).catch(function (err) {
        setActionMsg({ ok: false, text: explain(err).message });
      }).finally(function () {
        setPending(function (p) { const n = Object.assign({}, p); delete n[name]; return n; });
        refresh();
      });
    }

    const machines = (data && data.machines) || [];
    const upCount = machines.filter(function (m) { return m.up; }).length;
    const busyCount = machines.filter(function (m) { return m.up && m.busy > 0; }).length;
    return h("div", { className: "flota-page" },
      h("header", { className: "flota-header" },
        h("div", null,
          h("h1", { className: "flota-title" }, "Flota"),
          h("p", { className: "flota-subtitle" },
            "Your machines for subagents: woken on demand, put back to sleep when idle.")),
        h("div", { className: "flota-header-actions" },
          DEMO ? h(C.Badge, { tone: "warning", title: "?flota_demo in the URL: made-up machines, simulated actions" },
            "demo data") : null,
          data ? h(C.Badge, { tone: "secondary" }, upCount + " / " + machines.length + " up") : null,
          busyCount ? h(C.Badge, { tone: "warning" }, busyCount + " busy") : null,
          h(C.Button, { size: "sm", outlined: true, onClick: refresh }, "Refresh"),
          h(C.Button, { size: "sm", onClick: function () { setShowForm(!showForm); } },
            showForm ? "Hide form" : "Add machine"))),
      loadError ? h("div", { className: "flota-error", role: "alert" }, "Could not load the fleet: " + loadError) : null,
      actionMsg ? h("div", { className: actionMsg.ok ? "flota-ok" : "flota-error", role: "status" }, actionMsg.text) : null,
      h(C.Card, { className: "flota-card flota-map-card" },
        h(C.CardContent, { className: "flota-map-body" },
          !data && !loadError ? h("p", { className: "flota-muted flota-map-loading" }, "Checking your machines…") : null,
          h(FleetMap, { machines: machines, hub: data && data.hub, selected: selected, hovered: hovered,
            fresh: fresh, onSelect: select, onHover: setHovered }),
          h(Legend, { updated: updated, now: now, busyKnown: data ? data.busy_known : null }))),
      showForm ? h(AddMachineForm, { onSaved: refresh, onCancel: function () { setShowForm(false); } }) : null,
      data && data.problems && data.problems.length ? h("div", { className: "flota-warn", role: "alert" },
        h("strong", null, "Config problems"),
        h("ul", null, data.problems.map(function (p, i) { return h("li", { key: i }, p); }))) : null,
      data && !machines.length && !showForm ? h(EmptyState, { onAdd: function () { setShowForm(true); } }) : null,
      machines.length ? h("div", { className: "flota-section-head" },
        h("h2", null, "Machines"),
        h("span", { className: "flota-muted" },
          selected ? h("button", { type: "button", className: "flota-link-btn", onClick: function () { setSelected(null); } },
            "Clear selection") : "Click a machine on the map to jump to it.")) : null,
      h("div", { className: "flota-grid" }, machines.map(function (m) {
        return h(MachineCard, { key: m.name, m: m, allowOff: data.allow_power_off, pending: pending[m.name],
          selected: selected === m.name, onHover: setHovered, onAction: onAction });
      })),
      h("p", { className: "flota-muted flota-footnote" },
        "This tab is the quick view: status, wake, sleep and add by address. The full fleet view (crews, schedule, " +
        "pairing, flagship and ship roles) lives in the Flota page of Hermes Desktop: ",
        h("a", { href: "https://github.com/Bto-03/hermes-flota/blob/main/docs/desktop-page.md", target: "_blank",
          rel: "noreferrer noopener" }, "about the Flota page"), "."),
      h("p", { className: "flota-muted flota-footnote" },
        "Status refreshes every 5 s (SSH port probe). ",
        data && !data.busy_known ? "No running gateway has reported its subagents, so Sleep cannot tell " +
          "whether a machine is busy. " : null,
        "Auto-sleep, default sleep action and \"Allow power off\" are in Plugins > Flota."));
  }

  window.__HERMES_PLUGINS__.register("flota", FlotaPage);
})();
