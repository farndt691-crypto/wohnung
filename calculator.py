"""
calculator.py – Rendite-Rechner für den Immobilien-Sniper
=========================================================
Alle Berechnungen zentral und testbar.
"""

from config import (
    DEFAULT_WOHNFLAECHE_QM,
    DEAL_SCORE_THRESHOLDS,
    FINANCING,
    MIETSPIEGEL_DEFAULT_EUR_PRO_QM,
    MIETSPIEGEL_EUR_PRO_QM,
)


def get_mietspiegel_qm(quadrat: str | None) -> float:
    """
    Gibt den €/m²-Wert aus dem Mietspiegel zurück.
    Nutzt den ersten Buchstaben des Quadrats (z. B. "Q" aus "Q4").
    """
    if quadrat and len(quadrat) >= 1:
        buchstabe = quadrat[0].upper()
        return MIETSPIEGEL_EUR_PRO_QM.get(buchstabe, MIETSPIEGEL_DEFAULT_EUR_PRO_QM)
    return MIETSPIEGEL_DEFAULT_EUR_PRO_QM


def estimate_rent(
    quadrat: str | None,
    wohnflaeche_qm: float | None,
) -> tuple[float, bool]:
    """
    Schätzt die monatliche Kaltmiete.

    Returns:
        (kaltmiete_monat, geschaetzt)
        geschaetzt=True wenn aus Mietspiegel, False wenn aus Inserat
    """
    flaeche = wohnflaeche_qm if wohnflaeche_qm and wohnflaeche_qm > 10 else DEFAULT_WOHNFLAECHE_QM
    miete_qm = get_mietspiegel_qm(quadrat)
    return round(flaeche * miete_qm, 2), True


def berechne_bankrate(kaufpreis: float) -> float:
    """
    Monatliche Annuitätsrate bei Vollfinanzierung (110%).
    Formel: (Kaufpreis × 1.10) × (Zins + Tilgung) / 12
    """
    f = FINANCING
    darlehensbetrag = kaufpreis * f["nebenkosten_factor"]
    jahresrate = darlehensbetrag * (f["zins"] + f["tilgung"])
    return round(jahresrate / 12, 2)


def berechne_cashflow(
    kaltmiete_monat: float,
    bankrate_monat: float,
) -> float:
    """
    Monatlicher Cashflow nach Bankrate und nicht-umlagefähigem Hausgeld.
    Cashflow = Kaltmiete - Bankrate - Hausgeld
    """
    hausgeld = FINANCING["nicht_umlagefaehiges_hausgeld"]
    return round(kaltmiete_monat - bankrate_monat - hausgeld, 2)


def berechne_bruttomietrendite(
    kaufpreis: float,
    kaltmiete_monat: float,
) -> float:
    """
    Bruttomietrendite in Prozent: (Kaltmiete × 12) / Kaufpreis × 100
    Schnellfilter: Objekte < 4% sind in der Regel unattraktiv.
    """
    if kaufpreis <= 0:
        return 0.0
    return round((kaltmiete_monat * 12) / kaufpreis * 100, 2)


def berechne_deal_score(cashflow_monat: float) -> str:
    """
    Ampelsystem basierend auf monatlichem Cashflow.

    🟢 strong_buy : Cashflow >= 0  (Objekt trägt sich selbst)
    🟡 watch      : -150 <= CF < 0 (knapp negativ, evtl. mit EK darstellbar)
    🔴 skip       : CF < -150      (nicht darstellbar)
    """
    t = DEAL_SCORE_THRESHOLDS
    if cashflow_monat >= t["strong_buy_min"]:
        return "strong_buy"
    elif cashflow_monat >= t["watch_min"]:
        return "watch"
    else:
        return "skip"


def berechne_kennzahlen(
    kaufpreis: float | None,
    wohnflaeche_qm: float | None,
    kaltmiete_monat_inserat: float | None,
    quadrat: str | None,
) -> dict:
    """
    Hauptfunktion: Berechnet alle Kennzahlen für ein Listing.

    Args:
        kaufpreis               : Kaufpreis in € (aus Inserat)
        wohnflaeche_qm          : Wohnfläche m² (aus Inserat, kann None sein)
        kaltmiete_monat_inserat : Kaltmiete aus Inserat (selten vorhanden)
        quadrat                 : Erkanntes Mannheimer Quadrat (z. B. "Q4")

    Returns:
        dict mit allen berechneten Werten oder leeres dict bei fehlenden Daten
    """
    if not kaufpreis or kaufpreis <= 0:
        return {}

    # Miete: aus Inserat nehmen, sonst schätzen
    if kaltmiete_monat_inserat and kaltmiete_monat_inserat > 0:
        kaltmiete = kaltmiete_monat_inserat
        geschaetzt = False
    else:
        kaltmiete, geschaetzt = estimate_rent(quadrat, wohnflaeche_qm)

    bankrate    = berechne_bankrate(kaufpreis)
    cashflow    = berechne_cashflow(kaltmiete, bankrate)
    rendite     = berechne_bruttomietrendite(kaufpreis, kaltmiete)
    score       = berechne_deal_score(cashflow)

    return {
        "kaltmiete_monat":          kaltmiete,
        "miete_geschaetzt":         geschaetzt,
        "bankrate_monat":           bankrate,
        "cashflow_monat":           cashflow,
        "bruttomietrendite_pct":    rendite,
        "deal_score":               score,
    }
