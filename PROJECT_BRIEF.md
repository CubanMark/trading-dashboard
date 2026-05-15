# Trading Dashboard – Projekt-Brief

> Übergabedokument für Claude Code. Enthält alle Entscheidungen, Architektur-Prinzipien und Phasen aus der vorgelagerten Konzeptarbeit. Sprache des Projekts: Deutsch. Code-Kommentare auf Englisch erlaubt.

---

## 1. Ziel & Scope

**Persönliches Trading-Dashboard für US-Märkte.** Aktuell **Lernprojekt** für Paper Trading; Architektur so, dass es später produktiv genutzt werden kann, ohne neu gebaut werden zu müssen.

**Das Tool muss leisten, was TradingView nicht personalisiert leistet:**
- Synthese über viele Tickers (Breadth, Sektor-Rotation)
- Eigene historische Zeitreihen (Breadth, Risk-On/Off-Verhältnisse)
- Composite-Marktbild über mehrere Dimensionen
- Scanner-Output mit Sektor-Kontext und Filterbarkeit
- Trade Journal mit Setup-Auswertung (Phase 3)

**Das Tool macht ausdrücklich NICHT:**
- Einzelchart-Analyse, Indikator-Spielwiese, Drawing (→ TradingView)
- Realtime-Daten
- Options Flow, News-Aggregation, Crypto-Korrelationen

---

## 2. Bezug zu anderen Trading-Projekten

Dieses Projekt liegt unter `Trading/` neben zwei existierenden Schwesterprojekten:

| Projekt | Rolle | Status |
|---|---|---|
| `Trading/Swing Life/` | Aktuelles Paper Trading – operative Trade-Ausführung | aktiv |
| `Trading/Swing Lab/` | Strategie-Forschung, Backtests, Hypothesen-Tests | aktiv, enthält Python-Notebooks und gesammelte Daten |
| `Trading/Dashboard/` (dieses Projekt) | Marktlage + Scanner-Output – steuernde Information | neu |

**Logischer Informationsfluss (Soll-Zustand):**

```
Swing Lab  →  validierte Setup-Regeln  →  Dashboard (Scanner-Definitionen)
Dashboard  →  Marktlage + tägliche Hits  →  Swing Life (Trade-Entscheidung)
Swing Life →  Trade-Resultate           →  Dashboard Phase 3 (Journal/Auswertung)
```

**Wiederverwendungs-Auftrag für Claude Code (vor Neu-Bau zwingend prüfen):**

Im ersten Schritt `Trading/Swing Lab/` sichten und folgende Bausteine identifizieren, falls vorhanden:
- yfinance-Wrapper / Datenfetch-Funktionen
- Indikator-Berechnungen (ATR, RS, gleitende Durchschnitte, etc.)
- bereits gesammelte historische Daten (Format, Speicherort)
- Strategie-Definitionen, die zu Scannern (Pullback, VCP, Breakout) passen könnten

Pro Fundstück entscheiden: **übernehmen** (kopieren/anpassen) oder **bewusst neu** (Begründung dokumentieren). Ergebnis als kurze Notiz im Projekt ablegen.

**Bewusste Grenzen (Anti-Coupling):**
- Keine gemeinsame `shared/`-Library in Phase 1. Code-Duplikation wird in Kauf genommen, bis das Muster der echten Gemeinsamkeit sichtbar ist (frühestens Phase 2).
- Jedes Projekt hat eigene SQLite-DB. Dashboard-DB kann später *read-only* von Swing Lab konsumiert werden, falls sinnvoll – aber keine Schreibzugriffe übers Projekt hinweg.
- Strategien aus Swing Lab werden via **klarer schriftlicher Regelwerk-Definition** in Dashboard-Scanner überführt, nicht via Python-Import. Das hält die Projekte entkoppelt und zwingt zur sauberen Strategie-Dokumentation.

---

## 3. Architektur-Prinzipien

1. **Saubere Schichtentrennung** – Datenholen / Berechnen / Darstellen. Jede Schicht austauschbar (yfinance → Polygon, HTML → Streamlit).
2. **Hub & Spoke im UI** – Homepage zeigt pro Dimension EINE Kennzahl mit Trend-Pfeil und Ampel; Drilldown auf Unterseite.
3. **Eigene Datenhistorie** – inkrementelles Update statt jedes Mal Full-Reload. Schutz vor yfinance-Ausfällen und Basis für eigene Zeitreihen (Breadth-Verlauf etc.).
4. **Keine Auto-Composite-Ampel anfangs** – Dimensionen einzeln darstellen, Markus liest selbst. Composite erst nach Monaten Erfahrung mit eigener Gewichtung.
5. **Vollständige Dateien, keine Fragmente** (globale Präferenz).
6. **Pro Kachel auf der Homepage muss in einem Satz beantwortbar sein:** *Welche Entscheidung treffe ich morgens anders, wenn diese Zahl rot ist?* Wenn nicht → Kachel raus.

---

## 4. Tech-Stack

