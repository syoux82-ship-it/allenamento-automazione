#!/usr/bin/env python3
"""
Aggiornamento giornaliero automatico - Allenamento Sebastiano Di Pasquale
===========================================================================

Scheletro dello script che GitHub Actions esegue ogni mattina al posto del
"Riepilogo mattino" fatto finora in chat con Claude.

Cosa fa (in questo ordine):
  1. Legge i dati di ieri da Garmin Connect e da Intervals.icu
  2. Applica le regole gia' note al progetto (dedup Fenix/Edge, zone, CTL/ATL...)
  3. Chiede ad Anthropic un giudizio testuale (avvisi HRV/sonno, proposte)
  4. Scrive tutto in data/latest.json (e aggiorna data/storico.jsonl)

Il file prodotto e' quello che poi la dashboard/PWA legge per mostrare i
dati aggiornati, da qualunque dispositivo, senza che nessun Mac debba essere
acceso: lo esegue GitHub Actions in cloud (vedi .github/workflows/daily.yml).

Le chiavi/credenziali non vanno MAI scritte qui: arrivano come variabili
d'ambiente (i "secrets" configurati nel repository GitHub).
"""

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
LATEST_PATH = DATA_DIR / "latest.json"
HISTORY_PATH = DATA_DIR / "storico.jsonl"


# ---------------------------------------------------------------------------
# 1. Fonti dati
# ---------------------------------------------------------------------------

def fetch_garmin(target_date: date) -> dict:
    """Scarica da Garmin Connect i dati fisiologici e le attivita' del giorno.

    TODO: usare la libreria `garminconnect` (login con GARMIN_EMAIL /
    GARMIN_PASSWORD) per leggere: sonno, HRV, FC a riposo, stress, Body
    Battery, prontezza, e le attivita' del giorno (incluso il file FIT con
    include_records per power/HR series).

    Riferimento alla logica gia' definita nel progetto Claude:
    `claude/stato-attuale.md`, sezione 5 "Fonti dati e come leggerle".
    """
    from garminconnect import Garmin  # import locale: non serve se si cambia libreria

    email = os.environ["GARMIN_EMAIL"]
    password = os.environ["GARMIN_PASSWORD"]

    client = Garmin(email, password)
    client.login()

    iso_date = target_date.isoformat()

    return {
        "sleep": client.get_sleep_data(iso_date),
        "hrv": client.get_hrv_data(iso_date),
        "stress": client.get_stress_data(iso_date),
        "activities": client.get_activities_by_date(iso_date, iso_date),
        # TODO: per ogni attivita' rilevante, scaricare anche il dettaglio
        # (potenza/FC a serie) con client.get_activity_details(activity_id)
    }


def fetch_intervals(target_date: date) -> dict:
    """Scarica da Intervals.icu il carico (CTL/ATL), eFTP, curva di potenza.

    TODO: chiamata REST con Basic Auth "API_KEY:<chiave>" (header
    Authorization), stesso endpoint gia' usato in chat:
    https://intervals.icu/api/v1/athlete/{ATHLETE_ID}/wellness?oldest=...&newest=...
    """
    import requests

    athlete_id = os.environ["INTERVALS_ATHLETE_ID"]
    api_key = os.environ["INTERVALS_API_KEY"]

    resp = requests.get(
        f"https://intervals.icu/api/v1/athlete/{athlete_id}/wellness",
        params={"oldest": target_date.isoformat(), "newest": target_date.isoformat()},
        auth=("API_KEY", api_key),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# 2. Regole gia' definite nel progetto (da portare qui una volta sola)
# ---------------------------------------------------------------------------

def deduplicate_activities(activities: list[dict]) -> list[dict]:
    """Applica la regola doppioni Fenix/Edge: se due attivita' si sovrappongono
    per piu' di meta' durata, tiene solo quella con piu' dati (Edge/potenza).

    TODO: portare qui la logica descritta in
    `claude/doppioni-fenix-esclusi.md`.
    """
    return activities  # placeholder


def compute_training_metrics(garmin_data: dict, intervals_data: dict) -> dict:
    """Calcola zone, CTL/ATL/forma, eFTP, bilanciamento ecc.

    TODO: portare qui la logica di calcolo gia' scritta in JavaScript nella
    dashboard (`claude/piemontgravel-2027-artifact-source.html`), in
    particolare le funzioni di zona (hrZoneFor / _zoneFromWatts) e i valori
    di CTL/ATL/eFTP letti da Intervals.icu.
    """
    return {
        "date": None,
        "ctl": None,
        "atl": None,
        "form": None,
        "eftp": None,
        "resting_hr": None,
        "hrv": None,
        "sleep_hours": None,
    }


# ---------------------------------------------------------------------------
# 3. Giudizio (equivalente del "Riepilogo mattino" fatto da Claude in chat)
# ---------------------------------------------------------------------------

def ask_anthropic_for_judgment(metrics: dict) -> str:
    """Chiede ad Anthropic un breve giudizio/avviso sulla giornata, con le
    stesse regole gia' in uso (avviso HRV solo se la media 7gg e' sotto
    fascia da >=2 giorni + seduta di qualita', avviso sonno <6h, ecc.)

    Richiede la variabile d'ambiente ANTHROPIC_API_KEY.
    """
    import anthropic

    client = anthropic.Anthropic()  # legge ANTHROPIC_API_KEY dall'ambiente

    prompt = f"""
Sei un coach di endurance. Ecco i dati di oggi: {json.dumps(metrics, ensure_ascii=False)}.
Regole da rispettare (decise in precedenza):
- HRV: solo avviso se la media 7 giorni e' sotto fascia da >=2 giorni E oggi c'e' una seduta di qualita'.
- Sonno < 6h: avviso.
- Nessuna modifica automatica al piano: solo una proposta testuale, mai applicata senza conferma.
Scrivi un giudizio breve (massimo 5 righe), in italiano, solo se c'e' qualcosa di rilevante da segnalare.
Altrimenti rispondi con una riga che dice che non ci sono avvisi.
"""

    message = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ---------------------------------------------------------------------------
# 4. Scrittura del risultato
# ---------------------------------------------------------------------------

def write_output(payload: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    LATEST_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    target_date = date.today() - timedelta(days=1)  # i dati di "ieri"

    try:
        garmin_data = fetch_garmin(target_date)
        intervals_data = fetch_intervals(target_date)
    except Exception as exc:  # pragma: no cover - placeholder
        print(f"Errore nel recupero dati: {exc}", file=sys.stderr)
        return 1

    garmin_data["activities"] = deduplicate_activities(garmin_data.get("activities", []))
    metrics = compute_training_metrics(garmin_data, intervals_data)
    metrics["date"] = target_date.isoformat()

    try:
        judgment = ask_anthropic_for_judgment(metrics)
    except Exception as exc:  # pragma: no cover - placeholder
        judgment = f"(giudizio non disponibile: {exc})"

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "metrics": metrics,
        "judgment": judgment,
    }

    write_output(payload)
    print(f"Aggiornato {LATEST_PATH} per il {target_date.isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
