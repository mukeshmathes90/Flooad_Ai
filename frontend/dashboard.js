/* ==========================================================================
   FloodWatch - Early Warning System  |  dashboard.js
   Vanilla JS. Talks only to the real backend API. Never fabricates values:
   missing/partial data is shown as "—" and forecasts are kept separate from
   measured readings.
   ========================================================================== */
"use strict";

/* ------------------------------------------------------------------ state */
const POLL_MS = 3000;
const AUX_MS = 15000;
const ALERT_MS = 10000;

const state = {
  source: "esp32",
  section: "overview",
  auto: true,
  thresholds: null,
  riskLevels: ["normal", "warning", "high", "critical", "unknown"],
  charts: {},          // key -> Chart instance
  timers: {},
  lastSummary: null,
  health: null,
  model: null,
  notification: null,
  email: null,
  esp: null,
  histFilters: { from: "", to: "", limit: 300 },
  alertFilters: { risk: "", origin: "", limit: 100 },
  busy: false,
};

const RISK_COLORS = {
  normal: "#15a34a",
  warning: "#c97706",
  high: "#e1590c",
  critical: "#d92b2b",
  unknown: "#64748b",
  muted: "#64748b",
};

/* ------------------------------------------------------------------ utils */
const $ = (id) => document.getElementById(id);
const clampNum = (v) => (v === null || v === undefined || v === "" || Number.isNaN(Number(v)) ? null : Number(v));

function fmt(v, digits = 1) {
  const n = clampNum(v);
  return n === null ? "—" : n.toFixed(digits);
}
function fmtInt(v) {
  const n = clampNum(v);
  return n === null ? "—" : Math.round(n).toLocaleString();
}
function timeOf(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleTimeString();
}
function dateTimeOf(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
}
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function classify(level, thresholds) {
  const t = thresholds || state.thresholds || {};
  const n = clampNum(level);
  if (n === null || t.warning_cm == null) return "unknown";
  if (n >= t.critical_cm) return "critical";
  if (n >= t.high_cm) return "high";
  if (n >= t.warning_cm) return "warning";
  return "normal";
}
function riskTag(level) {
  const l = (level || "unknown").toLowerCase();
  const cls = { accepted: "tag-ok" }[l] || `tag-${l}`;
  const known = { normal: "tag-ok", warning: "tag-warn", high: "tag-high", critical: "tag-bad", unknown: "tag-unknown" };
  return `<span class="tag ${known[l] || "tag-info"}">${esc(l)}</span>`;
}
function statusTag(status) {
  const map = { accepted: "tag-ok", constrained: "tag-warn", rejected: "tag-bad", unavailable: "tag-bad" };
  return `<span class="tag ${map[status] || "tag-info"}">${esc(status || "n/a")}</span>`;
}

/* ----------------------------------------------------------------- toasts */
function toast(message, kind = "info", timeout = 4200) {
  const box = $("toasts");
  const el = document.createElement("div");
  el.className = "toast";
  el.dataset.kind = kind;
  el.innerHTML = message;
  box.appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transform = "translateY(8px)"; }, timeout - 300);
  setTimeout(() => el.remove(), timeout);
}

function showError(msg) {
  const box = $("error-box");
  if (!msg) { box.hidden = true; $("error-text").textContent = ""; return; }
  box.hidden = false;
  $("error-text").textContent = msg;
}

/* --------------------------------------------------------------- sections */
function setSection(id, { scroll = false } = {}) {
  if (!id || !$(id)) return;
  state.section = id;
  document.querySelectorAll(".section").forEach((s) => { s.hidden = s.id !== id; });
  document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("is-active", n.dataset.target === id));
  const active = $(id);
  $("page-title").textContent = active.dataset.title || "Overview";
  $("page-sub").textContent = active.dataset.sub || "";
  closeSidebar();
  if (scroll) window.scrollTo({ top: 0, behavior: "smooth" });
  // Chart.js needs a resize once a previously-hidden canvas becomes visible.
  requestAnimationFrame(() => {
    Object.values(state.charts).forEach((c) => { try { c.resize(); } catch (e) {} });
  });
  // On-demand fetches for data-heavy sections.
  if (id === "alerts") pollAlerts();
  if (id === "historical") pollHistorical();
  if (id === "performance" || id === "status") pollAux();
}

function openSidebar() { $("sidebar").classList.add("open"); $("scrim").classList.add("show"); }
function closeSidebar() { $("sidebar").classList.remove("open"); $("scrim").classList.remove("show"); }

/* ----------------------------------------------------------- connection */
function renderConnection(summary) {
  const st = summary.device_status || {};
  const map = {
    live: ["live", "LIVE"],
    stale: ["stale", "STALE"],
    offline: ["offline", "OFFLINE"],
    no_data: ["unknown", "NO DATA"],
    unknown: ["unknown", "UNKNOWN"],
  };
  const [dataState, text] = map[st.status] || map.unknown;
  const pill = $("conn-pill");
  pill.dataset.state = dataState;
  $("conn-text").textContent = text + (st.device_id ? " · " + st.device_id : "");
  $("last-update").textContent = st.last_seen ? timeOf(st.last_seen) : "—";
  $("data-age").textContent = st.age_seconds != null ? Math.round(st.age_seconds) + " s" : "—";
  $("footer-conn").textContent =
    `Source: ${state.source === "test" ? "TEST" : "LIVE"} · updated ${new Date().toLocaleTimeString()}`;
}

