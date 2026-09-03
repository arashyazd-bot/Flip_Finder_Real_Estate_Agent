// Shared report rendering — loaded by BOTH /app (index.html) and /analyze (analyze.html).
// Classic script: top-level declarations land on the global scope; app.js (loaded after)
// calls these directly. MUST stay free of app state (no currentIntent/_reports/favorites/
// lastRun*): pure formatters + DOM-by-id readers only, so /analyze can render a report
// without loading app.js (whose init IIFE bounces anonymous visitors to /login).

function fmtMoney(n) {
  if (n == null) return "—";
  const sign = n < 0 ? "-$" : "$";
  return sign + Math.abs(Math.round(n)).toLocaleString();
}
function fmtNum(n) { return n == null ? "—" : Math.round(n).toLocaleString(); }
function fmtPct(n) { return n == null ? "—" : `${n}%`; }
function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function safeUrl(u) {
  // Third-party listing data flows into href/style attributes; allow only http(s).
  u = String(u == null ? "" : u);
  return /^https?:\/\//i.test(u) ? escapeHtml(u) : "#";
}

// Punchy one-line deal headline for the compact card front (intent-aware).
function headlineHtml(r, intent) {
  if (intent === "rent") {
    const cf = r.monthly_cash_flow;
    if (cf == null) return "";
    return `<span class="hl ${cf >= 0 ? "pos" : "neg"}">${fmtMoney(cf)}/mo</span> cash flow · ${fmtPct(r.cap_rate_pct)} cap`;
  }
  const p = r.projected_profit;
  if (p == null) return "";
  const cls = p >= 0 ? "pos" : "neg";
  const rule = r.passes_70_rule ? "✓ 70% rule" : "✗ 70% rule";
  return `<span class="hl ${cls}">${fmtMoney(p)}</span> profit · ${fmtPct(r.profit_margin_pct)} · ${rule}`;
}

// The full math/comps body for a report, WITHOUT the outer <details> wrappers
// for Risks/Comps. `expandLists` controls whether Risks/Comps render as always-open
// lists (true → for the print report) or collapsed <details> (false → on the card).
// NOTE: the only edit made while moving this out of app.js — server-sourced strings
// (verdicts, reasons, arv_source/confidence, rehab_signal, comps, risk flags) are now
// wrapped in escapeHtml(); they were interpolated raw before.
function breakdownRows(r, expandLists) {
  const compRange = (r.comp_psf_range && r.comp_psf_range[0] != null)
    ? `$${Math.round(r.comp_psf_range[0])}–$${Math.round(r.comp_psf_range[1])}/sqft`
    : "n/a";
  // Comp provenance: what the comp prices actually are (sold sales vs Zillow estimates).
  const prov = r.comp_provenance || {};
  const provText = r.comp_count
    ? ` · ${prov.sold || 0} sold / ${r.comp_count - (prov.sold || 0)} est`
    : "";
  const dataAsOf = r.enriched_at
    ? `<div class="row"><span>Data as of</span><span class="v muted">${new Date(r.enriched_at).toLocaleString()}</span></div>`
    : "";
  const passEmoji = r.passes_70_rule ? "✅" : "❌";
  const compsHtml = (r.comps_summary || []).map(c => `<li>${escapeHtml(c)}</li>`).join("") || "<li>(no comps)</li>";
  const risksHtml = (r.risk_flags || []).map(f => `<li>${escapeHtml(f)}</li>`).join("") || "<li>none flagged</li>";

  const listsHtml = expandLists
    ? `
    <h4>Risks (${(r.risk_flags || []).length})</h4>
    <ul>${risksHtml}</ul>
    <h4>Comps used (${(r.comps_summary || []).length})</h4>
    <ul>${compsHtml}</ul>`
    : `
    <details><summary>Risks (${(r.risk_flags || []).length})</summary><ul>${risksHtml}</ul></details>
    <details><summary>Comps used (${(r.comps_summary || []).length})</summary><ul>${compsHtml}</ul></details>`;

  return `
    <h4>Flip: <strong>${escapeHtml(r.verdict.replace(/_/g, ' '))}</strong> · Score ${r.flip_score}/100</h4>
    <p>${escapeHtml(r.verdict_reason)}</p>

    <h4>Rent: <strong>${escapeHtml((r.rental_verdict || 'NO_RENT_DATA').replace(/_/g, ' '))}</strong> · Score ${r.rental_score || 0}/100</h4>
    <p>${escapeHtml(r.rental_verdict_reason || '')}</p>

    <h4>Flip math</h4>
    <div class="row"><span>ARV</span><span class="v">${fmtMoney(r.arv)} <span class="muted">(${escapeHtml(r.arv_source)}, ${escapeHtml(r.arv_confidence)}, ${r.comp_count} comps ${compRange}${provText})</span></span></div>
    ${dataAsOf}
    <div class="row"><span>Rehab</span><span class="v">${fmtMoney(r.rehab_estimate)} <span class="muted">($${r.rehab_psf}/sqft, ${escapeHtml(r.rehab_signal)})</span></span></div>
    <div class="row"><span>Buy-side closing</span><span class="v">${fmtMoney(r.buy_closing_cost)}</span></div>
    <div class="row"><span>Hold 6mo / Financing / Sell</span><span class="v">${fmtMoney(r.holding_cost_6mo)} / ${fmtMoney(r.financing_cost)} / ${fmtMoney(r.selling_cost)}</span></div>
    <div class="row"><span>All-in cost</span><span class="v">${fmtMoney(r.all_in_cost)}</span></div>
    <div class="row"><span>Net resale</span><span class="v">${fmtMoney(r.net_resale)}</span></div>
    <div class="row"><span>Projected profit</span><span class="v">${fmtMoney(r.projected_profit)} (${fmtPct(r.profit_margin_pct)})</span></div>
    <div class="row"><span>70% rule MAO</span><span class="v">${fmtMoney(r.mao_70_rule)} ${passEmoji}</span></div>

    <h4>Rental math (BRRRR)</h4>
    ${r.monthly_rent_est ? `
      <div class="row"><span>Rent est</span><span class="v">${fmtMoney(r.monthly_rent_est)}/mo</span></div>
      <div class="row"><span>Cap rate</span><span class="v">${fmtPct(r.cap_rate_pct)}</span></div>
      <div class="row"><span>Cash flow</span><span class="v">${fmtMoney(r.monthly_cash_flow)}/mo</span></div>
      <div class="row"><span>BRRRR refi proceeds</span><span class="v">${fmtMoney(r.brrrr_refi_proceeds)}</span></div>
    ` : `<p class="muted">No rent comps available.</p>`}
    ${listsHtml}
  `;
}

