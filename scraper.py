"""
scraper.py – Immobilien-Sniper (GitHub Actions Edition)
=======================================================
Scrapt Kleinanzeigen + WG-Gesucht mit Playwright,
öffnet jede Detailseite für vollständigen Preistext,
analysiert mit Google Gemini (gemini-1.5-flash, kostenlos),
schreibt nach data/deals.json → wird ins Repo gepusht.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import google.generativeai as genai
from playwright.sync_api import Page, sync_playwright

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")
DEALS_PATH = Path("data/deals.json")
MANNHEIM_BENCHMARK_QM = 14.50
MAX_NEW_PER_RUN = 40       # Gemini Free-Tier: 15 req/min → ~3 Min. für 40 Deals
DEFAULT_ZIMMER = 2.5       # Fallback wenn Gemini nichts erkennt

# Städtenamen die NICHT Mannheim sind → Inserat verwerfen
NON_MANNHEIM_PATTERN = re.compile(
    r"\b(Hamburg|Berlin|München|Frankfurt am Main|Stuttgart|Köln|Düsseldorf|"
    r"Dortmund|Essen|Leipzig|Dresden|Hannover|Nürnberg|Bremen|Duisburg|"
    r"Bochum|Wuppertal|Bielefeld|Bonn|Münster|Freiburg|"
    r"Heidelberg(?!er Straße|er Weg|er Str))\b",
    re.IGNORECASE,
)

KEYWORD_AUFSCHLAEGE: dict[str, float] = {
    "EBK": 1.00, "Balkon": 0.50, "Terrasse": 0.50,
    "renoviert": 1.00, "möbliert": 3.00, "Neubau": 2.00,
    "Aufzug": 0.50, "Garage": 0.30,
}

# r10 = 10 km Radius → nur Mannheim + direktes Umland (kein ganz Deutschland)
SOURCES = [
    {
        "name": "kleinanzeigen_kauf", "scraper": "kleinanzeigen",
        "listing_type": "kauf", "enabled": True, "pages": 3,
        "url": "https://www.kleinanzeigen.de/s-wohnung-kaufen/mannheim/c196l9409r10",
    },
    {
        "name": "kleinanzeigen_miete", "scraper": "kleinanzeigen",
        "listing_type": "miete", "enabled": True, "pages": 3,
        "url": "https://www.kleinanzeigen.de/s-wohnung-mieten/mannheim/c203l9409r10",
    },
    {
        "name": "wg_gesucht", "scraper": "wg_gesucht",
        "listing_type": "miete", "enabled": True, "pages": 3,
        "url": "https://www.wg-gesucht.de/wohnungen-in-Mannheim.124.2.0.0.html",
    },
]


# ---------------------------------------------------------------------------
# deals.json lesen / schreiben
# ---------------------------------------------------------------------------

def load_deals() -> dict:
    DEALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DEALS_PATH.exists():
        try:
            with open(DEALS_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error(f"deals.json konnte nicht geladen werden: {e}")
    return {"last_updated": None, "deals": []}


def save_deals(data: dict) -> None:
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(DEALS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info(f"💾  {len(data['deals'])} Deals gespeichert → {DEALS_PATH}")


def get_existing_urls(data: dict) -> set[str]:
    return {d["url"] for d in data.get("deals", [])}


# ---------------------------------------------------------------------------
# Deal-Score + Finanzierungsrechnung (lokal, unabhängig von Gemini)
# ---------------------------------------------------------------------------

def berechne_scores(deal: dict) -> dict:
    """
    Kauf: berechnet bankrate_monat + cashflow_monat (Vollfinanzierung).
    Miete: berechnet kaltmiete_qm + Abweichung vom Mannheimer Benchmark.
    Setzt deal_score in jedem Fall.
    """
    if deal.get("listing_type") == "kauf":
        kaufpreis = deal.get("kaufpreis")
        kaltmiete = deal.get("kaltmiete")
        wfl = deal.get("wohnflaeche_qm")

        if not kaufpreis:
            log.warning(f"   Kein Kaufpreis → Score=skip: {deal.get('title','')[:50]}")
            deal.setdefault("deal_score", "skip")
            return deal

        # Miete schätzen falls nicht bekannt (Mietspiegel-Durchschnitt)
        if not kaltmiete and wfl:
            kaltmiete = round(wfl * 12.00, 2)
            deal["kaltmiete"] = kaltmiete
            deal["miete_geschaetzt"] = True
            log.info(f"   Kaltmiete geschätzt: {kaltmiete} €/Mo.")

        if kaltmiete:
            # Vollfinanzierung: 10% Kaufnebenkosten, 3,5% Zins + 1,5% Tilgung = 5% p.a.
            bankrate = round((kaufpreis * 1.10 * 0.05) / 12, 2)
            cashflow = round(kaltmiete - bankrate - 50.0, 2)   # 50 € Rücklagen
            deal["bankrate_monat"] = bankrate
            deal["cashflow_monat"] = cashflow
            deal["deal_score"] = (
                "strong_buy" if cashflow >= 0 else
                "watch"      if cashflow >= -150 else
                "skip"
            )
            log.info(
                f"   Kauf-Score: kaufpreis={kaufpreis:,.0f} € | "
                f"bank={bankrate:.0f} € | cf={cashflow:+.0f} € → {deal['deal_score']}"
            )
        else:
            deal.setdefault("deal_score", "skip")

    elif deal.get("listing_type") == "miete":
        kaltmiete = deal.get("kaltmiete")
        wfl = deal.get("wohnflaeche_qm")
        features = deal.get("features", [])

        if kaltmiete and wfl and wfl > 0:
            kaltmiete_qm = round(kaltmiete / wfl, 2)
            deal["kaltmiete_qm"] = kaltmiete_qm
            aufschlag = sum(KEYWORD_AUFSCHLAEGE.get(f, 0.0) for f in features)
            expected_qm = round(MANNHEIM_BENCHMARK_QM + aufschlag, 2)
            deal["expected_market_qm"] = expected_qm
            deviation = round(((kaltmiete_qm - expected_qm) / expected_qm) * 100, 1)
            deal["deal_deviation_pct"] = deviation
            deal["deal_score"] = (
                "gut"   if deviation <= -10 else
                "okay"  if deviation <=  10 else
                "teuer"
            )
            log.info(
                f"   Miete-Score: {kaltmiete:.0f} €/Mo. | "
                f"{kaltmiete_qm:.2f} €/m² | abw={deviation:+.1f}% → {deal['deal_score']}"
            )
        else:
            if not kaltmiete:
                log.warning(f"   Kaltmiete fehlt → Score=okay: {deal.get('title','')[:50]}")
            if not wfl:
                log.warning(f"   Wohnfläche fehlt → kein €/m²: {deal.get('title','')[:50]}")
            deal.setdefault("deal_score", "okay")

    return deal


# ---------------------------------------------------------------------------
# Detail-Seite scrapen – der echte Content für Gemini
# ---------------------------------------------------------------------------

def scrape_detail(page: Page, url: str, source_name: str) -> str:
    """
    Öffnet die Detailseite des Inserats und extrahiert:
    - Preis-Tags (Kaufpreis / Kaltmiete / Nebenkosten)
    - Ausstattungsmerkmale (Zimmer, Fläche)
    - Freitext-Beschreibung
    Gibt kombinierten Text zurück (max 3000 Zeichen) für Gemini.
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=35000)
        page.wait_for_timeout(2000)
        parts: list[str] = []

        if "kleinanzeigen.de" in url:
            # 1. Preis
            for sel in [
                "#viewad-price",
                ".addetailsview--detail--price",
                "[data-testid='price-amount']",
                ".boxedarticle--details--price",
                "strong.price",
            ]:
                el = page.query_selector(sel)
                if el:
                    txt = el.inner_text().strip()
                    if txt:
                        parts.append(f"Preis: {txt}")
                        break

            # 2. Ort / Adresse
            for sel in ["#viewad-locality", ".addetailsview--detail--address",
                        ".breadcrumps", "#viewad-address"]:
                el = page.query_selector(sel)
                if el:
                    txt = el.inner_text().strip()
                    if txt:
                        parts.append(f"Ort: {txt}")
                        break

            # 3. Kerndaten (Zimmer, Fläche, Etage etc.)
            for sel in [
                "#viewad-details",
                ".addetailsview--detail--keyfeature",
                ".addetailsview--detail--attributes",
                ".boxedarticle--details",
            ]:
                el = page.query_selector(sel)
                if el:
                    txt = el.inner_text().strip()
                    if txt:
                        parts.append(txt)
                        break

            # 4. Freitext-Beschreibung
            for sel in [
                "#viewad-description-text",
                ".addetailsview--detail--description",
                "#viewad-description",
                "[data-testid='description-text']",
            ]:
                el = page.query_selector(sel)
                if el:
                    txt = el.inner_text().strip()
                    if txt:
                        parts.append(txt)
                        break

        elif "wg-gesucht.de" in url:
            # Cookie-Banner wegklicken
            for btn_sel in [
                "button:has-text('Akzeptieren')",
                "#cmpwelcomebtnyes",
                ".cmplz-accept",
            ]:
                try:
                    page.click(btn_sel, timeout=2000)
                    page.wait_for_timeout(500)
                    break
                except Exception:
                    pass

            # Preis / Titel-Bereich
            for sel in [".headline-title", ".kaltmiete", "h1.headline",
                        ".cost-overview"]:
                el = page.query_selector(sel)
                if el:
                    txt = el.inner_text().strip()
                    if txt:
                        parts.append(txt)

            # Angaben-Tabelle (Fläche, Zimmer, Miete)
            for sel in [".row.print_overview", ".listing-details",
                        ".utilities-overview", "#ad_details"]:
                el = page.query_selector(sel)
                if el:
                    txt = el.inner_text().strip()
                    if txt:
                        parts.append(txt)

            # Freitext
            for sel in ["#freitext", ".panel-body p", ".ad-detail-description",
                        "#ad_description_text"]:
                el = page.query_selector(sel)
                if el:
                    txt = el.inner_text().strip()
                    if txt:
                        parts.append(txt)
                        break

        combined = "\n\n".join(p for p in parts if p).strip()
        if combined:
            log.info(f"   Detail geladen: {len(combined)} Zeichen ({url[:55]})")
            return combined[:3000]
        else:
            # Fallback: ganzer Body-Text
            log.warning(f"   Keine Selektoren getroffen – nutze Body-Text ({url[:55]})")
            return page.inner_text("body")[:2000]

    except Exception as e:
        log.error(f"   Detail-Seite Fehler ({url[:55]}): {e}")
        return ""


