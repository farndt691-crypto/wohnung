"""
scraper.py - Immobilien-Sniper v3.3
60 deals/run: 30 miete + 30 kauf, 4 pages each, parallel async.
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

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
CONFIG_PATH = Path("config.json")
DEALS_PATH  = Path("data/deals.json")
MAX_NEW_PER_RUN      = 60   # total per run (30 miete + 30 kauf)
MAX_PER_SOURCE       = 30   # max candidates per source
PAGE_TIMEOUT_MS      = 12000
DETAIL_WAIT_MS       = 500
MAX_RUNTIME_SECS     = 2400  # 40 min hard limit
DETAIL_WORKERS       = 3
GEMINI_MIN_INTERVAL  = 4.5   # ~13 RPM (free tier: 15 RPM)

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
_gemini_last_call = 0.0
_gemini_rate_lock = None


def load_config():
    if not CONFIG_PATH.exists():
        log.error("config.json nicht gefunden!")
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    log.info("Config: Stadt=%s minZimmer=%s", cfg.get("target_city"), cfg.get("min_rooms"))
    return cfg


def load_deals():
    DEALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DEALS_PATH.exists():
        try:
            with open(DEALS_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error("deals.json fehler: %s", e)
    return {"last_updated": None, "deals": []}


def save_deals(data):
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(DEALS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info("Gespeichert: %d Deals", len(data["deals"]))


def get_existing_urls(data):
    return {d["url"] for d in data.get("deals", [])}


def time_ok():
    return (time.time() - _start_time) < MAX_RUNTIME_SECS


def is_mannheim(title, raw_text):
    combined = title + " " + raw_text
    if NON_MANNHEIM_RE.search(combined):
        return False
    return bool(MANNHEIM_RE.search(combined))


def valid_mannheim_coords(lat, lon):
    if lat is None or lon is None:
        return False
    try:
        return MHM_LAT_MIN <= float(lat) <= MHM_LAT_MAX and MHM_LON_MIN <= float(lon) <= MHM_LON_MAX
    except (TypeError, ValueError):
        return False


def berechne_scores(deal, cfg):
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


def build_deal(raw, ai, now_iso, cfg):
    deal = {
        "url":            raw["url"],
        "source":         raw["source"],
        "listing_type":   raw["listing_type"],
        "title":          raw["title"],
        "first_seen":     now_iso,
        "last_seen":      now_iso,
        "boni":           [],
        "kurz_bewertung": "",
        "zimmeranzahl":   None,
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


async def scrape_detail_async(ctx, url, sem):
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
            log.warning("   Detail fehler (%s): %s", url[35:75], e)
            return ""
        finally:
            await page.close()


def _gemini_call_sync(title, raw_text, listing_type_hint, cfg):
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
            resp     = client.models.generate_content(model="gemini-2.5-flash-lite", contents=prompt)
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

            log.info("   Gemini OK: typ=%s miete=%s kauf=%s qm=%s zi=%s",
                     result.get("listing_type"), result.get("kaltmiete"),
                     result.get("kaufpreis"), result.get("quadratmeter"),
                     result.get("zimmeranzahl"))
            return result

        except json.JSONDecodeError:
            log.warning("   JSON-Fehler Versuch %d", attempt + 1)
        except Exception as e:
            err = str(e).lower()
            if any(x in err for x in ("not found", "404", "not supported", "deprecated")):
                log.error("   Modell nicht gefunden: %s", e)
                return None
            elif any(x in err for x in ("api_key", "invalid_argument", "unauthenticated",
                                        "api_key_invalid", "permission_denied")):
                log.error("   API-Key ungueltig: %s", e)
                _gemini_key_invalid = True
                return None
            else:
                log.error("   Gemini Versuch %d: %s", attempt + 1, e)
    return None


async def analyse_mit_gemini_async(title, raw_text, listing_type_hint, cfg):
    global _gemini_last_call
    async with _gemini_rate_lock:
        now = asyncio.get_event_loop().time()
        wait = _gemini_last_call + GEMINI_MIN_INTERVAL - now
        if wait > 0:
            await asyncio.sleep(wait)
        _gemini_last_call = asyncio.get_event_loop().time()
    return await asyncio.to_thread(_gemini_call_sync, title, raw_text, listing_type_hint, cfg)


async def scrape_kleinanzeigen_async(ctx, source, existing_urls, cfg):
    raw      = []
    base_url = source["url"]
    lst_type = source["listing_type"]

    search_page = await ctx.new_page()
    try:
        for page_num in range(1, source.get("pages", 1) + 1):
            if len(raw) >= MAX_PER_SOURCE or not time_ok():
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
                    if len(raw) >= MAX_PER_SOURCE:
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
                        # Capture price + snippet from search card as fallback
                        card_parts = []
                        for psel in [".aditem-main--middle--price-shipping",
                                     ".price-tile__price", "[data-testid='price']",
                                     ".aditem-main--bottom"]:
                            pel = await item.query_selector(psel)
                            if pel:
                                pt = (await pel.inner_text()).strip()
                                if pt:
                                    card_parts.append(pt)
                                    break
                        for dsel in [".aditem-main--middle--description",
                                     ".text-module-begin"]:
                            del_ = await item.query_selector(dsel)
                            if del_:
                                dt = (await del_.inner_text()).strip()
                                if dt:
                                    card_parts.append(dt)
                                    break
                        card_text = " | ".join(card_parts)
                        raw.append({"url": href, "source": "kleinanzeigen",
                                    "listing_type": lst_type, "title": title[:400],
                                    "card_text": card_text})
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

    log.info("   %d Detailseiten parallel (%dx)...", len(raw), DETAIL_WORKERS)
    det_sem = asyncio.Semaphore(DETAIL_WORKERS)
    detail_results = await asyncio.gather(
        *[scrape_detail_async(ctx, e["url"], det_sem) for e in raw],
        return_exceptions=True
    )

    verified = []
    for entry, raw_text in zip(raw, detail_results):
        if isinstance(raw_text, Exception):
            raw_text = ""
        detail = str(raw_text) if raw_text else ""
        card   = entry.get("card_text", "")
        if not detail:
            log.info("   Detail leer - nutze Karte: %s", entry["title"][:50])
        # Combine detail + card text for Gemini (detail first, card as supplement)
        entry["raw_text"] = (detail + "\n\n" + card).strip() if detail else card
        if not is_mannheim(entry["title"], entry["raw_text"] + " " + card):
            log.info("   SKIP (kein Mannheim): %s", entry["title"][:55])
            continue
        verified.append(entry)

    log.info("   %d/%d nach Mannheim-Filter", len(verified), len(raw))
    return verified


async def main():
    global _start_time, _gemini_rate_lock
    _start_time = time.time()
    _gemini_rate_lock = asyncio.Lock()

    log.info("=" * 58)
    log.info("Immobilien-Sniper v3.3 | MAX=%d | PRO_QUELLE=%d | PAGES=4",
             MAX_NEW_PER_RUN, MAX_PER_SOURCE)
    log.info("MaxRun=%ds | Details=%dx | Gemini=%.1fs/Call",
             MAX_RUNTIME_SECS, DETAIL_WORKERS, GEMINI_MIN_INTERVAL)
    log.info("API-Key: %s", "OK" if GEMINI_API_KEY else "FEHLT!")
    log.info("=" * 58)

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