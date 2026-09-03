"""
Arithmetic self-check for the post-audit FlipperEvaluator fixes.
Plain asserts, no framework:  python3 tests/test_flipper_calcs.py
Each block maps to a fix from the calc audit (HIGH-1..4, MEDIUM-5..8, LOW-9).
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.property import Property
from agents.flipper_evaluator import (
    FlipperEvaluator, BUY_CLOSING_PCT, HARD_MONEY_POINTS_PCT,
    HARD_MONEY_APR, HARD_MONEY_LTV, RENTAL_OPEX_PCT, INSURANCE_ANNUAL,
    STRONG_MARGIN_PCT, RENTAL_SCORE_BANDS, _rental_verdict_and_score,
)


def _prop(pid, price, sqft, year=1985, ptype="Single Family"):
    return Property(
        property_id=pid, address=f"{pid} Test St", city="Sacramento", state="CA",
        price=price, bedrooms=3, bathrooms=2.0, sqft=sqft, year_built=year,
        property_type=ptype, estimated_rent=0, hoa_fees=0,
    )


def _comp(addr, psf, sqft, hdp="RecentlySold"):
    return json.dumps({
        "streetAddress": addr, "price": int(psf * sqft), "livingArea": sqft,
        "homeType": "SINGLE_FAMILY", "bedrooms": 3, "bathrooms": 2,
        "hdpTypeDimension": hdp,
    })


ev = FlipperEvaluator()

# ── Scenario A: flip, comps present, NO zestimate ──────────────────────────────
# Tests HIGH-1 (buy closing in all_in), HIGH-2 (points in financing),
# HIGH-4 (as-is ceiling skipped when only list-price anchor), MEDIUM-8 band.
a_price, a_sqft = 400_000, 2000
encA = {
    "price": a_price, "livingArea": a_sqft, "homeType": "SINGLE_FAMILY",
    "yearBuilt": 1985, "propertyTaxRate": 1.25, "description": "",
    "_cached_at": 1784159751.0,  # provenance: enrichment snapshot timestamp
    "nearbyHomes": [_comp("11 A", 250, 2000), _comp("12 A", 255, 2000),
                    _comp("13 A", 260, 2000), _comp("14 A", 265, 2000)],
}
A = ev.evaluate(_prop("A", a_price, a_sqft), enriched=encA)

buy_closing = int(a_price * BUY_CLOSING_PCT)
# HIGH-1: all_in = price + buy_closing + rehab + holding + financing (identity)
assert A.all_in_cost == (A.purchase_price + buy_closing + A.rehab_estimate
                         + A.holding_cost_6mo + A.financing_cost), A.all_in_cost
# HIGH-2: financing = interest carry + origination points. Derived from the constants, not
# magic numbers, so retuning a default (e.g. per-market) can't silently rot this test.
expect_fin = int(a_price * HARD_MONEY_LTV * HARD_MONEY_APR * 0.5
                 + a_price * HARD_MONEY_LTV * HARD_MONEY_POINTS_PCT)
assert A.financing_cost == expect_fin, (A.financing_cost, expect_fin)
# HIGH-4: no zestimate ⇒ as-is ceiling skipped ⇒ comp ARV (~551k) NOT crushed to 1.15x list (460k)
assert A.arv > a_price * 1.15, (A.arv, a_price * 1.15)
# MEDIUM-8: verdict/score band consistency
assert A.verdict == "MARGINAL_FLIP", (A.verdict, A.projected_profit, A.profit_margin_pct)
assert 55 <= A.flip_score <= 79, A.flip_score
print(f"A  MARGINAL_FLIP  arv={A.arv:,}  all_in={A.all_in_cost:,}  fin={A.financing_cost:,}  score={A.flip_score}")

# ── Scenario B: strong rental, thin flip spread ────────────────────────────────
# Tests HIGH-3 (cap rate on rental_basis), MEDIUM-5 (rental not buried by thin-spread
# gate), MEDIUM-6 (RENTAL_PLAY aligned to GOOD_RENTAL).
b_price, b_sqft = 300_000, 1500
encB = {
    "price": b_price, "livingArea": b_sqft, "homeType": "SINGLE_FAMILY",
    "yearBuilt": 1985, "propertyTaxRate": 1.25, "description": "", "rentZestimate": 4500,
    "nearbyHomes": [_comp("21 B", 185, 1500), _comp("22 B", 187, 1500),
                    _comp("23 B", 189, 1500), _comp("24 B", 186, 1500)],
}
B = ev.evaluate(_prop("B", b_price, b_sqft), enriched=encB)

assert B.arv < b_price * 1.05, B.arv                       # genuinely thin flip spread
assert B.rental_verdict == "GOOD_RENTAL", B.rental_verdict
assert B.verdict == "RENTAL_PLAY", (B.verdict, B.cap_rate_pct, B.monthly_cash_flow)  # MEDIUM-5/6
# HIGH-3: cap rate divides by rental_basis (price+closing+rehab), not all_in
rental_basis = b_price + int(b_price * BUY_CLOSING_PCT) + B.rehab_estimate
noi_annual = 4500 * 12 * (1 - RENTAL_OPEX_PCT) - b_price * 0.0125 - INSURANCE_ANNUAL
assert B.cap_rate_pct == round(noi_annual / rental_basis * 100, 2), B.cap_rate_pct
assert 40 <= B.rental_score <= 100
print(f"B  RENTAL_PLAY  cap={B.cap_rate_pct}%  cf=${B.monthly_cash_flow}/mo  score={B.flip_score}")

# ── Pure-formula invariants ────────────────────────────────────────────────────
# MEDIUM-7 (superseded): the rental score is clamped to its verdict's band, so it can never
# contradict the label. Exercises the REAL function — an earlier version of this test re-declared
# the formula in local lambdas, so it asserted behaviour the module no longer had and still passed.
assert _rental_verdict_and_score(8, -100)[0] == "POOR_RENTAL"      # high cap, negative cash flow
assert _rental_verdict_and_score(5, 0)[0] == "DECENT_RENTAL"
assert _rental_verdict_and_score(7, 200)[0] == "GOOD_RENTAL"
# The inversion that motivated the clamp: POOR used to score 71.5 vs DECENT's 60.
assert _rental_verdict_and_score(8, -100)[2] < _rental_verdict_and_score(5, 0)[2]
# Exhaustive: no POOR may ever out-score any DECENT/GOOD, and bands must not overlap.
_grid = [(c / 4, f) for c in range(0, 80) for f in range(-2000, 2001, 50)]
_by_verdict = {}
for _c, _f in _grid:
    _v, _, _s = _rental_verdict_and_score(_c, _f)
    _lo, _hi = RENTAL_SCORE_BANDS[_v]
    assert _lo <= _s <= _hi, (_c, _f, _v, _s)          # score always inside its band
    _by_verdict.setdefault(_v, []).append(_s)
assert max(_by_verdict["POOR_RENTAL"]) < min(_by_verdict["DECENT_RENTAL"]), "POOR out-scores DECENT"
assert max(_by_verdict["DECENT_RENTAL"]) < min(_by_verdict["GOOD_RENTAL"]), "DECENT out-scores GOOD"

# STRONG_FLIP is driven by our own P&L, and the bar subsumes the 70% rule (a price at the 70% MAO
# already implies ~23.7% margin), so the old `passes_70 AND margin>=15` gate is gone.
assert STRONG_MARGIN_PCT < 23.7, "strong bar must sit under the 70%-rule-implied margin"

# ── Comp provenance: every report discloses what its comp prices ARE and when fetched ──
assert A.comp_provenance == {"sold": 4}, A.comp_provenance
assert A.enriched_at == "2026-07-15T23:55:51Z", A.enriched_at   # UTC of 1784159751 (16:55:51 PDT)
assert all(l.endswith("[sold]") for l in A.comps_summary), A.comps_summary
assert not any("0 actual sales" in r for r in A.risk_flags)      # real sales → no flag

# Scenario C: 5 tight comps, ALL Zestimates (the dominant real-world case — 67.7% measured).
# Tight spread would earn "high" confidence, but zero actual sales must demote it to medium
# and disclose the fact as a risk flag.
encC = {
    "price": 400_000, "livingArea": 2000, "homeType": "SINGLE_FAMILY",
    "yearBuilt": 1985, "propertyTaxRate": 1.25, "description": "",
    "nearbyHomes": [_comp(f"{i} C", psf, 2000, hdp="Zestimate")
                    for i, psf in enumerate([250, 252, 254, 256, 258])],
}
C = ev.evaluate(_prop("C", 400_000, 2000), enriched=encC)
assert C.comp_provenance == {"zestimate": 5}, C.comp_provenance
assert C.arv_source == "comps" and C.arv_confidence == "medium", (C.arv_source, C.arv_confidence)
assert any("0 actual sales" in r for r in C.risk_flags), C.risk_flags
assert C.enriched_at is None                                     # no _cached_at supplied
print(f"C  zero-sold comps -> confidence demoted to {C.arv_confidence}, flag present")

# ── Cross-subject comp pooling: pooled SOLD comps within range upgrade the ARV set ──
def _pool_comp(zpid, addr, psf, sqft):
    return {"zpid": zpid, "streetAddress": addr, "price": int(psf * sqft), "livingArea": sqft,
            "homeType": "SINGLE_FAMILY", "hdpTypeDimension": "RecentlySold",
            "latitude": 38.47, "longitude": -121.45}

pool = [_pool_comp(901, "1 Pool St", 255, 2000),
        _pool_comp(902, "2 Pool St", 260, 2000),
        _pool_comp(903, "3 Pool St", 265, 2000)]
# Same subject as C (5 tight Zestimate-only own comps) + 3 pooled real sales:
# sold-priority (>= SOLD_PRIORITY_MIN) must narrow the ARV set to the 3 solds,
# clear the zero-sold flag, and keep confidence undemoted.
D = ev.evaluate(_prop("D", 400_000, 2000), enriched=encC, extra_comp_candidates=pool)
assert D.comp_provenance == {"sold": 3}, D.comp_provenance
assert not any("0 actual sales" in r for r in D.risk_flags), D.risk_flags
assert all(l.endswith("[sold]") for l in D.comps_summary), D.comps_summary
assert D.comp_count == 3, D.comp_count
print(f"D  pooled solds -> ARV rests on {D.comp_provenance}, no zero-sold flag")

# Dedupe: a pool candidate sharing a zpid with the subject's own comps counts once.
encE = dict(encC)
encE["nearbyHomes"] = [json.dumps({"zpid": 901, "streetAddress": "1 Pool St",
                                   "price": 510_000, "livingArea": 2000,
                                   "homeType": "SINGLE_FAMILY", "bedrooms": 3, "bathrooms": 2,
                                   "hdpTypeDimension": "Zestimate"})]
E = ev.evaluate(_prop("E", 400_000, 2000), enriched=encE, extra_comp_candidates=pool)
assert E.comp_count == 3, (E.comp_count, E.comps_summary)  # 901 own + 902/903 pooled, not 4
print(f"E  zpid dedupe -> {E.comp_count} comps (shared zpid not double-counted)")

# Address dedupe: a pooled record for the SAME house as one of the subject's own comps
# must replace it, not join it. The county feed keys by APN (never collides with a Zillow
# zpid) and carries the sqft of record; seen live on 1815 2nd Ave, where 1816 Commercial
# Way was counted twice — at Zillow's wrong 1,142 sqft AND the county's 1,411.
encG = dict(encC)
encG["nearbyHomes"] = [json.dumps({"zpid": 555, "streetAddress": "1816 Commercial Way",
                                   "price": 730_000, "livingArea": 1142,
                                   "homeType": "SINGLE_FAMILY", "hdpTypeDimension": "Zestimate"})]
county_dupe = {"zpid": "APN-01003330050000", "price": 730_000, "livingArea": 1411,
               "homeType": "SINGLE_FAMILY", "hdpTypeDimension": "RecentlySold",
               "address": {"streetAddress": "1816  COMMERCIAL WAY"},   # nested, padded, upper
               "latitude": 38.557, "longitude": -121.4905}
G = ev.evaluate(_prop("G", 400_000, 1207), enriched=encG,
                extra_comp_candidates=[county_dupe] + pool)
assert G.comp_count == 4, (G.comp_count, G.comps_summary)          # 1 replaced + 3 pooled, not 5
_g1816 = [l for l in G.comps_summary if "commercial way" in l.lower()]
assert len(_g1816) == 1, G.comps_summary                            # the house appears ONCE
assert "1411sqft" in _g1816[0] and "1142sqft" not in _g1816[0], _g1816   # county sqft won
assert _g1816[0].endswith("[sold]"), _g1816                         # and its sold status
print(f"G  address dedupe -> {G.comp_count} comps; 1816 Commercial Way once, at county sqft")

# LOW-9: profit floor scales with price (flat $20k on cheap, 5% on expensive)
assert max(20_000, int(0.05 * 300_000)) == 20_000
assert max(20_000, int(0.05 * 1_000_000)) == 50_000

# ── Buy-closing as its own field + user-tunable assumptions ─────────────────────
assert A.buy_closing_cost == int(a_price * BUY_CLOSING_PCT), A.buy_closing_cost

# Engine honors overridden assumptions: closing 2%→5%, points 2%→3%, APR 12%→15%.
ev2 = FlipperEvaluator(buy_closing_pct=0.05, points_pct=0.03, hard_money_apr=0.15)
A2 = ev2.evaluate(_prop("A2", a_price, a_sqft), enriched=encA)
assert A2.buy_closing_cost == int(a_price * 0.05) == 20000, A2.buy_closing_cost
assert A2.financing_cost == int(a_price * 0.75 * 0.15 * 0.5 + a_price * 0.75 * 0.03), A2.financing_cost
assert A2.financing_cost > A.financing_cost
assert A2.all_in_cost > A.all_in_cost              # higher costs → higher all-in
assert A2.projected_profit < A.projected_profit    # → lower profit
print(f"A2 overrides  closing={A2.buy_closing_cost:,}  fin={A2.financing_cost:,}  all_in={A2.all_in_cost:,}")

# ── Purchase price is a cost basis, NOT a substitute for list price ────────────
# The obvious way to model "what I actually paid" — overriding enriched["price"] — silently
# corrupts the report, because the ARV sanity cap scales off `price`: a below-list purchase
# shrinks ARV to a multiple of the buyer's own number and prints a risk flag calling it
# "list". Scenario F proves the real `purchase_price` kwarg leaves the value side untouched,
# and F_old proves the shortcut really does corrupt it (so this test is not tautological).
f_paid = 300_000                                   # well under A's $400k list
F = ev.evaluate(_prop("F", a_price, a_sqft), enriched=encA, purchase_price=f_paid)
assert F.arv == A.arv, (F.arv, A.arv)              # ARV anchored on LIST — unchanged
assert F.arv_confidence == A.arv_confidence
assert not any("capped at" in r for r in F.risk_flags), F.risk_flags   # no phantom cap flag
assert F.list_price == a_price and F.purchase_price == f_paid, (F.list_price, F.purchase_price)
assert F.list_psf == A.list_psf == round(a_price / a_sqft, 2), F.list_psf   # still LIST psf
assert F.purchase_psf == round(f_paid / a_sqft, 2), F.purchase_psf
# Every cost-side line moves to the basis; the identity holds on the basis.
assert F.buy_closing_cost == int(f_paid * BUY_CLOSING_PCT), F.buy_closing_cost
assert F.financing_cost == int(f_paid * HARD_MONEY_LTV * HARD_MONEY_APR * 0.5
                               + f_paid * HARD_MONEY_LTV * HARD_MONEY_POINTS_PCT), F.financing_cost
assert F.all_in_cost == (f_paid + F.buy_closing_cost + F.rehab_estimate
                         + F.holding_cost_6mo + F.financing_cost), F.all_in_cost
assert F.projected_profit > A.projected_profit     # cheaper basis, same ARV → more profit
assert F.mao_70_rule == A.mao_70_rule              # MAO derives from ARV + rehab only
assert F.passes_70_rule and not A.passes_70_rule   # $300k clears the MAO that $400k missed
# The shortcut this replaces: same deal with price itself overridden. ARV collapses and the
# phantom flag appears — exactly what the public "what I paid" route must never do.
F_old = ev.evaluate(_prop("Fo", f_paid, a_sqft), enriched={**encA, "price": f_paid})
assert F_old.arv < A.arv, (F_old.arv, A.arv)
assert any("capped at" in r and "list" in r for r in F_old.risk_flags), F_old.risk_flags
# With no purchase_price the basis IS list, so every existing report is byte-identical.
assert A.purchase_price == A.list_price == a_price
print(f"F  purchase_price={f_paid:,} -> arv unchanged {F.arv:,}, profit {A.projected_profit:,} -> {F.projected_profit:,}; "
      f"old shortcut would have capped ARV to {F_old.arv:,}")

# Trust-boundary guard: whitelist + range-clamp on user-supplied assumptions.
from dashboard.search_service import _clean_assumptions
cleaned = _clean_assumptions({"hard_money_apr": 9.9, "rental_opex_pct": -1,
                              "hold_months": 999, "bogus": 5, "selling_cost_pct": "x"})
assert cleaned["hard_money_apr"] == 0.40, cleaned     # clamped to max
assert cleaned["rental_opex_pct"] == 0.10, cleaned    # clamped to min
assert cleaned["hold_months"] == 24, cleaned          # clamped + int
assert "bogus" not in cleaned                         # unknown key dropped
assert "selling_cost_pct" not in cleaned              # non-numeric skipped
assert _clean_assumptions(None) == {}                 # no assumptions → engine defaults

print("\nALL CALC CHECKS PASS")