function breakdownHtml(r) {
  return breakdownRows(r, false);
}

// [inputId, query key (FlipperEvaluator-mapped), shown default, isPercent]
// Percent fields are entered as whole numbers (12 = 12%) and sent as fractions (0.12); the
// backend clamps every value, so a blank or out-of-range box just falls back to the default.
const ASSUMPTION_FIELDS = [
  ["as-apr",    "hm_apr",        10.5, true],
  ["as-hold",   "hold_months",      6, false],
  ["as-close",  "buy_closing_pct", 1.5, true],
  ["as-points", "points_pct",     2.5, true],
  ["as-sell",   "selling_pct",   6.25, true],
  ["as-opex",   "opex_pct",        25, true],
  ["as-refi",   "refi_apr",       7.5, true],
];
// [label, unit] for the human-readable snapshot shown in the report header.
const ASSUMPTION_LABELS = {
  "as-apr":    ["Hard-money APR", "%"], "as-hold":   ["Hold", " mo"],
  "as-close":  ["Buy closing", "%"],   "as-points": ["Loan points", "%"],
  "as-sell":   ["Selling", "%"],       "as-opex":   ["Rental OpEx", "%"],
  "as-refi":   ["Refi APR", "%"],
};
function assumptions() {
  const q = {};
  for (const [id, key, , isPct] of ASSUMPTION_FIELDS) {
    const el = document.getElementById(id);
    if (!el || el.value === "") continue;       // blank → backend default
    const n = parseFloat(el.value);
    if (!isFinite(n)) continue;
    q[key] = isPct ? n / 100 : Math.round(n);
  }
  return q;
}
function assumptionsQuery() {
  return Object.entries(assumptions())
    .map(([k, val]) => `&${k}=${encodeURIComponent(val)}`).join("");
}
// Full effective set (defaults + overrides), captured at search time so a shared PDF says
// exactly what numbers produced it. Overrides are flagged "(adj)".
function effectiveAssumptions() {
  return ASSUMPTION_FIELDS.map(([id, , def]) => {
    const el = document.getElementById(id);
    const entered = el && el.value !== "" ? parseFloat(el.value) : NaN;
    const val = isFinite(entered) ? entered : def;
    const [label, unit] = ASSUMPTION_LABELS[id];
    return `${label}: ${val}${unit}${val !== def ? " (adj)" : ""}`;
  });
}
