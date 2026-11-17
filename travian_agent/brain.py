"""Cervello: trasforma un GameState nella prossima Action.

Due implementazioni:
- RuleBasedBrain: albero di regole deterministiche, valutate in ordine
  di priorita'. La prima regola che produce un'Action vince.
- LLMBrain: stub che mostra dove inserire un modello linguistico
  (serializza lo stato -> prompt -> parsa la mossa scelta).
"""

from __future__ import annotations

import logging
from typing import Callable, Protocol

from . import builder, debug_trace
from .config import Settings
from .models import (
    GID_BARRACKS,
    GID_GRANARY,
    GID_MARKETPLACE,
    GID_PALACE,
    GID_STABLE,
    GID_WAREHOUSE,
    GID_WORKSHOP,
    Action,
    ActionType,

    Field,
    GameState,
    ResourceType,
)

logger = logging.getLogger(__name__)

Rule = Callable[[GameState, Settings], Action | None]


class DecisionMaker(Protocol):
    """Interfaccia comune: qualsiasi 'cervello' deve implementarla."""

    def decide(self, state: GameState) -> Action: ...


# --------------------------------------------------------------------------
# Regole deterministiche (ordine = priorita')
# --------------------------------------------------------------------------

# NOTA anti-razzia (spec): nessuna regola 'alert under attack' nel
# decision tree — un ALERT qui congela il builder su QUALSIASI ondata
# in arrivo. Sotto attacco il builder deve invece continuare a spendere
# (la risposta anti-raid e' delegata a defense.py: panic <180s ->
# resource drain forzato su code/addestramento + drain mercato).

# Livelli massimi noti degli edifici Travian nel dorf2 (default prudente: 20).
_BUILDING_MAX_LEVEL: dict[int, int] = {
    GID_WAREHOUSE: 20,
    GID_GRANARY: 20,
}
# Campi risorsa del dorf1: livello 10 nei villaggi normali, 21 in capitale.
_FIELD_MAX_NORMAL = 10
_FIELD_MAX_CAPITAL = 21


def _is_capital(state: GameState) -> bool:
    """True se nel dorf2 esiste il Palazzo (presente solo in capitale)."""
    return builder.is_capital(state)


def _field_max_level(state: GameState, f: Field) -> int:
    """Livello massimo raggiungibile dal field (stima conservativa)."""
    return builder.field_max_level(state, f)


def _queued_slot_ids(state: GameState) -> set[int]:
    """Slot gia' occupati da una costruzione in coda."""
    return {q.slot_id for q in state.build_queue if q.slot_id > 0}


def _upgradeable_fields(state: GameState, fields: list[Field]) -> list[Field]:
    """Filtro di validazione preventiva sui candidati UPGRADE_FIELD.

    Esclude gli slot gia' occupati da una costruzione in corso (slot_id
    letto dalla coda) e i field gia' al livello massimo, cosi' l'azione
    contiene solo upgrade realmente avviabili.
    """
    return builder.upgradeable_fields(state, fields)


_RES_NAMES: tuple[str, ...] = ("legno", "argilla", "ferro", "grano")


def _field_upgrade_affordable(state: GameState, f: Field) -> bool:
    """True se lo stock copre il costo stimato del prossimo livello."""
    cost = builder.field_upgrade_cost(builder.field_gid(f), f.level + 1)
    if cost is None:
        return True  # tabella non applicabile: non bloccare il campo
    res = state.resources
    return (
        res.wood >= cost[0]
        and res.clay >= cost[1]
        and res.iron >= cost[2]
        and res.crop >= cost[3]
    )


def _field_upgrade_deficit(state: GameState, f: Field) -> str:
    """Deficit per risorsa del prossimo livello (per i messaggi di skip)."""
    cost = builder.field_upgrade_cost(builder.field_gid(f), f.level + 1)
    res = state.resources
    stock = (res.wood, res.clay, res.iron, res.crop)
    if cost is None:
        return "costo stimato non disponibile"
    parts = [
        f"servono ~{cost[i]} {_RES_NAMES[i]}, disponibili {stock[i]}"
        for i in range(4)
        if cost[i] > stock[i]
    ]
    return "; ".join(parts) or "costo coperto"


def _queue_free(state: GameState, settings: Settings, resource_field: bool) -> bool:
    """True se la coda pertinente (campi o interni) puo' accettare un upgrade."""
    if builder.parallel_build_enabled(settings):
        if resource_field:
            return (
                len(state.resource_queue()) < settings.max_resource_queue
                and len(state.build_queue) < settings.max_build_queue
            )
        return (
            len(state.interior_queue()) < settings.max_interior_queue
            and len(state.build_queue) < settings.max_build_queue
        )
    return not state.build_queue


