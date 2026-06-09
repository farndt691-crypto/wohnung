@echo off
REM ============================================================
REM  Immobilien-Sniper – Ersteinrichtung (Windows)
REM  Doppelklick oder: setup.bat
REM ============================================================

echo.
echo  === Immobilien-Sniper Setup ===
echo.

REM Python prüfen
python --version >nul 2>&1
IF ERRORLEVEL 1 (
    echo [FEHLER] Python nicht gefunden. Bitte Python 3.11+ installieren.
    echo         https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [1/4] Erstelle virtuelles Environment...
python -m venv venv
call venv\Scripts\activate.bat

echo [2/4] Installiere Python-Pakete...
pip install -r requirements.txt

echo [3/4] Installiere Playwright Chromium-Browser...
playwright install chromium

echo [4/4] Setup abgeschlossen!
echo.
echo  Starten mit:
echo    venv\Scripts\activate
echo    python main.py
echo.
echo  Dashboard: http://localhost:8000
echo.
pause