| Schicht | Wahl | Begründung |
|---|---|---|
| Datenquelle | yfinance + FRED | Free, ausreichend für EOD. Wechselbar später. |
| Frequenz | End-of-Day | Pullback/VCP/Breakout sind Tagessetups. |
| Speicher | SQLite | Lokal, einfach, eine Datei, portabel. |
| Hosting | GitHub Actions + GitHub Pages | Daily Cron in Cloud, gratis, passt zu bestehender GitHub-Infrastruktur. |
| Frontend | Statisches HTML + Plotly | Genug für Phase 1–2; später ggf. Streamlit. |
| Sprache | Python 3.11+ | Standard für Finance-Stack. |

**Bewusste Risiken:**
- yfinance ist inoffiziell – Retry-Logik und "letzter gültiger Stand"-Fallback nötig.
- Splits/Dividenden: adjusted vs. unadjusted Storage muss entschieden werden (Empfehlung: unadjusted + Adjustment-Faktoren separat).
- Tägliche Sanity Checks (NaN, Ausreißer) Pflicht, sonst korrupte Breadth-Historie.

---

## 5. Datenuniversum

- **Indizes/Makro:** SPY, QQQ, IWM, VIX, VIX3M, US10Y, US2Y, DXY, USO, TLT, GLD, HYG, LQD
- **Sektoren:** 11 GICS ETFs (XLK, XLV, XLF, XLY, XLP, XLE, XLI, XLU, XLB, XLRE, XLC)
- **Branchen:** Industry ETFs (SMH, IGV, XBI, KRE, etc.) + Mapping S&P 1500 → GICS Industry
- **Aktien-Universum:** S&P 1500 + persönliche Watchlist (separat pflegbar)
- **Macro-Daten:** FRED-Serien für HY OAS, Yield Curve

---

## 6. Inhalts-Konzept (Hub & Spoke)

### Homepage / Dashboard
- **Composite Strip** (6 Dimensions-Pills, jede klickbar zum Drilldown)
- **Interpretations-Zeile** in einem Satz ("Uptrend, aber Breadth schwächelnd, Sentiment in Greed – Setups verkleinern")
- Index-Snapshot (kompakt, eine Zeile, MA-Lage statt nur Tagesperformance)
- 6 Dimensions-Detailkacheln mit EINER Kennzahl + 20-Tage-Sparkline + Drilldown-Link
- Sektor-Heatmap (1M, später togglebar)
- Top Scanner-Hits heute (3–5)
- Kalender Woche

### Unterseiten
- `/breadth` – alle Breadth-Indikatoren + Historie
- `/sentiment` – F&G + Komponenten, AAII, Put/Call
- `/risk` – XLY/XLP, HYG/LQD, Copper/Gold, BTC, SPY/TLT
- `/credit-macro` – HY OAS, 2s10s, DXY
- `/volatility` – VIX, Term Structure, Vola-Regime
- `/sectors` – GICS + Branchen, RRG (Phase 2)
- `/scanners` – alle Scanner mit Filtern (Branche, RS, ADR%)
- `/journal` – Phase 3

---

## 7. Marktdimensionen – Homepage-Kennzahlen

| Dimension | EINE Kennzahl | Ampel-Logik (initial) |
|---|---|---|
| Breadth | % S&P 500 > 50DMA | >60 grün, 40–60 gelb, <40 rot |
| Sentiment | Fear & Greed Score (0–100) | 25–75 gelb, Extreme = Contrarian-Warnung |
| Risk On/Off | XLY/XLP Ratio (20T-Trend) | steigend grün, fallend rot |
| Credit | HY OAS (bps + 20T-Trend) | <350 grün, steigend = Warnung |
| Volatility | VIX + VIX/VIX3M | VIX<20 grün; Term <1 = contango/ruhig |
| OB/OS | SPY-Distanz zu MA50 in ATR | <2 grün, 2–3 gelb, >3 extended |

**Erweiterungen pro Drilldown** (nicht erschöpfend):
- Breadth: A/D-Line, McClellan, NH-NL, % >200DMA, % innerhalb 5% von 52W-Hoch
- Sentiment: Put/Call, AAII Bull-Bear-Spread, F&G-Komponenten
- Risk: HYG/TLT, SPY/TLT, BTC als Risk-Appetite-Proxy
- Credit: 2s10s-Spread, US10Y Veränderungsrate
- Volatility: Vola-Regime-Klassifikation (low/mid/high) → Setup-Empfehlung

---

## 8. Scanner

**Drei initiale Setups:**
1. **Pullback to MA** (zu präzisieren: 20er oder 50er? Tiefe in % oder ATR? Volumen-Filter? Trend-Vorfilter Aktie > 200DMA?)
2. **VCP** (Volatility Contraction)
3. **Breakout**

**Pro Hit anzuzeigen:** Ticker, Sektor, Industry, RS-Rang (Perzentil), 1M-Performance, ADR%, ATR, durchschnittliches Volumen, Distance to 52W High, Earnings-Datum.

**Filter:** nach Sektor-/Branchen-Trend (nur Hits in leading/improving Industries), nach RS-Rang, nach ADR%.

---

## 9. Phasen-Plan

