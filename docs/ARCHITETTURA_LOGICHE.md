# Travian Agent — Documentazione delle Logiche

Documento tecnico completo delle logiche implementate nell'agente, dei moduli
che le realizzano e delle connessioni tra loro.

## Indice

- [1. Panoramica architetturale](#1-panoramica-architetturale)
- [2. Entry point](#2-entry-point)
  - [2.1 main.py — loop da terminale](#21-mainpy--loop-da-terminale)
  - [2.2 app.py — dashboard Streamlit](#22-apppy--dashboard-streamlit)
- [3. Moduli infrastrutturali](#3-moduli-infrastrutturali)
  - [3.1 config.py](#31-configpy)
  - [3.2 models.py](#32-modelspy)
  - [3.3 selectors.py](#33-selectorSpy)
  - [3.4 browser.py](#34-browserpy)
  - [3.5 sensors.py](#35-sensorspy)
- [4. Moduli decisionali](#4-moduli-decisionali)
  - [4.1 builder.py — Smart Building Planner](#41-builderpy--smart-building-planner)
  - [4.2 building_prereqs.py — catalogo prerequisiti](#42-building_prereqspy--catalogo-prerequisiti)
  - [4.3 brain.py — cervello a regole / LLM](#43-brainpy--cervello-a-regole--llm)
- [5. Moduli esecutivi](#5-moduli-esecutivi)
  - [5.1 actions.py — ActionExecutor](#51-actionspy--actionexecutor)
- [6. Orchestrazione](#6-orchestrazione)
  - [6.1 orchestrator.py](#61-orchestratorpy)
- [7. Moduli autonomi](#7-moduli-autonomi)
  - [7.1 farming.py](#71-farmingpy)
  - [7.2 expansion.py](#72-expansionpy)
  - [7.3 cropper.py](#73-cropperpy)
  - [7.4 ai_scout_brain.py](#74-ai_scout_brainpy)
  - [7.5 replenishment.py](#75-replenishmentpy)
  - [7.6 defense.py](#76-defensepy)
  - [7.7 hero.py](#77-heropy)
- [8. Connessioni tra le logiche](#8-connessioni-tra-le-logiche)
- [9. File di stato e persistenza](#9-file-di-stato-e-persistenza)

---

## 1. Panoramica architetturale

L'agente segue il ciclo classico **Sense → Decide → Act**, ripetuto per
ogni villaggio di ogni account configurato:

```
┌──────────┐   Playwright/Chromium   ┌──────────┐
│ browser  │ ──────────────────────► │ sensors  │──► GameState
│ (driver) │                         │ (parser) │
└──────────┘                         └──────────┘      │
                                                      ▼
┌──────────┐                         ┌────────────────────────┐
│ actions  │ ◄────── Action ─────── │ brain + builder +      │
│ (esecuz.)│                         │ moduli autonomi        │
└──────────┘                         └────────────────────────┘
```

- **`sensors.py`** traduce il DOM in un `GameState` tipizzato.
- **`brain.py`** trasforma lo `GameState` in una `Action` (priorità a
  catena), appoggiandosi a **`builder.py`** per la scelta degli upgrade.
- **`actions.py`** esegue l'`Action` sul browser.
- **`orchestrator.py`** tiene in vita il ciclo 24/7 per ogni account,
  riavvia i browser caduti e innesta i moduli autonomi nel giro.
- **`config.py` / `models.py` / `selectors.py`** sono il collante:
  configurazione, dataclasses condivise e selettori CSS centralizzati.

Punto chiave del design: **il brain decide solo "cosa fare"** mentre
**i moduli autonomi** (farming, difesa, espansione, eroe, replenish,
AI scout) hanno ciascuno un proprio mini-ciclo con stato persistito su
disco e sono invocati dall'orchestratore in punti precisi del giro.

---

## 2. Entry point

### 2.1 main.py — loop da terminale

`python main.py [--account ID] [--once] [--dump] [--login] [--headed] [--brain rules|llm]`

Logiche applicate:

- **Guardia anti-duplicati (PID lock)**: `_acquire_agent_lock` scrive
  `state/agent_<id>.pid` per ogni account da avviare. Se un PID file
  punta a un processo vivo, l'avvio è rifiutato (evita bot doppi/orfani).
  `_pid_alive` verifica l'esistenza del processo in modo portabile
  (`OpenProcess` su Windows, `os.kill(pid, 0)` altrove). All'uscita
  `_release_agent_lock` rimuove i file.
- **Caricamento account**: `load_accounts()` (accounts.json → fallback
  `.env`); `--account` filtra per `account_id`.
- **Login manuale** (`--login`): apre browser visibile, attende il
  marker di login e salva `state/session_<id>.json`, poi esce.
- **Dispatch**: 1 account → `run_single_account_loop`; N account →
  `run_all_accounts` (un task asyncio per account, isolati).
- **Pulizia flag**: all'avvio rimuove eventuali `stop_<id>.flag`
  residui che altrimenti terminerebbero l'agente al primo ciclo.
- **Patch Windows** (in `travian_agent/__init__.py`, attiva a ogni
  import del package): sopprime il rumore `WinError 10054` di
  `_ProactorBasePipeTransport._call_connection_lost` alla chiusura dei
  pipe di Chromium.

### 2.2 app.py — dashboard Streamlit

UI web (porta 8501) che **non contiene logica di gioco**: governa i
processi agente e legge/scrive i file di stato condivisi.

- **Gestione processi**: rileva gli agenti attivi via PID file
  (`find_running_agents`), può avviare `main.py` come subprocess per
  account o per tutti gli account.
- **Controllo selettivo via file-flag** (meccanismo consumato
  dall'orchestratore):
  - `state/stop_<id>.flag` → termina il loop di quell'account;
  - `state/pause_<id>.flag` → sospende il ciclo (attesa 10s a giro).
- **Hot-reload configurazione**: le modifiche fatte nella dashboard a
  `accounts.json` vengono rilette dall'agente a ogni ciclo senza
  riavvio.
- **Lettura stato moduli** per la visualizzazione:
  `scout_<id>.json` (AI Scout/War Room), `hero_<id>.json` (eroe),
  `replenish_<id>.json` (blocchi/congelamenti),
  `cropper_candidates.json` (approvazione tile 9c/15c — human-in-the-loop),
  `build_plan_<id>.json` (preview upgrade), `history_*.jsonl` (storico),
  `expansion_config.json` (override target fondazione).
- **Tab principali**: Controllo & Avvio, Villaggi & Strategia (ruoli,
  focus edifici, unità, target truppe), War Room (AI Scout), Farming &
  Moduli, Espansione & Cropper (approve/reject), Account, Log, Debug.

---

## 3. Moduli infrastrutturali

### 3.1 config.py

Centralizza la configurazione: `Settings` (un'istanza per account) +
config dei moduli + profili per-villaggio.

- **`Settings`**: server_url, credenziali, headless, intervalli del loop
  (`loop_interval_min/max`), delay umani (`action_delay_min/max`), limiti
  coda (`max_build_queue`, `max_resource_queue`, `max_interior_queue`),
  `allow_parallel_build` (costruzione parallela campo+edificio),
  `auto_video_bonus`, `min_net_crop_threshold` (Grain Safety Net
  globale), `strategy_profile`, e le sub-config: `farming`, `expansion`,
  `scout_brain`, `hero`, `defense`, `replenishment`.
- **`VillageConfig`** (per-villaggio, chiave = newdid o nome): `role`
  (hammer/defensive/croplog/feeder/standard), `target_village` (dove
  mandare il surplus), `allowed_units`/`priority_units` (mutualmente
  speculari in `__post_init__`), `building_focus` (override manuale),
  `farm_enabled`/`farm_unit`, `market_slot`, coordinate `x`/`y`,
  `min_net_crop_threshold` (override della soglia globale),
  `defense_enabled`, `troop_targets` (per il replenishment).
- **Sub-config dei moduli**:
  - `FarmingConfig`: enabled, intervallo raid con jitter (12–18 min
    default), farm list e unità preferite.
  - `ExpansionConfig`: `auto_expand`, `founder_village`,
    `expansion_slot`, `target_slots` (coordinate manuali), `crop_min`
    (9c/15c), `cropper_scan` + `scan_radius`.
  - `ScoutBrainConfig`: enabled, `gemini_api_key`/`gemini_model`,
    `scan_radius`, `max_travel_time_minutes`, `min_loot_ratio`,
    `blacklist_hours`, `max_targets_per_wave`.
  - `HeroConfig`: enabled, `auto_adventures`, `heal_threshold`,
    `min_health_to_adventure`, `attribute_focus` (auto→strength se
    profilo hammer, altrimenti resources).
  - `DefenseConfig`: enabled, `panic_threshold_seconds` (180s),
    `fake_troop_threshold`, `dodge_margin_seconds`,
    `recall_delay_seconds`, `min_dodge_troops`, drain via mercato e
    `dodge_target`.
  - `ReplenishmentConfig`: enabled, `auto_block`,
    `fail_streak_threshold`, `siege_waves`, `crop_reserve_pct`,
    `skip_while_building`.
- **Risoluzione per-villaggio**: `village_config_for(name, village_id)`
  cerca per newdid → nome → attributo `name`; `village_role_for` ricade
  sulla legacy `village_roles` e infine su `"standard"`.
- **Persistenza**: `load_accounts` legge `accounts.json` (con backup
  `.bak` se corrotto) e salta le voci malformate; `save_accounts` scrive
  **atomicamente** (tmp+rename, backup `.bak`) e **rifiuta liste vuote**
  su file popolati (anti-perdita configurazione).

### 3.2 models.py

Le dataclass condivise da tutti i moduli — il "linguaggio comune".

- **`Resources`**: scorte (wood/clay/iron/crop), capacità magazzino/
  granaio, `production_per_hour`, `crop_balance` (netturo
  produzione−upkeep da production.php; `None` = non letto, **mai**
  stimato dal lordo). `scarcest()` = risorsa con peggior rapporto
  scorte/capacità.
- **`VillageRole`**: enum strategico con alias (`capital`→croplog,
  `defence`/`shield`→defensive, `economy`→feeder).
- **`Field`**: uno slot del villaggio (`slot_id`≤18 = dorf1 campi;
  19–40 = dorf2 edifici interni con `gid`); `is_empty_site` = area
  vuota (`gid==0`).
- **`BuildQueueItem`**, **`GameState`**: fotografia completa (risorse,
  campi, coda, attacchi, `peer_villages` = snapshot risorse degli altri
  villaggi, CP/coloni, `troops_home`). Helper: `resource_fields`,
  `interior_fields`, code separate, `free_warehouse`/`free_granary`.
- **`ActionType`**: WAIT, UPGRADE_FIELD, SEND_RESOURCES, TRAIN_TROOPS,
  TRAIN_SETTLERS, CLAIM_VIDEO_BONUS, SEND_RAID, SEND_CUSTOM_RAID,
  FOUND_VILLAGE, ALERT, LOGIN, NOOP.
- **`IncomingAttack`**: movimento ostile con `is_likely_fake` (rinforzi,
  scout, pochi uomini visibili → fake; composizione ignota → prudente =
  reale) e `is_threat`.
- **`ScoutTarget`** (radar AI Scout: loot ratio, perdite, blacklist con
  scadenza) e **`AccountPowerAssessment`** (potenza militare, net crop
  impero, saturazione magazzini).
- Costanti `GID_*` degli edifici e `STATIC_TROOP_LABELS` (t1–t30, tre
  tribù).

### 3.3 selectors.py

Tutti i CSS selector e i path del gioco (`dorf1.php`, `dorf2.php`,
`build.php`, `karte.php`, `questmaster.php`, `login.php`), con liste di
alternative per tollerare le differenze di layout T4.4/T4.5/T4.6 e
internazionalizzazione. Se il markup cambia, si tocca solo questo file.

### 3.4 browser.py

`TravianBrowser` = lifecycle Playwright + autenticazione.

- **Delay "umani"**: `human_delay` = uniforme min/max con moltiplicatore
  quasi-gaussiano (¼ delle volte ×1.15); `sleep_human` avvolge ogni
  navigazione (`goto`, `reload`) con il range configurato.
- **Sessione persistente**: alla partenza ricarica
  `storage_state` da `state/session_<id>.json`; a ogni stop salva
  cookie+localStorage.
- **Login ibrido a cascata** (`ensure_logged_in`):
  1. sessione salvata → già autenticati;
  2. login via form (`_automated_login`) con digitazione non istantanea
     (60–140ms/tasto) e rilevamento marker login;
  3. fallimento in modalità visibile → `wait_for_manual_login`
     (polling del marker fino a 300s, copre captcha e lobby);
     in headless → dump debug + errore con suggerimento `main.py --login`.
- **Resilienza**: `is_alive` verifica page aperta; `recover` = stop +
  restart completo (usato dall'orchestratore dopo errori).
- **`dump_debug`**: screenshot+HTML in `debug/<account>/`.

### 3.5 sensors.py

`GameObserver`/`TravianParser` = i sensori: JS Injection + CSS selectors
con fallback multipli. Produce il `GameState` e i dati di supporto dei
moduli.

- **Economia**: `extract_resources` (stock bar), `read_fields` (dorf1,
  slot 1–18), `read_interior_fields` (dorf2, slot 19–40 con `gid`),
  `read_build_queue` (coda con timer e `slot_id`), `read_production`
  (production.php → produzione oraria + **crop_balance netto**, la
  chiave del Grain Safety Net).
- **Villaggi**: `get_all_villages` (sidebar → lista `Village` con
  newdid, coordinate, attivo; fallback DOM), `extract_village_name`,
  `read_village_coords` (fallback via mappa per server senza coord in
  sidebar).
- **Militare**: `get_incoming_attacks` (panoramica Punto di Raduno →
  lista `IncomingAttack` classificata), `read_troops_home` (icone
  u1–u30 → dict truppe a casa), `read_training_queue` (truppe in
  addestramento), `read_farm_lists`, `read_combat_reports` (berichte.php:
  coordinate, loot_ratio, perdite, difensori).
- **Mappa**: `scan_map_area` (karte.php → tile villaggi/oasi in raggio),
  `read_map_tile` (position_details.php → occupato, # campi grano 9c/15c,
  link "fonda").
- **Varie**: `has_video_bonus_available` (InfoBox bonus +15%),
  `read_culture_points` (CP residenza), `read_state`/`read_fields`
  compongono il `GameState` completo.

---

## 4. Moduli decisionali

### 4.1 builder.py — Smart Building Planner

Il **planner dinamico degli upgrade**: sostituisce selezioni statiche
con un'unica catena di priorità `select_upgrade`:

1. **Storage** — `storage_needed`: magazzino ≥90% (max di
   wood/clay/iron) o granaio ≥90% → upgrade dello storage esistente o
   nuova costruzione su area vuota.
2. **Grain Safety Net** — `crop_safety_needed`: se `crop_balance` netto
   < soglia (`VillageConfig.min_net_crop_threshold` > globale
   `Settings.min_net_crop_threshold` default 10/h) → `grain_safety_intents`:
   campi grano dal livello più basso, poi Mulino/Forno (esistenti o
   nuovi se prerequisiti ok).
3. **Focus manuale** — `focus_intents`: `building_focus` da dashboard
   batte il ruolo; ogni elemento e' una chiave legacy (es. `barracks`,
   `cropland`) oppure uno step `{"key"|"gid": ..., "target_level": int}`
   (coda ordinata con livelli target, 0 = massimo). Le chiavi si
   traducono in gid via `gids_for_focus_key` (`_focus_steps`), match su
   edifici esistenti + nuova costruzione se assente; gli step gia' al
   target vengono saltati.
4. **Ruolo** — `role_intents`: infrastrutture della `VillageRole`
   sviluppate **bilanciate per livello** (l'ordine per livello crescente
   porta tutti gli edifici al 10 prima che qualcuno superi quella soglia):
   - hammer → Caserma, Scuderia, Officina;
   - defensive → Caserma, Residenza + mura (31/32/33);
   - croplog → Granaio, Magazzino, Mulino, Forno;
   - feeder → Mercato, Magazzino, Granaio.
   La **capitale è auto-rilevata** (presenza Palazzo gid 26) e aggiunge
   il profilo croplog al proprio.
5. **Economia** — `economy_intents`: campo risorsa più debole della
   risorsa più scarsa (`Resources.scarcest()`), con eccezione croplog →
   sempre grano.

Primitive di selezione condivise:

- `upgradeable_fields`: esclude slot già in coda e livello massimo
  (campi 10, 21 in capitale; edifici 20).
- `new_building_intents`: pseudo-`Field` su area vuota per edifici
  assenti, **solo se `prereqs_met` ok** (catalogo T4.x) e con check
  `capital_only` (es. Grande Caserma, Scultore).
- `construct_gids`: mappa `slot_id → gid` per le nuove costruzioni
  (l'esecutore costruisce con `dorf2.php?a=<gid>&id=<slot>`).
- `local_spend_intents`: interni dal livello più alto — usati dal
  resource drain e dall'emergency overflow (smaltimento risorse).
- `plan_upgrades`: simula i prossimi N upgrade (default 4) incrementando
  livelli su una copia dello stato → preview dashboard persistita con
  `save_build_plan` in `state/build_plan_<account>.json`.

### 4.2 building_prereqs.py — catalogo prerequisiti

Catalogo T4.x dei prerequisiti per **nuove costruzioni**:
`_PREREQS[gid] = ([(gid_richiesto, livello_min)], capital_only)`.

- Copre mulini/forni (Mulino: cropland 5; Forno: cropland 10 + Mulino 5),
  catena militare (Caserma: Municipio 3 + Raduno 1; Scuderia: Accademia
  5 + Fabbro 3; Officina: Accademia 10 + Municipio 5), commercio
  (Mercato: Municipio 3 + Granaio 1 + Magazzino 1), residenza/palazzo,
  edifici solo-capitale e tribù-specifici (Trappolaio gallo, Birreria
  teutonica, Abbeveratoio romano, Acquedotto egizio).
- `prereqs_met` ritorna `(ok, motivo_blocco)`; i prerequisiti sui campi
  usano il livello massimo dei campi del tipo richiesto.
- `FOCUS_KEY_GIDS`: chiavi dashboard (`"barracks"`, `"wall"`→tupla mura
  per tribù, gid numerico diretto) → gid.
- Serve a `builder.new_building_intents` e `focus_intents`/`role_intents`
  per non proporre edifici non costruibili.

### 4.3 brain.py — cervello a regole / LLM

`RuleBasedBrain` = **albero di decisione**: le regole sono valutate in
ordine di priorità, la prima che produce un'Action vince
(`DEFAULT_RULES`):

1. `rule_alert_under_attack` — attacchi in arrivo → `ALERT` (la difesa
   attiva è del modulo `defense.py`, qui solo notifica).
2. `rule_upgrade_storage` — storage ≥90% → `UPGRADE_FIELD` sullo storage
   (gid o keyword multi-lingua), inclusa nuova costruzione su area vuota.
3. `rule_grain_safety` — margine grano sotto soglia → solo upgrade
   grano/Mulino/Forno.
4. `rule_emergency_overflow` — storage al 100% e upgrade non avviabile:
   in cascata → (a) spesa locale sull'upgrade più costoso,
   (b) addestramento in Caserma/Scuderia/Officina con `priority_units`
   del villaggio, (c) `SEND_RESOURCES` dell'eccedenza (~15% capacità)
   al peer con più spazio (`_best_peer_for_surplus` su `peer_villages`,
   mercato richiesto), (d) `ALERT`.
5. `rule_building_focus` — `building_focus` da dashboard (batte ruolo).
6. `rule_role_buildings` — infrastrutture di ruolo bilanciate.
7. `rule_upgrade_weakest_field` — campo più debole della risorsa scarsa
   (croplog → grano).
8. `rule_wait` — fallback: `WAIT` con ETA della prima costruzione.

Logiche trasversali del brain:

- **Coda parallela** (`_queue_free`): se `allow_parallel_build`, i
  limiti sono separati per dorf1 (`max_resource_queue`) e dorf2
  (`max_interior_queue`), entrambi sotto `max_build_queue`; altrimenti
  coda unica.
- **Grain safety ha precedenza assoluta**: tutte le regole di upgrade
  chiamano `builder.crop_safety_needed` e si auto-annullano quando il
  margine è sotto soglia (tranne `rule_grain_safety` stessa).
- **Delega a builder**: quasi ogni regola usa gli intenti di
  `builder.py` (storage/grain/focus/role/economy/local_spend): il brain
  decide la categoria, builder produce i candidati ordinati con
  candidati alternativi e `construct` per le nuove costruzioni.
- **`LLMBrain`**: stub pronto (serializza lo stato → prompt → parse),
  fallback su `RuleBasedBrain` finché non è collegato un client LLM.

---

## 5. Moduli esecutivi

### 5.1 actions.py — ActionExecutor

Esegue un'`Action` sul browser. `execute()` dispatch per `ActionType`;
prima di tutto `_ensure_village(village_id)` forza il contesto villaggio
giusto (`newdid` nell'URL, verificato dopo la navigazione).

- **`_upgrade_field`**: apre `build.php?id=<slot>` preservando newdid;
  per le nuove costruzioni usa `dorf2.php?a=<gid>&id=<slot>` (da
  `params.construct`); itera i candidati finché uno è cliccabile.
- **`_send_resources`**: mercato (slot esplicito o gid 17) → tab
  "Invia risorse" → quantità **cappate alla portata dei mercanti**
  (`_calculate_capped_resources`) → destinazione: nome villaggio con
  autocomplete, o coordinate x|y → conferma.
- **`_send_raid`**: Punto di Raduno `tt=99` → farm list (configurata o
  prima disponibile) → invio raid.
- **`_send_custom_raid`**: Punto di Raduno `tt=2` → inserimento
  coordinate libere e truppe per target multipli (usato da AI Scout e
  dalla Difesa; `move_type="2"` per rinforzi).
- **`_train_troops`**: legge stock + unità addestrabili
  (`_extract_training_data`), calcola il lotto massimo sostenibile con
  margine risorse (`_pick_training_batch`, res_margin 95%, crop 90%),
  privilegia `priority_units`/`allowed_units` del villaggio; modalità
  `unit_quantities` (dal replenishment) = quantità mirate; fallback
  legacy "max link"/1 unità.
- **`_train_settlers`**: Residenza/Palazzo → input coloni fino al target
  (3).
- **`_found_village`**: karte.php → tile → click "Fonda".
- **`_claim_video_bonus`**: apre shop/InfoBox → video ad per le 4
  risorse → attesa completamento video (incl. iframe) → chiusura overlay.
- **`claim_village_tasks`**: questmaster → riscossione premi per
  villaggio.
- `WAIT`/`NOOP`/`ALERT`/`LOGIN`: gestiti direttamente nel dispatch
  (`ALERT` logga, `LOGIN` chiama `ensure_logged_in`).

---

## 6. Orchestrazione

### 6.1 orchestrator.py

Cuore del funzionamento: istanzia per-account i manager dei moduli
(`FarmingManager`, `ExpansionManager`, `AIScoutBrain`, `HeroManager`,
`DefenseManager`, `TroopReplenishmentManager`, cache in dict globali),
li collega e li esegue ai punti giusti del ciclo.

**`run_single_account_loop`** — loop infinito per account:

1. **Hot-reload** `accounts.json` → aggiorna `settings` in-place
   (villaggi, ruoli, moduli, intervalli) a ogni giro.
2. **File-flag dashboard**: `stop_<id>.flag` → uscita pulita (e rimozione
   PID file); `pause_<id>.flag` → sleep 10s e ciclo saltato.
3. **Browser recovery**: se non vivo → `browser.recover()`; observer ed
   executor reistanziati/agganciati alla nuova page.
4. `monitor_all_villages` (il giro vero), poi **intervallo casuale**
   `loop_interval_min–max` secondi.
5. **Error handling**: `consecutive_errors` → **backoff esponenziale con
   jitter** (`_calculate_backoff`: base 10s ×2^n, cap 300s, ±10%), tenta
   `browser.reload()` o `recover()`, non muore mai.
6. **`run_all_accounts`**: un task asyncio per account via
   `_supervised_account_loop` (crash isolati), `asyncio.gather`.

**`monitor_all_villages`** — il ciclo per-villaggio:

1. Lista villaggi dalla sidebar (`get_all_villages`), backfill
   coordinate da `VillageConfig` o via `read_village_coords` →
   `_persist_village_coords` su accounts.json.
2. **Bonus video +15%**: ogni ~7.8h o su notifica → `CLAIM_VIDEO_BONUS`.
3. Per ogni villaggio:
   - navigazione al `newdid` → `claim_village_tasks` (questmaster);
   - **Sense**: `read_state` + `read_fields` + `read_interior_fields` +
     `read_production` (crop_balance) → `GameState`; `peer_villages` =
     snapshot risorse degli altri villaggi (per il surplus routing);
   - **Modulo Difesa** (`defense_mgr.handle_village`) con priorità sulle
     azioni ordinarie;
   - **Decide**: `brain.decide(state)` → `Action`; arricchimento
     coordinate target per `SEND_RESOURCES` (sidebar/`VillageConfig`);
   - **Act**: `executor.execute(action, village_id)`; storico JSONL
     (`_append_history`); `plan_upgrades` → `save_build_plan` (preview);
   - **Moduli autonomi in sequenza**: farming → AI Scout →
     replenishment → feeder espansione → pausa umana (2.5–5s) →
     villaggio successivo (errori per-villaggio isolati, mai fatali).
4. **Step account-level a fine giro**: `expansion_mgr.step` (fondazione),
   `hero_mgr.run` (eroe), `assess_account_power` (Global Power Assessment
   → salvato nello scout state, letto dalla War Room dashboard).

---

## 7. Moduli autonomi

### 7.1 farming.py

Raid periodici dalle farm list del Punto di Raduno (gid 16).

- **Scheduler con jitter anti-ban**: `FarmingManager` mantiene
  `next_due` per villaggio in `state/farming_<acc>.json`; intervallo
  sempre `uniform(interval_min, interval_max)` — mai due cicli uguali.
- **Condizioni**: `farming.enabled` globale + `farm_enabled` del
  villaggio + rally point presente + `is_due` scaduto.
- **Verifica truppe**: `read_troops_home`; se dict vuoto → tenta
  comunque (raid vuoto fallisce senza danni); se non ci sono raider
  (`_LIGHT_RAIDERS`/`_UNIT_KEY_ALIASES`, match nome ITA→icone uN) →
  retry breve (4–7 min).
- Emette `SEND_RAID` (farm_list + unità) → `ActionExecutor`.

### 7.2 expansion.py

Fondazione autonoma di nuovi villaggi (`ExpansionManager`, stato su
`state/expansion_<acc>.json`). Step una volta per ciclo account, dopo
il giro villaggi.

- **Config effettiva**: merge `ExpansionConfig` + override dashboard
  (`state/expansion_config.json`: enable, source_village, slot,
  target_coordinates).
- **Cropper scan**: se `cropper_scan` e scaduto cooldown (3h; retry
  breve 10min se 0 tile letti) → `MapScanner.scan` → candidati pending.
- **Founder selection** (`_pick_founder`): `founder_village` configurato
  o auto (primo con Residenza/Palazzo ≥10, poi ≥1); `expansion_slot`
  può forzare lo slot residenza.
- **Sequenza gate**:
  1. CP via `read_culture_points` → insufficienti → esci (e `feed_target`
     resettato);
  2. coloni (`count_settlers` su icone u10/u20/u30) → <3 →
     `TRAIN_SETTLERS` + **`feed_target = founder` attiva la rete
     feeder**: nel giro successivo ogni altro villaggio con mercato
     invia il surplus (`feeder_surplus` = oltre il 55% di capienza, cap
     2000/risorsa) al fondatore;
  3. **target**: prima i cropper `approved` (priorità sui manuali), poi
     `target_slots`; `read_map_tile` verifica libero + `crop_fields`
     ≥ `crop_min`; record nello store accettato solo se `approved`;
  4. `FOUND_VILLAGE` → status `settling` → successo `settled` +
     `founded_tiles`; fallimento → torna `approved` + cooldown 15 min.

### 7.3 cropper.py

Cropper Finder **human-in-the-loop**: il bot propone, l'uomo dispone.

- `MapScanner.scan`: per ogni villaggio con coordinate →
  `scan_map_area` nel raggio → tile liberi non-oasi → `read_map_tile`
  per rilevare 9c/15c (`CROPPER_FIELD_COUNTS`) → `CropperCandidate`
  (tipo, bonus % oasi grano entro 3 campi, distanza in ore a
  ~5 campi/h coloni, villaggio più vicino) → **status `pending`** in
  `state/cropper_candidates.json`.
- `CropperStore`: CRUD su file condiviso con la dashboard (approve/
  reject); `upsert` non sovrascrive stati decisi.
- **Guardia incrociata**: `ExpansionManager._pick_target` fonda **solo**
  candidati `approved`; `settling`/`settled` aggiornati automaticamente.

### 7.4 ai_scout_brain.py

Intelligence di guerra + raid dinamici (`AIScoutBrain`, stato
`state/scout_<acc>.json`).

- **Target DB**: `update_targets_from_map` indicizza i tile scansionati
  (popolazione, nome, alleanza, distanza, oasi).
- **Report processing**: `process_combat_reports` aggiorna
  `last_loot_ratio`, accumula perdite, e applica **auto-blacklist**
  (perdite o difensori > 0 → blacklist `blacklist_hours` ore; scaduta
  dopo esito pulito → riabilitato).
- **Selezione**: `get_candidate_targets` filtra blacklist scadute,
  distanza ≤ 2×scan_radius, loot_ratio ≥ soglia; `decide_raid_plan`
  → **Gemini AI** (`gemini_evaluate_farming_targets`, prompt con stato
  account+target → JSON piano raid) con **fallback deterministico**
  `heuristic_evaluate_farming_targets` (score = loot×pop/dist^1.2,
  truppe proporzionali alla popolazione target, max N target).
- **Global Power Assessment** (`assess_account_power`): aggrega per
  impero — potenza militare (`TROOP_STATS`: att + media difese), upkeep
  totale, **net crop impero** (produzione − upkeep), saturazione
  magazzini, `ready_for_expansion`; salvato e mostrato in War Room.
- Emette `SEND_CUSTOM_RAID` con il piano (target+troop dict).

### 7.5 replenishment.py

Smart Troop Replenishment (`TroopReplenishmentManager`, stato
`state/replenish_<acc>.json`, visibile in dashboard).

- **Formula deficit**: `da_addestrare = target − (truppe_a_casa +
  truppe_in_coda)`, per i `troop_targets` configurati (chiavi `tN` o
  nomi unità → normalizzati via `unit_label_to_key`/`normalize_unit_key`
  su `STATIC_TROOP_LABELS`).
- **Anti-suicidio (Scenario 1)**: `process_combat_reports` — report con
  perdite su target difeso e bottino ≤20% → `blocked_targets` e
  `fail_streaks`; a soglia → `frozen_units` (unità offensive congelate:
  quelle perse o le `RAID_UNIT_PREFERENCE`); esito positivo → reset
  streak e sblocco.
- **Sostenibilità difesa (Scenario 2)**: `update_siege_state` — villaggio
  con ≥ `siege_waves` attacchi in arrivo → `frozen_villages` (niente
  rimpiazzo né rinforzi); assedio rientrato → sblocco automatico.
- **`plan_training` filtri**: villaggio congelato → stop;
  `skip_while_building` → cede a code costruzioni attive; **guardia
  grano** → niente addestramento sotto `crop_reserve_pct` del granaio o
  con produzione netta ≤0 e scorte < 2× riserva.
- Piano → `TRAIN_TROOPS` con `unit_quantities` mirate.

### 7.6 defense.py

Difesa reattiva (`DefenseManager`, stato `state/defense_<acc>.json`:
dodges e recalls sopravvivono ai riavvii). Eseguito **prima** del brain
ordinario per ogni villaggio.

- **Fase 0 — recall/dodge pendenti**: processa i rientri scaduti e le
  fughe programmate dovute.
- **Threat assessment**: `get_incoming_attacks` → filtra `is_threat` e
  `not is_likely_fake` → attacco reale più vicino.
- **Monitoraggio** (> panic_threshold 180s): registra `PendingDodge`
  (truppe lette ora, `dodge_at = arrival − dodge_margin`), destinazione
  = `dodge_target` (nome o "x|y") o **villaggio proprio più vicino** con
  coordinate.
- **Emergenza** (≤ panic): **Resource Drain** —
  1. addestramento massivo (98% res margin / 90% crop) nei trainer;
  2. upgrade più costoso accodabile nel dorf2 (JS cost-sum);
  3. surplus residuo via mercato se `drain_via_market` configurato
     (keep 200/risorsa).
  **Troop Dodging**: `SEND_CUSTOM_RAID` con `move_type=2` (rinforzo)
  verso destinazione; al successo registra `PendingRecall`
  (`arrival + recall_delay`) → Smart Recall automatico che rispedisce a
  casa dal villaggio ospite.

### 7.7 hero.py

Eroe autonomo (`HeroManager`, snapshot `state/hero_<acc>.json` per la
dashboard), una volta per ciclo account con cooldown 4–7 min.

Flusso: `hero.php` → `_sense` (JS: salute, stato home/moving/adventure/
dead, avventure, punti liberi, livello) → **auto-cura** (inventario →
unguento/pozione → Usa) se HP < `heal_threshold` → **punti attributo**
(focus: config o auto → hammer=strength, altrimenti resources) →
**avventura** se `auto_adventures` + a casa + HP ≥ `min_health_to_adventure`
(navigazione su `/hero/adventures` + click "Esplora"/"Start" multi-lingua).
Livello=0 → dump HTML per calibrazione selettori. Best-effort totale:
mai crashare il ciclo.

---

## 8. Connessioni tra le logiche

Mappa delle dipendenze funzionali (chi chiama/dipende da chi):

```
main.py ──► orchestrator.run_all_accounts / run_single_account_loop
            │
            ├─► config.load_accounts (hot-reload ogni ciclo)
            ├─► browser.TravianBrowser (lifecycle, login, recovery)
            ├─► sensors.GameObserver ──► models.GameState
            │
            ├─► brain.RuleBasedBrain.decide(state)
            │      └─► builder.* intents ──► building_prereqs.prereqs_met
            │      └─► config.Settings.village_role_for / village_config_for
            │
            ├─► actions.ActionExecutor.execute(action)  ◄── tutte le Action
            │
            ├─► defense.DefenseManager.handle_village   (PRIMA del brain)
            │      ├─► observer.get_incoming_attacks / read_troops_home
            │      └─► executor (TRAIN/SEND_CUSTOM_RAID/SEND_RESOURCES)
            │
            ├─► farming.FarmingManager.run_for_village
            │      ├─► observer.read_troops_home
            │      └─► executor.execute(SEND_RAID)
            │
            ├─► ai_scout_brain.AIScoutBrain
            │      ├─► observer.scan_map_area / read_combat_reports / read_troops_home
            │      ├─► Gemini API (o fallback euristico)
            │      └─► executor.execute(SEND_CUSTOM_RAID)
            │
            ├─► replenishment.TroopReplenishmentManager
            │      ├─► observer.read_combat_reports / read_troops_home / read_training_queue
            │      ├─► condivide `reports` con AI Scout (lettura unica)
            │      └─► executor.execute(TRAIN_TROOPS unit_quantities)
            │
            ├─► expansion.ExpansionManager (feeder nel giro; step a fine)
            │      ├─► cropper.MapScanner + CropperStore (approved-only)
            │      ├─► observer.read_culture_points / read_map_tile / read_troops_home
            │      └─► executor.execute(TRAIN_SETTLERS / FOUND_VILLAGE / SEND_RESOURCES)
            │
            ├─► hero.HeroManager.run (a fine giro, cooldown)
            │      └─► browser.page JS (cura/attributi/avventure)
            │
            └─► assess_account_power ──► scout state ──► dashboard War Room
```

Connessioni logiche principali:

- **`builder` ↔ `brain`**: il brain decide la categoria (storage/grain/
  focus/ruolo/economia), builder produce i `Field` candidati ordinati e
  `construct_gids` per le nuove costruzioni; `building_prereqs` valida.
- **`config` pervasivo**: `Settings`/`VillageConfig` guidano ruolo,
  focus, soglia grano, unità consentite, market/rally slot, e ogni
  flag dei moduli (riletto a caldo a ogni ciclo).
- **`models` = DTO**: `GameState`, `Action`, `Field`, `IncomingAttack`,
  `ScoutTarget` scorrono sensors→brain/moduli→actions.
- **`selectors` condiviso** da browser, sensors, actions, defense, hero.
- **Condivisione sensori**: `read_combat_reports` letto una volta per
  villaggio e riusato da AI Scout e Replenishment; `read_troops_home`
  condiviso da farming/scout/defense/replenish/espansione.
- **Difesa ↔ Replenishment**: `incoming_attacks` e i report alimentano
  sia il threat assessment sia lo siege-state.
- **Cropper ↔ Expansion ↔ Dashboard**: scanner → `pending` → approvazione
  umana via `cropper_candidates.json` → `approved` → fondazione →
  `settling`/`settled`.
- **Feeder network**: `feed_target` di Expansion attiva invii
  `SEND_RESOURCES` dagli altri villaggi; stesso meccanismo del
  `rule_emergency_overflow` del brain (mercato + peer_villages).
- **Dashboard ↔ stato**: `app.py` legge tutti i `state/*.json` e scrive
  flag `stop_/pause_` + `accounts.json` + `expansion_config.json`.
- **Anti-duplicati**: PID file `agent_<id>.pid` (scritti da main.py,
  letti/rimossi da dashboard e orchestratore).

---

## 9. File di stato e persistenza

Tutto in `state/` (volume persistente su Docker/Coolify):

| File | Produttore | Contenuto |
|---|---|---|
| `accounts.json` (+`.bak`/`.tmp`) | dashboard, config | account, villaggi, moduli |
| `session_<acc>.json` | browser | cookie/localStorage login |
| `agent_<acc>.pid` | main.py | PID agente attivo (anti-doppio) |
| `stop_<acc>.flag` | dashboard → orchestrator | stop selettivo account |
| `pause_<acc>.flag` | dashboard → orchestrator | pausa selettiva account |
| `history_<acc>.jsonl` | orchestrator | storico decisioni per villaggio |
| `build_plan_<acc>.json` | builder | preview prossimi upgrade |
| `farming_<acc>.json` | farming | `next_due` raid per villaggio |
| `expansion_<acc>.json` | expansion | founded tiles, feed_target, cooldown |
| `expansion_config.json` | dashboard → expansion | override auto-expand/target |
| `cropper_candidates.json` | cropper + dashboard | candidati 9c/15c con status |
| `scout_<acc>.json` | ai_scout_brain | target DB, blacklist, assessment |
| `defense_<acc>.json` | defense | dodges/recalls pendenti |
| `hero_<acc>.json` | hero | snapshot eroe per dashboard |
| `replenish_<acc>.json` | replenishment | blocked/frozen units, events |
| `agent.log` | logging | log applicativo |

File di debug in `debug/<account>/`: dump HTML/screenshot (login
fallito, mercato, stato eroe, dorf1).
