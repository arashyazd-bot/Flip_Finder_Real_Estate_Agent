"""
FastAPI server for the dashboard.

Endpoints:
  POST   /api/login                    — email + password -> session cookie
  POST   /api/logout                   — clear session
  GET    /api/me                       — { email, is_admin, runs_today, daily_cap, remaining }
  GET    /api/health                   — both APIs status + bot up?
  GET    /api/search/stream            — SSE: city + count -> stream of events (quota-checked)
  GET    /api/history                  — [{slug, city, count, queried_at}, ...]
  GET    /api/history/{slug}           — full cached payload for one city
  DELETE /api/history/{slug}
  GET    /api/favorites                — {properties: [], cities: []}
  POST   /api/favorites/properties/{zpid}
  DELETE /api/favorites/properties/{zpid}
  POST   /api/favorites/cities/{slug}
  DELETE /api/favorites/cities/{slug}

  --- admin only ---
  GET    /api/admin/users              — [users]
  POST   /api/admin/users              — create user {email, password, daily_cap?}
  PATCH  /api/admin/users/{email}      — update cap / disabled / password
  DELETE /api/admin/users/{email}
  GET    /api/admin/runs               — recent run log
  GET    /api/admin/stats              — aggregate metrics

  --- public, no auth (rate-limited; the landing page's lead magnet) ---
  GET    /analyze                          — single-property workspace page
  GET    /api/public/analyze/stream        — SSE: one Zillow URL (+ purchase price, assumptions) -> report
  GET    /api/public/report/{token}/pdf    — PDF of a report this server produced (token from `complete`)

Static frontend served from /.
"""
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dashboard import storage, db
from dashboard.search_service import stream_search

# --- env loading (no python-dotenv dependency) ---
def _load_env_file():
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip(); v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)

_load_env_file()

SECRET_KEY = os.environ.get("DASHBOARD_SECRET_KEY", "dev-secret-change-me-" + os.urandom(8).hex())
# secure=True only when served over HTTPS (COOKIE_SECURE=1 in production). Module-level so
# login and logout always agree on cookie attributes (Safari requires an exact match to clear).
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "").lower() in ("1", "true", "yes")
SESSION_COOKIE = "dashboard_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

logger = logging.getLogger("dashboard")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

if not os.environ.get("DASHBOARD_SECRET_KEY"):
    logger.warning(
        "DASHBOARD_SECRET_KEY not set — using a random ephemeral key. All sessions will "
        "be invalidated on every restart/redeploy. Set DASHBOARD_SECRET_KEY in the environment."
    )

# Bootstrap DB + initial admin
db.init_db()
admin_email = db.bootstrap_admin_if_empty()
if admin_email:
    logger.info(f"Created initial admin user: {admin_email}")

app = FastAPI(title="Real Estate Investment Dashboard")
serializer = URLSafeTimedSerializer(SECRET_KEY, salt="session")

# The frontend is served same-origin by this same app, so cross-origin credentialed
# requests are never needed. Wildcard origins + credentials is an anti-pattern (lets
# any site make authenticated calls), and the spec forbids combining them anyway.
# Allow an explicit comma-separated FRONTEND_ORIGINS for any legit cross-origin use.
_cors_origins = [o.strip() for o in os.environ.get("FRONTEND_ORIGINS", "").split(",") if o.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )
else:
    # No cross-origin clients configured — allow read-only wildcard WITHOUT credentials.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=False,
    )


# ---- auth helpers ----

def _issue_session(email: str) -> str:
    return serializer.dumps({"v": 2, "email": email})


def _verify_session(token: Optional[str]) -> Optional[str]:
    """Return email if the session is valid and the user still exists & not disabled."""
    if not token:
        return None
    try:
        data = serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, Exception):
        return None
    if not isinstance(data, dict) or data.get("v") != 2:
        return None
    email = data.get("email")
    if not email:
        return None
    user = db.get_user(email)
    if not user or user.disabled:
        return None
    return email


def current_user_email(session: Optional[str] = Cookie(None, alias=SESSION_COOKIE)) -> str:
    email = _verify_session(session)
    if not email:
        raise HTTPException(status_code=401, detail="auth required")
    return email


def require_admin(email: str = Depends(current_user_email)) -> str:
    user = db.get_user(email)
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="admin required")
    return email


# ---- auth endpoints ----

