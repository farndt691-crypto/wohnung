"""
scraper.py - Immobilien-Sniper v4.0
Quellen: Kleinanzeigen + ImmoScout24.
Regex → Preise (kein Quota). Groq/Llama3 → GPS, Umfeld-Analyse, Enrichment.
Groq Free-Tier: ~14.400 Req/Tag, 30 RPM.
v4: price_history, dedup-hash, 60-Tage-Archiv, ImmoScout24.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from groq import Groq
from playwright.async_api import async_playwright
try:
    from playwright_stealth import stealth_async as _stealth_async
    _HAS_STEALTH = True
except ImportError:
    _HAS_STEALTH = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

GROQ_API_KEY         = os.environ.get("GROQ_API_KEY", "")
IS24_COOKIES_JSON    = os.environ.get("IS24_COOKIES", "")
CONFIG_PATH          = Path("config.json")
DEALS_PATH           = Path("data/deals.json")
MAX_NEW_PER_RUN      = 60
MAX_PER_SOURCE       = 5    # default; overridden per source via config 'max_new'
PAGE_TIMEOUT_MS      = 12000
DETAIL_WAIT_MS       = 500
MAX_RUNTIME_SECS     = 2400
DETAIL_WORKERS       = 3
GROQ_SLEEP_SECS      = 2.0
ARCHIVE_AFTER_DAYS   = 60

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
_groq_lock  = None


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


def get_existing_hashes(data):
    return {d["_hash"] for d in data.get("deals", []) if d.get("_hash")}


def compute_deal_hash(title, listing_type, source):
    """Stable hash for duplicate detection (title+type+source)."""
    normalized = re.sub(r'\s+', ' ', title.lower().strip())[:200]
    key = f"{source}|{listing_type}|{normalized}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


def archive_old_deals(data):
    """Remove deals not seen in 60+ days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=ARCHIVE_AFTER_DAYS)
    before = len(data["deals"])
    kept = []
    for d in data["deals"]:
        seen = d.get("last_seen") or d.get("first_seen")
        if not seen:
            kept.append(d)
            continue
        try:
            ts = datetime.fromisoformat(seen)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts > cutoff:
                kept.append(d)
        except Exception:
            kept.append(d)
    data["deals"] = kept
    removed = before - len(kept)
    if removed:
        log.info("Archiviert: %d Deals (>%d Tage alt) | verbleibend: %d",
                 removed, ARCHIVE_AFTER_DAYS, len(kept))


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
        # Sanity check: Kaufpreis < 20.000 € ist kein echter Kaufpreis (Mietinserat falsch kategorisiert)
        if kaufpreis and kaufpreis < 20_000:
            log.info("   Kauf: Kaufpreis %.0f EUR zu niedrig – wahrscheinlich Mietinserat, ignoriert", kaufpreis)
            deal["kaufpreis"] = None
            kaufpreis = None
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


def cleanup_deals(data, cfg):
    """Bereinigt bestehende deals.json bei jedem Lauf:
    - 'kauf' mit Mini-Preis (<20.000 €) → als Miete reklassifizieren (Monatsmiete)
      bzw. bei unbrauchbarem Wert entfernen.
    Korrigiert so auch Altdaten ohne kompletten Re-Scrape."""
    kept, reclass, dropped = [], 0, 0
    for d in data.get("deals", []):
        if d.get("listing_type") == "kauf" and d.get("kaufpreis") and d["kaufpreis"] < 20_000:
            p = d["kaufpreis"]
            if 100 <= p <= 6_000:
                d["listing_type"] = "miete"
                if not d.get("kaltmiete"):
                    d["kaltmiete"] = p
                d["kaufpreis"] = None
                for k in ("bankrate_monat", "cashflow_monat", "cashflow_vor_tilgung",
                          "bruttorendite_pct", "kaufpreisfaktor"):
                    d.pop(k, None)
                berechne_scores(d, cfg)
                d["_hash"] = compute_deal_hash(d.get("title", ""), "miete", d.get("source", ""))
                reclass += 1
            else:
                dropped += 1
                continue
        kept.append(d)
    data["deals"] = kept
    if reclass or dropped:
        log.info("Bereinigung: %d kauf→miete reklassifiziert, %d entfernt", reclass, dropped)
    return data


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
        "hat_balkon":     False,
        "price_history":  [],
        "etage":          None,
        "baujahr":        None,
        "energieklasse":  None,
        "heizungsart":    None,
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

    # Fehl-Kategorisierung korrigieren: "Kauf" mit Mini-Preis ist real ein
    # (oft möbliertes) Mietinserat – der Preis ist die Monatsmiete.
    if deal["listing_type"] == "kauf" and deal.get("kaufpreis") and deal["kaufpreis"] < 20_000:
        p = deal["kaufpreis"]
        if 100 <= p <= 6_000:
            deal["listing_type"] = "miete"
            if not deal.get("kaltmiete"):
                deal["kaltmiete"] = p
            deal["kaufpreis"] = None
            log.info("   Reklassifiziert kauf→miete (Mini-Preis %.0f EUR): %s",
                     p, deal.get("title", "")[:50])
        else:
            deal["kaufpreis"] = None  # unbrauchbarer Wert

    deal = berechne_scores(deal, cfg)

    # Initialize price history with current price
    hauptpreis = deal.get("kaufpreis") or deal.get("kaltmiete")
    if hauptpreis:
        deal["price_history"] = [{"date": now_iso, "price": hauptpreis}]

    # Dedup hash (title + type + source)
    deal["_hash"] = compute_deal_hash(raw["title"], deal["listing_type"], raw["source"])

    return deal


# ── Detail scraper ───────────────────────────────────────────────────────────