def rule_upgrade_weakest_field(state: GameState, settings: Settings) -> Action | None:
    """Se la coda risorse e' libera, propone l'upgrade del campo piu' debole.

    Supporta la costruzione parallela (es. Romani o Travian Plus): se c'e'
    gia' un edificio interno in costruzione nel dorf2 ma nessun campo
    risorsa nel dorf1, l'upgrade del campo puo' comunque partire.
    """
    # Grain safety: margine netto sotto soglia -> niente upgrade non-grano
    if builder.crop_safety_needed(state, settings):
        return None

    # Freeze espansione (spec Corsa 15c): i campi risorsa non sono nella
    # whitelist dei passi critici — se questa regola vincesse, il demote
    # la trasformerebbe in WAIT scavalcando la Coda Prioritaria
    # (deadlock osservato alle 21:49 su Hipponion).
    if builder.expansion_freeze(state, settings):
        return None

    if builder.parallel_build_enabled(settings):
        if (
            len(state.resource_queue()) >= settings.max_resource_queue
            or len(state.build_queue) >= settings.max_build_queue
        ):
            return None
    elif state.build_queue:
        return None

    fields = _upgradeable_fields(state, state.resource_fields())
    if not fields:
        return None

    role = settings.village_role_for(state.village_name, state.village_id)
    if role == "croplog":
        scarcest = ResourceType.CROP
    else:
        scarcest = state.resources.scarcest()
    candidates = sorted(
        (f for f in fields if f.resource_type is scarcest),
        key=lambda f: (f.level, f.slot_id),
    )
    if not candidates:
        candidates = sorted(fields, key=lambda f: (f.level, f.slot_id))

    # Affordability: il campo piu' debole non pagabile non deve bloccare
    # gli altri — prima tra i campi del tipo scarcest, poi tra tutti.
    affordable = [f for f in candidates if _field_upgrade_affordable(state, f)]
    if not affordable:
        affordable = [
            f
            for f in sorted(fields, key=lambda f: (f.level, f.slot_id))
            if _field_upgrade_affordable(state, f)
        ]
    if not affordable:
        return None

    target = affordable[0]
    return Action(
        type=ActionType.UPGRADE_FIELD,
        params={
            "candidates": [f.slot_id for f in candidates],
            "resource": scarcest.value,
        },
        reason=(
            f"Coda libera: upgrade di '{builder.field_label(target)}' "
            f"(livello {target.level}) per aumentare {scarcest.value}"
        ),
    )


def weakest_field_skip_reason(
    state: GameState, settings: Settings
) -> str:
    """Motivo esatto per cui rule_upgrade_weakest_field non produce azioni.

    Replica i gate della regola nell'ordine in cui vengono valutati e
    restituisce il primo blocco incontrato, con conteggi espliciti:
    fields_count=0 (dorf1 non letto/esportato), campi tutti in coda o
    al cap, coda dorf1/totale piena, freeze o grain safety.
    """
    if builder.crop_safety_needed(state, settings):
        return "grain safety attiva (solo upgrade grano ammessi)"
    if builder.expansion_freeze(state, settings):
        return "freeze espansione attivo (campi fuori whitelist)"
    if builder.parallel_build_enabled(settings):
        res_q = len(state.resource_queue())
        tot_q = len(state.build_queue)
        if res_q >= settings.max_resource_queue:
            return (
                f"coda dorf1 piena "
                f"({res_q}/{settings.max_resource_queue})"
            )
        if tot_q >= settings.max_build_queue:
            return (
                f"coda totale piena "
                f"({tot_q}/{settings.max_build_queue})"
            )
    elif state.build_queue:
        return f"coda occupata ({len(state.build_queue)} lavori)"

    raw_fields = state.resource_fields()
    if not raw_fields:
        return (
            "fields_count=0: nessun campo dorf1 nello stato "
            "(snapshot/lettura sensori senza i 18 campi)"
        )
    queued = _queued_slot_ids(state)
    in_queue = [f for f in raw_fields if f.slot_id in queued]
    at_cap = [
        f
        for f in raw_fields
        if f.level >= builder.field_max_level(state, f)
    ]
    upgradable = [
        f
        for f in raw_fields
        if f.slot_id not in queued
        and f.level < builder.field_max_level(state, f)
    ]
    if upgradable:
        # Campi upgradabili ma nessuna azione: quasi certamente il
        # filtro affordability ha scartato tutti i candidati.
        affordable = [
            f for f in upgradable if _field_upgrade_affordable(state, f)
        ]
        if not affordable:
            weakest = sorted(
                upgradable, key=lambda f: (f.level, f.slot_id)
            )[0]
            return (
                f"risorse insufficienti per "
                f"'{builder.field_label(weakest)}' lv.{weakest.level + 1}: "
                f"{_field_upgrade_deficit(state, weakest)}"
            )
        return (
            f"{len(upgradable)} campi upgradabili e pagabili ma "
            f"nessuna azione (anomalia non classificata)"
        )
    parts = []
    if in_queue:
        parts.append(f"{len(in_queue)} in coda")
    if at_cap:
        parts.append(f"{len(at_cap)} al cap")
    return (
        f"0 campi upgradabili su {len(raw_fields)} dorf1 "
        f"({', '.join(parts) or 'tutti filtrati'})"
    )


