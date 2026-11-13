"""Selettori CSS centralizzati.

I selettori qui sotto corrispondono al markup attuale di Travian Legends.
Cambiano spesso tra versioni/server: se il parser non trova un elemento,
aggiornare SOLO questo file (o aggiungere alternative nelle liste).

Ogni voce e' una lista di selettori alternativi: il primo che trova
un elemento vince.
"""

from __future__ import annotations

LOGIN_FORM: list[str] = [
    "#loginForm",
    "form[name='login']",
    "form.login",
]
LOGIN_USERNAME_INPUT: list[str] = [
    "input[name='name']",
    "input[name='login']",
    "input[type='text'][name='email']",
]
LOGIN_PASSWORD_INPUT: list[str] = [
    "input[name='password']",
    "input[type='password']",
]
LOGIN_SUBMIT_BUTTON: list[str] = [
    "button[type='submit']",
    "input[type='submit']",
    "button.green",
    ".loginButton button",
]

# Marker che indica una sessione autenticata (IN-GAME)
LOGGED_IN_MARKER: list[str] = [
    "#stockBar",
    "#resourceFieldContainer",
    "#village_map",
    "#villageMap",
    "#sidebarBoxHero",
    "#sidebarBoxVillagelist",
    "#sidebarBoxVillageList",
    "a[href*='logout.php']",
    "#questmasterButton",
    ".villageView",
]

# Form di login (per rilevare sessione non attiva)
LOGIN_CONTAINERS: list[str] = [
    "input[name='password']",
    "input[type='password']",
    "form[name='login']",
    "#loginForm",
]

# Stock bar: li#l1 legno, l2 argilla, l3 ferro, l4 grano
STOCK_BAR_ITEMS: dict[str, str] = {
    "wood": "#l1",
    "clay": "#l2",
    "iron": "#l3",
    "crop": "#l4",
}
STOCK_VALUE: list[str] = [".value", ".stockBarButton", ".text"]

# Magazzini (capacita'): di solito nel title/tooltip del li, es. "3300||8000"
WAREHOUSE_ITEM: list[str] = [
    "#stockBar .warehouse",
    "#stockBar li.warehouse",
    "#stockBarWarehouse",
    ".stockBarButton.warehouse",
    ".warehouse",
    "[id*='warehouse' i]",
]
GRANARY_ITEM: list[str] = [
    "#stockBar .granary",
    "#stockBar li.granary",
    "#stockBarGranary",
    ".stockBarButton.granary",
    ".granary",
    "[id*='granary' i]",
]

