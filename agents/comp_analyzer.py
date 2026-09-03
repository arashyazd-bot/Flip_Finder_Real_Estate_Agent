"""
CompAnalyzer — extracts comparable-sale data from a Bright Data enriched record.

Input: a Bright Data Zillow detail record (dict).
Output: a CompSet with median $/sqft, rent estimate, confidence rating.

Notes:
  - Bright Data returns `nearbyHomes` as a list of JSON STRINGS (one per home),
    not dicts. We parse them defensively.
  - Some nearby entries have price=0 (off-market with no sold price); we drop those.
  - We also fold in the subject's own zestimate and rentZestimate when present.
"""
import json
import math
import re
import statistics
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Comp:
    address: str
    price: int
    sqft: int
    psf: float
    beds: Optional[int] = None
    baths: Optional[float] = None
    rent_zestimate: Optional[int] = None
    adj_psf: Optional[float] = None  # size-adjusted $/sqft (to the subject's size)
    status: str = "unknown"  # what the price MEANS: 'sold' | 'zestimate' | 'pending' | 'for_sale' | 'for_rent' | 'unknown'


# Zillow's hdpTypeDimension tells us what a nearbyHomes price actually represents —
# an algorithmic Zestimate vs a real recent sale vs an asking price. Measured on 5,082
# cached comps: 67.7% Zestimate, 17% RecentlySold, 0 carry sale dates. Provenance is
# recorded per-comp so every report can disclose what its ARV actually rests on.
_HDP_STATUS = {
    "recentlysold": "sold",
    "zestimate": "zestimate",
    "pending": "pending",
    "forsale": "for_sale",
    "forrent": "for_rent",
}


def _comp_status(h: dict) -> str:
    hdp = str(h.get("hdpTypeDimension") or "").replace(" ", "").lower()
    if hdp in _HDP_STATUS:
        return _HDP_STATUS[hdp]
    hs = str(h.get("homeStatus") or "").upper()
    if hs in ("SOLD", "RECENTLY_SOLD"):
        return "sold"
    if hs == "PENDING":
        return "pending"
    if hs == "FOR_SALE":
        return "for_sale"
    if hs == "FOR_RENT":
        return "for_rent"
    return "unknown"


@dataclass
class CompSet:
    comps: List[Comp] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)  # status → count over the comps actually USED
    median_psf: Optional[float] = None       # size-WEIGHTED MEAN of adjusted $/sqft (falls back
                                             # to the plain median when no subject_sqft) — drives ARV
    raw_median_psf: Optional[float] = None    # unadjusted median (telemetry)
    psf_low: Optional[float] = None           # raw comp range (for display)
    psf_high: Optional[float] = None
    median_rent: Optional[int] = None
    median_rent_psf: Optional[float] = None
    confidence: str = "none"  # 'high' | 'medium' | 'low' | 'none'
    subject_self_value: Optional[int] = None  # subject's own value if it appears in its
                                              # own nearbyHomes — used as an as-is anchor


# psf ∝ (subject_sqft / comp_sqft) ** (-SIZE_PSF_EXPONENT): larger homes carry a LOWER
# $/sqft, so a small comp's psf is scaled DOWN when applied to a bigger subject (and
# vice-versa). Exponent 0.30 ≈ a price-size elasticity of 0.70, a moderate market norm.
SIZE_PSF_EXPONENT = 0.30

# Minimum ACTUAL-SALE comps required before the ARV set narrows to sold comps only
# (3 = the URAR/Fannie minimum closed-comp count).
SOLD_PRIORITY_MIN = 3


