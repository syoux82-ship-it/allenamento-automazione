#!/usr/bin/env python3
"""
Aggiornamento giornaliero automatico - Allenamento Sebastiano Di Pasquale
===========================================================================

Script che GitHub Actions esegue ogni mattina al posto del "Riepilogo
mattino" fatto finora in chat con Claude.

Cosa fa (in questo ordine):
  1. Legge i dati di ieri da Garmin Connect e da Intervals.icu
  2. Applica la regola gia' nota al progetto per i doppioni Fenix/Edge
  3. Chiede ad Anthropic un giudizio testuale (avvisi HRV/sonno, proposte)
  4. Scrive tutto in data/latest.json (e aggiorna data/storico.jsonl)

Il file prodotto e' quello che poi la dashboard/PWA legge per mostrare i
dati aggiornati, da qualunque dispositivo, senza che nessun Mac debba essere
acceso: lo esegue GitHub Actions in cloud (vedi .github/workflows/daily.yml).

Le chiavi/credenziali non vanno MAI scritte qui: arrivano come variabili
d'ambiente (i "secrets" configurati nel repository GitHub).

Nota di disegno: CTL/ATL/eFTP/HRV/sonno/FC a riposo vengono letti da
Intervals.icu, che li calcola gia' lui (sincronizzando da Garmin) - e' la
stessa fonte gia' usata finora per il resoconto in chat. Non vengono
ricalcolati qui da zero.

Limite noto: se un doppione Fenix/Edge non viene eliminato manualmente anche
su Intervals.icu, il CTL/ATL che arriva da li' puo' restare leggermente
gonfiato per quel giorno (vedi claude/doppioni-fenix-esclusi.md). Lo script
non cancella nulla su Intervals.icu in autonomia.
"""

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
LATEST_PATH = DATA_DIR / "latest.json"
HISTORY_PATH = DATA_DIR / "storico.jsonl"

# Quanti giorni indietro guardare per il contesto HRV/sonno (serve alla
# regola "avviso HRV solo se la media 7gg e' sotto fascia da >=2 giorni").
HISTORY_WINDOW_DAYS = 9

# Chiavi dei tipi di attivita' ciclistiche su cui applicare la deduplica
# Fenix/Edge. Rulli e virtual ride sono esclusi per regola esplicita del
# progetto (non sono doppioni, sono sessioni diverse per natura).
CYCLING_TYPE_KEYS = {
    "cycling",
    "road_biking",
    "gravel_cycling",
    "mountain_biking",
    "cyclocross",
    "track_cycling",
    "recumbent_cycling",
}
EXCLUDED_TYPE_KEYS = {"indoor_cycling", "virtual_ride"}


# ---------------------------------------------------------------------------
# 1. Fonti dati
# ---------------------------------------------------------------------------

def fetch_garmin(target_date: date) -> dict:
    """Scarica da Garmin Connect i dati fisiologici e le attivita' del giorno.

    Ogni singola chiamata e' avvolta in try/except: se un dato manca (es.
    stress non registrato quel giorno) il resto non deve fallire.
    """
    from garminconnect import Garmin

    email = os.environ["GARMIN_EMAIL"]
    password = os.environ["GARMIN_PASSWORD"]

    client = Garmin(email, password)
    client.login()

    iso_date = target_date.isoformat()
    data: dict = {"sleep": None, "stress": None, "activities": []}

    try:
        data["sleep"] = client.get_sleep_data(iso_date)
    except Exception as exc:  # noqa: BLE001
        print(f"Avviso: sonno Garmin non disponibile ({exc})", file=sys.stderr)

    try:
        data["stress"] = client.get_stress_data(iso_date)
    except Exception as exc:  # noqa: BLE001
        print(f"Avviso: stress Garmin non disponibile ({exc})", file=sys.stderr)

    try:
        data["activities"] = client.get_activities_by_date(iso_date, iso_date) or []
    except Exception as exc:  # noqa: BLE001
        print(f"Avviso: attivita' Garmin non disponibili ({exc})", file=sys.stderr)
        data["activities"] = []

    return data


