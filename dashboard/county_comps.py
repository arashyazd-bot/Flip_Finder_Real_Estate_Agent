"""
Free recorded sold comps from the Sacramento County assessor ArcGIS layer
("Sales by Property Type"). No key, no auth. Output is shaped like a Zillow
nearbyHomes entry so agents.comp_analyzer.analyze_comps and the sold-pool code in
dashboard.search_service consume it unchanged.

Self-check (live network):  ./venv/bin/python -m dashboard.county_comps
"""
import functools
import logging
import math
from datetime import datetime, timedelta
from typing import Optional

import requests

from dashboard.search_service import _pool_comps_near

logger = logging.getLogger(__name__)

URL = "https://mapservices.gis.saccounty.net/arcgis/rest/services/ASSESSOR/MapServer/1/query"
TIMEOUT = 8

# TOTAL_LIVING_AREA, never BUILDING_SF: BUILDING_SF is populated on ~1.6k of 57k rows,
# so filtering on it silently drops 99% of sales.
FIELDS = ("PARCEL_NUMBER,SITUS_ADDRESS1,NUMBER_OF_BEDROOMS,NUMBER_OF_BATHS,"
          "NUMBER_OF_HALF_BATHS,TOTAL_LIVING_AREA,EFFECTIVE_YEAR_BUILT,"
          "INDICATED_SALES_PRICE,DOCUMENT_DATE,LOT_SIZE_SQFEET,NUMBER_OF_STORIES,"
          "Property_Type,DOCUMENT_TYPE_DESCRIPTION")

# Explicit map; anything else (Vacant Land, Commercial, Other) is skipped. An empty
# homeType would PASS comp_analyzer's NON_COMP_TYPES filter, so unmapped must mean None.
HOME_TYPES = {
    "Single Family Residence": "SINGLE_FAMILY",
    "Condominium": "CONDO",
    "Multiple Family Residence": "MULTI_FAMILY",
}

# Sacramento County bbox. Deliberately loose: it also swallows Davis / West Sac (Yolo)
# and Roseville (Placer), which this layer does NOT cover — sold_comps() treats
# "request ok, zero rows" as out_of_area for exactly that reason.
BBOX = (38.02, 38.74, -121.87, -121.03)


def in_market(lat: float, lng: float) -> bool:
    lat_min, lat_max, lng_min, lng_max = BBOX
    return lat_min <= lat <= lat_max and lng_min <= lng <= lng_max


def _get(params: dict) -> Optional[list]:
    """Raw ArcGIS query. None on any failure (timeout, non-200, {"error":...} body)."""
    try:
        r = requests.get(URL, params={"f": "json", "outFields": FIELDS,
                                      "returnGeometry": "true", "outSR": "4326",
                                      **params}, timeout=TIMEOUT)
        if r.status_code != 200:
            logger.warning("county_comps: HTTP %s", r.status_code)
            return None
        body = r.json()
        if "error" in body:
            logger.warning("county_comps: API error %s", body["error"])
            return None
        return body.get("features") or []
    except Exception as e:  # network, JSON, anything — comps are optional
        logger.warning("county_comps: request failed: %s", e)
        return None


# maxsize=16, not 256: each cell is ~0.5 MB of parcel polygons and prod has 512 MB.
# Cached value is the raw feature list so per-subject ranking still runs per call.
@functools.lru_cache(maxsize=16)
def _fetch_cell(lat: float, lng: float, radius_km: float, months: int) -> Optional[tuple]:
    dlat = radius_km / 111.0
    dlng = radius_km / (111.0 * math.cos(math.radians(lat)))
    cutoff = (datetime.utcnow() - timedelta(days=30 * months)).date().isoformat()
    # Spatial envelope, not PARCEL_NUMBER LIKE — the LIKE scans time out server-side;
    # a ~2 km envelope returns in under a second and stays under the 2000-row cap.
    feats = _get({
        "geometry": f"{lng - dlng},{lat - dlat},{lng + dlng},{lat + dlat}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "where": f"TOTAL_LIVING_AREA>0 AND NUMBER_OF_BEDROOMS>0 AND DOCUMENT_DATE >= DATE '{cutoff}'",
    })
    return None if feats is None else tuple(feats)


