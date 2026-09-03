"""
Bright Data Zillow enricher.

Calls Bright Data's Web Scraper API to fetch full Zillow listing detail
(year built, price/tax history, zestimate, description, schools, photos, etc.)
for a batch of Zillow URLs. Single async snapshot per batch.

Reads BRIGHT_DATA_API_TOKEN from .env (no python-dotenv dependency).
"""
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import requests

PROJECT_ROOT = Path(__file__).parent.parent
CACHE_DIR = PROJECT_ROOT / "data" / "bright_data_cache"

TRIGGER_URL = "https://api.brightdata.com/datasets/v3/trigger"
SNAPSHOT_URL = "https://api.brightdata.com/datasets/v3/snapshot/{snapshot_id}"
ZILLOW_DETAIL_DATASET_ID = "gd_lfqkr8wm13ixtbd8f5"

POLL_INTERVAL_SEC = int(os.environ.get("BRIGHT_DATA_POLL_INTERVAL_SEC", "10"))
POLL_TIMEOUT_SEC = int(os.environ.get("BRIGHT_DATA_POLL_TIMEOUT_SEC", "1200"))
# Cached records older than this are treated as stale (prices/comps move). 0 = no TTL.
CACHE_TTL_DAYS = float(os.environ.get("BRIGHT_DATA_CACHE_TTL_DAYS", "14"))


def _cache_fresh(rec: dict) -> bool:
    """True if a cached record is within the TTL window (or TTL disabled)."""
    if CACHE_TTL_DAYS <= 0:
        return True
    ts = rec.get("_cached_at")
    if not ts:
        return False  # legacy cache without a timestamp — refetch to be safe
    return (time.time() - ts) < CACHE_TTL_DAYS * 86400.0


def _filter_error_records(records: list):
    """Split Bright Data records into (clean, num_errors). include_errors=true can
    return {url, error, error_code,...} stubs that would otherwise pollute scoring."""
    clean, errors = [], 0
    for r in records:
        if isinstance(r, dict) and (r.get("error") or r.get("error_code") or r.get("error_type")):
            errors += 1
            continue
        clean.append(r)
    return clean, errors


def _load_token() -> str:
    token = os.environ.get("BRIGHT_DATA_API_TOKEN")
    if token:
        return token
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == "BRIGHT_DATA_API_TOKEN":
                return v.strip().strip('"').strip("'")
    raise RuntimeError(
        "BRIGHT_DATA_API_TOKEN not set. Add it to .env or export it."
    )


def _zpid_from_url(url: str) -> Optional[str]:
    # https://www.zillow.com/homedetails/.../15190324_zpid/
    for part in url.rstrip("/").split("/"):
        if part.endswith("_zpid"):
            return part[: -len("_zpid")]
    return None


def _cache_path(zpid: str) -> Path:
    return CACHE_DIR / f"{zpid}.json"


