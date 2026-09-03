// Real Estate Analyzer dashboard frontend
// SSE consumer + history/favorites/quota handling

const api = {
  async me() { return fetch("/api/me", {credentials: "include"}).then(r => r.json()); },
  async logout() { return fetch("/api/logout", {method: "POST", credentials: "include"}); },
  async history() { return fetch("/api/history", {credentials: "include"}).then(r => r.json()); },
  async historyOne(slug) { return fetch(`/api/history/${slug}`, {credentials: "include"}).then(r => r.json()); },
  async favorites() { return fetch("/api/favorites", {credentials: "include"}).then(r => r.json()); },
  async favProp(zpid, on, citySlug = null) {
    const qs = citySlug ? `?city_slug=${encodeURIComponent(citySlug)}` : "";
    return fetch(`/api/favorites/properties/${zpid}${qs}`, {
      method: on ? "POST" : "DELETE", credentials: "include",
    }).then(r => r.json());
  },
  async favCity(slug, on) {
    return fetch(`/api/favorites/cities/${slug}`, {
      method: on ? "POST" : "DELETE", credentials: "include",
    }).then(r => r.json());
  },
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// in-memory: current view + favorites set
let favorites = {properties: new Set(), cities: new Set()};
let currentCity = null;
let currentSlug = null;
// Last run's summary/market + load source — surfaced in the Print/PDF report header.
let lastRunSummary = null;   // { enriched, requested, from_cache, fresh, cost_usd, ... }
let lastRunMarket = null;    // { scanned, strong, marginal, quality, ... }
let lastRunTotal = null;     // ranked count from the `complete` event
let lastRunQueriedAt = null; // ISO timestamp when loaded from the Archive
let lastRunScope = null;     // { kind, label, zip_count } from the `scope` event
let lastRunAssumptions = null; // human-readable underwriting assumptions used by the run (PDF header)

// --- auth gate ---
async function ensureAuth() {
  const r = await api.me();
  if (!r.authenticated) { window.location = "/login"; return false; }
  renderUserChip(r);
  return true;
}

// Populate the account menu (avatar, runs-left, email) + admin link.
// `me` shape: { authenticated, email, is_admin, daily_cap, runs_today, remaining }
// remaining === null means unlimited (admin).
function renderUserChip(me) {
  const adminLink = document.getElementById("admin-link");
  if (adminLink) adminLink.hidden = !me.is_admin;

  const toggle = document.getElementById("account-toggle");
  const avatar = document.getElementById("account-avatar");
  const runs = document.getElementById("account-runs");
  const emailEl = document.getElementById("account-email");

  if (emailEl) emailEl.textContent = me.email || "";
  if (avatar) avatar.textContent = ((me.email || "?").trim()[0] || "?").toUpperCase();
  if (toggle) toggle.classList.remove("warn", "empty");

  if (!runs) return;
  if (me.remaining === null) {
    runs.textContent = "∞";
    runs.title = "Unlimited runs (admin)";
  } else {
    runs.textContent = `${me.remaining}/${me.daily_cap}`;
    runs.title = `${me.remaining} of ${me.daily_cap} runs left today`;
    if (toggle) {
      if (me.remaining === 0) toggle.classList.add("empty");
      else if (me.remaining <= 1) toggle.classList.add("warn");
    }
  }
}

async function refreshQuotaChip() {
  try {
    const r = await api.me();
    if (r.authenticated) {
      renderUserChip(r);
      // The status dot was static before; reflect real backend health.
      fetch("/api/health", {credentials: "include"})
        .then(x => x.json())
        .then(h => {
          const dot = document.getElementById("api-status");
          if (!dot) return;
          const ok = h && h.ok && h.bright_data_token;
          dot.classList.toggle("status-ok", ok);
          dot.classList.toggle("status-err", !ok);
          const label = dot.nextElementSibling;
          if (label) label.textContent = ok ? "APIs healthy" : "API config issue";
        })
        .catch(() => {});
    }
  } catch (_) { /* non-fatal */ }
}

// --- helpers ---
// Abbreviated price for map pins / chips: $459K, $1.6M
function fmtPriceShort(n) {
  if (n == null) return "—";
  const a = Math.abs(n), s = n < 0 ? "-" : "";
  if (a >= 1e6) return `${s}$${(a / 1e6).toFixed(2).replace(/\.?0+$/, "")}M`;
  if (a >= 1e3) return `${s}$${Math.round(a / 1e3)}K`;
  return `${s}$${Math.round(a)}`;
}
function timeAgo(iso) {
  if (!iso) return "";
  let s = iso;
  // Normalize common SQLite format 'YYYY-MM-DD HH:MM:SS' to ISO UTC
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(s)) {
    s = s.replace(' ', 'T') + 'Z';
  }
  const t = new Date(s);
  const dt = (Date.now() - t.getTime()) / 1000;
  if (isNaN(dt)) return "";
  if (dt < 0) return `0s ago`;
  if (dt < 60) return `${Math.floor(dt)}s ago`;
  if (dt < 3600) return `${Math.floor(dt/60)}m ago`;
  if (dt < 86400) return `${Math.floor(dt/3600)}h ago`;
  return `${Math.floor(dt/86400)}d ago`;
}
function setStatus(msg) {
  const el = $("#status-line");
  if (el) { el.textContent = msg || ""; el.title = msg || ""; }
  // While scouting, the filter bar (and its status line) is hidden — mirror
  // progress into the loader's sub-line so the user still sees activity.
  if (isScouting()) {
    const prog = document.getElementById("scout-progress");
    if (prog) prog.textContent = msg || "";
  }
}
function showSpinner() { const s = $("#enrich-spinner"); if (s) s.hidden = false; }
function hideSpinner() { const s = $("#enrich-spinner"); if (s) s.hidden = true; }

// --- scout loader: full-screen "working" state; results stay hidden until complete ---
function isScouting() {
  const r = document.querySelector(".results");
  return !!(r && r.classList.contains("is-scouting"));
}
function showScoutLoader() {
  const results = document.querySelector(".results");
  const loader = document.getElementById("scout-loader");
  if (!results || !loader) return;
  document.body.classList.add("has-results");  // collapse the landing hero → working view
  results.classList.add("is-scouting");
  loader.hidden = false;
  loader.classList.remove("error");
  const cap = loader.querySelector(".scout-caption");
  if (cap) cap.textContent = "Scouting your location…";
  const sub = loader.querySelector(".scout-sub");
  if (sub) sub.hidden = false;
  const prog = document.getElementById("scout-progress");
  if (prog) prog.textContent = "";
}
function hideScoutLoader() {
  const results = document.querySelector(".results");
  const loader = document.getElementById("scout-loader");
  if (loader) loader.hidden = true;
  if (results) results.classList.remove("is-scouting");
  const fb = document.getElementById("filter-bar");
  if (fb) fb.hidden = !document.querySelector("#results-grid .card");
  syncPrintButton();
  // Map initialized & fitted while its container was display:none — re-measure
  // and re-fit now that it's actually visible.
  requestAnimationFrame(() => requestAnimationFrame(refitMapToMarkers));
  setTimeout(refitMapToMarkers, 250);
}
function scoutError(msg) {
  const loader = document.getElementById("scout-loader");
  if (!loader || loader.hidden) return;
  loader.classList.add("error");
  const cap = loader.querySelector(".scout-caption");
  if (cap) cap.textContent = msg || "Something went wrong with this run.";
  const sub = loader.querySelector(".scout-sub");
  if (sub) sub.hidden = true;
}

// Honest market-quality note shown after a run completes.
// " across N ZIPs" — or " across N of M ZIPs (large county…)" when the scan was truncated.
function zipScanLabel(m) {
  if (!m || !m.zips) return "";
  if (m.zips_total && m.zips_total > m.zips) {
    return ` across ${m.zips} of ${m.zips_total} ZIPs (large county — narrow to a city for full coverage)`;
  }
  return ` across ${m.zips} ZIPs`;
}

// HUD/FHA bank-owned (REO) foreclosure leads — separate from the flip pipeline (no price).
// Sparse (~19/state), so it's scoped to the STATE and labeled "statewide", and carried into the PDF.
let lastHud = [], lastHudState = "";
function renderHud(props, state) {
  lastHud = props || []; lastHudState = state || "";
  const el = $("#hud-section");
  if (!el) return;
  if (!props || !props.length) { el.hidden = true; el.innerHTML = ""; return; }
  const rows = props.map(p => `
    <a class="hud-row" href="${safeUrl(p.link)}" target="_blank" rel="noopener">
      <span class="hud-addr">${escapeHtml(p.address)}</span>
      <span class="hud-meta">case ${escapeHtml(p.case_num || "—")} · View ↗</span>
    </a>`).join("");
  el.innerHTML = `<div class="hud-head">🏛 HUD/REO foreclosures — ${escapeHtml(state || "")} statewide (${props.length})` +
    ` <span class="muted">— bank-owned leads, no price/analysis; click to view</span></div>${rows}`;
  el.hidden = false;
}

