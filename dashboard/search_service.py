"""
Search pipeline as an async generator yielding SSE events.

Events:
  - status:     {message: str}
  - quota:      {api: 'rapidapi'|'bright_data', message: str}
  - error:      {message: str}
  - discovery:  {city, count, properties: [base_card,...]}  (immediate)
  - enrich_tick:{elapsed: int, requested: int}
  - property:   {zpid, photo, report}  (one per scored property)
  - complete:   {city, slug, total, queried_at, summary: {...}}

Notes on quota: we let the existing scrapers/enricher do their thing; we wrap their
exceptions and surface 'quota'/'error' events. Quota is detected from RapidAPI
rate-limit headers (when discovery returns) and Bright Data 402/429 responses.
"""
import asyncio
import json
import logging
import math
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import AsyncIterator, Dict, Iterable, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from models.property import Property
from agents.flipper_evaluator import FlipperEvaluator
from scrapers.bright_data_enricher import BrightDataZillowEnricher

logger = logging.getLogger(__name__)

_EXECUTOR = ThreadPoolExecutor(max_workers=4)


def _zpid_of(prop) -> str:
    return prop.property_id.replace("ZILLOW-", "")


def _base_card(prop) -> dict:
    """Card data available immediately from RapidAPI discovery (no Bright Data yet)."""
    return {
        "zpid": _zpid_of(prop),
        "address": prop.address,
        "city": prop.city,
        "state": prop.state,
        "price": prop.price,
        "bedrooms": prop.bedrooms,
        "bathrooms": prop.bathrooms,
        "sqft": prop.sqft,
        "home_type": prop.property_type,
        "photo": getattr(prop, "img_src", None),  # may be None; frontend uses placeholder
        "link": getattr(prop, "link", ""),
        "latitude": getattr(prop, "latitude", None),
        "longitude": getattr(prop, "longitude", None),
        "zipcode": getattr(prop, "zipcode", "") or "",  # per-area grouping/labels
        "zestimate": getattr(prop, "zestimate", 0) or 0,
        "days_on_zillow": getattr(prop, "days_on_zillow", 0) or 0,
        "enriched": False,
    }


def _best_photo_variant(jpegs: list, target: int = 1024) -> Optional[dict]:
    """From the responsive width-variants of ONE Bright Data photo, pick a single
    representative URL (smallest width >= target, else the largest available).

    Bright Data shape: photos[N]['mixedSources']['jpeg'] = [{url, width}, ...] where
    every entry is the SAME image at a different width. Returns one
    {url, width, srcset:[{url,width}...]} per distinct photo so the carousel shows
    DISTINCT images, not the same photo at 8 resolutions.
    """
    variants = [
        {"url": j.get("url"), "width": j.get("width") or j.get("w")}
        for j in jpegs if isinstance(j, dict) and j.get("url")
    ]
    if not variants:
        return None
    with_w = [v for v in variants if v["width"]]
    if with_w:
        with_w.sort(key=lambda v: v["width"])
        chosen = next((v for v in with_w if v["width"] >= target), with_w[-1])
    else:
        chosen = variants[0]
    return {"url": chosen["url"], "width": chosen["width"], "srcset": variants}


def _flip_report_to_dict(a, prop, enriched) -> dict:
    """Serialize a FlipReport + cover photo into a UI-friendly dict.

    Photos are returned as a list of objects [{url, width, srcset?}] — ONE entry per
    distinct property photo (not per responsive width-variant).
    """
    photos_list = []
    if enriched:
        raw_photos = enriched.get("photos") or []
        for item in raw_photos:
            # Each item is ONE distinct property photo. Append a SINGLE representative
            # url per photo so the carousel cycles through different images.
            if isinstance(item, dict):
                jpegs = item.get("mixedSources", {}).get("jpeg") or []
                if jpegs:
                    best = _best_photo_variant(jpegs)
                    if best:
                        photos_list.append(best)
                elif item.get("url"):
                    photos_list.append({"url": item.get("url"), "width": item.get("width")})
            elif isinstance(item, str):
                photos_list.append({"url": item})

    # Cap at 10 DISTINCT photos — Bright Data can return hundreds; carousel needs a few
    photos_list = photos_list[:10]

    # Fallback to the Skolit discovery thumbnail when Bright Data has no photos
    if not photos_list:
        img = getattr(prop, "img_src", None)
        if img:
            photos_list = [{"url": img}]

    # Default cover photo = first distinct photo (already the ~1024px variant)
    photo = photos_list[0].get("url") if photos_list else None

    return {
        "zpid": _zpid_of(prop),
        "address": prop.address,
        "city": prop.city,
        "state": prop.state,
        "link": getattr(prop, "link", ""),
        "photo": photo,
        "photos": photos_list,
        "latitude": (enriched.get("latitude") if enriched else None) or getattr(prop, "latitude", None),
        "longitude": (enriched.get("longitude") if enriched else None) or getattr(prop, "longitude", None),
        "enriched": bool(enriched),
        "verdict": a.verdict,
        "verdict_reason": a.verdict_reason,
        "flip_score": a.flip_score,
        "purchase_price": a.purchase_price,
        "list_price": a.list_price,        # differs from purchase_price only when the user set a basis
        "purchase_psf": a.purchase_psf,
        "bedrooms": (enriched.get("bedrooms") if enriched else None) or getattr(prop, "bedrooms", None),
        "bathrooms": (enriched.get("bathrooms") if enriched else None) or getattr(prop, "bathrooms", None),
        "sqft": a.sqft,
        "year_built": a.year_built,
        "home_type": a.home_type,
        "days_on_market": a.days_on_market,
        "list_psf": a.list_psf,
        "arv": a.arv,
        "arv_source": a.arv_source,
        "arv_confidence": a.arv_confidence,
        "comp_count": a.comp_count,
        "comp_psf_range": list(a.comp_psf_range) if a.comp_psf_range else [None, None],
        "comp_provenance": a.comp_provenance,
        "enriched_at": a.enriched_at,
        "rehab_estimate": a.rehab_estimate,
        "rehab_psf": a.rehab_psf,
        "rehab_signal": a.rehab_signal,
        "buy_closing_cost": a.buy_closing_cost,
        "holding_cost_6mo": a.holding_cost_6mo,
        "financing_cost": a.financing_cost,
        "selling_cost": a.selling_cost,
        "all_in_cost": a.all_in_cost,
        "net_resale": a.net_resale,
        "projected_profit": a.projected_profit,
        "profit_margin_pct": a.profit_margin_pct,
        "mao_70_rule": a.mao_70_rule,
        "passes_70_rule": a.passes_70_rule,
        "monthly_rent_est": a.monthly_rent_est,
        "monthly_noi": a.monthly_noi,
        "cap_rate_pct": a.cap_rate_pct,
        "monthly_cash_flow": a.monthly_cash_flow,
        "brrrr_refi_proceeds": a.brrrr_refi_proceeds,
        "rental_verdict": a.rental_verdict,
        "rental_verdict_reason": a.rental_verdict_reason,
        "rental_score": a.rental_score,
        "risk_flags": a.risk_flags,
        "comps_summary": a.comps_summary,
    }