# Parole chiave multi-lingua per riconoscere lo storage quando il gid non e' letto
_STORAGE_KEYWORDS: dict[int, tuple[str, ...]] = {
    GID_WAREHOUSE: ("warehouse", "magazzino", "lager", "entrep", "almac", "depo"),
    GID_GRANARY: ("granary", "granaio", "silo", "kornkammer", "grenier", "celeiro", "ambar"),
}


def _storage_needed(state: GameState, threshold: float = 0.90) -> list[int]:
    """Gid degli storage da potenziare perche' quasi pieni (10=mag., 11=granaio)."""
    return builder.storage_needed(state, threshold)


def rule_upgrade_storage(state: GameState, settings: Settings) -> Action | None:
    """Se magazzino/granaio sono quasi pieni, potenzia lo storage del dorf2.

    Ha priorita' sui campi risorsa: con lo storage pieno la produzione
    in eccesso viene sprecata. Cerca lo storage per gid (affidabile,
    indipendente dalla lingua) e ricade sulle parole chiave del nome.

    Supporta la costruzione parallela: se c'e' gia' un campo risorsa
    in corso ma nessun edificio interno, lo storage puo' essere avviato.
    """
    if builder.parallel_build_enabled(settings):
        if (
            len(state.interior_queue()) >= settings.max_interior_queue
            or len(state.build_queue) >= settings.max_build_queue
        ):
            return None
    elif state.build_queue:
        return None

    # Jitter anti-detection: soglia storage campionata per ciclo
    # (uniform 0.82-0.93) invece del 0.90 fisso — niente firma metrica.
    _jit = getattr(settings, "cycle_jitter", None) or {}
    needed = _storage_needed(state, float(_jit.get("storage", 0.90)))
    if not needed:
        return None

    interior = _upgradeable_fields(state, state.interior_fields())
    candidates: list[Field] = []
    for gid in needed:
        by_gid = sorted(
            (f for f in interior if f.gid == gid),
            key=lambda f: (f.level, f.slot_id),
        )
        if by_gid:
            candidates.extend(by_gid)
            continue
        keywords = _STORAGE_KEYWORDS[gid]
        by_name = sorted(
            (f for f in interior if any(k in f.name.lower() for k in keywords)),
            key=lambda f: (f.level, f.slot_id),
        )
        candidates.extend(by_name)

    # Storage del tipo richiesto assente del tutto: nuova costruzione su
    # area vuota (magazzino/granaio non hanno prerequisiti).
    candidates.extend(
        builder.new_building_intents(
            state, needed, {f.slot_id for f in candidates}
        )
    )

    if not candidates:
        logger.info(
            "[%s] Storage al limite ma nessun magazzino/granaio ne' area "
            "vuota trovati nel dorf2.",
            state.village_name,
        )
        return None

    target = candidates[0]
    return Action(
        type=ActionType.UPGRADE_FIELD,
        params={
            "candidates": [f.slot_id for f in candidates],
            "storage": True,
            "construct": builder.construct_gids(state, candidates),
        },
        reason=(
            f"Storage quasi pieno: upgrade di '{builder.field_label(target)}' "
            f"(livello {target.level}, slot {target.slot_id})"
        ),
    )


def _storage_full(state: GameState) -> bool:
    """True se magazzino o granaio sono al 100% (o oltre) della capienza."""
    res = state.resources
    wh_full = (
        res.warehouse_capacity > 0
        and max(res.wood, res.clay, res.iron) >= res.warehouse_capacity
    )
    gr_full = (
        res.granary_capacity > 0
        and res.crop >= res.granary_capacity
    )
    return wh_full or gr_full