/* ---------------------------------------------------------------- hero */
function renderHero(summary) {
  const hero = $("risk-hero");
  const overall = (summary.overall_risk || "unknown").toLowerCase();
  hero.dataset.risk = overall;
  $("hero-risk").textContent = overall.toUpperCase();
  $("chip-current").textContent = summary.current_risk || "unknown";
  $("chip-forecast").textContent = (summary.prediction && summary.prediction.forecast_risk) || "unknown";
  $("chip-source").textContent = summary.source === "test" ? "test data" : "live esp32";

  const st = summary.device_status || {};
  let note;
  if (summary.source === "test" && (st.status === "no_data" || st.status === "unknown")) {
    note = "No TEST data yet — press TEST NORMAL / WARNING / HIGH-RISK / CRITICAL in System Status to inject one, then the numbers appear here.";
  } else if (summary.source === "test") {
    note = "TEST SCENARIO data (source=test). Not live ESP32 measurements — no SMS or hardware action performed.";
  } else if (st.status === "offline") {
    note = "Device offline — showing the last known values. Check power, Wi-Fi and the backend URL.";
  } else if (st.status === "stale") {
    note = "Last reading is stale — it is NOT presented as live. Waiting for a fresh measurement.";
  } else if (st.status === "no_data") {
    note = "No readings received yet. Power on the ESP32 or use the TEST buttons in System Status.";
  } else {
    note = "Live monitoring active. Current risk is measured; forecast risk comes from the validated ML model.";
  }
  $("hero-note").textContent = note;
}

/* ---------------------------------------------------------------- KPIs */
function renderKpis(summary) {
  const r = summary.latest_reading || {};
  const t = summary.thresholds || {};
  const pred = summary.prediction || {};
  const st = summary.device_status || {};

  $("kpi-water").textContent = fmt(r.water_level_cm, 1);
  const age = st.age_seconds;
  $("kpi-water-foot").textContent =
    age != null && age > 120
      ? `⚠ stale reading (${Math.round(age)} s old)`
      : `above reference (${fmt(t.reference_height_cm, 0)} cm)`;

  $("kpi-rain").textContent = fmt(r.rainfall_intensity, 0);
  $("kpi-rain-foot").textContent = `relative intensity · raw ADC ${fmtInt(r.rainfall_reading)}`;

  $("kpi-dist").textContent = fmt(r.ultrasonic_distance_cm, 1);

  $("kpi-rate").textContent = fmt(summary.rate_cm_per_min, 2);
  const rate = clampNum(summary.rate_cm_per_min);
  $("kpi-rate-foot").textContent =
    rate === null ? "not enough readings yet"
      : rate > 0.05 ? "rising" : rate < -0.05 ? "falling" : "steady";

  const accepted = (pred.horizons || []).filter((h) => h.accepted && h.predicted_level_cm != null);
  if (accepted.length) {
    const peak = accepted.reduce((a, b) => (b.predicted_level_cm > a.predicted_level_cm ? b : a));
    $("kpi-pred").textContent = fmt(peak.predicted_level_cm, 1);
    $("kpi-pred-foot").textContent = `peak at +${fmt(peak.horizon_min, 0)} min · ${pred.forecast_risk || "unknown"} risk`;
  } else {
    $("kpi-pred").textContent = "—";
    $("kpi-pred-foot").textContent = pred.model_available === false
      ? "model unavailable" : "waiting for enough history";
  }

  const cur = (summary.current_risk || "unknown").toLowerCase();
  const riskEl = $("kpi-risk");
  riskEl.textContent = cur;
  riskEl.style.color = RISK_COLORS[cur] || RISK_COLORS.unknown;
  $("kpi-risk-foot").textContent = "current measured risk";

  const dev = { live: "Online", stale: "Stale", offline: "Offline", no_data: "No data", unknown: "Unknown" }[st.status] || "Unknown";
  $("kpi-device").textContent = dev;
  $("kpi-device").style.color =
    st.status === "live" ? RISK_COLORS.normal : st.status === "stale" ? RISK_COLORS.warning
      : st.status === "offline" ? RISK_COLORS.critical : RISK_COLORS.unknown;
  $("kpi-device-foot").textContent = st.last_seen ? `last seen ${timeOf(st.last_seen)}` : "no contact yet";

  const alerts = summary.alerts || [];
  const active = alerts.filter((a) => ["warning", "high", "critical"].includes((a.risk_level || "").toLowerCase()));
  $("kpi-alerts").textContent = String(active.length);
  $("kpi-alerts-foot").textContent = active.length ? "risk ≥ warning logged" : "no active warnings";
}

/* ----------------------------------------------------------- monitoring */
function renderMonitoring(summary) {
  const st = summary.device_status || {};
  const r = summary.latest_reading || {};
  const t = summary.thresholds || {};

  $("mon-status").textContent = st.status || "—";
  $("mon-device").textContent = st.device_id || r.device_id || "—";
  $("mon-source").textContent = summary.source === "test" ? "test" : (st.source || "esp32");
  $("mon-last").textContent = dateTimeOf(st.last_seen);
  $("mon-age").textContent = st.age_seconds != null ? Math.round(st.age_seconds) + " s" : "—";
  $("mon-valid").innerHTML = r.valid ? '<span class="tag tag-ok">valid</span>' : '<span class="tag tag-bad">invalid</span>';
  $("mon-invalid").textContent = r.invalid_reason || "—";

  $("mon-water").textContent = fmt(r.water_level_cm, 2) + " cm";
  $("mon-dist").textContent = fmt(r.ultrasonic_distance_cm, 2) + " cm";
  $("mon-rainraw").textContent = fmtInt(r.rainfall_reading);
  $("mon-rainpct").textContent = fmt(r.rainfall_intensity, 1) + " %";
  $("mon-wraw").textContent = fmtInt(r.water_sensor_raw);
  $("mon-pir").textContent = r.pir_motion === 1 ? "MOTION" : r.pir_motion === 0 ? "clear" : "—";
  $("mon-ts").textContent = dateTimeOf(r.server_timestamp);

  $("mon-thresholds").innerHTML = `
    <div class="stat-row"><span>Normal</span><b>&lt; ${fmt(t.warning_cm, 0)} cm</b></div>
    <div class="stat-row"><span>Warning</span><b>${fmt(t.warning_cm, 0)} – ${fmt(t.high_cm, 0)} cm</b></div>
    <div class="stat-row"><span>High</span><b>${fmt(t.high_cm, 0)} – ${fmt(t.critical_cm, 0)} cm</b></div>
    <div class="stat-row"><span>Critical</span><b>≥ ${fmt(t.critical_cm, 0)} cm</b></div>
    <div class="stat-row"><span>Extreme (ref.)</span><b>≥ ${fmt(t.extreme_cm, 0)} cm</b></div>`;
  $("mon-threshold-note").textContent =
    `Reference height: ${fmt(t.reference_height_cm, 0)} cm. Prototype thresholds — calibrate to the real installation.`;

  const rows = (summary.recent_readings || []).slice().reverse().slice(0, 60);
  const tb = $("readings-table");
  if (!rows.length) {
    tb.innerHTML = `<tr><td class="empty" colspan="9">No readings yet.</td></tr>`;
  } else {
    tb.innerHTML = rows.map((x) => {
      const lvl = classify(x.water_level_cm, t);
      return `<tr>
        <td>${timeOf(x.server_timestamp)}</td>
        <td>${esc(x.device_id)}</td>
        <td>${esc(x.source)}</td>
        <td class="num">${fmt(x.water_level_cm, 2)}</td>
        <td class="num">${fmt(x.ultrasonic_distance_cm, 1)}</td>
        <td class="num">${fmtInt(x.rainfall_reading)}</td>
        <td class="num">${fmt(x.rainfall_intensity, 0)}</td>
        <td class="num">${fmtInt(x.water_sensor_raw)}</td>
        <td>${riskTag(lvl)}</td>
      </tr>`;
    }).join("");
  }
}