@app.post("/api/login")
async def login(request: Request, response: Response):
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    pw = body.get("password") or ""
    user = db.authenticate(email, pw)
    if not user:
        raise HTTPException(status_code=401, detail="invalid email or password")
    token = _issue_session(user.email)
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_MAX_AGE,
        httponly=True, samesite="lax", secure=COOKIE_SECURE,
    )
    return {"ok": True, "email": user.email, "is_admin": user.is_admin}


@app.post("/api/logout")
async def logout(response: Response):
    # The delete MUST mirror the attributes the cookie was set with. WebKit/iOS Safari
    # refuses to clear a Secure+HttpOnly cookie when the expiring Set-Cookie lacks those
    # attributes, which left users signed in after "Sign out" once COOKIE_SECURE went on
    # in production. (Chrome is lenient here; Safari is not.)
    response.delete_cookie(
        SESSION_COOKIE, path="/",
        httponly=True, samesite="lax", secure=COOKIE_SECURE,
    )
    return {"ok": True}


@app.get("/api/me")
async def me(session: Optional[str] = Cookie(None, alias=SESSION_COOKIE)):
    email = _verify_session(session)
    if not email:
        return {"authenticated": False}
    user = db.get_user(email)
    if not user:
        return {"authenticated": False}
    runs = db.runs_today(email)
    remaining = db.remaining_runs(email)
    return {
        "authenticated": True,
        "email": user.email,
        "is_admin": user.is_admin,
        "daily_cap": user.daily_cap,
        "runs_today": runs,
        "remaining": remaining,  # None = unlimited (admin)
    }


# ---- health ----

@app.get("/api/health")
async def health(_: str = Depends(current_user_email)):
    return {
        "ok": True,
        "bright_data_token": bool(os.environ.get("BRIGHT_DATA_API_TOKEN")),
        "cache_dir": str(PROJECT_ROOT / "data" / "bright_data_cache"),
    }


# ---- search (SSE) ----

def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


@app.get("/api/scope")
async def api_scope(q: str = Query(...), email: str = Depends(current_user_email)):
    """Resolve a search string to its geographic scope + estimated API cost, so the UI can
    show a confirm before launching a heavy county report."""
    from dashboard.geo import resolve_scope
    from dashboard.search_service import _COUNTY_MAX_ZIPS, _COUNTY_ZIP_PAGES
    sc = resolve_scope(q)
    total = len(sc.zips)
    n = min(total, _COUNTY_MAX_ZIPS) if sc.kind == "county" else total
    return {
        "kind": sc.kind, "label": sc.label,
        "zip_count": n, "total_zips": total,
        "est_calls": (n * _COUNTY_ZIP_PAGES) if sc.kind == "county" else 0,
        "truncated": sc.kind == "county" and total > _COUNTY_MAX_ZIPS,
    }


@app.get("/api/hud")
async def api_hud(q: str = Query(...), email: str = Depends(current_user_email)):
    """HUD/FHA bank-owned (REO) foreclosures for the searched area — a free leads feed,
    separate from the flip analysis (HUD has no price/comps to value)."""
    import re as _re
    from dashboard.geo import resolve_scope
    from dashboard.hud import fetch_hud_reo
    sc = resolve_scope(q)
    state = sc.state or (lambda m: m.group(1).upper() if m else "")(_re.search(r",\s*([A-Za-z]{2})\b", q or ""))
    # ponytail: state-level — HUD REO is ~19/state, so zip-scoping mostly returns 0. Label as
    # "statewide" in the UI so it's never mistaken for local. Narrow by ZIP if it ever gets dense.
    return {"state": state, "properties": fetch_hud_reo(state)}