# Deep-scan tuning. The Skolit /bylocation endpoint caps any single location at ~800
# listings / 20 pages, so a big city like Sacramento returns a *truncated* market. A
# zip or neighborhood slug, however, returns its own small market (verified: 95818→49,
# 95820→65). When the user opts into a deep scan AND the city is capped, we fan out one
# fetch per active zip and merge+dedupe — assembling well past the 800 ceiling.
_CAP_THRESHOLD = 800        # market total at/above this ⇒ city is truncated ⇒ fan out
_DEEP_MAX_ZIPS = 30         # cap zips per deep run (quota guardrail)
_DEEP_ZIP_PAGES = 2         # pages per zip (scraper early-stops via has_next; usually 1)
_DEEP_CONCURRENCY = 6       # parallel zip fetches
_DEEP_CITY_PAGES = 6        # citywide pages in deep mode (just enough to enumerate zips)

# County/city SCOPE fan-out (crosswalk-driven, dashboard/geo.py). Unlike deep scan, the zip
# set is the COMPLETE authoritative list for the scope — so no neighborhood is missed. A
# county is never covered by one query, so we always fan out over every zip. Env-overridable.
import os as _os_mod  # noqa: E402
_COUNTY_MAX_ZIPS = int(_os_mod.environ.get("COUNTY_MAX_ZIPS", "120"))   # guardrail for huge counties
_COUNTY_ZIP_PAGES = int(_os_mod.environ.get("COUNTY_ZIP_PAGES", "4"))   # per-zip depth (dense zips)
_COUNTY_CONCURRENCY = int(_os_mod.environ.get("COUNTY_CONCURRENCY", "4"))  # Render free = 1 worker


def _parse_props(raw: list) -> list:
    """Build Property objects from the scraper's JSON payload."""
    props = []
    for p in raw:
        prop = Property(
            property_id=p["property_id"], address=p["address"], city=p["city"],
            state=p["state"], price=p["price"], bedrooms=p["bedrooms"],
            bathrooms=p["bathrooms"], sqft=p["sqft"], year_built=p["year_built"],
            property_type=p["property_type"], estimated_rent=p["estimated_rent"],
            hoa_fees=p["hoa_fees"], property_tax_annual=p["property_tax_annual"],
            insurance_annual=p["insurance_annual"],
        )
        prop.link = p.get("link", "")
        prop.img_src = p.get("img_src") or p.get("photo")
        # Geo coords for map pins (available immediately from RapidAPI discovery)
        prop.latitude = p.get("latitude")
        prop.longitude = p.get("longitude")
        prop.zipcode = (p.get("zipcode") or "").strip()  # deep-scan per-zip fan-out
        # Carry discovery-phase signals from RapidAPI (available before Bright Data)
        prop.zestimate = p.get("zestimate") or 0
        prop.days_on_zillow = p.get("days_on_zillow") or 0
        prop.tax_assessed_value = p.get("tax_assessed_value") or 0
        props.append(prop)
    return props


def _fetch_one(location: str, max_pages) -> tuple:
    """One RapidAPI scraper subprocess. Returns (props, quota_signal, market_total)."""
    import re as _re
    result = subprocess.run(
        [sys.executable, "scrapers/zillow_api_scraper.py", location, str(max_pages)],
        capture_output=True, text=True, timeout=300,  # full-market (20pg) scans run ~2min
        cwd=str(PROJECT_ROOT),
    )
    out, stderr = result.stdout, result.stderr
    quota_signal = None
    # Cheap quota detect from stderr (we don't have headers exposed to subprocess output)
    if ("429" in stderr or "QUOTA_EXCEEDED" in stderr or
            "rate limit" in stderr.lower() or "quota" in stderr.lower()):
        quota_signal = "RapidAPI rate limit hit"
    m = _re.search(r"Market total=(\d+)", stderr)
    market_total = int(m.group(1)) if m else 0
    if "JSON_START:" not in out:
        return [], quota_signal or f"discovery failed: {stderr[-300:]}", market_total
    try:
        raw = json.loads(out.split("JSON_START:")[1].split(":JSON_END")[0])
    except (json.JSONDecodeError, IndexError) as e:
        # Subprocess crashed mid-output or emitted malformed JSON — fail gracefully
        # instead of crashing the SSE stream with an unhandled exception.
        return [], quota_signal or f"discovery JSON parse failed: {str(e)[:200]}", market_total
    return _parse_props(raw), quota_signal, market_total


