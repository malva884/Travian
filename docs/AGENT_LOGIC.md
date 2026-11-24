# Travian Agent — Logica, Priorità e Code

Documento di riferimento per capire **come l'agente decide e agisce** a ogni
ciclo, per ogni modulo. Basato sul codice in `travian_agent/`.

---

## 1. Architettura generale

- **Un worker per account** (`orchestrator.run_account`): loop infinito che
  apre il browser, esegue un *ciclo* su tutti i villaggi, scrive la telemetria
  e attende un intervallo "umano" casuale.
- **Config fonte di verità**: `state/accounts.json` — dataclass `Settings`
  (`config.py`). A inizio di ogni ciclo c'è un **hot-reload**: ruoli,
  `building_queue_plan`, espansione, farming, scout, hero, defense,
  replenishment, `builder_enabled`, `auto_video_bonus`,
  `min_net_crop_threshold`, intervalli loop vengono riletti dal file
  **senza riavviare l'agente** (`orchestrator.py:3219-3238`).
- **Telemetria**: a fine ciclo `telemetry.export_dashboard_state` scrive
  `state/dashboard_state_<account>.json` — è l'unica fonte della dashboard
  (badge ruoli, freeze, risorse, code). F5 rilegge l'ultimo file, non lo
  stato live.
- **Stealth / anti-detection** (`stealth.py`):
  - intervallo ciclo random `loop_interval_min..max` secondi;
  - pause 2.5–5s tra un villaggio e l'altro;
  - **pause modulo**: un modulo che fallisce/salta (SKIPPED/BLOCKED) non
    riprova al ciclo dopo ma dopo 15–30 min (`module_pause_minutes/max`);
  - **night sleep**: pausa totale di 4–6h a partire da
    `night_sleep_start_hour` (randomizzata per giorno);
  - **session conflict**: se il login rileva un'altra sessione attiva,
    attesa di `session_conflict_pause_minutes` (default 25) — mai due
    sessioni contemporanee.
- **Navigazione umana** (`browser.py`): `browser.goto` preferisce sempre
  il click DOM (`navigate_via_dom`: sidebar per `newdid`, navbar per
  dorf1/dorf2, slot della mappa per `build.php?id=N`) al GET diretto —
  fallback `page.goto` solo se nessun elemento è cliccabile. I click
  usano `human_click`: cursore con traiettoria spezzata e punto casuale
  *interno* all'elemento (mai il centro). Attese: cambio villaggio
  `uniform(1.4, 3.8)s`, scheda edificio `uniform(1.0, 2.5)s`.
- **Resource-ETA Sleep**: se un `UPGRADE_FIELD` fallisce per deficit
  risorse, l'esecutore riporta il costo del contratto in
  `ActionResult.data["cost"]`; l'orchestrator calcola
  `ETA = max(deficit_r / prod_r)` e mette il modulo `builder` in pausa
  per `ETA * uniform(0.95, 1.12)` (cap 45–60 min, jitter ±120s) invece
  dei 15–30 min standard — niente polling a vuoto.
- **Jitter soglie per ciclo**: `settings.cycle_jitter` (transient, mai
  persistito) è campionato a ogni ciclo via `sample_cycle_thresholds()`:
  `storage ~ U(0.82, 0.93)` letta da `rule_upgrade_storage`,
  `hero_heal ~ U(0.45, 0.62)` letta dal modulo eroe al posto della
  percentuale fissa configurata.

---

## 2. Sequenza del ciclo (per account)

`orchestrator._cycle` (o equivalente) esegue, in ordine:

| Step | Cosa fa |
|------|---------|
| **0** | Hot-reload config da `accounts.json` |
| **0.5** | Esecuzione comandi manuali dalla dashboard (`telemetry.drain_commands`, audit `MANUAL_*`) |
| **1** | Naviga su dorf1, legge la lista villaggi dalla sidebar, applica i ruoli da config |
| **1.05** | Coordinate mancanti: fallback via mappa, persistite su `accounts.json` (serve alla scansione cropper) |
| **1.06** | **New Village Bootstrap**: un newdid nuovo su tile "settled" ottiene VillageConfig automatica (croplog 9c/15c, feeder standard) + `is_bootstrap` |
| **1.07** | **Reclaim & Chiefing**: storico vs sidebar → `STOLEN_VILLAGE` (perso) / `RECLAIMING` → `RECLAIMED` (re-onboarding + bootstrap d'emergenza) |
| **1.1** | Bonus +15% risorse da video ad (`auto_video_bonus`) |
| **2–4** | Ciclo su **ogni villaggio** (vedi §3) |
| **4.5** | Modulo **Eroe**: avventure, unguenti, inventario (`hero_mgr.run`) |
| **5** | Telemetria: scrive `dashboard_state_<acc>.json` |
| **6** | Attesa umana; interrompibile da `force_tick_<acc>.flag` (pulsante "Forza Ciclo") → poi night sleep |

---

## 3. Sequenza per villaggio (dentro il ciclo)

| Step | Modulo | Cosa fa |
|------|--------|---------|
| 2.5 | task/premi | Riscossione **on-demand** dei premi localizzati (riserva protetta, vedi §6.1) |
| 3 | sensors | `read_state` + `read_fields` + `read_build_queue` + attacchi in arrivo |
| 3.5 | **defense** | Reazione agli attacchi: dodge truppe, schedulazione — *priorità su tutto il resto* |
| 3.52 | radar | Movimenti truppe in/out dalla Piazza d'Armi (telemetria) |
| 3.55 | **defense** | Smart Reinforce Coordinator: villaggi non minacciati mandano rinforzi agli alleati sotto attacco |
| 3.6 | **war** | Wave Reclaim/Chiefing con `depart_ts` scaduto partono dal Punto di Raduno |
| 4 | **brain** | `brain.decide(state)` → `executor` agisce (con gate, vedi §4) |
| 4.0x | build plan | `plan_upgrades` simula i prossimi 4 upgrade → `state/build_plan_<acc>.json` (preview dashboard) |
| 4.1 | **farming** | Raid sui Custom Farm Targets approvati (o farm list nativa) |
| 4.1b | **AI Scout Brain** | Scansione mappa radar + report → raid dinamici, blacklist, arricchimento farm store |
| 4.1c | **replenishment** | Report di combattimento → blocco target "suicida", rimpiazzo perdite verso `troop_targets`/preset |
| 4.2 | **expansion** | Villaggi non-founder con Mercato spediscono surplus al founder (ponte risorse coloni) |
| 4.3 | **smithy** | Upgrade unità in Fucina, max 1 tentativo/villaggio ogni ~30 min |
| 5 | stealth | Pausa umana 2.5–5s prima del villaggio successivo |

**Gate trasversali per villaggio:**
- `builder_enabled = false` → nessuna nuova costruzione `UPGRADE_FIELD`
  (le code già avviate proseguono).
- `expansion_freeze` → demozione azioni (vedi §6).
- `farm_enabled` / ruolo → esclusione dai raid.
- Modulo in pausa anti-detection → audit `MODULE_PAUSED` e skip.

---

## 4. Il Brain: regole in cascata (`brain.RuleBasedBrain`)

`decide()` valuta le regole **in ordine**: la prima che ritorna un'azione
vince; le altre non si eseguono (`brain.py:689-699`):

| # | Regola | Trigger | Azione |
|---|--------|---------|--------|
| 1 | `rule_upgrade_storage` | Magazzino/granaio ≥90% | Potenzia/costruisci storage |
| 2 | `rule_grain_safety` | Bilancio netto grano < `min_net_crop_threshold` | Solo campi grano / granaio |
| 3 | `rule_emergency_overflow` | Storage al 100% e coda piena | Spesa locale costosa → invio a peer → (emergenza) |
| 4 | `rule_anti_raid` | Villaggio sotto pressione raid | Depositi Segreti multipli (GID 23) |
| 5 | `rule_building_focus` | Coda manuale attiva (`building_focus_enabled`) | Step della coda prioritaria |
| 6 | `rule_culture_boost` | Founder con Residenza/Palazzo ≥10, PC quasi pronti | Edifici ad alta resa PC |
| 7 | `rule_role_buildings` | Ruolo assegnato | Infrastrutture del ruolo |
| 8 | `rule_upgrade_weakest_field` | Coda risorse libera | Campo più debole della risorsa scarsa |
| 9 | `rule_wait` | fallback | Attesa ETA coda |

Il **Planner** (`builder.select_upgrade`, usato dentro le regole) ha la sua
catena di fonti, anch'essa in ordine (`builder.py:1186-1294`):

1. **storage** — magazzino/granaio quasi pieni;
2. **grain_safety** — margine grano sotto soglia;
3. **bootstrap** — sequenza startup nuovo villaggio (storage 5, Municipio 5,
   campi 3, Mercato + Punto di Raduno);
4. **reclaim** — `is_reclaim_source`: Accademia 20 + Residenza/Palazzo 20;
5. **anti_raid** — Nascondigli sotto pressione raid;
6. **focus** — `building_queue_plan` manuale;
7. **role** — preset del ruolo;
8. **economy** — campo più debole della risorsa scarsa.

---

## 5. La Coda Prioritaria Edifici (`building_queue_plan`)

Config per-villaggio: lista ordinata di step `{"key"|"gid", "target", "priority"}`.
`focus_intents` (`builder.py:438`) la consuma così:

- **Ordine sequenziale, non bloccante**: i candidati di ogni step incompleto
  sono raccolti nell'ordine della coda; il builder prende il **primo
  disponibile** → lavora lo step N finché ha slot utili, poi N+1.
- **"Tutti i campi" (risorsa)**: lo step include *tutti* i campi di quel tipo;
  è completo solo quando **ogni campo ≥ target**. I campi più bassi salgono
  per primi.
- **Edificio singolo**: basta **un'istanza ≥ target** per chiudere lo step.
- **Gruppi multi-GID** (mura 31/32/33, Residenza 25/Palazzo 26): alternative —
  ne basta una variante; `Residenza` e `Palazzo` sono mutuamente esclusivi.
- **Edificio assente**: nuova costruzione su slot vuoto se i prerequisiti
  reggono (`building_prereqs`).
- **Step saltati**: tutti gli slot in coda di costruzione o al livello max →
  si passa oltre.
- `building_focus_enabled = false` → la coda è ignorata (non cancellata): il
  bot torna a ruolo/economia.
- Priorità più alte (storage, grain safety, bootstrap, reclaim, anti-raid)
  possono **scavalcare** la coda in qualsiasi momento.
- **Doppia coda** (`builder.queue_slots_free`): i candidati di una vista con
  cantiere occupato vengono filtrati — con `allow_parallel_build` (romani /
  Plus) dorf1 e dorf2 hanno code indipendenti con i propri cap
  (`max_resource_queue`/`max_interior_queue` + tetto `max_build_queue`),
  altrimenti condividono l'unico slot coda.
- **Opportunistic Fallback** (in `_upgrade_field`, `actions.py`): se il primo
  candidato **C1** non è pagabile, il costo del contratto è letto dalla
  pagina e `ETA(C1) = max(deficit_r / prod_r)`:
  - `ETA(C1) ≤ 15 min` → attesa di C1 (niente fallback);
  - `ETA(C1) > 15 min` → si cerca il primo **Ck** pagabile con
    `ETA(C1 dopo aver pagato Ck) − ETA(C1) ≤ 30 min` (non-interferenza) e lo
    si avvia; altrimenti Resource-ETA Sleep su C1. L'orchestrator inietta
    `stock`/`prod_h` nei params dell'azione per il calcolo.

---

## 6. Freeze Espansione (`builder.expansion_freeze`)

Attivo **solo se TUTTE** queste condizioni (`builder.expansion_freeze`):

1. `expansion.spending_freeze = true`;
2. villaggio corrente con **ruolo founder** (`early_cropper_founder`, alias
   `founder`/`pioneer`/`early_cropper`);
3. coloni disponibili < 3;
4. **base economica matura** (`_freeze_economic_base`): almeno UNA tra
   - produzione oraria lorda totale (wood+clay+iron+crop) >
     `expansion.min_freeze_hourly_prod` (default **900/h** su server 1x,
     da impostare a ~ `speed * 900` su speed server);
   - media dei livelli dei campi risorsa ≥ **4.0**;
   - Residenza o Palazzo ≥ **8**;
5. PC sufficienti a fondare **oppure** Residenza/Palazzo ≥ 8.

La condizione 4 evita il freeze prematuro sui founder appena creati, dove
i PC del 2° villaggio sono già disponibili fin dall'account creation:
il villaggio sviluppa liberamente i campi fino a ~liv. 4-5, poi il
freeze scatta automaticamente per accumulare i 3 coloni.

Effetto: le azioni `UPGRADE_FIELD` **non in whitelist** vengono demotate a
`WAIT`; anche `TRAIN_TROOPS`/`TRAIN_SETTLERS` non critici sono bloccati.
**Whitelist GID**: `10, 11, 15, 17, 23, 25, 26` (Magazzino, Granaio,
Municipio, Mercato, Nascondiglio, Residenza, Palazzo).

In telemetria ogni villaggio esporta `freeze_active` (calcolo reale) → il
pill "⏸ Freeze" in dashboard appare solo a regola effettivamente attiva.

### 6.1 Riscatto premi on-demand (Step 2.5)

`claim_village_tasks` (actions.py) non incassa più ciecamente: `_scan_task_rewards`
legge contenuto e stato di ogni premio **senza cliccare**, poi
`task_reward_claimable` decide premio per premio. Un premio-risorse è
riscattato **solo se TUTTE** le condizioni valgono:

1. **Zero overflow**: per ogni risorsa assegnata,
   `stock + premio ≤ 92%` della capienza (`TASK_CLAIM_CAP_RATIO`);
   capienza non leggibile → fail-closed;
2. **Anti-raid**: nessuna minaccia registrata con impatto entro
   20 min (`_TASK_CLAIM_THREAT_WINDOW`, da `defense_mgr.threats`);
3. **Trigger di utilità**: esiste un obiettivo pendente **non**
   pagabile con lo stock che diventa pagabile col premio — il primo
   step del build plan persistito (`level_cost` stimato) o, sul
   fondatore in deficit coloni (`feed_target`), il costo di un colono.

**Eccezione**: premi non-risorsa (XP eroe, Punti Cultura, oggetti) sono
sempre liberi; premi-truppe solo con `crop_balance ≥ 0`. Il riscatto
avviene prima del Sense, quindi il premio entra nello stock letto allo
step 3 e il builder lo spende nello stesso ciclo. `claim_ctx=None`
mantiene la modalità legacy (riscuote tutto).

---

## 7. Moduli

### Espansione (`expansion.py`)
- Scansiona i cropper 9c/15c sulla mappa (`cropper_scan`), classifica per
  qualità, gestisce `founded_tiles` e `bootstrap_target`.
- **Founder** (`expansion.founder_village`): il villaggio designato addestra
  i 3 coloni in Residenza/Palazzo e fonda quando i PC sono pronti.
- **Feeder bridge**: `feed_target` = destinatario del surplus; i villaggi
  non-founder con Mercato spediscono risorse al founder (e al villaggio in
  bootstrap finché non chiude).

### Farming (`farming.py`)
- Raid a intervallo `farming.interval_min` sui target approvati
  (`farm_targets_<acc>.json`), solo da villaggi con ruolo raiding
  (esclusi `early_cropper_founder` e `feeder`, `orchestrator.py:2167`):
  ogni villaggio raida solo i target di cui è il più vicino, non attaccati
  di recente. Fallback: farm list nativa del Punto di Raduno.

### AI Scout Brain (`ai_scout_brain.py`)
- Scansiona la mappa (`scan_radius`), legge i report di combattimento,
  classifica i target (bottino/difesa), mantiene blacklist e alimenta il
  farm store. Escluso dai villaggi non-raiding.

### Replenishment (`replenishment.py`)
- Analizza i report: blocca i target che hanno causato wipe
  (`auto_block`, `fail_streak_threshold`), rimpiazza le perdite verso
  `troop_targets` o il preset tribù (`troop_replenishment_preset`).
- **Congelamento**: durante emergenza difesa o assedio con ondate ≥
  `siege_waves` il rimpiazzo si ferma (villaggio "frozen").

### Difesa (`defense.py`)
- Prima riga del ciclo villaggio: legge gli attacchi in arrivo, decide
  dodge/schedulazione evacuazione, coordina i rinforzi tra villaggi
  (`reinforce_coordination`).

### Eroe (`hero.py`)
- Avventure automatiche (`auto_adventures`) se salute ≥ soglia, unguenti
  sotto `heal_threshold`, `emergency_unpack` di risorse dall'inventario in
  caso di necessità, focus attributi.

### Guerra / Reclaim (`war.py`)
- Registro `war_targets_<acc>.json`: villaggi `STOLEN` → `RECLAIMING` →
  `RECLAIMED`; pianifica e lancia wave chiefing con `depart_ts`.

### Anti-Raid (in `builder.py`)
- Sotto pressione raid costruisce/potenzia Depositi Segreti multipli
  (upkeep 0 → nessun impatto grano); protezione risorse oltre la capacità
  di un singolo nascondiglio.

---

## 8. Ruoli villaggio

`presets.resolve_role` normalizza (`VillageRole` in `models.py`):

| Ruolo | Profilo |
|-------|---------|
| `standard` | Sviluppo bilanciato |
| `early_cropper_founder` | Founder: 3 coloni, freeze spese, escluso dai raid |
| `croplog` | Capitale grano: focus campi, accetta surplus |
| `feeder` | Economico: Mercato, spedisce surplus, escluso dai raid |
| `hammer` | Offensivo: caserma/scuderia/officina |
| `defensive` | Scudo: mura, residenza, truppe difensive |

Sorgente del ruolo: `village_config_for` → `cfg.role`; fallback
`village_roles` legacy; default `standard`. La telemetria mostra il ruolo
configurato, altrimenti quello runtime del modello.

---

## 9. Dashboard ↔ Agente

- **Config save**: `POST /api/config` → patch su `accounts.json` +
  settings live del command server; patch vuoto → rifiutato
  (`command_server._handle_config_patch`).
- **Force cycle**: `POST /api/command` `FORCE_TICK` → flag file → interrompe
  l'attesa del loop.
- **Pausa**: `state/pause_<acc>.flag` → il loop si ferma e riprova ogni 10s.
- **Audit**: ogni decisione finisce nel log decisioni (status
  EXECUTED/SKIPPED/BLOCKED + motivo) mostrato in dashboard.
- **Hot-reload** copre: villaggi/ruoli/code, expansion, farming, scout,
  hero, defense, replenishment, `builder_enabled`, `auto_video_bonus`,
  `min_net_crop_threshold`, intervalli, `strategy_profile`,
  `allow_parallel_build`.

---

## 10. File di stato (`state/`)

| File | Contenuto |
|------|-----------|
| `accounts.json` | Config completa account (fonte di verità) |
| `dashboard_state_<acc>.json` | Snapshot telemetria ultimo ciclo |
| `session_<acc>.json` | Stato sessione browser/login |
| `build_plan_<acc>.json` | Preview 4 upgrade simulati per villaggio |
| `expansion_config.json` / `expansion_<acc>.json` | Stato ExpansionManager (CP, coloni, tile) |
| `farm_targets_<acc>.json` | Target raid approvati/blacklist |
| `war_targets_<acc>.json` | Reclaim/chiefing registry |
| `manual_commands_<acc>.json` | Coda comandi dashboard |
| `pause_<acc>.flag` / `force_tick_<acc>.flag` | Pausa worker / forza ciclo |
