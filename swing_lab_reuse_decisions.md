# Swing Lab – Wiederverwendungs-Entscheidungen

Sichtung vom 2026-05-14. Quelle: `02_Swing-Lab/`.

---

## ÜBERNEHMEN

### 1. yfinance-Wrapper (anpassen)
**Quelle:** `notebooks/00_data_pipeline_universe.ipynb`  
**Funktionen:** `normalize_yfinance_frame()`, `load_or_download_ticker()`, `summarize_ticker()`  
**Was übernehmen:** MultiIndex-Handling, Timezone-Bereinigung, Column-Normalisierung, Cache-Logik mit REFRESH-Flag, `auto_adjust=False, actions=True`-Pattern.  
**Anpassung für Dashboard:** Aus Notebook-Code in ein eigenständiges `data/yfinance_client.py` extrahieren. Cache-Logik durch SQLite-Storage ersetzen (statt CSV-Cache).

### 2. ATR-Berechnung (direkt kopieren)
**Quelle:** `notebooks/01_pullback_uptrend_v1.ipynb` (~Z. 85) + `tools/legacy_quality_filter.py` (~Z. 80)  
**Was übernehmen:** True Range mit Overnight-Gap-Korrektur (`max(H-L, |H-PrevC|, |L-PrevC|)`), dann SMA14.  
**Begründung:** Korrekte Implementierung, Swing Lab hat das bereits gut gelöst.

### 3. SMA + Slopes (direkt kopieren)
**Quelle:** `notebooks/01_pullback_uptrend_v1.ipynb`  
**Was übernehmen:** SMA10/20/50/200, Slope = `sma / sma.shift(20) - 1`.  
**Begründung:** Standardmuster, keine Anpassung nötig.

### 4. Relative Strength (adaptieren)
**Quelle:** `tools/legacy_quality_filter.py` (~Z. 100–110), Funktion `get_rs()`  
**Was übernehmen:** Outperformance vs. SPY über 1M / 3M als Basis.  
**Anpassung für Dashboard:** Dashboard braucht RS als **Perzentil-Rang** über das gesamte Universum (0–100). Die `get_rs()`-Funktion liefert absolute Outperformance – Umrechnung in Rang muss im `compute/`-Layer ergänzt werden.

### 5. Pullback-Klassifikation als Ausgangspunkt (adaptieren)
**Quelle:** `tools/legacy_quality_filter.py` (~Z. 113–144), Funktion `classify_setup()`  
**Definitionen im Swing Lab:**
- Pullback-MA10: Close ±3% von SMA10, SMA10 steigend, SMA20 > SMA50
- Pullback-MA20: Close ±3% von SMA20, SMA20 steigend, SMA20 > SMA50  
**Anpassung:** Offene Entscheidungen aus PROJECT_BRIEF.md müssen noch getroffen werden (MA-Länge, Tiefe in % oder ATR, Volumen-Filter) – danach Swing-Lab-Implementierung als Vorlage nehmen.

### 6. Liquiditäts-Filter (Parameter prüfen)
**Quelle:** `notebooks/00_data_pipeline_universe.ipynb`  
**Parameter:** `min_price=5.0`, `min_median_dollar_volume_126d=20_000_000`, `min_history_days=252`  
**Übernehmen:** Gleiche Parameter als Dashboard-Default. Review nach erstem Scan-Lauf.

### 7. Universe-CSV als Starterpaket (einmalig nutzen)
**Quelle:** `artifacts/2026-05-01_phase1_universe_filtered.csv`  
**Was übernehmen:** Ticker-Liste mit GICS Sector / Sub-Industry als Ausgangspunkt für den Initial Bulk Load – spart Download-Zeit und gibt sofort das GICS-Mapping.  
**Achtung:** Stand 2026-05-01, einmalig kopieren und nicht als Live-Quelle nutzen. Dashboard pflegt sein eigenes Universe.

### 8. Earnings-Kalender (direkt übernehmen)
**Quelle:** `tools/fetch_edgar_earnings.py`, `tools/build_earnings_calendar.py`  
**Was übernehmen:** EDGAR-Scraper ist produktionsreif (resumable, inkrementell). Für Phase 1 reicht der CSV-Output als Earnings-Filter-Grundlage.  
**Anpassung:** Als eigenständiges Skript in `data/fetch_earnings.py` ablegen, Ausgabe in SQLite-Tabelle `earnings_dates` statt CSV.

---

## BEWUSST NEU

### Backtest-Engine
**Warum nicht übernehmen:** Das Dashboard braucht einen **Scanner** (heutige Hits identifizieren), keine Backtest-Engine (historische Trades simulieren). Die Notebook-Engine ist auf Walk-Forward-Validation mit 10+ Jahren ausgelegt – falscher Scope.

### SQLite-Storage-Layer
**Warum neu:** Swing Lab nutzt CSV-Caches und Pickle-DataFrames. Das Dashboard braucht eine saubere relationale Struktur (`prices`, `breadth_daily`, `scanner_hits`, etc.) mit inkrementellem Update. Von Grund auf neu entwerfen.

### Breadth-Berechnung
**Warum neu:** Swing Lab hat keine Breadth-Metriken (% > 50DMA, A/D-Line, etc.) – das ist ein neues Feature-Set fürs Dashboard.

### Macro-Daten (FRED)
**Warum neu:** Kein FRED-Client in Swing Lab vorhanden.

---

## NICHT VERWENDEN

- `.cache/es7_*.pkl` (132 MB Intraday-Daten) – nur für Backtesting relevant
- Backtest-Equity-Curves – kein Dashboard-Use-Case
- Portfolio-Simulation (Notebooks 03–07) – falscher Scope

---

## FAZIT FÜR PHASE 1

Der yfinance-Wrapper, die Indikator-Berechnungen (ATR, SMA) und der Earnings-Kalender sind direkt nutzbar und sparen ~2–3 Tage Implementierungsarbeit. Das Pullback-Setup aus Swing Lab ist gut dokumentiert und löst die offene Entscheidung zu MA-Länge und Tiefe zu einem guten Teil – Swing Lab nutzt MA20 mit ±3% Tiefe als beste Klasse.