def _train_affordable(
    state: GameState,
    priority_units: list[str],
    crop_margin: float = 0.90,
) -> bool:
    """True se il grano disponibile copre almeno 1 unita' autorizzata.

    Con il granaio a secco l'addestramento fallisce a prescindere dalle
    altre risorse (batch sizing scarta ogni unita'): in quel caso conviene
    lasciare il passo allo smaltimento via Mercato. Se le tabelle dei
    costi non sono disponibili si assume l'addestramento possibile.
    """
    try:
        from .actions import STATIC_TROOP_COSTS, UNIT_ALIASES  # import ritardato: no cicli
    except Exception:  # noqa: BLE001
        return True
    if not STATIC_TROOP_COSTS:
        return True

    priorities = [
        p.strip().lower() for p in (priority_units or []) if p and p.strip()
    ]

    def _matches(unit_key: str) -> bool:
        if not priorities:
            return True
        aliases = UNIT_ALIASES.get(unit_key.lower(), set()) | {unit_key.lower()}
        return any(p in alias or alias in p for p in priorities for alias in aliases)

    crop_costs = [
        int(c.get("crop") or 0)
        for key, c in STATIC_TROOP_COSTS.items()
        if _matches(key) and c.get("crop")
    ]
    cheapest = min(crop_costs) if crop_costs else 0
    if cheapest <= 0:
        return True
    return int(state.resources.crop * crop_margin) >= cheapest


def _overflow_surplus(state: GameState, relief_ratio: float = 0.15) -> dict[str, int]:
    """Risorse da smaltire: per ogni tipo a tappo si spedisce il 15% della capienza.

    La UI mostra gli stock cappati alla capienza, quindi l'eccedenza non e'
    misurabile direttamente: si invia una quota di alleggerimento che tiene
    il deposito sotto il 100% per qualche ciclo.
    """
    res = state.resources
    surplus: dict[str, int] = {}
    if res.warehouse_capacity > 0:
        relief = max(1, int(res.warehouse_capacity * relief_ratio))
        for rtype in (ResourceType.WOOD, ResourceType.CLAY, ResourceType.IRON):
            if res.amount(rtype) >= res.warehouse_capacity:
                surplus[rtype.value] = relief
    if res.granary_capacity > 0 and res.crop >= res.granary_capacity:
        surplus[ResourceType.CROP.value] = max(1, int(res.granary_capacity * relief_ratio))
    return surplus


def _find_interior_by_gid(state: GameState, gids: tuple[int, ...]) -> Field | None:
    """Primo edificio interno che corrisponde a uno dei gid forniti."""
    for f in state.interior_fields():
        if f.gid in gids:
            return f
    return None


def _find_all_interiors_by_gid(state: GameState, gids: tuple[int, ...]) -> list[Field]:
    """Tutti gli edifici interni che corrispondono a uno dei gid forniti."""
    return [f for f in state.interior_fields() if f.gid in gids]


def _best_peer_for_surplus(state: GameState, surplus: dict[str, int]) -> str | None:
    """Villaggio con piu' spazio libero per assorbire l'eccesso.

    Punteggio = min(eccesso inviabile, spazio libero stimato del peer)
    calcolato sul tipo di storage pertinente (magazzino vs granaio).

    Regola rigida (smistamento risorse per la fondazione): se il modulo
    espansione ha un ``feed_target`` attivo (villaggio founder in deficit
    coloni) e quel peer ha spazio libero, l'overflow va a lui prima di
    qualunque altro villaggio (es. l'hammer).
    """
    feed = (getattr(state, "feed_target", "") or "").strip().lower()
    best_name: str | None = None
    best_score = 0
    for name, res in state.peer_villages.items():
        if name == state.village_name:
            continue
        score = 0
        for rtype, amount in surplus.items():
            if rtype == ResourceType.CROP.value:
                free = max(0, res.granary_capacity - res.crop)
            else:
                free = max(
                    0,
                    res.warehouse_capacity - max(res.wood, res.clay, res.iron),
                )
            score += min(amount, free)
        # Founder in deficit coloni: priorita' assoluta se puo' assorbire
        # almeno una parte del surplus.
        if feed and name.strip().lower() == feed:
            if score > 0:
                return name
            continue
        if score > best_score:
            best_score = score
            best_name = name
    return best_name


