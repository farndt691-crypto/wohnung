"""
scraper.py – Immobilien-Sniper (GitHub Actions Edition)
=======================================================
Scrapt Kleinanzeigen + WG-Gesucht mit Playwright,
analysiert neue Inserate mit Google Gemini (kostenlos, 1.5-flash),
schreibt alles nach data/deals.json → wird ins Repo gepusht.
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import google.generativeai as genai
from playwright.sync_api import Page, sync_playwright

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")
DEALS_PATH = Path("data/deals.json")
MANNHEIM_BENCHMARK_QM = 14.50

KEYWORD_AUFSCHLAEGE: dict[str, float] = {
    "EBK": 1.00, "Balkon": 0.50, "Terrasse": 0.50,
    "renoviert": 1.00, "möbliert": 3.00, "Neubau": 2.00,
    "Aufzug": 0.50, "Garage": 0.30,
}

SOURCES = [
    {
        "name": "kleinanzeigen_kauf", "scraper": "kleinanzeigen",
        "listing_type": "kauf", "enabled": True, "pages": 3,
        "url": "https://www.kleinanzeigen.de/s-wohnung-kaufen/mannheim/c196l9409",
    },
    {
        "name": "kleinanzeigen_miete", "scraper": "kleinanzeigen",
        "listing_type": "miete", "enabled": True, "pages": 3,
        "url": "https://www.kleinanzeigen.de/s-wohnung-mieten/mannheim/c203l9409",
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
        except Exception:
            pass
    return {"last_updated": None, "deals": []}


def save_deals(data: dict) -> None:
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(DEALS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"💾  {len(data['deals'])} Deals gespeichert → {DEALS_PATH}")


def get_existing_urls(data: dict) -> set[str]:
    return {d["url"] for d in data.get("deals", [])}


# ---------------------------------------------------------------------------
# Deal-Score-Berechnung (lokal)
# ---------------------------------------------------------------------------

def berechne_scores(deal: dict) -> dict:
    if deal.get("listing_type") == "kauf":
        kaufpreis = deal.get("kaufpreis")
        kaltmiete = deal.get("kaltmiete")
        wfl = deal.get("wohnflaeche_qm")
        if not kaufpreis:
            return deal
        if not kaltmiete and wfl:
            kaltmiete = wfl * 12.00
        if kaltmiete:
            bankrate = (kaufpreis * 1.10 * 0.05) / 12
            cashflow = kaltmiete - bankrate - 50
            deal["bankrate_monat"] = round(bankrate, 2)
            deal["cashflow_monat"] = round(cashflow, 2)
            deal["deal_score"] = (
                "strong_buy" if cashflow >= 0 else
                "watch" if cashflow >= -150 else "skip"
            )
    elif deal.get("listing_type") == "miete":
        kaltmiete = deal.get("kaltmiete")
        wfl = deal.get("wohnflaeche_qm")
        features = deal.get("features", [])
        if kaltmiete and wfl and wfl > 0:
            kaltmiete_qm = kaltmiete / wfl
            deal["kaltmiete_qm"] = round(kaltmiete_qm, 2)
            aufschlag = sum(KEYWORD_AUFSCHLAEGE.get(f, 0.0) for f in features)
            expected_qm = MANNHEIM_BENCHMARK_QM + aufschlag
            deal["expected_market_qm"] = round(expected_qm, 2)
            deviation = ((kaltmiete_qm - expected_qm) / expected_qm) * 100
            deal["deal_deviation_pct"] = round(deviation, 1)
            deal["deal_score"] = (
                "gut" if deviation <= -10 else
                "okay" if deviation <= 10 else "teuer"
            )
    return deal


# ---------------------------------------------------------------------------
# Gemini KI-Analyse
# ---------------------------------------------------------------------------

def analyse_mit_gemini(title: str, raw_text: str, listing_type_hint: str) -> Optional[dict]:
    if not GEMINI_API_KEY:
        print("   ⚠️  GEMINI_API_KEY fehlt – KI-Analyse übersprungen")
        return None

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-1.5-flash")

    prompt = f"""Analysiere diese Immobilienanzeige aus Mannheim.
Antworte NUR mit einem gültigen JSON-Objekt – kein Markdown, keine Erklärung.

Typ-Hinweis: {listing_type_hint}
Titel: {title[:300]}
Text: {raw_text[:1500]}