def _deep_fanout(city_props: list, zips: Optional[list] = None,
                 zip_pages: int = _DEEP_ZIP_PAGES, max_zips: int = _DEEP_MAX_ZIPS,
                 concurrency: int = _DEEP_CONCURRENCY, on_progress=None) -> list:
    """Fan out one fetch per zip and merge into city_props, deduping by property_id.

    zips=None → derive the zip set from the citywide listings (scan-derived deep scan).
    zips=[...] → fan out over an EXPLICIT, complete list (crosswalk-driven county/city scope) —
    this is what guarantees every neighborhood is scanned, including ones a single capped scan
    never surfaced. on_progress(done, total) is called after each zip completes, if provided."""
    import re as _re
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if zips is None:
        counts: Counter = Counter()
        for p in city_props:
            z = (getattr(p, "zipcode", "") or "").strip()
            if _re.fullmatch(r"\d{5}", z):
                counts[z] += 1
        zips = [z for z, _ in counts.most_common(max_zips)]
    else:
        zips = [z for z in zips if _re.fullmatch(r"\d{5}", str(z))][:max_zips]
    if not zips:
        return city_props

    seen = {p.property_id for p in city_props}
    merged = list(city_props)
    done = 0

    def _one(z):
        zp, _q, _t = _fetch_one(z, zip_pages)
        return zp

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        for fut in as_completed([ex.submit(_one, z) for z in zips]):
            try:
                for zp in fut.result():
                    if zp.property_id and zp.property_id not in seen:
                        seen.add(zp.property_id)
                        merged.append(zp)
            except Exception as e:
                logger.warning("deep zip fetch failed: %s", e)
            done += 1
            if on_progress:
                try:
                    on_progress(done, len(zips))
                except Exception:
                    pass

    logger.info("deep fanout: %d zips, %d→%d listings", len(zips), len(city_props), len(merged))
    return merged


def _discover(location: str, deep: bool = False, scope=None, on_progress=None):
    """Run the RapidAPI scraper and return (props, quota_signal).

    RAPIDAPI_MAX_PAGES controls how many pages a single-location scan fetches (default 20 —
    the API tops out at ~800 listings, scraper early-stops via has_next).

    scope (dashboard.geo.Scope), with deep=True, drives a COMPLETE crosswalk-based fan-out:
      • county → always fan out over EVERY residential zip in the county (one query can never
        cover a whole county) — this is the core "don't miss any neighborhood" guarantee.
      • city → if the single city scan is capped (≥800), fan out over the city's authoritative
        zip list (more complete than scan-derived); if not capped, the one scan is already the
        full city market.
    deep without a resolvable scope (kind='raw') keeps the original scan-derived fan-out.

    Quota: a normal run ≤20 calls; a county run ≈ len(zips) × COUNTY_ZIP_PAGES (Sacramento ~66
    zips × ~2 effective pages ≈ ~120 calls — ~1–2% of the 10k/mo Pro quota).
    """
    import os as _os
    max_pages = _os.environ.get("RAPIDAPI_MAX_PAGES", "20")
    kind = getattr(scope, "kind", "raw") if scope else "raw"
    scope_zips = (getattr(scope, "zips", None) or []) if scope else []

    # County scope: fan out over the complete authoritative zip list (always).
    if deep and kind == "county" and scope_zips:
        zips = scope_zips[:_COUNTY_MAX_ZIPS]
        first, quota_signal, _ = _fetch_one(zips[0], _COUNTY_ZIP_PAGES)
        props = _deep_fanout(first, zips=zips[1:], zip_pages=_COUNTY_ZIP_PAGES,
                             max_zips=_COUNTY_MAX_ZIPS, concurrency=_COUNTY_CONCURRENCY,
                             on_progress=on_progress)
        return props, quota_signal

    # City / raw: single scan first; fan out only if the market is capped.
    city_pages = min(int(max_pages), _DEEP_CITY_PAGES) if deep else max_pages
    props, quota_signal, total = _fetch_one(location, city_pages)
    if deep and props and total and total >= _CAP_THRESHOLD:
        try:
            if kind == "city" and scope_zips:
                props = _deep_fanout(props, zips=scope_zips, zip_pages=_DEEP_ZIP_PAGES,
                                     max_zips=_COUNTY_MAX_ZIPS, concurrency=_DEEP_CONCURRENCY,
                                     on_progress=on_progress)
            else:
                props = _deep_fanout(props, on_progress=on_progress)
        except Exception as e:
            logger.warning("deep fanout failed (%s); using citywide pool", e)
    return props, quota_signal


def _detect_bd_quota_error(exc: Exception) -> Optional[str]:
    msg = str(exc)
    low = msg.lower()
    if "402" in msg or "Payment Required" in msg or "quota" in low or "credit" in low:
        return msg
    if "not active" in low or "customer is not active" in low or ("400" in msg and "inactive" in low):
        return (
            "Bright Data account is inactive — your subscription may have lapsed. "
            "Log in to brightdata.com/cp to reactivate, then retry."
        )
    return None


# --- Cross-subject comp pooling -------------------------------------------------
# Every enrichment's nearbyHomes carries a few RecentlySold entries; individually each
# subject sees only its own ~8, but across a market scan they form a real sold-comp pool.
POOL_RADIUS_KM = 1.6   # ~1 mile — standard appraisal proximity for suburban comps
POOL_MAX_COMPS = 12    # cap per subject; nearest first (analyze_comps filters further)