async function maybeFetchHud(city) {
  if (!$("#pf-hud")?.checked || !city) { renderHud(null); return; }
  try {
    const d = await (await fetch(`/api/hud?q=${encodeURIComponent(city)}`, { credentials: "include" })).json();
    renderHud(d.properties || [], d.state);
  } catch (_) { renderHud(null); }
}

function renderMarketNote(m) {
  const el = $("#market-note");
  if (!el) return;
  if (!m || m.scanned == null) { el.hidden = true; return; }
  // Honest coverage framing: distinguish the full market scanned from the top slice we
  // deep-analyze, so "scanned 112" never reads as "the whole market is 112".
  const cov = (m.discovered && m.discovered > m.scanned)
    ? `Scanned ${m.discovered.toLocaleString()} listings${zipScanLabel(m)} — deep-analyzed the top ${m.scanned}`
    : `Analyzed ${m.scanned} listings`;
  let cls = "market-none", msg = "";
  if (m.quality === "strong") {
    cls = "market-strong";
    msg = `🟢 ${cov} — found ${m.strong} strong` +
          (m.marginal ? ` + ${m.marginal} marginal` : "") + ` flip opportunit${(m.strong + m.marginal) === 1 ? "y" : "ies"}.`;
  } else if (m.quality === "some") {
    cls = "market-some";
    msg = `🟡 ${cov} — no strong flips, but ${m.opportunities} ` +
          `marginal/rental opportunit${m.opportunities === 1 ? "y" : "ies"}. Showing the best of the market.`;
  } else {
    cls = "market-none";
    msg = `🔴 ${cov} — none cleared the deal criteria in this market. ` +
          `Showing the closest near-misses (ranked best-first), not recommended buys.`;
  }
  el.className = `market-note ${cls}`;
  el.textContent = msg;
  el.hidden = false;
}
function showQuota(text) {
  const el = $("#quota-banner-text");
  // linkify brightdata.com/cp so the admin can click straight through
  el.innerHTML = text.replace(
    /(brightdata\.com\/cp)/g,
    '<a href="https://$1" target="_blank" rel="noopener" style="color:inherit;text-decoration:underline;">$1</a>'
  );
  $("#quota-banner").hidden = false;
}
function hideQuota() { $("#quota-banner").hidden = true; }

// --- map (Leaflet + OpenStreetMap) ---
// Markers are colored by DEAL VERDICT, not price: green = strong, amber = marginal,
// red = no-deal/teardown, gray = still analyzing. One marker per zpid, synced to its card.
let _map = null;
let _markerLayer = null;
const _markers = {};                 // zpid -> { marker, ll, label, bucket, report }
const _reports = {};                 // zpid -> merged base-card + scored report (for sort/filter)
const US_CENTER = [39.5, -98.35];

function ensureMap() {
  if (_map) return _map;
  const el = document.getElementById("map");
  if (!el || typeof L === "undefined") return null;
  _map = L.map(el, { zoomControl: true, scrollWheelZoom: true })
          .setView(US_CENTER, 4);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(_map);
  _markerLayer = L.layerGroup().addTo(_map);
  // Backstop: whenever the map container actually changes size (e.g. the pane
  // transitions from hidden→visible when .has-map is added), re-read the size so
  // tiles fill the full container. invalidateSize(false) preserves the user's view.
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(() => { try { _map.invalidateSize(false); } catch (_) {} });
    ro.observe(el);
  }
  return _map;
}

function _coords(c) {
  if (!c) return null;
  const lat = parseFloat(c.latitude), lng = parseFloat(c.longitude);
  if (isNaN(lat) || isNaN(lng) || (lat === 0 && lng === 0)) return null;
  return [lat, lng];
}

function verdictBucket(v) {
  switch (v) {
    case "STRONG_FLIP": case "GOOD_RENTAL": return "good";
    case "MARGINAL_FLIP": case "DECENT_RENTAL": case "RENTAL_PLAY": return "mid";
    case "TEAR_DOWN": case "NO_DEAL": case "POOR_RENTAL": return "bad";
    default: return "pending";
  }
}
const _bucketRank = { good: 3, mid: 2, bad: 1, pending: 0 };
function pinBucket(report) {
  if (currentIntent === "flip") return verdictBucket(report.verdict);
  if (currentIntent === "rent") return verdictBucket(report.rental_verdict);
  const a = verdictBucket(report.verdict), b = verdictBucket(report.rental_verdict);
  return _bucketRank[a] >= _bucketRank[b] ? a : b;
}

function _pinIcon(label, bucket, active) {
  return L.divIcon({
    className: "",
    html: `<div class="map-pin ${bucket}${active ? " active" : ""}">${label}</div>`,
    iconSize: [1, 1],
    iconAnchor: [0, 0],
  });
}

function clearMarkers() {
  if (_markerLayer) _markerLayer.clearLayers();
  for (const k in _markers) delete _markers[k];
  const results = document.querySelector(".results");
  if (results) results.classList.remove("has-map", "show-map");
  const vt = document.getElementById("view-toggle");
  if (vt) vt.hidden = true;
}

// Plot gray "pending" pins from discovery-phase base cards. Skips coordless lists.
function addBaseMarkers(cards) {
  const withGeo = (cards || []).filter(c => _coords(c));
  if (!withGeo.length) { clearMarkers(); return; }
  const results = document.querySelector(".results");
  if (results) results.classList.add("has-map");
  const vt = document.getElementById("view-toggle");
  if (vt) vt.hidden = false;
  const map = ensureMap();
  if (!map) return;
  _markerLayer.clearLayers();
  for (const k in _markers) delete _markers[k];
  const latlngs = [];
  withGeo.forEach(c => {
    const ll = _coords(c);
    const label = fmtPriceShort(c.price);
    const marker = L.marker(ll, { icon: _pinIcon(label, "pending", false), riseOnHover: true });
    marker.addTo(_markerLayer);
    _wireMarker(marker, String(c.zpid));
    _markers[c.zpid] = { marker, ll, label, bucket: "pending", report: null };
    latlngs.push(ll);
  });
  // Adding .has-map changes the layout (split-view), so the map container only
  // reaches its final size on the next paint. invalidateSize(false) — synchronous,
  // NOT animated — so the new size is applied BEFORE fitBounds reads it. (Animated
  // invalidateSize defers the resize, leaving fitBounds on a stale 1-tile size.)
  const fit = () => {
    try { map.invalidateSize(false); } catch (_) {}
    if (latlngs.length) { try { map.fitBounds(latlngs, { padding: [40, 40], maxZoom: 15 }); } catch (_) {} }
  };
  requestAnimationFrame(() => requestAnimationFrame(fit));
  setTimeout(fit, 250);  // belt-and-suspenders for slow layout/paint
}

// Re-measure + re-fit to all current markers. Used when the map pane transitions
// from hidden (scout loader) to visible — bounds computed while hidden are stale.
function refitMapToMarkers() {
  if (!_map) return;
  try { _map.invalidateSize(false); } catch (_) {}
  const lls = Object.values(_markers).map(e => e && e.ll).filter(Boolean);
  if (lls.length) {
    try { _map.fitBounds(lls, { padding: [40, 40], maxZoom: 15 }); } catch (_) {}
  }
}

// Recolor + relabel a property's marker once it's been scored (called from upgradeCard).
function updateMarker(report) {
  const entry = _markers[report.zpid];
  const ll = _coords(report) || (entry && entry.ll);
  if (!ll) return;
  const bucket = pinBucket(report);
  const label = fmtPriceShort(report.purchase_price || report.price);
  if (entry) {
    entry.marker.setIcon(_pinIcon(label, bucket, false));
    entry.bucket = bucket; entry.label = label; entry.report = report;
    entry.marker.bindPopup(_popupHtml(report));
  } else {
    const map = ensureMap();
    if (!map) return;
    const results = document.querySelector(".results");
    if (results) results.classList.add("has-map");
    const marker = L.marker(ll, { icon: _pinIcon(label, bucket, false), riseOnHover: true });
    marker.addTo(_markerLayer);
    _wireMarker(marker, String(report.zpid));
    marker.bindPopup(_popupHtml(report));
    _markers[report.zpid] = { marker, ll, label, bucket, report };
  }
}

// Drop markers for properties trimmed out of the top-N, then refit.
function removeMarkersNotIn(keepZpids) {
  const keep = new Set((keepZpids || []).map(String));
  Object.keys(_markers).forEach(zpid => {
    if (!keep.has(String(zpid))) {
      if (_markerLayer) _markerLayer.removeLayer(_markers[zpid].marker);
      delete _markers[zpid];
    }
  });
  const lls = Object.values(_markers).map(m => m.ll);
  if (_map && lls.length) {
    try { _map.fitBounds(lls, { padding: [40, 40], maxZoom: 15 }); } catch (_) {}
  }
}