async def scrape_detail_async(ctx, url, sem):
    async with sem:
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            await page.wait_for_timeout(DETAIL_WAIT_MS)
            parts = []

            for sel in ["#viewad-price", "[data-testid='price-amount']",
                        ".boxedarticle--details--price",
                        "[data-testid='purchase-price']",
                        ".is24-value"]:
                el = await page.query_selector(sel)
                if el:
                    txt = (await el.inner_text()).strip()
                    if txt:
                        parts.append("Preis: " + txt)
                        break

            for sel in ["#viewad-locality", "#viewad-address",
                        "[data-testid='address-block']"]:
                el = await page.query_selector(sel)
                if el:
                    txt = (await el.inner_text()).strip()
                    if txt:
                        parts.append("Adresse: " + txt)
                        break

            for sel in ["#viewad-details", ".boxedarticle--details",
                        "[data-testid='ad-detail-attributes']",
                        ".criteriagroup"]:
                el = await page.query_selector(sel)
                if el:
                    txt = (await el.inner_text()).strip()
                    if txt:
                        parts.append(txt)
                        break

            for sel in ["#viewad-description-text", "#viewad-description",
                        "[data-testid='expose-description']"]:
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

    # Preise: "275.000 EUR", "1.045.000 €", "275.000,00 €", "275000€", "850 €"
    # Alt1: Tausender mit Trennzeichen (führende Gruppe 1–3 Ziffern → auch Millionen
    #       wie 1.045.000). Alt2: zusammenhängende Ziffernfolge ohne Trennzeichen.
    price_re = re.compile(
        r'(\d{1,3}(?:[.\s]\d{3})+(?:,\d{2})?|\d{3,8}(?:,\d{2})?)\s*(?:€|EUR|Euro)',
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

    # Balkon / Terrasse / Loggia – with negative context check
    balkon_neg = re.search(r'kein\s+Balkon|ohne\s+Balkon|kein\s+Terrasse', text, re.IGNORECASE)
    balkon_pos = re.search(r'\bBalkon\b|\bTerrasse\b|\bLoggia\b', text, re.IGNORECASE)
    if balkon_pos and not balkon_neg:
        result["hat_balkon"] = True

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
        '    "demografie": <"Wer wohnt hier? Ungeschönt">,\n'
        '    "vibe_sicherheit": <"Atmosphäre und Sicherheitsgefühl">,\n'
        '    "einschaetzung_student": <"Eignung für Informatikstudenten">,\n'
        '    "einschaetzung_investor": <"Eignung als Kapitalanlage">\n'
        '  },\n'
        '  "etage": <Stockwerk als String z.B. "2. OG" oder "EG" oder null>,\n'
        '  "baujahr": <Baujahr als Integer oder null>,\n'
        '  "energieklasse": <"A+","A","B","C","D","E","F","G","H" oder null>,\n'
        '  "heizungsart": <"Fernwärme","Gas","Öl","Wärmepumpe","Elektro","Pellets" oder null>\n'
        "}"
    )

    try:
        client = Groq(api_key=GROQ_API_KEY)
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=1024,
        )
        raw_resp = response.choices[0].message.content.strip()
        raw_resp = re.sub(r"```(?:json)?\s*|\s*```", "", raw_resp).strip()
        m = re.search(r"\{[\s\S]*\}", raw_resp)
        if not m:
            log.warning("   Groq: kein JSON in Antwort")
            return None

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

        ua = result.get("umfeld_analyse")
        if not isinstance(ua, dict):
            result["umfeld_analyse"] = None

        log.info("   Groq OK: typ=%s kauf=%s miete=%s umfeld=%s",
                 result.get("listing_type"), result.get("kaufpreis"),
                 result.get("kaltmiete"),
                 "ja" if result.get("umfeld_analyse") else "nein")

        time.sleep(GROQ_SLEEP_SECS)
        return result

    except json.JSONDecodeError as e:
        log.warning("   Groq JSON-Fehler: %s", e)
    except Exception as e:
        err = str(e).lower()
        if any(x in err for x in ("api_key", "invalid_api_key", "authentication",
                                   "unauthorized", "403")):
            log.error("   Groq API-Key ungueltig: %s", e)
            _groq_key_invalid = True
        elif any(x in err for x in ("decommissioned", "no longer supported",
                                     "model_not_found", "model not found")):
            log.error("   Groq Modell abgeschaltet: %s", str(e)[:120])
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

    regex_result = extract_by_regex(title, raw_text, listing_type_hint, cfg)

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
            elif k == "hat_balkon":
                if v:  # groq confirms balkon
                    regex_result[k] = True
            elif v is not None and regex_result.get(k) is None:
                regex_result[k] = v

    return regex_result


# ── Kleinanzeigen scraper ────────────────────────────────────────────────────

async def scrape_kleinanzeigen_async(ctx, source, existing_urls, cfg):
    """Returns (verified_new, price_updates_for_existing)."""
    raw           = []
    price_updates = []
    base_url      = source["url"]
    lst_type      = source["listing_type"]
    src_limit     = source.get("max_new", MAX_PER_SOURCE)

    search_page = await ctx.new_page()
    try:
        for page_num in range(1, source.get("pages", 1) + 1):
            if len(raw) >= src_limit or not time_ok():
                break
            url = base_url if page_num == 1 else base_url + "?pageNum=" + str(page_num)
            log.info("   KA Seite %d: %s", page_num, url[-50:])
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
                    if len(raw) >= src_limit:
                        break
                    try:
                        link_el = await item.query_selector(
                            "a[href*='/s-anzeige/'], a.ellipsis, h2 a")
                        href = (await link_el.get_attribute("href") or "") if link_el else ""
                        if not href:
                            continue
                        if href.startswith("/"):
                            href = "https://www.kleinanzeigen.de" + href

                        # Collect card text for price tracking
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

                        if href in existing_urls:
                            # Track price for existing deal
                            price_updates.append({"url": href, "card_text": card_text})
                            continue

                        title_el = await item.query_selector(
                            "h2, .ellipsis, .aditem-main--middle--headline a")
                        title = ((await title_el.inner_text()).strip()
                                 if title_el else "").strip()
                        if not title or NON_MANNHEIM_RE.search(title):
                            continue

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
        return [], price_updates

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
        entry["raw_text"] = (detail + "\n\n" + card).strip() if detail else card
        if not is_mannheim(entry["title"], entry["raw_text"] + " " + card):
            log.info("   SKIP (kein Mannheim): %s", entry["title"][:55])
            continue
        verified.append(entry)

    log.info("   %d/%d nach Mannheim-Filter", len(verified), len(raw))
    return verified, price_updates