# ---------------------------------------------------------------------------
# Gemini KI-Analyse
# ---------------------------------------------------------------------------

def analyse_mit_gemini(title: str, raw_text: str, listing_type_hint: str) -> Optional[dict]:
    if not GEMINI_API_KEY:
        log.error(
            "GEMINI_API_KEY ist nicht gesetzt! "
            "Bitte in GitHub → Settings → Secrets → Actions als GEMINI_API_KEY hinterlegen."
        )
        return None

    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-1.5-flash")
    except Exception as e:
        log.error(f"Gemini-Konfiguration fehlgeschlagen: {e}")
        return None

    prompt = f"""Analysiere diese Immobilienanzeige. Extrahiere alle Zahlen exakt wie im Text.
Antworte NUR mit einem gültigen JSON-Objekt – kein Markdown, kein Kommentar davor/danach.

Typ-Hinweis: {listing_type_hint}
Titel: {title[:300]}
Text:
{raw_text[:2500]}

JSON-Format (alle Zahlen ohne Tausenderpunkte, als reine Zahl):
{{
  "listing_type": "miete" oder "kauf",
  "zimmer": Zimmeranzahl z.B. 2.5 oder null,
  "wohnflaeche_qm": m² als Zahl oder null,
  "kaltmiete": Kaltmiete €/Monat als Zahl oder null,
  "nebenkosten": Nebenkosten €/Monat als Zahl oder null,
  "warmmiete": Warmmiete €/Monat als Zahl oder null,
  "kaufpreis": Kaufpreis in € als Zahl (z.B. 285000 nicht 285.000) oder null,
  "quadrat": Mannheimer Quadrat z.B. "J2" oder null,
  "features": Teilmenge von ["EBK","Balkon","Terrasse","renoviert","möbliert","Neubau","Aufzug","Garage"],
  "einschaetzung": "1-2 Sätze kompakte Deal-Einschätzung auf Deutsch"
}}"""

    for attempt in range(3):
        try:
            resp = model.generate_content(prompt)
            raw_resp = resp.text.strip()

            # Markdown-Fences + führenden/nachfolgenden Text entfernen
            raw_resp = re.sub(r"```(?:json)?\s*|\s*```", "", raw_resp).strip()

            # JSON-Block extrahieren
            json_match = re.search(r"\{[\s\S]*\}", raw_resp)
            if not json_match:
                log.warning(f"   Kein JSON in Gemini-Antwort (Versuch {attempt+1}): "
                             f"{raw_resp[:120]!r}")
                continue

            result: dict = json.loads(json_match.group())

            # Zahlenfelder bereinigen (Gemini schreibt manchmal "285.000" oder "285.000 €")
            for field in ("kaufpreis", "kaltmiete", "nebenkosten", "warmmiete",
                          "wohnflaeche_qm", "zimmer"):
                val = result.get(field)
                if isinstance(val, str):
                    # Tausenderpunkte entfernen, Komma → Punkt
                    cleaned = re.sub(r"[^\d,.]", "", val)
                    # Wenn mehrere Punkte: Tausendertrennzeichen
                    if cleaned.count(".") > 1:
                        cleaned = cleaned.replace(".", "")
                    cleaned = cleaned.replace(",", ".")
                    try:
                        result[field] = float(cleaned) if cleaned else None
                    except ValueError:
                        log.warning(f"   Konnte {field}={val!r} nicht parsen → None")
                        result[field] = None

            log.info(
                f"   ✅ Gemini OK – typ={result.get('listing_type')} | "
                f"kaufpreis={result.get('kaufpreis')} | "
                f"kaltmiete={result.get('kaltmiete')} | "
                f"qm={result.get('wohnflaeche_qm')} | "
                f"zimmer={result.get('zimmer')}"
            )
            return result

        except json.JSONDecodeError as e:
            log.warning(f"   JSON-Parse-Fehler (Versuch {attempt+1}): {e}")
        except Exception as e:
            err = str(e).lower()
            if any(x in err for x in ("quota", "rate", "429", "resource_exhausted")):
                wait = 65 * (attempt + 1)
                log.warning(f"   ⏳ Rate-Limit – warte {wait}s ...")
                time.sleep(wait)
            elif any(x in err for x in ("api_key", "invalid_argument", "unauthenticated")):
                log.error(f"   ❌ Ungültiger GEMINI_API_KEY: {e}")
                return None
            else:
                log.error(f"   ❌ Gemini-Fehler (Versuch {attempt+1}): {e}")
            if attempt == 2:
                return None

    return None


