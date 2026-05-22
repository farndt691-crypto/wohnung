"""
scraper.py - Immobilien-Sniper v3.6
60 deals/run: 30 miete + 30 kauf, 4 pages each, parallel async.
Regex → Preise (kein Quota). Groq/Llama3 → GPS, Umfeld-Analyse, Enrichment.
Groq Free-Tier: ~14.400 Req/Tag, 30 RPM – kein Limit-Problem mehr.
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

from groq import Groq
from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

GROQ_API_KEY  = os.environ.get("GROQ_API_KEY", "")
CONFIG_PATH   = Path("config.json")
DEALS_PATH    = Path("data/deals.json")
MAX_NEW_PER_RUN      = 60
MAX_PER_SOURCE       = 30
PAGE_TIMEOUT_MS      = 12000
DETAIL_WAIT_MS       = 500
MAX_RUNTIME_SECS     = 2400
DETAIL_WORKERS       = 3
GROQ_SLEEP_SECS      = 2.0   # safety buffer under 30 RPM free-tier limit

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

_groq_key_invalid = False
_start_time = None
_groq_lock  = None   # serialises Groq calls to respect RPM limit


# ── Config / Data helpers ────────────────────────────────────────────────────

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


# ── Scoring ──────────────────────────────────────────────────────────────────

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
        "umfeld_analyse": None,
    }
    if ai:
        for k, v in ai.items():
            if k == "umfeld_analyse":
                if isinstance(v, dict) and any(v.values()):
                    deal[k] = v
            elif k in ("boni", "kurz_bewertung"):
                if v:
                    deal[k] = v
            elif v is not None:
                deal[k] = v
        if ai.get("listing_type") in ("kauf", "miete"):
            deal["listing_type"] = ai["listing_type"]
    return berechne_scores(deal, cfg)


# ── Detail scraper ───────────────────────────────────────────────────────────

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


# ── Regex extraction (no API quota) ─────────────────────────────────────────

def extract_by_regex(title, raw_text, listing_type_hint, cfg):
    """Fast regex extraction for prices/rooms/sqm. No API quota needed."""
    text = (title + " " + raw_text).replace("\n", " ")
    result = {"listing_type": listing_type_hint}

    # Zimmer
    m = re.search(r'(\d[,.]?\d?)\s*[-–]?\s*Zimmer', text, re.IGNORECASE)
    if not m:
        m = re.search(r'(\d[,.]?\d?)\s*Zi\.', text, re.IGNORECASE)
    if m:
        try:
            result["zimmeranzahl"] = float(m.group(1).replace(",", "."))
        except ValueError:
            pass

    # Quadratmeter
    m = re.search(r'(\d{2,4}(?:[,\.]\d{1,2})?)\s*m[2²]', text)
    if m:
        try:
            result["quadratmeter"] = float(m.group(1).replace(",", "."))
        except ValueError:
            pass

    # Preise: "275.000 EUR", "275.000,00 €", "275000€"
    price_re = re.compile(
        r'(\d{2,3}(?:[.\s]\d{3})*(?:,\d{2})?)\s*(?:€|EUR|Euro)',
        re.IGNORECASE
    )
    prices = []
    for pm in price_re.finditer(text):
        raw_p = pm.group(1).replace(" ", "").replace(".", "").replace(",", ".")
        try:
            prices.append(float(raw_p))
        except ValueError:
            pass

    if listing_type_hint == "kauf":
        buy_prices = [p for p in prices if p > 30_000]
        if buy_prices:
            result["kaufpreis"] = max(buy_prices)
        rent_m = re.search(
            r'(?:Kalt|Warm|Miete)[^\d]{0,20}(\d{3,5})'
            r'|(\d{3,5})\s*(?:€|EUR)?\s*/\s*(?:Mon|Mo\.)',
            text, re.IGNORECASE
        )
        if rent_m:
            val = rent_m.group(1) or rent_m.group(2)
            try:
                result["kaltmiete"] = float(val)
            except ValueError:
                pass
    else:
        rent_prices = [p for p in prices if 100 < p < 8_000]
        if rent_prices:
            result["kaltmiete"] = min(rent_prices)

    # Stadtteil
    stadtteile = [
        "Lindenhof", "Neckarstadt", "Schwetzingerstadt", "Jungbusch",
        "Feudenheim", "Kaefertal", "Waldhof", "Sandhofen", "Rheinau",
        "Seckenheim", "Friedrichsfeld", "Vogelstang", "Hochstaett",
        "Oststadt", "Weststadt", "Innenstadt", "Almenhof",
    ]
    for st in stadtteile:
        if st.lower() in text.lower():
            result["stadtteil"] = "Mannheim " + st
            break

    # Boni
    boni_keys = list(cfg.get("rent_bonuses", {}).keys())
    found_boni = [b for b in boni_keys if b.lower() in text.lower()]
    if found_boni:
        result["boni"] = found_boni

    log.info("   Regex: typ=%s miete=%s kauf=%s qm=%s zi=%s",
             result.get("listing_type"), result.get("kaltmiete"),
             result.get("kaufpreis"), result.get("quadratmeter"),
             result.get("zimmeranzahl"))
    return result


# ── Groq/Llama3 (GPS + Umfeld-Analyse + Enrichment) ─────────────────────────

SYSTEM_PROMPT = (
    "Du bist ein JSON-Extraktions- und Analyse-Assistent für Immobilien. "
    "Antworte IMMER und AUSSCHLIESSLICH mit einem einzigen validen JSON-Objekt. "
    "KEIN Markdown, KEINE Code-Blöcke, KEINE Erklärungen, KEIN Text außerhalb des JSON. "
    "Zahlen als reine Integers/Floats ohne Punkte oder Kommas als Tausendertrenner "
    "(285000 nicht 285.000)."
)


def _groq_call_sync(title, raw_text, listing_type_hint, cfg):
    global _groq_key_invalid
    if _groq_key_invalid or not time_ok():
        return None
    if not GROQ_API_KEY:
        log.error("GROQ_API_KEY fehlt!")
        _groq_key_invalid = True
        return None

    city      = cfg.get("target_city", "Mannheim")
    boni_keys = list(cfg.get("rent_bonuses", {}).keys())
    all_pois  = [cfg.get("default_poi", {})] + cfg.get("alternative_pois", [])
    poi_hints = ", ".join(
        "%s=%.4f/%.4f" % (p["name"], p["latitude"], p["longitude"])
        for p in all_pois[:4] if p.get("latitude")
    )

    user_prompt = (
        "Analysiere diese Immobilienanzeige aus " + city + ".\n\n"
        "AUFGABE 1 – Zahlenextraktion: Lies alle Kennzahlen exakt aus dem Text.\n"
        "AUFGABE 2 – Umfeldanalyse: Nutze dein Weltwissen über den erkannten Stadtteil "
        "in " + city + ", um eine UNGESCHÖNTE und KRITISCHE Lageanalyse zu liefern. "
        "Sei realistisch – nicht beschönigend!\n\n"
        "Anzeigentyp: " + listing_type_hint + "\n"
        "Titel: " + title[:250] + "\n"
        "Anzeigentext:\n" + raw_text[:2000] + "\n\n"
        "GPS-Referenzpunkte " + city + ": " + poi_hints + "\n\n"
        "Gib exakt dieses JSON zurück (null wenn unbekannt):\n"
        "{\n"
        '  "listing_type": "' + listing_type_hint + '",\n'
        '  "zimmeranzahl": <Zahl oder null>,\n'
        '  "quadratmeter": <Zahl oder null>,\n'
        '  "kaltmiete": <EUR/Monat oder null>,\n'
        '  "nebenkosten": <EUR/Monat oder null>,\n'
        '  "warmmiete": <EUR/Monat oder null>,\n'
        '  "kaufpreis": <EUR oder null>,\n'
        '  "adresse": <"Straße Hausnummer" oder null>,\n'
        '  "stadtteil": <"' + city + ' Stadtteilname" oder null>,\n'
        '  "quadrat": <"Mannheimer Quadrat z.B. C3" oder null>,\n'
        '  "latitude": <GPS-Breitengrad geschätzt oder null>,\n'
        '  "longitude": <GPS-Längengrad geschätzt oder null>,\n'
        '  "boni": <Liste aus ' + json.dumps(boni_keys) + ' nur klar bestätigte>,\n'
        '  "kurz_bewertung": <"1 prägnanter Satz zur Immobilie auf Deutsch">,\n'
        '  "umfeld_analyse": {\n'
        '    "demografie": <"Wer wohnt hier hauptsächlich? Ungeschönt – z.B. Studentenviertel, Arbeiterklasse, Aufwertungsviertel, sozialer Brennpunkt">,\n'
        '    "vibe_sicherheit": <"Wie ist die Atmosphäre? z.B. Ruhig und bürgerlich / Lautes Ausgehviertel nachts unruhig / Eher meidungswürdig">,\n'
        '    "einschaetzung_student": <"Eignung für Informatikstudenten: Lernruhe, Anbindung Uni Mannheim/Heidelberg, ÖPNV – kurzes Fazit">,\n'
        '    "einschaetzung_investor": <"Eignung als Kapitalanlage: Mietnomaden-Risiko, Leerstand, Aufwertungspotenzial, Mieterzuverlässigkeit – kurzes Fazit">\n'
        '  }\n'
        "}"
    )

    try:
        client = Groq(api_key=GROQ_API_KEY)
        response = client.chat.completions.create(
            model="llama3-8b-8192",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=1024,
        )
        raw_resp = response.choices[0].message.content.strip()

        # Strip any accidental markdown fences
        raw_resp = re.sub(r"```(?:json)?\s*|\s*```", "", raw_resp).strip()
        m = re.search(r"\{[\s\S]*\}", raw_resp)
        if not m:
            log.warning("   Groq: kein JSON in Antwort")
            return None

        result = json.loads(m.group())

        # Sanitize numeric fields
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

        ua = result.get("umfeld_analyse")
        if not isinstance(ua, dict):
            result["umfeld_analyse"] = None

        log.info("   Groq OK: typ=%s kauf=%s miete=%s umfeld=%s",
                 result.get("listing_type"), result.get("kaufpreis"),
                 result.get("kaltmiete"),
                 "ja" if result.get("umfeld_analyse") else "nein")

        time.sleep(GROQ_SLEEP_SECS)  # stay well under 30 RPM
        return result

    except json.JSONDecodeError as e:
        log.warning("   Groq JSON-Fehler: %s", e)
    except Exception as e:
        err = str(e).lower()
        if any(x in err for x in ("api_key", "invalid_api_key", "authentication",
                                   "unauthorized", "403")):
            log.error("   Groq API-Key ungueltig: %s", e)
            _groq_key_invalid = True
        elif any(x in err for x in ("429", "rate_limit", "rate limit",
                                     "too many requests")):
            log.warning("   Groq Rate-Limit – 10s Pause")
            time.sleep(10)
        else:
            log.error("   Groq Fehler: %s", str(e)[:150])
    return None


async def analyse_async(title, raw_text, listing_type_hint, cfg):
    """Regex für Preise (kein Quota), Groq für Umfeldanalyse + GPS + Enrichment."""
    global _groq_lock

    # Step 1: Regex – blitzschnell, kein API-Limit
    regex_result = extract_by_regex(title, raw_text, listing_type_hint, cfg)

    # Step 2: Groq – serialisiert für RPM-Sicherheit, liefert umfeld_analyse
    if _groq_key_invalid or not GROQ_API_KEY or not time_ok():
        return regex_result

    async with _groq_lock:
        groq_result = await asyncio.to_thread(
            _groq_call_sync, title, raw_text, listing_type_hint, cfg
        )

    if groq_result:
        for k, v in groq_result.items():
            if k == "umfeld_analyse":
                if isinstance(v, dict) and any(v.values()):
                    regex_result[k] = v
            elif k == "boni":
                existing = set(regex_result.get(k) or [])
                merged   = list(existing | set(v or []))
                if merged:
                    regex_result[k] = merged
            elif v is not None and regex_result.get(k) is None:
                regex_result[k] = v

    return regex_result


# ── Kleinanzeigen scraper ────────────────────────────────────────────────────

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
        entry["raw_text"] = (detail + "\n\n" + card).strip() if detail else card
        if not is_mannheim(entry["title"], entry["raw_text"] + " " + card):
            log.info("   SKIP (kein Mannheim): %s", entry["title"][:55])
            continue
        verified.append(entry)

    log.info("   %d/%d nach Mannheim-Filter", len(verified), len(raw))
    return verified


# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    global _start_time, _groq_lock
    _start_time = time.time()
    _groq_lock  = asyncio.Lock()

    log.info("=" * 58)
    log.info("Immobilien-Sniper v3.6 | MAX=%d | PRO_QUELLE=%d | PAGES=4",
             MAX_NEW_PER_RUN, MAX_PER_SOURCE)
    log.info("MaxRun=%ds | Details=%dx | Groq sleep=%.1fs",
             MAX_RUNTIME_SECS, DETAIL_WORKERS, GROQ_SLEEP_SECS)
    log.info("Groq-Key: %s", "OK" if GROQ_API_KEY else "FEHLT!")
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
                if len(all_raw) >= MAX_NEW_PER_RUN:
                    break
                log.info("Pause 15s zwischen Quellen (Anti-Bot)...")
                await asyncio.sleep(15)
            except Exception as e:
                log.error("Quelle %s fehler: %s", source["name"], e)

        await browser.close()

    all_raw = all_raw[:MAX_NEW_PER_RUN]
    now_iso = datetime.now(timezone.utc).isoformat()
    ai_ok = ai_fail = 0

    log.info("\nAnalyse: %d Inserate (Regex + Groq/Llama3)...", len(all_raw))
    # Groq calls serialised via _groq_lock, but I/O and regex run concurrently
    ai_results = await asyncio.gather(
        *[analyse_async(
            r["title"], r.get("raw_text", ""), r["listing_type"], cfg)
          for r in all_raw],
        return_exceptions=True
    )

    for i, (raw_entry, ai) in enumerate(zip(all_raw, ai_results), 1):
        if isinstance(ai, Exception):
            ai = None
            ai_fail += 1
        elif ai is not None:
            ai_ok += 1
        else:
            ai_fail += 1
        log.info("[%2d/%d] %s", i, len(all_raw), raw_entry["title"][:60])
        deal = build_deal(raw_entry, ai, now_iso, cfg)
        data["deals"].append(deal)
        if i % 10 == 0:
            save_deals(data)

    elapsed = int(time.time() - _start_time)
    log.info("=" * 58)
    log.info("Fertig: %ds | neu=%d | AI ok=%d fail=%d | gesamt=%d",
             elapsed, len(all_raw), ai_ok, ai_fail, len(data["deals"]))
    save_deals(data)


if __name__ == "__main__":
    asyncio.run(main())