def rule_emergency_overflow(state: GameState, settings: Settings) -> Action | None:
    """Emergenza: storage al 100% e nessun upgrade avviabile.

    Se il magazzino/granaio e' a tappo, ogni risorsa prodotta viene
    sprecata. La regola interviene SOLO quando rule_upgrade_storage non
    puo' agire (coda piena o nessuno storage trovato), provando in ordine:

    1. spesa locale: upgrade interno costoso (smaltimento diretto);
    2. addestramento di una truppa nella Caserma/Scuderia/Officina;
    3. invio dell'eccedenza al villaggio con piu' capienza libera
       (fallback finale, richiede Mercato e snapshot dei peer);
    4. ALERT: notifica il problema senza azioni rischiose.
    """
    if not _storage_full(state):
        return None

    # Se l'upgrade dello storage e' possibile, lo preferiamo sempre
    if rule_upgrade_storage(state, settings) is not None:
        return None

    surplus = _overflow_surplus(state)
    if not surplus:
        return None

    # --- 1) Spesa locale: l'upgrade interno piu' costoso smaltisce subito ---
    if _queue_free(state, settings, resource_field=False):
        spenders = builder.local_spend_intents(state)
        if spenders:
            head = spenders[0]
            return Action(
                type=ActionType.UPGRADE_FIELD,
                params={
                    "candidates": [f.slot_id for f in spenders],
                    "construct": builder.construct_gids(state, spenders),
                },
                reason=(
                    f"Storage pieno: spesa locale su "
                    f"'{builder.field_label(head)}' "
                    f"(livello {head.level}) per smaltire {surplus}"
                ),
            )

    # --- 2) Caserma/Scuderia/Officina: addestra truppe autorizzate ---
    trainers = _find_all_interiors_by_gid(
        state, (GID_BARRACKS, GID_STABLE, GID_WORKSHOP)
    )
    if trainers:
        trainer = trainers[0]
        vcfg = settings.village_config_for(state.village_name, state.village_id)
        priority_units = (
            list(vcfg.priority_units or vcfg.allowed_units)
            if vcfg is not None
            else []
        )
        if _train_affordable(state, priority_units):
            return Action(
                type=ActionType.TRAIN_TROOPS,
                params={
                    "trainer_slot": trainer.slot_id,
                    "trainer_gid": trainer.gid,
                    "trainer_slots": [t.slot_id for t in trainers],
                    "priority_units": priority_units,
                },
                reason=(
                    f"Storage pieno: addestramento in '{builder.field_label(trainer)}' "
                    f"(slot {trainer.slot_id}) per smaltire risorse"
                ),
            )
        logger.info(
            "[%s] Storage pieno ma grano insufficiente per addestrare "
            "(crop=%s): si passa allo smaltimento via Mercato.",
            state.village_name,
            state.resources.crop,
        )

    # --- 3) Mercato: invia l'eccedenza al peer con piu' spazio ---
    # Slot manuale (dashboard) ha priorita' sull'auto-detect gid=17
    vcfg = settings.village_config_for(state.village_name, state.village_id)
    market = _find_interior_by_gid(state, (GID_MARKETPLACE,))
    market_slot = (
        int(vcfg.market_slot or 0)
        if vcfg is not None and vcfg.market_slot
        else (market.slot_id if market is not None else 0)
    )
    target = _best_peer_for_surplus(state, surplus)
    if market_slot and target:
        send_params: dict = {
            "market_slot": market_slot,
            "target_village": target,
            "target": target,
            "resources": surplus,
        }
        target_cfg = settings.village_config_for(target)
        if target_cfg and target_cfg.x is not None and target_cfg.y is not None:
            send_params["target_x"] = target_cfg.x
            send_params["target_y"] = target_cfg.y

        return Action(
            type=ActionType.SEND_RESOURCES,
            params=send_params,
            reason=(
                f"Storage pieno: invio {surplus} a '{target}' "
                f"via Mercato (slot {market_slot})"
            ),
        )

    # --- 4) Nessuna via d'uscita: alert esplicito ---
    missing = "mercato" if market is None else ""
    logger.warning(
        "[%s] EMERGENZA storage al 100%% (eccesso %s), coda occupata e "
        "nessun %s disponibile: produzione sprecata.",
        state.village_name,
        surplus,
        missing or "sfogo",
    )
    return Action(
        type=ActionType.ALERT,
        params={"surplus": surplus, "storage_full": True},
        reason=(
            f"Storage al 100% (eccesso {surplus}): nessuna azione possibile, "
            "produzione in spreco"
        ),
    )


def rule_grain_safety(state: GameState, settings: Settings) -> Action | None:
    """Grain Safety Net: margine netto grano sotto soglia -> solo grano.

    Prima di ogni costruzione si verifica che il bilancio netto
    (produzione - upkeep, letto da production.php) resti sopra la
    soglia configurata; in caso contrario gli unici upgrade ammessi
    sono campi di grano, poi Mulino/Forno.
    """
    if not builder.crop_safety_needed(state, settings):
        return None
    intents = builder.grain_safety_intents(state)
    if not intents:
        return None
    head = intents[0]
    if not _queue_free(state, settings, head.is_resource_field):
        return None
    return Action(
        type=ActionType.UPGRADE_FIELD,
        params={
            "candidates": [f.slot_id for f in intents],
            "construct": builder.construct_gids(state, intents),
        },
        reason=(
            f"Grain safety: margine {builder.net_crop(state)}/h sotto soglia "
            f"{builder.crop_threshold(state, settings):g}/h — upgrade di "
            f"'{builder.field_label(head)}' (livello {head.level})"
        ),
    )


