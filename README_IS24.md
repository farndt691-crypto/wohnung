# ImmoScout24 (IS24) – Einrichtung & Hintergrund

IS24 ist jetzt als Quelle aktiviert (`config.json` → `immoscout24_kauf` + `immoscout24_miete`)
und der Scraper-Code dafür ist vorhanden (`scraper.py` → `scrape_immoscout24_async`).
Damit IS24 tatsächlich Daten liefert, sind **zwei GitHub-Secrets** nötig.

---

## Warum IS24 ein Sonderfall ist

IS24 sitzt hinter **Cloudflare** mit aggressivem Bot-Schutz. Ein einfacher Server-Abruf
(z. B. aus GitHub Actions) bekommt nur eine leere Seite / Challenge zurück – getestet,
bestätigt. Deshalb funktioniert IS24 **nicht** wie Kleinanzeigen/Immonet einfach „out of the box".

Es gibt drei realistische Wege (recherchiert über offizielle Doku, Foren & Blogs):

| Weg | Kosten | Eignung | Bewertung |
|-----|--------|---------|-----------|
| **Offizielle API** (`api.immobilienscout24.de`) | – | Nur **Content-Partner** (Makler, die *eigene* Inserate einstellen) | Nicht geeignet, um *fremde* Angebote auszulesen |
| **Bezahl-Scraper** (Apify, Scrapfly, ScraperAPI) | ab ~$1–50/Monat | Liefert zuverlässig JSON, Cloudflare wird vom Anbieter gelöst | Zuverlässigste Variante, aber kostenpflichtig |
| **Echter Browser + Cookies** (eingebaut) | kostenlos | Playwright (Stealth) + dein `cf_clearance`-Cookie | Kostenlos, funktioniert – braucht aber gelegentlich Cookie-Refresh |

Dieses Projekt nutzt **Weg 3** (kostenlos). Unten die Einrichtung.

---

## Schritt 1 – GROQ_API_KEY setzen (für GPS & Umfeld-Analyse)

Der Scraper nutzt Groq/Llama3. Der bisherige Workflow übergab fälschlich `GEMINI_API_KEY`
(unbenutzt) – das ist jetzt korrigiert auf `GROQ_API_KEY`.

1. Kostenlosen Key holen: <https://console.groq.com> → API Keys.
2. GitHub-Repo → **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `GROQ_API_KEY`
   - Value: dein Key

## Schritt 2 – IS24_COOKIES setzen (Cloudflare-Umgehung)

1. In **Chrome** auf <https://www.immobilienscout24.de> gehen, Cookie-Banner akzeptieren,
   eine Suchseite öffnen (z. B. „Wohnung kaufen Mannheim") bis Ergebnisse erscheinen.
2. Cookie-Export-Extension installieren, z. B. **„Cookie-Editor"** oder **„EditThisCookie"**.
3. Auf der IS24-Seite die Cookies **als JSON exportieren** (Export → JSON).
   Wichtig sind v. a. `cf_clearance`, `reese84`, `__cf_bm`.
4. Diesen JSON-Text als GitHub-Secret speichern:
   - Name: `IS24_COOKIES`
   - Value: das komplette JSON-Array (`[ {...}, {...} ]`)

Der Scraper liest das in `inject_is24_cookies()` und injiziert die Cookies vor dem Seitenaufruf.

---

## Wichtiger Hinweis zu `cf_clearance` (bitte lesen)

Das `cf_clearance`-Cookie ist an **IP-Adresse + User-Agent** gebunden.
Ein Cookie, das du **zuhause** erzeugst, kann von der **GitHub-Actions-IP** abgelehnt werden.

Wenn IS24 trotz Cookies leer bleibt, hast du diese Optionen:
- **Cookie regelmäßig erneuern** (alle paar Tage) – einfachster Fix.
- **Self-hosted Runner** verwenden, dessen IP zur Cookie-IP passt.
- **Bezahl-Scraper** (Weg 2) einbinden – einzige wirklich „wartungsfreie" Lösung.

Der Scraper ist robust gebaut: Findet er die normalen HTML-Karten nicht (z. B. weil IS24
seine CSS-Klassen ändert), zieht er die Inserate als **Fallback direkt über die Expose-IDs**
aus dem Seiten-HTML. Die anderen Quellen (Kleinanzeigen, Immonet) laufen unabhängig weiter,
falls IS24 mal blockt.

---

## Manuell testen (lokal)

```bash
pip install -r requirements.txt
playwright install chromium
export GROQ_API_KEY="..."
export IS24_COOKIES='[{"name":"cf_clearance","value":"...","domain":".immobilienscout24.de"}]'
python scraper.py
```

In den Logs erscheint dann `IS24: N Cookies injiziert | Cloudflare-Cookies: [...]`
und `IS24: N Kandidaten auf Seite 1`.