# ── IS24 Cookie Injection ────────────────────────────────────────────────────

async def inject_is24_cookies(ctx):
    """Inject real browser cookies into context to bypass Cloudflare IP block."""
    if not IS24_COOKIES_JSON:
        log.warning("   IS24: Kein IS24_COOKIES Secret gesetzt")
        return False
    try:
        raw = json.loads(IS24_COOKIES_JSON)
        if isinstance(raw, dict):
            raw = [raw]
        playwright_cookies = []
        for c in raw:
            name  = c.get("name") or c.get("Name", "")
            value = c.get("value") or c.get("Value", "")
            if not name or not value:
                continue
            domain = c.get("domain") or c.get("Domain", ".immobilienscout24.de")
            if domain and not domain.startswith(".") and not domain.startswith("http"):
                domain = "." + domain
            pc = {
                "name":     name,
                "value":    value,
                "domain":   domain,
                "path":     c.get("path") or c.get("Path", "/"),
                "secure":   bool(c.get("secure") or c.get("Secure", False)),
                "httpOnly": bool(c.get("httpOnly") or c.get("HttpOnly", False)),
            }
            exp = c.get("expirationDate") or c.get("expires") or c.get("Expires")
            if exp:
                try:
                    pc["expires"] = int(float(exp))
                except (ValueError, TypeError):
                    pass
            playwright_cookies.append(pc)
        if not playwright_cookies:
            log.warning("   IS24: Cookie-JSON leer oder unlesbar")
            return False
        await ctx.add_cookies(playwright_cookies)
        cf = [c["name"] for c in playwright_cookies if "cf_" in c["name"]]
        log.info("   IS24: %d Cookies injiziert | Cloudflare-Cookies: %s",
                 len(playwright_cookies), cf or "keine")
        return True
    except json.JSONDecodeError as e:
        log.error("   IS24: Cookie-JSON Parse-Fehler: %s", e)
    except Exception as e:
        log.error("   IS24: Cookie-Injection Fehler: %s", e)
    return False


# ── ImmoScout24 scraper ──────────────────────────────────────────────────────

IS24_EXPOSE_RE = re.compile(r'/expose/(\d{5,})')


async def _is24_html_fallback(search_page, lst_type, existing_urls,
                              price_updates, raw, src_limit):
    """Fallback-Extraktion: Expose-IDs direkt aus dem Seiten-HTML.
    Robust gegen CSS-Klassen-Aenderungen; Titel werden aus den
    Detailseiten nachgezogen. Gibt Anzahl neuer Kandidaten zurueck."""
    try:
        html = await search_page.content()
    except Exception:
        return 0
    seen, found = set(), 0
    for m in IS24_EXPOSE_RE.finditer(html):
        eid = m.group(1)
        if eid in seen:
            continue
        seen.add(eid)
        if len(raw) >= src_limit:
            break
        href = f"https://www.immobilienscout24.de/expose/{eid}"
        if href in existing_urls:
            price_updates.append({"url": href, "card_text": ""})
            continue
        raw.append({
            "url":          href,
            "source":       "immoscout24",
            "listing_type": lst_type,
            "title":        f"IS24 Expose {eid}",   # aus Detailseite verbessert
            "card_text":    "",
            "_needs_title":  True,
        })
        existing_urls.add(href)
        found += 1
    return found


