"""
Public single-property analyzer: one Zillow URL in, the same FlipReport the city scan
produces out — no account required.

This is the landing page's lead magnet, so it is the only unauthenticated path that can
spend money (one Bright Data record per fresh run, ~$0.0015). Everything here exists to
make that safe:

  * parse_zillow_url  — the URL is rebuilt from the zpid alone. Only digits we validated
                        ever reach Bright Data, so this can't become a paid scraping relay.
  * check_and_charge  — per-visitor/day, global/day, burst cooldown, one in flight per
                        visitor, kill switch. In-memory: Render runs ONE instance, so the
                        state is coherent; it resets on restart, which is acceptable.
  * stream_one        — the pipeline, mirroring dashboard/reenrich.py: enrich → county sold
                        comps → FlipperEvaluator(purchase_price=…) → _flip_report_to_dict.
                        Runs on its OWN 2-worker pool so anonymous traffic can never starve
                        the authenticated city search's executor.
  * token store       — the PDF route renders only a payload THIS pipeline produced, keyed
                        by an opaque token. A public endpoint that rendered a client-supplied
                        payload would stamp attacker text into a FlipFinder-branded PDF.
"""
import asyncio
import json
import logging
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncIterator, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from dashboard import db
from dashboard import county_comps
from dashboard.search_service import (
    _clean_assumptions, _detect_bd_quota_error, _flip_report_to_dict,
)
from agents.flipper_evaluator import (
    FlipperEvaluator, DEFAULT_HOLD_MONTHS, HARD_MONEY_APR, BUY_CLOSING_PCT,
    HARD_MONEY_POINTS_PCT, SELLING_COST_PCT, RENTAL_OPEX_PCT, REFI_APR,
)
from models.property import Property
from scrapers.bright_data_enricher import (
    BrightDataZillowEnricher, _cache_fresh, _cache_path,
)

logger = logging.getLogger("dashboard.public")

# --- knobs (all env-tunable from the Render dashboard, no redeploy) ---
ENABLED = os.environ.get("PUBLIC_ANALYZE_ENABLED", "1").lower() in ("1", "true", "yes")
PER_IP_DAY = int(os.environ.get("PUBLIC_ANALYZE_PER_IP_DAY", "3"))
GLOBAL_DAY = int(os.environ.get("PUBLIC_ANALYZE_GLOBAL_DAY", "100"))
COOLDOWN_SEC = int(os.environ.get("PUBLIC_ANALYZE_COOLDOWN_SEC", "20"))
# Bright Data's module default is 1200s — fine for a batch scan, unacceptable for a web
# request. This bounds the wait; the orphaned worker still finishes and warms the cache.
ENRICH_TIMEOUT_SEC = int(os.environ.get("PUBLIC_ENRICH_TIMEOUT_SEC", "120"))
TOKEN_TTL_SEC = 30 * 60
TOKEN_MAX = 500
BD_COST_PER_RECORD = float(os.environ.get("BRIGHT_DATA_COST_PER_RECORD_USD", "0.0015"))

PRICE_MIN, PRICE_MAX = 10_000, 50_000_000
ANON_EMAIL = "anon@public"   # runs.user_email has no FK, so cost telemetry works unchanged

# Separate from search_service._EXECUTOR on purpose (starvation isolation).
_EXEC = ThreadPoolExecutor(max_workers=2, thread_name_prefix="public-analyze")


# ---------------------------------------------------------------- input validation

_ZPID_RE = re.compile(r"/(\d{5,12})_zpid/?")


def parse_zillow_url(raw: str) -> Tuple[str, str]:
    """Validate a Zillow listing URL and return (canonical_url, zpid).

    Host is parsed, not substring-matched: `"zillow." in url` would accept
    `https://attacker.example/zillow.com/1234567_zpid/`, which also satisfies the zpid
    regex, and the whole URL would then be handed to Bright Data on our account. The
    canonical form is rebuilt from the zpid alone so nothing else from the input survives.
    """
    raw = (raw or "").strip()
    if not raw or len(raw) > 600:
        raise ValueError("Paste a Zillow listing link.")
    p = urlparse(raw)
    if p.scheme.lower() != "https":
        raise ValueError("The link must start with https://")
    host = (p.hostname or "").lower()
    if not (host == "zillow.com" or host.endswith(".zillow.com")):
        raise ValueError("Only zillow.com listing links are accepted.")
    m = _ZPID_RE.search(p.path or "")
    if not m:
        raise ValueError("That doesn't look like a Zillow listing link (no _zpid in the address).")
    zpid = m.group(1)
    return f"https://www.zillow.com/homedetails/{zpid}_zpid/", zpid


