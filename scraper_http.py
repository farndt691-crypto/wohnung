"""
scraper_http.py – Leichtgewichtiger HTTP-Scraper ohne Headless-Browser
======================================================================
Nutzt httpx + BeautifulSoup. Vercel-kompatibel (kein Playwright nötig).
Läuft innerhalb der 10s-Timeout-Grenze für Serverless-Funktionen.
"""

import logging
import random
import re
import time
from datetime import datetime
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from calculator import berechne_kennzahlen
from config import PLAYWRIGHT, QUADRAT_REGEX, SCRAPING_SOURCES
from database import upsert_listing, url_exists

logger = logging.getLogger(__name__)

# Timeout pro HTTP-Request (Sekunden) – Vercel hat 10s Gesamt-Limit
REQUEST_TIMEOUT = 8.0


# ---------------------------------------------------------------------------
# Hilfsfunktionen (gemeinsam)
# ---------------------------------------------------------------------------
def _get_headers() -> dict:
    return {
        "User-Agent": random.choice(PLAYWRIGHT["user_agents"]),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
    }


def _extract_price(text: str) -> Optional[float]:
    text = text.replace("\xa0", "").replace(".", "").replace(",", ".").replace(" ", "")
    match = re.search(r"(\d{4,7}(?:\.\d+)?)", text)
    if match:
        val = float(match.group(1))
        if 30_000 <= val <= 2_000_000:
            return val
    return None


def _extract_area(text: str) -> Optional[float]:
    text = text.replace(",", ".")
    match = re.search(r"(\d+(?:\.\d+)?)\s*m[²2]?", text, re.IGNORECASE)
    if match:
        val = float(match.group(1))
        if 15 <= val <= 500:
            return val
    return None


def _extract_quadrat(text: str) -> Optional[str]:
    match = re.search(QUADRAT_REGEX, text, re.IGNORECASE)
    return match.group(1).upper() if match else None


def _fetch(url: str) -> Optional[str]:
    """HTTP-GET mit Retry-Logik. Gibt HTML zurück oder None."""
    for attempt in range(2):
        try:
            with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
                resp = client.get(url, headers=_get_headers())
                if resp.status_code == 200:
                    return resp.text
                elif resp.status_code == 429:
                    logger.warning(f"Rate-limit ({url}), warte 3s …")
                    time.sleep(3)
                else:
                    logger.warning(f"HTTP {resp.status_code} für {url}")
                    return None
        except Exception as e:
            logger.error(f"Fetch-Fehler (Versuch {attempt+1}): {e}")
            time.sleep(1)
    return None