def _norm_addr(s: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _addr_of(h: dict) -> str:
    """Street address of a nearbyHomes-shaped dict — flat `streetAddress` or nested
    `address.streetAddress` (the county feed and some Bright Data records use the latter)."""
    a = h.get("streetAddress")
    if not a and isinstance(h.get("address"), dict):
        a = h["address"].get("streetAddress")
    return a or ""


def _size_adjust_psf(comp_psf: float, comp_sqft: int, subject_sqft: int) -> float:
    """Adjust a comp's $/sqft to the subject's size, bounded to ±20% so a single odd
    comp can't wildly swing ARV. Without subject_sqft, returns the raw psf unchanged."""
    if not (comp_sqft and subject_sqft) or comp_sqft <= 0 or subject_sqft <= 0:
        return comp_psf
    factor = (subject_sqft / comp_sqft) ** (-SIZE_PSF_EXPONENT)
    factor = max(0.80, min(1.20, factor))
    return comp_psf * factor


def _parse_nearby(entries) -> List[dict]:
    """Bright Data may return nearbyHomes as list of JSON strings OR list of dicts."""
    out = []
    for e in entries or []:
        if isinstance(e, str):
            try:
                out.append(json.loads(e))
            except Exception:
                continue
        elif isinstance(e, dict):
            out.append(e)
    return out


def analyze_comps(enriched: dict, subject_home_type: Optional[str] = None,
                  subject_sqft: Optional[int] = None,
                  subject_zpid: Optional[str] = None,
                  subject_address: Optional[str] = None,
                  extra_candidates: Optional[List[dict]] = None) -> CompSet:
    """Build a CompSet from an enriched Zillow record.

    subject_sqft, when provided, size-adjusts each comp's $/sqft to the subject (so a
    2000-sqft home isn't valued at a 1200-sqft comp's inflated $/sqft). subject_zpid /
    subject_address exclude the subject from its own nearbyHomes (its value is captured
    as an as-is anchor instead).

    extra_candidates: additional nearbyHomes-shaped dicts pooled from OTHER subjects'
    enrichments in the same market scan (geo-selected by the caller). Deduped by zpid
    against the subject's own list, then subject to every filter below identically —
    pooling widens the candidate set, never loosens the bar."""
    cs = CompSet()
    nearby = _parse_nearby(enriched.get("nearbyHomes"))
    if extra_candidates:
        # Dedupe by zpid AND by normalized street address. A pooled candidate describing the
        # same house as one of the subject's own nearbyHomes REPLACES it rather than joining
        # it. Why: the county assessor feed (dashboard/county_comps) keys records by APN, so
        # its zpid can never collide with Zillow's — and it carries the sqft of record where
        # Zillow was measured 24% off (1816 Commercial Way: 1,142 vs 1,411). Counting both
        # double-weighted the sale AND mixed a known-bad sqft into the ARV. Replacing is never
        # a downgrade: every pooled source only contributes actual sales (_build_sold_pool
        # filters to recentlysold; the county feed is recorded deeds), so a pooled record can
        # only match or improve the status of the Zillow entry it displaces.
        _seen_zpids = {str(h.get("zpid") or "").strip() for h in nearby}
        _by_addr = {}
        for i, h in enumerate(nearby):
            k = _norm_addr(_addr_of(h))
            if k:
                _by_addr.setdefault(k, i)
        for h in extra_candidates:
            z = str(h.get("zpid") or "").strip()
            if z and z in _seen_zpids:
                continue
            k = _norm_addr(_addr_of(h))
            if k and k in _by_addr:
                nearby[_by_addr[k]] = h   # same house from a better-keyed source: replace
                _seen_zpids.add(z)
                continue
            _seen_zpids.add(z)
            if k:
                _by_addr[k] = len(nearby)
            nearby.append(h)

    # Property types that should never be used as comps for a residential flip
    # (they distort $/sqft badly — verified: MULTI_FAMILY listings were polluting
    # SINGLE_FAMILY subjects in real Bright Data records).
    NON_COMP_TYPES = {"MULTI_FAMILY", "LOT", "LAND", "MANUFACTURED", "MOBILE", "APARTMENT"}

    subj_zpid = str(subject_zpid or enriched.get("zpid") or "").strip()
    _enr_addr = enriched.get("streetAddress")
    if not _enr_addr and isinstance(enriched.get("address"), dict):
        _enr_addr = enriched["address"].get("streetAddress")
    subj_addr_norm = _norm_addr(subject_address or _enr_addr)

    for h in nearby:
        price = h.get("price") or h.get("unformattedPrice") or 0
        sqft = h.get("livingArea") or h.get("area") or 0
        if not (price and sqft and price > 50_000 and sqft > 200):
            continue
        # Exclude the subject from its own comp set; keep its value as an as-is anchor.
        h_zpid = str(h.get("zpid") or "").strip()
        h_addr = h.get("streetAddress") or (
            h["address"].get("streetAddress") if isinstance(h.get("address"), dict) else "")
        is_subject = ((subj_zpid and h_zpid and h_zpid == subj_zpid)
                      or (subj_addr_norm and _norm_addr(h_addr) == subj_addr_norm))
        if is_subject:
            if not cs.subject_self_value:
                cs.subject_self_value = int(price)
            continue
        ht = (h.get("homeType") or "").upper()
        subj = (subject_home_type or "").upper()
        # Drop fundamentally different property types outright.
        if ht in NON_COMP_TYPES and subj not in NON_COMP_TYPES:
            continue
        # CONDO comps only for CONDO subjects, and vice-versa.
        if subj == "CONDO" and ht and ht != "CONDO":
            continue
        if subj in ("SINGLE_FAMILY", "TOWNHOUSE") and ht == "CONDO":
            continue
        # Size mismatch: a comp far larger/smaller than the subject isn't comparable
        # (e.g. a 14,000-sqft estate as a comp for a 720-sqft home). Keep a 0.5x–2.0x band.
        if subject_sqft and not (0.5 <= sqft / subject_sqft <= 2.0):
            continue
        addr = h.get("streetAddress") or h.get("address", {}).get("streetAddress") if isinstance(h.get("address"), dict) else h.get("streetAddress", "?")
        cs.comps.append(Comp(
            address=addr or "?",
            price=int(price),
            sqft=int(sqft),
            psf=round(price / sqft, 2),
            beds=h.get("bedrooms") or h.get("beds"),
            baths=h.get("bathrooms") or h.get("baths"),
            rent_zestimate=h.get("rentZestimate"),
            status=_comp_status(h),
        ))

    # Trim $/sqft outliers vs the comp-set median — rejects polluted comps (a $66/sqft
    # parcel/teardown, or a $700/sqft anomaly) that make the range meaningless. Fires at
    # 3+ comps and keeps at least 2 clean ones (2 good comps beat 3 with a parcel).
    if len(cs.comps) >= 3:
        _med = statistics.median([c.psf for c in cs.comps])
        _kept = [c for c in cs.comps if _med * 0.5 <= c.psf <= _med * 2.0]
        if len(_kept) >= 2:
            cs.comps = _kept

    # Rent signals come from the FULL filtered set (sold comps rarely carry rentZestimate),
    # captured before any sold-priority narrowing of the ARV set below.
    _full_set = list(cs.comps)
    rents = [c.rent_zestimate for c in _full_set if c.rent_zestimate]

    # Sold-priority ARV: when enough ACTUAL SALES survive the filters (>= 3, the appraisal
    # minimum), the ARV rests on those alone — a real transaction outranks a Zestimate or an
    # asking price. Below that threshold we keep the mixed set (coverage beats purity when
    # sales are scarce).
    _solds = [c for c in cs.comps if c.status == "sold"]
    if len(_solds) >= SOLD_PRIORITY_MIN:
        cs.comps = _solds

    # Provenance over the comps the ARV actually rests on.
    for c in cs.comps:
        cs.provenance[c.status] = cs.provenance.get(c.status, 0) + 1

    raw_psfs = [c.psf for c in cs.comps]

    # Size-adjust each comp's $/sqft to the subject; the ARV median uses adjusted values,
    # while the displayed range stays raw (actual sale data).
    for c in cs.comps:
        c.adj_psf = round(_size_adjust_psf(c.psf, c.sqft, subject_sqft), 2) if subject_sqft else c.psf
    adj_psfs = [c.adj_psf for c in cs.comps]

    if raw_psfs:
        # ARV basis = SIZE-WEIGHTED mean of the size-adjusted $/sqft: comps closer to the
        # subject's footprint carry more weight, so a 0.6x or 1.6x comp can't drag the
        # midpoint as hard as a same-size neighbor. Falls back to the plain median.
        if subject_sqft:
            _num = _den = 0.0
            for c in cs.comps:
                w = 1.0 / (1.0 + abs(math.log((c.sqft or subject_sqft) / subject_sqft))) if c.sqft else 0.5
                _num += c.adj_psf * w
                _den += w
            cs.median_psf = round(_num / _den, 2) if _den else round(statistics.median(adj_psfs), 2)
        else:
            cs.median_psf = round(statistics.median(adj_psfs), 2)
        cs.raw_median_psf = round(statistics.median(raw_psfs), 2)
        cs.psf_low = round(min(raw_psfs), 2)
        cs.psf_high = round(max(raw_psfs), 2)

    if rents:
        cs.median_rent = int(statistics.median(rents))
        # Per-sqft rent (avg) for cross-application
        rent_psfs = [c.rent_zestimate / c.sqft for c in _full_set if c.rent_zestimate and c.sqft]
        if rent_psfs:
            cs.median_rent_psf = round(statistics.median(rent_psfs), 3)
    else:
        # Fall back to subject's own rentZestimate
        rz = enriched.get("rentZestimate")
        if rz:
            cs.median_rent = int(rz)

    # Confidence: count + tightness (spread measured on raw comps)
    n = len(raw_psfs)
    if n >= 5:
        spread = (cs.psf_high - cs.psf_low) / cs.raw_median_psf if cs.raw_median_psf else 1.0
        cs.confidence = "high" if spread < 0.4 else "medium"
    elif n >= 3:
        cs.confidence = "medium"
    elif n >= 1:
        cs.confidence = "low"
    else:
        cs.confidence = "none"

    return cs


def estimate_arv(cs: CompSet, subject_sqft: int, post_rehab_uplift: float = 1.05) -> Optional[int]:
    """ARV = median comp $/sqft × subject sqft × small uplift for post-rehab quality."""
    if not (cs.median_psf and subject_sqft):
        return None
    return int(cs.median_psf * subject_sqft * post_rehab_uplift)


def estimate_rent(cs: CompSet, subject_sqft: int) -> Optional[int]:
    """Monthly rent estimate: prefer median rent if comps had rent data; else median rent_psf × sqft."""
    if cs.median_rent_psf and subject_sqft:
        return int(cs.median_rent_psf * subject_sqft)
    if cs.median_rent:
        return cs.median_rent
    return None
