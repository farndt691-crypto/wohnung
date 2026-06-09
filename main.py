"""
main.py – FastAPI App (Vercel-kompatibel, Supabase REST)
=========================================================
Starten: uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.requests import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from calculator import berechne_kennzahlen
from config import SERVER_HOST, SERVER_PORT
from database import (
    get_listings,
    get_stats,
    init_db,
    patch_listing_miete,
    update_listing,
    url_exists,
    upsert_listing,
)
from scraper_http import run_scraping_sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
_templates_dir = os.environ.get(
    "TEMPLATES_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"),
)
templates = Jinja2Templates(directory=_templates_dir)

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
_startup_error: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _startup_error
    try:
        init_db()
        logger.info("Supabase-Verbindung OK.")
    except Exception as e:
        import traceback
        _startup_error = traceback.format_exc()
        logger.error(f"Startup-Fehler (App läuft trotzdem): {e}")
    yield


app = FastAPI(
    title="Immobilien-Sniper",
    description="Deal-Prüfung für Eigentumswohnungen in Mannheim",
    version="3.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# HTML Dashboard
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard(
    request: Request,
    score:  Optional[str] = Query(None),
    source: Optional[str] = Query(None),
):
    try:
        listings = get_listings(deal_score=score, source=source)
        stats    = get_stats()
    except Exception as e:
        listings = []
        stats = {"total": 0, "strong_buy": 0, "watch": 0, "skip": 0,
                 "last_run": f"Fehler: {e}"}
    try:
        return templates.TemplateResponse(
            request,
            "index.html",
            context={
                "listings":      listings,
                "stats":         stats,
                "filter_score":  score  or "",
                "filter_source": source or "",
            },
        )
    except Exception as e:
        import traceback
        return HTMLResponse(
            f"<pre>Template-Fehler:\n{traceback.format_exc()}</pre>",
            status_code=500,
        )


# ---------------------------------------------------------------------------
# Vercel Cron (08:00 + 20:00 UTC)
# ---------------------------------------------------------------------------
@app.get("/api/cron", include_in_schema=False)
def cron_scrape(request: Request):
    secret = os.environ.get("CRON_SECRET", "")
    if secret and request.headers.get("authorization") != f"Bearer {secret}":
        raise HTTPException(401, "Unauthorized")
    result = run_scraping_sync()
    return {"ok": True, **result}


# ---------------------------------------------------------------------------
# Manuelles Scraping
# ---------------------------------------------------------------------------
@app.post("/api/scrape")
def api_trigger_scrape(background_tasks: BackgroundTasks):
    background_tasks.add_task(run_scraping_sync)
    return {"message": "Scraping gestartet.", "status": "running"}


# ---------------------------------------------------------------------------
# Listings API
# ---------------------------------------------------------------------------
@app.get("/api/listings")
def api_get_listings(
    score:  Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    limit:  int           = Query(200, ge=1, le=1000),
):
    return get_listings(deal_score=score, source=source, limit=limit)


@app.patch("/api/listings/{listing_id}/miete")
def api_patch_miete(
    listing_id: int,
    kaltmiete_monat: float = Query(...),
):
    item = patch_listing_miete(listing_id, kaltmiete_monat)
    if not item:
        raise HTTPException(404, "Listing nicht gefunden")
    kz = berechne_kennzahlen(
        kaufpreis=item.get("kaufpreis"),
        wohnflaeche_qm=item.get("wohnflaeche_qm"),
        kaltmiete_monat_inserat=kaltmiete_monat,
        quadrat=item.get("quadrat"),
    )
    kz["miete_geschaetzt"] = False
    return update_listing(listing_id, kz)


@app.get("/api/stats")
def api_stats():
    return get_stats()


# ---------------------------------------------------------------------------
# Debug
# ---------------------------------------------------------------------------
@app.get("/api/debug")
def api_debug():
    import sys
    return {
        "startup_error":      _startup_error,
        "python":             sys.version,
        "SUPABASE_URL_set":   bool(os.environ.get("SUPABASE_URL")),
        "SUPABASE_KEY_set":   bool(os.environ.get("SUPABASE_KEY")),
        "templates_dir":      _templates_dir,
    }


# ---------------------------------------------------------------------------
# Lokaler Start
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=SERVER_HOST, port=SERVER_PORT, reload=True)