/* ---------------------------------------------------------- predictions */
function renderPredictions(summary) {
  const pred = summary.prediction || {};
  $("pred-model-pill").textContent = pred.model_available
    ? `model ${pred.model_version || "?"}`
    : "model unavailable";

  const horizons = (pred.horizons || []).slice().sort((a, b) => a.horizon_min - b.horizon_min);
  const tb = $("pred-table");
  if (!horizons.length) {
    tb.innerHTML = `<tr><td class="empty" colspan="6">${
      pred.model_available === false
        ? "Model unavailable — no forecast. Run train_model.bat."
        : "No prediction yet — waiting for enough valid readings."
    }</td></tr>`;
  } else {
    tb.innerHTML = horizons.map((h) => `
      <tr>
        <td>+${fmt(h.horizon_min, 0)} min</td>
        <td class="num">${fmt(h.raw_predicted_level_cm, 2)}</td>
        <td class="num">${h.accepted ? fmt(h.predicted_level_cm, 2) : "—"}</td>
        <td>${statusTag(h.validation_status)}</td>
        <td>${riskTag(h.risk_level)}</td>
        <td>${esc(h.reason || "")}</td>
      </tr>`).join("");
  }

  const note = $("pred-note");
  if (pred.error) { note.hidden = false; note.className = "note note-warn"; note.textContent = "Prediction note: " + pred.error; }
  else if (pred.data_quality && pred.data_quality.status && pred.data_quality.status !== "ok") {
    note.hidden = false; note.className = "note note-info"; note.textContent = "Data quality: " + pred.data_quality.status + ".";
  } else { note.hidden = true; }
}

/* -------------------------------------------------------------- alerts */
function renderEmailStatus(email) {
  const note = $("email-status-note");
  const detail = $("email-status-detail");
  if (!note) return;
  if (!email) {
    note.className = "note";
    note.textContent = "Email: status unavailable.";
    if (detail) detail.innerHTML = "";
    return;
  }
  if (email.configured) {
    note.className = "note note-info";
    note.textContent = "Email notifications: CONFIGURED. Alerts are sent only when the risk category genuinely changes.";
  } else {
    note.className = "note";
    note.textContent = "Email notifications: NOT CONFIGURED" + (email.reason ? " (" + email.reason + ")" : "") + " — the rest of the system remains operational.";
  }
  if (detail) {
    detail.innerHTML = `
      <div class="stat-row"><span>Status</span><b>${esc(email.status || "—")}</b></div>
      <div class="stat-row"><span>Sender</span><b>${esc(email.sender || "—")}</b></div>
      <div class="stat-row"><span>SMTP server</span><b>${esc((email.host || "—") + ":" + (email.port || ""))}</b></div>
      <div class="stat-row"><span>Latest attempt</span><b>${esc(dateTimeOf(email.latest_attempt))}</b></div>
      <div class="stat-row"><span>Latest delivery status</span><b>${esc(email.latest_delivery_status || "—")}</b></div>
      <div class="stat-row"><span>Latest subject</span><b>${esc(email.latest_subject || "—")}</b></div>
      ${email.latest_failure_reason ? `<div class="stat-row"><span>Failure reason</span><b>${esc(email.latest_failure_reason)}</b></div>` : ""}`;
  }
  renderEmailHistory(email.history || []);
}

function renderEmailHistory(emails) {
  const tb = $("email-table");
  if (!tb) return;
  if (!emails.length) {
    tb.innerHTML = `<tr><td class="empty" colspan="4">No email notifications yet.</td></tr>`;
    return;
  }
  tb.innerHTML = emails.map((e) => `
    <tr>
      <td>${dateTimeOf(e.created_at)}</td>
      <td>${esc(e.subject || "")}${e.is_test ? " [TEST]" : ""}</td>
      <td>${esc(e.status || "—")}</td>
      <td>${esc(e.error || "—")}</td>
    </tr>`).join("");
}

function renderAlerts(alerts, notification) {
  const tb = $("alert-table");
  if (!alerts || !alerts.length) {
    tb.innerHTML = `<tr><td class="empty" colspan="6">No alerts logged.</td></tr>`;
  } else {
    tb.innerHTML = alerts.map((a) => `
      <tr>
        <td>${dateTimeOf(a.created_at)}</td>
        <td>${riskTag(a.risk_level)}</td>
        <td>${esc(a.origin || "")}</td>
        <td>${esc(a.device_id || "")}</td>
        <td>${esc(a.message || "")}</td>
        <td>${esc(a.notification_status || "—")}${a.notified ? " ✓" : ""}</td>
      </tr>`).join("");
  }
  const note = $("notify-note");
  if (notification) {
    if (notification.configured) {
      note.className = "note note-info";
      note.textContent = `Remote notifications: configured (${notification.provider}). "sent" means the provider accepted the request.`;
    } else {
      note.className = "note";
      note.textContent = "Remote notifications: NOT configured — alerts are logged locally only (test alerts are never sent).";
    }
  }
  const active = (alerts || []).filter((a) => ["warning", "high", "critical"].includes((a.risk_level || "").toLowerCase())).length;
  const badge = $("nav-alert-count");
  badge.textContent = String(active);
  badge.classList.toggle("show", active > 0);
}