# ---------------------------------------------------------------------------
# Kleinanzeigen Scraper
# ---------------------------------------------------------------------------
def scrape_kleinanzeigen_page(url: str) -> list[dict]:
    """Scrapt eine Suchergebnisseite von kleinanzeigen.de."""
    html = _fetch(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    results = []

    # Inserat-Container (Kleinanzeigen verwendet article.aditem oder li-Einträge)
    items = soup.select("article.aditem, li.ad-listitem[data-adid]")
    if not items:
        # Fallback: alle Links die wie Inserat-URLs aussehen
        items = soup.select('[id^="ad-"]')

    logger.info(f"[Kleinanzeigen] {len(items)} Items auf {url[:60]}")

    for item in items:
        try:
            # Link & URL
            link = item.select_one("a[href*='/s-anzeige/'], a.ellipsis, h2 a")
            if not link:
                link = item.select_one("a[href]")
            href = link.get("href", "") if link else ""
            if not href:
                continue
            if href.startswith("/"):
                href = "https://www.kleinanzeigen.de" + href
            # Nur Kauf-Inserate
            if not any(kw in href.lower() for kw in ["/s-anzeige/", "immobilien", "wohnung"]):
                continue

            # Titel
            title_el = item.select_one("h2, .ellipsis, .aditem-main--middle--headline a")
            title = title_el.get_text(strip=True) if title_el else ""
            if not title:
                continue

            # Preis
            price_el = item.select_one(
                ".aditem-main--middle--price-shipping--price, "
                ".price-rating, p.aditem-main--middle--price"
            )
            price_text = price_el.get_text(strip=True) if price_el else ""

            # Beschreibung
            desc_el = item.select_one(
                ".aditem-main--middle--description, "
                "p.aditem-main--middle--description"
            )
            desc_text = desc_el.get_text(strip=True) if desc_el else ""

            full_text = f"{title} {desc_text}"
            quadrat = _extract_quadrat(full_text)
            if not quadrat and "mannheim" not in full_text.lower():
                continue

            results.append({
                "url":           href,
                "source":        "kleinanzeigen",
                "title":         title[:500],
                "quadrat":       quadrat,
                "kaufpreis":     _extract_price(price_text),
                "wohnflaeche_qm": _extract_area(full_text),
            })
        except Exception as e:
            logger.debug(f"Item-Fehler: {e}")

    return results


def scrape_kleinanzeigen(source_cfg: dict) -> list[dict]:
    results = []
    base_url = source_cfg["search_url"]
    for page_num in range(1, source_cfg.get("max_pages", 2) + 1):
        url = base_url if page_num == 1 else f"{base_url}?pageNum={page_num}"
        page_results = scrape_kleinanzeigen_page(url)
        results.extend(page_results)
        if not page_results:
            break
    return results


# ---------------------------------------------------------------------------
# Immonet Scraper
# ---------------------------------------------------------------------------
def scrape_immonet_page(url: str) -> list[dict]:
    """Scrapt eine Suchergebnisseite von immonet.de."""
    html = _fetch(url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    results = []

    items = soup.select("div[id^='selObject'], .item-container, article.result-list-entry")
    logger.info(f"[Immonet] {len(items)} Items auf {url[:60]}")

    for item in items:
        try:
            link = item.select_one("a.lnkToObj, a[href*='/expose/'], h2 a")
            href = link.get("href", "") if link else ""
            if not href:
                continue
            if href.startswith("/"):
                href = "https://www.immonet.de" + href

            title_el = item.select_one("a.lnkToObj, h2, .headline")
            title = title_el.get_text(strip=True) if title_el else ""
            if not title:
                continue

            price_el = item.select_one(".price, [class*='price'], [data-testid='price']")
            price_text = price_el.get_text(strip=True) if price_el else ""

            area_el = item.select_one("[class*='area'], [class*='sqm'], [data-testid='area']")
            area_text = area_el.get_text(strip=True) if area_el else ""

            full_text = f"{title} {price_text} {area_text}"
            quadrat = _extract_quadrat(full_text)

            results.append({
                "url":            href,
                "source":         "immonet",
                "title":          title[:500],
                "quadrat":        quadrat,
                "kaufpreis":      _extract_price(price_text),
                "wohnflaeche_qm": _extract_area(area_text or full_text),
            })
        except Exception as e:
            logger.debug(f"Immonet Item-Fehler: {e}")

    return results


def scrape_immonet(source_cfg: dict) -> list[dict]:
    results = []
    base_url = source_cfg["search_url"]
    for page_num in range(1, source_cfg.get("max_pages", 2) + 1):
        url = base_url if page_num == 1 else f"{base_url}&page={page_num}"
        page_results = scrape_immonet_page(url)
        results.extend(page_results)
        if not page_results:
            break
    return results


# ---------------------------------------------------------------------------
# Haupt-Scraping-Funktion (synchron, für Vercel Serverless)
# ---------------------------------------------------------------------------
def run_scraping_sync() -> dict:
    """
    Führt einen kompletten Scraping-Durchlauf synchron durch.
    Wird vom /api/cron Endpunkt und dem manuellen Button aufgerufen.
    """
    start_time = datetime.utcnow()
    stats = {"new": 0, "updated": 0, "errors": 0, "sources": []}

    logger.info("=== Scraping-Lauf gestartet ===")

    for source_cfg in SCRAPING_SOURCES:
        if not source_cfg.get("enabled", True):
            continue

        source_name = source_cfg["name"]
        logger.info(f"Starte: {source_name}")

        try:
            if source_name == "kleinanzeigen":
                raw_listings = scrape_kleinanzeigen(source_cfg)
            elif source_name == "immonet":
                raw_listings = scrape_immonet(source_cfg)
            else:
                continue

            stats["sources"].append({"name": source_name, "found": len(raw_listings)})
            logger.info(f"{source_name}: {len(raw_listings)} Rohdaten")

            for raw in raw_listings:
                try:
                    kennzahlen = berechne_kennzahlen(
                        kaufpreis=raw.get("kaufpreis"),
                        wohnflaeche_qm=raw.get("wohnflaeche_qm"),
                        kaltmiete_monat_inserat=raw.get("kaltmiete_monat"),
                        quadrat=raw.get("quadrat"),
                    )
                    existed = url_exists(raw["url"])
                    upsert_listing({**raw, **kennzahlen, "active": True})
                    if existed:
                        stats["updated"] += 1
                    else:
                        stats["new"] += 1
                except Exception as e:
                    logger.error(f"Listing-Fehler: {e}")
                    stats["errors"] += 1

        except Exception as e:
            logger.error(f"Quelle {source_name} Fehler: {e}")
            stats["errors"] += 1

    duration = (datetime.utcnow() - start_time).total_seconds()
    logger.info(f"=== Fertig in {duration:.1f}s | Neu: {stats['new']} ===")
    return {**stats, "duration_s": round(duration, 1), "timestamp": start_time.isoformat()}