# ---------------------------------------------------------------------------
# Scraper: Kleinanzeigen
# ---------------------------------------------------------------------------

def scrape_kleinanzeigen(
    search_page: Page,
    detail_page: Page,
    source: dict,
    existing_urls: set,
) -> list[dict]:
    raw: list[dict] = []
    base_url = source["url"]
    listing_type = source["listing_type"]

    for page_num in range(1, source["pages"] + 1):
        url = base_url if page_num == 1 else f"{base_url}?pageNum={page_num}"
        log.info(f"   📄 Seite {page_num}: {url[:90]}")
        try:
            search_page.goto(url, wait_until="domcontentloaded", timeout=30000)
            search_page.wait_for_timeout(2000)

            items = search_page.query_selector_all(
                "article.aditem, li.ad-listitem[data-adid]"
            )
            if not items:
                log.info("   Keine Einträge auf dieser Seite – stoppe")
                break

            found = 0
            for item in items:
                try:
                    link_el = item.query_selector(
                        "a[href*='/s-anzeige/'], a.ellipsis, h2 a"
                    )
                    href = (link_el.get_attribute("href") or "") if link_el else ""
                    if not href:
                        continue
                    if href.startswith("/"):
                        href = "https://www.kleinanzeigen.de" + href
                    if href in existing_urls:
                        continue

                    title_el = item.query_selector(
                        "h2, .ellipsis, .aditem-main--middle--headline a"
                    )
                    title = (title_el.inner_text().strip() if title_el else "").strip()
                    if not title:
                        continue

                    # Mannheim-Filter: Inserate aus anderen Städten sofort verwerfen
                    card_text = item.inner_text()
                    if NON_MANNHEIM_PATTERN.search(title + " " + card_text):
                        log.info(f"   ⏭  Nicht Mannheim – übersprungen: {title[:55]}")
                        continue

                    raw.append({
                        "url": href,
                        "source": "kleinanzeigen",
                        "listing_type": listing_type,
                        "title": title[:400],
                    })
                    existing_urls.add(href)   # Duplikat-Schutz innerhalb des Laufs
                    found += 1

                except Exception as e:
                    log.debug(f"   Item-Parse-Fehler: {e}")

            log.info(f"   ✅ {found} neue Inserate auf Seite {page_num}")
            if found == 0:
                break

        except Exception as e:
            log.error(f"   Seitenfehler Kleinanzeigen: {e}")
            break

    # Jetzt Detailseiten öffnen
    log.info(f"   🔍 Öffne {len(raw)} Detail-Seiten ...")
    for entry in raw:
        entry["raw_text"] = scrape_detail(detail_page, entry["url"], "kleinanzeigen")
        time.sleep(1.5)

    return raw