def rule_anti_raid(state: GameState, settings: Settings) -> Action | None:
    """Anti-Raid: Depositi Segreti multipli sotto pressione raid.

    Attiva solo se l'orchestrator ha segnalato
    ``state.under_raid_pressure`` (attacchi attivi, storico recente
    nel registro difesa, o villaggio singolo gia' colpito senza via
    di fuga via mercato). Potenzia i Nascondigli esistenti e ne
    fonda di nuovi finche' la capacita' protetta non copre il target
    di ruolo. L'upkeep del deposito e' 0: resta comunque dietro alla
    Grain Safety Net come tutti gli upgrade non-grano.
    """
    if not getattr(state, "under_raid_pressure", False):
        return None
    if builder.crop_safety_needed(state, settings):
        return None
    intents = builder.anti_raid_intents(state, settings)
    if not intents:
        return None
    head = intents[0]
    if not _queue_free(state, settings, head.is_resource_field):
        return None
    protected = builder.cranny_protected_capacity(state, settings)
    target = builder.cranny_target_protection(state, settings)
    return Action(
        type=ActionType.UPGRADE_FIELD,
        params={
            "candidates": [f.slot_id for f in intents],
            "construct": builder.construct_gids(state, intents),
        },
        reason=(
            f"Anti-Raid: protezione {protected} < target {target} — "
            f"upgrade di '{builder.field_label(head)}' "
            f"(livello {head.level}, slot {head.slot_id})"
        ),
    )


def rule_building_focus(state: GameState, settings: Settings) -> Action | None:
    """Override manuale: building_focus da dashboard batte la logica ruolo."""
    # Grain safety ha la precedenza anche sul focus manuale
    if builder.crop_safety_needed(state, settings):
        return None
    intents = builder.focus_intents(state, settings)
    if not intents:
        return None
    head = intents[0]
    if not _queue_free(state, settings, head.is_resource_field):
        return None
    return Action(
        type=ActionType.UPGRADE_FIELD,
        params={
            "candidates": [f.slot_id for f in intents],
            "construct": builder.construct_gids(state, intents),
        },
        reason=(
            f"Focus manuale: upgrade di '{builder.field_label(head)}' "
            f"(livello {head.level}, slot {head.slot_id})"
        ),
    )


def rule_culture_boost(state: GameState, settings: Settings) -> Action | None:
    """Culture Points Booster: upgrade interni ad alta resa PC.

    Attivo sui villaggi founder con Residenza/Palazzo >= 10 finche'
    i Punti Cultura non bastano per il prossimo slot. Entra in gioco
    dopo il focus manuale e spende solo quando le risorse superano la
    riserva dei 3 coloni (i magazzini possono gia' pagarli).
    """
    if builder.crop_safety_needed(state, settings):
        return None
    if not builder.culture_boost_active(state, settings):
        return None
    if not builder.settler_surplus_ok(state):
        return None
    intents = builder.culture_boost_intents(state)
    if not intents:
        return None
    head = intents[0]
    if not _queue_free(state, settings, head.is_resource_field):
        return None
    return Action(
        type=ActionType.UPGRADE_FIELD,
        params={
            "candidates": [f.slot_id for f in intents],
            "construct": builder.construct_gids(state, intents),
        },
        reason=(
            f"CP Booster: PC {state.culture_points}/"
            f"{state.culture_points_required} — upgrade di "
            f"'{builder.field_label(head)}' "
            f"(livello {head.level})"
        ),
    )


def rule_role_buildings(state: GameState, settings: Settings) -> Action | None:
    """Infrastrutture di ruolo bilanciate (hammer/defensive/croplog/feeder).

    Sviluppo coordinato: tra gli edifici del ruolo si upgrade quello al
    livello piu' basso, cosi' Caserma/Scuderia/Officina avanzano insieme.
    La capitale (auto-rilevata) somma granaio/mulino/forno al profilo.
    """
    if builder.crop_safety_needed(state, settings):
        return None
    intents = builder.role_intents(state, settings)
    if not intents:
        return None
    head = intents[0]
    if not _queue_free(state, settings, head.is_resource_field):
        return None
    role = settings.village_role_for(state.village_name, state.village_id)

    # Fallback automatico: se gli edifici interni del ruolo non sono ancora
    # pagabili (es. carenza di ferro/grano), l'esecutore ripiega sul campo
    # risorsa piu' debole senza sprecare il ciclo. Sotto freeze espansione
    # il fallback sparirebbe comunque nel demote a WAIT: escluso a monte.
    seen_slots = {f.slot_id for f in intents}
    if builder.expansion_freeze(state, settings):
        fallback_fields = []
    else:
        fallback_fields = [
            f for f in builder.economy_intents(state, settings)
            if f.slot_id not in seen_slots
        ]
    all_candidates = [f.slot_id for f in intents] + [
        f.slot_id for f in fallback_fields
    ]

    return Action(
        type=ActionType.UPGRADE_FIELD,
        params={
            "candidates": all_candidates,
            "construct": builder.construct_gids(state, intents),
        },
        reason=(
            f"Sviluppo ruolo '{role}': upgrade di "
            f"'{builder.field_label(head)}' "
            f"(livello {head.level}, slot {head.slot_id})"
        ),
    )