def clean_price(v) -> Optional[int]:
    """User-supplied purchase price → bounded int, or None when absent/junk."""
    if v is None or v == "":
        return None
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return n if PRICE_MIN <= n <= PRICE_MAX else None


def is_cached(zpid: str) -> bool:
    """A fresh cache hit costs nothing, so it must not burn a visitor's daily slot."""
    try:
        path = _cache_path(zpid)
        return path.exists() and _cache_fresh(json.loads(path.read_text()))
    except Exception:
        return False


# ---------------------------------------------------------------- rate limiting

_LOCK = threading.Lock()
_HITS: Dict[str, List[float]] = {}     # ip -> charged timestamps, rolling 24h
_GLOBAL: List[float] = []              # every charged timestamp, rolling 24h
_LAST: Dict[str, float] = {}           # ip -> last accepted request (burst cooldown)
_INFLIGHT: set = set()

# Render's proxy sets X-Forwarded-For; when a client sends its own, the proxy APPENDS the
# real address, so the LAST hop is the client and the first is spoofable. The index is a
# knob only because this was not verifiable before deploy — the route logs the raw header
# so it can be confirmed from production logs, then this can be pinned.
# ponytail: env knob, drop it once a prod log line confirms Render's header shape.
_XFF_INDEX = int(os.environ.get("PUBLIC_ANALYZE_XFF_INDEX", "-1"))