# Mercato (dorf2, gid 17): form di invio risorse ad altri villaggi.
# T4.x: build.php?gid=17 / build.php?id=<slot>, tab "send" con input r1-r4.
MARKET_TAB_SEND: list[str] = [
    "a[href*='t=5']",
    "a[href*='tt=1']",
    "a[href*='t=0']",
    "a.tabItem[href*='t=5']",
    "a.tabItem[href*='tt=1']",
    "a.tabItem[href*='t=0']",
    ".contentNavi a[href*='t=5']",
    ".contentNavi a[href*='tt=1']",
    ".contentNavi a[href*='t=0']",
    "a.tabItem:has-text('Invia')",
    "a.tabItem:has-text('Send')",
    "a.tabItem:has-text('Gestione')",
    "a.tabItem:has-text('Management')",
    # UI moderne: le tab del mercato possono essere button/div/role=tab,
    # non <a> — il click va provato anche su elementi non-anchor
    "button:has-text('Invia risorse')",
    "button:has-text('Send resources')",
    "[role='tab']:has-text('Invia')",
    "[role='tab']:has-text('Send')",
    ".tabItem:has-text('Invia risorse')",
    ".tabItem:has-text('Send resources')",
    "#content button:has-text('Invia')",
    "#content button:has-text('Send')",
    "#content a:has-text('Invia risorse')",
    "#content a:has-text('Send resources')",
    "#content a:has-text('Invia')",
    "#content a:has-text('Send')",
    ".contentNavi a:first-child",
]
MARKET_SEND_INPUTS: dict[str, list[str]] = {
    "wood": ["input[name='lumber']", "input[name='r1']", "input#r1", "input#lumber", "input[id*='lumber']", "input[id*='r1']", "input[name*='r1']"],
    "clay": ["input[name='clay']", "input[name='r2']", "input#r2", "input#clay", "input[id*='clay']", "input[id*='r2']", "input[name*='r2']"],
    "iron": ["input[name='iron']", "input[name='r3']", "input#r3", "input#iron", "input[id*='iron']", "input[id*='r3']", "input[name*='r3']"],
    "crop": ["input[name='crop']", "input[name='r4']", "input#r4", "input#crop", "input[id*='crop']", "input[id*='r4']", "input[name*='r4']"],
}
# ATTENZIONE: questi selettori vanno usati SEMPRE scoped dentro il form di
# invio marcato ([data-tts-sendform]). Il campo 'rinomina villaggio' in
# testata alle pagine dorf/build usa lo stesso name='dname'/'illage':
# applicati all'intera pagina rinominerebbero il villaggio attivo.
MARKET_TARGET_INPUT: list[str] = [
    "input[name='dname']",
    "input[name='targetVillage']",
    "input#enterVillageName",
    "input[name='villageName']",
    "input[name='targetVillageName']",
    "input[name*='illage']",
    "input[id*='illage']",
    "input[name='destination']",
    "input[name*='destination']",
    "input[name='recipient']",
    "input[id*='village' i]",
    "input[placeholder*='illagg']",  # 'villaggio'
    "input[placeholder*='illage']",  # 'village'
    "input[placeholder*='estin']",   # 'destinazione'/'destination'
]
# Suggerimenti dell'autocomplete dopo aver digitato il nome villaggio
MARKET_TARGET_SUGGESTION: list[str] = [
    "ul.suggestList li",
    ".autoComplete li",
    ".ui-autocomplete li",
    ".ui-menu-item",
    ".suggestions li",
    "li.suggestion",
    "[class*='suggest'] li",
    "[class*='autocomplete'] li",
    # NB: NON includere '.villageList li' — e' la sidebar dei villaggi:
    # cliccarla naviga verso quel villaggio e cambia il contesto attivo.
]
MARKET_SEND_BUTTON: list[str] = [
    "button:has-text('Invia risorse')",
    "button:has-text('Send resources')",
    "button[type='submit']",
    "input[type='submit']",
    "button.green",
    "#enabledButton",
]
# Seconda pagina di conferma invio (T4.x mostra un riepilogo prima dello smistamento)
# Il riepilogo di conferma va cercato solo nel contenuto principale e mai
# nel form di rinomina villaggio (che contiene input[name='dname']).
MARKET_CONFIRM_BUTTON: list[str] = [
    "#content button:has-text('Invia risorse')",
    "#content button:has-text('Send resources')",
    "#content button:has-text('Conferma')",
    "#content button:has-text('Confirm')",
    "#content button.send",
    "#content #btn_ok",
    "#content form:not(:has(input[name='dname'])) button[type='submit']",
    "#content form:not(:has(input[name='dname'])) input[type='submit']",
]

# Punto di Raduno (dorf2, gid 16): tab liste raid / farm list.
# T4.x: build.php?id=<slot>&tt=99 (o t=99) mostra le liste pre-impostate.
RALLY_FARM_TAB: list[str] = [
    "a[href*='tt=99']",
    "a[href*='t=99']",
    "a[href*='farm_list']",
    "a.tabItem:has-text('Liste raid')",
    "a.tabItem:has-text('Lista raid')",
    "a.tabItem:has-text('Lista farm')",
    "a.tabItem:has-text('Raid list')",
    "a.tabItem:has-text('Farm list')",
    "[role='tab']:has-text('Lista farm')",
    "button:has-text('Lista farm')",
    "a:has-text('Liste raid')",
    "a:has-text('Lista raid')",
    "a:has-text('Lista farm')",
    "a:has-text('Raid list')",
    "a:has-text('Farm list')",
]
# Righe delle farm list disponibili (ogni riga ha nome, n. obiettivi, controlli)
FARM_LIST_ROWS: list[str] = [
    ".farmList .listEntry",
    "#raidList .listEntry",
    ".raidList .listEntry",
    "#raidList tr",
    ".raidList tr",
    "#raidListTable tr",
    "form[name*='farm' i] tr",
    ".farmList .slotRow",
    ".listEntry",
    "tr.farmList",
    ".farmList li",
    "#raidList li",
]
# Checkbox "seleziona tutti" dentro una riga di farm list
FARM_LIST_SELECT_ALL: list[str] = [
    "input[type='checkbox'].markAll",
    ".markAll input[type='checkbox']",
    "input[type='checkbox'][name*='all' i]",
    "input[type='checkbox'].checkAll",
]
# Pulsante di avvio raid (in basso alla pagina o dentro la riga).
# UI corrente: il pulsante riporta il conteggio target, es. "Avvia (6)".
# La parentesi nel testo evita di matchare "Avvia tutte le liste farm".
FARM_LIST_SEND_BUTTON: list[str] = [
    "button:has-text('Avvia (')",
    "[role='button']:has-text('Avvia (')",
    "a:has-text('Avvia (')",
    "button:has-text('Start (')",
    "[role='button']:has-text('Start (')",
    "a:has-text('Start (')",
    "input[type='submit'][value*='Avvia']",
    "input[type='submit'][value*='Start']",
    "input[type='submit'][value*='Invia']",
    "input[type='submit'][value*='Send']",
    "button:has-text('Avvia raid')",
    "button:has-text('Invia raid')",
    "button:has-text('Lancia raid')",
    "button:has-text('Start raid')",
    "button:has-text('Send raid')",
    "button.green:has-text('raid')",
    "button.green:has-text('Raid')",
    # Fallback: qualsiasi bottone "Avvia"/"Start" — in ultima posizione
    # perche' puo' catturare anche "Avvia tutte le liste farm".
    "button:has-text('Avvia')",
    "button:has-text('Start')",
    "button[type='submit'].green",
    "button[type='submit']",
    "input[type='submit'].green",
    "input[type='submit']",
]

