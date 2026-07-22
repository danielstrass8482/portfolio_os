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
| `main.py` | APScheduler-Jobs (Preise, Reports) |

## Wichtige Prinzipien

- Das LLM **empfiehlt**, der Nutzer **entscheidet** immer selbst. Kein Autoexec ohne
  explizite Bestätigung (`confirm_proposal`).
- Bei API-Ausfall (Anthropic oder SMTP) läuft das System im degraded mode ohne Absturz weiter.
- Tabellenpräfix `pos_` für alle Tabellen, um Konflikte mit dem Trading Bot auszuschließen.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # Werte eintragen
python database.py     # Tabellen anlegen
streamlit run dashboard.py --server.port 8502
```

## Deployment

Läuft auf dem VPS als systemd-Dienst `portfolio-dashboard` (Port 8502) und ist über
`https://portfolio.straesschen.de` per nginx-Reverse-Proxy erreichbar.