def client_ip(request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    if hops:
        try:
            return hops[_XFF_INDEX]
        except IndexError:
            return hops[-1]
    return request.client.host if request.client else "unknown"


def _prune(now: float) -> None:
    cutoff = now - 86400
    for ip in list(_HITS):
        _HITS[ip] = [t for t in _HITS[ip] if t > cutoff]
        if not _HITS[ip]:
            del _HITS[ip]
    _GLOBAL[:] = [t for t in _GLOBAL if t > cutoff]
    for ip in list(_LAST):
        if now - _LAST[ip] > 3600:
            del _LAST[ip]


def check_and_charge(ip: str, cached: bool) -> Optional[dict]:
    """Admit-or-reject in ONE locked step, charging on admit.

    Charging at admission (not when the result lands) is deliberate: on the timeout path
    Bright Data has already billed the trigger, so charging at completion would let exactly
    the traffic most likely to be abusive slip past the budget. Charging here also closes
    the race where two concurrent requests both pass a separate check.

    Returns None when admitted, else {"status", "detail", "retry_after"}.
    """
    if not ENABLED:
        return {"status": 503, "retry_after": 3600,
                "detail": "The free analyzer is paused right now. Request access for the full product."}
    now = time.time()
    with _LOCK:
        _prune(now)
        if ip in _INFLIGHT:
            return {"status": 429, "retry_after": 30,
                    "detail": "One analysis at a time — yours is still running."}
        since = now - _LAST.get(ip, 0)
        if since < COOLDOWN_SEC:
            return {"status": 429, "retry_after": int(COOLDOWN_SEC - since) + 1,
                    "detail": "Slow down a little — try again in a few seconds."}
        if not cached:
            if len(_HITS.get(ip, [])) >= PER_IP_DAY:
                oldest = min(_HITS[ip])
                return {"status": 429, "retry_after": int(oldest + 86400 - now) + 1,
                        "detail": (f"You've used your {PER_IP_DAY} free analyses for today. "
                                   "Request access for unlimited runs.")}
            if len(_GLOBAL) >= GLOBAL_DAY:
                return {"status": 429, "retry_after": 3600,
                        "detail": "The free analyzer hit today's budget. Try again tomorrow, "
                                  "or request access."}
            _HITS.setdefault(ip, []).append(now)
            _GLOBAL.append(now)
        _LAST[ip] = now
        _INFLIGHT.add(ip)
    return None


def acquire_inflight(ip: str) -> None:
    """For authenticated callers who bypass the limiter but still get one-at-a-time."""
    with _LOCK:
        _INFLIGHT.add(ip)


def release(ip: str) -> None:
    with _LOCK:
        _INFLIGHT.discard(ip)


# ---------------------------------------------------------------- PDF token store

_TOKENS: Dict[str, Tuple[float, dict]] = {}


def stash_payload(payload: dict) -> str:
    tok = secrets.token_urlsafe(24)
    now = time.time()
    with _LOCK:
        for k, (t, _) in list(_TOKENS.items()):
            if now - t > TOKEN_TTL_SEC:
                del _TOKENS[k]
        if len(_TOKENS) >= TOKEN_MAX:              # bound memory on a 512MB box
            for k in sorted(_TOKENS, key=lambda k: _TOKENS[k][0])[: len(_TOKENS) - TOKEN_MAX + 1]:
                del _TOKENS[k]
        _TOKENS[tok] = (now, payload)
    return tok


def get_payload(tok: str) -> Optional[dict]:
    with _LOCK:
        item = _TOKENS.get(tok or "")
    if not item or time.time() - item[0] > TOKEN_TTL_SEC:
        return None
    return item[1]


# ---------------------------------------------------------------- the pipeline

_ASSUMPTION_LABELS = [
    # (evaluator kwarg, label, default, is_percent, unit)
    ("hard_money_apr",   "Hard-money APR", HARD_MONEY_APR,          True,  "%"),
    ("hold_months",      "Hold",           DEFAULT_HOLD_MONTHS,     False, " mo"),
    ("buy_closing_pct",  "Buy closing",    BUY_CLOSING_PCT,         True,  "%"),
    ("points_pct",       "Loan points",    HARD_MONEY_POINTS_PCT,   True,  "%"),
    ("selling_cost_pct", "Selling",        SELLING_COST_PCT,        True,  "%"),
    ("rental_opex_pct",  "Rental OpEx",    RENTAL_OPEX_PCT,         True,  "%"),
    ("refi_apr",         "Refi APR",       REFI_APR,                True,  "%"),
]


def _assumption_lines(kw: dict, purchase_price: Optional[int], list_price: int) -> List[str]:
    """Human-readable effective assumptions for the PDF header — server-side equivalent of
    app.js effectiveAssumptions(), which is DOM-coupled and unavailable here."""
    lines = []
    for key, label, default, is_pct, unit in _ASSUMPTION_LABELS:
        val = kw.get(key, default)
        shown = round(val * 100, 2) if is_pct else int(val)
        shown = int(shown) if float(shown).is_integer() else shown
        adj = " (adj)" if key in kw and abs(kw[key] - default) > 1e-9 else ""
        lines.append(f"{label}: {shown}{unit}{adj}")
    if purchase_price and purchase_price != list_price:
        lines.insert(0, f"Purchase price: ${purchase_price:,} (your number; list ${list_price:,})")
    return lines


def _prop_from_record(rec: dict, zpid: str, url: str) -> Property:
    """Mirror of reenrich._prop_from_history, from a Bright Data record instead."""
    p = Property(
        property_id=f"ZILLOW-{zpid}",                      # _zpid_of() strips this prefix
        address=rec.get("streetAddress") or "?",
        city=rec.get("city") or "", state=rec.get("state") or "",
        price=int(rec.get("price") or rec.get("unformattedPrice") or 0),
        bedrooms=int(rec.get("bedrooms") or 0), bathrooms=float(rec.get("bathrooms") or 0),
        sqft=int(rec.get("livingArea") or 0),
        year_built=int(rec.get("yearBuilt") or 1990),
        property_type=rec.get("homeType") or "?", estimated_rent=0,
    )
    # _flip_report_to_dict reads these as instance attrs
    p.link = url
    p.img_src = None
    p.latitude = rec.get("latitude")
    p.longitude = rec.get("longitude")
    p.zestimate = rec.get("zestimate") or 0
    p.days_on_zillow = rec.get("daysOnZillow") or 0
    return p


async def stream_one(url: str, zpid: str, purchase_price: Optional[int],
                     assumptions: Optional[dict], run_email: str) -> AsyncIterator[dict]:
    """Yield {"event", "data"} dicts (the server wraps them as SSE), same contract as
    search_service.stream_search. Every early return closes the run row."""
    loop = asyncio.get_running_loop()
    t0 = time.time()
    run_id: Optional[int] = None
    enricher: Optional[BrightDataZillowEnricher] = None

    def ev(event: str, **data) -> dict:
        return {"event": event, "data": data}

    def finish(status: str, error: Optional[str] = None) -> None:
        if run_id is None:
            return
        stats = getattr(enricher, "last_stats", None) or {}
        fresh = int(stats.get("fresh", 0))
        try:
            db.log_run_finish(run_id, int(stats.get("attempted", 1)), int(stats.get("from_cache", 0)),
                              fresh, fresh * BD_COST_PER_RECORD, 0.0, status=status, error=error)
        except Exception:
            logger.exception("run finish failed")

    try:
        run_id = db.log_run_start(run_email, f"public:{zpid}", "flip", 1)
    except Exception:
        logger.exception("run start failed")

    # 1) Enrich — one record, bounded wait, honest heartbeat while Bright Data works.
    yield ev("status", step="fetch", message="Pulling the listing from Zillow…", elapsed=0)
    try:
        enricher = BrightDataZillowEnricher(poll_timeout_sec=ENRICH_TIMEOUT_SEC)
    except RuntimeError as e:                      # no token configured
        logger.error("enricher unavailable: %s", e)
        yield ev("error", code="UPSTREAM", message="The analyzer isn't configured right now.")
        finish("error", "no bright data token"); return
    fut = loop.run_in_executor(_EXEC, enricher.enrich, [url])
    while True:
        done, _ = await asyncio.wait({fut}, timeout=4)
        if done:
            break
        el = int(time.time() - t0)
        yield ev("status", step="fetch", elapsed=el,
                 message=f"Still pulling the listing… ({el}s — a first look at a home takes up to a minute)")
    try:
        emap = fut.result()
    except Exception as e:
        quota = _detect_bd_quota_error(e)
        logger.exception("public enrich failed")
        msg = ("The data provider is out of credit — please try again later."
               if quota else "We couldn't reach the listing data provider. Try again in a minute.")
        yield ev("error", code="QUOTA" if quota else "UPSTREAM", message=msg)
        finish("error", str(e)[:300]); return

    rec = emap.get(zpid) or (next(iter(emap.values())) if emap else None)
    if not rec:
        last_err = getattr(enricher, "last_error", None) or ""
        if "did not complete" in last_err:
            msg = ("Zillow was slow and the listing didn't come back in time. Try again in a "
                   "minute — it's usually instant on the retry.")
            yield ev("error", code="TIMEOUT", message=msg); finish("timeout", last_err[:300]); return
        msg = "We couldn't pull that listing from Zillow. Check the link and try again."
        yield ev("error", code="NOT_FOUND", message=msg); finish("not_found", msg); return

    stats = enricher.last_stats or {}
    from_cache = int(stats.get("from_cache", 0)) > 0
    notes: List[str] = []

    # 2) County recorded sales (Sacramento only; free; best-effort).
    yield ev("status", step="comps", elapsed=int(time.time() - t0),
             message="Loading county recorded sales…")
    lat, lng = rec.get("latitude"), rec.get("longitude")
    extra: List[dict] = []
    county_status = "out_of_area"
    if lat and lng:
        try:
            extra, county_status = await loop.run_in_executor(_EXEC, county_comps.sold_comps, lat, lng)
        except Exception:
            logger.exception("county comps failed")
            extra, county_status = [], "unavailable"
        if county_status == "ok":
            # The assessor of record beats Zillow on the SUBJECT's own size — ARV scales
            # linearly on it, and Zillow was 24% off on a comp this session (1,142 vs 1,411).
            # Guarded to a sane band: a wildly different number is far more likely a wrong
            # parcel match on the address LIKE than a Zillow error.
            # ponytail: band guard instead of APN matching; tighten if a mismatch ever surfaces.
            try:
                subj = await loop.run_in_executor(_EXEC, county_comps.subject_record,
                                                  rec.get("streetAddress") or "")
            except Exception:
                subj = None
            z_sqft = int(rec.get("livingArea") or 0)
            c_sqft = int((subj or {}).get("livingArea") or 0)
            if z_sqft and c_sqft and 0.7 <= c_sqft / z_sqft <= 1.5 and abs(c_sqft - z_sqft) / z_sqft > 0.03:
                rec = dict(rec)
                rec["livingArea"] = c_sqft
                notes.append(f"Living area corrected to {c_sqft:,} sqft from the county assessor "
                             f"(Zillow listed {z_sqft:,}).")
            if subj and subj.get("bathrooms") and float(subj["bathrooms"]) != float(rec.get("bathrooms") or 0):
                rec = dict(rec)
                rec["bathrooms"] = float(subj["bathrooms"])
                notes.append(f"Bath count corrected to {subj['bathrooms']} from the county assessor.")

    # 3) Underwrite with the REAL purchase_price kwarg (never by overriding the list price —
    #    that silently caps the ARV; see FlipperEvaluator.evaluate).
    yield ev("status", step="underwrite", elapsed=int(time.time() - t0), message="Underwriting the deal…")
    kw = _clean_assumptions(assumptions)
    prop = _prop_from_record(rec, zpid, url)
    try:
        report = await loop.run_in_executor(
            _EXEC, lambda: FlipperEvaluator(**kw).evaluate(
                prop, enriched=rec, extra_comp_candidates=extra or None, purchase_price=purchase_price))
    except Exception as e:
        logger.exception("public evaluate failed")
        yield ev("error", code="UPSTREAM", message="Something went wrong while underwriting.")
        finish("error", str(e)[:300]); return

    out = _flip_report_to_dict(report, prop, rec)
    out["comp_sources"] = {"county": county_status, "county_count": len(extra)}
    out["notes"] = notes
    out["from_cache"] = from_cache

    payload = {
        "city": out.get("address") or f"zpid {zpid}",
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": None,
        "assumptions": _assumption_lines(kw, purchase_price, report.list_price),
        "properties": [out],
        "hud": [], "hud_state": "",
    }
    token = stash_payload(payload)

    yield ev("report", report=out)
    yield ev("complete", token=token, elapsed=int(time.time() - t0), from_cache=from_cache,
             assumptions=payload["assumptions"], notes=notes,
             comp_sources=out["comp_sources"])
    finish("ok")


# ---------------------------------------------------------------- self-check

if __name__ == "__main__":
    # No network. python -m dashboard.public_analyze
    ok = parse_zillow_url("https://www.zillow.com/homedetails/1815-2nd-Ave-Sacramento-CA-95818/25791450_zpid/")
    assert ok == ("https://www.zillow.com/homedetails/25791450_zpid/", "25791450"), ok
    assert parse_zillow_url("https://zillow.com/homedetails/25791450_zpid")[1] == "25791450"
    for bad in ("https://zillow.com.evil.tld/homedetails/25791450_zpid/",
                "https://evil.com/www.zillow.com/homedetails/25791450_zpid/",
                "http://www.zillow.com/homedetails/25791450_zpid/",
                "file:///etc/passwd", "https://www.zillow.com/homes/for_sale/", "", "x" * 700,
                "https://www.zillow.com/homedetails/1_zpid/"):
        try:
            parse_zillow_url(bad); raise AssertionError(f"accepted {bad!r}")
        except ValueError:
            pass
    assert clean_price("495000") == 495000 and clean_price(495000.4) == 495000
    assert clean_price("junk") is None and clean_price(5) is None and clean_price(None) is None
    assert clean_price(10**9) is None

    # limiter: 3 fresh admits, 4th rejected; cache hits never charge; cooldown; inflight.
    COOLDOWN_SEC = 0
    for i in range(PER_IP_DAY):
        assert check_and_charge("1.1.1.1", cached=False) is None, i
        release("1.1.1.1")
    rej = check_and_charge("1.1.1.1", cached=False)
    assert rej and rej["status"] == 429 and "free analyses" in rej["detail"], rej
    assert check_and_charge("1.1.1.1", cached=True) is None      # cache hit still allowed
    assert check_and_charge("1.1.1.1", cached=True)["detail"].startswith("One analysis")  # inflight
    release("1.1.1.1")
    assert check_and_charge("2.2.2.2", cached=False) is None; release("2.2.2.2")
    COOLDOWN_SEC = 20
    r2 = check_and_charge("2.2.2.2", cached=False)
    assert r2 and "Slow down" in r2["detail"], r2

    # token store round-trip + expiry
    t = stash_payload({"properties": [{"address": "x"}]})
    assert get_payload(t)["properties"][0]["address"] == "x"
    assert get_payload("nope") is None
    _TOKENS[t] = (time.time() - TOKEN_TTL_SEC - 1, _TOKENS[t][1])
    assert get_payload(t) is None

    lines = _assumption_lines({"hard_money_apr": 0.14}, 320_000, 628_700)
    assert lines[0].startswith("Purchase price: $320,000 (your number; list $628,700)"), lines
    assert "Hard-money APR: 14% (adj)" in lines and "Hold: 6 mo" in lines, lines

    class _R:  # client_ip with a forged first hop
        headers = {"x-forwarded-for": "9.9.9.9, 203.0.113.7"}
        client = None
    assert client_ip(_R()) == "203.0.113.7"
    print("PUBLIC_ANALYZE SELF-CHECK PASS")