async def scrape_immoscout24_async(ctx, source, existing_urls, cfg):
    """Returns (verified_new, price_updates_for_existing)."""
    raw           = []
    price_updates = []
    base_url      = source["url"]
    lst_type      = source["listing_type"]
    src_limit     = source.get("max_new", MAX_PER_SOURCE)

    # Inject cookies BEFORE opening page (context-level)
    await inject_is24_cookies(ctx)

    search_page = await ctx.new_page()
    if _HAS_STEALTH:
        await _stealth_async(search_page)
    try:
        for page_num in range(1, source.get("pages", 1) + 1):
            if len(raw) >= src_limit or not time_ok():
                break
            url = base_url if page_num == 1 else base_url + "?pagenumber=" + str(page_num)
            log.info("   IS24 Seite %d: %s", page_num, url[-60:])
            try:
                await search_page.goto(url, wait_until="domcontentloaded",
                                       timeout=PAGE_TIMEOUT_MS * 2)
                await search_page.wait_for_timeout(3000)

                # Accept cookies if banner appears
                for cookie_sel in [
                    "button#uc-btn-accept-banner",
                    "button[data-testid='uc-accept-all-button']",
                    "button.sc-lbVpMG",
                    "[data-gdpr='true'] button",
                    "button[id*='accept']",
                    "div[class*='consent'] button",
                    "#usercentrics-root button",
                ]:
                    try:
                        btn = await search_page.query_selector(cookie_sel)
                        if btn:
                            await btn.click()
                            await search_page.wait_for_timeout(1000)
                            break
                    except Exception:
                        pass

                # Try to wait for listing content
                try:
                    await search_page.wait_for_selector(
                        "article[data-id], [data-testid='result-list-entry']",
                        timeout=5000
                    )
                except Exception:
                    pass  # continue with query_selector_all anyway

                items = await search_page.query_selector_all(
                    "article[data-id], li.result-list__listing article, "
                    "[data-testid='result-list-entry']"
                )
                if not items:
                    # Fallback: Expose-IDs direkt aus dem HTML ziehen (robust gegen
                    # CSS-Klassen-Aenderungen von IS24). Titel kommen aus Detailseiten.
                    fb = await _is24_html_fallback(
                        search_page, lst_type, existing_urls, price_updates, raw, src_limit)
                    if fb:
                        log.info("   IS24: %d Kandidaten via HTML-Fallback (Seite %d)", fb, page_num)
                        if len(raw) >= src_limit:
                            break
                        continue
                    log.info("   IS24: Keine Eintraege auf Seite %d", page_num)
                    break

                found = 0
                for item in items:
                    if len(raw) >= src_limit:
                        break
                    try:
                        link_el = await item.query_selector("a[href*='/expose/']")
                        href = (await link_el.get_attribute("href") or "") if link_el else ""
                        if not href:
                            continue
                        if href.startswith("/"):
                            href = "https://www.immobilienscout24.de" + href
                        # Normalize: strip query params
                        href = href.split("?")[0]

                        card_text = (await item.inner_text()).strip()[:600]

                        if href in existing_urls:
                            price_updates.append({"url": href, "card_text": card_text})
                            continue

                        # Get title
                        title = ""
                        for t_sel in [
                            "[data-testid='result-list-entry-brand-title']",
                            "h5.result-list-entry__brand-title-container",
                            "h3", "h4", "h5",
                        ]:
                            t_el = await item.query_selector(t_sel)
                            if t_el:
                                title = (await t_el.inner_text()).strip()
                                if title:
                                    break

                        if not title:
                            # Fallback: first non-empty text line
                            lines = [l.strip() for l in card_text.split("\n") if l.strip()]
                            title = lines[0][:200] if lines else ""

                        if not title or NON_MANNHEIM_RE.search(title):
                            continue

                        raw.append({
                            "url":          href,
                            "source":       "immoscout24",
                            "listing_type": lst_type,
                            "title":        title[:400],
                            "card_text":    card_text,
                        })
                        existing_urls.add(href)
                        found += 1
                    except Exception as ex:
                        log.debug("   IS24 item error: %s", ex)

                log.info("   IS24: %d Kandidaten auf Seite %d", found, page_num)
                if found == 0:
                    break

            except Exception as e:
                log.error("   IS24 Seitenfehler: %s", e)
                break
    finally:
        await search_page.close()

    if not raw:
        return [], price_updates

    log.info("   %d IS24 Detailseiten parallel...", len(raw))
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
        entry["raw_text"] = (detail + "\n\n" + card).strip() if detail else card
        # Titel aus Detailseite nachziehen, wenn nur Fallback-Platzhalter vorhanden
        if entry.pop("_needs_title", False) and detail:
            for line in detail.split("\n"):
                line = line.strip()
                if len(line) > 15 and not line.lower().startswith("immobilienscout24"):
                    entry["title"] = line[:400]
                    break
        if not is_mannheim(entry["title"], entry["raw_text"] + " " + card):
            log.info("   SKIP IS24 (kein Mannheim): %s", entry["title"][:55])
            continue
        verified.append(entry)

    log.info("   %d/%d IS24 nach Mannheim-Filter", len(verified), len(raw))
    return verified, price_updates


# ── Source dispatcher ────────────────────────────────────────────────────────

async def scrape_source(ctx, source, existing_urls, cfg):
    """Dispatches to the correct scraper based on source['scraper']."""
    scraper_type = source.get("scraper", "kleinanzeigen")
    if scraper_type == "immoscout24":
        return await scrape_immoscout24_async(ctx, source, existing_urls, cfg)
    elif scraper_type == "immowelt":
        return await scrape_immowelt_async(ctx, source, existing_urls, cfg)
    elif scraper_type == "gbg_immomio":
        return await scrape_gbg_async(ctx, source, existing_urls, cfg)
    elif scraper_type == "vonovia":
        return await scrape_vonovia_async(ctx, source, existing_urls, cfg)
    elif scraper_type == "immonet":
        return await scrape_immonet_async(ctx, source, existing_urls, cfg)
    else:
        return await scrape_kleinanzeigen_async(ctx, source, existing_urls, cfg)


# ── Price history updater ────────────────────────────────────────────────────

def update_price_history(data, price_updates, now_iso, cfg):
    """Update last_seen and price_history for existing deals based on card prices."""
    existing_by_url = {d["url"]: d for d in data["deals"]}
    updated = 0
    for upd in price_updates:
        deal = existing_by_url.get(upd["url"])
        if not deal:
            continue
        deal["last_seen"] = now_iso
        card_text = upd.get("card_text", "")
        if not card_text:
            continue
        regex_upd = extract_by_regex("", card_text, deal["listing_type"], cfg)
        new_price  = regex_upd.get("kaufpreis") or regex_upd.get("kaltmiete")
        stored     = deal.get("kaufpreis") or deal.get("kaltmiete")
        history    = deal.setdefault("price_history", [])

        # Seed history if empty
        if not history and stored:
            history.append({"date": deal.get("first_seen", now_iso), "price": stored})

        # Record price change (>100 EUR = real change)
        if new_price and stored and abs(new_price - stored) > 100:
            history.append({"date": now_iso, "price": new_price})
            log.info("   Preisaenderung: %s → %.0f EUR (war %.0f)",
                     deal["url"][35:70], new_price, stored)
            if deal["listing_type"] == "kauf":
                deal["kaufpreis"] = new_price
            else:
                deal["kaltmiete"] = new_price
            berechne_scores(deal, cfg)
            updated += 1

    if updated:
        log.info("Preisaenderungen: %d Deals aktualisiert", updated)