/* --------------------------------------------------------- performance */
function renderPerformance(model) {
  const el = model || {};
  const evaluation = el.evaluation || {};
  const metrics = evaluation.metrics || {};
  const test = metrics.test || {};
  const val = metrics.validation || {};

  const warning = $("perf-warning");
  if (evaluation.warning) { warning.hidden = false; warning.textContent = "⚠ " + evaluation.warning; }
  else if (!el.available) { warning.hidden = false; warning.className = "note note-warn"; warning.textContent = "⚠ Model not available: " + (el.load_error || "unknown"); }
  else warning.hidden = true;

  const keys = Object.keys(test).map(Number).sort((a, b) => a - b);
  if (keys.length) {
    const maeAvg = keys.reduce((s, k) => s + test[k].mae_cm, 0) / keys.length;
    const rmseAvg = keys.reduce((s, k) => s + test[k].rmse_cm, 0) / keys.length;
    const r2Avg = keys.reduce((s, k) => s + test[k].r2, 0) / keys.length;
    $("perf-mae").textContent = maeAvg.toFixed(3);
    $("perf-rmse").textContent = rmseAvg.toFixed(3);
    $("perf-r2").textContent = r2Avg.toFixed(3);
    $("perf-mae-foot").textContent = `mean across horizons +${keys[0]}…+${keys[keys.length - 1]} min`;
  } else {
    $("perf-mae").textContent = $("perf-rmse").textContent = $("perf-r2").textContent = "—";
    $("perf-mae-foot").textContent = "no evaluation report";
  }

  const tb = $("perf-table");
  const order = ["validation", "test"];
  let rowsHtml = "";
  order.forEach((split) => {
    const src = metrics[split] || {};
    Object.keys(src).map(Number).sort((a, b) => a - b).forEach((k) => {
      const x = src[k];
      rowsHtml += `<tr>
        <td>+${k} min</td><td>${split}</td>
        <td class="num">${x.mae_cm}</td><td class="num">${x.rmse_cm}</td>
        <td class="num">${x.r2}</td><td class="num">${x.n}</td>
      </tr>`;
    });
  });
  tb.innerHTML = rowsHtml || `<tr><td class="empty" colspan="6">No metrics available.</td></tr>`;

  const meta = el.metadata || {};
  const feats = (evaluation.feature_names || meta.feature_names || []);
  $("perf-details").innerHTML = `
    <div class="stat-row"><span>Model type</span><b>${esc(evaluation.model_type || meta.model_type || "—")}</b></div>
    <div class="stat-row"><span>Version</span><b>${esc(evaluation.model_version || meta.model_version || "—")}</b></div>
    <div class="stat-row"><span>Data source</span><b>${esc(evaluation.data_source || "—")}</b></div>
    <div class="stat-row"><span>Trained at</span><b>${esc(dateTimeOf(evaluation.trained_at))}</b></div>
    <div class="stat-row"><span>Window</span><b>${esc(evaluation.window || "—")} samples</b></div>
    <div class="stat-row"><span>Horizons</span><b>${(evaluation.horizons_min || []).map((h) => "+" + h).join(", ") || "—"} min</b></div>
    <div class="stat-row"><span>Feature count</span><b>${feats.length || "—"}</b></div>
    <div class="stat-row"><span>Rows (train / val / test)</span><b>${evaluation.n_train ?? "—"} / ${evaluation.n_val ?? "—"} / ${evaluation.n_test ?? "—"}</b></div>`;

  renderModelChart(test);
}

/* ------------------------------------------------------------- system */
function renderSystem(health, model, notification, summary) {
  const hOk = health && health.status === "ok";
  $("sys-backend").textContent = health ? (hOk ? "Healthy" : "Degraded") : "Unreachable";
  $("sys-backend").style.color = health ? (hOk ? RISK_COLORS.normal : RISK_COLORS.warning) : RISK_COLORS.critical;
  $("sys-backend-foot").textContent = health ? "GET /health responded" : "no response from /health";

  const dbOk = health && health.database && health.database.ok;
  $("sys-db").textContent = dbOk ? "Connected" : "Error";
  $("sys-db").style.color = dbOk ? RISK_COLORS.normal : RISK_COLORS.critical;
  $("sys-db-foot").textContent = health && health.database ? health.database.path : "—";

  const modelOk = model && model.available;
  $("sys-model").textContent = modelOk ? "Loaded" : "Unavailable";
  $("sys-model").style.color = modelOk ? RISK_COLORS.normal : RISK_COLORS.warning;
  $("sys-model-foot").textContent = modelOk
    ? `version ${(model.metadata && model.metadata.model_version) || "?"}`
    : (model && model.load_error) || "no model";

  const stats = (health && health.database && health.database.stats) || (summary && summary.db_stats) || {};
  $("sys-db-stats").innerHTML = Object.keys(stats).map(
    (k) => `<div class="stat-row"><span>${esc(k)}</span><b>${fmtInt(stats[k])}</b></div>`
  ).join("") || `<div class="muted">No statistics.</div>`;

  const srv = (health && health.server) || {};
  $("sys-config").innerHTML = `
    <div class="stat-row"><span>Bind host</span><b>${esc(srv.host || "—")}</b></div>
    <div class="stat-row"><span>Port</span><b>${esc(srv.port ?? "—")}</b></div>
    <div class="stat-row"><span>Data source</span><b>${state.source === "test" ? "test" : "esp32"}</b></div>
    <div class="stat-row"><span>Notification provider</span><b>${esc((notification && notification.provider) || "disabled")}</b></div>
    <div class="stat-row"><span>Notification configured</span><b>${notification && notification.configured ? "yes" : "no"}</b></div>`;

  $("sb-server").textContent = health ? `${srv.host || "0.0.0.0"}:${srv.port || 8000}` : "—";
  $("sb-model").textContent = modelOk ? "ready" : "n/a";
  $("sb-source").textContent = state.source === "test" ? "TEST" : "LIVE";
}