# ---------------------------------------------------------------------------
# Scraper: WG-Gesucht
# ---------------------------------------------------------------------------

def scrape_wg_gesucht(
    search_page: Page,
    detail_page: Page,
    source: dict,
    existing_urls: set,
) -> list[dict]:
    raw: list[dict] = []
    base_url = source["url"]

    # Erste Seite laden + Cookie-Banner wegklicken
    try:
        search_page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
        search_page.wait_for_timeout(2000)
        for btn_sel in ["button:has-text('Akzeptieren')", "#cmpwelcomebtnyes",
                        ".cmplz-accept"]:
            try:
                search_page.click(btn_sel, timeout=3000)
                search_page.wait_for_timeout(600)
                break
            except Exception:
                pass
    except Exception as e:
        log.error(f"   WG-Gesucht Startseite: {e}")
        return raw

    for page_num in range(1, source["pages"] + 1):
        if page_num > 1:
            url = re.sub(r"\.0\.0\.html$", f".0.{page_num - 1}.html", base_url)
            log.info(f"   📄 Seite {page_num}: {url[:90]}")
            try:
                search_page.goto(url, wait_until="domcontentloaded", timeout=30000)
                search_page.wait_for_timeout(2000)
            except Exception as e:
                log.error(f"   WG-Gesucht Seite {page_num}: {e}")
                break
        else:
            log.info(f"   📄 Seite 1: {base_url[:90]}")

        try:
            items = search_page.query_selector_all(
                ".offer_list_item, [id^='liste-'], .wgg-card"
            )
            if not items:
                log.info("   Keine Einträge – stoppe")
                break

            found = 0
            for item in items:
                try:
                    link_el = item.query_selector(
                        "a[href*='/wohnungen-in-'], h3 a, .headline a, h2 a"
                    )
                    href = (link_el.get_attribute("href") or "") if link_el else ""
                    if not href or href in existing_urls:
                        continue
                    if href.startswith("/"):
                        href = "https://www.wg-gesucht.de" + href

                    title_el = item.query_selector(
                        ".truncate_title, h3, .headline, h2"
                    )
                    title = (title_el.inner_text().strip() if title_el else "").strip()
                    if not title:
                        continue

                    raw.append({
                        "url": href,
                        "source": "wg_gesucht",
                        "listing_type": "miete",
                        "title": title[:400],
                    })
                    existing_urls.add(href)
                    found += 1

                except Exception as e:
                    log.debug(f"   Item-Parse-Fehler: {e}")

            log.info(f"   ✅ {found} neue Inserate auf Seite {page_num}")
            if found == 0:
                break

        except Exception as e:
            log.error(f"   Seitenfehler WG-Gesucht: {e}")
            break

    # Detailseiten öffnen
    log.info(f"   🔍 Öffne {len(raw)} Detail-Seiten ...")
    for entry in raw:
        entry["raw_text"] = scrape_detail(detail_page, entry["url"], "wg_gesucht")
        time.sleep(1.5)

    return raw


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> None:
    log.info("=" * 65)
    log.info("🎯  Immobilien-Sniper gestartet")
    log.info(f"⏰  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    log.info(f"🔑  GEMINI_API_KEY: {'✅ gesetzt' if GEMINI_API_KEY else '❌ FEHLT – keine KI-Analyse!'}")
    log.info(f"🏙️  Filter: Mannheim (10 km Radius) | Standard Zimmer: {DEFAULT_ZIMMER}")
    log.info("=" * 65)

    data = load_deals()
    existing_urls = get_existing_urls(data)
    log.info(f"📂  Bestehende Deals: {len(existing_urls)}")

    all_raw: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ],
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
        # Zwei separate Seiten: eine für Suche, eine für Detailseiten
        search_page = ctx.new_page()
        detail_page = ctx.new_page()

        for source in SOURCES:
            if not source.get("enabled", True):
                log.info(f"   ⏭  {source['name']} deaktiviert")
                continue
            log.info(f"\n{'─'*55}")
            log.info(f"🔍  Quelle: {source['name']}")
            try:
                if source["scraper"] == "kleinanzeigen":
                    results = scrape_kleinanzeigen(
                        search_page, detail_page, source, existing_urls
                    )
                elif source["scraper"] == "wg_gesucht":
                    results = scrape_wg_gesucht(
                        search_page, detail_page, source, existing_urls
                    )
                else:
                    results = []
                log.info(f"   📦 {len(results)} neue Inserate von {source['name']}")
                all_raw.extend(results)
            except Exception as e:
                log.error(f"   ❌ Quelle {source['name']} komplett fehlgeschlagen: {e}")

        browser.close()

    # Rate-Limit einhalten
    if len(all_raw) > MAX_NEW_PER_RUN:
        log.info(f"⚡  Begrenze auf {MAX_NEW_PER_RUN} neue Inserate "
                 f"(gefunden: {len(all_raw)})")
        all_raw = all_raw[:MAX_NEW_PER_RUN]

    log.info(f"\n{'─'*55}")
    log.info(f"🤖  Gemini analysiert {len(all_raw)} neue Inserate ...")
    now_iso = datetime.now(timezone.utc).isoformat()
    ai_ok = 0
    ai_fail = 0

    for i, raw in enumerate(all_raw, 1):
        log.info(f"\n   [{i:>3}/{len(all_raw)}] {raw['title'][:65]}")

        ai = analyse_mit_gemini(
            raw["title"],
            raw.get("raw_text", ""),
            raw["listing_type"],
        )
        if ai:
            ai_ok += 1
        else:
            ai_fail += 1

        # Basis-Deal mit sicheren Defaults
        deal: dict = {
            "url":           raw["url"],
            "source":        raw["source"],
            "listing_type":  raw["listing_type"],
            "title":         raw["title"],
            "first_seen":    now_iso,
            "last_seen":     now_iso,
            "features":      [],
            "einschaetzung": "",
            "zimmer":        DEFAULT_ZIMMER,
        }

        # KI-Felder einfügen (nur Nicht-None-Werte, außer features/einschaetzung)
        if ai:
            for k, v in ai.items():
                if k in ("features", "einschaetzung"):
                    if v:
                        deal[k] = v
                elif v is not None:
                    deal[k] = v

            # listing_type aus KI nur übernehmen wenn valide
            if ai.get("listing_type") in ("kauf", "miete"):
                deal["listing_type"] = ai["listing_type"]

        # Finanzkennzahlen lokal berechnen (unabhängig von Gemini)
        deal = berechne_scores(deal)

        # Schema-Check: Pflichtfelder loggen
        if deal["listing_type"] == "kauf" and not deal.get("kaufpreis"):
            log.warning(f"   ⚠️  kaufpreis=None → Deal wird als 'skip' markiert")
        if deal["listing_type"] == "miete" and not deal.get("kaltmiete"):
            log.warning(f"   ⚠️  kaltmiete=None → kein €/m² berechenbar")

        data["deals"].append(deal)
        time.sleep(4)   # Gemini Free-Tier: max 15 req/min

    log.info(f"\n{'='*65}")
    log.info(f"✅  {len(all_raw)} neue Deals verarbeitet")
    log.info(f"   KI erfolgreich: {ai_ok} | KI fehlgeschlagen: {ai_fail}")
    log.info(f"   Gesamt in deals.json: {len(data['deals'])}")
    save_deals(data)


if __name__ == "__main__":
    run()