def _build_sold_pool(enriched_map: dict) -> list:
    """One market-wide pool of unique, usable SOLD comps from every enrichment's
    nearbyHomes. Usable = price + sqft + geo (analyze_comps re-applies its own filters)."""
    pool: dict = {}
    for rec in (enriched_map or {}).values():
        for e in rec.get("nearbyHomes") or []:
            if isinstance(e, str):
                try:
                    e = json.loads(e)
                except Exception:
                    continue
            if not isinstance(e, dict):
                continue
            if str(e.get("hdpTypeDimension") or "").lower() != "recentlysold":
                continue
            z = str(e.get("zpid") or "").strip()
            if not (z and e.get("price") and e.get("livingArea")
                    and e.get("latitude") and e.get("longitude")):
                continue
            pool[z] = e
    return list(pool.values())


def _pool_comps_near(pool: list, lat, lng,
                     radius_km: float = POOL_RADIUS_KM,
                     limit: int = POOL_MAX_COMPS) -> list:
    """Nearest pooled sold comps within radius of the subject (equirectangular approx —
    exact enough at ~1-mile scale). Returns [] without geo, keeping evaluate() unchanged."""
    if not (pool and lat and lng):
        return []
    out = []
    for e in pool:
        try:
            dx = (e["longitude"] - lng) * math.cos(math.radians((lat + e["latitude"]) / 2)) * 111.32
            dy = (e["latitude"] - lat) * 110.57
            d = (dx * dx + dy * dy) ** 0.5
        except (TypeError, KeyError):
            continue
        if d <= radius_km:
            out.append((d, e))
    out.sort(key=lambda t: t[0])
    return [e for _, e in out[:limit]]


# Tunable financial assumptions the dashboard can pass through to FlipperEvaluator.
# Each maps to an evaluator kwarg with a (min, max) clamp — a hard guardrail at this trust
# boundary so a hand-crafted query string can't inject a nonsensical (or non-numeric) value.
_ASSUMPTION_BOUNDS = {
    "hold_months":      (1, 24),
    "hard_money_apr":   (0.0, 0.40),
    "ltv":              (0.50, 0.95),
    "selling_cost_pct": (0.0, 0.15),
    "buy_closing_pct":  (0.0, 0.06),
    "points_pct":       (0.0, 0.06),
    "rental_opex_pct":  (0.10, 0.70),
    "refi_apr":         (0.0, 0.20),
    "refi_ltv":         (0.50, 0.90),
}


def _clean_assumptions(assumptions: Optional[dict]) -> dict:
    """Whitelist + range-clamp user-supplied assumptions into safe FlipperEvaluator kwargs.
    Unknown keys are dropped; non-numeric values are skipped; everything else is clamped to
    its bound so the evaluator only ever sees sane inputs."""
    out: dict = {}
    for key, (lo, hi) in _ASSUMPTION_BOUNDS.items():
        if not assumptions or key not in assumptions or assumptions[key] is None:
            continue
        try:
            val = float(assumptions[key])
        except (TypeError, ValueError):
            continue
        val = max(lo, min(hi, val))
        out[key] = int(round(val)) if key == "hold_months" else val
    return out