/* ---------------------------------------------------------------- esp32 */
function renderEsp(esp) {
  const hostInput = $("esp-host");
  const portInput = $("esp-port");
  const box = $("esp-status");
  if (!box) return;
  const e = esp || {};
  // Fill the inputs once, but never fight the user while they are typing.
  if (hostInput && document.activeElement !== hostInput && !hostInput.dataset.touched) {
    hostInput.value = e.host || "";
  }
  if (portInput && document.activeElement !== portInput && e.port) {
    portInput.value = e.port;
  }
  const conn = e.connected;
  let stateText, stateColor;
  if (!e.configured) { stateText = "Not configured (push mode)"; stateColor = RISK_COLORS.muted; }
  else if (conn === true) { stateText = "Connected — pulling readings"; stateColor = RISK_COLORS.normal; }
  else if (conn === false) { stateText = "Unreachable — check IP / same hotspot"; stateColor = RISK_COLORS.critical; }
  else { stateText = "Saved — waiting for first poll"; stateColor = RISK_COLORS.warning; }
  box.innerHTML = `
    <div class="stat-row"><span>Status</span><b style="color:${stateColor}">${esc(stateText)}</b></div>
    <div class="stat-row"><span>ESP32 address</span><b>${esc(e.host || "—")}${e.port ? ":" + esc(String(e.port)) : ""}</b></div>
    <div class="stat-row"><span>Pull endpoint</span><b>${esc(e.endpoint || "—")}</b></div>
    <div class="stat-row"><span>Readings ingested</span><b>${fmtInt(e.readings_ingested || 0)}</b></div>
    <div class="stat-row"><span>Last success</span><b>${esc(dateTimeOf(e.last_ok))}</b></div>
    ${e.last_error ? `<div class="stat-row"><span>Last error</span><b>${esc(e.last_error)}</b></div>` : ""}`;
}

async function espSave() {
  const host = ($("esp-host").value || "").trim();
  const port = Number($("esp-port").value) || 80;
  try {
    if (!host) {
      await postJSON("/api/esp/config/clear", {});
      toast("ESP32 address cleared — the backend will not pull from a device.", "info");
    } else {
      await postJSON("/api/esp/config", { host, port });
      toast("ESP32 address saved. The backend will poll <b>" + esc(host) + ":" + esc(String(port)) + "</b>.", "success");
    }
    $("esp-host").dataset.touched = "";
    renderEsp((await getJSON("/api/esp/status")) || {});
  } catch (err) {
    toast("Could not save ESP32 address: " + esc(err.message), "error", 6000);
  }
}

async function espTest() {
  const host = ($("esp-host").value || "").trim();
  const port = Number($("esp-port").value) || 80;
  if (!host) { toast("Enter the ESP32 IP first (shown on its OLED).", "info"); return; }
  try {
    const r = await postJSON("/api/esp/test", { host, port });
    if (r.ok) {
      toast("ESP32 reachable! Device <b>" + esc(r.device_id || "esp32") + "</b> responded on port " + esc(String(port)) + ".", "success");
    } else {
      toast("ESP32 not reachable: " + esc(r.reason || "no response") +
        "<br><span class='muted'>Confirm same hotspot and correct IP.</span>", "error", 7000);
    }
  } catch (err) {
    toast("Test failed: " + esc(err.message), "error", 6000);
  }
}

async function espClear() {
  try {
    await postJSON("/api/esp/config/clear", {});
    $("esp-host").value = "";
    $("esp-host").dataset.touched = "";
    renderEsp({ connected: null, configured: false });
    toast("ESP32 address cleared.", "info");
  } catch (err) {
    toast("Clear failed: " + esc(err.message), "error", 5000);
  }
}

/* -------------------------------------------------------------- charts */
function chartsAvailable() { return typeof Chart !== "undefined"; }

function baseOptions(extra = {}) {
  return Object.assign({
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 300 },
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { labels: { color: "#33465f", boxWidth: 12, boxHeight: 12, usePointStyle: true, padding: 14, font: { size: 11 } } },
      tooltip: {
        backgroundColor: "#0e2c4f", titleColor: "#fff", bodyColor: "#dbe7f5",
        padding: 10, cornerRadius: 8, boxPadding: 4,
      },
    },
    scales: {
      x: { ticks: { color: "#61748d", maxTicksLimit: 8, font: { size: 10.5 } }, grid: { color: "#eaf0f7" }, border: { color: "#dbe4f0" } },
      y: { ticks: { color: "#61748d", font: { size: 10.5 } }, grid: { color: "#eaf0f7" }, border: { color: "#dbe4f0" }, beginAtZero: false },
    },
  }, extra);
}

function upsert(key, canvasId, cfg) {
  if (!chartsAvailable()) return;
  const canvas = $(canvasId);
  if (!canvas) return;
  if (state.charts[key]) {
    state.charts[key].data = cfg.data;
    if (cfg.type && state.charts[key].config.type !== cfg.type) {
      state.charts[key].destroy();
      state.charts[key] = new Chart(canvas.getContext("2d"), cfg);
    } else {
      state.charts[key].options = cfg.options;
      state.charts[key].update();
    }
  } else {
    state.charts[key] = new Chart(canvas.getContext("2d"), cfg);
  }
}

function thresholdDatasets(labels, t, withLabels = true) {
  const mk = (val, color, label) => ({
    label, data: labels.map(() => val), borderColor: color, borderWidth: 1.4, borderDash: [7, 5],
    pointRadius: 0, pointHitRadius: 0, fill: false, tension: 0,
  });
  const ds = [];
  if (t.warning_cm != null) ds.push(mk(t.warning_cm, RISK_COLORS.warning, withLabels ? `Warning ${t.warning_cm} cm` : undefined));
  if (t.high_cm != null) ds.push(mk(t.high_cm, RISK_COLORS.high, withLabels ? `High ${t.high_cm} cm` : undefined));
  if (t.critical_cm != null) ds.push(mk(t.critical_cm, RISK_COLORS.critical, withLabels ? `Critical ${t.critical_cm} cm` : undefined));
  return ds;
}

