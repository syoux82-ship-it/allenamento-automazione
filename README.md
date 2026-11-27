# Aggiornamento automatico allenamento (senza Mac, senza costi)

Questo mini-progetto fa girare ogni mattina, in automatico e gratis su
GitHub, lo script che prima veniva eseguito da Claude in chat (riepilogo
mattino): legge Garmin e Intervals.icu, calcola i dati, chiede un giudizio
ad Anthropic e scrive il risultato in `data/latest.json`.

## Come attivarlo (una tantum)

1. **Crea un repository privato su GitHub** (es. `allenamento-automazione`)
   e carica dentro questi file mantenendo la stessa struttura di cartelle
   (`script.py`, `requirements.txt`, `.github/workflows/daily.yml`,
   `data/`).

2. **Aggiungi i secrets** (Settings del repository → Secrets and variables →
   Actions → New repository secret):
   - `GARMIN_EMAIL`
   - `GARMIN_PASSWORD`
   - `INTERVALS_API_KEY`
   - `INTERVALS_ATHLETE_ID` (il tuo, es. 1948657)
   - `ANTHROPIC_API_KEY`

   Non scrivere mai questi valori nel codice o nei file del repository:
   solo nei secrets.

3. **Testa manualmente** prima di fidarti dello schedule: nella scheda
   "Actions" del repository, apri il workflow "Aggiornamento giornaliero
   allenamento" e clicca "Run workflow" (funziona grazie a
   `workflow_dispatch` nel file `.github/workflows/daily.yml`). Controlla
   che `data/latest.json` venga aggiornato e committato.

4. **Lascia fare allo schedule**: una volta verificato che funziona, il
   workflow gira da solo ogni mattina alle 05:00 UTC (vedi commento nel
   file `.github/workflows/daily.yml` per l'ora civile in Italia).

## Cosa resta da completare

Lo script (`script.py`) e' uno scheletro funzionante nella struttura ma
contrassegnato con `TODO` nei punti dove va portata la logica gia'
sviluppata nel progetto Claude (parsing FIT, dedup Fenix/Edge, calcolo
zone/CTL/ATL, formato del giudizio). Questi punti vanno completati prima
di fidarsi del risultato in automatico.

## Come la dashboard legge il risultato

Il file `data/latest.json`, una volta nel repository, e' raggiungibile da
un URL pubblico readonly tipo:

```
https://raw.githubusercontent.com/<utente>/<repo>/main/data/latest.json
```

(se il repository e' privato serve un piccolo accorgimento in piu' per
leggerlo senza esporlo: se arrivi a questo punto, ne parliamo insieme).
La dashboard/PWA puo' quindi leggere questo URL a ogni apertura e mostrare
sempre l'ultimo dato disponibile, da qualunque dispositivo.