# Mappa (karte.php): fondazione di un nuovo villaggio su tile libero.
# Il bottone compare nel pannello dettagli tile: "Fondare un villaggio".
MAP_TILE_INFO: list[str] = [
    "#map_details",
    ".map_details",
    "#tileDetails",
    ".tileDetails",
    "#map_infobox",
    ".infobox",
]
MAP_SETTLE_BUTTON: list[str] = [
    "a[href*='a=settler']",
    "a[href*='settler']",
    "a:has-text('Fondare un villaggio')",
    "a:has-text('Fonda un villaggio')",
    "a:has-text('Fonda qui')",
    "button:has-text('Fondare un villaggio')",
    "button:has-text('Fonda un villaggio')",
    "button:has-text('Fonda qui')",
    "a:has-text('Settle')",
    "a:has-text('Found a village')",
    "button:has-text('Settle')",
    "button:has-text('Found')",
]
MAP_SETTLE_CONFIRM: list[str] = [
    "button:has-text('Fonda il villaggio')",
    "button:has-text('Fonda')",
    "button:has-text('Conferma')",
    "button:has-text('Confirm')",
    "button:has-text('Settle')",
    "button[type='submit']",
    "input[type='submit']",
    "#btn_ok",
]

# Residenza/Palazzo (gid 25/26): punti cultura e slot coloni
CP_TEXT_CONTAINER: list[str] = [
    "#content .content",
    "#build",
    ".buildingDetails",
    "table#build_value",
    "#mainContent",
]
SETTLER_UNIT_NAMES: list[str] = [
    "colono",
    "coloni",
    "settler",
    "settlers",
    "siedler",
    "colon",
]

# Info Box / Notifiche
INFO_BOX: list[str] = [
    "#sidebarBoxInfoBox",
    "#infoBox",
    ".infoBox",
]
INFO_BOX_SHOP_BUTTON: list[str] = [
    "#sidebarBoxInfoBox button:has-text('Apri il negozio')",
    "#sidebarBoxInfoBox a:has-text('Apri il negozio')",
    "#infoBox button:has-text('Apri il negozio')",
    "#infoBox a:has-text('Apri il negozio')",
    ".infoBox button:has-text('Apri il negozio')",
    "button:has-text('Apri il negozio')",
    "button:has-text('Open shop')",
    "a:has-text('Apri il negozio')",
]

# Shop / Finestra Vantaggi (+15% video bonus)
SHOP_GOLD_BUTTON: list[str] = [
    "#goldButton",
    "a.gold",
    "button.gold",
    ".gold.coins",
    "a[href*='payment']",
    "a[href*='gold']",
    "#sidebarBoxHero .gold",
]
SHOP_TAB_ADVANTAGES: list[str] = [
    "a.tabItem:has-text('Vantaggi')",
    "a.tabItem:has-text('Advantages')",
    ".tabItem:has-text('Vantaggi')",
    ".tabItem:has-text('Advantages')",
    "a:has-text('Vantaggi')",
    "a:has-text('Advantages')",
    "[data-tab='advantages']",
    ".contentNavi a:has-text('Vantaggi')",
    ".contentNavi a:last-child",
]
SHOP_VIDEO_BONUS_BUTTONS: list[str] = [
    ".dialog button:has-text('Attiva')",
    "#paymentWizard button:has-text('Attiva')",
    ".featureProduction button",
    ".productionBoost button",
    "button.video",
    "button:has-text('Attiva')",
    "button:has-text('Activate')",
]
SHOP_DIALOG_CLOSE: list[str] = [
    ".dialogCancelButton",
    ".dialogCloseButton",
    "button.dialogCancelButton",
    "button.dialogCloseButton",
    "a.dialogCancelButton",
    "#paymentWizard .dialogCancelButton",
    "#paymentWizard .x",
    ".dialog .x",
    "button.x",
    "button[title*='Chiudi']",
    "button[title*='Close']",
]

