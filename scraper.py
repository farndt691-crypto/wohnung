"""
scraper.py - Immobilien-Sniper v2
Config-driven, GPS extraction, strict Mannheim filter, modular scoring.
"""

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
from playwright.sync_api import Page, sync_playwright

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")
CONFIG_PATH = Path("config.json")
DEALS_PATH  = Path("data/deals.json")
MAX_NEW_PER_RUN = 20
PAGE_TIMEOUT_MS = 20000
DETAIL_WAIT_MS  = 1000

# Mannheim bounding box for coordinate validation
MHM_LAT_MIN, MHM_LAT_MAX = 49.40, 49.60
MHM_LON_MIN, MHM_LON_MAX = 8.35,  8.65

MANNHEIM_RE = re.compile(r'\bMannheim\b', re.IGNORECASE)
NON_MANNHEIM_RE = re.compile(
    r'\b(Hamburg|Berlin|Muenchen|Frankfurt am Main|Stuttgart|Koeln|Duesseldorf|'
    r'Dortmund|Essen|Leipzig|Dresden|Hannover|Nuernberg|Bremen|Duisburg|'
    r'Bochum|Wuppertal|Bielefeld|Bonn|Muenster|Freiburg|HafenCity|'
    r'Buxtehude|Harburg|Altona|Wandsbek|Eilbek|Barmbek|Bergedorf|'
    r'Pankow|Kreuzberg|Friedrichshain|Prenzlauer|Schoeneberg|'
    r'Karlsruhe|Heidelberg|Ludwigshafen)\b',
    re.IGNORECASE,
)

_gemini_key_invalid = False


# ---------------------------------------------------------------------------
# Config & Data I/O
# ---------------------------------------------------------------------------
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.error("config.json nicht gefunden!")
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    log.info("Config geladen: Stadt=%s Radius=%skm minZimmer=%s",
             cfg.get("target_city"), cfg.get("search_radius_km"), cfg.get("min_rooms"))
    return cfg