def rule_wait(state: GameState, settings: Settings) -> Action:
    """Fallback: niente da fare in questa iterazione.

    Doppia coda (Romani/Plus): "occupata" solo se entrambe le viste
    sono piene; se una resta libera il motivo lo dichiara — serve a
    distinguere un attesa reale da un candidato mancante/non pagabile.
    """
    if state.build_queue:
        eta = min(
            (item.remaining_seconds for item in state.build_queue),
            default=0,
        )
        can_out, can_in = builder.queue_slots_free(state, settings)
        if not can_out and not can_in:
            reason = (
                f"Coda occupata, prossima costruzione tra ~{eta}s"
            )
        else:
            free_side = "campi (dorf1)" if can_out else "edifici (dorf2)"
            reason = (
                f"Coda {free_side} libera ma nessun upgrade avviabile; "
                f"prossima costruzione tra ~{eta}s"
            )
        return Action(
            type=ActionType.WAIT,
            params={"suggest_seconds": eta},
            reason=reason,
        )
    return Action(type=ActionType.WAIT, reason="Nessuna azione necessaria")


# --------------------------------------------------------------------------
# Implementazioni
# --------------------------------------------------------------------------

class RuleBasedBrain:
    """Albero di decisione: prima regola non-None vince."""

    DEFAULT_RULES: tuple[Rule, ...] = (
        rule_upgrade_storage,
        rule_grain_safety,
        rule_emergency_overflow,
        rule_anti_raid,
        rule_building_focus,
        rule_culture_boost,
        rule_role_buildings,
        rule_upgrade_weakest_field,
        rule_wait,
    )

    def __init__(self, settings: Settings, rules: tuple[Rule, ...] | None = None) -> None:
        self.settings = settings
        self.rules = rules or self.DEFAULT_RULES
        # Decision Trace: ultima radiografia decisionale (letta anche dal
        # dry-run /api/debug/simulate). trace_tag marca le simulazioni.
        self.last_trace: dict = {}
        self.trace_tag: str = ""

    def _rule_skip_note(self, rule: Rule, state: GameState) -> str:
        """Motivo piu' probabile per cui la regola non ha prodotto azioni.

        Euristica per regola sui flag gia' calcolati: basta a spiegare
        il 90% dei 'no_match' senza strumentare ogni funzione.
        """
        name = getattr(rule, "__name__", "")
        try:
            if name == "rule_upgrade_storage":
                if not _queue_free(state, self.settings, resource_field=False):
                    return "coda edifici (dorf2) occupata"
                return "nessuno storage oltre la soglia di riempimento"
            if name == "rule_grain_safety":
                if not builder.crop_safety_needed(state, self.settings):
                    return "margine netto grano sopra soglia (safety inattiva)"
                return "grain safety attiva ma nessun candidato/coda piena"
            if name == "rule_emergency_overflow":
                return "storage non al 100% (o upgrade storage gia' possibile)"
            if name == "rule_anti_raid":
                if not getattr(state, "under_raid_pressure", False):
                    return "nessuna pressione raid sul villaggio"
                return "pressione raid ma nessun intent Nascondigli/coda piena"
            if name == "rule_building_focus":
                why = builder.focus_skip_reasons(state, self.settings)
                if why:
                    return "; ".join(why)
                try:
                    pre = builder.focus_candidates(state, self.settings)
                    post = builder.focus_intents(state, self.settings)
                    if pre and not post:
                        return (
                            f"{len(pre)} candidati scartati: slot coda "
                            f"occupato (dorf1/dorf2 pieni)"
                        )
                    if pre:
                        return "candidati non eseguibili in questo ciclo"
                except Exception:  # noqa: BLE001
                    pass
                return "coda prioritaria vuota o disabilitata"
            if name == "rule_culture_boost":
                if not builder.culture_boost_active(state, self.settings):
                    return "culture boost non attivo (ruolo/CP/residenza)"
                return "riserva 3 coloni non coperta o nessun candidato"
            if name == "rule_role_buildings":
                return "nessun intent di ruolo applicabile o coda piena"
            if name == "rule_upgrade_weakest_field":
                return weakest_field_skip_reason(state, self.settings)
        except Exception:  # noqa: BLE001
            pass
        return "condizioni non soddisfatte"

    def _decision_trace(
        self, state: GameState, evaluated: list[dict], action: Action | None
    ) -> dict:
        """Radiografia completa del ciclo decisionale (spec debug pipeline)."""
        pool: dict = {}
        try:
            pre = builder.focus_candidates(state, self.settings)
            post = builder.focus_intents(state, self.settings)
            pre_slots = {f.slot_id for f in pre}
            post_slots = {f.slot_id for f in post}
            def _fld(f: Field) -> dict:
                return {
                    "slot": f.slot_id,
                    "gid": f.gid,
                    "name": builder.field_label(f),
                    "level": f.level,
                    "dorf": 1 if f.is_resource_field else 2,
                }
            pool = {
                "focus_pre_filter": [_fld(f) for f in pre],
                "focus_post_filter": [_fld(f) for f in post],
                "dropped_by_queue": sorted(pre_slots - post_slots),
                "queued_slot_ids": sorted(
                    builder.queued_slot_ids(state)
                ),
            }
        except Exception:  # noqa: BLE001
            pool = {}
        trace = {
            "village": state.village_name,
            "inputs_snapshot": debug_trace.inputs_snapshot(
                state, self.settings
            ),
            "rules_evaluated": evaluated,
            "candidates_pool": pool,
            "final_action": (
                {
                    "type": action.type.value,
                    "reason": action.reason,
                    "rule": str((action.params or {}).get("rule") or ""),
                }
                if action is not None
                else None
            ),
        }
        if self.trace_tag:
            trace["tag"] = self.trace_tag
        return trace

    def decide(self, state: GameState) -> Action:
        evaluated: list[dict] = []
        for rule in self.rules:
            rule_name = getattr(rule, "__name__", str(rule))
            action = rule(state, self.settings)
            if action is not None:
                # Decision Audit Log: la regola vincente viaggia nei
                # params e viene registrata dall'orchestrator.
                action.params = dict(action.params or {})
                action.params["rule"] = rule_name
                evaluated.append(
                    {
                        "rule": rule_name,
                        "result": "matched",
                        "action": action.type.value,
                        "reason": action.reason,
                    }
                )
                self.last_trace = self._decision_trace(
                    state, evaluated, action
                )
                debug_trace.append_trace(
                    self.settings.account_id,
                    "decision_simulated"
                    if self.trace_tag == "simulate"
                    else "decision",
                    self.last_trace,
                )
                return action
            evaluated.append(
                {
                    "rule": rule_name,
                    "result": "no_match",
                    "reason": self._rule_skip_note(rule, state),
                }
            )
        action = Action(
            type=ActionType.NOOP,
            reason="Nessuna regola ha prodotto azioni",
        )
        self.last_trace = self._decision_trace(state, evaluated, action)
        debug_trace.append_trace(
            self.settings.account_id,
            "decision_simulated" if self.trace_tag == "simulate" else "decision",
            self.last_trace,
        )
        return action


