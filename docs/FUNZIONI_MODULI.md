# Travian Agent — Catalogo Funzioni e Logiche dei Moduli

Documento di riferimento completo: **ogni classe e ogni funzione** dei moduli
che compongono l'agente, con la logica implementata. Per la visione d'insieme
delle architetture e delle interazioni si veda `ARCHITETTURA_LOGICHE.md`.

## Indice

- [1. Entry point](#1-entry-point)
  - [1.1 main.py](#11-mainpy)
  - [1.2 app.py — dashboard Streamlit](#12-apppy--dashboard-streamlit)
- [2. Moduli di supporto](#2-moduli-di-supporto)
  - [2.1 config.py](#21-configpy)
  - [2.2 models.py](#22-modelspy)
  - [2.3 selectors.py](#23-selectorspy)
  - [2.4 presets.py](#24-presetspy)
  - [2.5 building_prereqs.py](#25-building_prereqspy)
- [3. Infrastruttura browser e sensori](#3-infrastruttura-browser-e-sensori)
  - [3.1 browser.py](#31-browserpy)
  - [3.2 sensors.py](#32-sensorspy)
- [4. Motore decisionale](#4-motore-decisionale)
  - [4.1 builder.py](#41-builderpy)
  - [4.2 brain.py](#42-brainpy)
- [5. Esecuzione](#5-esecuzione)
  - [5.1 actions.py](#51-actionspy)
- [6. Orchestrazione](#6-orchestrazione)
  - [6.1 orchestrator.py](#61-orchestratorpy)
- [7. Moduli autonomi](#7-moduli-autonomi)
  - [7.1 farming.py](#71-farmingpy)
  - [7.1a farm_store.py](#71a-farm_storepy)
  - [7.2 expansion.py](#72-expansionpy)
  - [7.3 cropper.py](#73-cropperpy)
  - [7.4 ai_scout_brain.py](#74-ai_scout_brainpy)
  - [7.5 replenishment.py](#75-replenishmentpy)
  - [7.6 defense.py](#76-defensepy)
  - [7.7 hero.py](#77-heropy)
  - [7.8 war.py](#78-warpy)

---

## 1. Entry point

### 1.1 main.py

Loop da terminale: `python main.py [--account ID] [--once] [--dump] [--login] [--headed] [--brain rules|llm]`

| Funzione | Logica |
|---|---|
| `_pid_path(account_id)` | Path del PID file `state/agent_<id>.pid`. |
| `_pid_alive(pid)` | Verifica processo vivo in modo portabile (`OpenProcess` su Windows, `os.kill(pid,0)` altrove). |
| `_acquire_agent_lock(accounts)` | Scrive un PID file per account; se un PID esistente punta a processo vivo, l'avvio è rifiutato (**anti bot doppi/orfani**). |
| `_release_agent_lock(paths)` | Rimuove i PID file all'uscita. |
| `_asyncio_exception_handler(loop, context)` | Sopprime il rumore `ConnectionResetError` sui pipe chiusi di Chromium. |
| `parse_args()` | Parsing CLI. |
| `async main()` | Carica `load_accounts()`, filtra per `--account`, applica i lock, rimuove `stop_*.flag` residui, gestisce `--login` manuale, dispatch a `run_single_account_loop` (1 account) o `run_all_accounts` (N account). |

### 1.2 app.py — dashboard Streamlit

UI web (porta 8501). **Nessuna logica di gioco**: governa i processi agente e
legge/scrive i file di stato condivisi con l'orchestratore.

| Funzione | Logica |
|---|---|
| `_pid_alive(pid)` | Verifica processo vivo (duplicata da main.py). |
| `find_running_agents()` | Scansiona `state/agent_*.pid` → lista `(account_id, pid)` attivi. |
| `is_agent_running()` | True se esiste almeno un agente attivo. |
| `stop_all_agents()` | Crea `stop_<id>.flag` per ogni agente attivo → stop ordinato. |
| `set_account_stop(account_id)` / `stop_account(account_id)` | Crea/rimuove il flag di stop per singolo account. |
| `is_account_paused(account_id)` / `set_account_pause(account_id, paused)` | Gestione `pause_<id>.flag` (l'orchestratore dorme 10s a giro). |
| `get_latest_village_snapshots(account_id)` | Ultimi stati per villaggio da `state/history_*.jsonl` (tabella dashboard). |
| `get_scout_brain_data(account_id)` | Legge `state/scout_<id>.json` per la War Room. |
| `get_hero_data(account_id)` | Legge `state/hero_<id>.json`. |
| `get_account_session_info(acc)` | Info sessione/login per account. |
| `_on_village_account_change()` | Callback Streamlit al cambio account selezionato. |

Il resto del file è rendering UI: tab Controllo & Avvio, Villaggi & Strategia
(ruoli, preset, building_queue_plan, troop targets, smithy), War Room (AI Scout
+ War/chiefing), Farming & Moduli, Espansione & Cropper (approve/reject),
Account, Log, Debug.

---

## 2. Moduli di supporto

### 2.1 config.py

Centralizza la configurazione: un'istanza `Settings` per account + sub-config
dei moduli + profili per-villaggio. Persistenza su `accounts.json` (atomica,
con backup `.bak` e guardia anti-lista-vuota) e `.env`.

**Dataclass `VillageConfig`** — profilo strategico di un villaggio:

- `name`, `role` (hammer/defensive/croplog/feeder/standard), `target_village`
- `allowed_units` / `priority_units` (in `__post_init__` rese mutuamente coerenti)
- `building_focus` (focus manuale dashboard) e `building_queue_plan`
  (coda ordinata di step `{key|gid, target_level}`, 0 = massimo)
- `is_bootstrap` (villaggio appena fondato → sequenza startup di `builder.bootstrap_intents`)
- `is_reclaim_source` (villaggio origine di un treno di riconquista → `builder.reclaim_intents`)
- `farm_enabled`/`farm_unit`, `market_slot`, coordinate `x`/`y`
- `min_net_crop_threshold` (override per-villaggio della soglia globale)
- `defense_enabled`, `troop_targets` (replenishment)
- `preset_applied`, `custom_modified`, `troop_replenishment_preset`,
  `smithy_auto_upgrade` (preset Fucina)

**Dataclass sub-config dei moduli:**

- `ExpansionTarget` (x, y, note) — tile candidato alla fondazione.
- `ExpansionConfig` — `auto_expand`, `founder_village`, `expansion_slot`,
  `target_slots`, `crop_min` (9/15 campi grano), `min_culture_margin`,
  `cropper_scan` + `scan_radius`.
- `FarmingConfig` — `enabled`, `interval_min/max_minutes` (jitter anti-ban),
  `farm_list`, `unit`.
- `ScoutBrainConfig` — `enabled`, `gemini_api_key`/`gemini_model`,
  `scan_radius`, `max_travel_time_minutes`, `min_loot_ratio`,
  `blacklist_hours`, `max_targets_per_wave`.
- `HeroConfig` — `enabled`, `auto_adventures`, `heal_threshold`,
  `min_health_to_adventure`, `attribute_focus` (auto → strength se hammer,
  altrimenti resources), `emergency_unpack`.
- `ReplenishmentConfig` — `enabled`, `auto_block`, `fail_streak_threshold`,
  `siege_waves`, `crop_reserve_pct`, `skip_while_building`.
- `DefenseConfig` — `enabled`, `panic_threshold_seconds` (180s),
  `fake_troop_threshold`, `dodge_margin_seconds`, `recall_delay_seconds`,
  `min_dodge_troops`, `drain_via_market`, `drain_target_village`, `dodge_target`.
- `Settings` — server_url, credenziali, `headless`, intervalli loop
  (`loop_interval_min/max`), delay umani (`action_delay_min/max`), limiti coda
  (`max_build_queue`, `max_resource_queue`, `max_interior_queue`),
  `allow_parallel_build`, `auto_video_bonus`, `min_net_crop_threshold`,
  `state_file`/`debug_dir`, `browser_locale`, `navigation_timeout_ms`,
  `tribe`, `villages` (dict nome→VillageConfig), `village_roles` (legacy),
  `strategy_profile` + le sei sub-config dei moduli.

**Funzioni:**

- `Settings.village_config_for(name, village_id)` — risolve la config del
  villaggio per newdid → nome → attributo `name`.
- `Settings.village_role_for(name, village_id)` — `villages` > `village_roles`
  legacy > `"standard"`.
- `_slugify(text)` — normalizza nomi per chiavi file.
- `_read_accounts_json(path)` — legge/valida accounts.json, None se corrotto.
- `_parse_account_item(item)` — costruisce `Settings` da una voce JSON.
- `load_accounts()` — tutti gli account configurati (accounts.json → fallback .env).
- `save_accounts(accounts, allow_empty)` — scrittura atomica tmp+rename,
  backup `.bak`, rifiuta liste vuote su file popolati.
- `get_raw_env(env_path)` — valori grezzi .env senza eccezioni.
- `save_env_settings(values, env_path)` — aggiorna .env.
- `load_settings(env_path, require_credentials)` — singolo Settings da .env.

### 2.2 models.py

DTO condivisi (il "linguaggio comune" sensors→brain→actions).

- **`ResourceType(str, Enum)`** — wood/clay/iron/crop.
- **`Resources`** — scorte, capacità magazzino/granaio,
  `production_per_hour`, `crop_balance` (netto produzione−upkeep da
  production.php; `None` = non letto, mai stimato dal lordo).
  Metodi: `amount(res_type)`, `capacity(res_type)`, `scarcest()` (peggior
  rapporto scorte/capacità).
- **`VillageRole(str, Enum)`** — ruoli strategici con `parse(value)` che
  accetta alias (`capital`→croplog, `defence`/`shield`→defensive,
  `economy`→feeder).
- **`Field`** — uno slot villaggio: `slot_id` (≤18 dorf1 campi; 19–40 dorf2
  con `gid`), `name`, `level`, `resource_type`. Property `is_resource_field`,
  `is_empty_site` (gid==0).
- **`BuildQueueItem`** — `name`, `level`, `remaining_seconds`,
  `is_resource_field`, `slot_id`.
- **`Village`** — `name`, `url`, `village_id` (newdid), `is_active`, `role`,
  `x`/`y`.
- **`GameState`** — fotografia completa: `resources`, `fields`,
  `build_queue`, `incoming_attacks`, `village_name`/`village_id`,
  `peer_villages` (snapshot risorse altri villaggi), `culture_points`/
  `culture_points_required`, `settlers_home`, `troops_home`, `timestamp`.
  Metodi: `resource_fields()`, `interior_fields()`, `resource_queue()`,
  `interior_queue()`, `free_warehouse()`, `free_granary()`.
- **`ActionType(str, Enum)`** — WAIT, UPGRADE_FIELD, SEND_RESOURCES,
  TRAIN_TROOPS, TRAIN_SETTLERS, CLAIM_VIDEO_BONUS, SEND_RAID,
  SEND_CUSTOM_RAID, FOUND_VILLAGE, ALERT, LOGIN, NOOP.
- **`IncomingAttack`** — `movement_type`, `seconds_remaining`, `troop_count`,
  `arrival_ts`, `attacker`, `label`. `is_likely_fake(troop_threshold)` =
  rinforzi/scout/pochi uomini visibili → fake; composizione ignota → prudente
  = reale. Property `is_threat`.
- **`ScoutTarget`** — target radar AI Scout: coordinate, player, alleanza,
  popolazione, distanza, `is_oasis`, `last_loot_ratio`, `losses_recorded`,
  `enemy_units`, `is_blacklisted`/`blacklist_until`, `notes`.
- **`AccountPowerAssessment`** — potenza impero: `military_power`,
  `total_troops`, `net_crop_production`, `total_hourly_production`,
  `total_resources`/`total_capacity`, `storage_saturation_pct`,
  `ready_for_expansion`, `village_summaries`.
- **`Action`** — `type` + `params` + `reason`.
- **`ActionResult`** — `success` + `message`.
- Costanti `GID_*` (EMPTY, WAREHOUSE, GRANARY, RALLY_POINT, MARKETPLACE,
  BARRACKS, STABLE, WORKSHOP, RESIDENCE, PALACE) e `STATIC_TROOP_LABELS`
  (t1–t30, tre tribù).

### 2.3 selectors.py

Solo costanti: tutti i CSS selector (liste di alternative per tollerare
T4.4/T4.5/T4.6 e i18n) e i path del gioco. Gruppi: login, stock bar, mercato,
rally/farm list, mappa/fondazione, CP, InfoBox/shop video bonus, training,
dorf/coda costruzioni, troop movement/attacchi, upgrade button, sidebar
villaggi, questmaster, `PATH_*` (dorf1, dorf2, build, karte, questmaster,
tasks, login, index), `PARAM_NEWDID`.

### 2.4 presets.py

Preset edilizi e militari completi per ruolo strategico (applicati dalla
dashboard su `VillageConfig`).

- Costanti: `ROLE_PRESETS` (code `{key|gid, target_level}` per ruolo),
  `_ROLE_ALIASES`, `TRIBE_UNIT_SLOTS` (slot astratti → tN per tribù),
  `TROOP_REPLENISHMENT_PRESETS`, `ALLOWED_UNIT_SLOTS`, `SMITHY_PRESETS`,
  `DEFAULT_SMITHY_PRESET`.
- `detect_tribe_from_keys(keys)` — deduce tribù da chiavi `tN`/`uN`.
- `detect_tribe_from_labels(labels)` — deduce tribù dai nomi italiani unità.
- `resolve_troop_slot(slot, tribe)` — slot astratto (`infantry_off`, ecc.) → `tN`.
- `resolve_troop_labels(slots, tribe)` — slot → nomi italiani (per UI allowed_units).
- `troop_preset_for_role(role)` — preset replenishment del ruolo (slot-agnostico).
- `resolve_troop_preset(preset, tribe)` — preset → `{tN: qty}` mergiabile in `troop_targets`.
- `allowed_unit_slots_for_role(role)` — slot truppe sbloccati dal ruolo.
- `smithy_preset_for_role(role)` — preset Fucina (`smithy_auto_upgrade`).
- `resolve_role(role)` — normalizza nome ruolo (alias) → chiave preset.
- `preset_for_role(role)` — coda prioritaria completa per `building_queue_plan`.
- `roles_with_preset()` — ruoli con preset applicabile.

### 2.5 building_prereqs.py

Catalogo prerequisiti T4.x per **nuove costruzioni**.

- Costanti `GID_*` di tutti gli edifici, `GID_LABELS`, `WALL_GIDS` (mura per
  tribù), `FIELD_GID_TYPES` (gid campo → ResourceType),
  `_PREREQS[gid] = ([(gid_richiesto, livello_min)], capital_only)`,
  `FOCUS_KEY_GIDS` (chiavi dashboard → gid/tupla gid).
- `gids_for_focus_key(key)` — traduce una chiave `building_focus` in gid.
- `_field_level(fields, res_type)` — livello massimo tra i campi del tipo.
- `prereqs_met(fields, gid, is_capital)` → `(ok, motivo_blocco)` — verifica
  prerequisiti (su edifici interni per gid esatto; su campi per livello
  massimo del tipo) + guardia `capital_only`.

Copertura: mulini/forni, catena militare (Caserma/Scuderia/Officina/Grandi),
commercio (Mercato/Grande Magazzino), Residenza/Palazzo, tribù-specifici
(Trappolaio, Birreria, Abbeveratoio, Acquedotto).

---

## 3. Infrastruttura browser e sensori

### 3.1 browser.py

`TravianBrowser` = lifecycle Playwright + autenticazione.

- `human_delay(min_s, max_s)` — delay quasi-gaussiano (¼ delle volte ×1.15).
- `sleep_human(min_s, max_s)` — sleep asincrono con delay umano.

**`TravianBrowser`:**

- `__aenter__/__aexit__` — context manager (start/stop).
- `start()` — avvia Playwright/Chromium (headless da settings), context con
  `storage_state` da `state/session_<id>.json` se presente, locale e timeout
  di navigazione configurati.
- `save_state()` — persiste cookie+localStorage su disco.
- `stop()` — salva stato e chiude context/browser/playwright.
- `is_alive()` — page aperta e reattiva.
- `reload()` — reload con delay (o `recover()` se morto).
- `recover()` — stop + start completi (usato dall'orchestratore).
- `url(path)` — URL assoluto da `server_url`.
- `goto(path)` — navigazione avvolta in `sleep_human`.
- `_first(selector_list, timeout)` — primo locator esistente fra alternative.
- `is_logged_in()` — marker login presente.
- `ensure_logged_in()` — **login ibrido a cascata**: sessione salvata →
  `_automated_login` (form, digitazione 60–140ms/tasto) → in modalità visibile
  `wait_for_manual_login` (polling marker fino a 300s, copre captcha/lobby);
  in headless dump debug + errore con suggerimento `--login`.
- `_automated_login()` — compila form credenziali con delay umani.
- `wait_for_manual_login(timeout)` — attesa login manuale.
- `dump_debug(tag)` — screenshot+HTML in `debug/<account>/`.

### 3.2 sensors.py

`GameObserver`/`TravianParser` = i sensori: JS Injection (`page.evaluate`) +
CSS selector con fallback multipli → `GameState` e dati di supporto dei moduli.

**Helper di parsing:**

- `parse_int(text)` — primo intero da stringhe tipo `"1 234 / 8 000"`.
- `parse_capacity(text)` — capacità dal tooltip `corrente||capacita`.
- `parse_timer(text)` — `"HH:MM:SS"`/`"MM:SS"` → secondi.
- `guess_resource_type(name)` — tipo risorsa da keyword multi-lingua.
- `_first_locator(page, selector_list)` / `_text_of(locator, selector_list)` —
  utility selettori con fallback.

**`TravianParser` (alias `GameObserver`):**

*Economia:*

- `get_village_state()` — stato villaggio in dict (API grezza).
- `extract_resources()` — stock bar via JS → risorse + capacità.
- `extract_building_queue()` — edifici/campi in costruzione + timer.
- `read_state()` → `GameState` — compone la fotografia tipizzata completa.
- `read_fields()` — slot 1–18 dal dorf1.
- `read_interior_fields()` — slot 19–40 dal dorf2 (slot_id, gid, nome, livello).
- `read_production(village_id)` — production.php → produzione oraria +
  **`crop_balance` netto** (chiave del Grain Safety Net).
- `read_build_queue()` — coda con timer in secondi e `slot_id`.
- `_read_resources()` / `_read_village_name()` — letture interne.
- `has_video_bonus_available()` — disponibilità bonus video +15% (InfoBox).
- `read_culture_points(residence_slot, village_id)` — `(CP attuali, CP richiesti)`
  dalla pagina Residenza/Palazzo.

*Villaggi:*

- `get_all_villages()` — sidebar → lista `Village` (newdid, coordinate,
  attivo); fallback `_dom_extract_villages()`.
- `extract_village_name()` — nome villaggio attivo.
- `_current_newdid()` — newdid dall'URL corrente.
- `read_village_coords(village_id)` — coordinate via mappa (fallback per
  server senza coord in sidebar).

*Militare:*

- `check_incoming_attacks()` — booleano movimenti ostili.
- `get_incoming_attacks(rally_slot, village_id)` — panoramica Punto di
  Raduno → lista `IncomingAttack` classificata.
- `_read_incoming_attacks()` — conteggio attacchi per `GameState`.
- `read_troops_home(rally_slot, village_id)` — icone u1–u30 → truppe a casa.
- `read_training_queue(trainer_slot, village_id)` — unità in addestramento.
- `read_academy_state(academy_slot, village_id)` — pagina Accademia
  (gid 22): `research_active` (coda ricerche in corso), `units` per
  chiave `tN` con status `researched|researchable|blocked|in_progress`,
  label e costi; URL routing-aware via `_building_url_path` +
  `server_profile`. Normalizzazione pura: `normalize_academy_state`.
- `read_trainable_units(trainer_slot, village_id)` — chiavi `tN`
  addestrabili (input `tN` della form) = unità già sbloccate; tie-breaker
  per unità assenti dal DOM Accademia.
- `read_farm_lists(rally_slot, village_id)` — farm list del rally point.
- `read_combat_reports(limit)` — berichte.php: coordinate, loot_ratio,
  perdite, difensori (condiviso da AI Scout e Replenishment).

*Mappa:*

- `scan_map_area(center_x, center_y, radius)` — karte.php → tile
  villaggi/oasi nel raggio.
- `read_map_tile(x, y)` — position_details.php → occupato, # campi grano
  (9c/15c), link "fonda" (usa `_eval_map_tile`).

---

## 4. Motore decisionale

### 4.1 builder.py

**Smart Building Planner**: produce `UpgradeIntent` ordinati secondo la
catena di priorità (vedi `select_upgrade`). Logiche di base condivise col
brain: il brain decide *la categoria*, builder produce *i candidati*.

**Dataclass `UpgradeIntent`** — `kind`, `slot_id`, `name`, `gid`,
`target_level`, `is_resource_field`, `reason`, `candidates` (slot alternativi),
`construct` (mappa slot→gid nuove costruzioni), `to_dict()`.

**Primitive di stato:**

- `is_capital(state)` — Palazzo (gid 26) nel dorf2 → capitale.
- `field_max_level(state, f)` — max campo (10 normale / 21 capitale) o edificio (20).
- `queued_slot_ids(state)` — slot già occupati da lavori in coda.
- `upgradeable_fields(state, fields)` — non in coda e sotto livello max.
- `empty_slots(state)` — aree vuote dorf2 (gid 0) libere.
- `storage_needed(state, threshold=0.9)` — gid 10/11 se magazzino/granaio ≥90%.
- `crop_threshold(state, settings)` — soglia margine grano: per-villaggio > globale.
- `net_crop(state)` — `crop_balance` da production.php.
- `crop_safety_needed(state, settings)` — margine netto sotto soglia → True.

**Generatori di intenti (catena di priorità in `select_upgrade`):**

1. `bootstrap_intents(state, settings)` — **villaggio `is_bootstrap`**: sequenza
   startup (campi bassi → Municipio `BOOTSTRAP_MAIN_LEVEL` → storage
   `BOOTSTRAP_STORAGE_LEVEL`), attiva finché `bootstrap_complete()` è False.
2. `reclaim_intents(state, settings)` — **villaggio `is_reclaim_source`**:
   Accademia `RECLAIM_ACADEMY_LEVEL` + Residenza `RECLAIM_RESIDENCE_LEVEL`
   per sbloccare nobili/coloni del treno di riconquista.
3. Storage — storage esistente o `new_building_intents` su area vuota.
4. `grain_safety_intents(state)` — campi grano dal livello più basso, poi
   Mulino/Forno (esistenti o nuovi se `prereqs_met`).
5. `focus_intents(state, settings)` — `building_queue_plan`/`building_focus`
   da dashboard; step via `_focus_steps` (chiave→gid con `gids_for_focus_key`,
   match esistenti + nuova costruzione, step al target saltati); `_pick_group_gid`
   sceglie il gid nel gruppo alternative (es. mura per tribù rilevata da
   `_detect_tribe`).
6. `role_intents(state, settings)` — infrastrutture `VillageRole` bilanciate
   per livello (`_ROLE_INTERIORS`); capitale aggiunge profilo croplog.
7. `economy_intents(state, settings)` — campo più debole della risorsa più
   scarsa (`Resources.scarcest()`); croplog → sempre grano.
8. `local_spend_intents(state)` — interni dal livello più alto (costo
   maggiore) → usati da resource drain e emergency overflow.

**Supporto nuove costruzioni:**

- `new_building_intents(state, gids, extra_occupied)` — pseudo-`Field` su
  aree vuote, solo se `prereqs_met` ok + check `capital_only`.
- `construct_gids(state, fields)` — `slot_id → gid` (esecutore costruisce con
  `dorf2.php?a=<gid>&id=<slot>`).

**Piano e persistenza:**

- `select_upgrade(state, settings)` → `UpgradeIntent | None` — la catena
  completa di priorità.
- `plan_upgrades(state, settings, depth=4)` — simula i prossimi N upgrade su
  copia dello stato → preview dashboard.
- `save_build_plan(account_id, village_name, plan)` / `load_build_plans(account_id)`
  — `state/build_plan_<acc>.json`.

### 4.2 brain.py

`RuleBasedBrain` = **albero di decisione**: regole in `DEFAULT_RULES`
valutate in ordine, la prima che produce un'Action vince.

**Interfaccia:**

- `DecisionMaker(Protocol)` — `decide(state) -> Action`.

**Regole (in ordine di priorità):**

1. `rule_alert_under_attack` — attacchi in arrivo → `ALERT` (la difesa attiva
   è di `defense.py`).
2. `rule_upgrade_storage` — storage ≥90% (`_storage_needed`) → UPGRADE_FIELD
   sullo storage (keyword multi-lingua `_STORAGE_KEYWORDS` o gid), incluse
   nuove costruzioni.
3. `rule_grain_safety` — margine grano sotto soglia → solo grano/Mulino/Forno
   (`builder.grain_safety_intents`). **Precedenza assoluta**: le altre regole
   di upgrade si auto-annullano quando `crop_safety_needed` è True.
4. `rule_emergency_overflow` — storage al 100% (`_storage_full`) e upgrade
   non avviabile, in cascata: (a) spesa locale sull'upgrade più costoso
   (`local_spend_intents`), (b) addestramento Caserma/Scuderia/Officina con
   `priority_units` (`_train_affordable`), (c) `SEND_RESOURCES` dell'eccedenza
   (`_overflow_surplus`, ~15% capacità) al peer con più spazio
   (`_best_peer_for_surplus` su `peer_villages`, mercato richiesto), (d) ALERT.
5. `rule_building_focus` — `building_focus`/queue plan da dashboard.
6. `rule_role_buildings` — infrastrutture di ruolo bilanciate.
7. `rule_upgrade_weakest_field` — campo più debole della risorsa scarsa.
8. `rule_wait` — fallback: `WAIT` con ETA prima costruzione.

**Helper interni:**

- `_is_capital` / `_field_max_level` / `_queued_slot_ids` / `_upgradeable_fields`
  — mirror delle primitive builder (logica di validazione preventiva).
- `_queue_free(state, settings, resource_field)` — se `allow_parallel_build`
  limiti separati dorf1 (`max_resource_queue`) / dorf2 (`max_interior_queue`)
  sotto `max_build_queue`; altrimenti coda unica.
- `_find_interior_by_gid` / `_find_all_interiors_by_gid` — lookup edifici per gid.

**Classi:**

- `RuleBasedBrain(settings, rules)` — `decide(state)` itera le regole,
  ritorna la prima Action non-None.
- `LLMBrain(settings, fallback)` — stub: `serialize_state` → prompt → parse;
  ricade su `RuleBasedBrain` finché non c'è un client LLM.

---

## 5. Esecuzione

### 5.1 actions.py

`ActionExecutor` — traduce un'`Action` in click/navigazioni. `execute()`
dispatch per `ActionType`; prima `_ensure_village(village_id)` forza il
contesto villaggio corretto (`newdid` nell'URL, verificato post-navigazione).
Costanti JS inline (`_JS_*`) per mercato, video ad e dialog.

- `execute(action, village_id)` → `ActionResult` — dispatcher centrale;
  WAIT/NOOP/ALERT/LOGIN gestiti inline.
- `_ensure_village(village_id)` — naviga al newdid se diverso dall'attivo.
- `_upgrade_field(action, village_id)` — `build.php?id=<slot>` con newdid
  preservato; nuove costruzioni via `dorf2.php?a=<gid>&id=<slot>`
  (`params.construct`); itera `candidates` finché uno è cliccabile.
- `_send_resources(action, village_id)` — mercato (slot esplicito o gid 17)
  → tab "Invia risorse" (`_select_send_resources_tab`) → quantità cappate
  alla portata mercanti (`_calculate_capped_resources`) → destinazione nome
  (autocomplete `_pick_target_suggestion`) o coordinate
  (`_resolve_village_coords_from_sidebar`) → conferma.
- `_send_raid(action, village_id)` — rally point `tt=99` → farm list
  configurata (o prima disponibile) → invio.
- `_send_custom_raid(action, village_id)` — rally point `tt=2` → coordinate
  libere + truppe per target multipli (AI Scout, Difesa; `move_type="2"` =
  rinforzi).
- `_found_village(action, village_id)` — karte.php → tile → click "Fonda".
- `_train_settlers(action, village_id)` — Residenza/Palazzo → coloni fino a 3.
- `_upgrade_smithy_units(action, village_id)` — accoda potenziamento unità
  in Fucina (preset `smithy_auto_upgrade`).
- `_research_unit(action, village_id)` — apre lo slot Accademia (gid 22)
  con URL routing-aware (`_building_path_for`: `building?id=` sui server
  moderni, `build.php?id=` altrove), clicca il pulsante "Ricerca" della
  riga unità (`unit` = `tN`/`uN` o `label`) e verifica la comparsa del
  timer di ricerca attiva.
- `_train_troops(action, village_id)` — legge stock + unità addestrabili
  (`_extract_training_data`), lotto massimo sostenibile
  (`_pick_training_batch`, res_margin 95%/crop 90%), privilegia
  `priority_units`/`allowed_units`; modalità `unit_quantities`
  (replenishment) → `_train_troops_targeted` (margini 85%/70%); fallback
  `_train_troops_legacy` ("max link" o 1 unità).
- `_claim_video_bonus(action)` — shop/InfoBox → video ad per le 4 risorse →
  `_wait_for_video_ad_completion` (attesa popup, `_video_ended_anywhere`
  incl. iframe) → `_close_ad_overlay`.
- `claim_village_tasks(village_id, village_name, claim_ctx=None)` — questmaster → badge
  (`_tasks_pending_badge`) → riscossione premi on-demand: `_scan_task_rewards`
  legge contenuto/stato di ogni premio senza cliccare, `task_reward_claimable`
  (funzione pura) incassa solo se non c'è overflow (≤92% capienza), nessun
  attacco in arrivo e il premio sblocca un obiettivo pendente; premi
  non-risorsa (XP/PC) sempre liberi. `claim_ctx=None` = legacy (tutto).
  Guardia `_storage_has_headroom` (≥30% spazio libero) su task generali.
- Utility: `_click_first`, `_active_village_name`, `_dump_market_html`.

---

## 6. Orchestrazione

### 6.1 orchestrator.py

Cuore 24/7: istanzia i manager per-account (`_farming_managers`,
`_expansion_managers`, `_scout_brains`, `_hero_managers`,
`_defense_managers`, `_replenish_managers`, `_war_stores`), li collega e li
esegue ai punti giusti del ciclo.

**Funzioni principali:**

- `run_all_accounts(accounts, brain_type, once, dump)` — un task asyncio per
  account via `_supervised_account_loop` (crash isolati), `asyncio.gather`.
- `run_single_account_loop(settings, brain_type, once, dump)` — loop infinito:
  **hot-reload** accounts.json → file-flag `stop_<id>.flag` (uscita pulita) /
  `pause_<id>.flag` (sleep 10s) → `browser.recover()` se morto →
  `monitor_all_villages` → intervallo casuale `loop_interval_min–max`.
  Errori → `_calculate_backoff` (10s ×2^n, cap 300s, jitter ±10%) + reload/
  recover: **non muore mai**.
- `monitor_all_villages(...)` — il giro vero:
  1. `get_all_villages` → backfill coordinate → `_persist_village_coords`
     su accounts.json; `_register_founded_villages` (auto-discovery
     post-fondazione: newdid → VillageConfig bootstrap).
  2. `_reconcile_village_losses` — confronto sidebar vs storico `WarStore`:
     villaggio sparito → `STATUS_STOLEN`; villaggio con target reclaim
     pianificato → `_onboard_reclaimed_village` (VillageConfig + bootstrap
     d'emergenza); `_dispatch_reclaim_waves` spedisce le wave con
     `depart_ts` scaduto.
  3. Bonus video +15% ogni ~7.8h (`_last_video_bonus_claim`).
  4. Per villaggio: navigazione newdid → `claim_village_tasks` → **Sense**
     (`read_state` + campi + interni + `read_production`) → `peer_villages`
     → **Defense** (`defense_mgr.handle_village`, prima del brain) →
     **Decide** (`brain.decide`) → arricchimento coordinate SEND_RESOURCES →
     **Act** (`executor.execute`) + `_append_history` JSONL +
     `plan_upgrades`/`save_build_plan` → moduli autonomi in sequenza:
     **farming → AI Scout → replenishment → feeder espansione →
     smithy auto-upgrade (cooldown `SMITHY_RETRY_SECONDS`)** → pausa umana.
  5. Fine giro: `expansion_mgr.step` (fondazione), `hero_mgr.run`,
     `assess_account_power` → scout state → War Room.
- `_persist_village_coords`, `_register_founded_villages`,
  `_complete_bootstrap` — sincronizzazione stato ↔ accounts.json.
- `_append_history(account_id, state, action)` — storico decisionale
  `state/history_<acc>.jsonl`.

---

## 7. Moduli autonomi

Ogni manager: `__post_init__` → path `state/<modulo>_<acc>.json`, `_load()` /
`_save()` con tolleranza errori (best-effort, mai crashare il ciclo).

### 7.1 farming.py

Raid periodici dalle farm list del Punto di Raduno.

- `find_rally_point(interior)` — campo gid 16 negli interni.
- **`FarmingManager`** — `next_due` per villaggio su `state/farming_<acc>.json`.
  - `next_interval_seconds(settings)` — `uniform(interval_min, interval_max)`:
    mai due cicli uguali (anti-ban).
  - `is_due(village_key)` / `schedule_next(village_key, settings)` —
    scheduling.
  - `_schedule_retry` — retry breve (`RETRY_MIN/MAX`) se senza truppe;
    `NO_LIST_RETRY_*` se senza farm list.
  - `_has_raiders(troops, unit_pref)` — match raider via `_LIGHT_RAIDERS` +
    `_UNIT_KEY_ALIASES` (nome ITA → icone uN).
  - `run_for_village(browser, observer, executor, settings, village,
    rally_slot, prefix, farm_store)` — se `farming.enabled` + `farm_enabled`
    + rally point + `is_due` → `read_troops_home` → **Custom Farm Targets
    prioritari**: `_discover_targets` (scansione mappa con cooldown
    `next_scan` 1.5–2.5h se `farming.auto_scan`) → `_send_approved_raids`
    (target `approved` ordinati per vicinanza, cap `targets_per_wave`,
    truppe `farm_unit_qty` o auto-split dei raider → `SEND_CUSTOM_RAID`
    `move_type=4`); se nessun approved → fallback `SEND_RAID` farm list
    nativa (raid vuoto fallisce senza danni).
  - `_raider_key(troops, unit_pref)` — risolve `farm_unit` in chiave `tN`
    via `unit_label_to_key`; auto = prima unita' leggera a casa.
  - `_schedule_scan(village_key)` — pianifica la prossima scansione di
    scoperta (persistita in `next_scan`).

### 7.1a farm_store.py

Custom Farm Target Manager (alternativa al Gold Club), human-in-the-loop:
scanner → `pending` → approvazione dashboard → raid solo su `approved` →
report con perdite/difensori → `blacklisted`. Stato
`state/farm_targets_<acc>.json`.

- **`FarmTarget`** — `id` (`f_minus12_45`), `x`/`y`, `name`, `is_oasis`,
  `population`, `estimated_loot` (pop×60, base 120 oasi),
  `last_loot_ratio`, `losses_recorded`, `distance`, `nearest_village`,
  `status`, `manual`, `updated_ts`; `from_dict` classmethod.
- **`FarmTargetStore`** (una istanza per account):
  - `list_targets(status)` / `find` / `status_at` / `pending_count` — query.
  - `get_approved_targets(near_x, near_y)` — solo approved, riordinati
    per distanza dal punto dato (villaggio attivo).
  - `upsert` / `set_status` / `set_status_at` / `approve` / `reject` /
    `blacklist` — write; `upsert` non retrocede stati decisi.
  - `add_manual_target(x, y, name, is_oasis, population)` — inserimento
    manuale → pending in testa (niente duplicati per coordinate).
  - `register_scanned(scanned_tiles, origin_x, origin_y, nearest_village,
    skip_names)` — **discovery**: tile razziabili (oasi + villaggi con
    pop>0) → pending; chiamato dalla scansione AI Scout e da farming.
  - `process_combat_reports(reports)` — aggiorna loot ratio/perdite;
    perdite o difensori >0 → `blacklisted` (sblocco solo manuale).
- `farm_target_store(account_id)` — istanza condivisa cached (agente +
  dashboard).

### 7.1b radar_store.py

Radar Movement Store — vettori movimento live del **RADAR SETTORE** del
Command Center (mercanti, avventure eroe, raid/attacchi in uscita e
attacchi in arrivo). Persistenza KV SQLite (`radar_<acc>.json`).

- `CAT_*` / `VALID_CATEGORIES` — categorie `merchant`, `hero_adventure`,
  `outgoing_raid`, `outgoing_attack`, `incoming_attack`; direzioni
  `outbound|inbound|return`.
- `make_move_id(...)` — id deterministico con bucket ETA 60s (dedup
  tra azione e sync passivo).
- `normalize_movement(village_key, data)` — valida e completa lo schema
  (`from_*`/`to_*` possono restare `None` → edge-projection frontend).
- **`RadarStore`** — `register`, `register_many`, `sync_categories`
  (sync passivo dalle pagine rilette: righe non più osservate =
  completate), `movements_for`, `prune`; thread-safe, rilettura storage
  a ogni accesso.
- `radar_store(account_id)` — istanza condivisa per account.
- `merge_village_movements(...)` — store + live non persistiti (dedup).

Integrazione zero-overhead: `get_troop_movements` (rally overview)
sincronizza in/out; `_send_resources` registra mercanti outbound +
ritorno; `_send_raid`/`_send_custom_raid` registrano i vettori
letti da `tr.out` (ETA DOM o stima `travel_seconds`);
`_send_to_adventure` registra avventura + rientro; `telemetry`
espone `villages[vid].radar_movements` in `/api/dashboard-state`.

### 7.2 expansion.py

Fondazione autonoma di nuovi villaggi (`ExpansionManager`, stato
`state/expansion_<acc>.json`); `step` una volta per ciclo account.

- `cropper_store(account_id)` — `CropperStore` condiviso per account
  (dashboard + agente).
- `find_residence(interior, min_level)` / `find_market(interior)` — lookup edifici.
- `feeder_surplus(state, keep_ratio=0.55)` — surplus spedibile oltre la quota
  tenuta, cappato `FEED_CAP_PER_RESOURCE`.
- `count_settlers(troops)` / `get_available_settlers(observer, residence_slot, village_id)`
  — coloni da icone u10/u20/u30.
- `load_expansion_overrides(account_id)` / `effective_expansion_config(cfg, account_id)`
  — merge config con override dashboard `state/expansion_config.json`.

**`ExpansionManager.step(...)`** — sequenza gate:

1. Cropper scan se `cropper_scan` + cooldown (`next_map_scan`, 3h; retry
   10min se 0 tile) → `MapScanner.scan`.
2. `_pick_founder` — `founder_village` o auto (Residenza/Palazzo ≥10, poi ≥1);
   `expansion_slot` forza lo slot.
3. CP via `read_culture_points` → insufficienti → esci (e `feed_target` reset).
4. Coloni <3 → `TRAIN_SETTLERS` + `feed_target = founder` attiva la **rete
   feeder**: gli altri villaggi con mercato inviano `feeder_surplus`.
5. `_pick_target` — prima cropper `approved` (priorità sui manuali), poi
   `target_slots`; `read_map_tile` verifica libero + `crop_fields ≥ crop_min`.
6. `FOUND_VILLAGE` → `settling` → `settled` + `founded_tiles`; fallimento →
   `approved` + cooldown `FOUND_RETRY_SECONDS` (15min).

### 7.3 cropper.py

Cropper Finder **human-in-the-loop**: il bot propone, l'uomo dispone.

- `make_candidate_id(crop_fields, x, y)` — id stabile `c_15_minus15_72`.
- **`CropperCandidate`** — `id`, `x`/`y`, `type` (9c/15c),
  `bonus_crop_percent`, `distance_hours` (~5 campi/h coloni),
  `nearest_village`, `oases`, `status`, `manual`, `updated_ts`;
  `from_dict` classmethod.
- **`CropperStore`** — CRUD su `state/cropper_candidates.json` condiviso:
  `list_candidates`, `find`, `status_at`, `approved_targets`, `upsert`
  (non sovrascrive stati decisi), `set_status`/`set_status_at`,
  `approve`/`reject`, `add_manual_candidate`.
- **`MapScanner.scan(observer, account_id, villages, radius, max_detail_checks)`**
  — per ogni villaggio con coordinate: `_scan_grid` (`scan_map_area`) →
  tile liberi non-oasi → `read_map_tile` per 9c/15c
  (`CROPPER_FIELD_COUNTS`) → `_build_candidate` (distanza, villaggio più
  vicino, oasi grano entro `OASIS_INFLUENCE_RADIUS` via `_parse_oasis_bonus`)
  → status `pending`.
- Guardia incrociata: `ExpansionManager._pick_target` fonda **solo**
  `approved`; `settling`/`settled` aggiornati dal manager.

### 7.4 ai_scout_brain.py

Intelligence di guerra + raid dinamici (`AIScoutBrain`, stato
`state/scout_<acc>.json`).

- `normalize_unit_key(key)` — `u1`/`t1`/`unit1` → `t1`.
- `calculate_distance(x1,y1,x2,y2)` — distanza euclidea.
- `assess_account_power(settings, villages, states_by_village, troops_by_village)`
  — **Global Power Assessment**: potenza militare (`TROOP_STATS` att + media
  difese), upkeep totale, net crop impero, saturazione magazzini,
  `ready_for_expansion`, `village_summaries` → War Room.
- `_extract_json_from_text(text)` — JSON puro anche dentro markdown fences.
- `gemini_evaluate_farming_targets(account_status, nearby_targets, api_key,
  model_name)` — prompt a Gemini (stato account+target → piano raid JSON);
  errore → fallback euristico.
- `heuristic_evaluate_farming_targets(account_status, nearby_targets)` —
  score deterministico = loot × pop / dist^1.2, truppe proporzionali alla
  popolazione, max `max_targets_per_wave`.

**`AIScoutBrain`:**

- `update_targets_from_map(scanned_tiles, origin_x, origin_y)` — indicizza
  tile nel Target DB (`ScoutTarget`).
- `process_combat_reports(reports, blacklist_hours)` — aggiorna
  `last_loot_ratio`, accumula perdite, **auto-blacklist** (perdite o
  difensori >0 → blacklist N ore; esito pulito dopo scadenza → riabilitato).
- `get_candidate_targets(origin_x, origin_y, max_distance, min_loot_ratio)` —
  filtra blacklist scadute, distanza ≤2×scan_radius, loot ≥ soglia, ordinati
  per distanza.
- `decide_raid_plan(origin_village, troops_home, settings)` — Gemini (o
  euristico) → piano `{targets, troops}` → `SEND_CUSTOM_RAID`.

### 7.5 replenishment.py

Smart Troop Replenishment (`TroopReplenishmentManager`, stato
`state/replenish_<acc>.json`).

- `settler_reserve()` — riserva anti-saturazione = 3× costo colono.
- `_troop_costs()` — costi unitari da `actions.STATIC_TROOP_COSTS`.
- `clamp_plan_to_budget(plan, budget)` — riduce il piano al budget sostenibile.
- `normalize_unit_key(key)` / `unit_label_to_key(label)` — chiavi `u5`/`T5`/
  `unit5`/nomi ITA → `tN` canonico (su `STATIC_TROOP_LABELS`).

**`TroopReplenishmentManager`:**

- `process_combat_reports(reports, auto_block, fail_streak_threshold,
  raid_units)` — **anti-suicidio**: report con perdite su target e bottino
  ≤20% → `blocked_targets` + `fail_streaks`; a soglia → `frozen_units`
  (unità offensive congelate); esito positivo → reset streak e sblocco.
- `update_siege_state(village_name, incoming_attacks, siege_waves)` —
  **sostenibilità difesa**: ≥`siege_waves` attacchi in arrivo →
  `frozen_villages` (niente rimpiazzo né rinforzi); assedio rientrato → sblocco.
- `record_defense_report(village_name, losses, village_collapsing,
  defensive_units)` — registra perdite delle truppe in rinforzo.
- `is_unit_frozen` / `is_village_frozen` / `is_target_blocked` — query stato.
- `compute_deficit(troop_targets, troops_home, troops_queued)` — formula
  `da_addestrare = target − (casa + coda)`.
- `plan_training(deficit, state, settings, village_name, reserve_settlers)` —
  filtra il deficit: villaggio congelato → stop; `skip_while_building` → cede
  a code attive; **guardia grano** → niente training sotto
  `crop_reserve_pct` del granaio o con produzione netta ≤0 e scorte <2×
  riserva; opzionale riserva coloni (`reserve_settlers`). Piano →
  `TRAIN_TROOPS` con `unit_quantities` mirate.
- `_log_event(kind, detail)` — eventi per dashboard (`last_events`).

### 7.6 defense.py

Difesa reattiva (`DefenseManager`, stato `state/defense_<acc>.json`:
dodge/recall sopravvivono ai riavvii). Eseguito **prima** del brain per
ogni villaggio.

- **`PendingDodge`** — fuga programmata: `home_village_id/name`,
  `attack_arrival_ts`, `dodge_at_ts`, `troops` (lette al monitor), destinazione
  (`dest_village_id`/`x`/`y`), `sent`.
- **`PendingRecall`** — rientro programmato dal villaggio ospite.

**`DefenseManager.handle_village(...)`** — pipeline:

1. **Fase 0** — `_process_recalls` (rientri scaduti → rispedisce a casa) e
   dodges dovuti (`_execute_dodge` → `SEND_CUSTOM_RAID` `move_type=2`
   rinforzo verso destinazione → registra `PendingRecall`).
2. **Threat assessment** — `get_incoming_attacks` → filtra `is_threat` e non
   `is_likely_fake` → attacco reale più vicino.
3. **Monitoraggio** (> `panic_threshold` 180s) — `_schedule_dodge` registra
   `PendingDodge` (`dodge_at = arrival − dodge_margin`); destinazione da
   `_resolve_dodge_destination` (`dodge_target` "x|y"/nome o villaggio
   proprio più vicino con coordinate).
4. **Emergenza** (≤ panic) — `_lock_village` + **Resource Drain**
   (`_drain_resources`): (a) `_emergency_train` addestramento massivo
   (98% res / 90% crop) nei trainer `_TRAINER_GIDS`; (b) `_emergency_upgrade`
   upgrade più costoso accodabile nel dorf2 (JS cost-sum); (c)
   `_drain_via_market` surplus residuo al villaggio sicuro (`drain_via_market`,
   keep `_DRAIN_KEEP`=200/risorsa).
- `is_emergency_locked(village_id)` / `_lock_village(village_id, until_ts)` —
  lock emergenza (consultato dagli altri moduli).

### 7.7 hero.py

Eroe autonomo (`HeroManager`, snapshot `state/hero_<acc>.json`), una volta
per ciclo account con cooldown `CHECK_MIN/MAX_SECONDS` (4–7 min).

- `run(browser, settings, prefix)` — gate cooldown → `hero.php` → `_sense`
  → auto-cura → attributi → avventura; best-effort totale.
- `_sense(page)` — JS `_JS_HERO_STATE`: salute, stato
  home/moving/adventure/dead, avventure disponibili, punti liberi, livello
  (livello=0 → `_dump_html` per calibrazione selettori).
- `_sense_inventory_resources(browser, prefix)` — consumabili-risorsa nel
  baule (tab inventario).
- `_goto_hero_tab(browser, candidates)` — prova i path tab (nomi/numeri per
  server).
- `_apply_healing(browser, prefix)` — inventario → unguento/pozione → "Usa"
  se HP < `heal_threshold`.
- `_attribute_focus(settings)` — attributo target: config esplicita o
  profilo (hammer→strength, altrimenti resources) via `_ATTRIBUTE_LABELS`.
- `_assign_attribute_points(browser, settings, points, prefix)` — assegna i
  punti liberi all'attributo scelto.
- `_send_to_adventure(browser, prefix)` — tab avventure → prima disponibile
  → conferma (se `auto_adventures` + a casa + HP ≥ `min_health_to_adventure`).
- `emergency_unpack(browser, village_id, prefix)` — **pacchi risorsa**:
  consuma pack dal baule per coprire un deficit (max `_UNPACK_MAX_USES`),
  scelti da `_pick_resource_pack` (copertura massima del deficit residuo);
  usato dalla difesa/drain quando `hero.emergency_unpack` attivo.
- `_use_inventory_item(page, idx)` — click item + conferma "Usa".

### 7.8 war.py

**Reclaim & Chiefing Engine** — gestione villaggi sottratti e treni di
riconquista (stato `state/war_targets_<acc>.json`).

- Costanti: stati `STATUS_STOLEN/RECLAIMING/RECLAIMED`, stati wave
  `WAVE_PLANNED/SENT/FAILED`, `MAP_SIZE` (mappa toroidale −400..400),
  `IMPACT_GAP_SECONDS`, `DEPART_BUFFER_SECONDS`, `MAX_WAVES_PER_DISPATCH`,
  `UNIT_SPEED` (campi/ora per unità), `CHIEF_UNITS` (nobili/senatori/capi per
  tribù), `LOYALTY_DROP_PER_CHIEF`.
- `chief_waves_needed(tribe, loyalty)` — colpi nobile stimati per azzerare la
  lealtà residua.
- `_unit_key(raw)` — `u9`/`T9` → `t9`.
- `map_distance(x1,y1,x2,y2)` — distanza euclidea su **mappa toroidale**
  (wrap-around ai bordi).
- `wave_speed(troops)` — velocità = unità più lenta presente.
- `travel_seconds(troops, sx, sy, tx, ty)` — tempo di viaggio in secondi (x1).
- `plan_chiefing_train(tx, ty, waves_spec, impact_ts, gap_s, now)` —
  costruisce il **treno di riconquista**: wave con partenze sincronizzate
  all'indietro da `impact_ts` a intervalli `gap_s`, ciascuna con `depart_ts`
  calcolato da `travel_seconds`.
- **`WarStore`** — persistenza target:
  - `target_at(x, y)` / `list_targets(status)` — query.
  - `due_waves(source_id, now)` — coppie `(target, wave)` pianificate e
    scadute per villaggio origine.
  - `add_stolen(x, y, name, village_id, old_role, enemy_info)` — registra un
    villaggio sottratto (None se coordinate già note).
  - `mark_status(x, y, status)` — transizione di stato.
  - `plan_waves(x, y, waves, source_id)` — aggancia il treno pianificato al
    target → stato `RECLAIMING`.

Integrazione: l'orchestratore istanzia `WarStore` per account
(`_war_stores`), `_reconcile_village_losses` confronta sidebar vs storico →
`add_stolen`/`mark_status`/`_onboard_reclaimed_village`,
`_dispatch_reclaim_waves` spedisce le wave scadute come `SEND_CUSTOM_RAID`
dal villaggio origine.

---

## Mappa file di stato

Tutto in `state/`: `accounts.json`(+`.bak`), `session_<acc>.json`,
`agent_<acc>.pid`, `stop_/pause_<acc>.flag`, `history_<acc>.jsonl`,
`build_plan_<acc>.json`, `farming_<acc>.json`, `expansion_<acc>.json`,
`expansion_config.json`, `cropper_candidates.json`, `scout_<acc>.json`,
`defense_<acc>.json`, `hero_<acc>.json`, `replenish_<acc>.json`,
`war_targets_<acc>.json`, `radar_<acc>.json`, `agent.log`.
Debug: `debug/<account>/`.