function _popupHtml(r) {
  const thumb = r.photo
    ? `<div class="pin-pop-thumb" style="background-image:url('${safeUrl(r.photo)}')"></div>` : "";
  const profit = (r.projected_profit != null)
    ? `<div class="pin-pop-row">Profit <b class="${r.projected_profit >= 0 ? "pos" : "neg"}">${fmtMoney(r.projected_profit)}</b> · ${fmtPct(r.profit_margin_pct)}</div>` : "";
  return `<div class="pin-pop">
      ${thumb}
      <div class="pin-pop-body">
        <div class="pin-pop-addr">${escapeHtml(r.address || "")}</div>
        <div class="pin-pop-price">${fmtMoney(r.purchase_price || r.price)}</div>
        <div class="pin-pop-verdicts">
          <span class="verdict ${r.verdict || "PENDING"}">${(r.verdict || "PENDING").replace(/_/g, " ")}</span>
        </div>
        ${profit}
        <button class="pin-pop-view" data-pop-view="${r.zpid}">View details →</button>
      </div>
    </div>`;
}

function _wireMarker(marker, zpid) {
  marker.on("mouseover", () => _setCardHover(zpid, true));
  marker.on("mouseout", () => _setCardHover(zpid, false));
  marker.on("click", () => focusCardFromMarker(zpid));
}

function _setMarkerActive(zpid, on) {
  const entry = _markers[zpid];
  if (!entry || !entry.marker._icon) return;
  const pin = entry.marker._icon.querySelector(".map-pin");
  if (pin) pin.classList.toggle("active", on);
}

// Show/hide a marker in lockstep with its card when filters are applied.
function setMarkerVisible(zpid, visible) {
  const entry = _markers[zpid];
  if (!entry || !_markerLayer) return;
  const has = _markerLayer.hasLayer(entry.marker);
  if (visible && !has) _markerLayer.addLayer(entry.marker);
  else if (!visible && has) _markerLayer.removeLayer(entry.marker);
}
function _setCardHover(zpid, on) {
  const card = document.getElementById(`card-${zpid}`);
  if (card) card.classList.toggle("map-hover", on);
}

// Marker click → ensure the list is visible (mobile), then scroll to the card.
function focusCardFromMarker(zpid) {
  const results = document.querySelector(".results");
  if (results && results.classList.contains("show-map") && typeof setMapView === "function") {
    setMapView(false);  // flip back to list on mobile
  }
  setTimeout(() => scrollToCard(zpid), 60);
}

// Mobile List|Map toggle: swap which pane is visible; Leaflet needs invalidateSize
// once its container becomes visible or tiles render gray.
function setMapView(showMap) {
  const results = document.querySelector(".results");
  if (!results) return;
  results.classList.toggle("show-map", !!showMap);
  document.querySelectorAll("#view-toggle .vt-btn").forEach(b => {
    b.classList.toggle("active", (b.dataset.view === "map") === !!showMap);
  });
  if (showMap && _map) {
    setTimeout(() => { try { _map.invalidateSize(); } catch (_) {} }, 60);
  }
}
document.addEventListener("click", (e) => {
  const vt = e.target.closest("#view-toggle .vt-btn");
  if (vt) setMapView(vt.dataset.view === "map");
});

// --- carousel ---
// Each card's photo div stores _photos (array) and _idx (current index) directly on the element.

function photoUrl(p) {
  // photos array may hold {url, width} objects or plain url strings
  return (p && typeof p === 'object') ? (p.url || '') : (p || '');
}

// Update carousel counter + arrows without touching the background image
function _carouselControls(photoDiv) {
  const photos = photoDiv._photos || [];
  const idx = photoDiv._idx || 0;
  const prev = photoDiv.querySelector(".carousel-prev");
  const next = photoDiv.querySelector(".carousel-next");
  const counter = photoDiv.querySelector(".carousel-counter");
  if (photos.length > 1) {
    if (prev) prev.hidden = false;
    if (next) next.hidden = false;
    if (counter) { counter.hidden = false; counter.textContent = `${idx + 1} / ${photos.length}`; }
  } else {
    if (prev) prev.hidden = true;
    if (next) next.hidden = true;
    if (counter) counter.hidden = true;
  }
}

// Set background + controls; used for arrow-click navigation
function setCarouselFrame(photoDiv) {
  const photos = photoDiv._photos || [];
  if (!photos.length) return;
  const idx = photoDiv._idx || 0;
  const url = photoUrl(photos[idx]) || photoDiv.dataset.photo || '';
  if (url) photoDiv.style.backgroundImage = `url('${url}')`;
  photoDiv.classList.remove("placeholder");
  _carouselControls(photoDiv);
}

// Probe a URL with an Image element; set as background only if it actually loads.
// Falls back to fallbackUrl if the primary fails. Safe against cross-origin CDN blocks.
function _probePhoto(photoDiv, primaryUrl, fallbackUrl) {
  if (!primaryUrl) {
    if (fallbackUrl) photoDiv.style.backgroundImage = `url('${fallbackUrl}')`;
    return;
  }
  const img = new Image();
  img.onload = () => { photoDiv.style.backgroundImage = `url('${primaryUrl}')`; };
  img.onerror = () => {
    photoDiv._bdFailed = true;  // flag so IntersectionObserver also avoids BD urls
    if (fallbackUrl) photoDiv.style.backgroundImage = `url('${fallbackUrl}')`;
  };
  img.src = primaryUrl;
}

// Global delegated handler for carousel arrows (attached once at bottom of file)
function _handleCarouselClick(e) {
  const btn = e.target.closest(".carousel-btn");
  if (!btn) return;
  e.stopPropagation();
  e.preventDefault();
  const photoDiv = btn.closest(".card-photo");
  if (!photoDiv || !photoDiv._photos) return;
  const dir = parseInt(btn.dataset.dir, 10);
  const len = photoDiv._photos.length;
  photoDiv._idx = ((photoDiv._idx || 0) + dir + len) % len;
  setCarouselFrame(photoDiv);
}

// --- rendering ---
function cardSkeleton(card, idx) {
  const isPlaceholder = card.photo ? '' : ' placeholder';
  const dataPhotoAttr = card.photo ? `data-photo="${card.photo}"` : '';
  return `
    <article class="card" data-zpid="${card.zpid}" id="card-${card.zpid}">
      <div class="card-photo${isPlaceholder}" ${dataPhotoAttr}>
        <span class="card-rank">#${idx + 1}</span>
        <button class="card-favorite" data-fav title="Favorite">${favorites.properties.has(card.zpid) ? '★' : '☆'}</button>
        <button class="carousel-btn carousel-prev" data-dir="-1" hidden title="Previous photo">&#8249;</button>
        <button class="carousel-btn carousel-next" data-dir="1" hidden title="Next photo">&#8250;</button>
        <span class="carousel-counter" hidden></span>
      </div>
      <div class="card-body">
        <div class="card-title"><a href="${safeUrl(card.link)}" target="_blank" rel="noopener">${escapeHtml(card.address)}, ${escapeHtml(card.city)}, ${escapeHtml(card.state)}</a></div>
        <div class="card-meta">$${(card.price || 0).toLocaleString()} · ${card.bedrooms || '?'}bd / ${card.bathrooms || '?'}ba · ${card.sqft || '?'} sqft · ${escapeHtml(card.home_type || '')}</div>
        <div class="verdict-row">
          <span class="verdict PENDING">analyzing…</span>
        </div>
        <div class="card-headline" data-headline></div>
        <div class="breakdown" data-breakdown></div>
      </div>
    </article>`;
}

// --- Print / Save-as-PDF report ---------------------------------------------
// Builds a clean, fully-expanded, paginated report for EVERY property in the
// current run (live search OR archive load) into the hidden #print-root, which
// @media print reveals while hiding all on-screen chrome. We read the cards in
// their on-screen DOM order so the report honors the active sort/filter + ranks,
// and pull each report object from the _reports registry for the full math.
// One property's fully-expanded section for the print report.
function printPropertyHtml(r, rank) {
  const addr = [r.address, r.city, r.state].filter(Boolean).join(", ");
  const beds = r.bedrooms != null ? r.bedrooms : "?";
  const baths = r.bathrooms != null ? r.bathrooms : "?";
  const sqft = r.sqft != null ? fmtNum(r.sqft) : "?";
  const type = r.home_type || "";
  const price = fmtMoney(r.purchase_price != null ? r.purchase_price : r.price);
  const flipV = (r.verdict || "PENDING").replace(/_/g, " ");
  const rentV = (r.rental_verdict || "NO_RENT_DATA").replace(/_/g, " ");
  const headline = headlineHtml(r, currentIntent);

  return `
    <section class="pr-prop">
      <div class="pr-prop-head">
        <span class="pr-rank">#${rank}</span>
        <div class="pr-prop-id">
          <div class="pr-addr">${escapeHtml(addr)}</div>
          <div class="pr-meta">${price} · ${beds}bd / ${baths}ba · ${sqft} sqft${type ? " · " + escapeHtml(type) : ""}</div>
        </div>
      </div>
      <div class="pr-verdicts">
        <span class="verdict ${r.verdict || "PENDING"}">Flip: ${flipV} (${r.flip_score != null ? r.flip_score : 0})</span>
        <span class="verdict ${r.rental_verdict || "NO_RENT_DATA"}">Rent: ${rentV} (${r.rental_score || 0})</span>
      </div>
      ${headline ? `<div class="pr-headline">${headline}</div>` : ""}
      <div class="pr-breakdown breakdown">${breakdownRows(r, true)}</div>
    </section>`;
}