def fetch_intervals(target_date: date) -> list[dict]:
    """Scarica da Intervals.icu il wellness (CTL/ATL/eFTP/HRV/sonno/FC a
    riposo) di oggi e dei giorni precedenti, per avere anche il contesto
    della tendenza a 7 giorni (serve alla regola sull'avviso HRV).

    Ritorna una lista di voci giornaliere (puo' essere vuota se Intervals
    non ha ancora sincronizzato il giorno richiesto), ordinata per data.
    """
    import requests

    athlete_id = os.environ["INTERVALS_ATHLETE_ID"]
    api_key = os.environ["INTERVALS_API_KEY"]

    oldest = target_date - timedelta(days=HISTORY_WINDOW_DAYS)

    resp = requests.get(
        f"https://intervals.icu/api/v1/athlete/{athlete_id}/wellness",
        params={"oldest": oldest.isoformat(), "newest": target_date.isoformat()},
        auth=("API_KEY", api_key),
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()

    # L'API puo' rispondere con una lista o, per una singola data, con un
    # oggetto solo: normalizziamo sempre a lista.
    if isinstance(body, dict):
        body = [body]
    return sorted(body, key=lambda entry: entry.get("id") or entry.get("date") or "")


# ---------------------------------------------------------------------------
# 2. Regole gia' definite nel progetto
# ---------------------------------------------------------------------------

def _activity_type_key(activity: dict) -> str:
    activity_type = activity.get("activityType") or {}
    return (activity_type.get("typeKey") or "").lower()


def _activity_start(activity: dict):
    from dateutil import parser as date_parser

    raw = activity.get("startTimeGMT") or activity.get("startTimeLocal")
    if not raw:
        return None
    try:
        return date_parser.parse(raw)
    except (ValueError, TypeError):
        return None


def _activity_richness(activity: dict) -> int:
    """Punteggio euristico di 'quanti dati utili ha questa attivita''.

    Usato per scegliere quale delle due tenere quando si trova un doppione:
    per regola di progetto si tiene quella dell'Edge (di solito con potenza),
    si esclude quella del fenix. In pratica l'Edge ha quasi sempre piu' campi
    valorizzati (potenza inclusa), quindi contare i campi non nulli e'
    equivalente e non richiede di identificare il device.
    """
    score = sum(1 for v in activity.values() if v not in (None, "", [], {}))
    if activity.get("avgPower") or activity.get("averagePower"):
        score += 100  # la potenza e' il segnale piu' forte: quasi certo Edge
    return score


def deduplicate_activities(activities: list[dict]) -> list[dict]:
    """Applica la regola doppioni Fenix/Edge decisa da Sebastiano
    (claude/doppioni-fenix-esclusi.md):

    Due uscite in bici con partenza entro 15 minuti e distanza entro +-10%
    sono la stessa uscita vista da due device: si tiene quella con piu'
    dati utili (di norma l'Edge, con potenza), si esclude l'altra (di norma
    il fenix). Rulli (indoor_cycling) e virtual ride (Zwift/MyWhoosh) non
    vengono mai considerati doppioni tra loro ne' con le uscite outdoor.
    """
    candidates = [a for a in activities if _activity_type_key(a) in CYCLING_TYPE_KEYS]
    others = [a for a in activities if _activity_type_key(a) not in CYCLING_TYPE_KEYS]

    to_drop: set[int] = set()
    for i, a in enumerate(candidates):
        if i in to_drop:
            continue
        start_a = _activity_start(a)
        dist_a = a.get("distance")
        for j in range(i + 1, len(candidates)):
            if j in to_drop:
                continue
            b = candidates[j]
            start_b = _activity_start(b)
            dist_b = b.get("distance")

            if start_a is None or start_b is None or not dist_a or not dist_b:
                continue

            minutes_apart = abs((start_a - start_b).total_seconds()) / 60
            if minutes_apart > 15:
                continue

            distance_ratio = abs(dist_a - dist_b) / max(dist_a, dist_b)
            if distance_ratio > 0.10:
                continue

            # Doppione confermato: scarta quella con meno dati.
            if _activity_richness(a) >= _activity_richness(b):
                to_drop.add(j)
            else:
                to_drop.add(i)
                break

    deduped_cycling = [a for i, a in enumerate(candidates) if i not in to_drop]
    return deduped_cycling + others


# ---------------------------------------------------------------------------
# 3. Calcolo metriche (lette da Intervals.icu, non ricalcolate)
# ---------------------------------------------------------------------------

def compute_training_metrics(garmin_data: dict, intervals_history: list[dict]) -> dict:
    """Estrae CTL/ATL/forma/eFTP/HRV/sonno/FC a riposo dal wellness di
    Intervals.icu per il giorno target, e prepara il contesto a 7 giorni
    (HRV, sonno) che serve alla regola di avviso usata dal giudizio AI.
    """
    today_entry = intervals_history[-1] if intervals_history else {}

    ctl = today_entry.get("ctl")
    atl = today_entry.get("atl")
    form = (ctl - atl) if (ctl is not None and atl is not None) else None

    eftp = None
    for sport in today_entry.get("sportInfo", []) or []:
        if (sport.get("type") or "").lower() in {"ride", "virtualride"}:
            eftp = sport.get("eftp")
            if eftp:
                break

    sleep_secs = today_entry.get("sleepSecs")
    sleep_hours = round(sleep_secs / 3600, 1) if sleep_secs else None

    # Media mobile HRV sui 7 giorni precedenti il target (esclude oggi),
    # cosi' il giudizio AI puo' applicare correttamente la regola gia'
    # decisa ("avviso solo se la media 7gg e' sotto fascia da >=2 giorni").
    prior_days = intervals_history[:-1][-7:] if len(intervals_history) > 1 else []
    hrv_history = [d.get("hrv") for d in prior_days if d.get("hrv") is not None]
    hrv_avg_7d = round(sum(hrv_history) / len(hrv_history), 1) if hrv_history else None

    daily_stress = None
    stress_data = garmin_data.get("stress") or {}
    if isinstance(stress_data, dict):
        for key in ("avgStressLevel", "overallStressLevel", "averageStressLevel", "avgStress"):
            if stress_data.get(key) is not None:
                daily_stress = stress_data.get(key)
                break

    return {
        "date": None,  # impostato dal chiamante
        "ctl": ctl,
        "atl": atl,
        "form": round(form, 1) if form is not None else None,
        "eftp": eftp,
        "resting_hr": today_entry.get("restingHR"),
        "hrv": today_entry.get("hrv"),
        "hrv_avg_7d_precedenti": hrv_avg_7d,
        "sleep_hours": sleep_hours,
        "stress_medio": daily_stress,
    }


def summarize_activities(activities: list[dict]) -> list[dict]:
    """Riassunto compatto delle attivita' (dopo la deduplica) da passare al
    giudizio AI, cosi' puo' commentare la seduta del giorno senza dover
    rifare il parsing delle zone."""
    summary = []
    for a in activities:
        summary.append(
            {
                "nome": a.get("activityName"),
                "tipo": _activity_type_key(a),
                "durata_min": round((a.get("duration") or 0) / 60, 1) or None,
                "distanza_km": round((a.get("distance") or 0) / 1000, 1) or None,
                "potenza_media": a.get("avgPower") or a.get("averagePower"),
            }
        )
    return summary


# ---------------------------------------------------------------------------
# 4. Giudizio (equivalente del "Riepilogo mattino" fatto da Claude in chat)
# ---------------------------------------------------------------------------

def ask_anthropic_for_judgment(metrics: dict, activities_today: list[dict]) -> str:
    """Chiede ad Anthropic un breve giudizio/avviso sulla giornata, con le
    stesse regole gia' in uso (avviso HRV solo se la media 7gg e' sotto
    fascia da >=2 giorni + seduta di qualita', avviso sonno <6h, ecc.)

    Richiede la variabile d'ambiente ANTHROPIC_API_KEY.
    """
    import anthropic

    client = anthropic.Anthropic()  # legge ANTHROPIC_API_KEY dall'ambiente

    prompt = f"""
Sei il coach di endurance di Sebastiano. Dati di oggi ({metrics.get("date")}):
{json.dumps(metrics, ensure_ascii=False, indent=2)}

Attivita' di oggi (gia' deduplicate Fenix/Edge):
{json.dumps(activities_today, ensure_ascii=False, indent=2)}

Regole da rispettare (gia' decise in precedenza, non cambiarle):
- HRV: avviso solo se "hrv_avg_7d_precedenti" indica una media sotto la norma
  da almeno 2 giorni E oggi risulta una seduta di qualita' (soglia, VO2max,
  sweet spot, non una semplice Z2). Se non hai abbastanza contesto per
  giudicarlo con certezza, non sollevare l'avviso.
- Sonno < 6 ore: avviso.
- Nessuna modifica automatica al piano: solo una proposta testuale, mai
  applicata senza conferma esplicita di Sebastiano.
- Se un valore e' null/mancante, dillo esplicitamente invece di ignorarlo o
  di inventarlo.

Scrivi un giudizio breve (massimo 5 righe), in italiano, solo se c'e'
qualcosa di rilevante da segnalare. Altrimenti scrivi una sola riga che dice
che non ci sono avvisi.
"""

    message = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )

    # La risposta puo' contenere blocchi di tipo diverso (es. "thinking"
    # prima del testo finale): prendiamo il primo blocco di tipo "text",
    # non semplicemente il primo blocco in assoluto.
    for block in message.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return "(giudizio non disponibile: nessun blocco di testo nella risposta dell'AI)"


