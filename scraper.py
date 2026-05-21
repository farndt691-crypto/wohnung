"""
scraper.py - Immobilien-Sniper v3.0
Parallel: 4 concurrent detail pages + parallel Gemini calls via asyncio.
~3-4x faster than v2.1 sequential approach.
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from google import genai
from playwright.async_api import async_playwright, BrowserContext

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")
CONFIG_PATH = Path("config.json")
DEALS_PATH  = Path("data/deals.json")
MAX_NEW_PER_RUN   = 20
PAGE_TIMEOUT_MS   = 12000
DETAIL_WAIT_MS    = 500
MAX_RUNTIME_SECS  = 1500   # 25 min hard limit (workflow: 45 min)
DETAIL_WORKERS    = 4      # parallel Playwright detail pages
GEMINI_WORKERS    = 4      # parallel Gemini threads (stay under 15 RPM free tier)

MHM_LAT_MIN, MHM_LAT_MAX = 49.40, 49.60
MHM_LON_MIN, MHM_LON_MAX = 8.35,  8.65

MANNHEIM_RE = re.compile(r'\bMannheim\b', re.IGNORECASE)
NON_MANNHEIM_RE = re.compile(
    r'\b(Hamburg|Berlin|Muenchen|Frankfurt am Main|Stuttgart|Koeln|Duesseldorf|'
    r'Dortmund|Essen|Leipzig|Dresden|Hannover|Nuernberg|Bremen|Duisburg|'
    r'Bochum|Wuppertal|Bielefeld|Bonn|Muenster|Freiburg|HafenCity|'
    r'Buxtehude|Harburg|Altona|Wandsbek|Barmbek|Bergedorf|'
    r'Pankow|Kreuzberg|Friedrichshain|Karlsruhe|Heidelberg|Ludwigshafen)\b',
    re.IGNORECASE,
)

_gemini_key_invalid = False
_start_time = None


# ─── Config / data helpers ────────────────────────────────────────────────────

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.error("config.json nicht gefunden!")
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    log.info("Config: Stadt=%s minZimmer=%s", cfg.get("target_city"), cfg.get("min_rooms"))
    return cfg


def load_deals() -> dict:
    DEALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DEALS_PATH.exists():
        try:
            with open(DEALS_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error("deals.json fehler: %s", e)
    return {"last_updated": None, "deals": []}


def save_deals(data: dict) -> None:
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(DEALS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info("Gespeichert: %d Deals", len(data["deals"]))


def get_existing_urls(data: dict) -> set:
    return {d["url"] for d in data.get("deals", [])}


def time_ok() -> bool:
    return (time.time() - _start_time) < MAX_RUNTIME_SECS


def is_mannheim(title: str, raw_text: str) -> bool:
    combined = title + " " + raw_text
    if NON_MANNHEIM_RE.search(combined):
        return False
    return bool(MANNHEIM_RE.search(combined))


def valid_mannheim_coords(lat, lon) -> bool:
    if lat is None or lon is None:
        return False
    try:
        return MHM_LAT_MIN <= float(lat) <= MHM_LAT_MAX and MHM_LON_MIN <= float(lon) <= MHM_LON_MAX
    except (TypeError, ValueError):
        return False


# ─── Score calculation ────────────────────────────────────────────────────────

def berechne_scores(deal: dict, cfg: dict) -> dict:
    fin      = cfg.get("financing", {})
    base     = cfg.get("base_rent_per_sqm", 14.50)
    boni_map = cfg.get("rent_bonuses", {})

    if deal.get("listing_type") == "kauf":
        kaufpreis = deal.get("kaufpreis")
        kaltmiete = deal.get("kaltmiete")
        qm        = deal.get("quadratmeter")
        if not kaufpreis:
            deal.setdefault("deal_score", "skip")
            return deal
        if not kaltmiete and qm:
            kaltmiete = round(qm * 12.00, 2)
            deal["kaltmiete"] = kaltmiete
            deal["miete_geschaetzt"] = True
        if kaltmiete:
            rate     = fin.get("interest_rate", 0.035) + fin.get("repayment_rate", 0.015)
            bankrate = round((kaufpreis * fin.get("overhead_factor", 1.10)) * rate / 12, 2)
            cashflow = round(kaltmiete - bankrate - fin.get("maintenance_monthly", 50), 2)
            deal["bankrate_monat"] = bankrate
            deal["cashflow_monat"] = cashflow
            deal["deal_score"] = (
                "strong_buy" if cashflow >= 0 else
                "watch"      if cashflow >= -150 else "skip"
            )
            log.info("   Kauf: %s EUR | bank=%.0f cf=%+.0f -> %s",
                     kaufpreis, bankrate, cashflow, deal["deal_score"])
        else:
            deal.setdefault("deal_score", "skip")

    elif deal.get("listing_type") == "miete":
        kaltmiete = deal.get("kaltmiete")
        qm        = deal.get("quadratmeter")
        boni      = deal.get("boni", [])
        if kaltmiete and qm and qm > 0:
            kaltmiete_qm = round(kaltmiete / qm, 2)
            deal["kaltmiete_qm"] = kaltmiete_qm
            aufschlag    = sum(boni_map.get(b, 0.0) for b in boni)
            expected_qm  = round(base + aufschlag, 2)
            deal["expected_market_qm"] = expected_qm
            deviation    = round(((kaltmiete_qm - expected_qm) / expected_qm) * 100, 1)
            deal["deal_deviation_pct"] = deviation
            deal["deal_score"] = (
                "gut"   if deviation <= -10 else
                "okay"  if deviation <=  10 else "teuer"
            )
            log.info("   Miete: %.0f EUR | %.2f EUR/m2 | abw=%+.1f%% -> %s",
                     kaltmiete, kaltmiete_qm, deviation, deal["deal_score"])
        else:
            deal.setdefault("deal_score", "okay")
    return deal


def build_deal(raw: dict, ai: Optional[dict], now_iso: str, cfg: dict) -> dict:
    deal = {
        "url":            raw["url"],
        "source":         raw["source"],
        "listing_type":   raw["listing_type"],
        "title":          raw["title"],
        "first_seen":     now_iso,
        "last_seen":      now_iso,
        "boni":           [],
        "kurz_bewertung": "",
        "zimmeranzahl":   cfg.get("min_rooms", 2.5),
        "latitude":       None,
        "longitude":      None,
        "stadtteil":      None,
        "adresse":        None,
    }
    if ai:
        for k, v in ai.items():
            if k in ("boni", "kurz_bewertung"):
                if v:
                    deal[k] = v
            elif v is not None:
                deal[k] = v
        if ai.get("listing_type") in ("kauf", "miete"):
            deal["listing_type"] = ai["listing_type"]
    return berechne_scores(deal, cfg)


# ─── Async detail scraping ────────────────────────────────────────────────────

async def scrape_detail_async(ctx: BrowserContext, url: str,
                               sem: asyncio.Semaphore) -> str:
    async with sem:
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            await page.wait_for_timeout(DETAIL_WAIT_MS)
            parts = []

            for sel in ["#viewad-price", "[data-testid='price-amount']",
                        ".boxedarticle--details--price"]:
                el = await page.query_selector(sel)
                if el:
                    txt = (await el.inner_text()).strip()
                    if txt:
                        parts.append("Preis: " + txt)
                        break

            for sel in ["#viewad-locality", "#viewad-address"]:
                el = await page.query_selector(sel)
                if el:
                    txt = (await el.inner_text()).strip()
                    if txt:
                        parts.append("Adresse: " + txt)
                        break

            for sel in ["#viewad-details", ".boxedarticle--details",
                        "[data-testid='ad-detail-attributes']"]:
                el = await page.query_selector(sel)
                if el:
                    txt = (await el.inner_text()).strip()
                    if txt:
                        parts.append(txt)
                        break

            for sel in ["#viewad-description-text", "#viewad-description"]:
                el = await page.query_selector(sel)
                if el:
                    txt = (await el.inner_text()).strip()
                    if txt and len(txt) > 20:
                        parts.append(txt[:1200])
                        break

            combined = "\n\n".join(p for p in parts if p).strip()
            if combined:
                log.info("   Detail: %d Z. (%s)", len(combined), url[35:75])
                return combined[:2500]
            return (await page.inner_text("body"))[:1500]

        except Exception as e:
            log.warning("   Detail timeout/fehler (%s): %s", url[35:75], e)
            return ""
        finally:
            await page.close()


# ─── Gemini (sync, runs in thread pool) ──────────────────────────────────────

def _gemini_call_sync(title: str, raw_text: str,
                      listing_type_hint: str, cfg: dict) -> Optional[dict]:
    global _gemini_key_invalid
    if _gemini_key_invalid or not time_ok():
        return None
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY fehlt!")
        _gemini_key_invalid = True
        return None

    city      = cfg.get("target_city", "Mannheim")
    boni_keys = list(cfg.get("rent_bonuses", {}).keys())
    all_pois  = [cfg.get("default_poi", {})] + cfg.get("alternative_pois", [])
    poi_hints = ", ".join(
        "%s=%.4f/%.4f" % (p["name"], p["latitude"], p["longitude"])
        for p in all_pois[:4] if p.get("latitude")
    )

    prompt = (
        "Analysiere diese Immobilienanzeige aus " + city + ". "
        "Extrahiere alle Zahlen exakt. "
        "Antworte NUR mit validem JSON - kein Markdown.\n\n"
        "Typ: " + listing_type_hint + "\n"
        "Titel: " + title[:250] + "\n"
        "Text:\n" + raw_text[:2000] + "\n\n"
        "GPS-Referenzen " + city + ": " + poi_hints + "\n\n"
        "JSON (Zahlen rein, z.B. 285000 nicht 285.000):\n"
        '{"listing_type":"miete" oder "kauf",'
        '"zimmeranzahl":Zahl oder null,'
        '"quadratmeter":Zahl oder null,'
        '"kaltmiete":EUR/Mo oder null,'
        '"nebenkosten":EUR/Mo oder null,'
        '"warmmiete":EUR/Mo oder null,'
        '"kaufpreis":EUR oder null,'
        '"adresse":"Strasse/Stadtteil" oder null,'
        '"stadtteil":"' + city + ' Stadtteil" oder null,'
        '"quadrat":"Mannheimer Quadrat z.B. J2" oder null,'
        '"latitude":GPS-Lat geschaetzt oder null,'
        '"longitude":GPS-Lon geschaetzt oder null,'
        '"boni":' + str(boni_keys) + ' nur positiv bestaetigte,'
        '"kurz_bewertung":"1 Satz auf Deutsch"'
        "}"
    )

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        log.error("Gemini init fehler: %s", e)
        return None

    for attempt in range(2):
        try:
            resp     = client.models.generate_content(model="gemini-1.5-flash", contents=prompt)
            raw_resp = re.sub(r"```(?:json)?\s*|\s*```", "", resp.text.strip()).strip()
            m        = re.search(r"\{[\s\S]*\}", raw_resp)
            if not m:
                continue

            result = json.loads(m.group())

            for field in ("kaufpreis", "kaltmiete", "nebenkosten", "warmmiete",
                          "quadratmeter", "zimmeranzahl", "latitude", "longitude"):
                val = result.get(field)
                if isinstance(val, str):
                    cleaned = re.sub(r"[^\d,.]", "", val)
                    if cleaned.count(".") > 1:
                        cleaned = cleaned.replace(".", "", cleaned.count(".") - 1)
                    cleaned = cleaned.replace(",", ".")
                    try:
                        result[field] = float(cleaned) if cleaned else None
                    except ValueError:
                        result[field] = None

            if not valid_mannheim_coords(result.get("latitude"), result.get("longitude")):
                result["latitude"]  = None
                result["longitude"] = None

            log.info("   Gemini: typ=%s kauf=%s miete=%s qm=%s lat=%s",
                     result.get("listing_type"), result.get("kaufpreis"),
                     result.get("kaltmiete"), result.get("quadratmeter"),
                     "%.4f" % result["latitude"] if result.get("latitude") else "null")
            return result

        except json.JSONDecodeError:
            log.warning("   JSON-Fehler Versuch %d", attempt + 1)
        except Exception as e:
            err = str(e).lower()
            if any(x in err for x in ("quota", "rate", "429", "resource_exhausted")):
                log.warning("   Rate-Limit 30s ...")
                time.sleep(30)
            elif any(x in err for x in ("api_key", "invalid_argument", "unauthenticated",
                                        "api_key_invalid", "permission_denied")):
                log.error("   API-Key ungueltig: %s", e)
                _gemini_key_invalid = True
                return None
            else:
                log.error("   Gemini Versuch %d: %s", attempt + 1, e)
    return None


async def analyse_mit_gemini_async(title: str, raw_text: str,
                                    listing_type_hint: str, cfg: dict,
                                    sem: asyncio.Semaphore) -> Optional[dict]:
    """Non-blocking Gemini call: runs sync function in thread pool."""
    async with sem:
        return await asyncio.to_thread(
            _gemini_call_sync, title, raw_text, listing_type_hint, cfg)


# ─── Main scraper logic ───────────────────────────────────────────────────────

async def scrape_kleinanzeigen_async(ctx: BrowserContext, source: dict,
                                      existing_urls: set, cfg: dict) -> list:
    raw      = []
    base_url = source["url"]
    lst_type = source["listing_type"]

    # Phase 1: Collect candidate URLs (sequential search pages)
    search_page = await ctx.new_page()
    try:
        for page_num in range(1, source.get("pages", 1) + 1):
            if len(raw) >= MAX_NEW_PER_RUN or not time_ok():
                break
            url = base_url if page_num == 1 else base_url + "?pageNum=" + str(page_num)
            log.info("   Seite %d: %s", page_num, url[-50:])
            try:
                await search_page.goto(url, wait_until="domcontentloaded",
                                       timeout=PAGE_TIMEOUT_MS)
                await search_page.wait_for_timeout(800)
                items = await search_page.query_selector_all(
                    "article.aditem, li.ad-listitem[data-adid]")
                if not items:
                    log.info("   Keine Eintraege")
                    break
                found = 0
                for item in items:
                    if len(raw) >= MAX_NEW_PER_RUN:
                        break
                    try:
                        link_el = await item.query_selector(
                            "a[href*='/s-anzeige/'], a.ellipsis, h2 a")
                        href = (await link_el.get_attribute("href") or "") if link_el else ""
                        if not href:
                            continue
                        if href.startswith("/"):
                            href = "https://www.kleinanzeigen.de" + href
                        if href in existing_urls:
                            continue
                        title_el = await item.query_selector(
                            "h2, .ellipsis, .aditem-main--middle--headline a")
                        title = ((await title_el.inner_text()).strip()
                                 if title_el else "").strip()
                        if not title or NON_MANNHEIM_RE.search(title):
                            continue
                        raw.append({"url": href, "source": "kleinanzeigen",
                                    "listing_type": lst_type, "title": title[:400]})
                        existing_urls.add(href)
                        found += 1
                    except Exception:
                        pass
                log.info("   %d Kandidaten auf Seite %d", found, page_num)
                if found == 0:
                    break
            except Exception as e:
                log.error("   Seitenfehler: %s", e)
                break
    finally:
        await search_page.close()

    if not raw:
        return []

    # Phase 2: Parallel detail scraping (DETAIL_WORKERS concurrent pages)
    log.info("   %d Detailseiten parallel (%d gleichzeitig)...", len(raw), DETAIL_WORKERS)
    sem = asyncio.Semaphore(DETAIL_WORKERS)
    detail_tasks = [scrape_detail_async(ctx, entry["url"], sem) for entry in raw]
    detail_results = await asyncio.gather(*detail_tasks, return_exceptions=True)

    verified = []
    for entry, raw_text in zip(raw, detail_results):
        if isinstance(raw_text, Exception):
            raw_text = ""
        entry["raw_text"] = str(raw_text) if raw_text else ""
        if not is_mannheim(entry["title"], entry["raw_text"]):
            log.info("   SKIP (kein Mannheim): %s", entry["title"][:55])
            continue
        verified.append(entry)

    log.info("   %d/%d nach Mannheim-Filter", len(verified), len(raw))
    return verified


# ─── Entry point ──────────────────────────────────────────────────────────────

async def main() -> None:
    global _start_time
    _start_time = time.time()

    log.info("=" * 55)
    log.info("Immobilien-Sniper v3.0 (parallel async)")
    log.info("Zeit: %s UTC | MaxRun: %ds | Details: %dx | Gemini: %dx",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             MAX_RUNTIME_SECS, DETAIL_WORKERS, GEMINI_WORKERS)
    log.info("API-Key: %s", "OK" if GEMINI_API_KEY else "FEHLT!")
    log.info("=" * 55)

    cfg  = load_config()
    data = load_deals()
    existing_urls = get_existing_urls(data)
    log.info("Bestehende Deals: %d", len(existing_urls))

    all_raw = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                  "--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="de-DE",
            extra_http_headers={"Accept-Language": "de-DE,de;q=0.9"},
        )

        for source in cfg.get("sources", []):
            if not source.get("enabled", True) or not time_ok():
                continue
            log.info("\nQuelle: %s", source["name"])
            try:
                results = await scrape_kleinanzeigen_async(ctx, source, existing_urls, cfg)
                log.info("%d verifiziert von %s", len(results), source["name"])
                all_raw.extend(results)
                if len(all_raw) >= MAX_NEW_PER_RUN:
                    break
            except Exception as e:
                log.error("Quelle %s fehler: %s", source["name"], e)

        await browser.close()

    all_raw = all_raw[:MAX_NEW_PER_RUN]
    log.info("\nGemini: %d Inserate parallel analysieren...", len(all_raw))
    now_iso = datetime.now(timezone.utc).isoformat()
    ai_ok = ai_fail = 0

    if all_raw and not _gemini_key_invalid and time_ok():
        # Parallel Gemini calls (semaphore-limited to GEMINI_WORKERS)
        gemini_sem = asyncio.Semaphore(GEMINI_WORKERS)
        gemini_tasks = [
            analyse_mit_gemini_async(
                r["title"], r.get("raw_text", ""), r["listing_type"], cfg, gemini_sem)
            for r in all_raw
        ]
        log.info("   Starte %d parallele Gemini-Calls (max %d gleichzeitig)...",
                 len(gemini_tasks), GEMINI_WORKERS)
        ai_results = await asyncio.gather(*gemini_tasks, return_exceptions=True)
    else:
        ai_results = [None] * len(all_raw)

    for i, (raw, ai) in enumerate(zip(all_raw, ai_results), 1):
        if isinstance(ai, Exception):
            log.warning("   Gemini Exception [%d]: %s", i, ai)
            ai = None
            ai_fail += 1
        elif ai is not None:
            ai_ok += 1
        else:
            ai_fail += 1

        log.info("[%2d/%d] %s", i, len(all_raw), raw["title"][:60])
        deal = build_deal(raw, ai, now_iso, cfg)
        data["deals"].append(deal)

        if i % 5 == 0:
            save_deals(data)
            log.info("   [Zwischenspeicherung nach %d Deals]", len(data["deals"]))

    elapsed = int(time.time() - _start_time)
    log.info("=" * 55)
    log.info("Fertig in %ds | neu=%d KI ok=%d fail=%d | gesamt=%d",
             elapsed, len(all_raw), ai_ok, ai_fail, len(data["deals"]))
    save_deals(data)


if __name__ == "__main__":
    asyncio.run(main())
