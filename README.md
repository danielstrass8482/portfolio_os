# Portfolio-OS

Persönliches Portfolio-Management-System mit KI-Analyse und Rebalancing.

Läuft auf demselben VPS wie der [Trading Bot](https://github.com/danielstrass8482/trading_bot)
und teilt dieselbe PostgreSQL-Datenbank (eigene Tabellen mit Präfix `pos_`, kein Konflikt).

## Architektur

- **Python 3.12**, Streamlit, SQLAlchemy, APScheduler
- **Anthropic Claude API** für KI-Analyse (Klumpenrisiko, Rebalancing-Begründung, Quartalsberichte)
- **PostgreSQL** (gemeinsam mit dem Trading Bot)
- **E-Mail** via bestehendem Mailserver (SMTP-Zugangsdaten aus dem Trading Bot übernommen)

## Module

| Datei | Zweck |
|---|---|
| `config.py` | Zentrale Konfiguration aus `.env` |
| `database.py` | SQLAlchemy-Modelle (`pos_*`-Tabellen) |
| `portfolio.py` | Positionen, Transaktionen, Kursaktualisierung, Performance |
| `tax_engine.py` | Deutsche Kapitalertragsteuer (Abgeltungssteuer, Freistellungsauftrag, Verlusttopf) |
| `rebalancing.py` | Ist/Soll-Abweichungen, Sparraten-Empfehlung, Rebalancing-Vorschläge |
| `llm_analyst.py` | Claude-Integration für Analyse und Berichte |
| `notifier.py` | E-Mail-Benachrichtigungen (Tages-/Wochen-/Monats-/Quartals-/Jahres-Reports) |
| `dashboard.py` | Streamlit-Dashboard (7 Tabs) |
| `api.py` | FastAPI-Backend (JWT-Auth, Alpaca-Connect, Bot-Config-Proxy zum Trading Bot, Admin) |
| `main.py` | APScheduler-Jobs (Preise, Reports) — aktuell **nicht** als eigener systemd-Dienst deployed |

## Wichtige Prinzipien

- Das LLM **empfiehlt**, der Nutzer **entscheidet** immer selbst. Kein Autoexec ohne
  explizite Bestätigung (`confirm_proposal`).
- Bei API-Ausfall (Anthropic oder SMTP) läuft das System im degraded mode ohne Absturz weiter.
- Tabellenpräfix `pos_` für alle Tabellen, um Konflikte mit dem Trading Bot auszuschließen.

## Setup

Zwei **getrennte** venvs seit 2026-08-05 (siehe "Warum zwei venvs?" unten) — eines pro
Service, nicht ein gemeinsames für den ganzen Ordner:

```bash
# API (FastAPI/uvicorn, api.py)
python3 -m venv venv_api
source venv_api/bin/activate
pip install -r requirements-api.txt

# Dashboard (Streamlit, dashboard.py)
python3 -m venv venv_dashboard
source venv_dashboard/bin/activate
pip install -r requirements-dashboard.txt

cp .env.example .env   # Werte eintragen (von beiden venvs/Services gemeinsam genutzt)
python database.py     # Tabellen anlegen (mit einer der beiden venvs, database.py ist in beiden enthalten)

# Lokal starten:
venv_api/bin/uvicorn api:app --port 8503 --ws none
venv_dashboard/bin/streamlit run dashboard.py --server.port 8502
```

`requirements.txt` (die alte, gemeinsame Datei) ist deprecated und wird von keinem
Service mehr installiert — nur noch als Referenz vorhanden, nicht länger aktuell halten.

### Warum zwei venvs?

Bis 2026-08-05 teilten sich `portfolio-api` und `portfolio-dashboard` ein venv. Das
Hinzufügen von `alpaca-trade-api` für `api.py` (Alpaca-Connect-Feature) hat dabei
transitiv `streamlit`/`yfinance`/`websockets` im **selben** venv auf ältere, für
`alpaca-trade-api` kompatible Versionen zurückgezogen — unbeabsichtigt, weil beide
Services nichts mit `alpaca-trade-api` zu tun hatten außer eben dem geteilten venv.
Folge war u.a. ein `uvicorn`-Crash-Loop (`--ws auto` importierte eine Websockets-API,
die durch den Downgrade nicht mehr existierte, siehe `--ws none` im ExecStart unten).
Die Trennung stellt sicher, dass ein künftiges Dependency-Update für den einen Service
den anderen nicht mehr unbemerkt beeinflussen kann.

## Deployment

Läuft auf dem VPS als zwei systemd-Dienste, siehe [`systemd/`](systemd/) für die
versionierten Unit-Dateien (bei Neuaufsetzen des Servers nach `/etc/systemd/system/`
kopieren + `daemon-reload`):

- `portfolio-api` (Port 8503, `venv_api`) — FastAPI-Backend, `/api/*`
- `portfolio-dashboard` (Port 8502, `venv_dashboard`) — Streamlit-Dashboard

Erreichbar über `https://portfolio.diestraesschens.de` per nginx-Reverse-Proxy
(React-Frontend + `/api/`-Proxy auf `portfolio-api`, siehe `portfolio_react`-Repo).