# ---------------------------------------------------------------------------
# 5. Scrittura del risultato
# ---------------------------------------------------------------------------

TEST_OUTPUT_PATH = DATA_DIR / "test-output.json"


def write_output(payload: dict, *, test_mode: bool) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if test_mode:
        # Un test manuale su una data passata non deve mai sovrascrivere
        # data/latest.json (che rappresenta "oggi") ne' sporcare lo storico:
        # va in un file separato, cosi' si puo' verificare senza rischi.
        TEST_OUTPUT_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return

    LATEST_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    test_date_raw = (os.environ.get("TARGET_DATE") or "").strip()
    test_mode = bool(test_date_raw)

    if test_mode:
        try:
            target_date = date.fromisoformat(test_date_raw)
        except ValueError:
            print(
                f"TARGET_DATE non valida ('{test_date_raw}'), attesa YYYY-MM-DD",
                file=sys.stderr,
            )
            return 1
    else:
        target_date = date.today() - timedelta(days=1)  # i dati di "ieri"

    try:
        garmin_data = fetch_garmin(target_date)
        intervals_history = fetch_intervals(target_date)
    except Exception as exc:  # pragma: no cover
        print(f"Errore nel recupero dati: {exc}", file=sys.stderr)
        return 1

    garmin_data["activities"] = deduplicate_activities(garmin_data.get("activities", []))
    metrics = compute_training_metrics(garmin_data, intervals_history)
    metrics["date"] = target_date.isoformat()

    activities_today = summarize_activities(garmin_data["activities"])

    try:
        judgment = ask_anthropic_for_judgment(metrics, activities_today)
    except Exception as exc:  # pragma: no cover
        judgment = f"(giudizio non disponibile: {exc})"

    payload = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "metrics": metrics,
        "attivita_oggi": activities_today,
        "judgment": judgment,
    }

    write_output(payload, test_mode=test_mode)
    out_path = TEST_OUTPUT_PATH if test_mode else LATEST_PATH
    print(f"Aggiornato {out_path} per il {target_date.isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