def _centroid(feature: dict) -> Optional[tuple]:
    # returnCentroid=true is ignored by this server; average rings[0] of the parcel polygon.
    rings = (feature.get("geometry") or {}).get("rings") or []
    pts = rings[0] if rings else []
    if not pts:
        return None
    return (sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts))


def _to_comp(feature: dict) -> Optional[dict]:
    a = feature.get("attributes") or {}
    home_type = HOME_TYPES.get(a.get("Property_Type") or "")
    centroid = _centroid(feature)
    try:
        price = int(float(a.get("INDICATED_SALES_PRICE") or 0))
        sqft = int(a.get("TOTAL_LIVING_AREA") or 0)
    except (TypeError, ValueError):
        return None
    # analyze_comps drops price<=50k / sqft<=200 anyway; fail early so the pool stays lean.
    if not (home_type and centroid and price > 50_000 and sqft > 200):
        return None
    ms = a.get("DOCUMENT_DATE")
    return {
        "zpid": f"APN-{a.get('PARCEL_NUMBER')}",
        "price": price,
        "livingArea": sqft,
        "bedrooms": int(a["NUMBER_OF_BEDROOMS"]) if a.get("NUMBER_OF_BEDROOMS") else None,
        "bathrooms": float(a.get("NUMBER_OF_BATHS") or 0) + 0.5 * float(a.get("NUMBER_OF_HALF_BATHS") or 0),
        "yearBuilt": int(a["EFFECTIVE_YEAR_BUILT"]) if a.get("EFFECTIVE_YEAR_BUILT") else None,
        "homeType": home_type,
        "hdpTypeDimension": "RecentlySold",
        "dateSold": datetime.utcfromtimestamp(ms / 1000).date().isoformat() if ms else None,
        # SITUS_ADDRESS1 pads with multiple spaces ('1438   U ST'); collapse them.
        "address": {"streetAddress": " ".join(str(a.get("SITUS_ADDRESS1") or "").split())},
        "latitude": centroid[0],
        "longitude": centroid[1],
        "source": "sacramento_county",
    }


def sold_comps(lat: float, lng: float, radius_km: float = 1.6, months: int = 18,
               limit: int = 15) -> tuple:
    """(comps, status); status in "ok" | "out_of_area" | "unavailable"."""
    if not in_market(lat, lng):
        return [], "out_of_area"
    feats = _fetch_cell(round(lat, 2), round(lng, 2), radius_km, months)
    if feats is None:
        return [], "unavailable"
    pool = [c for c in map(_to_comp, feats) if c]
    comps = _pool_comps_near(pool, lat, lng, radius_km=radius_km, limit=limit)
    # Request succeeded but nothing usable: we're inside the loose bbox but outside the
    # county's data (Yolo/Placer), or a truly dead cell. Either way: not our market.
    return (comps, "ok") if comps else ([], "out_of_area")