def load_deals() -> dict:
    DEALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DEALS_PATH.exists():
        try:
            with open(DEALS_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error("deals.json laden fehlgeschlagen: %s", e)
    return {"last_updated": None, "deals": []}


def save_deals(data: dict) -> None:
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(DEALS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info("Gespeichert: %d Deals -> %s", len(data["deals"]), DEALS_PATH)


def get_existing_urls(data: dict) -> set:
    return {d["url"] for d in data.get("deals", [])}


# ---------------------------------------------------------------------------
# Mannheim filter
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Deal scoring (fully config-driven)
# ---------------------------------------------------------------------------
def berechne_scores(deal: dict, cfg: dict) -> dict:
    fin  = cfg.get("financing", {})
    base = cfg.get("base_rent_per_sqm", 14.50)
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
            rate = fin.get("interest_rate", 0.035) + fin.get("repayment_rate", 0.015)
            bankrate = round((kaufpreis * fin.get("overhead_factor", 1.10)) * rate / 12, 2)
            cashflow = round(kaltmiete - bankrate - fin.get("maintenance_monthly", 50), 2)
            deal["bankrate_monat"] = bankrate
            deal["cashflow_monat"] = cashflow
            deal["deal_score"] = (
                "strong_buy" if cashflow >= 0 else
                "watch"      if cashflow >= -150 else
                "skip"
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
            deviation = round(((kaltmiete_qm - expected_qm) / expected_qm) * 100, 1)
            deal["deal_deviation_pct"] = deviation
            deal["deal_score"] = (
                "gut"   if deviation <= -10 else
                "okay"  if deviation <=  10 else
                "teuer"
            )
            log.info("   Miete: %.0f EUR | %.2f EUR/m2 | abw=%+.1f%% -> %s",
                     kaltmiete, kaltmiete_qm, deviation, deal["deal_score"])
        else:
            deal.setdefault("deal_score", "okay")

    return deal


# ---------------------------------------------------------------------------
# Detail-page scraper
# ---------------------------------------------------------------------------
def scrape_detail(page: Page, url: str) -> str:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        page.wait_for_timeout(DETAIL_WAIT_MS)
        parts = []

        for sel in ["#viewad-price", "[data-testid='price-amount']",
                    ".boxedarticle--details--price"]:
            el = page.query_selector(sel)
            if el:
                txt = el.inner_text().strip()
                if txt:
                    parts.append("Preis: " + txt)
                    break

        for sel in ["#viewad-locality", "#viewad-address",
                    ".addetailsview--detail--address"]:
            el = page.query_selector(sel)
            if el:
                txt = el.inner_text().strip()
                if txt:
                    parts.append("Adresse: " + txt)
                    break

        for sel in ["#viewad-details", ".boxedarticle--details",
                    "[data-testid='ad-detail-attributes']"]:
            el = page.query_selector(sel)
            if el:
                txt = el.inner_text().strip()
                if txt:
                    parts.append(txt)
                    break

        for sel in ["#viewad-description-text", "#viewad-description"]:
            el = page.query_selector(sel)
            if el:
                txt = el.inner_text().strip()
                if txt and len(txt) > 20:
                    parts.append(txt[:1500])
                    break

        combined = "\n\n".join(p for p in parts if p).strip()
        if combined:
            log.info("   Detail: %d Zeichen (%s)", len(combined), url[:60])
            return combined[:3000]

        body = page.inner_text("body")
        log.warning("   Fallback body: %d Zeichen", len(body))
        return body[:2000]

    except Exception as e:
        log.error("   Detail-Fehler (%s): %s", url[:60], e)
        return ""


# ---------------------------------------------------------------------------
# Gemini AI extraction
# ---------------------------------------------------------------------------
def analyse_mit_gemini(title: str, raw_text: str,
                       listing_type_hint: str, cfg: dict) -> Optional[dict]:
    global _gemini_key_invalid
    if _gemini_key_invalid:
        return None
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY fehlt!")
        _gemini_key_invalid = True
        return None

    city = cfg.get("target_city", "Mannheim")
    boni_keys = list(cfg.get("rent_bonuses", {}).keys())

    # GPS reference hints for the city
    poi_hints = city + " GPS-Referenzen: "
    all_pois = [cfg.get("default_poi", {})] + cfg.get("alternative_pois", [])
    poi_parts = []
    for p in all_pois:
        if p.get("name") and p.get("latitude"):
            poi_parts.append("%s=%.4f/%.4f" % (p["name"], p["latitude"], p["longitude"]))
    poi_hints += ", ".join(poi_parts[:5])

    prompt = (
        "Analysiere diese Immobilienanzeige aus " + city + ". "
        "Extrahiere alle Zahlen exakt aus dem Text.\n"
        "Antworte NUR mit einem gueltigen JSON-Objekt - kein Markdown, kein Kommentar.\n\n"
        "Typ-Hinweis: " + listing_type_hint + "\n"
        "Titel: " + title[:300] + "\n"
        "Text:\n" + raw_text[:2500] + "\n\n"
        + poi_hints + "\n\n"
        "JSON-Format (Zahlen als reine Zahl, z.B. 285000 nicht 285.000):\n"
        "{\n"
        '  "listing_type": "miete" oder "kauf",\n'
        '  "zimmeranzahl": Zimmer z.B. 2.5 oder null,\n'
        '  "quadratmeter": Wohnflaeche m2 als Zahl oder null,\n'
        '  "kaltmiete": EUR/Monat als Zahl oder null,\n'
        '  "nebenkosten": EUR/Monat als Zahl oder null,\n'
        '  "warmmiete": EUR/Monat als Zahl oder null,\n'
        '  "kaufpreis": EUR als reine Zahl oder null,\n'
        '  "adresse": "Strassenname oder Stadtteil" oder null,\n'
        '  "stadtteil": "' + city + ' Stadtteil z.B. Lindenhof" oder null,\n'
        '  "quadrat": "Mannheimer Quadrat z.B. J2" oder null,\n'
        '  "latitude": GPS-Breitengrad als Dezimalzahl (schaetze anhand Adresse/Stadtteil) oder null,\n'
        '  "longitude": GPS-Laengengrad als Dezimalzahl (schaetze anhand Adresse/Stadtteil) oder null,\n'
        '  "boni": Array aus ' + str(boni_keys) + ' - NUR wenn im Text POSITIV erwaehnt,\n'
        '  "kurz_bewertung": "1-2 Saetze Einschaetzung auf Deutsch"\n'
        "}"
    )

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        log.error("Gemini init fehlgeschlagen: %s", e)
        return None

    for attempt in range(2):
        try:
            resp = client.models.generate_content(model="gemini-1.5-flash", contents=prompt)
            raw_resp = re.sub(r"```(?:json)?\s*|\s*```", "", resp.text.strip()).strip()
            m = re.search(r"\{[\s\S]*\}", raw_resp)
            if not m:
                log.warning("   Kein JSON (Versuch %d)", attempt + 1)
                continue

            result = json.loads(m.group())

            # Coerce numeric strings to float
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

            # Validate coordinates are inside Mannheim bounding box
            if not valid_mannheim_coords(result.get("latitude"), result.get("longitude")):
                result["latitude"]  = None
                result["longitude"] = None

            log.info("   Gemini OK: typ=%s kauf=%s miete=%s qm=%s zi=%s lat=%.4f lon=%.4f",
                     result.get("listing_type"),
                     result.get("kaufpreis"),
                     result.get("kaltmiete"),
                     result.get("quadratmeter"),
                     result.get("zimmeranzahl"),
                     result.get("latitude")  or 0,
                     result.get("longitude") or 0)
            return result

        except json.JSONDecodeError as e:
            log.warning("   JSON-Fehler (Versuch %d): %s", attempt + 1, e)
        except Exception as e:
            err = str(e).lower()
            if any(x in err for x in ("quota", "rate", "429", "resource_exhausted")):
                log.warning("   Rate-Limit - warte 60s ...")
                time.sleep(60)
            elif any(x in err for x in ("api_key", "invalid_argument", "unauthenticated",
                                        "api_key_invalid", "permission_denied",
                                        "invalid api key", "api key not valid")):
                log.error("   GEMINI_API_KEY ungueltig: %s", e)
                _gemini_key_invalid = True
                return None
            else:
                log.error("   Gemini-Fehler (Versuch %d): %s", attempt + 1, e)

    return None


# ---------------------------------------------------------------------------
# Kleinanzeigen scraper
# ---------------------------------------------------------------------------
def scrape_kleinanzeigen(search_page: Page, detail_page: Page,
                         source: dict, existing_urls: set, cfg: dict) -> list:
    raw       = []
    base_url  = source["url"]
    list_type = source["listing_type"]
    min_rooms = cfg.get("min_rooms", 1.0)

    for page_num in range(1, source.get("pages", 2) + 1):
        if len(raw) >= MAX_NEW_PER_RUN:
            break
        url = base_url if page_num == 1 else base_url + "?pageNum=" + str(page_num)
        log.info("   Seite %d: %s", page_num, url[:80])
        try:
            search_page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            search_page.wait_for_timeout(1200)
            items = search_page.query_selector_all("article.aditem, li.ad-listitem[data-adid]")
            if not items:
                log.info("   Keine Eintraege - stoppe")
                break
            found = 0
            for item in items:
                try:
                    link_el = item.query_selector("a[href*='/s-anzeige/'], a.ellipsis, h2 a")
                    href = (link_el.get_attribute("href") or "") if link_el else ""
                    if not href:
                        continue
                    if href.startswith("/"):
                        href = "https://www.kleinanzeigen.de" + href
                    if href in existing_urls:
                        continue
                    title_el = item.query_selector("h2, .ellipsis, .aditem-main--middle--headline a")
                    title = (title_el.inner_text().strip() if title_el else "").strip()
                    if not title:
                        continue
                    if NON_MANNHEIM_RE.search(title):
                        log.info("   Skip (Titel nicht Mannheim): %s", title[:60])
                        continue
                    raw.append({"url": href, "source": "kleinanzeigen",
                                "listing_type": list_type, "title": title[:400]})
                    existing_urls.add(href)
                    found += 1
                except Exception as e:
                    log.debug("   Item-Fehler: %s", e)
            log.info("   %d Kandidaten auf Seite %d", found, page_num)
            if found == 0:
                break
        except Exception as e:
            log.error("   Seitenfehler: %s", e)
            break

    # Scrape details + verify Mannheim
    log.info("   Scrape %d Detailseiten ...", len(raw))
    verified = []
    for entry in raw:
        entry["raw_text"] = scrape_detail(detail_page, entry["url"])
        if not is_mannheim(entry["title"], entry["raw_text"]):
            log.info("   SKIP (kein Mannheim): %s", entry["title"][:60])
            continue
        verified.append(entry)
        time.sleep(0.8)

    log.info("   %d/%d nach Mannheim-Filter", len(verified), len(raw))
    return verified


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run() -> None:
    log.info("=" * 60)
    log.info("Immobilien-Sniper v2 gestartet")
    log.info("Zeit: %s UTC", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("GEMINI_API_KEY: %s", "gesetzt" if GEMINI_API_KEY else "FEHLT!")
    log.info("=" * 60)

    cfg  = load_config()
    data = load_deals()
    existing_urls = get_existing_urls(data)
    log.info("Bestehende Deals: %d", len(existing_urls))

    all_raw = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                  "--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="de-DE",
            extra_http_headers={"Accept-Language": "de-DE,de;q=0.9"},
        )
        search_page = ctx.new_page()
        detail_page = ctx.new_page()

        for source in cfg.get("sources", []):
            if not source.get("enabled", True):
                continue
            log.info("\nQuelle: %s", source["name"])
            try:
                results = scrape_kleinanzeigen(
                    search_page, detail_page, source, existing_urls, cfg)
                log.info("%d verifizierte Inserate von %s", len(results), source["name"])
                all_raw.extend(results)
                if len(all_raw) >= MAX_NEW_PER_RUN:
                    break
            except Exception as e:
                log.error("Quelle %s fehlgeschlagen: %s", source["name"], e)

        browser.close()

    all_raw = all_raw[:MAX_NEW_PER_RUN]
    log.info("\nGemini analysiert %d Inserate ...", len(all_raw))
    now_iso = datetime.now(timezone.utc).isoformat()
    ai_ok = ai_fail = 0

    for i, raw in enumerate(all_raw, 1):
        log.info("[%2d/%d] %s", i, len(all_raw), raw["title"][:65])

        ai = None
        if not _gemini_key_invalid:
            ai = analyse_mit_gemini(
                raw["title"], raw.get("raw_text", ""), raw["listing_type"], cfg)
            if ai:
                ai_ok += 1
            else:
                ai_fail += 1
        else:
            ai_fail += 1

        deal = {
            "url":          raw["url"],
            "source":       raw["source"],
            "listing_type": raw["listing_type"],
            "title":        raw["title"],
            "first_seen":   now_iso,
            "last_seen":    now_iso,
            "boni":         [],
            "kurz_bewertung": "",
            "zimmeranzahl": cfg.get("min_rooms", 2.5),
            "latitude":     None,
            "longitude":    None,
            "stadtteil":    None,
            "adresse":      None,
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

        deal = berechne_scores(deal, cfg)
        data["deals"].append(deal)
        time.sleep(2)

    log.info("=" * 60)
    log.info("Fertig: %d neue | KI ok=%d fail=%d | Gesamt=%d",
             len(all_raw), ai_ok, ai_fail, len(data["deals"]))
    save_deals(data)


if __name__ == "__main__":
    run()
