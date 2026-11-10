# Travian Agent

Agente Python didattico per interagire con Travian Legends (o altri giochi
browser di strategia) tramite automazione del browser.

> **Avviso importante** — I Termini di Servizio di Travian vietano bot e
> script automatizzati. L'uso su un account reale puo' causare il **ban
> permanente**. Questo progetto e' pensato per studiare automazione web,
> parsing del DOM e architettura di agenti. Usalo solo a scopo didattico
> (es. server di test locali) e a tuo rischio.

## Architettura

| Modulo | File | Ruolo |
|---|---|---|
| Driver | `travian_agent/browser.py` | Playwright: login ibrido, sessione persistente, delay umani |
| Sensori | `travian_agent/sensors.py` | Parsing DOM -> `GameState` (risorse, campi, coda, attacchi) |
| Cervello | `travian_agent/brain.py` | Regole deterministiche + stub `LLMBrain` pluggable |
| Attore | `travian_agent/actions.py` | Esegue le `Action` (upgrade edifici, ecc.) |
| Loop | `main.py` | Ciclo Sense -> Decide -> Act con backoff e intervalli casuali |
| Selettori | `travian_agent/selectors.py` | Tutti i CSS selector in un unico file, con alternative |

## Interfaccia Grafica Web (Streamlit)

Puoi gestire e configurare l'intero agente direttamente dal browser senza toccare file di testo o terminale:

```powershell
# Avvia la Dashboard Streamlit
.\.venv\Scripts\python.exe -m streamlit run app.py
# Oppure fai doppio click su start_dashboard.bat
```

Dall'interfaccia potrai:
1. **Configurare i parametri** (URL server di gioco, credenziali, intervalli di tempo, delay).
2. **Effettuare il Login Manuale Assistito** con un click (apre il browser, ti logghi e salva i cookie).
3. **Avviare e fermare il Bot** con visualizzazione dello stato in tempo reale.
4. **Monitorare i Log** e guardare gli screenshot di debug generati.

---

## Setup da Terminale

```powershell
# 1. Attiva il venv e installa le dipendenze
.\.venv\Scripts\pip.exe install -r requirements.txt

# 2. Scarica i browser di Playwright
.\.venv\Scripts\python.exe -m playwright install chromium

# 3. Configura le credenziali
copy .env.example .env
# poi modifica .env con server, username e password
```

## Login (strategia ibrida)

I sistemi anti-bot (captcha, Cloudflare, lobby Travian Games) rendono
fragile il login via script. Il flusso consigliato e' ibrido:

```powershell
# 1. PRIMO AVVIO: login manuale in finestra visibile.
#    Risolvi captcha/lobby a mano; la sessione viene salvata in
#    state/session.json e il programma esce.
.\.venv\Scripts\python.exe main.py --login

# 2. Avvii successivi: i cookie vengono ricaricati e il login saltato.
.\.venv\Scripts\python.exe main.py
```

`ensure_logged_in` gestisce comunque una cascata automatica:
sessione salvata → login via form → (solo se la finestra e' visibile)
attesa del login manuale. In headless un fallimento produce un errore
con suggerimento di rilanciare `--login`.

## Uso

```powershell
# Loop continuo (CTRL+C per fermare)
.\.venv\Scripts\python.exe main.py

# Singola iterazione con finestra visibile: ideale per il debug
.\.venv\Scripts\python.exe main.py --once --headed

# Dump di screenshot + HTML per calibrare i selettori
.\.venv\Scripts\python.exe main.py --dump --once --headed
```

## Deploy su Coolify (Docker)

Il repository include un `Dockerfile` pronto per Coolify (build "Dockerfile" nativo):

- **Tipo di risorsa**: Application → Build pack `Dockerfile`, porta esposta `8501`.
- **Storage persistente**: monta un volume su `/app/state`
  (contiene `accounts.json`, sessioni `session_*.json`, storico, PID e `agent.log`).
  Senza volume, configurazione e sessioni vengono perse a ogni redeploy.
- **Variabili d'ambiente** (Environment → Coolify): `TRAVIAN_SERVER_URL`,
  `TRAVIAN_USERNAME`, `TRAVIAN_PASSWORD`, `HEADLESS=true` ecc.
  (equivalenti a `.env`; servono solo come fallback se `accounts.json` non esiste).
- **Risorse consigliate**: almeno 1 GB di RAM (Chromium headless).

### Login manuale sul server

Il login assistito (`main.py --login`) apre una finestra del browser: su un
server headless non c'e' display, quindi la procedura consigliata e':

1. Esegui `python main.py --login --account <id>` **in locale** per generare
   `state/session_<id>.json`.
2. Copia il file dentro il volume `/app/state` su Coolify
   (pulsante "Terminal/File Manager" della risorsa, oppure `docker cp`).
3. Al prossimo avvio l'agente riutilizza la sessione salvata.

## Note

- **Selettori fragili**: il markup di Travian cambia tra versioni e server.
  Se il parsing non trova qualcosa, lancia `--dump` e aggiorna solo
  `selectors.py`.
- **Login via lobby**: alcuni server richiedono il login attraverso la
  lobby di Travian Games invece del form `login.php`: usa `--login` e
  completa il flusso a mano; il marker `LOGGED_IN_MARKER` resta valido
  dopo il redirect.
- **Sessione persistente**: cookie e localStorage vengono salvati in
  `state/session.json` a ogni avvio/chiusura. Eliminalo per forzare un
  nuovo login. La sessione puo' scadere lato server: in tal caso
  rilancia `--login`.
- **Cervello LLM**: `LLMBrain` in `brain.py` e' uno stub pronto da
  collegare a un client LLM; in assenza ricade sulle regole.