class LLMBrain:
    """Scheletro per un cervello guidato da un LLM.

    Idea d'uso:
    1. `serialize_state(state)` -> dict/JSON compatto del GameState.
    2. Prompt di sistema che elenca le ActionType ammesse e chiede
       una risposta JSON tipo {"type": "upgrade_field", "params": {...}}.
    3. `parse_response()` valida l'output (es. con pydantic) e, se non
       valido, ricade su `fallback` (RuleBasedBrain) per non bloccare il loop.
    """

    def __init__(self, settings: Settings, fallback: DecisionMaker | None = None) -> None:
        self.settings = settings
        self.fallback = fallback or RuleBasedBrain(settings)

    def serialize_state(self, state: GameState) -> dict:
        return {
            "resources": {
                "wood": state.resources.wood,
                "clay": state.resources.clay,
                "iron": state.resources.iron,
                "crop": state.resources.crop,
                "warehouse_capacity": state.resources.warehouse_capacity,
                "granary_capacity": state.resources.granary_capacity,
            },
            "fields": [
                {"slot": f.slot_id, "name": f.name, "level": f.level}
                for f in state.fields
            ],
            "build_queue": [
                {"name": i.name, "remaining_s": i.remaining_seconds}
                for i in state.build_queue
            ],
            "incoming_attacks": state.incoming_attacks,
            "village": state.village_name,
        }

    def decide(self, state: GameState) -> Action:
        # TODO: collegare qui il client LLM preferito (OpenAI, Anthropic,
        # locale...). Fino ad allora deleghiamo al cervello a regole.
        logger.debug("LLMBrain non configurato: uso il fallback a regole")
        return self.fallback.decide(state)