async def stream_search(city: str, count: int = 10, intent: str = "flip",
                         enrich_limit: Optional[int] = None,
                         user_email: Optional[str] = None,
                         min_price: Optional[float] = None,
                         max_price: Optional[float] = None,
                         min_beds: Optional[int] = None,
                         min_baths: Optional[float] = None,
                         home_type: Optional[str] = None,
                         deep: bool = False,
                         assumptions: Optional[dict] = None) -> AsyncIterator[dict]:
    """Async generator producing SSE-ready dicts.

    intent: 'flip' | 'rent' | 'both' — same analysis, different ranking.
    user_email: if set, runs are logged to the admin DB for cost telemetry + quota.
    """
    if intent not in ("flip", "rent", "both"):
        yield {"event": "error", "data": {"message": f"Unknown intent: {intent}"}}
        return

    # Cost knobs (overridable via .env)
    import os as _os
    bd_cost_per_record = float(_os.environ.get("BRIGHT_DATA_COST_PER_RECORD_USD", "0.0015"))
    rapidapi_cost_per_call = float(_os.environ.get("RAPIDAPI_COST_PER_CALL_USD", "0"))

    # Log run start if we have an authed user
    run_id: Optional[int] = None
    if user_email:
        try:
            from dashboard import db as _db
            run_id = _db.log_run_start(user_email, city, intent, count)
        except Exception as e:
            logger.warning("log_run_start failed: %s", e)

    def _finish_run(status: str = "ok"):
        # Close out the run row on any early return so it never sticks as 'pending'.
        if run_id is None:
            return
        try:
            from dashboard import db as _db
            _db.log_run_finish(
                run_id, enrich_attempted=0, enrich_from_cache=0, enrich_fresh=0,
                cost_bright_data_usd=0.0, cost_rapidapi_usd=0.0, status=status, error=None,
            )
        except Exception as e:
            logger.warning("log_run_finish failed: %s", e)

    yield {"event": "status", "data": {"message": f"Searching {city} ({intent})…"}}
    if run_id:
        try:
            from dashboard import db as _db
            _db.add_run_event(run_id, "status", json.dumps({"message": f"Searching {city} ({intent})…"}))
        except Exception:
            logger.exception("add_run_event failed")

    # Resolve geographic scope (county / city / raw) → complete zip list for the fan-out.
    from dashboard.geo import resolve_scope
    scope = resolve_scope(city)
    if scope.kind == "county":
        deep = True  # a county is never covered by one query — always fan out over every zip
        n_zips = min(len(scope.zips), _COUNTY_MAX_ZIPS)
        if len(scope.zips) > _COUNTY_MAX_ZIPS:
            yield {"event": "status", "data": {"message":
                f"{scope.label}: {len(scope.zips)} zips — scanning the first {_COUNTY_MAX_ZIPS}. "
                f"Narrow to a city for full depth."}}
        yield {"event": "scope", "data": {"kind": "county", "label": scope.label, "zip_count": n_zips}}
    elif scope.kind == "city" and scope.zips:
        yield {"event": "scope", "data": {"kind": "city", "label": scope.label, "zip_count": len(scope.zips)}}

    loop = asyncio.get_running_loop()
    client_disconnected = False
    discovery_fut = loop.run_in_executor(_EXECUTOR, _discover, city, deep, scope)
    # Poll discovery with periodic heartbeats. A full-market (20-page) scan can take
    # ~2 min, so we keep the SSE stream alive (every ≤4s) and feed the scout loader a
    # live counter — going silent for 2 min risks a proxy dropping the connection.
    _elapsed = 0
    while not discovery_fut.done():
        try:
            await asyncio.sleep(4)
        except asyncio.CancelledError:
            client_disconnected = True
            logger.info("Client disconnected during discovery; continuing in background.")
            while not discovery_fut.done():
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    continue
            break
        _elapsed += 4
        if not client_disconnected:
            if scope.kind == "county":
                _scan_label = f"Scanning {scope.label} — {min(len(scope.zips), _COUNTY_MAX_ZIPS)} neighborhoods"
            elif deep:
                _scan_label = "Deep scan — every neighborhood"
            else:
                _scan_label = "Scanning the market"
            yield {"event": "status", "data": {"message": f"{_scan_label} — {_elapsed}s…"}}
    props, quota = discovery_fut.result()

    if quota and not props:
        yield {"event": "quota", "data": {
            "api": "rapidapi",
            "message": (
                "RapidAPI rate limit reached — the Zillow Live Data Scraper API (Skolit) has "
                "no calls remaining. Check your plan at rapidapi.com/hub and wait for the quota "
                "to reset, or upgrade your plan."
            ),
        }}
        _finish_run("quota")
        return

    if not props:
        yield {"event": "error", "data": {"message": "No properties returned for that location."}}
        _finish_run("ok")
        return

    # Drop bad discovery data: sqft=0, and implausibly-low prices (a real for-sale home
    # is never < $10k — values like "$300" are a listing that dropped its trailing 000
    # upstream, which would otherwise produce a nonsensical report, e.g. a 45% cap rate).
    props = [p for p in props if p.sqft and p.sqft > 0 and (p.price or 0) >= 10000]
    _discovered_total = len(props)  # full for-sale market scanned (vs the top slice we enrich)

    # ── Discovery-phase market scan + pre-enrichment filtering (no extra API calls) ──
    # Skolit's /bylocation returns no zestimate, so we rank by how far each listing
    # sits BELOW the $/sqft of *comparable* homes — same neighborhood (a ~1km lat/long
    # cell) AND same property type — falling back to citywide-by-type, then citywide,
    # when a neighborhood is too sparse to trust. Cheaper-vs-local-comps = likely fixer /
    # value-add / motivated seller = better flip candidate, and ranking against LOCAL
    # comps catches homes underpriced relative to their own (pricier) block — the blind
    # spot of a single citywide median.
    pool = [p for p in props if getattr(p, "link", "")]

    def _psf(p):
        return (p.price / p.sqft) if (p.sqft and p.price) else None

    def _type_bucket(p):
        t = str(getattr(p, "property_type", "") or "").lower()
        if "condo" in t:
            return "condo"
        if "town" in t:
            return "townhouse"
        if any(w in t for w in ("multi", "duplex", "triplex", "apartment")):
            return "multi"
        return "house"

    def _cell(p):
        lat, lng = getattr(p, "latitude", None), getattr(p, "longitude", None)
        if lat is None or lng is None:
            return None
        try:
            return (round(float(lat), 2), round(float(lng), 2))  # ~1km neighborhood cell
        except (TypeError, ValueError):
            return None

    # ── Pre-enrichment filtering: constrain the pool to the user's criteria BEFORE we
    #    pick the 30–50 to enrich, so Bright Data never pays to analyze listings the user
    #    would discard. Discovery-phase fields only (all present pre-enrichment). ──
    def _passes_filter(p):
        price = p.price or 0
        # When a price filter is set, a listing with no usable price can't be
        # confirmed to match — exclude it rather than silently letting it through.
        if (min_price or max_price) and price <= 0:
            return False
        if min_price and price < min_price:
            return False
        if max_price and price > max_price:
            return False
        if min_beds and (p.bedrooms or 0) < min_beds:
            return False
        if min_baths and (p.bathrooms or 0) < min_baths:
            return False
        if home_type and _type_bucket(p) != home_type:
            return False
        return True

    _filters_active = bool(home_type) or any(
        v is not None for v in (min_price, max_price, min_beds, min_baths)
    )
    if _filters_active:
        pool = [p for p in pool if _passes_filter(p)]
        if not pool:
            yield {"event": "error", "data": {
                "message": "No listings matched your filters — widen the price/beds/baths/type and try again."
            }}
            _finish_run("ok")
            return

    # ── Local, type-segmented $/sqft baselines ──
    # Fallback chain, most → least local: cell+type → ZIP+type → ZIP → citywide type → citywide.
    # The ZIP tiers matter: a ~1.1km cell needs MIN_GROUP same-type listings to qualify, and on a
    # single capped (~800-listing) citywide scan only ~18% of listings had a dense enough cell —
    # the other 82% silently fell through to the CITYWIDE median. That made the ranker score homes
    # against the whole city, so it favored cheap ZIPs wholesale (every South Sac house looks
    # "below baseline") instead of finding homes underpriced against their OWN neighborhood.
    # A ZIP is the natural real-estate unit and is almost always dense enough (95823: 47 listings).
    MIN_GROUP = 5
    cell_type: Dict = {}
    by_type: Dict = {}
    zip_type: Dict = {}
    zip_all: Dict = {}
    all_psf: List[float] = []
    for p in pool:
        v = _psf(p)
        if v is None or v < 50:
            continue
        all_psf.append(v)
        tb = _type_bucket(p)
        by_type.setdefault(tb, []).append(v)
        z = (getattr(p, "zipcode", "") or "").strip()
        if z:
            zip_type.setdefault((z, tb), []).append(v)
            zip_all.setdefault(z, []).append(v)
        c = _cell(p)
        if c is not None:
            cell_type.setdefault((c, tb), []).append(v)

    local_med = {k: median(vs) for k, vs in cell_type.items() if len(vs) >= MIN_GROUP}
    zip_type_med = {k: median(vs) for k, vs in zip_type.items() if len(vs) >= MIN_GROUP}
    zip_med = {k: median(vs) for k, vs in zip_all.items() if len(vs) >= MIN_GROUP}
    type_med = {k: median(vs) for k, vs in by_type.items() if len(vs) >= MIN_GROUP}
    median_psf = median(all_psf) if all_psf else 0.0  # citywide baseline (+ telemetry)

    def _baseline(p):
        tb = _type_bucket(p)
        c = _cell(p)
        if c is not None and (c, tb) in local_med:
            return local_med[(c, tb)]
        z = (getattr(p, "zipcode", "") or "").strip()
        if z and (z, tb) in zip_type_med:
            return zip_type_med[(z, tb)]
        if z and z in zip_med:
            return zip_med[z]
        if tb in type_med:
            return type_med[tb]
        return median_psf

    # Drop severe low-$/sqft outliers BEFORE ranking: a listing priced at a tiny fraction of
    # its neighborhood's $/sqft is almost always a manufactured/mobile home, a fractional or
    # non-arms-length sale, or a data error — not a real flip (e.g. a $50k "SINGLE_FAMILY" at
    # ~20% of local $/sqft). Adaptive (relative to local comps) so it works in any market.
    _outlier_frac = float(_os.environ.get("DISCOVERY_OUTLIER_FRACTION", "0.4"))

    def _severe_low_outlier(p):
        v = _psf(p)
        b = _baseline(p)
        return v is not None and b > 0 and v < b * _outlier_frac

    _n_before = len(pool)
    pool = [p for p in pool if not _severe_low_outlier(p)]
    if _n_before != len(pool):
        logger.info("dropped %d severe low-$/sqft outliers (likely manufactured/bad data)",
                    _n_before - len(pool))
    if not pool:
        yield {"event": "error", "data": {"message": "No valid listings remained after removing data outliers."}}
        _finish_run("ok")
        return

    def _discovery_rank(p):
        # Higher = better deal: priced furthest below its own local/type comps.
        psf = _psf(p)
        if psf is None or psf < 50:
            return -1.0
        base = _baseline(p)
        if base <= 0 or psf > base * 5:  # sanity: ignore extreme outliers vs its baseline
            return -1.0
        return (base - psf) / base  # fraction below the home's local comp baseline

    props_with_link = sorted(pool, key=_discovery_rank, reverse=True)

    # Enrichment depth: analyze the WHOLE discovered market by default, not a slice.
    #
    # This used to default to max(count*3, 30) — i.e. ~30 of a ~780-listing city scan (4%) —
    # with the rest never enriched, never scored, and therefore unable to appear at any rank.
    # That funnel existed to save Bright Data spend, but at ~$0.0015/listing a full ~800-listing
    # scan is ~$1.20: it was analyzing 4% of the market to save ~$1.13, and it silently dropped
    # real deals purely on DISCOVERY rank — a pre-enrichment proxy (list $/sqft vs an area
    # median) that cannot see condition, comp quality, or ARV, and so has genuine false
    # negatives. Verified case: 4060 Black Tail Dr, Sacramento ranked #35, missed the top-30 cut,
    # and enriching it by hand scored a MARGINAL_FLIP at $78,598 / 23.7% off 8 high-confidence
    # comps. Ranking now only decides DISPLAY order, not what exists.
    #
    # Cost/latency knobs: enrichment is one Bright Data snapshot (POLL_TIMEOUT_SEC=1200s) and
    # results are cached per-zpid, so repeat scans of a market are near-free. Lower
    # BRIGHT_DATA_MAX_ENRICH / COUNTY_MAX_ENRICH to trade coverage back for run time.
    _is_scope = scope.kind in ("county", "city") and bool(getattr(scope, "zips", None))
    _max_enrich = int(_os.environ.get(
        "COUNTY_MAX_ENRICH" if _is_scope else "BRIGHT_DATA_MAX_ENRICH", "800"))
    if enrich_limit is not None:
        enrich_n = enrich_limit
    else:
        enrich_n = len(props_with_link)  # everything discovered; _max_enrich clamps it below

    if _is_scope:
        # Guarantee every neighborhood is represented: reserve the top-K per zip (each zip's
        # slice is already in local-comp rank order), then fill the rest of the budget with the
        # global leaders — so a uniformly-pricier area (e.g. Arden-Arcade) still gets analyzed.
        from collections import defaultdict as _dd
        k_per_zip = int(_os.environ.get("ENRICH_PER_ZIP_K", "2"))
        by_zip: dict = _dd(list)
        for p in props_with_link:
            by_zip[(getattr(p, "zipcode", "") or "_")].append(p)
        reserved = [p for plist in by_zip.values() for p in plist[:k_per_zip]]
        reserved_ids = {id(p) for p in reserved}
        rest = [p for p in props_with_link if id(p) not in reserved_ids]
        enrich_n = min(max(enrich_n, len(reserved)), _max_enrich, len(props_with_link))
        candidates = (reserved + rest)[:enrich_n]
    else:
        enrich_n = min(enrich_n, _max_enrich, len(props_with_link))
        candidates = props_with_link[:enrich_n]
    urls = [p.link for p in candidates]

    # Telemetry (not persisted) so we can tune ranking quality.
    if pool:
        ranked_psf = [round(c.price / c.sqft) for c in candidates[:5] if c.sqft]
        logger.info(
            "Discovery scan %s: pool=%d filtered=%s citywide_psf=%.0f local_groups=%d enrich_n=%d top5_psf=%s",
            city, len(pool), _filters_active, median_psf, len(local_med), enrich_n, ranked_psf,
        )

    # Discovery emits the candidate pool so the frontend can render skeletons,
    # but the final displayed set will be the top `count` by score (re-sorted client-side).
    yield {"event": "discovery", "data": {
        "city": city,
        "count": len(candidates),
        "display_count": count,
        "properties": [_base_card(p) for p in candidates],
    }}
    if run_id:
        try:
            from dashboard import db as _db
            _db.add_run_event(run_id, "discovery", json.dumps({
                "city": city, "count": len(candidates), "display_count": count
            }))
        except Exception:
            logger.exception("add_run_event failed")

    enriched_map: Dict[str, dict] = {}
    enrich_error: Optional[str] = None
    enrich_stats = {"attempted": 0, "from_cache": 0, "fresh": 0}

    if urls:
        yield {"event": "status", "data": {"message": f"Enriching {len(urls)} via Bright Data…"}}
        if run_id:
            try:
                from dashboard import db as _db
                _db.add_run_event(run_id, "status", json.dumps({"message": f"Enriching {len(urls)} via Bright Data…"}))
            except Exception:
                logger.exception("add_run_event failed")
        try:
            enricher = BrightDataZillowEnricher()
            fut = loop.run_in_executor(_EXECUTOR, enricher.enrich, urls)
            t0 = time.time()
            last_tick = t0
            client_disconnected = False
            # Poll the future; if the client disconnects (CancelledError), keep processing in background
            while not fut.done():
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    client_disconnected = True
                    logger.info("Client disconnected during enrichment; continuing in background.")
                    # do not break; continue polling until future completes
                    continue
                now = time.time()
                # Fire a tick every ~30s using elapsed-since-last-tick (robust to drift;
                # the old `elapsed % 30 == 0` check could skip ticks).
                if now - last_tick >= 30 and not client_disconnected:
                    last_tick = now
                    elapsed = int(now - t0)
                    yield {"event": "enrich_tick", "data": {"elapsed": elapsed, "requested": len(urls)}}
                    if run_id:
                        try:
                            from dashboard import db as _db
                            _db.add_run_event(run_id, "enrich_tick", json.dumps({"elapsed": elapsed, "requested": len(urls)}))
                        except Exception:
                            logger.exception("add_run_event failed")
            # Retrieve result from the executor
            enriched_map = fut.result() if fut.done() else await fut
            enrich_stats = getattr(enricher, "last_stats", enrich_stats) or enrich_stats
        except Exception as e:
            q = _detect_bd_quota_error(e)
            if q:
                yield {"event": "quota", "data": {
                    "api": "bright_data",
                    "message": q,
                }}
            else:
                enrich_error = str(e)
                yield {"event": "error", "data": {
                    "message": (
                        f"Enrichment failed: {e}. "
                        "Results below are unreliable fallbacks — not saving to history."
                    )
                }}

        # Hard fail if NOTHING came back
        if not enriched_map and not enrich_error:
            enrich_error = "Bright Data returned 0 records (snapshot timed out or empty)."
            yield {"event": "error", "data": {
                "message": (
                    "⚠️ Bright Data returned 0 enriched records. "
                    "All listings below use fallback math — ARV, year built, and rent estimates are unreliable. "
                    "Not saving these results to history. Try again in a few minutes."
                )
            }}

    # Score the ENTIRE enriched pool so we can pick the top `count` by score.
    # Cross-subject comp pooling: every enrichment's nearbyHomes contributes its SOLD
    # entries to one market-wide pool, and each subject may borrow pool comps within
    # ~1 mile. Measured on a real 636-record Sacramento cache: 713 unique usable sold
    # comps; 52% of subjects gain 3+ actual sales (vs seeing only their own ~8 widget
    # entries, 67.7% of which are Zestimates). Zero extra API cost — same data, shared.
    sold_pool = _build_sold_pool(enriched_map)
    if sold_pool:
        logger.info("comp pool: %d unique sold comps across %d enrichments",
                    len(sold_pool), len(enriched_map))
    evaluator = FlipperEvaluator(**_clean_assumptions(assumptions))
    scored = []
    for prop in candidates:
        zpid = _zpid_of(prop)
        enriched = enriched_map.get(zpid)
        try:
            _lat = (enriched.get("latitude") if enriched else None) or getattr(prop, "latitude", None)
            _lng = (enriched.get("longitude") if enriched else None) or getattr(prop, "longitude", None)
            extra = _pool_comps_near(sold_pool, _lat, _lng)
            a = evaluator.evaluate(prop, enriched=enriched, extra_comp_candidates=extra)
            scored.append({"prop": prop, "report": a, "enriched": enriched})
        except Exception as e:
            logger.error(f"score failed for {prop.address}: {e}")

    def _sort_key(item):
        # Tiered so a genuine FLIP always leads, then rental plays, then near-misses — a
        # money-losing flip never tops the list just because it happens to cash-flow.
        # within-tier score (0-100) breaks ties; tier*1000 keeps the bands separate.
        r = item["report"]
        if intent == "rent":
            return r.rental_score
        if intent == "flip":
            return r.flip_score
        # "both": rank by deal tier, then the score relevant to that tier.
        v, rv = r.verdict, r.rental_verdict
        if v == "STRONG_FLIP":
            tier, within = 5, r.flip_score
        elif v == "MARGINAL_FLIP":
            tier, within = 4, r.flip_score
        elif v == "RENTAL_PLAY":
            tier, within = 3, r.rental_score
        elif rv in ("GOOD_RENTAL", "DECENT_RENTAL"):
            tier, within = 2, r.rental_score
        elif v == "TEAR_DOWN":
            tier, within = 0, r.flip_score
        else:  # NO_DEAL near-miss — closest-to-viable (highest profit margin) ranks first
            tier, within = 1, max(-400, min(400, r.profit_margin_pct or 0))
        return tier * 1000 + (within or 0)
    scored.sort(key=_sort_key, reverse=True)

    # ── Honest market summary over the WHOLE scanned pool ─────────────────────
    # So we can tell the user "scanned N, found X real opportunities" rather than
    # always implying the top results are good deals.
    strong   = sum(1 for s in scored if s["report"].verdict == "STRONG_FLIP")
    marginal = sum(1 for s in scored if s["report"].verdict == "MARGINAL_FLIP")
    rental   = sum(1 for s in scored if s["report"].verdict == "RENTAL_PLAY")
    if intent == "rent":
        opportunities = sum(1 for s in scored
                            if s["report"].rental_verdict in ("GOOD_RENTAL", "DECENT_RENTAL"))
    else:
        opportunities = strong + marginal + (rental if intent != "flip" else 0)
    if strong:
        market_quality = "strong"
    elif opportunities:
        market_quality = "some"
    else:
        market_quality = "none"
    _zip_total = len(getattr(scope, "zips", []) or []) if scope and scope.kind in ("county", "city") else 0
    _zip_scanned = min(_zip_total, _COUNTY_MAX_ZIPS) if _zip_total else 0  # big counties truncate
    market_summary = {
        "scanned": len(scored),           # analyzed/enriched in depth
        "discovered": _discovered_total,  # full for-sale market scanned
        "zips": _zip_scanned,             # ZIPs actually fanned out over
        "zips_total": _zip_total,         # ZIPs in the scope (may exceed the scan cap)
        "scope_kind": getattr(scope, "kind", "raw") if scope else "raw",
        "strong": strong,
        "marginal": marginal,
        "rental": rental,
        "opportunities": opportunities,
        "quality": market_quality,
    }

    # Trim to top `count` after global ranking
    top_scored = scored[:count]

    serialized = []
    for s in top_scored:
        d = _flip_report_to_dict(s["report"], s["prop"], s["enriched"])
        serialized.append(d)
        yield {"event": "property", "data": d}
        if run_id:
            try:
                from dashboard import db as _db
                _db.add_run_event(run_id, "property", json.dumps({"zpid": d.get("zpid"), "verdict": d.get("verdict")}))
            except Exception:
                logger.exception("add_run_event failed")

    # Tell the frontend which candidates didn't make the cut (so it can remove their skeletons)
    final_zpids = [s["report"].property_id.replace("ZILLOW-", "") for s in top_scored]
    yield {"event": "trim", "data": {"keep": final_zpids}}
    if run_id:
        try:
            from dashboard import db as _db
            _db.add_run_event(run_id, "trim", json.dumps({"keep": final_zpids}))
        except Exception:
            logger.exception("add_run_event failed")

    # Final summary
    payload = {
        "city": city,
        "intent": intent,
        "queried_at": datetime.now(timezone.utc).isoformat(),
        "results": serialized,
        "enrichment_requested": len(urls),
        "enrichment_returned": len(enriched_map),
        "enrichment_error": enrich_error,
    }

    # Persist only if the data is reliable. If enrichment completely failed we keep
    # the in-memory render but don't pollute the archive with unreliable cached data.
    reliable = bool(enriched_map) or len(urls) == 0
    if reliable:
        from dashboard.storage import save_history
        slug = save_history(city, payload)
    else:
        slug = None
        yield {"event": "status", "data": {"message": "Result not saved to archive (unreliable)."}}
        if run_id:
            try:
                from dashboard import db as _db
                _db.add_run_event(run_id, "status", json.dumps({"message": "Result not saved to archive (unreliable)."}))
            except Exception:
                logger.exception("add_run_event failed")

    # Calculate cost (RapidAPI cost is per-discovery call; Bright Data is per fresh enrichment)
    cost_bd = enrich_stats["fresh"] * bd_cost_per_record
    cost_rapid = rapidapi_cost_per_call  # one discovery call per run
    total_cost = cost_bd + cost_rapid

    # Finalize the run record
    if run_id is not None:
        try:
            from dashboard import db as _db
            _db.log_run_finish(
                run_id,
                enrich_attempted=enrich_stats["attempted"],
                enrich_from_cache=enrich_stats["from_cache"],
                enrich_fresh=enrich_stats["fresh"],
                cost_bright_data_usd=cost_bd,
                cost_rapidapi_usd=cost_rapid,
                status="error" if enrich_error else "ok",
                error=enrich_error,
            )
        except Exception as e:
            logger.warning("log_run_finish failed: %s", e)

    yield {"event": "complete", "data": {
        "city": city,
        "slug": slug,
        "total": len(serialized),
        "queried_at": payload["queried_at"],
        "market": market_summary,
        "summary": {
            "enriched": len(enriched_map),
            "requested": len(urls),
            "from_cache": enrich_stats["from_cache"],
            "fresh": enrich_stats["fresh"],
            "cost_usd": round(total_cost, 4),
        },
    }}
    if run_id:
        try:
            from dashboard import db as _db
            _db.add_run_event(run_id, "complete", json.dumps({
                "slug": slug, "total": len(serialized), "summary": {
                    "enriched": len(enriched_map), "requested": len(urls),
                    "from_cache": enrich_stats["from_cache"], "fresh": enrich_stats["fresh"],
                    "cost_usd": round(total_cost, 4),
                }
            }))
        except Exception:
            logger.exception("add_run_event failed")