function renderWaterChart(summary) {
  const rows = summary.recent_readings || [];
  const t = summary.thresholds || {};
  const labels = rows.map((r) => timeOf(r.server_timestamp));
  const water = rows.map((r) => clampNum(r.water_level_cm));
  const cfg = {
    type: "line",
    data: {
      labels,
      datasets: [{
        label: "Water level (cm)", data: water, borderColor: "#2563eb",
        backgroundColor: "rgba(37,99,235,.12)", fill: true, tension: .3,
        pointRadius: 0, pointHoverRadius: 4, borderWidth: 2.2,
      }, ...thresholdDatasets(labels, t)],
    },
    options: baseOptions(),
  };
  upsert("water", "chart-water", cfg);
}

function renderRainChart(summary) {
  const rows = summary.recent_readings || [];
  const labels = rows.map((r) => timeOf(r.server_timestamp));
  const rain = rows.map((r) => clampNum(r.rainfall_intensity));
  const cfg = {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "Rainfall (relative %)", data: rain,
        backgroundColor: "rgba(8,145,178,.55)", borderColor: "rgba(8,145,178,.9)",
        borderWidth: 1, borderRadius: 3, maxBarThickness: 18,
      }],
    },
    options: baseOptions({ scales: {
      x: { ticks: { color: "#61748d", maxTicksLimit: 8, font: { size: 10.5 } }, grid: { display: false } },
      y: { beginAtZero: true, max: 100, ticks: { color: "#61748d", font: { size: 10.5 } }, grid: { color: "#eaf0f7" } },
    } }),
  };
  upsert("rain", "chart-rain", cfg);
}

function renderRiskChart(summary) {
  const rows = summary.recent_readings || [];
  const t = summary.thresholds || {};
  const counts = { normal: 0, warning: 0, high: 0, critical: 0, unknown: 0 };
  rows.forEach((r) => { counts[classify(r.water_level_cm, t)]++; });
  const order = ["normal", "warning", "high", "critical", "unknown"];
  const data = order.map((k) => counts[k]);
  const total = data.reduce((a, b) => a + b, 0);
  const cfg = {
    type: "doughnut",
    data: {
      labels: order.map((k) => k[0].toUpperCase() + k.slice(1)),
      datasets: [{
        data, backgroundColor: order.map((k) => RISK_COLORS[k]),
        borderColor: "#fff", borderWidth: 2, hoverOffset: 6,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: "62%",
      animation: { duration: 300 },
      plugins: {
        legend: { position: "right", labels: { color: "#33465f", boxWidth: 12, usePointStyle: true, padding: 12, font: { size: 11 } } },
        tooltip: { backgroundColor: "#0e2c4f", padding: 10, cornerRadius: 8,
          callbacks: { label: (c) => ` ${c.label}: ${c.parsed} (${total ? Math.round((c.parsed / total) * 100) : 0}%)` } },
      },
    },
  };
  upsert("risk", "chart-risk", cfg);
}

function renderForecastChart(summary) {
  const rows = summary.recent_readings || [];
  const t = summary.thresholds || {};
  const pred = summary.prediction || {};
  const horizons = (pred.horizons || []).slice().sort((a, b) => a.horizon_min - b.horizon_min);
  const latest = summary.latest_reading || {};
  const now = clampNum(latest.water_level_cm);

  const tail = rows.slice(-30);
  const tailLabels = tail.map((r) => timeOf(r.server_timestamp));
  const tailWater = tail.map((r) => clampNum(r.water_level_cm));
  const fLabels = horizons.map((h) => `+${h.horizon_min}m`);
  const labels = [...tailLabels, ...fLabels];

  const measured = [...tailWater, ...horizons.map(() => null)];
  const bridge = tail.length ? tail.length - 1 : 0;
  const accepted = [...Array(bridge).fill(null), now,
    ...horizons.map((h) => (h.accepted && h.predicted_level_cm != null ? h.predicted_level_cm : null))];
  const raw = [...Array(bridge).fill(null), now,
    ...horizons.map((h) => clampNum(h.raw_predicted_level_cm))];

  const cfg = {
    type: "line",
    data: {
      labels,
      datasets: [
        { label: "Measured (cm)", data: measured, borderColor: "#2563eb", backgroundColor: "rgba(37,99,235,.10)",
          fill: true, tension: .3, pointRadius: 0, borderWidth: 2.2 },
        { label: "Accepted forecast (cm)", data: accepted, borderColor: "#0d9488", backgroundColor: "rgba(13,148,136,.12)",
          fill: false, tension: .25, spanGaps: false, pointRadius: 3, pointBackgroundColor: "#0d9488", borderWidth: 2.4 },
        { label: "Raw model output (cm)", data: raw, borderColor: "#ea580c", borderDash: [6, 4],
          fill: false, tension: .25, spanGaps: true, pointRadius: 3, pointBackgroundColor: "#ea580c", borderWidth: 1.8 },
        ...thresholdDatasets(labels, t),
      ],
    },
    options: baseOptions(),
  };
  upsert("forecast", "chart-forecast", cfg);
}

function renderModelChart(test) {
  const keys = Object.keys(test || {}).map(Number).sort((a, b) => a - b);
  const labels = keys.map((k) => `+${k} min`);
  const cfg = {
    type: "bar",
    data: {
      labels,
      datasets: [
        { label: "MAE (cm)", data: keys.map((k) => test[k].mae_cm), backgroundColor: "rgba(37,99,235,.75)", borderRadius: 5, maxBarThickness: 34 },
        { label: "RMSE (cm)", data: keys.map((k) => test[k].rmse_cm), backgroundColor: "rgba(234,88,12,.75)", borderRadius: 5, maxBarThickness: 34 },
      ],
    },
    options: baseOptions({ scales: {
      x: { ticks: { color: "#61748d", font: { size: 11 } }, grid: { display: false } },
      y: { beginAtZero: true, ticks: { color: "#61748d", font: { size: 10.5 } }, grid: { color: "#eaf0f7" } },
    } }),
  };
  upsert("model", "chart-model", cfg);
}

function renderAllCharts(summary, test) {
  renderWaterChart(summary);
  renderRainChart(summary);
  renderRiskChart(summary);
  renderForecastChart(summary);
  if (test) renderModelChart(test);
}

/* --------------------------------------------------------------- fetching */
async function getJSON(url, opts) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).detail || ""; } catch (e) { /* not JSON */ }
    throw new Error(detail || ("HTTP " + res.status));
  }
  return res.json();
}