// Compose the run-summary line for the report header from whatever we captured.
function printSummaryLine() {
  const parts = [];
  const m = lastRunMarket;
  if (m && m.scanned != null) {
    const strong = m.strong || 0, marginal = m.marginal || 0;
    const cov = (m.discovered && m.discovered > m.scanned)
      ? `Scanned ${m.discovered.toLocaleString()} listings${zipScanLabel(m)} · deep-analyzed top ${m.scanned}`
      : `Analyzed ${m.scanned} listings`;
    parts.push(`${cov} · ${strong} strong + ${marginal} marginal`);
  } else if (lastRunTotal != null) {
    parts.push(`${lastRunTotal} propert${lastRunTotal === 1 ? "y" : "ies"} analyzed`);
  }
  const s = lastRunSummary;
  if (s) {
    if (s.enriched != null && s.requested != null) parts.push(`enriched ${s.enriched}/${s.requested}`);
    if (s.cost_usd != null) parts.push(`cost $${(s.cost_usd || 0).toFixed(4)}`);
  }
  return parts.join(" · ");
}

// Populate #print-root and open the browser print dialog (→ "Save as PDF").
function buildPrintReport() {
  const root = document.getElementById("print-root");
  if (!root) return;

  // On-screen DOM order honors the active sort/filter + server ranking. Skip
  // any cards the user has filtered out (display:none).
  const cards = $$("#results-grid .card").filter(c => c.style.display !== "none");
  const reports = cards
    .map(c => _reports[c.dataset.zpid])
    .filter(Boolean);

  if (!reports.length) {
    root.innerHTML = "";
    return;
  }

  const genStamp = lastRunQueriedAt
    ? `Queried ${escapeHtml(lastRunQueriedAt)} · report generated ${new Date().toLocaleString()}`
    : `Generated ${new Date().toLocaleString()}`;
  const summary = printSummaryLine();

  const header = `
    <header class="pr-header">
      <div class="pr-brand">FlipFinder</div>
      <h1 class="pr-city">${escapeHtml(currentCity || "Flip & Rental Report")}</h1>
      <div class="pr-stamp">${genStamp}</div>
      ${summary ? `<div class="pr-summary">${escapeHtml(summary)}</div>` : ""}
      <div class="pr-count">${reports.length} propert${reports.length === 1 ? "y" : "ies"} in this report</div>
      <div class="pr-disclaimer">
        <strong>Estimates, not appraisals.</strong> ARV, rehab, rent, cap rate, and projected profit are
        automated estimates derived from public listing &amp; comparable-sale data — not appraisals,
        guarantees, or financial advice. Projected profit is a modeled figure that can swing materially
        with the true purchase price, rehab scope, and resale value; verify every number independently
        before making an offer. <em>Comp confidence:</em> high = 5+ tight comps, medium = 3+, low = under 3;
        an ARV pinned to a cap multiple of the list or as-is value is model-limited and warrants manual review.
      </div>
    </header>`;

  const body = reports.map((r, i) => printPropertyHtml(r, i + 1)).join("");
  const hud = lastHud.length ? `<div class="pr-hud">
      <h2>HUD / FHA Foreclosure Leads — ${escapeHtml(lastHudState || "")} statewide (${lastHud.length})</h2>
      <p class="muted">Bank-owned (REO) leads. No price/beds/comps — NOT analyzed; verify at hudhomestore.gov.</p>
      ${lastHud.map(h => `<div class="pr-hud-row"><span>${escapeHtml(h.address || "")}</span><span>case ${escapeHtml(h.case_num || "")}</span></div>`).join("")}
    </div>` : "";
  const footer = `<footer class="pr-footer">Real Estate Analyzer — ${escapeHtml(currentCity || "")} — generated ${new Date().toLocaleDateString()} — estimates only, not financial advice; verify independently before any offer.</footer>`;

  root.innerHTML = header + body + hud + footer;
  window.print();
}

// Show/hide the Print/PDF button alongside Save-city / Reset, only when results exist.
function syncPrintButton() {
  const has = !!document.querySelector("#results-grid .card");
  const btn = document.getElementById("print-report-btn");
  if (btn) btn.hidden = !has;
  const dl = document.getElementById("download-pdf-btn");
  if (dl) dl.hidden = !has;
}