# ── Stale-Check ──────────────────────────────────────────────────────────────

async def check_stale_deals(data, now_iso):
    """Check deals not seen in 5+ days and mark offline if 404/410."""
    import urllib.request
    import urllib.error

    cutoff = datetime.now(timezone.utc) - timedelta(days=5)
    stale_sources = {"kleinanzeigen", "immowelt", "vonovia"}

    candidates = []
    for deal in data.get("deals", []):
        if deal.get("status") == "offline":
            continue
        if deal.get("source") not in stale_sources:
            continue
        last = deal.get("last_seen") or deal.get("first_seen")
        if not last:
            continue
        try:
            ts = datetime.fromisoformat(last)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                candidates.append(deal)
        except Exception:
            pass

    # Sort by oldest last_seen first, limit to 15
    candidates.sort(key=lambda d: d.get("last_seen") or d.get("first_seen") or "")
    candidates = candidates[:15]

    checked = 0
    offline_found = 0

    async def _check_one(deal):
        nonlocal checked, offline_found
        url = deal.get("url", "")
        if not url:
            return

        def _head_request():
            req = urllib.request.Request(url, method="HEAD")
            req.add_header("User-Agent", "Mozilla/5.0")
            try:
                with urllib.request.urlopen(req, timeout=8):
                    pass
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):
                    raise
            except Exception:
                pass  # timeout / network – don't mark offline

        try:
            await asyncio.to_thread(_head_request)
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                deal["status"] = "offline"
                deal["offline_since"] = now_iso
                log.info("   OFFLINE erkannt: %s", url[35:70])
                offline_found += 1
        except Exception:
            pass
        checked += 1

    await asyncio.gather(*[_check_one(d) for d in candidates])
    log.info("Stale-Check: %d geprüft | %d offline markiert", checked, offline_found)



# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    global _start_time, _groq_lock
    _start_time = time.time()
    _groq_lock  = asyncio.Lock()

    log.info("=" * 58)
    log.info("Immobilien-Sniper v4.1 | MAX=%d | PRO_QUELLE_DEFAULT=%d",
             MAX_NEW_PER_RUN, MAX_PER_SOURCE)
    log.info("MaxRun=%ds | Details=%dx | Groq sleep=%.1fs | Archiv=%dd",
             MAX_RUNTIME_SECS, DETAIL_WORKERS, GROQ_SLEEP_SECS, ARCHIVE_AFTER_DAYS)
    log.info("Groq-Key: %s", "OK" if GROQ_API_KEY else "FEHLT!")
    log.info("=" * 58)

    cfg  = load_config()
    data = load_deals()

    # Archive old deals first
    archive_old_deals(data)

    # Bestehende Daten bereinigen (Fehl-Kategorisierungen korrigieren)
    cleanup_deals(data, cfg)

    existing_urls   = get_existing_urls(data)
    existing_hashes = get_existing_hashes(data)
    log.info("Bestehende Deals: %d | URLs: %d | Hashes: %d",
             len(data["deals"]), len(existing_urls), len(existing_hashes))

    all_raw           = []
    all_price_updates = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,  # xvfb-run provides virtual display
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
            log.info("\nQuelle: %s (%s)", source["name"], source.get("scraper", "kleinanzeigen"))
            try:
                results, upds = await scrape_source(ctx, source, existing_urls, cfg)
                log.info("%d verifiziert | %d Preis-Updates von %s",
                         len(results), len(upds), source["name"])
                all_raw.extend(results)
                all_price_updates.extend(upds)
                if len(all_raw) >= MAX_NEW_PER_RUN:
                    break
                log.info("Pause 15s zwischen Quellen (Anti-Bot)...")
                await asyncio.sleep(15)
            except Exception as e:
                log.error("Quelle %s fehler: %s", source["name"], e)

        await browser.close()

    # Update price history for existing deals
    now_iso = datetime.now(timezone.utc).isoformat()
    update_price_history(data, all_price_updates, now_iso, cfg)

    all_raw = all_raw[:MAX_NEW_PER_RUN]
    ai_ok = ai_fail = dedup_skip = 0

    log.info("\nAnalyse: %d neue Inserate (Regex + Groq/Llama3)...", len(all_raw))
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

        # Dedup check by hash
        deal_hash = deal.get("_hash", "")
        if deal_hash and deal_hash in existing_hashes:
            log.info("   DEDUP skip (gleicher Titel): %s", raw_entry["title"][:55])
            dedup_skip += 1
            continue
        existing_hashes.add(deal_hash)

        data["deals"].append(deal)
        if i % 10 == 0:
            save_deals(data)

    elapsed = int(time.time() - _start_time)
    log.info("=" * 58)
    log.info("Fertig: %ds | neu=%d | dedup_skip=%d | AI ok=%d fail=%d | gesamt=%d",
             elapsed, len(all_raw) - dedup_skip, dedup_skip,
             ai_ok, ai_fail, len(data["deals"]))
    await check_stale_deals(data, now_iso)
    save_deals(data)




# ── Immowelt scraper ─────────────────────────────────────────────────────────

