"""
database.py – Supabase REST-Client (kein direkter PostgreSQL-Port!)
====================================================================
Nutzt die offizielle supabase-py Bibliothek über HTTPS Port 443.
Funktioniert zuverlässig in Vercel Serverless (kein Port 5432 nötig).
DSGVO: Nur Sachdaten, keine personenbezogenen Informationen.
"""

import os
from datetime import datetime, timezone
from typing import Optional

from supabase import create_client, Client

# ---------------------------------------------------------------------------
# Supabase-Verbindung
# ---------------------------------------------------------------------------
SUPABASE_URL = os.environ.get(
    "SUPABASE_URL",
    "https://fskfbipjhxxrxccmdpga.supabase.co"
)
SUPABASE_KEY = os.environ.get(
    "SUPABASE_KEY",
    # service_role key (voller Zugriff – wird in Vercel als Secret gesetzt)
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZza2ZiaXBqaHh4cnhjY21kcGdhIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3OTI2Njg0NiwiZXhwIjoyMDk0ODQyODQ2fQ.-iAI_DFCMXiq-cAZ1_KfWKHXXu9sJPWpVrozMy5Tv7E"
)

_client: Optional[Client] = None


def get_client() -> Client:
    """Singleton Supabase-Client."""
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


# ---------------------------------------------------------------------------
# DB Init (Tabelle wurde bereits via API erstellt – hier nur Verbindungstest)
# ---------------------------------------------------------------------------
def init_db() -> None:
    get_client().table("listings").select("id").limit(1).execute()


# ---------------------------------------------------------------------------
# CRUD-Operationen
# ---------------------------------------------------------------------------
def url_exists(url: str) -> bool:
    result = get_client().table("listings").select("id").eq("url", url).execute()
    return len(result.data) > 0


def upsert_listing(data: dict) -> dict:
    """Fügt ein Listing ein oder aktualisiert es (basierend auf URL)."""
    now = datetime.now(timezone.utc).isoformat()
    data.setdefault("first_seen", now)
    data["last_seen"] = now
    result = (
        get_client()
        .table("listings")
        .upsert(data, on_conflict="url")
        .execute()
    )
    return result.data[0] if result.data else data


def get_listings(
    deal_score: Optional[str] = None,
    source: Optional[str] = None,
    active_only: bool = True,
    limit: int = 200,
) -> list[dict]:
    query = get_client().table("listings").select("*")
    if active_only:
        query = query.eq("active", True)
    if deal_score:
        query = query.eq("deal_score", deal_score)
    if source:
        query = query.eq("source", source)
    result = query.order("first_seen", desc=True).limit(limit).execute()
    return result.data or []


def get_stats() -> dict:
    result = (
        get_client()
        .table("listings")
        .select("deal_score, last_seen")
        .eq("active", True)
        .execute()
    )
    rows = result.data or []
    last = max((r.get("last_seen", "") for r in rows), default=None)
    return {
        "total":      len(rows),
        "strong_buy": sum(1 for r in rows if r.get("deal_score") == "strong_buy"),
        "watch":      sum(1 for r in rows if r.get("deal_score") == "watch"),
        "skip":       sum(1 for r in rows if r.get("deal_score") == "skip"),
        "last_run":   last[:16].replace("T", " ") if last else "Noch nie",
    }


def patch_listing_miete(listing_id: int, kaltmiete_monat: float) -> Optional[dict]:
    result = (
        get_client()
        .table("listings")
        .select("*")
        .eq("id", listing_id)
        .execute()
    )
    if not result.data:
        return None
    return result.data[0]


def update_listing(listing_id: int, updates: dict) -> Optional[dict]:
    updates["last_seen"] = datetime.now(timezone.utc).isoformat()
    result = (
        get_client()
        .table("listings")
        .update(updates)
        .eq("id", listing_id)
        .execute()
    )
    return result.data[0] if result.data else None