// Server-side PDF: POST the on-screen (sorted/filtered) reports → clean branded PDF with no
// browser print chrome. Falls back to the browser print path if the server can't render.
async function downloadPdf() {
  const cards = $$("#results-grid .card").filter(c => c.style.display !== "none");
  const properties = cards.map(c => _reports[c.dataset.zpid]).filter(Boolean);
  if (!properties.length) return;
  const btn = document.getElementById("download-pdf-btn");
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "Generating…"; }
  try {
    const res = await fetch("/api/report/pdf", {
      method: "POST", credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        city: currentCity || "Flip & Rental Report",
        generated: lastRunQueriedAt ? `Queried ${lastRunQueriedAt}` : `Generated ${new Date().toLocaleString()}`,
        summary: printSummaryLine(),
        assumptions: lastRunAssumptions,
        properties,
        hud: lastHud,
        hud_state: lastHudState,
      }),
    });
    if (!res.ok) throw new Error("pdf " + res.status);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `FlipFinder Report - ${(currentCity || "report").replace(/[^A-Za-z0-9]+/g, "_")}.pdf`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (e) {
    // Fallback: the in-browser print path still works (just with browser print chrome).
    buildPrintReport();
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

function renderResults(city, baseCards) {
  document.body.classList.add("has-results");  // ensure working view (e.g. history loads)
  currentCity = city;
  // Reset the sort/filter registry for the new result set, then seed base-card fields.
  for (const k in _reports) delete _reports[k];
  baseCards.forEach(c => { _reports[c.zpid] = Object.assign({}, c); });
  const fb = $("#filter-bar");
  if (fb) fb.hidden = baseCards.length === 0;
  $("#results-grid").innerHTML = baseCards.map((c, i) => cardSkeleton(c, i)).join("");
  // Stamp each card's discovery-phase photo URL onto the element immediately so
  // upgradeCard (and the IntersectionObserver) can always fall back to it.
  baseCards.forEach(c => {
    const photoDiv = $(`#card-${c.zpid} .card-photo`);
    if (!photoDiv) return;
    if (c.photo) photoDiv.dataset.photoOriginal = c.photo;
    // Initialize carousels for cards that already have a photos array (history loads)
    if (c.photos && c.photos.length > 1) {
      photoDiv._photos = c.photos;
      photoDiv._idx = 0;
      setCarouselFrame(photoDiv);
    }
  });
  // Plot discovery-phase map pins (gray/pending); recolored later by upgradeCard.
  addBaseMarkers(baseCards);
  syncPrintButton();
}

let currentIntent = "both";  // updated on each search

// Mirrors the server _sort_key: tiered so genuine flips lead, then rental plays, then
// near-misses — a money-losing flip never tops the list just because it cash-flows.
function _primaryScore(r, intent) {
  if (intent === "rent") return r.rental_score || 0;
  if (intent === "flip") return r.flip_score || 0;
  const v = r.verdict, rv = r.rental_verdict;
  let tier, within;
  if (v === "STRONG_FLIP")        { tier = 5; within = r.flip_score; }
  else if (v === "MARGINAL_FLIP") { tier = 4; within = r.flip_score; }
  else if (v === "RENTAL_PLAY")   { tier = 3; within = r.rental_score; }
  else if (rv === "GOOD_RENTAL" || rv === "DECENT_RENTAL") { tier = 2; within = r.rental_score; }
  else if (v === "TEAR_DOWN")     { tier = 0; within = r.flip_score; }
  else { tier = 1; within = Math.max(-400, Math.min(400, r.profit_margin_pct || 0)); }  // near-miss: closest-to-viable first
  return tier * 1000 + (within || 0);
}

function upgradeCard(report) {
  const card = $(`#card-${report.zpid}`);
  if (!card) {
    // Property wasn't in the base discovery list (rare); append.
    const idx = $$("#results-grid .card").length;
    $("#results-grid").insertAdjacentHTML("beforeend", cardSkeleton(report, idx));
  }
  const target = $(`#card-${report.zpid}`);
  const photoDiv = target.querySelector(".card-photo");
  // Preserve the discovery-phase Skolit thumbnail URL as the ultimate fallback.
  // dataset.photoOriginal is written once and never overwritten.
  if (!photoDiv.dataset.photoOriginal) {
    photoDiv.dataset.photoOriginal = photoDiv.dataset.photo || '';
  }
  const skolit_url = photoDiv.dataset.photoOriginal;

  if (report.photos && report.photos.length) {
    photoDiv._photos = report.photos;
    photoDiv._idx = 0;
    photoDiv.dataset.photos = JSON.stringify(report.photos);
    photoDiv.dataset.photo = report.photo || photoUrl(report.photos[0]) || '';
    photoDiv.classList.remove("placeholder");
    _carouselControls(photoDiv);  // show counter/arrows immediately (bg set async below)
    // Probe the Bright Data URL; if it won't load, keep the Skolit thumbnail
    _probePhoto(photoDiv, report.photo || photoUrl(report.photos[0]) || '', skolit_url);
  } else if (report.photo) {
    photoDiv.dataset.photo = report.photo;
    photoDiv.classList.remove("placeholder");
    _probePhoto(photoDiv, report.photo, skolit_url);
  }
  const row = target.querySelector(".verdict-row");
  const score = _primaryScore(report, currentIntent);
  target.dataset.score = score;
  row.innerHTML = `
    <span class="verdict ${report.verdict}" title="Flip verdict">Flip: ${report.verdict.replace(/_/g, ' ')} (${report.flip_score})</span>
    <span class="verdict ${report.rental_verdict || 'NO_RENT_DATA'}" title="Rental verdict">Rent: ${(report.rental_verdict || 'NO_RENT_DATA').replace(/_/g, ' ')} (${report.rental_score || 0})</span>
  `;
  const headlineEl = target.querySelector("[data-headline]");
  if (headlineEl) headlineEl.innerHTML = headlineHtml(report, currentIntent);
  // Compact-by-default: full math/comps live behind a single expander.
  target.querySelector("[data-breakdown]").innerHTML =
    `<details class="deal-math"><summary>Deal math &amp; comps</summary>${breakdownHtml(report)}</details>`;
  // Merge scored fields into the sort/filter registry (keep base price/beds/etc).
  _reports[report.zpid] = Object.assign({}, _reports[report.zpid], report);
  // Keep the map marker (if any) in sync with this property's verdict + price.
  if (typeof updateMarker === "function") updateMarker(report);
  // NOTE: do NOT reorder here. Properties arrive already score-ranked from the
  // server; reordering on every event caused 10-30 redundant DOM re-sorts per run.
  // Final ordering is applied once by trimGrid() (live search) or by the caller
  // (loadFromHistory) after the fill loop.
}

function reorderByScore() {
  const grid = $("#results-grid");
  const cards = Array.from(grid.children);
  // Sort scored cards first (desc by score), then unscored skeletons at the end
  cards.sort((a, b) => {
    const sa = a.dataset.score != null ? parseFloat(a.dataset.score) : -Infinity;
    const sb = b.dataset.score != null ? parseFloat(b.dataset.score) : -Infinity;
    return sb - sa;
  });
  cards.forEach((c, i) => {
    const rankEl = c.querySelector(".card-rank");
    if (rankEl) rankEl.textContent = `#${i + 1}`;
    grid.appendChild(c);
  });
}

// --- client-side sort + filters (over the already-fetched result set) ---
function _activeFilters() {
  const val = (id) => { const el = document.getElementById(id); return el ? el.value : ""; };
  return {
    sort: val("sort-by") || "best",
    beds: parseInt(val("filter-beds") || "0", 10),
    baths: parseFloat(val("filter-baths") || "0"),
    type: (val("filter-type") || "").toLowerCase(),
    pmin: parseFloat(val("filter-pmin")) || 0,
    pmax: parseFloat(val("filter-pmax")) || Infinity,
  };
}

function _recPrice(rec) { return rec.price != null ? rec.price : (rec.purchase_price || 0); }

function _passesFilter(rec, f) {
  const price = _recPrice(rec);
  if (price < f.pmin || price > f.pmax) return false;
  if (f.beds && (rec.bedrooms || 0) < f.beds) return false;
  if (f.baths && (rec.bathrooms || 0) < f.baths) return false;
  if (f.type) {
    const t = String(rec.home_type || "").toLowerCase();
    const ok =
      (f.type === "house" && (t.includes("house") || t.includes("single"))) ||
      (f.type === "condo" && t.includes("condo")) ||
      (f.type === "townhouse" && t.includes("town")) ||
      (f.type === "multi" && (t.includes("multi") || t.includes("duplex") || t.includes("triplex")));
    if (!ok) return false;
  }
  return true;
}

// Returns a comparable number; lower sorts first (so we negate "higher is better" keys).
function _sortKey(rec, sort) {
  switch (sort) {
    case "flip":       return -(rec.flip_score || 0);
    case "rent":       return -(rec.rental_score || 0);
    case "price-asc":  return _recPrice(rec);
    case "price-desc": return -_recPrice(rec);
    case "psf":        return rec.list_psf || (_recPrice(rec) / (rec.sqft || 1)) || Infinity;
    case "profit":     return -(rec.profit_margin_pct != null ? rec.profit_margin_pct : -Infinity);
    case "best":
    default:           return -_primaryScore(rec, currentIntent);  // matches server ranking
  }
}

// Apply current filter/sort to the rendered cards AND their map markers in lockstep.
function applySortFilter() {
  const grid = $("#results-grid");
  if (!grid) return;
  const f = _activeFilters();
  const visible = [];
  Array.from(grid.children).forEach(card => {
    const zpid = card.dataset.zpid;
    const rec = _reports[zpid] || {};
    const ok = _passesFilter(rec, f);
    card.style.display = ok ? "" : "none";
    setMarkerVisible(zpid, ok);
    if (ok) visible.push(card);
  });
  visible.sort((a, b) => {
    const ra = _reports[a.dataset.zpid] || {}, rb = _reports[b.dataset.zpid] || {};
    return _sortKey(ra, f.sort) - _sortKey(rb, f.sort);
  });
  visible.forEach((c, i) => {
    const rankEl = c.querySelector(".card-rank");
    if (rankEl) rankEl.textContent = `#${i + 1}`;
    grid.appendChild(c);
  });
}

function trimGrid(keepZpids) {
  const keep = new Set(keepZpids);
  $$("#results-grid .card").forEach(card => {
    if (!keep.has(card.dataset.zpid)) {
      card.style.transition = "opacity 0.3s";
      card.style.opacity = "0";
      setTimeout(() => card.remove(), 350);
    }
  });
  removeMarkersNotIn(keepZpids);
  setTimeout(applySortFilter, 400);
}

// --- favorites ---
async function refreshFavorites() {
  const f = await api.favorites();
  favorites.properties = new Set(f.properties.map(p => p.zpid));
  favorites.cities = new Set(f.cities);
  $$(".card-favorite").forEach(btn => {
    const zpid = btn.closest("[data-zpid]")?.dataset.zpid;
    if (favorites.properties.has(zpid)) { btn.textContent = "★"; btn.classList.add("on"); }
    else { btn.textContent = "☆"; btn.classList.remove("on"); }
  });
  // Save city button reflects current city favorite state
  const btn = $("#save-city-btn");
  if (currentSlug && btn) {
    btn.hidden = false;
    if (favorites.cities.has(currentSlug)) {
      btn.textContent = "★ Saved (city)";
      btn.classList.add("on");
    } else {
      btn.textContent = "☆ Save this city";
      btn.classList.remove("on");
    }
  } else if (btn) {
    btn.hidden = true;
  }
}

document.addEventListener("click", _handleCarouselClick);

document.addEventListener("click", async (e) => {
  // Map popup "View details →" button
  const popView = e.target.closest("[data-pop-view]");
  if (popView) {
    e.preventDefault();
    focusCardFromMarker(popView.dataset.popView);
    if (_map) _map.closePopup();
    return;
  }
  const fav = e.target.closest("[data-fav]");
  if (fav) {
    const card = fav.closest("[data-zpid]");
    const zpid = card.dataset.zpid;
    const on = !favorites.properties.has(zpid);
    await api.favProp(zpid, on, currentSlug);
    await refreshFavorites();
  }
  if (e.target.closest("[data-close]")) {
    e.target.closest(".modal").hidden = true;
  }
});

// Card hover → highlight its map pin (delegated; flicker-free via relatedTarget check).
document.addEventListener("mouseover", (e) => {
  const card = e.target.closest("#results-grid .card");
  if (card) _setMarkerActive(card.dataset.zpid, true);
});
document.addEventListener("mouseout", (e) => {
  const card = e.target.closest("#results-grid .card");
  if (!card) return;
  if (card.contains(e.relatedTarget)) return;  // still inside the card
  _setMarkerActive(card.dataset.zpid, false);
});

// --- search ---
async function startSearch(city, count, intent) {
  hideQuota();
  renderMarketNote(null);  // clear any stale market note from a previous run
  renderHud(null);         // clear stale HUD leads
  // Reset run-summary state carried into the Print/PDF report header.
  lastRunSummary = null; lastRunMarket = null; lastRunTotal = null; lastRunQueriedAt = null; lastRunScope = null;
  currentIntent = intent;

  // Preflight: refresh quota chip and short-circuit cleanly if already capped.
  try {
    const me = await api.me();
    if (me.authenticated) {
      renderUserChip(me);
      if (me.remaining === 0) {
        const capMsg = `⚠️ Daily run limit reached (${me.daily_cap}/${me.daily_cap}). Try again tomorrow or ask your admin to raise your cap.`;
        setStatus(capMsg);
        showQuota(capMsg);  // status line may be hidden (filter bar); banner is always visible
        return;
      }
    }
  } catch (_) { /* non-fatal — let the server enforce */ }

  // County reports are heavier (scan every ZIP in the county). Confirm the cost up front.
  if (/\bcounty\b/i.test(city)) {
    try {
      const r = await fetch(`/api/scope?q=${encodeURIComponent(city)}`, { credentials: "include" });
      const sc = await r.json();
      if (sc.kind === "county") {
        const trunc = sc.truncated ? `\n\n(${sc.total_zips} ZIPs total — only the first ${sc.zip_count} will be scanned; narrow to a city for full depth.)` : "";
        const msg = `Full-county report — ${sc.label}\n\nScans ${sc.zip_count} ZIP codes (~${sc.est_calls} API calls) to cover every neighborhood. This is heavier than a normal search and counts against your daily limit.${trunc}\n\nRun it?`;
        if (!window.confirm(msg)) return;
      }
    } catch (_) { /* estimate failed — proceed; backend still gates by daily cap */ }
  }

  $("#search-btn").disabled = true;
  showScoutLoader();  // hide results until the run completes — no partial output
  setStatus(`Searching ${city} (${intent})…`);
  const deep = !!document.getElementById("pf-deep")?.checked;
  const url = `/api/search/stream?city=${encodeURIComponent(city)}&count=${count}&intent=${encodeURIComponent(intent)}`
            + preFilterQuery()           // pre-search filters → only enrich matches (lower cost)
            + assumptionsQuery()         // user-tuned underwriting assumptions (blank → defaults)
            + (deep ? "&deep=1" : "");   // deep scan → fan out per-zip past the ~800 cap
  lastRunAssumptions = effectiveAssumptions();  // snapshot for the report header
  const es = new EventSource(url, {withCredentials: true});

  es.addEventListener("status", (e) => {
    const d = JSON.parse(e.data); setStatus(d.message);
  });
  es.addEventListener("scope", (e) => {
    const d = JSON.parse(e.data);
    lastRunScope = d;  // surfaced in the coverage line on completion
    if (d.kind === "county") setStatus(`Scanning ${d.label} — ${d.zip_count} neighborhoods…`);
  });
  es.addEventListener("discovery", (e) => {
    const d = JSON.parse(e.data);
    renderResults(d.city, d.properties);
    setStatus(`Found ${d.count} properties — enriching…`);
    showSpinner();
  });
  es.addEventListener("enrich_tick", (e) => {
    const d = JSON.parse(e.data);
    setStatus(`Still enriching… ${d.elapsed}s elapsed (${d.requested} URLs)`);
    showSpinner();
  });
  es.addEventListener("property", (e) => {
    const r = JSON.parse(e.data);
    upgradeCard(r);
  });
  es.addEventListener("trim", (e) => {
    const d = JSON.parse(e.data);
    trimGrid(d.keep);
  });
  es.addEventListener("quota", (e) => {
    const d = JSON.parse(e.data);
    showQuota(d.message);
  });
  es.addEventListener("error", (e) => {
    let msg = "⚠️ Connection error";
    try { msg = `⚠️ ${JSON.parse(e.data).message}`; } catch (_) {}
    setStatus(msg);
    scoutError(msg);  // surface the failure in the loader; keep results hidden
    hideSpinner();
    es.close();
    $("#search-btn").disabled = false;
  });
  es.addEventListener("complete", (e) => {
    const d = JSON.parse(e.data);
    currentSlug = d.slug;
    // Stash the run summary for the Print/PDF report header.
    lastRunSummary = d.summary || null;
    lastRunMarket = d.market || null;
    lastRunTotal = d.total != null ? d.total : null;
    lastRunQueriedAt = null;  // fresh run → "Generated on" timestamp is now
    hideScoutLoader();  // reveal results + filter bar, re-fit the map
    const s = d.summary || {};
    const cacheNote = (s.from_cache != null && s.fresh != null)
      ? ` (${s.from_cache} cached, ${s.fresh} fresh, $${(s.cost_usd || 0).toFixed(4)})`
      : "";
    setStatus(`✅ Done — ${d.total} ranked. Enriched ${s.enriched}/${s.requested}${cacheNote}.`);
    renderMarketNote(d.market);
    maybeFetchHud(currentCity);  // free HUD foreclosure leads, if the toggle is on
    hideSpinner();
    es.close();
    $("#search-btn").disabled = false;
    refreshHistory();
    refreshFavorites();
    refreshQuotaChip();
    setTimeout(applySortFilter, 450);  // honor any active sort/filter after trim settles
  });
}

async function loadFromHistory(slug) {
  hideQuota();
  // History loads render instantly — clear any loader (e.g. error state from a failed run).
  if (isScouting()) hideScoutLoader();
  setStatus("Loading cached results…");
  const data = await api.historyOne(slug);
  currentCity = data.city;
  currentSlug = slug;
  // Archive loads have no SSE run summary; carry what we do know into the report header.
  lastRunSummary = null;
  lastRunAssumptions = null;  // history doesn't store the assumptions a past run used
  lastRunMarket = data.market || null;
  lastRunTotal = (data.results || []).length;
  lastRunQueriedAt = data.queried_at || null;
  const cards = data.results.map(r => ({
    zpid: r.zpid, address: r.address, city: r.city, state: r.state, price: r.purchase_price,
    bedrooms: null, bathrooms: null, sqft: r.sqft, home_type: r.home_type,
    latitude: r.latitude, longitude: r.longitude,  // so addBaseMarkers plots + fits the map
    photo: r.photo, photos: r.photos, link: r.link,
  }));
  renderResults(data.city, cards);
  data.results.forEach(r => upgradeCard(r));
  applySortFilter();  // upgradeCard no longer reorders per-card; sort/filter once after the fill
  setStatus(`📂 Cached: ${data.city} (queried ${timeAgo(data.queried_at)})`);
  refreshFavorites();
}

async function refreshHistory() {
  const data = await api.history();
  const ul = $("#history-list");
  ul.innerHTML = data.items.map(it => `
    <li data-slug="${it.slug}">
      <span>${it.city}</span>
      <span class="ago">${timeAgo(it.queried_at)}</span>
    </li>`).join("") || `<li class="muted" style="padding:0.5rem">No searches yet.</li>`;
  ul.querySelectorAll("li[data-slug]").forEach(li => {
    li.addEventListener("click", () => { loadFromHistory(li.dataset.slug); closeAllDropdowns(); });
  });
}

// --- toolbar dropdowns (Archive, Active Runs) ---
function closeAllDropdowns() {
  document.querySelectorAll(".dropdown-menu").forEach(m => { m.hidden = true; });
  document.querySelectorAll(".dropdown [aria-haspopup]").forEach(btn =>
    btn.setAttribute("aria-expanded", "false"));
}
function toggleDropdown(menuId, toggleBtn) {
  const menu = document.getElementById(menuId);
  if (!menu) return;
  const willOpen = menu.hidden;
  closeAllDropdowns();
  menu.hidden = !willOpen;
  if (toggleBtn) toggleBtn.setAttribute("aria-expanded", String(willOpen));
}
$("#archive-toggle")?.addEventListener("click", (e) => {
  e.stopPropagation();
  toggleDropdown("archive-menu", e.currentTarget);
});
$("#active-toggle")?.addEventListener("click", (e) => {
  e.stopPropagation();
  toggleDropdown("active-menu", e.currentTarget);
});
$("#account-toggle")?.addEventListener("click", (e) => {
  e.stopPropagation();
  toggleDropdown("account-menu", e.currentTarget);
});

// --- pre-search filters (constrain enrichment → cut cost) ---
function preFilters() {
  const v = (id) => { const el = document.getElementById(id); return el ? el.value : ""; };
  const f = {};
  const pmin = parseFloat(v("pf-pmin")); if (pmin > 0) f.min_price = pmin;
  const pmax = parseFloat(v("pf-pmax")); if (pmax > 0) f.max_price = pmax;
  const beds = parseInt(v("pf-beds") || "0", 10); if (beds > 0) f.min_beds = beds;
  const baths = parseFloat(v("pf-baths") || "0"); if (baths > 0) f.min_baths = baths;
  const type = v("pf-type"); if (type) f.home_type = type;
  return f;
}
function preFilterQuery() {
  return Object.entries(preFilters())
    .map(([k, val]) => `&${k}=${encodeURIComponent(val)}`).join("");
}
function updateFiltersBadge() {
  const extra = ["pf-deep", "pf-hud"].filter(id => document.getElementById(id)?.checked).length;
  const n = Object.keys(preFilters()).length + extra;
  const badge = document.getElementById("filters-badge");
  if (badge) { badge.textContent = String(n); badge.hidden = n === 0; }
}
$("#filters-toggle")?.addEventListener("click", (e) => {
  e.stopPropagation();
  toggleDropdown("filters-menu", e.currentTarget);
});
["pf-pmin", "pf-pmax", "pf-beds", "pf-baths", "pf-type"].forEach(id => {
  const el = document.getElementById(id);
  if (el) el.addEventListener("input", updateFiltersBadge);
});
["pf-deep", "pf-hud"].forEach(id => document.getElementById(id)?.addEventListener("change", updateFiltersBadge));
$("#pf-clear")?.addEventListener("click", () => {
  ["pf-pmin", "pf-pmax"].forEach(id => { const el = document.getElementById(id); if (el) el.value = ""; });
  ["pf-beds", "pf-baths"].forEach(id => { const el = document.getElementById(id); if (el) el.value = "0"; });
  const t = document.getElementById("pf-type"); if (t) t.value = "";
  ["pf-deep", "pf-hud"].forEach(id => { const el = document.getElementById(id); if (el) el.checked = false; });
  updateFiltersBadge();
});

// --- underwriting assumptions (tune the deal math; applied to the next search) ---
function updateAssumeBadge() {
  let n = 0;
  for (const [id, , def] of ASSUMPTION_FIELDS) {
    const el = document.getElementById(id);
    if (el && el.value !== "" && parseFloat(el.value) !== def) n++;
  }
  const badge = document.getElementById("assume-badge");
  if (badge) { badge.textContent = String(n); badge.hidden = n === 0; }
}
$("#assume-toggle")?.addEventListener("click", (e) => {
  e.stopPropagation();
  toggleDropdown("assume-menu", e.currentTarget);
});
ASSUMPTION_FIELDS.forEach(([id]) => {
  const el = document.getElementById(id);
  if (el) el.addEventListener("input", updateAssumeBadge);
});
$("#as-clear")?.addEventListener("click", () => {
  ASSUMPTION_FIELDS.forEach(([id]) => { const el = document.getElementById(id); if (el) el.value = ""; });
  updateAssumeBadge();
});
// Click anywhere outside an open dropdown closes it.
document.addEventListener("click", (e) => {
  if (!e.target.closest(".dropdown")) closeAllDropdowns();
});

// --- print / save-as-PDF report ---
$("#print-report-btn")?.addEventListener("click", () => buildPrintReport());
$("#download-pdf-btn")?.addEventListener("click", () => downloadPdf());

// --- save city button ---
$("#save-city-btn").addEventListener("click", async () => {
  if (!currentSlug) return;
  const on = !favorites.cities.has(currentSlug);
  await api.favCity(currentSlug, on);
  await refreshFavorites();
});

async function openFavoritesModal() {
  const f = await api.favorites();

  // --- Saved Listings tab: resolve each {zpid, city_slug} to the actual property data ---
  if (!f.properties.length) {
    $("#fav-props").innerHTML = `<div class="fav-empty">No favorited listings yet. Click ☆ on any property card.</div>`;
  } else {
    // Group by city to minimize history fetches
    const byCity = {};
    for (const p of f.properties) {
      (byCity[p.city_slug] ||= []).push(p.zpid);
    }
    const cityData = {};
    for (const slug of Object.keys(byCity)) {
      if (!slug) continue;
      try { cityData[slug] = await api.historyOne(slug); }
      catch { cityData[slug] = null; }
    }
    const cards = [];
    for (const p of f.properties) {
      const hist = cityData[p.city_slug];
      const rec = hist?.results?.find(r => r.zpid === p.zpid);
      if (!rec) {
        cards.push(`
          <div class="fav-card" data-orphan>
            <div class="fav-info">
              <div class="fav-addr">Property ${p.zpid}</div>
              <div class="fav-city muted">${p.city_slug || "city unknown"} — not in archive (re-run that city to see it)</div>
            </div>
            <button class="fav-remove" data-fav-remove="${p.zpid}" title="Remove from favorites">×</button>
          </div>`);
        continue;
      }
      cards.push(`
        <div class="fav-card" data-load-prop="${p.zpid}" data-city-slug="${p.city_slug}">
          <div class="fav-thumb" style="${rec.photo ? `background-image:url('${rec.photo}')` : ''}"></div>
          <div class="fav-info">
            <div class="fav-addr">${rec.address}</div>
            <div class="fav-city">${rec.city}, ${rec.state} · $${(rec.purchase_price || 0).toLocaleString()}</div>
            <div class="fav-meta">
              <span class="verdict ${rec.verdict}">Flip: ${rec.verdict.replace(/_/g, ' ')}</span>
              <span class="verdict ${rec.rental_verdict || 'NO_RENT_DATA'}">Rent: ${(rec.rental_verdict || 'NO_RENT_DATA').replace(/_/g, ' ')}</span>
            </div>
          </div>
          <button class="fav-remove" data-fav-remove="${p.zpid}" title="Remove from favorites">×</button>
        </div>`);
    }
    $("#fav-props").innerHTML = cards.join("");
  }

  // --- Watched Cities tab ---
  if (!f.cities.length) {
    $("#fav-cities").innerHTML = `<div class="fav-empty">No saved cities yet. Click "Save this city" after a search.</div>`;
  } else {
    const cards = [];
    for (const slug of f.cities) {
      try {
        const hist = await api.historyOne(slug);
        cards.push(`
          <div class="fav-card" data-load-city="${slug}">
            <div class="fav-info">
              <div class="fav-addr">${hist.city}</div>
              <div class="fav-city">${hist.results.length} listings · queried ${timeAgo(hist.queried_at)}</div>
            </div>
            <button class="fav-remove" data-fav-city-remove="${slug}" title="Remove from favorites">×</button>
          </div>`);
      } catch {
        cards.push(`
          <div class="fav-card">
            <div class="fav-info">
              <div class="fav-addr">${slug}</div>
              <div class="fav-city muted">(no cached data — re-run to see)</div>
            </div>
            <button class="fav-remove" data-fav-city-remove="${slug}" title="Remove from favorites">×</button>
          </div>`);
      }
    }
    $("#fav-cities").innerHTML = cards.join("");
  }

  // --- Click handlers ---
  $$("#fav-props [data-load-prop]").forEach(el => {
    el.addEventListener("click", async () => {
      const zpid = el.dataset.loadProp;
      const slug = el.dataset.citySlug;
      $("#modal-favorites").hidden = true;
      await loadFromHistory(slug);
      setTimeout(() => scrollToCard(zpid), 100);
    });
  });
  $$("#fav-cities [data-load-city]").forEach(el => {
    el.addEventListener("click", () => {
      const slug = el.dataset.loadCity;
      $("#modal-favorites").hidden = true;
      loadFromHistory(slug);
    });
  });
  $$("[data-fav-remove]").forEach(btn => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      await api.favProp(btn.dataset.favRemove, false);
      await refreshFavorites();
      openFavoritesModal();
    });
  });
  $$("[data-fav-city-remove]").forEach(btn => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      await api.favCity(btn.dataset.favCityRemove, false);
      await refreshFavorites();
      openFavoritesModal();
    });
  });

  $("#modal-favorites").hidden = false;
}

