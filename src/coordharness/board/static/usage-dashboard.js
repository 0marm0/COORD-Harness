(function () {
  "use strict";

  const CONTRACT = "coordharness.usage-intelligence.v1";
  const PROVIDERS = ["claude", "codex"];
  const ACCOUNT_CONTRACT = "coord.usage-account-actions.v1";
  const ACCOUNT_STATUS_PATH = "/api/v1/usage-actions/status";
  const ACCOUNT_ACTION_PATH = "/api/v1/usage-actions";
  const ACCOUNT_ACTION_HEADER = "X-Coord-Usage-Action";
  const CODEX_STATES = new Set(["idle", "starting", "waiting_browser", "completed", "failed", "cancelled", "expired", "unavailable"]);
  const CLAUDE_STATES = new Set(["connected", "sign_in_required", "waiting_user", "manual_connect_required", "unavailable"]);
  const STALE_PROVIDER_STATES = new Set(["stale", "stale_last_good", "stale_last_good_no_current_windows", "quota_observation_expired", "quota_observation_unavailable"]);
  const DAY_MS = 86_400_000;
  const historyStore = new Map();
  const historyRanges = new Map();
  let warmingRetries = 0;
  let lastGoodPayload = null;
  let lastStripPayload = null;
  let lastSystemTelemetry = null;
  let lastSystemPreferences = {cpu: true, gpu: true, memory: true, disk: true};
  const SYSTEM_METRICS = [
    {key: "cpu", label: "CPU", field: "usage_percent"},
    {key: "gpu", label: "GPU", field: "usage_percent"},
    {key: "memory", label: "RAM", field: "used_percent"},
    {key: "disk", label: "Disk", field: "used_percent"},
  ];
  const $ = selector => document.querySelector(selector);
  const escapeHTML = value => String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[character]));
  const hasDonorBranding = value => String(value ?? "").toLowerCase().replace(/[^a-z0-9]/g, "").includes("codexbar");
  const neutralCompatibilityText = value => String(value ?? "")
    .replace(/claude_codexbar_[a-z0-9_]*/gi, "legacy compatibility source")
    .replace(/codexbar_[a-z0-9_]*/gi, "legacy compatibility source")
    .replace(/codex\s*bar/gi, "legacy compatibility source")
    .replace(/legacy compatibility source source/gi, "legacy compatibility source");
  const visibleSourceText = (kind, label, fallback = "unknown source") => {
    const candidate = known(label) ? String(label) : known(kind) ? String(kind).replaceAll("_", " ") : fallback;
    return hasDonorBranding(candidate) || hasDonorBranding(kind) ? "Legacy compatibility source" : candidate;
  };
  const visibleErrorCode = code => hasDonorBranding(code)
    ? "Legacy compatibility source unavailable"
    : String(code || "unknown_error");
  const known = value => value !== null && value !== undefined && value !== "";
  const number = value => {
    const parsed = Number(value);
    return known(value) && Number.isFinite(parsed) ? parsed : null;
  };
  const integer = value => {
    const parsed = number(value);
    return parsed === null ? "Unknown" : Math.round(parsed).toLocaleString();
  };
  const tokens = value => {
    const parsed = number(value);
    return parsed === null ? "Unknown" : `${Math.round(parsed).toLocaleString()} tokens`;
  };
  const duration = value => {
    const seconds = number(value);
    if (seconds === null || seconds < 0) return "Unknown";
    if (seconds < 60) return `${Math.round(seconds)}s`;
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    if (hours < 48) return `${hours}h ${minutes % 60}m`;
    return `${Math.floor(hours / 24)}d ${hours % 24}h`;
  };
  const timestamp = value => {
    if (!known(value)) return "Unknown";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString();
  };
  const shortDate = value => {
    const parsed = new Date(`${String(value).slice(0, 10)}T00:00:00Z`);
    return Number.isNaN(parsed.getTime()) ? String(value) : new Intl.DateTimeFormat(undefined, {month: "short", day: "numeric", timeZone: "UTC"}).format(parsed);
  };
  const costValue = (amount, currencyValue) => {
    const currency = String(currencyValue || "").toUpperCase();
    if (!/^[A-Z]{3}$/.test(currency)) return `${(amount / 1_000_000_000).toLocaleString(undefined, {maximumFractionDigits: 4})} units`;
    try {
      return new Intl.NumberFormat(undefined, {style: "currency", currency, maximumFractionDigits: 2}).format(amount / 1_000_000_000);
    } catch (_error) {
      return `${currency} ${(amount / 1_000_000_000).toLocaleString()}`;
    }
  };
  const cost = item => {
    const amount = number(item && item.amount_nanos);
    if (amount !== null) return costValue(amount, item.currency);
    const currencies = item && item.by_currency && typeof item.by_currency === "object"
      ? Object.entries(item.by_currency).filter(([, value]) => number(value) !== null).sort(([left], [right]) => left.localeCompare(right))
      : [];
    return currencies.length ? currencies.map(([currency, value]) => costValue(number(value), currency)).join(" + ") : "Unknown";
  };
  const usdAmount = item => {
    if (!item || typeof item !== "object") return null;
    const retained = item.by_currency && typeof item.by_currency === "object"
      ? Object.entries(item.by_currency).find(([currency]) => String(currency).toUpperCase() === "USD")
      : null;
    const byCurrency = number(retained && retained[1]);
    if (byCurrency !== null) return byCurrency;
    return String(item.currency || "").toUpperCase() === "USD" ? number(item.amount_nanos) : null;
  };
  const sourceLabel = provider => {
    const source = provider.source || {};
    return `${visibleSourceText(source.kind, source.label)} · ${source.canonical === true ? "canonical" : "noncanonical"}`;
  };

  function normalizedDailyCost(daily, range) {
    const byDate = new Map();
    (Array.isArray(daily) ? daily : []).forEach(row => {
      const date = String(row && row.date || "").slice(0, 10);
      const time = Date.parse(`${date}T00:00:00Z`);
      const providerNative = number(row && row.provider_native_cost_nanos);
      const apiEstimate = number(row && row.api_rate_estimate_nanos);
      const amount = apiEstimate !== null && apiEstimate >= 0 ? apiEstimate : providerNative !== null && providerNative >= 0 ? providerNative : null;
      const kind = apiEstimate !== null && apiEstimate >= 0 ? "API-rate estimate" : "provider-native";
      if (Number.isFinite(time) && amount !== null) byDate.set(date, {date, time, amount, kind, providerNative, apiEstimate});
    });
    let points = [...byDate.values()].sort((left, right) => left.time - right.time);
    if (points.length && range !== "all") {
      const days = Number(range);
      const cutoff = points[points.length - 1].time - (days - 1) * DAY_MS;
      points = points.filter(point => point.time >= cutoff);
    }
    const spanDays = points.length > 1 ? Math.round((points[points.length - 1].time - points[0].time) / DAY_MS) + 1 : points.length;
    return {points, missingDays: Math.max(0, spanDays - points.length), spanDays};
  }

  function dailyCostPointLabel(point, currencies = {}) {
    const currency = point.kind === "API-rate estimate" ? currencies.api : currencies.native;
    const labels = [point.date + ": " + point.kind + " " + costValue(point.amount, currency)];
    if (point.providerNative !== null && point.apiEstimate !== null) labels.push("provider-native " + costValue(point.providerNative, currencies.native));
    if (point.kind === "API-rate estimate") labels.push("not billed spend");
    return labels.join(" · ");
  }

  function historyGraph(chartID, daily, range, currencies = {}, metadata = {}) {
    const {points, missingDays, spanDays} = normalizedDailyCost(daily, range);
    if (!points.length) {
      return '<div class="usage-graph-empty" role="status">Daily cost history unavailable for this source.</div>';
    }
    const width = 560, height = 176, left = 34, right = 14, top = 18, bottom = 34;
    const plotWidth = width - left - right, plotHeight = height - top - bottom;
    const maximum = Math.max(1, ...points.map(point => point.amount));
    const minimumTime = points[0].time, maximumTime = points[points.length - 1].time;
    const dayWidth = plotWidth / Math.max(1, spanDays), barWidth = Math.max(0.8, Math.min(10, dayWidth * 0.72));
    const coordinates = points.map(point => ({
      ...point,
      x: left + ((point.time - minimumTime) / Math.max(DAY_MS, maximumTime - minimumTime)) * Math.max(0, plotWidth - barWidth),
      barHeight: Math.max(1, point.amount / maximum * plotHeight),
    }));
    const authority = metadata.canonical === true
      ? (missingDays ? "canonical rows; calendar coverage incomplete" : "canonical rows")
      : metadata.sourceKey === "reported" ? "provider-reported; completeness not guaranteed" : "retained local envelope; noncanonical and incomplete";
    const description = `${points.length} observed daily cost rows across ${spanDays} calendar days from ${points[0].date} through ${points[points.length - 1].date}; ${missingDays} day${missingDays === 1 ? "" : "s"} missing; ${authority}.`;
    return `<div class="usage-history-figure"><div class="usage-history-caption"><span><b>Dollars per observed day</b> · ${escapeHTML(shortDate(points[0].date))} → ${escapeHTML(shortDate(points[points.length - 1].date))}</span><span>${points.length}/${spanDays} observed · ${missingDays} missing</span></div><svg class="usage-history" viewBox="0 0 ${width} ${height}" role="img" aria-labelledby="${chartID}-title ${chartID}-desc">
      <title id="${chartID}-title">Daily estimated cost</title><desc id="${chartID}-desc">${escapeHTML(description)}</desc>
      <line class="usage-axis" x1="${left}" y1="${height - bottom}" x2="${width - right}" y2="${height - bottom}"></line>
      <line class="usage-grid" x1="${left}" y1="${top}" x2="${width - right}" y2="${top}"></line>
      ${coordinates.map(point => `<rect class="usage-history-bar" x="${point.x.toFixed(1)}" y="${(top + plotHeight - point.barHeight).toFixed(1)}" width="${barWidth.toFixed(1)}" height="${point.barHeight.toFixed(1)}" rx="1" tabindex="0" aria-label="${escapeHTML(dailyCostPointLabel(point, currencies))}"><title>${escapeHTML(dailyCostPointLabel(point, currencies))}</title></rect>`).join("")}
      <text class="usage-axis-label" x="${left}" y="${height - 10}">${escapeHTML(shortDate(points[0].date))}</text><text class="usage-axis-label" x="${width - right}" y="${height - 10}" text-anchor="end">${escapeHTML(shortDate(points[points.length - 1].date))}</text>
    </svg><p class="usage-coverage-note ${metadata.canonical === true && !missingDays ? "" : "warn"}">${escapeHTML(description)} Missing days are not plotted as zero.</p></div>`;
  }

  function metric(label, value, detail) {
    return `<div class="usage-metric"><span>${escapeHTML(label)}</span><strong>${escapeHTML(value)}</strong>${detail ? `<small>${escapeHTML(detail)}</small>` : ""}</div>`;
  }

  function historyPanel(providerKey, sourceKey, title, history, fallbackSemantics, currencies = {}, canonical = false) {
    history = history && typeof history === "object" ? history : {};
    const chartID = `usage-${providerKey}-${sourceKey}`;
    const daily = Array.isArray(history.daily) ? history.daily : [];
    const metadata = {sourceKey, canonical};
    historyStore.set(chartID, {daily, currencies, metadata});
    const range = historyRanges.get(chartID) || "all";
    const semantics = neutralCompatibilityText(history.semantics || fallbackSemantics);
    return `<article class="usage-history-panel usage-history-${sourceKey}"><header><div><h4>${escapeHTML(title)}</h4><p>${escapeHTML(semantics || "source unavailable")}</p></div><div class="usage-range" role="group" aria-label="${escapeHTML(title)} range">${["7", "30", "all"].map(value => `<button type="button" data-history-id="${chartID}" data-history-range="${value}" class="${range === value ? "active" : ""}">${value === "all" ? "All" : `${value}d`}</button>`).join("")}</div></header><div class="usage-metrics">
      ${metric("Today", tokens(history.today_total_tokens), semantics)}${metric("Calendar week", tokens(history.calendar_week_total_tokens), semantics)}${metric("Rolling 7 days", tokens(history.rolling_7d_total_tokens), semantics)}${metric(sourceKey === "reported" ? "Account lifetime" : number(history.all_time_total_tokens) !== null ? "All-time" : "Retained total", tokens(history.all_time_total_tokens), semantics)}
    </div><div class="usage-history-slot" data-history-slot="${chartID}">${historyGraph(chartID, daily, range, currencies, metadata)}</div></article>`;
  }

  function paceMarkup(window) {
    const pace = window.pace && typeof window.pace === "object" ? window.pace : {};
    if (!["reserve", "deficit", "on_pace"].includes(pace.state)) return "";
    const state = pace.state === "on_pace" ? "On pace" : pace.state === "reserve" ? "Reserve" : "Deficit";
    const details = [];
    const delta = number(pace.delta_percent), expected = number(pace.expected_used_percent);
    if (delta !== null) details.push(delta.toLocaleString() + " points from expected");
    if (expected !== null) details.push(expected.toLocaleString() + "% expected used now");
    details.push(
      pace.source === "codexbar_local_projection"
        ? "Compatibility local advisory projection"
        : pace.source === "local_projection"
          ? "Local advisory projection"
          : "local advisory projection"
    );
    if (pace.will_last_to_reset === true) details.push("projected to last until reset");
    if (pace.will_last_to_reset === false) details.push("may run out before reset");
    if (number(pace.seconds_to_exhaustion) !== null) details.push("estimated exhaustion in " + duration(pace.seconds_to_exhaustion));
    return '<p class="usage-pace usage-pace-' + escapeHTML(pace.state) + '"><strong>' + state + "</strong>" + (details.length ? " · " + escapeHTML(details.join(" · ")) : "") + "</p>";
  }


  function windowRow(window) {
    const used = number(window.used_percent);
    const reportedRemaining = number(window.remaining_percent);
    const remaining = reportedRemaining !== null ? reportedRemaining : (used === null ? null : 100 - used);
    const width = remaining === null ? null : Math.max(0, Math.min(100, remaining));
    const remainingLabel = width === null ? "Remaining unknown" : `${width.toLocaleString()}% left`;
    return `<div class="usage-window" data-window-kind="${escapeHTML(window.kind || "bucket")}"><div class="usage-window-head"><strong>${escapeHTML(window.name || window.kind || "Window")}</strong><span>${remainingLabel}</span></div><div class="usage-quota-track" role="meter" aria-valuemin="0" aria-valuemax="100"${width === null ? "" : ` aria-valuenow="${escapeHTML(width)}"`} aria-label="${escapeHTML(`${window.name || window.kind || "Usage window"} remaining`)}"><i${width === null ? "" : ` data-usage-width="${width}"`}></i></div><div class="usage-window-meta"><span>${used === null ? "Used amount unknown" : `${Math.max(0, Math.min(100, used)).toLocaleString()}% used`}</span><span>${known(window.countdown_seconds) ? `resets in ${duration(window.countdown_seconds)}` : "reset unknown"}${known(window.resets_at) ? ` · ${timestamp(window.resets_at)}` : ""}</span></div>${paceMarkup(window)}</div>`;
  }

  function quotaGroup(group) {
    const windows = Array.isArray(group.windows) ? group.windows : [];
    const runout = group.runout || {};
    const forecast = runout.advisory === true
      ? `${known(runout.seconds_to_exhaustion) ? `${duration(runout.seconds_to_exhaustion)} to estimated exhaustion` : "ETA unavailable"}${runout.basis ? ` · ${neutralCompatibilityText(runout.basis)}` : ""}`
      : "No advisory ETA";
    return `<article class="usage-quota-group" data-quota-group="${escapeHTML(group.key || "account")}"><header><div><h4>${escapeHTML(neutralCompatibilityText(group.label || "Account quota"))}</h4><p>${escapeHTML(neutralCompatibilityText(group.semantics || "provider quota meter"))}</p></div><span>${escapeHTML(forecast)}</span></header>${windows.length ? windows.map(windowRow).join("") : '<p class="usage-unknown">Quota windows unavailable for this meter.</p>'}</article>`;
  }

  function breakdownSection(title, group) {
    group = group && typeof group === "object" ? group : {};
    const items = Array.isArray(group.items) ? group.items.slice(0, 20) : [];
    const context = [group.canonical === true ? "canonical" : neutralCompatibilityText(group.semantics || "source unavailable"), group.coverage_start && group.coverage_end ? `${String(group.coverage_start).slice(0, 10)} → ${String(group.coverage_end).slice(0, 10)}` : "", group.observed_at ? `observed ${timestamp(group.observed_at)}` : ""].filter(Boolean).join(" · ");
    if (!items.length) return `<section class="usage-section usage-breakdown"><h3>${escapeHTML(title)}</h3><p class="usage-meta">${escapeHTML(context || "No attributed rows")}</p></section>`;
    const visible = items.slice(0, 10), maximum = Math.max(1, ...visible.map(item => number(item.total_tokens) || 0));
    const hidden = Math.max(0, (number(group.omitted_count) || 0) + items.length - visible.length);
    return `<section class="usage-section usage-breakdown"><div class="usage-breakdown-head"><h3>${escapeHTML(title)}</h3><span>${escapeHTML(context)}</span></div><div class="usage-breakdown-list">${visible.map(item => {
      const total = number(item.total_tokens) || 0, width = Math.max(1, total / maximum * 100);
      const extra = item.top_model ? `top ${item.top_model}` : "";
      return `<div class="usage-breakdown-row"><div><strong>${escapeHTML(item.label || "Unattributed")}</strong><small>coverage ${escapeHTML(tokens(item.total_tokens))} · today ${escapeHTML(tokens(item.today_total_tokens))} · 7d ${escapeHTML(tokens(item.rolling_7d_total_tokens))} · week ${escapeHTML(tokens(item.calendar_week_total_tokens))}${extra ? ` · ${escapeHTML(extra)}` : ""}</small></div><i><em data-usage-width="${width}"></em></i></div>`;
    }).join("")}</div>${hidden ? `<p class="usage-meta">+${integer(hidden)} lower-ranked rows retained by the producer</p>` : ""}</section>`;
  }

  function usageOverview(provider, observedDay) {
    const history = provider.history || {}, daily = Array.isArray(history.daily) ? history.daily : [];
    const latest = daily.find(row => String(row && row.date || "").slice(0, 10) === observedDay) || {};
    const envelope = history.ever_observed_envelope || {}, estimate = number(latest.api_rate_estimate_nanos), retainedEstimate = number(provider.costs && provider.costs.api_rate_estimate && provider.costs.api_rate_estimate.amount_nanos);
    const apiCurrency = provider.costs && provider.costs.api_rate_estimate && provider.costs.api_rate_estimate.currency;
    const items = [];
    if (number(history.today_total_tokens) !== null) items.push(["Today tokens", tokens(history.today_total_tokens), "latest canonical day"]);
    if (estimate !== null) items.push(["Today API estimate", costValue(estimate, apiCurrency), String(latest.date || "latest day") + " · not billed spend"]);
    if (number(envelope.total_tokens) !== null) items.push(["Ever-observed tokens", tokens(envelope.total_tokens), "custody envelope; not canonical all-time"]);
    else if (number(history.all_time_total_tokens) !== null) items.push(["All-time tokens", tokens(history.all_time_total_tokens), neutralCompatibilityText(history.semantics || "retained coverage")]);
    if (retainedEstimate !== null) items.push(["API estimate · retained high-water", costValue(retainedEstimate, apiCurrency), "not provider-billed spend"]);
    if (!items.length) return "";
    return '<section class="usage-overview" aria-label="Usage overview">' + items.map(item => '<div><span>' + escapeHTML(item[0]) + '</span><strong>' + escapeHTML(item[1]) + '</strong><small>' + escapeHTML(item[2]) + "</small></div>").join("") + "</section>";
  }

  function primaryQuotaGroup(groups, provider) {
    const source = groups.length ? groups : (Array.isArray(provider.windows) && provider.windows.length
      ? [{key: "compatibility", label: "Account quota", windows: provider.windows}]
      : []);
    const isAccount = group => String(group && group.key || "").toLowerCase().includes("account")
      || String(group && group.label || "").toLowerCase().includes("account");
    const hasSession = group => (Array.isArray(group && group.windows) ? group.windows : [])
      .some(window => String(window && window.kind || "").toLowerCase() === "session");
    return source.find(isAccount) || source.find(hasSession)
      || source[0]
      || null;
  }

  function compactWindow(label, window) {
    if (!window || typeof window !== "object") return "";
    const used = number(window && window.used_percent);
    const reportedRemaining = number(window && window.remaining_percent);
    const remaining = reportedRemaining !== null ? reportedRemaining : (used === null ? null : 100 - used);
    const width = remaining === null ? null : Math.max(0, Math.min(100, remaining));
    const reset = number(window && window.countdown_seconds) !== null
      ? `Resets in ${duration(window.countdown_seconds)}`
      : known(window && window.resets_at) ? `Resets ${timestamp(window.resets_at)}` : "Reset unavailable";
    return `<div class="usage-compact-window" data-window-kind="${escapeHTML(String(window && window.kind || label).toLowerCase())}"><div><strong>${escapeHTML(label)}</strong><span>${width === null ? "Unavailable" : `${width.toLocaleString()}% left`}</span></div><i role="meter" aria-label="${escapeHTML(label)} quota remaining" aria-valuemin="0" aria-valuemax="100"${width === null ? "" : ` aria-valuenow="${escapeHTML(width)}"`}><em${width === null ? "" : ` data-usage-width="${width}"`}></em></i><small>${escapeHTML(reset)}</small></div>`;
  }

  function latestDailyCost(provider) {
    const costs = provider && provider.costs || {};
    const points = normalizedDailyCost(provider && provider.history && provider.history.daily, "all").points;
    const point = points[points.length - 1];
    if (!point) return "Unknown";
    const entry = point.kind === "API-rate estimate" ? costs.api_rate_estimate : costs.provider_native;
    return costValue(point.amount, entry && entry.currency);
  }

  function compactProviderSummary(provider, groups) {
    const group = primaryQuotaGroup(groups, provider);
    const windows = group && Array.isArray(group.windows) ? group.windows : [];
    const session = windows.find(window => String(window.kind || "").toLowerCase() === "session");
    const weekly = windows.find(window => String(window.kind || "").toLowerCase() === "weekly");
    const estimate = usdAmount(provider.costs && provider.costs.api_rate_estimate);
    const usdEstimate = estimate === null ? "Unknown" : costValue(estimate, "USD");
    return `<section class="usage-compact-summary" aria-label="Compact provider usage"><p>${escapeHTML(group && group.label || "Provider quota")}</p>${compactWindow("Session", session)}${compactWindow("Weekly", weekly)}<div class="usage-compact-metrics"><div><span>Today est.</span><strong>${escapeHTML(latestDailyCost(provider))}</strong><small>Daily cost estimate · not billed</small></div><div><span>Total Cost Est.</span><strong>${escapeHTML(usdEstimate)}</strong><small>Cumulative API-rate estimate · not billed</small></div></div></section>`;
  }

  function stripProviderData(providerKey, provider) {
    const groups = Array.isArray(provider.quota_groups) ? provider.quota_groups : [];
    const group = primaryQuotaGroup(groups, provider);
    const windows = group && Array.isArray(group.windows) ? group.windows : [];
    const session = windows.find(window => String(window && window.kind || "").toLowerCase() === "session");
    const weekly = windows.find(window => String(window && window.kind || "").toLowerCase() === "weekly");
    const fableGroup = groups.find(candidate => `${candidate && candidate.key || ""} ${candidate && candidate.label || ""}`.toLowerCase().includes("fable"));
    const fable = fableGroup && Array.isArray(fableGroup.windows) ? fableGroup.windows[0] : null;
    const remaining = window => {
      const reported = number(window && window.remaining_percent), used = number(window && window.used_percent);
      return reported !== null ? Math.max(0, Math.min(100, reported)) : used === null ? null : Math.max(0, Math.min(100, 100 - used));
    };
    const estimate = usdAmount(provider.costs && provider.costs.api_rate_estimate);
    return {
      providerKey,
      summaryWindows: [["S", session], ["W", weekly]].filter(([, window]) => window),
      windows: [["Session", session], ["Weekly", weekly], ["Fable", fable]].filter(([, window]) => window),
      remaining,
      cost: estimate === null ? "Unknown" : costValue(estimate, "USD"),
    };
  }

  function stripQuota(data, label, window) {
    const remaining = data.remaining(window), reset = number(window && window.countdown_seconds), runout = number(window && window.pace && window.pace.seconds_to_exhaustion);
    return `<div class="usage-strip-quota"><span>${escapeHTML(label)}</span><progress max="100"${remaining === null ? "" : ` value="${remaining}"`} aria-label="${escapeHTML(label)} quota remaining"></progress><strong>${remaining === null ? "N/A" : `${Math.round(remaining)}%`}</strong><small>↻ ${reset === null ? "—" : duration(reset)}${runout === null ? "" : ` · out ${duration(runout)}`}</small></div>`;
  }

  function stripMiniQuota(data, label, window) {
    const remaining = data.remaining(window);
    return `<span class="usage-strip-mini"><b>${escapeHTML(label)}</b><progress max="100"${remaining === null ? "" : ` value="${remaining}"`} aria-label="${escapeHTML(data.providerKey)} ${escapeHTML(label)} quota remaining"></progress><strong>${remaining === null ? "N/A" : `${Math.round(remaining)}%`}</strong></span>`;
  }

  function systemPercent(spec) {
    const metric = lastSystemTelemetry && lastSystemTelemetry[spec.key];
    if (!metric || String(metric.availability || "").toLowerCase() !== "available") return null;
    return number(metric[spec.field]);
  }

  function systemLevel(value) {
    return value === null ? "unavailable" : value >= 90 ? "critical" : value >= 70 ? "warning" : "normal";
  }

  function humanBytes(value) {
    const parsed = number(value);
    if (parsed === null || parsed < 0) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let scaled = parsed, unit = 0;
    while (scaled >= 1024 && unit < units.length - 1) { scaled /= 1024; unit += 1; }
    return `${scaled.toLocaleString(undefined, {maximumFractionDigits: scaled >= 100 ? 0 : 1})} ${units[unit]}`;
  }

  function humanRate(value) {
    const formatted = humanBytes(value);
    return formatted === "—" ? formatted : `${formatted}/s`;
  }

  function systemSummaryMarkup() {
    const metrics = SYSTEM_METRICS.filter(spec => lastSystemPreferences[spec.key] !== false).map(spec => {
      const value = systemPercent(spec);
      return `<span class="usage-strip-system-metric ${systemLevel(value)}"><b>${escapeHTML(spec.label)}</b><strong>${value === null ? "N/A" : `${Math.round(value)}%`}</strong></span>`;
    }).join("");
    return `<span class="usage-strip-system" data-system-summary role="group" aria-label="Live system statistics">${metrics || '<span class="usage-strip-unavailable">Stats hidden</span>'}</span>`;
  }

  function systemFacts(spec, metric) {
    if (!metric || typeof metric !== "object") return [];
    if (spec.key === "gpu") return [["Renderer", number(metric.renderer_percent) === null ? "—" : `${Math.round(metric.renderer_percent)}%`], ["Tiler", number(metric.tiler_percent) === null ? "—" : `${Math.round(metric.tiler_percent)}%`]];
    if (spec.key === "memory") return [["Used", humanBytes(metric.used_bytes)], ["Free", humanBytes(metric.free_bytes)], ["Swap", humanBytes(metric.swap_used_bytes)]];
    if (spec.key === "disk") return [["Used", humanBytes(metric.used_bytes)], ["Free", humanBytes(metric.free_bytes)], ["Read", humanRate(metric.read_bps)], ["Write", humanRate(metric.write_bps)]];
    return [];
  }

  function systemDetailsMarkup() {
    const freshness = String(lastSystemTelemetry && lastSystemTelemetry.freshness && lastSystemTelemetry.freshness.state || "unavailable").toLowerCase();
    const cards = SYSTEM_METRICS.map(spec => {
      const metric = lastSystemTelemetry && lastSystemTelemetry[spec.key];
      const value = systemPercent(spec);
      const facts = systemFacts(spec, metric).map(([label, datum]) => `<div><dt>${escapeHTML(label)}</dt><dd>${escapeHTML(datum)}</dd></div>`).join("");
      return `<article class="usage-strip-system-card ${systemLevel(value)}" data-system-card="${escapeHTML(spec.key)}"><header><b>${escapeHTML(spec.label)}</b><strong>${value === null ? "N/A" : `${Math.round(value)}%`}</strong></header>${facts ? `<dl>${facts}</dl>` : ""}</article>`;
    }).join("");
    const toggles = SYSTEM_METRICS.map(spec => `<label><input type="checkbox" data-system-metric="${escapeHTML(spec.key)}"${lastSystemPreferences[spec.key] === false ? "" : " checked"}>${escapeHTML(spec.label)}</label>`).join("");
    return `<section class="usage-strip-system-details" data-system-details aria-label="System statistics details"><header><b>System stats</b><span class="${escapeHTML(freshness)}">${freshness === "fresh" ? "Live" : escapeHTML(freshness)}</span></header><div class="usage-strip-system-grid">${cards}</div><fieldset class="usage-strip-system-toggles"><legend>Summary</legend>${toggles}</fieldset></section>`;
  }

  function bindSystemPreferenceControls(mount) {
    mount.querySelectorAll("[data-system-metric]").forEach(input => input.addEventListener("change", () => {
      window.CoordSystemTelemetryPreferences?.set(input.dataset.systemMetric, input.checked);
    }));
  }

  function renderUsageStrip(payload) {
    lastStripPayload = payload;
    const mount = $("#usage-strip");
    if (!mount) return;
    const providers = payload && payload.providers && typeof payload.providers === "object" ? payload.providers : {};
    const rows = PROVIDERS.filter(key => providers[key] && typeof providers[key] === "object").map(key => stripProviderData(key, providers[key]));
    const expanded = (() => { try { return localStorage.getItem("coord.usage-strip-expanded") === "1"; } catch (_error) { return false; } })();
    const collapsedRows = rows.map(data => `<span class="usage-strip-provider usage-strip-${escapeHTML(data.providerKey)}" role="group" aria-label="${escapeHTML(data.providerKey)} quotas"><img src="/static/mark-${escapeHTML(data.providerKey)}.png" alt="">${data.summaryWindows.map(([label, window]) => stripMiniQuota(data, label, window)).join("")}</span>`).join("");
    const expandedRows = rows.map(data => `<section class="usage-strip-expanded-provider"><header><img src="/static/mark-${escapeHTML(data.providerKey)}.png" alt=""><b>${escapeHTML(data.providerKey)}</b><span class="usage-strip-cost"><small>Cost</small><b>${escapeHTML(data.cost)}</b></span></header>${data.windows.map(([label, window]) => stripQuota(data, label, window)).join("")}</section>`).join("");
    mount.innerHTML = `<details${expanded ? " open" : ""}><summary><span class="usage-strip-label">USAGE</span><span class="usage-strip-providers">${collapsedRows || '<span class="usage-strip-unavailable">Provider quota unavailable</span>'}</span>${systemSummaryMarkup()}</summary><div class="usage-strip-expanded">${expandedRows || '<section class="usage-strip-expanded-provider"><span class="usage-strip-unavailable">Provider quota unavailable</span></section>'}${systemDetailsMarkup()}</div></details>`;
    const details = mount.querySelector("details");
    details && details.addEventListener("toggle", () => { try { localStorage.setItem("coord.usage-strip-expanded", details.open ? "1" : "0"); } catch (_error) {} });
    bindSystemPreferenceControls(mount);
  }

  function setSystemTelemetry(data, preferences) {
    lastSystemTelemetry = data && typeof data === "object" ? data : null;
    lastSystemPreferences = {...lastSystemPreferences, ...(preferences && typeof preferences === "object" ? preferences : {})};
    const mount = $("#usage-strip");
    if (!mount) return;
    const summary = mount.querySelector("[data-system-summary]");
    const details = mount.querySelector("[data-system-details]");
    if (!summary || !details) { renderUsageStrip(lastStripPayload); return; }
    summary.outerHTML = systemSummaryMarkup();
    details.outerHTML = systemDetailsMarkup();
    bindSystemPreferenceControls(mount);
  }

  function providerCard(providerKey, provider, observedDay) {

    const history = provider.history || {}, reported = history.provider_reported_account || {}, breakdowns = provider.breakdowns || {};
    const envelope = history.ever_observed_envelope || {}, costs = provider.costs || {}, active = provider.active_sessions || {}, account = provider.account || {};
    const currencies = {api: (costs.api_rate_estimate || {}).currency, native: (costs.provider_native || {}).currency};
    const credits = Array.isArray(provider.reset_credits) ? provider.reset_credits : [], activeItems = Array.isArray(active.items) ? active.items : [];
    const groups = Array.isArray(provider.quota_groups) && provider.quota_groups.length
      ? provider.quota_groups
      : (Array.isArray(provider.windows) && provider.windows.length ? [{key: "compatibility", label: "Account quota", semantics: "backward-compatible meter", windows: provider.windows, runout: provider.runout || {}}] : []);
    const quotaSource = provider.quota_source && typeof provider.quota_source === "object" ? provider.quota_source : {};
    const quotaAuthority = quotaSource.canonical === true ? "canonical quota" : "noncanonical quota";
    const quotaSourceMarkup = known(quotaSource.label) || known(quotaSource.kind)
      ? '<p class="usage-quota-source">Source: ' + escapeHTML(visibleSourceText(quotaSource.kind, quotaSource.label, "Live quota source")) + " · " + quotaAuthority + (quotaSource.warning ? " · " + escapeHTML(neutralCompatibilityText(quotaSource.warning)) : "") + "</p>"
      : "";
    const quotaSourceKind = String(quotaSource.kind || "").toLowerCase();
    const indirectClaudeLabel = providerKey === "claude" ? ({
      codexbar_widget_snapshot: "Legacy snapshot / fallback",
      codexbar_cli_live: "Compatibility snapshot",
    })[quotaSourceKind] : null;
    const indirectClaudeConnection = known(indirectClaudeLabel);
    const connection = indirectClaudeConnection
      ? indirectClaudeLabel
      : account.authenticated === true ? "Connected" : account.authenticated === false ? "Sign-in required" : "Connection unavailable";
    const directlyConnected = account.authenticated === true && !indirectClaudeConnection;
    const resetCreditInventory = credits.length > 0 && credits.every(credit => credit && credit.semantics === "earned_credit_inventory_not_current_reset_eligibility");
    const resetCreditHeading = resetCreditInventory ? "Earned reset-credit inventory" : "Reset credits";
    const resetCreditRows = credits.map(credit => resetCreditInventory
      ? `<p><strong>${known(credit.count) ? integer(credit.count) : "Count unknown"}</strong> earned credit${credit.count === 1 ? "" : "s"} · current reset eligibility unavailable</p>`
      : `<p><strong>${known(credit.count) ? integer(credit.count) : "Count unknown"}</strong> · ${escapeHTML(credit.status || "status unknown")}</p>`).join("");
    return `<article class="usage-provider usage-provider-${escapeHTML(providerKey)}" data-provider="${escapeHTML(providerKey)}"><header class="usage-provider-head"><div><p class="usage-kicker">Provider account usage</p><h2>${escapeHTML(providerKey)}</h2></div><strong class="usage-connection ${directlyConnected ? "connected" : ""}">${escapeHTML(connection)}</strong></header>${compactProviderSummary(provider, groups)}<details class="usage-disclosure"><summary>Details, provenance &amp; history</summary><div class="usage-detail-body"><div class="usage-detail-identity"><span class="usage-source ${provider.source && provider.source.canonical === true ? "canonical" : "custody"}">${escapeHTML(sourceLabel(provider))}</span><p class="usage-account">${escapeHTML(account.plan || "Plan unknown")} · ${escapeHTML(account.status || "account status unknown")}</p></div>${provider.source && provider.source.warning ? `<p class="usage-warning">${escapeHTML(neutralCompatibilityText(provider.source.warning))}</p>` : ""}${usageOverview(provider, observedDay)}
      <section class="usage-section usage-quota-section" aria-label="Quota meters"><div class="usage-section-head"><div><h3>Quota limits</h3><p>Independent provider meters; session and weekly windows are never paired across groups.</p>${quotaSourceMarkup}</div></div><div class="usage-quota-groups">${groups.length ? groups.map(quotaGroup).join("") : '<p class="usage-unknown">Provider quota meters unavailable.</p>'}</div></section>
      <section class="usage-section usage-history-section"><div class="usage-section-head"><div><h3>Usage and daily cost</h3><p>Token totals remain as metrics; charts show dollars per observed day. Source families are never merged.</p></div></div><div class="usage-history-grid">${historyPanel(providerKey, "retained", provider.source && provider.source.canonical === true ? "Canonical / retained" : "Retained custody", history, "retained history", currencies, provider.source && provider.source.canonical === true)}${historyPanel(providerKey, "reported", "Provider-reported account", reported, "provider-reported account", currencies, false)}</div>${known(envelope.total_tokens) ? `<p class="usage-envelope">Ever-observed custody envelope: <strong>${escapeHTML(tokens(envelope.total_tokens))}</strong>. Component maxima may not describe one coherent observation.</p>` : ""}</section>
      ${breakdownSection("Models", breakdowns.models)}${breakdownSection("Projects", breakdowns.projects)}
      <section class="usage-section"><div class="usage-section-head"><div><h3>Cost semantics</h3><p>Invoice evidence, provider-native values, and API-rate estimates are not interchangeable.</p></div></div><div class="usage-costs">${metric("Provider billed", cost(costs.provider_billed || {}), neutralCompatibilityText((costs.provider_billed || {}).semantics || "invoice/source only"))}${metric("Provider native", cost(costs.provider_native || {}), neutralCompatibilityText((costs.provider_native || {}).semantics || "provider-reported"))}${metric("API-rate estimate", cost(costs.api_rate_estimate || {}), neutralCompatibilityText((costs.api_rate_estimate || {}).semantics || "not subscription spend"))}</div></section>
      ${activeItems.length ? `<section class="usage-section usage-details"><h3>Active sessions</h3>${activeItems.slice(0, 8).map(item => `<p><strong>${escapeHTML(item.provider || providerKey)}</strong> · active ${duration(item.duration_seconds)} · idle ${duration(item.idle_seconds)}</p>`).join("")}</section>` : ""}
      ${credits.length ? `<section class="usage-section usage-details"><h3>${escapeHTML(resetCreditHeading)}</h3>${resetCreditRows}</section>` : ""}
      <footer class="usage-provider-foot"><span>Active sessions: ${known(active.count) ? integer(active.count) : "Unknown"} · ${escapeHTML(active.status || "status unknown")}</span>${Array.isArray(provider.errors) && provider.errors.length ? `<span class="usage-error">${provider.errors.length} provider error${provider.errors.length === 1 ? "" : "s"}</span>` : ""}</footer></div></details></article>`;
  }

  const CODEX_COPY = {
    idle: "Ready to start a secure Codex browser sign-in.",
    starting: "Starting secure Codex sign-in.",
    waiting_browser: "Finish sign-in in the browser window that opened.",
    completed: "Codex sign-in completed. Usage will refresh shortly.",
    failed: "Codex sign-in did not complete. You can try again.",
    cancelled: "Codex sign-in was cancelled.",
    expired: "Codex sign-in expired. Start again when ready.",
    unavailable: "Codex sign-in is unavailable on this device.",
  };
  const CLAUDE_COPY = {
    connected: "Claude is connected through Claude Code via the local provider service.",
    sign_in_required: "Claude needs direct Claude Code sign-in via the local provider service.",
    waiting_user: "Claude Code sign-in is open. Finish the provider-owned browser flow.",
    manual_connect_required: "Open direct Claude Code sign-in via the local provider service.",
    unavailable: "Direct Claude Code sign-in is currently unavailable from the local provider service.",
  };
  const RESULT_COPY = {
    browser_opened: "The secure Codex browser sign-in was opened.",
    login_already_active: "A Codex sign-in is already active.",
    login_start_failed: "Codex sign-in could not be started.",
    cancelled: "Codex sign-in was cancelled.",
    no_active_login: "There is no active Codex sign-in to cancel.",
    connect_window_opened: "The local provider service opened direct Claude Code sign-in.",
    connect_already_connected: "Claude is already connected through Claude Code.",
    connect_already_active: "A Claude Code sign-in is already active.",
    connect_unavailable: "Direct Claude Code sign-in is unavailable from the local provider service.",
    profile_add: "Account profile added and selected.",
    profile_select: "Active account profile changed.",
    profile_remove: "Account profile removed from the selector.",
  };
  const REASON_COPY = {
    login_expired: "The Codex sign-in window expired.",
    login_start_failed: "Codex sign-in could not be started.",
    login_failed: "Codex sign-in did not complete.",
    login_interrupted: "Codex sign-in was interrupted.",
  };

  function safeAccountDocument(raw) {
    if (!raw || raw.schema !== ACCOUNT_CONTRACT || typeof raw !== "object") {
      return {
        codex: {state: "unavailable", can_start: false, can_cancel: false},
        claude: {state: "unavailable", connect_available: false},
      };
    }
    const codexRaw = raw.codex && typeof raw.codex === "object" ? raw.codex : {};
    const claudeRaw = raw.claude && typeof raw.claude === "object" ? raw.claude : {};
    const state = CODEX_STATES.has(codexRaw.state) ? codexRaw.state : "unavailable";
    const claudeState = CLAUDE_STATES.has(claudeRaw.state) ? claudeRaw.state : "unavailable";
    const safeProfiles = provider => {
      const source = raw.profiles && raw.profiles[provider];
      if (!source || !Array.isArray(source.profiles)) return null;
      return source.profiles.filter(row => row && typeof row.id === "string" && typeof row.label === "string")
        .slice(0, 13).map(row => ({id: row.id, label: row.label, active: row.active === true, isolated: row.isolated === true}));
    };
    return {
      codex: {
        state,
        can_start: codexRaw.can_start === true,
        can_cancel: codexRaw.can_cancel === true,
        reason: Object.prototype.hasOwnProperty.call(REASON_COPY, codexRaw.reason_code)
          ? REASON_COPY[codexRaw.reason_code] : "",
      },
      claude: {
        state: claudeState,
        connect_available: claudeRaw.connect_available === true,
        opened: claudeRaw.opened === true,
      },
      result: Object.prototype.hasOwnProperty.call(RESULT_COPY, raw.result)
        ? RESULT_COPY[raw.result] : "",
      profiles: {codex: safeProfiles("codex"), claude: safeProfiles("claude")},
    };
  }

  function providerAccountsMarkup() {
    const profile = provider => `<div class="usage-profile-controls"><b>Account profile</b><div data-profile-list="${provider}"></div><form data-profile-add="${provider}"><input maxlength="40" aria-label="New ${provider} account name" placeholder="New account name"><button type="submit" class="secondary">Add</button></form><small>Isolated official CLI session; login and quota reads follow the selected profile.</small></div>`;
    return `<dialog class="usage-accounts-dialog" data-provider-accounts aria-labelledby="usage-accounts-title"><div class="usage-accounts-sheet"><header><div><p class="usage-kicker">Provider settings</p><h2 id="usage-accounts-title">Provider Accounts</h2><p>Persistent provider-owned sign-in plus isolated multi-account profiles. Credentials never pass through CORD.</p></div><button type="button" class="usage-account-close" data-account-close aria-label="Close Provider Accounts">×</button></header><div class="usage-account-live" data-account-live role="status" aria-live="polite">Account status has not been loaded.</div><div class="usage-account-grid"><section class="usage-account-card"><div><h3>Codex</h3><span data-account-codex-state>Unavailable</span></div><p data-account-codex-copy>Codex sign-in is unavailable on this device.</p>${profile("codex")}<div class="usage-account-actions"><button type="button" data-account-action="codex_login_start" disabled>Start sign-in</button><button type="button" data-account-action="codex_login_cancel" class="secondary" disabled>Cancel sign-in</button></div></section><section class="usage-account-card"><div><h3>Claude</h3><span data-account-claude-state>Unavailable</span></div><p data-account-claude-copy>Direct Claude Code sign-in is unavailable from the local provider service.</p>${profile("claude")}<div class="usage-account-actions"><button type="button" data-account-action="claude_connect_open" disabled>Open Claude Code sign-in</button></div></section></div><footer><p>Profile metadata contains only an opaque local ID and label. Removing a profile retains its credential directory for recovery.</p><button type="button" class="secondary" data-account-refresh>Refresh status</button></footer></div></dialog>`;
  }

  function renderProviderAccounts(dialog, raw, options = {}) {
    const document = safeAccountDocument(raw);
    const busy = options.busy === true;
    const codexState = dialog.querySelector("[data-account-codex-state]");
    const codexCopy = dialog.querySelector("[data-account-codex-copy]");
    const claudeState = dialog.querySelector("[data-account-claude-state]");
    const claudeCopy = dialog.querySelector("[data-account-claude-copy]");
    const live = dialog.querySelector("[data-account-live]");
    if (codexState) codexState.textContent = document.codex.state.replaceAll("_", " ");
    if (codexCopy) codexCopy.textContent = document.codex.reason || CODEX_COPY[document.codex.state];
    if (claudeState) claudeState.textContent = document.claude.state.replaceAll("_", " ");
    if (claudeCopy) claudeCopy.textContent = document.claude.opened
      ? "The local provider service opened direct Claude Code sign-in."
      : CLAUDE_COPY[document.claude.state];
    if (live) live.textContent = options.notice || document.result || "Provider account status is up to date.";
    for (const provider of ["codex", "claude"]) {
      const slot = dialog.querySelector(`[data-profile-list="${provider}"]`);
      const rows = document.profiles && document.profiles[provider];
      if (slot) slot.innerHTML = Array.isArray(rows) ? rows.map(row => `<div class="usage-profile-row"><button type="button" class="secondary ${row.active ? "active" : ""}" data-profile-select="${provider}" data-profile-id="${escapeHTML(row.id)}" ${busy || row.active ? "disabled" : ""}>${row.active ? "● " : "○ "}${escapeHTML(row.label)}</button>${row.isolated ? `<button type="button" class="usage-profile-remove" data-profile-remove="${provider}" data-profile-id="${escapeHTML(row.id)}" aria-label="Remove ${escapeHTML(row.label)}" ${busy ? "disabled" : ""}>−</button>` : ""}</div>`).join("") : "<small>Profiles unavailable.</small>";
    }
    const start = dialog.querySelector('[data-account-action="codex_login_start"]');
    const cancel = dialog.querySelector('[data-account-action="codex_login_cancel"]');
    const connect = dialog.querySelector('[data-account-action="claude_connect_open"]');
    const refresh = dialog.querySelector("[data-account-refresh]");
    if (start) start.disabled = busy || !document.codex.can_start;
    if (cancel) cancel.disabled = busy || !document.codex.can_cancel;
    if (connect) {
      connect.disabled = busy || !document.claude.connect_available || document.claude.state === "connected" || document.claude.state === "waiting_user";
      connect.textContent = document.claude.state === "connected" ? "Claude connected" : document.claude.state === "waiting_user" ? "Sign-in opened" : "Open Claude Code sign-in";
    }
    if (refresh) refresh.disabled = busy;
  }

  function bindProviderAccounts(mount) {
    const dialog = mount.querySelector("[data-provider-accounts]");
    const openButton = mount.querySelector("[data-account-open]");
    if (!dialog || !openButton) return;
    const allowedActions = new Set(["codex_login_start", "codex_login_cancel", "claude_connect_open", "profile_add", "profile_select", "profile_remove"]);
    let lastDocument = null;
    let busy = false;

    const request = async document => {
      const action = typeof document === "string" ? document : document && document.action;
      if (busy || (action && !allowedActions.has(action))) return;
      busy = true;
      renderProviderAccounts(dialog, lastDocument, {busy: true, notice: action ? "Sending your requested account action." : "Loading provider account status."});
      try {
        const isAction = Boolean(action);
        const headers = {"Accept": "application/json"};
        if (isAction) {
          headers["Content-Type"] = "application/json";
          headers[ACCOUNT_ACTION_HEADER] = "v1";
        }
        const response = await fetch(isAction ? ACCOUNT_ACTION_PATH : ACCOUNT_STATUS_PATH, {
          method: isAction ? "POST" : "GET",
          credentials: "same-origin",
          cache: "no-store",
          headers,
          body: isAction ? JSON.stringify(typeof document === "string" ? {action} : document) : undefined,
        });
        const raw = await response.json();
        lastDocument = raw;
        const recognizedResult = safeAccountDocument(raw).result;
        const notice = response.ok || recognizedResult
          ? undefined
          : "The requested account action was not available.";
        busy = false;
        renderProviderAccounts(dialog, raw, {notice});
      } catch (_error) {
        lastDocument = null;
        busy = false;
        renderProviderAccounts(dialog, null, {notice: "Provider account status could not be loaded."});
      }
    };

    openButton.addEventListener("click", () => {
      if (typeof dialog.showModal === "function" && !dialog.open) dialog.showModal();
      else dialog.setAttribute("open", "");
      request(null);
    });
    dialog.querySelector("[data-account-close]")?.addEventListener("click", () => dialog.close());
    dialog.querySelector("[data-account-refresh]")?.addEventListener("click", () => request(null));
    dialog.querySelectorAll("[data-account-action]").forEach(button => button.addEventListener("click", () => request(button.dataset.accountAction)));
    dialog.addEventListener("click", event => {
      const button = event.target.closest("[data-profile-select],[data-profile-remove]");
      if (!button) return;
      const provider = button.dataset.profileSelect || button.dataset.profileRemove;
      request({action: button.dataset.profileSelect ? "profile_select" : "profile_remove", provider, profile_id: button.dataset.profileId});
    });
    dialog.querySelectorAll("[data-profile-add]").forEach(form => form.addEventListener("submit", event => {
      event.preventDefault();
      const input = form.querySelector("input"), label = input.value.trim();
      if (!label) return;
      input.value = "";
      request({action: "profile_add", provider: form.dataset.profileAdd, label});
    }));
    dialog.addEventListener("click", event => { if (event.target === dialog) dialog.close(); });
  }

  function bindUsageInteractions(mount) {
    mount.querySelectorAll("[data-usage-width]").forEach(element => {
      const width = number(element.dataset.usageWidth);
      if (width !== null) element.style.width = `${Math.max(0, Math.min(100, width))}%`;
    });
    mount.querySelectorAll("[data-history-range]").forEach(button => button.addEventListener("click", () => {
      const chartID = button.dataset.historyId, range = button.dataset.historyRange;
      if (!chartID || !range || !historyStore.has(chartID)) return;
      historyRanges.set(chartID, range);
      const stored = historyStore.get(chartID);
      const slot = mount.querySelector(`[data-history-slot="${chartID}"]`);
      if (slot) slot.innerHTML = historyGraph(chartID, stored.daily, range, stored.currencies, stored.metadata);
      mount.querySelectorAll(`[data-history-id="${chartID}"]`).forEach(candidate => candidate.classList.toggle("active", candidate.dataset.historyRange === range));
    }));
  }

  function providerHasMeaningfulData(provider) {
    if (!provider || typeof provider !== "object") return false;
    if (Array.isArray(provider.windows) && provider.windows.length) return true;
    if (Array.isArray(provider.quota_groups) && provider.quota_groups.some(group => Array.isArray(group && group.windows) && group.windows.length)) return true;
    if (Array.isArray(provider.reset_credits) && provider.reset_credits.length) return true;
    if (provider.account && typeof provider.account === "object" && typeof provider.account.authenticated === "boolean") return true;
    const history = provider.history && typeof provider.history === "object" ? provider.history : {};
    if ((Array.isArray(history.daily) && history.daily.length) || ["today_total_tokens", "rolling_7d_total_tokens", "calendar_week_total_tokens", "all_time_total_tokens"].some(key => number(history[key]) !== null)) return true;
    const costs = provider.costs && typeof provider.costs === "object" ? provider.costs : {};
    if (Object.values(costs).some(entry => entry && typeof entry === "object" && (number(entry.amount_nanos) !== null || (entry.by_currency && Object.values(entry.by_currency).some(value => number(value) !== null))))) return true;
    const active = provider.active_sessions && typeof provider.active_sessions === "object" ? provider.active_sessions : {};
    if (number(active.count) !== null || (Array.isArray(active.items) && active.items.length)) return true;
    const breakdowns = provider.breakdowns && typeof provider.breakdowns === "object" ? provider.breakdowns : {};
    return Object.values(breakdowns).some(group => group && Array.isArray(group.items) && group.items.length);
  }

  function payloadHasMeaningfulData(payload) {
    if (!payload || payload.schema !== CONTRACT) return false;
    const state = String(payload.refresh && payload.refresh.state || "").toLowerCase();
    if (state === "error" || state === "unavailable") return false;
    const providers = payload.providers && typeof payload.providers === "object" ? payload.providers : {};
    return Object.values(providers).some(providerHasMeaningfulData);
  }

  function hasQuotaWindows(provider) {
    if (!provider || typeof provider !== "object") return false;
    if (Array.isArray(provider.windows) && provider.windows.length) return true;
    return Array.isArray(provider.quota_groups)
      && provider.quota_groups.some(group => Array.isArray(group && group.windows) && group.windows.length);
  }

  function futureResetWindow(window, now = Date.now()) {
    if (!window || typeof window !== "object" || !known(window.resets_at)) return false;
    const reset = Date.parse(window.resets_at);
    return Number.isFinite(reset) && reset > now;
  }

  function boundedClaudeQuota(provider, now = Date.now()) {
    if (!provider || typeof provider !== "object") return {groups: [], windows: []};
    const groups = (Array.isArray(provider.quota_groups) ? provider.quota_groups : []).map(group => {
      const windows = (Array.isArray(group && group.windows) ? group.windows : [])
        .filter(window => futureResetWindow(window, now));
      if (!windows.length) return null;
      const {runout: _runout, ...bounded} = group;
      return {...bounded, windows};
    }).filter(Boolean);
    const windows = (Array.isArray(provider.windows) ? provider.windows : [])
      .filter(window => futureResetWindow(window, now));
    return {groups, windows};
  }

  function claudeExplicitlyClearsQuota(provider) {
    if (!provider || provider.account?.authenticated !== true) return true;
    const state = String(provider.live_observation_state || "").toLowerCase();
    const accountState = String(provider.account?.status || "").toLowerCase();
    return state === "quota_observation_expired" || state === "expired"
      || accountState.includes("expired") || accountState.includes("sign_out")
      || accountState.includes("signed_out");
  }

  function retainingBoundedClaudeQuota(payload, prior, now = Date.now()) {
    if (!payload || payload.schema !== CONTRACT || !prior) return payload;
    const providers = payload.providers && typeof payload.providers === "object" ? payload.providers : {};
    const current = providers.claude;
    if (!current || hasQuotaWindows(current) || claudeExplicitlyClearsQuota(current)) return payload;
    const retained = boundedClaudeQuota(prior.providers && prior.providers.claude, now);
    if (!retained.groups.length && !retained.windows.length) return payload;
    const warning = "Showing bounded last-good Claude quota until its recorded reset; current quota windows are unavailable.";
    const quotaSource = prior.providers?.claude?.quota_source || {};
    const errors = Array.isArray(current.errors) ? current.errors.slice() : [];
    if (!errors.some(error => error && error.code === "claude_quota_windows_retained_last_good")) {
      errors.push({code: "claude_quota_windows_retained_last_good"});
    }
    return {
      ...payload,
      providers: {...providers, claude: {
        ...current,
        windows: retained.windows,
        quota_groups: retained.groups,
        quota_source: {...quotaSource, canonical: false, warning},
        live_observation_state: "stale_last_good_no_current_windows",
        errors,
      }},
      errors: [...(Array.isArray(payload.errors) ? payload.errors : []),
        {code: "claude_quota_windows_retained_last_good"}],
    };
  }

  function retainedLastGood(payload) {
    const errors = payload && Array.isArray(payload.errors) && payload.errors.length
      ? payload.errors
      : [{code: "coord_usage_refresh_unavailable"}];
    return {
      ...lastGoodPayload,
      refresh: {...(lastGoodPayload && lastGoodPayload.refresh || {}), state: "stale"},
      errors,
    };
  }

  function renderUsageDashboard(payload) {
    const mount = $("#usage");
    if (!mount) return;
    historyStore.clear();
    if (payloadHasMeaningfulData(payload)) {
      payload = retainingBoundedClaudeQuota(payload, lastGoodPayload);
      lastGoodPayload = payload;
    } else if (lastGoodPayload) payload = retainedLastGood(payload);
    renderUsageStrip(payload);
    if (!payload || payload.schema !== CONTRACT) {
      mount.innerHTML = '<div class="usage-state error" role="alert"><strong>Provider usage unavailable</strong><span>The upstream contract was absent or invalid.</span></div>';
      return;
    }
    const refresh = payload.refresh || {}, errors = Array.isArray(payload.errors) ? payload.errors : [], rawState = String(refresh.state || "unknown").toLowerCase(), providers = payload.providers || {};
    const providerStale = Object.values(providers).some(provider => STALE_PROVIDER_STATES.has(String(provider && provider.live_observation_state || "").toLowerCase()));
    const state = rawState === "fresh" && providerStale ? "stale" : rawState;
    const observedDay = String(payload.generated_at || refresh.generated_at || "").slice(0, 10);
    const cards = PROVIDERS.filter(provider => providers[provider] && typeof providers[provider] === "object").map(provider => providerCard(provider, providers[provider], observedDay)).join("");
    mount.innerHTML = `<header class="usage-header"><div><p class="usage-kicker">Provider usage intelligence</p><h1>Provider Usage</h1><p>Claude and Codex account usage, separate from CORD's coordination token ledger. Token receipts are not subscription credits or billed spend. Neither history is substituted for the other.</p></div><div class="usage-header-actions"><div class="usage-freshness ${escapeHTML(state)}" role="status"><strong>${escapeHTML(state)}</strong><span>${escapeHTML(timestamp(refresh.generated_at || payload.generated_at))}</span></div><button type="button" class="usage-accounts-open" data-account-open aria-label="Provider Accounts">Provider Accounts</button></div></header>${errors.length ? `<div class="usage-state ${state === "error" ? "error" : "stale"}" role="alert"><strong>${state === "error" ? "Usage unavailable" : "Showing bounded last-good usage"}</strong><span>${escapeHTML(errors.map(error => visibleErrorCode(error && error.code)).join(", "))}</span></div>` : ""}<div class="usage-provider-grid">${cards || '<div class="usage-state error" role="status"><strong>No provider data</strong><span>Claude and Codex remain unknown; no zero values were inferred.</span></div>'}</div>${providerAccountsMarkup()}<p class="usage-contract">${escapeHTML(CONTRACT)} · canonical payload values rendered without local total or cost derivation.</p>`;
    bindUsageInteractions(mount);
    bindProviderAccounts(mount);
  }

  function retryWarming(payload) {
    const providers = payload && payload.providers && typeof payload.providers === "object" ? payload.providers : {};
    const warming = payload && payload.refresh && payload.refresh.state === "warming" && Object.keys(providers).length === 0;
    if (warming && warmingRetries < 3) {
      warmingRetries += 1;
      window.setTimeout(loadUsageDashboard, 1500);
    } else if (!warming) warmingRetries = 0;
  }

  function loadUsageDashboard() {
    return fetch("/api/v1/usage-dashboard", {headers: {Accept: "application/json"}, cache: "no-store"}).then(response => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    }).then(payload => { renderUsageDashboard(payload); retryWarming(payload); return payload; }).catch(() => {
      renderUsageDashboard({schema: CONTRACT, generated_at: new Date().toISOString(), refresh: {state: "error", generated_at: new Date().toISOString()}, providers: {}, errors: [{code: "coord_usage_route_unavailable"}]});
      return null;
    });
  }

  window.CoordUsageDashboard = {CONTRACT, load: loadUsageDashboard, render: renderUsageDashboard, setSystemTelemetry};
}());