### Phase 1 – MVP (Fokus jetzt)
- Datenpipeline: yfinance + FRED, SQLite-Storage, daily GitHub Action
- Homepage mit Macro-Strip, 6 Dimensions-Kacheln, Sektor-Heatmap
- **EIN** Scanner: Pullback to MA (sauber definiert)
- Statisches HTML, deployed via GitHub Pages

### Phase 2
- Restliche Scanner (VCP, Breakout)
- RRG für Sektoren/Branchen
- Scanner-Filter (Branchen-Trend, RS, ADR%)
- Watchlist-Tracking separat

### Phase 3
- Trade Journal mit Chart-Snapshots (vor/nach Trade) via Plotly aus eigener DB
- Setup-Auswertung (Win-Rate pro Scanner, R-Multiple-Verteilung)

### Phase ∞ (bewusst weggelassen)
- Realtime, Options Flow, News, Crypto

---

## 10. Offene Entscheidungen

- [~] Pullback-Scanner: MA20 Tiefe **1.0 × ATR14**, MA10 Tiefe **0.75 × ATR14** (2026-05-15, ersetzt %-Regel — ATR normiert Volatilität über Aktien hinweg). Multiplier sind Startwerte, nach Wochen Live-Betrieb anpassen. Volumen-Filter beim Pullback selbst noch offen.
- [x] yfinance: **unadjusted speichern + Splits/Dividenden separat** (2026-05-14). Grund: yfinance-Adjustments haben bekannte rückwirkende Bugs; eigene Adjustment-Hoheit schützt die Breadth-Historie.
- [ ] Sektor-Heatmap: Zeitraum-Toggle (1W/1M/3M/6M) gleich oder Phase 2?
- [ ] Composite-Strip: alle Pills klickbar zum Drilldown machen
- [ ] Index-Strip oben: reduzieren oder MA-Lage statt Tagesperformance?
- [ ] Interpretations-Zeile: regelbasiert oder manuell?

---

## 11. Anti-Patterns (bewusst zu vermeiden)

1. **Over-Engineering für hypothetische Produktivnutzung.** Saubere Schichten ja, Features für "später" nein.
2. **Verliebt-ins-Mockup-Falle.** Wireframe ist Denkwerkzeug, nicht Bauplan.
3. **Auto-Composite-Ampel ohne Erfahrung.** Einfacher Durchschnitt täuscht – Breadth+Credit rot ≠ Sentiment+OB/OS rot.
4. **Mit Daten anfangen, bevor Inhaltslogik klar ist.** Pro Kachel muss Entscheidungsauftrag formulierbar sein.
5. **Konkurrenz zu TradingView aufbauen.** Nur das, was TradingView nicht personalisiert leistet.
6. **Parallel-Projekte-Falle.** Dies ist explizit Nebenprojekt für Zwischenzeiten – nicht in Hauptfokus drücken.
7. **Premature Sharing zwischen Trading-Projekten.** Keine `shared/`-Library, kein gemeinsamer DB-State, keine Python-Imports zwischen Swing Life/Lab/Dashboard in Phase 1. Code-Duplikation ist günstiger als falsche Abstraktion.

---

## 12. Nächste Schritte (für Claude Code)

0. **Sichtung Schwesterprojekte** (siehe Abschnitt 2): `Trading/Swing Lab/` durchsehen auf wiederverwendbare yfinance-Wrapper, Indikatoren, gesammelte Daten. Kurze Notiz mit Übernahme-Entscheidung pro Fundstück.
1. **Projekt-Skelett anlegen:**
   ```
   trading-dashboard/
   ├── data/        # Fetching, Storage (SQLite-Schema, yfinance/FRED clients)
   ├── compute/     # Indicators (breadth, ratios, ATR, RS)
   ├── render/      # HTML-Generierung, Plotly-Charts
   ├── scanners/    # Pullback, VCP, Breakout
   ├── pages/       # Output-HTML
   ├── db/          # SQLite-Files
   ├── .github/workflows/  # daily build
   └── tests/
   ```
2. **SQLite-Schema entwerfen:** `prices`, `corporate_actions`, `breadth_daily`, `macro_series`, `scanner_hits`, `trades`.
3. **Datenpipeline Phase 1.1:** Initial Bulk Load S&P 1500 + Macro-Tickers (~5 Jahre), Sanity Checks, Retry-Logik.
4. **Erste Homepage:** Macro-Strip + Breadth-Kachel (% > 50DMA). Erst wenn das stabil daily läuft, weitere Dimensionen.
5. **Pullback-Regeln präzisieren** vor dem Scanner-Bau.

---

## 13. Persönliche Präferenzen (für Claude Code)

- Deutsch, direkt, präzise, ohne Floskeln
- Kritisches Sparring statt Zustimmung
- Vollständige Dateien, keine Fragmente
- Bei komplexen Themen: Fakten / Annahmen / Risiken / Empfehlung / Nächste 3 Schritte
- Komplexität aktiv zurückdrängen, Umsetzung in kleine Schritte zerlegen
- Code-Kommentare und Variablennamen Englisch