class BrightDataZillowEnricher:
    def __init__(self, token: Optional[str] = None, dataset_id: str = ZILLOW_DETAIL_DATASET_ID,
                 poll_timeout_sec: int = POLL_TIMEOUT_SEC):
        self.token = token or _load_token()
        self.dataset_id = dataset_id
        # Per-instance so an interactive caller (a web request) can bound the wait, while the
        # batch city scan keeps the long module default. Note a caller cancelling its own
        # future does NOT stop the worker thread — only this deadline does.
        self.poll_timeout_sec = int(poll_timeout_sec)
        # Populated by enrich(): {"attempted": int, "from_cache": int, "fresh": int}
        self.last_stats: Dict[str, int] = {"attempted": 0, "from_cache": 0, "fresh": 0}
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def enrich(self, urls: Iterable[str], use_cache: bool = True) -> Dict[str, dict]:
        """Return {zpid: enriched_record} for every URL. Cached zpids skip the API.

        Side effect: populates self.last_stats with:
            {"attempted": N, "from_cache": C, "fresh": F}
        Callers (e.g. dashboard/search_service) read this for cost telemetry.
        """
        urls = list(urls)
        results: Dict[str, dict] = {}
        to_fetch: List[str] = []
        cache_hits = 0

        for u in urls:
            zpid = _zpid_from_url(u)
            if zpid and use_cache and _cache_path(zpid).exists():
                try:
                    rec = json.loads(_cache_path(zpid).read_text())
                    if _cache_fresh(rec):
                        results[zpid] = rec
                        cache_hits += 1
                        continue
                    # else: stale — fall through and refetch
                except Exception:
                    pass
            to_fetch.append(u)

        fresh_count = 0
        errors_dropped = 0
        self.last_error = None
        if to_fetch:
            # _trigger may raise on quota/inactive (402/400) — let that propagate so
            # the caller can surface a quota banner. Only the polling phase is made
            # resilient, so a snapshot timeout still returns the cache hits we have.
            snapshot_id = self._trigger(to_fetch)
            print(f"  Bright Data snapshot triggered: {snapshot_id}", file=sys.stderr)
            try:
                records = self._wait_for_snapshot(snapshot_id)
            except TimeoutError as e:
                print(f"  snapshot timed out: {e}", file=sys.stderr)
                self.last_error = str(e)
                records = []
            records, errors_dropped = _filter_error_records(records)
            print(f"  Got {len(records)} enriched records ({errors_dropped} error stubs dropped)",
                  file=sys.stderr)

            for rec in records:
                zpid = str(rec.get("zpid") or _zpid_from_url(rec.get("url", "")) or "")
                if not zpid:
                    continue
                rec["_cached_at"] = time.time()
                results[zpid] = rec
                fresh_count += 1
                try:
                    _cache_path(zpid).write_text(json.dumps(rec, indent=2))
                except Exception as e:
                    print(f"  cache write failed for {zpid}: {e}", file=sys.stderr)

        self.last_stats = {
            "attempted": len(urls),
            "from_cache": cache_hits,
            "fresh": fresh_count,
            "errors_dropped": errors_dropped,
        }
        return results

    def _trigger(self, urls: List[str]) -> str:
        payload = [{"url": u} for u in urls]
        r = requests.post(
            TRIGGER_URL,
            params={"dataset_id": self.dataset_id, "include_errors": "true"},
            headers=self._headers,
            json=payload,
            timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Trigger failed {r.status_code}: {r.text[:500]}")
        data = r.json()
        snapshot_id = data.get("snapshot_id") or data.get("id")
        if not snapshot_id:
            raise RuntimeError(f"No snapshot_id in trigger response: {data}")
        return snapshot_id

    def _wait_for_snapshot(self, snapshot_id: str) -> List[dict]:
        url = SNAPSHOT_URL.format(snapshot_id=snapshot_id)
        timeout = self.poll_timeout_sec
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = requests.get(url, params={"format": "json"}, headers=self._headers, timeout=30)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    return data
                # Some datasets wrap results
                return data.get("data") or data.get("results") or []
            if r.status_code == 202:
                elapsed = int(timeout - (deadline - time.time()))
                print(f"  ...still running ({elapsed}s elapsed)", file=sys.stderr)
                time.sleep(POLL_INTERVAL_SEC)
                continue
            raise RuntimeError(f"Snapshot poll failed {r.status_code}: {r.text[:500]}")
        raise TimeoutError(f"Snapshot {snapshot_id} did not complete within {timeout}s")


if __name__ == "__main__":
    # Smoke test: enrich one SF property
    test_urls = sys.argv[1:] or [
        "https://www.zillow.com/homedetails/263-Friedell-St-San-Francisco-CA-94124/241586489_zpid/"
    ]
    enricher = BrightDataZillowEnricher()
    out = enricher.enrich(test_urls)
    for zpid, rec in out.items():
        print(f"\n=== {zpid} ===")
        print(f"Fields ({len(rec)}): {sorted(rec.keys())}")
        for k in ("streetAddress", "city", "state", "price", "yearBuilt", "zestimate",
                  "rentZestimate", "livingArea", "lotSize", "homeType", "homeStatus",
                  "propertyTaxRate", "monthlyHoaFee", "description", "daysOnZillow"):
            if k in rec:
                v = rec[k]
                preview = (str(v)[:120] + "...") if len(str(v)) > 120 else v
                print(f"  {k}: {preview}")