# Caserma/Scuderia/Officina (dorf2): form di addestramento truppe.
# T4.x: build.php?id=<slot> lista le unita' sbloccate con input t1..tN.
TRAIN_INPUTS: list[str] = [
    "input[name^='t'][type='text']",
    "input[name^='t'][type='number']",
    "input[name^='t']",
    "input.text[name*='troop']",
    "input.troopInput",
    "input[name*='unit']",
    "input.unitText",
    ".trainUnits input[type='text']",
    ".trainUnits input[type='number']",
    ".innerTrainUnitBox input",
]
TRAIN_MAX_LINKS: list[str] = [
    "a.max",
    "span.max",
    ".unit .max",
    ".cta .max",
    "a[href*='troop']",
    "a.fill-max",
    "a[onclick*='max']",
    ".innerTrainUnitBox a[href*='javascript']",
    ".trainUnits .max",
]
TRAIN_SUBMIT: list[str] = [
    "button[type='submit']",
    "input[type='submit']",
    "button.startTraining",
    "button.train",
    "button.textButtonV1",
    "button.green[type='submit']",
    "button.green",
    "form[name='snd'] button",
    "form[name='snd'] input[type='submit']",
    "form[name='snd'] input[type='image']",
    ".trainUnits button",
    "button.build",
    "#s1",
    "button[value*='train' i]",
    "button[value*='addestr' i]",
    "button[value*='ausbilden' i]",
    ".action button",
]

# Campi risorse nel dorf1 (slot 1-18) ed edifici nel dorf2 (slot 19-40)
VILLAGE_MAP: list[str] = [
    "#resourceFieldContainer",
    "#resourceFields",
    ".resourceFields",
    "#village_map",
    "#villageMap",
    "#rx",
    ".villageView",
    "#map_dorf1",
    ".mapWrapper",
    "body",
]
VILLAGE_FIELD: list[str] = [
    ".buildingSlot",
    "a.buildingSlot",
    "div.buildingSlot",
    "[class*='buildingSlot']",
    "a[href*='build.php?id=']",
    "area",  # fallback imagemap versioni datate
]

# Coda costruzioni (sidebar)
BUILD_QUEUE_ROWS: list[str] = [
    "#buildingContract .buildingRow",
    ".buildingList .buildingRow",
    ".buildingList li",
    ".buildingsOverview .buildingRow",
    "table#building_contract tr",
]
BUILD_QUEUE_NAME: list[str] = [".name", ".building", "img[alt]"]
BUILD_QUEUE_TIMER: list[str] = [".timer", ".dur", ".clocks"]

# Attacchi in arrivo
TROOP_MOVEMENT_BOX: list[str] = [
    "#sidebarBoxActiveTroopMovements",
    "#sidebarBoxActiveTroopmovements",
    "[id*='TroopMovements' i]",
    "[id*='troop' i]",
    ".troopMovements",
]
INCOMING_ATTACK_ROWS: list[str] = [
    "tr.att",
    ".att",
    ".attack",
    "img.att1",
]

# Pagina costruzione (build.php?id=N)
UPGRADE_BUTTON: list[str] = [
    ".section1 button.green.build",
    "button.green.build",
    "button.green.upgrade",
    ".contractLink button.green",
]
NOT_ENOUGH_RESOURCES: list[str] = [
    ".contractLink .none",
    ".error",
    ".warning",
]
UPGRADE_COST_VALUE: list[str] = [".showCosts .resources .value", ".costs .r"]

# Nome villaggio attivo
ACTIVE_VILLAGE_NAME: list[str] = [
    "#sidebarBoxVillagelist .active .name",
    "#sidebarBoxVillageList .active .name",
    "[id*='villagelist' i] .active .name",
    "[id*='villageList' i] .active .name",
    ".villageList .active .name",
    ".villagesList .active .name",
    "#villageNameField",
    ".villageName",
    "#sidebarBoxVillagelist .active",
    "#sidebarBoxVillageList .active",
    "[data-did].active .name",
]