function scrollToCard(zpid) {
  const card = $(`#card-${zpid}`);
  if (!card) return;
  card.scrollIntoView({behavior: "smooth", block: "center"});
  card.classList.add("highlight");
  setTimeout(() => card.classList.remove("highlight"), 3000);
}

$("#nav-favorites").addEventListener("click", openFavoritesModal);

$$(".tab").forEach(t => {
  t.addEventListener("click", () => {
    $$(".tab").forEach(x => x.classList.toggle("active", x === t));
    const which = t.dataset.tab;
    $("#fav-props").hidden = which !== "props";
    $("#fav-cities").hidden = which !== "cities";
    $("#fav-props").classList.toggle("active", which === "props");
    $("#fav-cities").classList.toggle("active", which === "cities");
  });
});

// --- form ---
$("#search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const city = $("#city").value.trim();
  const count = parseInt($("#count").value, 10) || 10;
  const intent = $("#intent").value || "both";
  if (!city) return;
  startSearch(city, count, intent);
});

$("#logout-btn").addEventListener("click", async () => {
  // Redirect even if the logout call fails (network blip, cold start) — the server-side
  // cookie clear is best-effort; landing on /login is the part the user must always get.
  try { await api.logout(); } catch (_) { /* proceed */ }
  window.location = "/login";
});