async def scrape_immowelt_async(ctx, source, existing_urls, cfg):
    """Immowelt SSR – Karten/Seite, Pagination via JS-Button.
    Returns (verified_new, price_updates_for_existing)."""
    raw           = []
    price_updates = []
    base_url      = source["url"]
    lst_type      = source["listing_type"]
    src_limit     = source.get("max_new", MAX_PER_SOURCE)

    page = await ctx.new_page()
    try:
        log.info("   IW: Lade %s", base_url[-60:])
        await page.goto(base_url, wait_until="domcontentloaded",
                        timeout=PAGE_TIMEOUT_MS * 2)
        await page.wait_for_timeout(3000)

        # Accept cookie banner
        for sel in [
            "button#onetrust-accept-btn-handler",
            "button[data-testid='uc-accept-all-button']",
            "button[id*='accept']", "button[class*='accept-all']",
        ]:
            try:
                btn = await page.query_selector(sel)
                if btn:
                    await btn.click()
                    await page.wait_for_timeout(1000)
                    break
            except Exception:
                pass

        for page_num in range(1, source.get("pages", 1) + 1):
            if len(raw) >= src_limit or not time_ok():
                break

            if page_num > 1:
                try:
                    next_btn = await page.query_selector(
                        'button[aria-label="nächste seite"],'
                        'button[aria-label*="next"],'
                        '[data-testid*="pagination-next"]'
                    )
                    if not next_btn:
                        log.info("   IW: Kein 'nächste Seite' Button")
                        break
                    await next_btn.click()
                    await page.wait_for_timeout(2500)
                except Exception as e:
                    log.warning("   IW: Pagination Fehler: %s", e)
                    break

            cards = await page.query_selector_all(
                '[data-testid="serp-core-classified-card-testid"]'
            )
            if not cards:
                log.info("   IW: Keine Karten auf Seite %d", page_num)
                break

            found = 0
            for card in cards[:src_limit]:
                if len(raw) >= src_limit:
                    break
                try:
                    link_el = await card.query_selector('a[href*="/expose/"]')
                    if not link_el:
                        continue
                    href = (await link_el.get_attribute("href") or "").split("?")[0]
                    if not href:
                        continue
                    if href.startswith("/"):
                        href = "https://www.immowelt.de" + href

                    card_text = (await card.inner_text()).strip()[:800]

                    if href in existing_urls:
                        price_updates.append({"url": href, "card_text": card_text})
                        continue

                    # Title: prefer img alt (contains price+rooms+sqm+location)
                    title = ""
                    img_el = await card.query_selector("img[alt]")
                    if img_el:
                        title = (await img_el.get_attribute("alt") or "").strip()
                    if not title or len(title) < 10:
                        lines = [l.strip() for l in card_text.split("\n")
                                 if l.strip() and len(l.strip()) > 10]
                        title = lines[0][:300] if lines else ""

                    if not title or NON_MANNHEIM_RE.search(title):
                        continue

                    raw.append({
                        "url":          href,
                        "source":       "immowelt",
                        "listing_type": lst_type,
                        "title":        title[:400],
                        "card_text":    card_text,
                    })
                    existing_urls.add(href)
                    found += 1
                except Exception as ex:
                    log.debug("   IW card error: %s", ex)

            log.info("   IW: %d Kandidaten auf Seite %d", found, page_num)
            if found == 0:
                break

    except Exception as e:
        log.error("   IW Gesamt-Fehler: %s", e)
    finally:
        await page.close()

    if not raw:
        return [], price_updates

    log.info("   %d IW Detailseiten parallel...", len(raw))
    det_sem = asyncio.Semaphore(DETAIL_WORKERS)
    detail_results = await asyncio.gather(
        *[scrape_detail_async(ctx, e["url"], det_sem) for e in raw],
        return_exceptions=True
    )

    verified = []
    for entry, detail in zip(raw, detail_results):
        if isinstance(detail, Exception):
            detail = ""
        detail = str(detail) if detail else ""
        card = entry.get("card_text", "")
        entry["raw_text"] = (detail + "\n\n" + card).strip() if detail else card
        if not is_mannheim(entry["title"], entry["raw_text"] + " " + card):
            log.info("   SKIP IW (kein Mannheim): %s", entry["title"][:55])
            continue
        verified.append(entry)

    log.info("   %d/%d IW nach Mannheim-Filter", len(verified), len(raw))
    return verified, price_updates


# ── GBG Mannheim via Immomio SPA ─────────────────────────────────────────────

GBG_IMMOMIO_TOKEN = (
    "eyJhbGciOiJIUzI1NiJ9"
    ".eyJjdXN0b21lcklkIjozNDU0MTI3OCwiaWQiOjI5OTQ4MzI1MSwiY3JlYXRlZCI6MTY2NTY2ODc1NTc0Nn0"
    ".lFmmH-wmv9n5IUK79WY-O2gD1xpTdfgIgXt5yh2PP04"
)