@app.post("/api/report/pdf")
async def report_pdf(request: Request, email: str = Depends(current_user_email)):
    """Render the on-screen report to a real vector PDF server-side (fpdf2) — no browser
    print chrome (no onrender.com URL/timestamp header-footer)."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    props = (payload or {}).get("properties") or []
    if not props:
        raise HTTPException(status_code=400, detail="No properties to render")
    payload["properties"] = props[:200]  # sane upper bound
    from dashboard.pdf_report import build_pdf
    try:
        pdf_bytes = build_pdf(payload)
    except Exception:
        logger.exception("PDF generation failed")
        raise HTTPException(status_code=500, detail="PDF generation failed")
    import re as _re
    safe = _re.sub(r"[^A-Za-z0-9]+", "_", str(payload.get("city") or "report")).strip("_") or "report"
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="FlipFinder Report - {safe}.pdf"'},
    )


@app.get("/api/search/stream")
async def search_stream(
    city: str = Query(...),
    count: int = Query(10, ge=1, le=50),
    intent: str = Query("flip"),
    # Optional pre-enrichment filters — applied to the discovery pool before we pick
    # the 30–50 to enrich, so Bright Data only pays for listings the user wants.
    min_price: Optional[float] = Query(None, ge=0),
    max_price: Optional[float] = Query(None, ge=0),
    min_beds: Optional[int] = Query(None, ge=0),
    min_baths: Optional[float] = Query(None, ge=0),
    home_type: Optional[str] = Query(None),
    # Deep scan: fan out per-zip on capped (~800) cities to assemble the full market.
    deep: bool = Query(False),
    # Tunable underwriting assumptions (all optional; omitted → evaluator defaults). Fractions,
    # not whole percents (0.12 = 12%). FastAPI ge/le rejects out-of-range before we ever run.
    hm_apr: Optional[float] = Query(None, ge=0, le=0.40),
    hold_months: Optional[int] = Query(None, ge=1, le=24),
    buy_closing_pct: Optional[float] = Query(None, ge=0, le=0.06),
    points_pct: Optional[float] = Query(None, ge=0, le=0.06),
    selling_pct: Optional[float] = Query(None, ge=0, le=0.15),
    opex_pct: Optional[float] = Query(None, ge=0, le=0.70),
    refi_apr: Optional[float] = Query(None, ge=0, le=0.20),
    email: str = Depends(current_user_email),
):
    # Cap check
    remaining = db.remaining_runs(email)
    if remaining is not None and remaining <= 0:
        raise HTTPException(
            status_code=429,
            detail="Daily run limit reached. Try again tomorrow or contact your admin.",
        )

    # Map the query knobs to FlipperEvaluator kwarg names; drop the ones not supplied.
    assumptions = {
        k: v for k, v in {
            "hard_money_apr": hm_apr, "hold_months": hold_months,
            "buy_closing_pct": buy_closing_pct, "points_pct": points_pct,
            "selling_cost_pct": selling_pct, "rental_opex_pct": opex_pct,
            "refi_apr": refi_apr,
        }.items() if v is not None
    }

    async def gen():
        try:
            async for ev in stream_search(
                city=city, count=count, intent=intent, user_email=email,
                min_price=min_price, max_price=max_price,
                min_beds=min_beds, min_baths=min_baths, home_type=home_type,
                deep=deep, assumptions=assumptions or None,
            ):
                yield _sse(ev["event"], ev["data"])
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.exception("search stream crashed")
            yield _sse("error", {"message": str(e)})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---- history (shared across users — that's the cost-saving point) ----

@app.get("/api/history")
async def history_list(_: str = Depends(current_user_email)):
    return {"items": storage.list_history()}


@app.get("/api/history/{slug}")
async def history_get(slug: str, _: str = Depends(current_user_email)):
    data = storage.load_history(slug)
    if not data:
        raise HTTPException(status_code=404, detail="not found")
    return data


@app.delete("/api/history/{slug}")
async def history_delete(slug: str, _: str = Depends(require_admin)):
    # Only admins can delete from the shared archive
    ok = storage.delete_history(slug)
    return {"ok": ok}


# ---- favorites (shared for now; can be per-user later) ----

@app.get("/api/favorites")
async def favorites_get(_: str = Depends(current_user_email)):
    return storage.get_favorites()


@app.post("/api/favorites/properties/{zpid}")
async def fav_prop_add(zpid: str, city_slug: Optional[str] = Query(None),
                       _: str = Depends(current_user_email)):
    return storage.favorite_property(zpid, True, city_slug=city_slug)


@app.delete("/api/favorites/properties/{zpid}")
async def fav_prop_remove(zpid: str, _: str = Depends(current_user_email)):
    return storage.favorite_property(zpid, False)


@app.post("/api/favorites/cities/{slug}")
async def fav_city_add(slug: str, _: str = Depends(current_user_email)):
    return storage.favorite_city(slug, True)


@app.delete("/api/favorites/cities/{slug}")
async def fav_city_remove(slug: str, _: str = Depends(current_user_email)):
    return storage.favorite_city(slug, False)


# ---- admin endpoints ----

@app.get("/api/admin/users")
async def admin_users_list(_: str = Depends(require_admin)):
    users = db.list_users()
    out = []
    for u in users:
        out.append({
            "email": u.email,
            "daily_cap": u.daily_cap,
            "is_admin": u.is_admin,
            "disabled": u.disabled,
            "created_at": u.created_at,
            "runs_today": db.runs_today(u.email),
        })
    return {"users": out}


@app.post("/api/admin/users")
async def admin_user_create(request: Request, _: str = Depends(require_admin)):
    body = await request.json()
    try:
        u = db.create_user(
            email=body.get("email", ""),
            password=body.get("password", ""),
            daily_cap=int(body.get("daily_cap", db.DEFAULT_DAILY_CAP)),
            is_admin=bool(body.get("is_admin", False)),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "ok": True,
        "user": {"email": u.email, "daily_cap": u.daily_cap, "is_admin": u.is_admin},
    }


@app.patch("/api/admin/users/{email}")
async def admin_user_update(email: str, request: Request, _: str = Depends(require_admin)):
    body = await request.json()
    changed = []
    try:
        if "daily_cap" in body:
            db.update_user_cap(email, int(body["daily_cap"]))
            changed.append("daily_cap")
        if "password" in body and body["password"]:
            db.reset_password(email, body["password"])
            changed.append("password")
        if "disabled" in body:
            db.set_disabled(email, bool(body["disabled"]))
            changed.append("disabled")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "changed": changed}


@app.delete("/api/admin/users/{email}")
async def admin_user_delete(email: str, current: str = Depends(require_admin)):
    if email.strip().lower() == current.strip().lower():
        raise HTTPException(status_code=400, detail="cannot delete yourself")
    ok = db.delete_user(email)
    return {"ok": ok}


@app.get("/api/runs")
async def user_runs(limit: int = Query(50, ge=1, le=500), email: str = Depends(current_user_email)):
    # Return runs belonging to the current user (most recent first)
    return {"runs": db.get_runs_for_user(email, limit=limit)}


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: int, email: str = Depends(current_user_email)):
    # SSE endpoint streaming run events for a specific run. Only owner or admin may subscribe.
    run = db.get_run_by_id(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    if run["user_email"] != email:
        user = db.get_user(email)
        if not user or not user.is_admin:
            raise HTTPException(status_code=403, detail="not authorized")

    async def gen():
        last_id = 0
        try:
            # Send existing events first
            rows = db.get_run_events(run_id, since_id=0)
            for r in rows:
                last_id = r["id"]
                yield _sse(r["event_type"], json.loads(r["data"]) if r["data"] else {})
            # Then poll for new events until run is finished
            while True:
                await asyncio.sleep(1.5)
                rows = db.get_run_events(run_id, since_id=last_id)
                for r in rows:
                    last_id = r["id"]
                    yield _sse(r["event_type"], json.loads(r["data"]) if r["data"] else {})
                # refresh run row
                run_row = db.get_run_by_id(run_id)
                if run_row and run_row.get("status") != "pending":
                    # stream any remaining events then exit
                    rows = db.get_run_events(run_id, since_id=last_id)
                    for r in rows:
                        yield _sse(r["event_type"], json.loads(r["data"]) if r["data"] else {})
                    # emit final 'complete' if finished
                    if run_row.get("status") == "ok":
                        yield _sse("complete", {"slug": run_row.get("city"), "total": run_row.get("count_requested")})
                    else:
                        yield _sse("error", {"message": run_row.get("error") or "finished"})
                    break
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("run events stream crashed")
            yield _sse("error", {"message": "server error"})

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/admin/runs")
async def admin_runs(limit: int = Query(100, ge=1, le=500),
                     _: str = Depends(require_admin)):
    return {"runs": db.recent_runs(limit=limit)}


@app.get("/api/admin/stats")
async def admin_stats(_: str = Depends(require_admin)):
    return db.aggregate_stats()


# ---- static frontend ----

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)


@app.get("/")
async def root():
    # Public marketing landing page (SaaS front door). The app itself lives at /app —
    # its client redirects unauthenticated visitors to /login via /api/me.
    return FileResponse(STATIC_DIR / "landing.html")


@app.get("/app")
async def app_page():
    # no-store: after sign-out, Back must not resurrect a cached authenticated-looking
    # page from bfcache — the page reload re-runs the /api/me gate instead.
    return FileResponse(STATIC_DIR / "index.html",
                        headers={"Cache-Control": "no-store"})


@app.get("/login")
async def login_page():
    return FileResponse(STATIC_DIR / "login.html",
                        headers={"Cache-Control": "no-store"})


@app.get("/admin")
async def admin_page():
    return FileResponse(STATIC_DIR / "admin.html")


# ---- public single-property analyzer (no auth; rate-limited; see dashboard/public_analyze.py) ----

from dashboard import public_analyze as pub  # noqa: E402  (after env + db bootstrap above)


@app.get("/analyze")
async def analyze_page():
    return FileResponse(STATIC_DIR / "analyze.html", headers={"Cache-Control": "no-store"})


@app.get("/api/public/analyze/stream")
async def public_analyze_stream(
    request: Request,
    url: str = Query(..., max_length=600),
    # The buyer's actual/offer price. A real evaluator kwarg — NOT an override of the list
    # price, which would silently cap the ARV (see FlipperEvaluator.evaluate).
    price: Optional[float] = Query(None, ge=pub.PRICE_MIN, le=pub.PRICE_MAX),
    # Same knobs, names and bounds as /api/search/stream so the shared assumptions UI works.
    hm_apr: Optional[float] = Query(None, ge=0, le=0.40),
    hold_months: Optional[int] = Query(None, ge=1, le=24),
    buy_closing_pct: Optional[float] = Query(None, ge=0, le=0.06),
    points_pct: Optional[float] = Query(None, ge=0, le=0.06),
    selling_pct: Optional[float] = Query(None, ge=0, le=0.15),
    opex_pct: Optional[float] = Query(None, ge=0, le=0.70),
    refi_apr: Optional[float] = Query(None, ge=0, le=0.20),
    session: Optional[str] = Cookie(None, alias=SESSION_COOKIE),
):
    try:
        canon, zpid = pub.parse_zillow_url(url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    ip = pub.client_ip(request)
    # Logged on purpose: confirms Render's X-Forwarded-For shape from production so the
    # hop index in public_analyze can be pinned. Low volume (capped at GLOBAL_DAY).
    logger.info("public analyze ip=%s xff=%r zpid=%s",
                ip, request.headers.get("x-forwarded-for"), zpid)

    # Approved users bypass the anonymous limiter (they're already a known account) but
    # still get one-at-a-time; their runs log under their own email for cost telemetry.
    who = _verify_session(session) if session else None
    if who:
        pub.acquire_inflight(ip)
    else:
        rej = pub.check_and_charge(ip, cached=pub.is_cached(zpid))
        if rej:
            raise HTTPException(status_code=rej["status"], detail=rej["detail"],
                                headers={"Retry-After": str(rej.get("retry_after", 60))})
    # Admission (and the in-flight slot) is settled ABOVE, before the 200 is committed —
    # rejecting inside the generator would surface as a generic stream error to the client.

    assumptions = {
        k: v for k, v in {
            "hard_money_apr": hm_apr, "hold_months": hold_months,
            "buy_closing_pct": buy_closing_pct, "points_pct": points_pct,
            "selling_cost_pct": selling_pct, "rental_opex_pct": opex_pct,
            "refi_apr": refi_apr,
        }.items() if v is not None
    }

    async def gen():
        try:
            async for ev in pub.stream_one(canon, zpid, pub.clean_price(price),
                                           assumptions or None, who or pub.ANON_EMAIL):
                yield _sse(ev["event"], ev["data"])
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("public analyze stream crashed")
            yield _sse("error", {"code": "UPSTREAM", "message": "Something went wrong on our side."})
        finally:
            pub.release(ip)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/public/report/{token}/pdf")
async def public_report_pdf(token: str):
    """Render a report THIS server produced. Never accepts a client payload — a public
    renderer of arbitrary JSON would stamp attacker text into a FlipFinder-branded PDF."""
    payload = pub.get_payload(token)
    if not payload:
        raise HTTPException(status_code=410,
                            detail="This report link has expired — run the analysis again for a fresh PDF.")
    from dashboard.pdf_report import build_pdf
    try:
        pdf_bytes = build_pdf(payload)
    except Exception:
        logger.exception("public PDF generation failed")
        raise HTTPException(status_code=500, detail="PDF generation failed")
    import re as _re
    safe = _re.sub(r"[^A-Za-z0-9]+", "_", str(payload.get("city") or "report")).strip("_") or "report"
    return Response(
        content=pdf_bytes, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="FlipFinder Report - {safe}.pdf"'},
    )


# Fallbacks so crawlers/browsers that request /favicon.ico or /favicon.svg get the served static SVG
@app.get("/favicon.ico")
async def favicon_ico():
    return FileResponse(STATIC_DIR / "favicon.svg")


@app.get("/favicon.svg")
async def favicon_svg():
    return FileResponse(STATIC_DIR / "favicon.svg")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("DASHBOARD_PORT", "8000"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