async function postJSON(url, body) {
  return getJSON(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

async function pollSummary() {
  try {
    const summary = await getJSON(`/api/dashboard/summary?source=${state.source}`);
    state.lastSummary = summary;
    state.thresholds = summary.thresholds || null;
    showError("");
    renderConnection(summary);
    renderHero(summary);
    renderKpis(summary);
    renderMonitoring(summary);
    renderPredictions(summary);
    renderAllCharts(summary, null);
  } catch (err) {
    showError("Cannot reach the backend API (" + err.message + "). Is the server running? " +
      "Close this tab and relaunch run_project.bat.");
  }
}

async function pollAux() {
  try {
    const [health, model, notif, emailStatus, emailHist, espStatus] = await Promise.all([
      getJSON("/health").catch(() => null),
      getJSON("/api/predictions/model").catch(() => null),
      getJSON("/api/alerts/notification-status").catch(() => null),
      getJSON("/api/email/status").catch(() => null),
      getJSON("/api/email/history?limit=25").catch(() => null),
      getJSON("/api/esp/status").catch(() => null),
    ]);
    state.model = model;
    state.health = health;
    state.notification = notif;
    const email = emailStatus || (state.lastSummary && state.lastSummary.email) || null;
    if (email) email.history = (emailHist && emailHist.emails) || [];
    state.email = email;
    state.esp = espStatus || (state.lastSummary && state.lastSummary.esp32) || null;
    renderSystem(health, model, notif, state.lastSummary);
    renderPerformance(model);
    renderEmailStatus(email);
    renderEsp(state.esp);
    if (state.lastSummary) renderAlerts(state.lastSummary.alerts, notif);
  } catch (err) {
    /* individual catches above keep this quiet */
  }
}

async function pollAlerts() {
  const f = state.alertFilters;
  const qs = new URLSearchParams({ limit: String(f.limit) });
  if (f.origin) qs.set("origin_like", f.origin);
  try {
    let data = await getJSON("/api/alerts/history?" + qs.toString());
    let alerts = data.alerts || [];
    if (f.risk) alerts = alerts.filter((a) => (a.risk_level || "").toLowerCase() === f.risk);
    renderAlerts(alerts, state.notification);
  } catch (err) {
    /* keep old table; error banner already covers backend down */
  }
}

async function pollHistorical() {
  const f = state.histFilters;
  const qs = new URLSearchParams({ limit: String(f.limit), source: state.source, only_valid: "true" });
  try {
    const data = await getJSON("/api/sensors/recent?" + qs.toString());
    let rows = (data.readings || []);
    const fromTs = f.from ? new Date(f.from).getTime() : null;
    const toTs = f.to ? new Date(f.to).getTime() : null;
    if (fromTs !== null || toTs !== null) {
      rows = rows.filter((r) => {
        const ts = r.created_epoch != null ? r.created_epoch * 1000 : new Date(r.server_timestamp).getTime();
        if (fromTs !== null && ts < fromTs) return false;
        if (toTs !== null && ts > toTs) return false;
        return true;
      });
    }
    const t = state.thresholds || {};
    const tb = $("hist-table");
    if (!rows.length) {
      tb.innerHTML = `<tr><td class="empty" colspan="9">No readings match the filter.</td></tr>`;
    } else {
      tb.innerHTML = rows.map((r) => `
        <tr>
          <td>${dateTimeOf(r.server_timestamp)}</td>
          <td>${esc(r.device_id)}</td>
          <td>${esc(r.source)}</td>
          <td class="num">${fmt(r.water_level_cm, 2)}</td>
          <td class="num">${fmt(r.ultrasonic_distance_cm, 1)}</td>
          <td class="num">${fmtInt(r.rainfall_reading)}</td>
          <td class="num">${fmt(r.rainfall_intensity, 0)}</td>
          <td class="num">${fmtInt(r.water_sensor_raw)}</td>
          <td>${r.valid ? '<span class="tag tag-ok">yes</span>' : '<span class="tag tag-bad">no</span>'}</td>
        </tr>`).join("");
    }
    $("hist-summary").textContent =
      `Showing ${rows.length} reading(s) · source ${state.source === "test" ? "TEST" : "LIVE"}` +
      (f.from || f.to ? ` · range ${f.from || "start"} → ${f.to || "now"}` : "") + ".";
  } catch (err) {
    $("hist-summary").textContent = "Could not load readings: " + err.message;
  }
}

/* ----------------------------------------------------------- poll cycle */
function pollAll() {
  if (state.busy) return;
  state.busy = true;
  Promise.allSettled([
    pollSummary(),
    pollAlerts(),
    (state.section === "historical") ? pollHistorical() : Promise.resolve(),
  ]).finally(() => { state.busy = false; });
}

function startTimers() {
  stopTimers();
  state.timers.summary = setInterval(pollAll, POLL_MS);
  state.timers.aux = setInterval(pollAux, AUX_MS);
}
function stopTimers() {
  Object.values(state.timers).forEach(clearInterval);
  state.timers = {};
}

/* -------------------------------------------------------------- controls */
function setSource(src) {
  state.source = src;
  const isTest = src === "test";
  $("btn-src-live").classList.toggle("is-active", !isTest);
  $("btn-src-test").classList.toggle("is-active", isTest);
  $("sb-source").textContent = isTest ? "TEST" : "LIVE";
  // Keep the persistent banner in sync so test data is never mistaken for live.
  const banner = $("test-banner");
  if (banner) banner.hidden = !isTest;
  document.body.classList.toggle("is-test-mode", isTest);
  toast(isTest
    ? "Showing <b>TEST</b> scenario data (source=test)."
    : "Switched to <b>LIVE</b> ESP32 data.", "info", 2600);
  pollAll();
  if (state.section === "historical") pollHistorical();
}

async function runScenario(scenario, button) {
  const label = scenario.charAt(0).toUpperCase() + scenario.slice(1);
  if (button) button.disabled = true;
  try {
    const data = await getJSON("/api/test/scenario", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenario, device_id: "test-rig-01" }),
    });
    setSource("test");
    const finalRisk = (data.final && data.final.overall_risk) || (data.final && data.final.current_risk) || "?";

    // The backend also sends a clearly-marked [TEST] demo email so the operator
    // can verify layout + SMTP. Report the outcome honestly in the toast.
    const em = data.email || null;
    let emailLine;
    if (!em) {
      emailLine = "<span class='muted'>No demo email reported.</span>";
    } else if (em.sent) {
      emailLine = "📩 Demo email sent &mdash; <b>" + esc(em.subject || "[TEST]") + "</b>";
    } else {
      emailLine = "📩 Demo email not sent (" + esc(em.status || em.failure_reason || "not configured") + ")";
    }

    toast(`Injected <b>${esc(label)}</b> scenario · result risk: <b>${esc(finalRisk)}</b>.<br>${emailLine}`,
      em && em.sent ? "success" : "info", 7000);
    pollAll();
  } catch (err) {
    toast("Test scenario failed: " + esc(err.message), "error", 6000);
  } finally {
    if (button) button.disabled = false;
  }
}