def subject_record(street_address: str) -> Optional[dict]:
    """The subject's own county row (sqft/baths beat Zillow's). Only sold parcels exist
    in this layer, so a never-sold subject returns None — expected."""
    parts = " ".join(str(street_address or "").split()).split(" ", 1)
    if len(parts) < 2:
        return None
    num, rest = parts
    # '%' between number and street absorbs the layer's multi-space padding.
    feats = _get({"where": f"SITUS_ADDRESS1 LIKE '{num}%{rest.upper()}%'",
                  "orderByFields": "DOCUMENT_DATE DESC", "resultRecordCount": 1})
    if not feats:
        return None
    a = feats[0].get("attributes") or {}
    try:
        ms = a.get("DOCUMENT_DATE")
        return {
            "livingArea": int(a.get("TOTAL_LIVING_AREA") or 0),
            "bedrooms": int(a.get("NUMBER_OF_BEDROOMS") or 0),
            "bathrooms": float(a.get("NUMBER_OF_BATHS") or 0) + 0.5 * float(a.get("NUMBER_OF_HALF_BATHS") or 0),
            "yearBuilt": int(a.get("EFFECTIVE_YEAR_BUILT") or 0),
            "apn": str(a.get("PARCEL_NUMBER") or ""),
            "lastSale": {
                "price": int(float(a.get("INDICATED_SALES_PRICE") or 0)),
                "date": datetime.utcfromtimestamp(ms / 1000).date().isoformat() if ms else None,
            },
        }
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)

    def _fx(**over):
        attrs = {
            "PARCEL_NUMBER": "009-0123-004-0000", "SITUS_ADDRESS1": "1438   U ST",
            "NUMBER_OF_BEDROOMS": 3, "NUMBER_OF_BATHS": 2, "NUMBER_OF_HALF_BATHS": 1,
            "TOTAL_LIVING_AREA": 1500, "EFFECTIVE_YEAR_BUILT": 1950,
            "INDICATED_SALES_PRICE": "730000.0000", "DOCUMENT_DATE": 1750000000000,
            "Property_Type": "Single Family Residence",
        }
        geom = {"rings": [[[-121.0, 38.0], [-121.0, 38.1], [-120.9, 38.1], [-120.9, 38.0]]]}
        attrs.update({k: v for k, v in over.items() if k != "geometry"})
        return {"attributes": attrs, "geometry": over.get("geometry", geom)}

    # 1
    c = _to_comp(_fx())
    assert c["bathrooms"] == 2.5, c
    assert c["hdpTypeDimension"] == "RecentlySold"
    assert c["address"]["streetAddress"] == "1438 U ST"
    assert c["homeType"] == "SINGLE_FAMILY"
    assert abs(c["latitude"] - 38.05) < 1e-9 and abs(c["longitude"] + 120.95) < 1e-9, c
    assert c["price"] == 730000 and c["zpid"] == "APN-009-0123-004-0000"
    # 2
    assert _to_comp(_fx(Property_Type="Vacant Land")) is None
    assert _to_comp(_fx(INDICATED_SALES_PRICE="40000.0000")) is None
    assert _to_comp(_fx(geometry=None)) is None
    # 3
    assert in_market(37.77, -122.42) is False
    assert in_market(38.557, -121.4907) is True
    # 4 live
    comps, status = sold_comps(38.557022, -121.49068)
    assert status == "ok", status
    assert len(comps) >= 3, len(comps)
    for x in comps:
        assert x["price"] > 50000 and x["livingArea"] > 200
        assert x["hdpTypeDimension"] == "RecentlySold"
        assert x["latitude"] and x["longitude"]
    print(f"{len(comps)} comps near 1815 2nd Ave:")
    for x in comps[:5]:
        d = _pool_comps_near([x], 38.557022, -121.49068, radius_km=99, limit=1)
        dx = (x["longitude"] + 121.49068) * math.cos(math.radians(38.557)) * 111.32
        dy = (x["latitude"] - 38.557022) * 110.57
        print(f"  {x['address']['streetAddress']:<28} ${x['price']:>9,}  {x['livingArea']:>5} sqft"
              f"  {x['bathrooms']} ba  {x['dateSold']}  {math.hypot(dx, dy):.2f} km")
    # 5 live
    assert sold_comps(37.77, -122.42) == ([], "out_of_area")
    # 6 live
    s = subject_record("1805 2nd Ave")
    assert s and s["livingArea"] == 1208 and s["bathrooms"] == 1.0, s
    print("subject:", s)
    print("COUNTY_COMPS SELF-CHECK PASS")
