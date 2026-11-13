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

from .config import Settings
from . import builder
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

def rule_alert_under_attack(state: GameState, settings: Settings) -> Action | None:
    """Attacchi in arrivo: per ora notifica soltanto (evasione manuale)."""
    if state.incoming_attacks > 0:
        return Action(
            type=ActionType.ALERT,
            params={"attacks": state.incoming_attacks},
            reason=f"{state.incoming_attacks} attacchi in arrivo: verifica manualmente",
        )
    return None


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


def _queue_free(state: GameState, settings: Settings, resource_field: bool) -> bool:
    """True se la coda pertinente (campi o interni) puo' accettare un upgrade."""
    if settings.allow_parallel_build:
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

    if settings.allow_parallel_build:
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

    target = candidates[0]
    return Action(
        type=ActionType.UPGRADE_FIELD,
        params={
            "candidates": [f.slot_id for f in candidates],
            "resource": scarcest.value,
        },
        reason=(
            f"Coda libera: upgrade di '{target.name or 'slot ' + str(target.slot_id)}' "
            f"(livello {target.level}) per aumentare {scarcest.value}"
        ),
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
    if settings.allow_parallel_build:
        if (
            len(state.interior_queue()) >= settings.max_interior_queue
            or len(state.build_queue) >= settings.max_build_queue
        ):
            return None
    elif state.build_queue:
        return None

    needed = _storage_needed(state)
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
            f"Storage quasi pieno: upgrade di '{target.name or 'slot ' + str(target.slot_id)}' "
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
    """
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
                    f"'{head.name or 'slot ' + str(head.slot_id)}' "
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
                    f"Storage pieno: addestramento in '{trainer.name or 'slot ' + str(trainer.slot_id)}' "
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
            f"'{head.name or 'slot ' + str(head.slot_id)}' (livello {head.level})"
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
            f"Focus manuale: upgrade di '{head.name or 'slot ' + str(head.slot_id)}' "
            f"(livello {head.level}, slot {head.slot_id})"
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
    return Action(
        type=ActionType.UPGRADE_FIELD,
        params={
            "candidates": [f.slot_id for f in intents],
            "construct": builder.construct_gids(state, intents),
        },
        reason=(
            f"Sviluppo ruolo '{role}': upgrade di "
            f"'{head.name or 'slot ' + str(head.slot_id)}' "
            f"(livello {head.level}, slot {head.slot_id})"
        ),
    )


def rule_wait(state: GameState, settings: Settings) -> Action:
    """Fallback: niente da fare in questa iterazione."""
    if state.build_queue:
        eta = min(
            (item.remaining_seconds for item in state.build_queue),
            default=0,
        )
        return Action(
            type=ActionType.WAIT,
            params={"suggest_seconds": eta},
            reason=f"Coda occupata, prossima costruzione tra ~{eta}s",
        )
    return Action(type=ActionType.WAIT, reason="Nessuna azione necessaria")


# --------------------------------------------------------------------------
# Implementazioni
# --------------------------------------------------------------------------

class RuleBasedBrain:
    """Albero di decisione: prima regola non-None vince."""

    DEFAULT_RULES: tuple[Rule, ...] = (
        rule_alert_under_attack,
        rule_upgrade_storage,
        rule_grain_safety,
        rule_emergency_overflow,
        rule_building_focus,
        rule_role_buildings,
        rule_upgrade_weakest_field,
        rule_wait,
    )

    def __init__(self, settings: Settings, rules: tuple[Rule, ...] | None = None) -> None:
        self.settings = settings
        self.rules = rules or self.DEFAULT_RULES

    def decide(self, state: GameState) -> Action:
        for rule in self.rules:
            action = rule(state, self.settings)
            if action is not None:
                return action
        return Action(type=ActionType.NOOP, reason="Nessuna regola ha prodotto azioni")


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