$("#quota-banner-close").addEventListener("click", hideQuota);

// --- filter / sort bar wiring ---
["sort-by", "filter-beds", "filter-baths", "filter-type"].forEach(id => {
  const el = document.getElementById(id);
  if (el) el.addEventListener("change", applySortFilter);
});
["filter-pmin", "filter-pmax"].forEach(id => {
  const el = document.getElementById(id);
  if (el) el.addEventListener("input", applySortFilter);
});
{
  const reset = document.getElementById("filter-reset");
  if (reset) reset.addEventListener("click", () => {
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
    set("sort-by", "best"); set("filter-beds", "0"); set("filter-baths", "0");
    set("filter-type", ""); set("filter-pmin", ""); set("filter-pmax", "");
    applySortFilter();
  });
}

// --- active runs panel ---
let _activeSources = {};

async function fetchActiveRuns() {
  try {
    const r = await fetch('/api/runs', {credentials: 'include'});
    if (!r.ok) return;
    const data = await r.json();
    renderActiveRuns(data.runs || []);
  } catch (e) { /* ignore */ }
}

function renderActiveRuns(runs) {
  const ul = $('#active-runs');
  if (!ul) return;
  // Only show truly active runs (pending/running). Keep UI small.
  const active = runs.filter(r => (r.status || 'pending') === 'pending' || (r.status || '') === 'running');
  // Toolbar Active Runs toggle: show a count badge; hide the button entirely when idle.
  const toggle = document.getElementById('active-toggle');
  const badge = document.getElementById('active-badge');
  if (badge) badge.textContent = String(active.length);
  if (toggle) toggle.hidden = active.length === 0;
  if (!active.length) {
    ul.innerHTML = '<li class="muted">No active runs</li>';
    const menu = document.getElementById('active-menu');
    if (menu) menu.hidden = true;  // collapse its own menu (leave other dropdowns alone)
    return;
  }
  ul.innerHTML = active.map(run => {
    const id = run.id;
    const started = timeAgo(run.started_at);
    const status = run.status || 'pending';
    return `<li data-run-id="${id}" tabindex="0" role="button">
      <div class="run-city">${run.city}</div>
      <div class="run-meta">${status} · ${started}</div>
      <div class="run-progress" id="run-progress-${id}"></div>
    </li>`;
  }).join('');

  // Click handlers: open run details in right pane
  ul.querySelectorAll('li[data-run-id]').forEach(li => {
    li.addEventListener('click', (e) => {
      const id = parseInt(li.dataset.runId, 10);
      $$('#active-runs li').forEach(x => x.classList.toggle('selected', x === li));
      viewRun(id);
      closeAllDropdowns();
    });
    li.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); li.click(); } });
  });

  // attach SSE to running ones for compact progress in list
  active.forEach(run => {
    if ((run.status === 'pending' || run.status === 'running') && !_activeSources[run.id]) {
      const es = new EventSource(`/api/runs/${run.id}/events`, {withCredentials: true});
      _activeSources[run.id] = es;
      es.addEventListener('enrich_tick', (e) => {
        try {
          const d = JSON.parse(e.data);
          const el = $(`#run-progress-${run.id}`);
          if (el) el.textContent = `Enriching — ${d.elapsed}s elapsed (${d.requested} requested)`;
        } catch(_){}
      });
      es.addEventListener('property', (e) => {
        try {
          const d = JSON.parse(e.data);
          const el = $(`#run-progress-${run.id}`);
          if (el) {
            el.textContent = `Got property ${d.zpid} — ${d.verdict || ''}`;
          }
        } catch(_){}
      });
      es.addEventListener('status', (e) => {
        try { const d = JSON.parse(e.data); const el = $(`#run-progress-${run.id}`); if (el) el.textContent = d.message; } catch(_){}
      });
      es.addEventListener('complete', (e) => {
        try { const d = JSON.parse(e.data); const el = $(`#run-progress-${run.id}`); if (el) el.textContent = `Complete — ${d.total} items`; } catch(_){}
        // close source and refresh archive
        setTimeout(() => { es.close(); delete _activeSources[run.id]; fetchActiveRuns(); refreshHistory(); }, 1500);
      });
      es.addEventListener('error', (e) => {
        try { const d = JSON.parse(e.data); const el = $(`#run-progress-${run.id}`); if (el) el.textContent = `Error: ${d.message}`; } catch(_){}
        es.close(); delete _activeSources[run.id]; fetchActiveRuns();
      });
    }
  });
}