(function(){
  "use strict";
  const PATH="/api/v1/provider-management",HEADER="X-Coord-Usage-Action",SCHEMA="coord.provider-management.v1",esc=value=>String(value??"").replace(/[&<>\"']/g,ch=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[ch]));let busy=false,last=null;
  function rows(doc){const profiles=doc.profiles||{};return (doc.catalog||[]).map(p=>{const group=profiles[p.id]||{},accounts=group.profiles||[],active=accounts.find(row=>row.id===group.active)||accounts[0];return `<article class="provider-service ${p.enabled?"enabled":"disabled"}"><header><i data-service-accent="${esc(p.accent)}"></i><div><b>${esc(p.display_name)}</b><span>${esc((p.capabilities||[]).join(" · "))}</span></div><button type="button" data-service-toggle="${esc(p.id)}" data-enabled="${p.enabled}">${p.enabled?"On":"Enable"}</button></header>${p.enabled?`<label>Account<select data-account-select="${esc(p.id)}">${accounts.map(row=>`<option value="${esc(row.id)}" ${row.active?"selected":""}>${esc(row.label)}</option>`).join("")}</select></label><form data-account-add="${esc(p.id)}"><input name="label" maxlength="40" required placeholder="Add account"><select name="auth_mode">${(p.auth_modes||[]).map(mode=>`<option>${esc(mode)}</option>`).join("")}</select><input name="endpoint" maxlength="320" placeholder="Optional endpoint"><button>Add</button></form>${active&&["api_key","gateway"].includes(active.auth_mode)?`<form data-credential="${esc(p.id)}" data-profile="${esc(active.id)}"><input name="credential" type="password" maxlength="16384" required placeholder="Store in Keychain"><button>Save</button>${active.credential_set?`<button type="button" data-clear="${esc(p.id)}" data-profile="${esc(active.id)}">Clear</button>`:""}</form>`:""}`:""}</article>`}).join("")}
  function render(dialog,doc,note=""){last=doc;const policy=doc.routing_policy||{},winner=doc.recommendation&&doc.recommendation.selected;dialog.querySelector("[data-provider-note]").textContent=note||"Provider settings are current.";dialog.querySelector("[data-route-winner]").innerHTML=winner?`<b>${esc(winner.display_name)}</b><span>${winner.weekly_remaining==null?"weekly unknown":Math.round(winner.weekly_remaining)+"% weekly"}${winner.session_remaining==null?"":" · "+Math.round(winner.session_remaining)+"% session"}</span>`:"<b>No eligible route</b><span>Policy fails closed when usage or authentication is unavailable.</span>";const form=dialog.querySelector("[data-route-policy]");for(const key of ["mode","min_session_remaining","min_weekly_remaining","min_runway_minutes"])if(form.elements[key])form.elements[key].value=policy[key];for(const key of ["allow_metered_api","prefer_subscription","prefer_local"])form.elements[key].checked=policy[key]===true;const providerList=dialog.querySelector("[data-provider-list]");providerList.innerHTML=rows(doc);providerList.querySelectorAll("[data-service-accent]").forEach(node=>node.style.setProperty("--service-accent",node.dataset.serviceAccent));dialog.querySelectorAll("button,input,select").forEach(node=>node.disabled=busy)}
  async function request(dialog,document){if(busy)return;busy=true;if(last)render(dialog,last,document?"Applying setting…":"Refreshing…");try{const response=await fetch(PATH,{method:document?"POST":"GET",cache:"no-store",credentials:"same-origin",headers:{Accept:"application/json",[HEADER]:"v1",...(document?{"Content-Type":"application/json"}:{})},body:document?JSON.stringify(document):undefined}),raw=await response.json();busy=false;render(dialog,raw&&raw.schema===SCHEMA?raw:last||{catalog:[],profiles:{},routing_policy:{}},response.ok?"Saved.":"Request was rejected.")}catch(_error){busy=false;render(dialog,last||{catalog:[],profiles:{},routing_policy:{}},"Services unavailable.")}}
  function markup(){return `<dialog class="provider-management-dialog" data-provider-dialog><div class="provider-management-sheet"><header><div><span>COORD harness settings</span><h3>Services & intelligent routing</h3><p>Provider accounts remain in the configured provider service; COORD receives a sanitized control projection.</p></div><button type="button" data-provider-close>×</button></header><div data-provider-note>Not loaded.</div><section class="routing-card"><div data-route-winner><b>No recommendation yet</b></div><form data-route-policy><label>Mode<select name="mode"><option value="advisory">Advisory</option><option value="automatic">Automatic selection</option></select></label><label>Session reserve<input name="min_session_remaining" type="number" min="0" max="100"></label><label>Weekly reserve<input name="min_weekly_remaining" type="number" min="0" max="100"></label><label>Runway min<input name="min_runway_minutes" type="number" min="0" max="10080"></label><label><input name="prefer_subscription" type="checkbox">Prefer subscription</label><label><input name="prefer_local" type="checkbox">Prefer local</label><label><input name="allow_metered_api" type="checkbox">Allow metered/API</label><button>Save routing</button></form><small>Selection is advisory by default. Task launch and provider spend remain explicit.</small></section><details open><summary>Provider catalog & accounts</summary><div class="provider-service-list" data-provider-list></div></details><details><summary>Add custom gateway</summary><form class="provider-custom-form" data-custom><input name="id" required pattern="[a-z][a-z0-9_-]{1,31}" placeholder="service-id"><input name="display_name" required maxlength="48" placeholder="Display name"><input name="credential_env" pattern="[A-Z][A-Z0-9_]{1,63}" placeholder="API_KEY_ENV"><input name="endpoint_env" pattern="[A-Z][A-Z0-9_]{1,63}" placeholder="BASE_URL_ENV"><button>Add</button></form></details><footer><span>Credentials are write-only to macOS Keychain. COORD never returns or stores them.</span><button type="button" data-provider-refresh>Refresh</button></footer></div></dialog>`}
  function bind(){const root=document.querySelector("#usage"),actions=root&&root.querySelector(".usage-header-actions");if(!actions||actions.querySelector("[data-provider-open]"))return;const button=document.createElement("button");button.type="button";button.className="usage-accounts-open";button.dataset.providerOpen="";button.textContent="Services & Routing";actions.append(button);root.insertAdjacentHTML("beforeend",markup());const dialog=root.querySelector("[data-provider-dialog]");button.addEventListener("click",()=>{typeof dialog.showModal==="function"?dialog.showModal():dialog.setAttribute("open","");request(dialog)});dialog.querySelector("[data-provider-close]").onclick=()=>typeof dialog.close==="function"?dialog.close():dialog.removeAttribute("open");dialog.querySelector("[data-provider-refresh]").onclick=()=>request(dialog);dialog.addEventListener("change",event=>{const select=event.target.closest("[data-account-select]");if(select)request(dialog,{action:"account_select",provider_id:select.dataset.accountSelect,profile_id:select.value})});dialog.addEventListener("click",event=>{const toggle=event.target.closest("[data-service-toggle]"),clear=event.target.closest("[data-clear]");if(toggle)request(dialog,{action:"provider_configure",provider_id:toggle.dataset.serviceToggle,enabled:toggle.dataset.enabled!=="true",priority:50});if(clear)request(dialog,{action:"credential_clear",provider_id:clear.dataset.clear,profile_id:clear.dataset.profile})});dialog.addEventListener("submit",event=>{event.preventDefault();const form=event.target,data=new FormData(form);if(form.matches("[data-route-policy]")){request(dialog,{action:"routing_policy_update",policy:{version:1,mode:data.get("mode"),min_session_remaining:Number(data.get("min_session_remaining")),min_weekly_remaining:Number(data.get("min_weekly_remaining")),min_runway_minutes:Number(data.get("min_runway_minutes")),allow_metered_api:data.has("allow_metered_api"),prefer_subscription:data.has("prefer_subscription"),prefer_local:data.has("prefer_local"),required_capabilities:last.routing_policy.required_capabilities}})}else if(form.matches("[data-account-add]")){request(dialog,{action:"account_add",provider_id:form.dataset.accountAdd,label:data.get("label"),auth_mode:data.get("auth_mode"),endpoint:data.get("endpoint")||null})}else if(form.matches("[data-credential]")){const credential=String(data.get("credential")||"");form.reset();request(dialog,{action:"credential_set",provider_id:form.dataset.credential,profile_id:form.dataset.profile,credential})}else if(form.matches("[data-custom]")){request(dialog,{action:"provider_add",provider:{id:data.get("id"),display_name:data.get("display_name"),auth_modes:["gateway"],default_auth_mode:"gateway",capabilities:["chat","code","tools"],credential_env:data.get("credential_env")||null,endpoint_env:data.get("endpoint_env")||null}})}})}
  new MutationObserver(bind).observe(document.documentElement,{childList:true,subtree:true});bind();
}());
