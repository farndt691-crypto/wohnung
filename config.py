"""
config.py – Zentrale Konfiguration für Immobilien-Sniper
=========================================================
Alle Parameter hier anpassen – kein Code anfassen nötig.
"""
import os
import re

# ---------------------------------------------------------------------------
# Supabase (Datenbank – bleibt in der Cloud)
# ---------------------------------------------------------------------------
SUPABASE_URL = os.environ.get(
    "SUPABASE_URL", "https://fskfbipjhxxrxccmdpga.supabase.co"
)
SUPABASE_KEY = os.environ.get(
    "SUPABASE_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZza2ZiaXBqaHh4cnhjY21kcGdhIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3OTI2Njg0NiwiZXhwIjoyMDk0ODQyODQ2fQ.-iAI_DFCMXiq-cAZ1_KfWKHXXu9sJPWpVrozMy5Tv7E",
)

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
SERVER_HOST = "0.0.0.0"
SERVER_PORT = int(os.environ.get("PORT", 8000))

# ---------------------------------------------------------------------------
# Mannheimer Quadrate – Regex
# ---------------------------------------------------------------------------
QUADRAT_REGEX = r"(?<![A-Za-z0-9])([A-HK-U][1-9])(?![A-Za-z0-9])"

# ---------------------------------------------------------------------------
# Mietspiegel Mannheim – Kauf-Renditerechnung (Kaltmiete €/m² je Quadrat)
# ---------------------------------------------------------------------------
MIETSPIEGEL: dict[str, float] = {
    "A": 13.50, "B": 13.50, "C": 13.50, "D": 13.50,
    "E": 13.00, "F": 13.00, "G": 13.00, "H": 13.00,
    "K": 12.50, "L": 12.50, "M": 12.50, "N": 12.50,
    "O": 12.00, "P": 12.00, "Q": 12.00, "R": 12.00,
    "S": 11.50, "T": 11.50, "U": 11.50,
}
MIETSPIEGEL_DEFAULT = 12.00

# ---------------------------------------------------------------------------
# Kauf-Finanzierung
# ---------------------------------------------------------------------------
FINANCING = {
    "nebenkosten_factor": 1.10,   # 10 % Kaufnebenkosten
    "zins":               0.035,  # 3,5 % Zinsen p.a.
    "tilgung":            0.015,  # 1,5 % Tilgung p.a.
    "hausgeld":           50.0,   # €/Monat Rücklagen
}

# ---------------------------------------------------------------------------
# Miete – Deal-Analyse
# ---------------------------------------------------------------------------
# Basis-Benchmark Mannheim: durchschn. Kaltmiete €/m² für Wohnungen ≤60 m²
MANNHEIM_BENCHMARK_QM: float = 14.50

# Aufschläge in €/m² je Feature (wird auf Benchmark addiert)
KEYWORD_AUFSCHLAEGE: dict[str, float] = {
    "EBK":       1.00,
    "Balkon":    0.50,
    "Terrasse":  0.50,
    "renoviert": 1.00,
    "möbliert":  3.00,
    "Neubau":    2.00,
    "Aufzug":    0.50,
    "Garage":    0.30,
}

# Regex-Patterns für Keyword-Erkennung (Groß-/Kleinschreibung ignoriert)
KEYWORD_PATTERNS: dict[str, re.Pattern] = {
    "EBK":       re.compile(r"e[\s\-]?b[\s\-]?k|einbau[\s\-]?k[üu]che", re.I),
    "Balkon":    re.compile(r"balkon", re.I),
    "Terrasse":  re.compile(r"terrasse|loggia", re.I),
    "renoviert": re.compile(r"renoviert|saniert|modernisiert|kernsaniert", re.I),
    "möbliert":  re.compile(r"m[öo]bliert|voll\s*ausgestattet", re.I),
    "Neubau":    re.compile(r"neubau|erstbezug", re.I),
    "Aufzug":    re.compile(r"aufzug|fahrstuhl|lift\b", re.I),
    "Garage":    re.compile(r"tiefgarage|garage|stellplatz|parkplatz|carport", re.I),
}

# ---------------------------------------------------------------------------
# Scraping – User-Agents
# ---------------------------------------------------------------------------
PLAYWRIGHT = {
    "user_agents": [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    ]
}

# ---------------------------------------------------------------------------
# Scraping-Quellen (4 Quellen, modular)
# ---------------------------------------------------------------------------
SCRAPING_SOURCES = [
    # ── KAUFOBJEKTE ──────────────────────────────────────────────────────
    {
        "name": "kleinanzeigen_kauf", "scraper": "kleinanzeigen",
        "listing_type": "kauf", "enabled": True, "max_pages": 3,
        "search_url": "https://www.kleinanzeigen.de/s-wohnung-kaufen/mannheim/c196l9409",
    },
    {
        "name": "immonet_kauf", "scraper": "immonet",
        "listing_type": "kauf", "enabled": True, "max_pages": 3,
        "search_url": (
            "https://www.immonet.de/immobiliensuche/sel.do"
            "?city=2075&listsize=26&objecttype=1&sortby=19&parentcat=1"
        ),
    },
    {
        "name": "immoscout24_kauf", "scraper": "immoscout24",
        "listing_type": "kauf", "enabled": True, "max_pages": 3,
        "search_url": (
            "https://www.immobilienscout24.de/Suche/de/"
            "baden-wuerttemberg/mannheim/wohnung-kaufen"
        ),
    },
    # ── MIETOBJEKTE ──────────────────────────────────────────────────────
    {
        "name": "kleinanzeigen_miete", "scraper": "kleinanzeigen",
        "listing_type": "miete", "enabled": True, "max_pages": 3,
        "search_url": "https://www.kleinanzeigen.de/s-wohnung-mieten/mannheim/c203l9409",
    },
    {
        "name": "immonet_miete", "scraper": "immonet",
        "listing_type": "miete", "enabled": True, "max_pages": 3,
        "search_url": (
            "https://www.immonet.de/immobiliensuche/sel.do"
            "?city=2075&listsize=26&objecttype=1&sortby=19&parentcat=2"
        ),
    },
    {
        "name": "immoscout24_miete", "scraper": "immoscout24",
        "listing_type": "miete", "enabled": True, "max_pages": 3,
        "search_url": (
            "https://www.immobilienscout24.de/Suche/de/"
            "baden-wuerttemberg/mannheim/wohnung-mieten"
        ),
    },
    {
        "name": "wg_gesucht", "scraper": "wg_gesucht",
        "listing_type": "miete", "enabled": True, "max_pages": 3,
        "search_url": "https://www.wg-gesucht.de/wohnungen-in-Mannheim.124.2.0.0.html",
    },
]