async def scrape_gbg_async(ctx, source, existing_urls, cfg):
    """GBG Mannheim über Immomio React-SPA.
    Returns (verified_new, price_updates_for_existing)."""
    raw           = []
    price_updates = []
    lst_type      = source.get("listing_type", "miete")
    token         = source.get("token", GBG_IMMOMIO_TOKEN)
    url           = f"https://homepage.immomio.com/de/properties?token={token}"

    page = await ctx.new_page()
    try:
        log.info("   GBG: Lade Immomio SPA")
        await page.goto(url, wait_until="domcontentloaded",
                        timeout=PAGE_TIMEOUT_MS * 3)
        # SPA braucht Zeit zum Rendern
        await page.wait_for_timeout(6000)

        # Selector-Fallback-Kette
        card_selectors = [
            ".property-tile",
            "[class*='PropertyTile']",
            "[class*='property-card']",
            "[class*='property-item']",
            "[class*='listing-item']",
            "[class*='offer-item']",
            "article",
            "[class*='estate']",
        ]
        cards = []
        used_sel = ""
        for sel in card_selectors:
            found = await page.query_selector_all(sel)
            if found:
                cards = found
                used_sel = sel
                log.info("   GBG: %d Karten mit Selector '%s'", len(found), sel)
                break

        if not cards:
            body_text = (await page.inner_text("body"))[:400]
            log.warning("   GBG: Keine Karten gefunden. Body: %s", body_text)
            return [], price_updates

        src_limit = source.get('max_new', MAX_PER_SOURCE)
        for card in cards[:src_limit]:
            try:
                card_text = (await card.inner_text()).strip()[:800]
                if not card_text or len(card_text) < 20:
                    continue

                # Immomio nutzt onclick statt <a href> – URL aus data-id oder Hash
                href = ""
                # Try data attributes for property ID
                for attr in ["data-id", "id", "data-object-id", "data-property-id"]:
                    did = await card.get_attribute(attr)
                    if did and len(did) > 3:
                        href = f"https://homepage.immomio.com/de/property/{did}?token={token}"
                        break
                # Fallback: stable hash URL from card content
                if not href:
                    import hashlib
                    card_hash = hashlib.md5(card_text[:200].encode()).hexdigest()[:16]
                    href = f"https://homepage.immomio.com/de/gbg/{card_hash}"

                if href in existing_urls:
                    price_updates.append({"url": href, "card_text": card_text})
                    continue

                lines = [l.strip() for l in card_text.split("\n")
                         if l.strip() and len(l.strip()) > 5]
                title = lines[0][:300] if lines else card_text[:100]
                if not title:
                    continue

                raw.append({
                    "url":          href,
                    "source":       "gbg",
                    "listing_type": lst_type,
                    "title":        title,
                    "card_text":    card_text,
                })
                existing_urls.add(href)
            except Exception as ex:
                log.debug("   GBG card error: %s", ex)

        log.info("   GBG: %d neue Kandidaten", len(raw))

    except Exception as e:
        log.error("   GBG Gesamt-Fehler: %s", e)
    finally:
        await page.close()

    # GBG-Seiten sind SPA – raw_text = card_text (kein separater Detail-Scrape nötig)
    verified = []
    for entry in raw:
        entry["raw_text"] = entry.get("card_text", "")
        # GBG = immer Mannheim (keine URL-Filter nötig)
        if entry["title"]:
            entry["title"] = "GBG Mannheim: " + entry["title"]
            verified.append(entry)

    log.info("   %d GBG Deals gefunden", len(verified))
    return verified, price_updates


# ── Vonovia scraper ───────────────────────────────────────────────────────────

async def scrape_vonovia_async(ctx, source, existing_urls, cfg):
    """Vonovia Mannheim über Next.js SPA.
    Returns (verified_new, price_updates_for_existing)."""
    raw           = []
    price_updates = []
    base_url      = source["url"]
    lst_type      = source.get("listing_type", "miete")

    page = await ctx.new_page()
    try:
        log.info("   Vonovia: Lade %s", base_url[-60:])
        await page.goto(base_url, wait_until="networkidle",
                        timeout=PAGE_TIMEOUT_MS * 3)
        await page.wait_for_timeout(4000)

        card_selectors = [
            "[data-testid='estate-teaser']",
            "[data-testid*='estate']",
            "[data-testid*='listing']",
            "[data-testid*='property']",
            "[class*='EstateTile']",
            "[class*='estate-tile']",
            "[class*='PropertyCard']",
            "[class*='property-card']",
            "article",
        ]
        cards = []
        used_sel = ""
        for sel in card_selectors:
            found = await page.query_selector_all(sel)
            if found and len(found) > 1:
                cards = found
                used_sel = sel
                log.info("   Vonovia: %d Karten mit '%s'", len(found), sel)
                break

        if not cards:
            # Fallback: alle Links mit /expose/ oder /wohnung/ Pattern
            link_els = await page.query_selector_all(
                "a[href*='/expose/'], a[href*='/wohnung/'], a[href*='/immobilien/']"
            )
            if link_els:
                cards = link_els
                used_sel = "a[href*=expose/wohnung/immobilien]"
                log.info("   Vonovia: %d Links als Fallback", len(link_els))
            else:
                body_text = (await page.inner_text("body"))[:400]
                log.warning("   Vonovia: Keine Karten. Body: %s", body_text)
                return [], price_updates

        src_limit = source.get('max_new', MAX_PER_SOURCE)
        for card in cards[:src_limit]:
            try:
                # URL
                if used_sel.startswith("a["):
                    href = (await card.get_attribute("href") or "").split("?")[0]
                else:
                    link_el = await card.query_selector("a[href]")
                    href = (await link_el.get_attribute("href") or "").split("?")[0] if link_el else ""

                if not href:
                    continue
                if href.startswith("/"):
                    href = "https://www.vonovia.de" + href
                if not href.startswith("http"):
                    continue

                card_text = (await card.inner_text()).strip()[:800]

                if href in existing_urls:
                    price_updates.append({"url": href, "card_text": card_text})
                    continue

                lines = [l.strip() for l in card_text.split("\n")
                         if l.strip() and len(l.strip()) > 8]
                title = lines[0][:300] if lines else card_text[:100]

                if not title or NON_MANNHEIM_RE.search(title):
                    continue

                raw.append({
                    "url":          href,
                    "source":       "vonovia",
                    "listing_type": lst_type,
                    "title":        title,
                    "card_text":    card_text,
                })
                existing_urls.add(href)
            except Exception as ex:
                log.debug("   Vonovia card error: %s", ex)

        log.info("   Vonovia: %d neue Kandidaten", len(raw))

    except Exception as e:
        log.error("   Vonovia Gesamt-Fehler: %s", e)
    finally:
        await page.close()

    if not raw:
        return [], price_updates

    det_sem = asyncio.Semaphore(DETAIL_WORKERS)
    detail_results = await asyncio.gather(
        *[scrape_detail_async(ctx, e["url"], det_sem) for e in raw],
        return_exceptions=True
    )

    verified = []
    for entry, detail in zip(raw, detail_results):
        if isinstance(detail, Exception):
            detail = ""
        detail = str(detail) if detail else ""
        card = entry.get("card_text", "")
        entry["raw_text"] = (detail + "\n\n" + card).strip() if detail else card
        # Vonovia URL bereits auf Mannheim gefiltert
        verified.append(entry)

    log.info("   %d Vonovia Deals", len(verified))
    return verified, price_updates