/* Send a standalone [TEST] demo email (no scenario data injected). */
async function sendTestEmail(scenario) {
  const btn = $("email-send-test");
  if (btn) btn.disabled = true;
  try {
    const data = await getJSON("/api/email/send-test?scenario=" + encodeURIComponent(scenario || "warning"),
      { method: "POST" });
    if (data.sent) {
      toast("Demo email sent &mdash; <b>" + esc(data.subject || "[TEST]") + "</b>", "success", 7000);
    } else {
      toast("Demo email not sent: " + esc(data.error || data.status || "unknown"), "error", 7000);
    }
    pollAux();
  } catch (err) {
    toast("Demo email failed: " + esc(err.message), "error", 7000);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function resetTest() {
  try {
    await getJSON("/api/test/reset", { method: "POST" });
    toast("Test data cleared (live ESP32 rows untouched).", "success");
    setSource("esp32");
    pollAll();
  } catch (err) {
    toast("Reset failed: " + esc(err.message), "error", 6000);
  }
}

function manualRefresh() {
  const btn = $("refresh-btn");
  const svg = btn.querySelector("svg");
  svg.classList.add("spin");
  Promise.allSettled([pollFull()]).finally(() => setTimeout(() => svg.classList.remove("spin"), 400));
}
async function pollFull() {
  await Promise.allSettled([pollSummary(), pollAux(), pollAlerts(),
    state.section === "historical" ? pollHistorical() : Promise.resolve()]);
}

function toggleAuto(on) {
  state.auto = on;
  if (on) { startTimers(); toast("Auto-refresh on (every 3 s).", "info", 2200); }
  else { stopTimers(); toast("Auto-refresh paused.", "info", 2200); }
}

/* ----------------------------------------------------------------- clock */
function tickClock() {
  $("clock").textContent = new Date().toLocaleTimeString();
}

/* ------------------------------------------------------------------- init */
function init() {
  // Nav
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.addEventListener("click", (e) => { e.preventDefault(); setSection(item.dataset.target, { scroll: true }); });
  });
  $("menu-btn").addEventListener("click", () => {
    $("sidebar").classList.contains("open") ? closeSidebar() : openSidebar();
  });
  $("scrim").addEventListener("click", closeSidebar);

  // Header controls
  $("btn-src-live").addEventListener("click", () => setSource("esp32"));
  $("btn-src-test").addEventListener("click", () => setSource("test"));
  if ($("test-banner-exit")) $("test-banner-exit").addEventListener("click", resetTest);
  $("refresh-btn").addEventListener("click", manualRefresh);
  $("auto-toggle").addEventListener("change", (e) => toggleAuto(e.target.checked));

  // Test scenario buttons
  document.querySelectorAll("[data-scenario]").forEach((b) => {
    b.addEventListener("click", () => runScenario(b.dataset.scenario, b));
  });
  $("btn-reset").addEventListener("click", resetTest);

  // ESP32 pull-mode controls
  const espHost = $("esp-host");
  if (espHost) {
    espHost.addEventListener("input", () => { espHost.dataset.touched = "1"; });
    espHost.addEventListener("blur", () => { espHost.dataset.touched = ""; });
    espHost.addEventListener("keydown", (e) => { if (e.key === "Enter") espSave(); });
  }
  if ($("esp-save")) $("esp-save").addEventListener("click", espSave);
  if ($("esp-test")) $("esp-test").addEventListener("click", espTest);
  if ($("esp-clear")) $("esp-clear").addEventListener("click", espClear);

  // Demo-email button in the Email notifications panel
  if ($("email-send-test")) {
    $("email-send-test").addEventListener("click", () => sendTestEmail($("email-send-scenario").value));
  }

  // Historical filters
  $("hist-apply").addEventListener("click", () => {
    state.histFilters = { from: $("hist-from").value, to: $("hist-to").value, limit: Number($("hist-limit").value) || 300 };
    pollHistorical();
  });
  $("hist-clear").addEventListener("click", () => {
    $("hist-from").value = ""; $("hist-to").value = "";
    state.histFilters = { from: "", to: "", limit: Number($("hist-limit").value) || 300 };
    pollHistorical();
  });

  // Alert filters
  $("alert-apply").addEventListener("click", () => {
    state.alertFilters = { risk: $("alert-risk").value, origin: $("alert-origin").value.trim(), limit: Number($("alert-limit").value) || 100 };
    pollAlerts();
  });
  $("alert-risk").addEventListener("change", () => $("alert-apply").click());
  $("alert-limit").addEventListener("change", () => $("alert-apply").click());

  if (!chartsAvailable()) {
    toast("Chart.js could not be loaded from the CDN (offline?). Live data and tables still work.", "warn", 8000);
  }

  tickClock();
  setInterval(tickClock, 1000);

  // First load
  pollFull().then(() => startTimers());
  setSection("overview");
}

document.addEventListener("DOMContentLoaded", init);