Format:
{{
  "listing_type": "miete" oder "kauf",
  "zimmer": Zimmeranzahl als Zahl (z.B. 2.5) oder null,
  "wohnflaeche_qm": m² als Zahl oder null,
  "kaltmiete": Kaltmiete €/Monat als Zahl oder null,
  "nebenkosten": Nebenkosten €/Monat als Zahl oder null,
  "warmmiete": Warmmiete €/Monat als Zahl oder null,
  "kaufpreis": Kaufpreis € als Zahl oder null,
  "quadrat": Mannheimer Quadrat z.B. "J2" oder null,
  "features": Teilmenge von ["EBK","Balkon","Terrasse","renoviert","möbliert","Neubau","Aufzug","Garage"],
  "einschaetzung": "1-2 Sätze Deal-Einschätzung auf Deutsch"
}}"""

    try:
        resp = model.generate_content(prompt)
        text = re.sub(r"```(?:json)?\s*|\s*```", "", resp.text).strip()
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"   ⚠️  JSON-Parse-Fehler: {e}")
        return None
    except Exception as e:
        print(f"   ⚠️  Gemini-Fehler: {e}")
        return None


# ---------------------------------------------------------------------------
# Scraper: Kleinanzeigen
# ---------------------------------------------------------------------------

def scrape_kleinanzeigen(page: Page, source: dict, existing_urls: set) -> list[dict]:
    raw: list[dict] = []
    base_url = source["url"]
    for page_num in range(1, source["pages"] + 1):
        url = base_url if page_num == 1 else f"{base_url}?pageNum={page_num}"
        print(f"   📄 Seite {page_num}: {url[:70]}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(1500)
            items = page.query_selector_all("article.aditem, li.ad-listitem[data-adid]")
            if not items:
                break
            found = 0
            for item in items:
                try:
                    link_el = item.query_selector("a[href*='/s-anzeige/'], a.ellipsis, h2 a, a[href]")
                    href = (link_el.get_attribute("href") or "") if link_el else ""
                    if not href or href in existing_urls:
                        continue
                    if href.startswith("/"):
                        href = "https://www.kleinanzeigen.de" + href
                    title_el = item.query_selector("h2, .ellipsis, .aditem-main--middle--headline a")
                    title = title_el.inner_text().strip() if title_el else ""
                    if not title:
                        continue
                    raw.append({
                        "url": href, "source": "kleinanzeigen",
                        "listing_type": source["listing_type"],
                        "title": title[:400], "raw_text": item.inner_text()[:2000],
                    })
                    found += 1
                except Exception:
                    pass
            print(f"   ✅ {found} neue Inserate")
            if found == 0:
                break
        except Exception as e:
            print(f"   ⚠️  Fehler: {e}")
            break
    return raw


# ---------------------------------------------------------------------------
# Scraper: WG-Gesucht
# ---------------------------------------------------------------------------

def scrape_wg_gesucht(page: Page, source: dict, existing_urls: set) -> list[dict]:
    raw: list[dict] = []
    base_url = source["url"]
    for page_num in range(1, source["pages"] + 1):
        url = (
            base_url if page_num == 1
            else re.sub(r"\.0\.0\.html$", f".0.{page_num - 1}.html", base_url)
        )
        print(f"   📄 Seite {page_num}: {url[:70]}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            try:
                page.click("button:has-text('Akzeptieren')", timeout=3000)
                page.wait_for_timeout(500)
            except Exception:
                pass
            page.wait_for_timeout(1500)
            items = page.query_selector_all(".offer_list_item, [id^='liste-'], .wgg-card")
            if not items:
                break
            found = 0
            for item in items:
                try:
                    link_el = item.query_selector("a[href*='/wohnungen-in-'], h3 a, .headline a, h2 a")
                    href = (link_el.get_attribute("href") or "") if link_el else ""
                    if not href or href in existing_urls:
                        continue
                    if href.startswith("/"):
                        href = "https://www.wg-gesucht.de" + href
                    title_el = item.query_selector(".truncate_title, h3, .headline, h2")
                    title = title_el.inner_text().strip() if title_el else ""
                    if not title:
                        continue
                    raw.append({
                        "url": href, "source": "wg_gesucht",
                        "listing_type": "miete",
                        "title": title[:400], "raw_text": item.inner_text()[:2000],
                    })
                    found += 1
                except Exception:
                    pass
            print(f"   ✅ {found} neue Inserate")
            if found == 0:
                break
        except Exception as e:
            print(f"   ⚠️  Fehler: {e}")
            break
    return raw


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> None:
    print("=" * 60)
    print("🎯  Immobilien-Sniper gestartet")
    print(f"⏰  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    data = load_deals()
    existing_urls = get_existing_urls(data)
    print(f"📂  Bestehende Deals: {len(existing_urls)}")

    all_raw: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            locale="de-DE",
        )
        page = ctx.new_page()

        for source in SOURCES:
            if not source.get("enabled", True):
                continue
            print(f"\n🔍  {source['name']}")
            if source["scraper"] == "kleinanzeigen":
                results = scrape_kleinanzeigen(page, source, existing_urls)
            elif source["scraper"] == "wg_gesucht":
                results = scrape_wg_gesucht(page, source, existing_urls)
            else:
                results = []
            all_raw.extend(results)

        browser.close()

    print(f"\n🤖  Gemini analysiert {len(all_raw)} neue Inserate ...")
    now_iso = datetime.now(timezone.utc).isoformat()

    for i, raw in enumerate(all_raw, 1):
        print(f"   [{i:>3}/{len(all_raw)}] {raw['title'][:55]}")
        ai = analyse_mit_gemini(raw["title"], raw["raw_text"], raw["listing_type"])

        deal: dict = {
            "url": raw["url"], "source": raw["source"],
            "listing_type": raw["listing_type"], "title": raw["title"],
            "first_seen": now_iso, "last_seen": now_iso,
            "features": [], "einschaetzung": "",
        }
        if ai:
            deal.update({k: v for k, v in ai.items() if v is not None})
            if ai.get("listing_type") in ("kauf", "miete"):
                deal["listing_type"] = ai["listing_type"]

        deal = berechne_scores(deal)
        data["deals"].append(deal)
        existing_urls.add(raw["url"])
        time.sleep(4)  # Gemini Free-Tier: 15 req/min

    print(f"\n{'=' * 60}")
    print(f"✅  {len(all_raw)} neue Deals verarbeitet")
    save_deals(data)


if __name__ == "__main__":
    run()