// View a run's live output in the right panel
let _viewSource = null;
function viewRun(runId) {
  // close previous view source
  if (_viewSource) { try { _viewSource.close(); } catch(_){} _viewSource = null; }
  // clear results grid; show the scout loader until this run completes
  $("#results-grid").innerHTML = '';
  showScoutLoader();
  setStatus('Loading run output...');
  const es = new EventSource(`/api/runs/${runId}/events`, {withCredentials: true});
  _viewSource = es;
  es.addEventListener('status', (e) => { try { const d = JSON.parse(e.data); setStatus(d.message); } catch(_){} });
  es.addEventListener('discovery', (e) => {
    try {
      const d = JSON.parse(e.data);
      renderResults(d.city, d.properties);
      setStatus(`Found ${d.count} properties — enriching…`);
      showSpinner();
    } catch(_){}
  });
  es.addEventListener('enrich_tick', (e) => { try { const d = JSON.parse(e.data); setStatus(`Enriching — ${d.elapsed}s elapsed (${d.requested} requested)`); showSpinner(); } catch(_){} });
  es.addEventListener('property', (e) => { try { const r = JSON.parse(e.data); upgradeCard(r); } catch(_){} });
  es.addEventListener('trim', (e) => { try { const d = JSON.parse(e.data); trimGrid(d.keep); } catch(_){} });
  es.addEventListener('complete', (e) => {
    hideScoutLoader();  // reveal results + re-fit the map
    try { const d = JSON.parse(e.data); setStatus(`Run complete — ${d.total} items`); } catch(_){}
    hideSpinner();
    setTimeout(() => { try { es.close(); } catch(_){} _viewSource = null; refreshHistory(); fetchActiveRuns(); }, 1000);
  });
  es.addEventListener('error', (e) => {
    let msg = '⚠️ Run failed';
    try { msg = `⚠️ ${JSON.parse(e.data).message}`; } catch(_){}
    setStatus(msg);
    scoutError(msg);
    hideSpinner();
    try { es.close(); } catch(_){} _viewSource = null; fetchActiveRuns();
  });
}

// poll active runs periodically
setInterval(fetchActiveRuns, 5000);

// --- init ---
(async () => {
  if (!(await ensureAuth())) return;
  refreshHistory();
  refreshFavorites();
})();

// --- photo lazy-loading + map resize ---
(function(){
  // Re-tile the Leaflet map on viewport changes (full-width map now; no sidebar to collapse).
  let _rsTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(_rsTimer);
    _rsTimer = setTimeout(() => {
      if (typeof _map !== 'undefined' && _map) { try { _map.invalidateSize(false); } catch (_) {} }
    }, 200);
  });

  // Lazy-load card photos using IntersectionObserver
  const io = new IntersectionObserver((entries) => {
    for (const ent of entries) {
      const el = ent.target;
      if (ent.isIntersecting) {
        // mark visible
        el._visible = true;
        const photosJson = el.dataset.photos;
        let url = el.dataset.photo || null;
        try {
          if (photosJson) {
            const arr = JSON.parse(photosJson);
            // Each entry is now a DISTINCT photo ({url,width,srcset?} or a url string).
            // The cover photo is simply the first distinct photo.
            if (arr.length) url = photoUrl(arr[0]) || url;
          }
        } catch(_){ }
        // If upgradeCard already determined BD URLs fail for this element, use Skolit fallback
        if (el._bdFailed) url = el.dataset.photoOriginal || el.dataset.photo || url;
        if (url) {
          const fallback = el.dataset.photoOriginal || '';
          const isBdUrl = url !== fallback;
          if (isBdUrl) {
            // Probe before committing — avoids overwriting a working Skolit bg with a broken BD url
            _probePhoto(el, url, fallback || null);
          } else {
            el.style.backgroundImage = `url('${url}')`;
          }
          el.classList.remove('placeholder');
        }
        io.unobserve(el);
      }
    }
  }, {rootMargin: '300px 0px', threshold: 0.01});

  // Observe existing photo divs and future ones
  function observePhotos() {
    document.querySelectorAll('.card-photo').forEach(p => {
      if (!p._observed) { io.observe(p); p._observed = true; }
    });
  }
  // run on init and after discovery renders
  observePhotos();
  // Re-run periodically to catch newly added cards
  const obsTimer = setInterval(observePhotos, 1500);

})();