# ── Immonet.de scraper ────────────────────────────────────────────────────────

async def scrape_immonet_async(ctx, source, existing_urls, cfg):
    """Immonet.de scraper für Mannheim.
    Returns (verified_new, price_updates_for_existing)."""
    raw           = []
    price_updates = []
    base_url      = source["url"]
    lst_type      = source["listing_type"]
    src_limit     = source.get("max_new", MAX_PER_SOURCE)

    page = await ctx.new_page()
    try:
        log.info("   Immonet: Lade %s", base_url[-60:])
        await page.goto(base_url, wait_until="domcontentloaded",
                        timeout=PAGE_TIMEOUT_MS * 2)
        await page.wait_for_timeout(3000)

        # Accept cookie banner
        for sel in [
            "button[id*='accept']",
            "button[class*='accept-all']",
            "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
        ]:
            try:
                btn = await page.query_selector(sel)
                if btn:
                    await btn.click()
                    await page.wait_for_timeout(1000)
                    break
            except Exception:
                pass

        for page_num in range(1, source.get("pages", 1) + 1):
            if len(raw) >= src_limit or not time_ok():
                break

            if page_num > 1:
                try:
                    next_btn = await page.query_selector(
                        'a[aria-label*="nächste"], a[aria-label*="next"], '
                        '[data-testid*="pagination-next"], .pagination-next a, '
                        'a.next-page'
                    )
                    if not next_btn:
                        log.info("   Immonet: Kein Weiter-Button")
                        break
                    await next_btn.click()
                    await page.wait_for_timeout(2500)
                except Exception as e:
                    log.warning("   Immonet: Pagination Fehler: %s", e)
                    break

            # Try card selectors
            cards = []
            card_selectors_try = [
                "[data-testid='result-list-entry']",
                "article.result-list-entry",
                "[class*='result-list-entry']",
                "div[id*='selObject']",
                "li[class*='result-list-entry']",
            ]
            for sel in card_selectors_try:
                found = await page.query_selector_all(sel)
                if found:
                    cards = found
                    log.info("   Immonet: %d Karten mit '%s'", len(found), sel)
                    break

            if not cards:
                body_text = (await page.inner_text("body"))[:400]
                log.warning("   Immonet: Keine Karten. Body: %s", body_text)
                return [], price_updates

            found = 0
            for card in cards[:src_limit]:
                if len(raw) >= src_limit:
                    break
                try:
                    # Get link
                    href = ""
                    link_el = await card.query_selector("a[href*='/expose/']")
                    if not link_el:
                        link_el = await card.query_selector("a[href]")
                    if link_el:
                        href = (await link_el.get_attribute("href") or "").split("?")[0]
                    if not href:
                        continue
                    if href.startswith("/"):
                        href = "https://www.immonet.de" + href
                    if not href.startswith("http"):
                        href = "https://www.immonet.de" + href

                    card_text = (await card.inner_text()).strip()[:800]

                    if href in existing_urls:
                        price_updates.append({"url": href, "card_text": card_text})
                        continue

                    # Title: first meaningful line
                    lines = [l.strip() for l in card_text.split("\n")
                             if l.strip() and len(l.strip()) > 10]
                    title = lines[0][:300] if lines else card_text[:100]

                    if not title or NON_MANNHEIM_RE.search(title):
                        continue

                    raw.append({
                        "url":          href,
                        "source":       "immonet",
                        "listing_type": lst_type,
                        "title":        title[:400],
                        "card_text":    card_text,
                    })
                    existing_urls.add(href)
                    found += 1
                except Exception as ex:
                    log.debug("   Immonet card error: %s", ex)

            log.info("   Immonet: %d Kandidaten auf Seite %d", found, page_num)
            if found == 0:
                break

    except Exception as e:
        log.error("   Immonet Gesamt-Fehler: %s", e)
    finally:
        await page.close()

    if not raw:
        return [], price_updates

    log.info("   %d Immonet Detailseiten parallel...", len(raw))
    det_sem = asyncio.Semaphore(DETAIL_WORKERS)
    detail_results = await asyncio.gather(
        *[scrape_detail_async(ctx, e["url"], det_sem) for e in raw],
        return_exceptions=True
    )

    verified = []
    for entry, detail in zip(raw, detail_results):
        if isinstance(detail, Exception):
            detail = ""
        detail = str(detail) if detail else ""
        card = entry.get("card_text", "")
        entry["raw_text"] = (detail + "\n\n" + card).strip() if detail else card
        if not is_mannheim(entry["title"], entry["raw_text"] + " " + card):
            log.info("   SKIP Immonet (kein Mannheim): %s", entry["title"][:55])
            continue
        verified.append(entry)

    log.info("   %d/%d Immonet nach Mannheim-Filter", len(verified), len(raw))
    return verified, price_updates

if __name__ == "__main__":
    asyncio.run(main())