# Lista villaggi dell'account (sidebar destra)
VILLAGE_LIST_ENTRIES: list[str] = [
    "#sidebarBoxVillagelist .listEntry",
    "#sidebarBoxVillagelist .entry",
    "#sidebarBoxVillagelist li",
    "#sidebarBoxVillageList .listEntry",
    "#sidebarBoxVillageList .entry",
    "#sidebarBoxVillageList li",
    "[id*='villagelist' i] .entry",
    "[id*='villagelist' i] li",
    "[id*='villageList' i] .entry",
    "[id*='villageList' i] li",
    "[data-did]",
    ".villageList .entry",
    ".villageList .listEntry",
    ".villageList li",
    ".villagesList li",
]
VILLAGE_ENTRY_LINK: list[str] = ["a[href*='newdid']", "a"]
VILLAGE_ENTRY_NAME: list[str] = [".name", ".villageName", "a"]

# Parametro URL per cambiare villaggio attivo
PARAM_NEWDID = "newdid"

# Questmaster / Task giornaliere: pannello premi riscattabili.
# T4.x: l'icona #questmasterButton apre un dialog; le task completate
# mostrano un pulsante "Riscuoti"/"Claim reward" nella lista.
QUEST_MASTER_BUTTON: list[str] = [
    "#questmasterButton",
    "a[href*='questmaster']",
    "#questmaster a",
    ".questmasterButton",
    "a[onclick*='questmaster']",
    "a[href*='tasks?t=']",
    "a[href*='tasks.php']",
]
# Pulsanti di riscossione premio dentro il dialog/pagina task.
# UI nuova: bottoni verdi cliccabili dentro #tasks/.tasks con testo
# 'Raccogli' (fallback multilingua Collect/Claim).
QUEST_CLAIM_BUTTON: list[str] = [
    "#tasks button:has-text('Raccogli')",
    "#tasks button:has-text('Collect')",
    "#tasks button:has-text('Claim')",
    ".tasks button:has-text('Raccogli')",
    ".tasks button:has-text('Collect')",
    ".tasks button:has-text('Claim')",
    "#dialogContent .quest button:has-text('Riscuoti')",
    "#dialogContent .quest button:has-text('Claim')",
    "#dialogContent button:has-text('Riscuoti premio')",
    "#dialogContent button:has-text('Claim reward')",
    ".quest button:has-text('Riscuoti')",
    ".quest button:has-text('Claim')",
    "button:has-text('Raccogli')",
    "button:has-text('Riscuoti premio')",
    "button:has-text('Claim reward')",
    "button:has-text('Riscuoti')",
    "button:has-text('Claim reward')",
    "a:has-text('Raccogli')",
    "a:has-text('Riscuoti premio')",
    "a:has-text('Claim reward')",
]
# Badge/notifica sull'avatar del Questmaster (T4.6): indica task
# completate da riscuotere. Usato come gate prima di navigare /tasks.
QUEST_PENDING_BADGE: list[str] = [
    "#questmasterButton .speechBubble",
    "#questmasterButton .notification",
    "#questmasterButton .badge",
    "#questmasterButton .bubble",
    "#questmasterButton .count",
    "#questmaster .speechBubble",
    ".questmasterButton .speechBubble",
    ".questmasterButton .notification",
    "a[href*='tasks'] .speechBubble",
    "a[href*='tasks'] .notification",
    ".questIndicator",
]
# Contenitori del dialog task/questmaster
QUEST_DIALOG: list[str] = [
    "#tasks",
    ".tasks",
    "#dialogContent",
    ".questmaster",
    "#questmasterDialog",
    ".dialog",
    "#modal",
]
QUEST_DIALOG_CLOSE: list[str] = [
    "#dialogContent .close",
    ".dialog .close",
    "#modal .close",
    "button:has-text('Chiudi')",
    "button:has-text('Close')",
    ".dialogButtonCancel",
]

# Path interni del gioco
PATH_DORF1 = "dorf1.php"
PATH_DORF2 = "dorf2.php"
PATH_BUILD = "build.php"
PATH_MAP = "karte.php"
PATH_QUESTMASTER = "questmaster.php"
# Task giornaliere UI moderna: pagine dedicate accanto al legacy
# questmaster.php (village = task del villaggio, general = task account).
PATH_TASKS_VILLAGE = "tasks?t=village"
PATH_TASKS_GENERAL = "tasks?t=general"
PATH_LOGIN = "login.php"
PATH_INDEX = "index.php"
