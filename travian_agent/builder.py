"""Smart Building & Upgrade Planning.

Generatore dinamico di upgrade: sostituisce la selezione statica con
un'unica funzione `select_upgrade` che applica, in ordine di priorita':

1. **Storage** — magazzino/granaio quasi pieni (overflow prevention);
2. **Grain Safety Net** — margine netto di grano sotto soglia -> campi
   di grano, poi Mulino/Forno;
3. **Bootstrap / Reclaim** — sequenza startup del nuovo villaggio, e
   Accademia + Residenza/Palazzo quando il villaggio e' l'origine di un
   treno di riconquista (``VillageConfig.is_reclaim_source``);
4. **building_focus** — override manuale utente (priorita' sul ruolo);
5. **Ruolo** — hammer/defensive/croplog/feeder: infrastrutture militari
   o economiche bilanciate per livello;
6. **Economia** — campo risorsa piu' debole della risorsa piu' scarsa.

`plan_upgrades` materializza i prossimi N upgrade simulando gli scatti
di livello, per la preview in dashboard (persistita su
``state/build_plan_<account>.json``).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import db
from .building_prereqs import (
    FIELD_GID_TYPES,
    GID_ACADEMY,
    GID_BAKERY,
    GID_CLAY_PIT,
    GID_CRANNY,
    GID_CROPLAND,
    GID_EMBASSY,
    GID_GRAIN_MILL,
    GID_IRON_MINE,
    GID_WOODCUTTER,
    missing_prereqs,
    GID_LABELS,
    GID_MAIN_BUILDING,
    GID_TOWN_HALL,
    WALL_GIDS,
    gids_for_focus_key,
    prereqs_met,
)
from .config import Settings
from .presets import resolve_role
from .models import (
    GID_BARRACKS,
    GID_GRANARY,
    GID_MARKETPLACE,
    GID_PALACE,
    GID_RALLY_POINT,
    GID_RESIDENCE,
    GID_STABLE,
    GID_WAREHOUSE,
    GID_WORKSHOP,
    BuildQueueItem,
    Field,
    GameState,
    PriorityQueueItem,
    ResourceType,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state"

# Livelli massimi: campi 10 (21 in capitale), edifici interni default 20.
_FIELD_MAX_NORMAL = 10
_FIELD_MAX_CAPITAL = 21
_BUILDING_MAX_LEVEL = 20
# Soglia "quasi pieno" per l'upgrade preventivo dello storage.
_STORAGE_WARN_RATIO = 0.90
# Profondita' default della preview piano (3-5 per spec).
PLAN_DEPTH = 4

# Anti-Raid (Depositi Segreti multipli): capacita' protetta per
# livello del Deposito Segreto (gid 23), tabella T4.x — raddoppiata per
# i Galli; gli attaccanti teutoni forano il 20% del deposito ->
# fattore prudenziale 0.8 sulla capacita' effettiva.
_CRANNY_CAP_BY_LEVEL = (0, 100, 130, 170, 220, 280, 360, 460, 600, 770, 1000)
_CRANNY_MAX_LEVEL = 10
_CRANNY_TEUTON_LOOT_FACTOR = 0.8
# Aree vuote minime da conservare dopo la fondazione (Magazzino,
# Granaio, Mercato, Reggia e Accademia devono restare piazzabili).
_CRANNY_MIN_FREE_SLOTS = 2
# Soglie di protezione per singola risorsa sotto raid.
_CRANNY_TARGET_FOUNDER = 3500   # early_cropper_founder (3-4 depositi)
_CRANNY_TARGET_DEFAULT = 2000   # altri ruoli (2 depositi al 10)

# New Village Bootstrap (v2.0, spec 2): requisiti minimi del villaggio
# appena fondato prima di passare al ruolo assegnato.
BOOTSTRAP_STORAGE_LEVEL = 5   # magazzino + granaio >= 5
BOOTSTRAP_MAIN_LEVEL = 5      # Edificio Principale >= 5
BOOTSTRAP_FIELD_LEVEL = 5     # tutti i campi risorsa >= 5 (spec biforcazione)

# Ruolo -> gid interni sviluppati in modo bilanciato (livello minimo prima).
_ROLE_INTERIORS: dict[str, tuple[int, ...]] = {
    "hammer": (GID_BARRACKS, GID_STABLE, GID_WORKSHOP),
    "defensive": (GID_BARRACKS, GID_RESIDENCE) + WALL_GIDS,
    # Sicurezza passiva (cranny) e presidio militare (caserma) sono
    # parte del bilanciamento: il villaggio non resta scoperto ai raid
    # notturni mentre sviluppa campi/strutture.
    "croplog": (
        GID_GRANARY, GID_WAREHOUSE, GID_GRAIN_MILL, GID_BAKERY,
        GID_CRANNY, GID_EMBASSY, GID_PALACE, GID_BARRACKS,
    ) + WALL_GIDS,
    "feeder": (
        GID_MARKETPLACE, GID_WAREHOUSE, GID_GRANARY, GID_CRANNY,
        GID_BARRACKS,
    ) + WALL_GIDS,
    "chiefing": (GID_ACADEMY, GID_RESIDENCE, GID_TOWN_HALL),
    "early_cropper_founder": (
        GID_RESIDENCE, GID_EMBASSY, GID_MARKETPLACE, GID_ACADEMY,
        GID_TOWN_HALL, GID_BARRACKS,
    ),
}

# Culture Points Booster (spec): edifici interni con il miglior
# rapporto costo / PC giornalieri, nell'ordine di efficienza.
_CP_BOOST_GIDS: tuple[int, ...] = (
    GID_EMBASSY,
    GID_ACADEMY,
    GID_MARKETPLACE,
    GID_TOWN_HALL,
    GID_MAIN_BUILDING,
)
FOUNDER_ROLE = "early_cropper_founder"
# Residenza/Palazzo >= 10 = booster attivo sul villaggio fondatore
_CP_BOOST_RESIDENCE_LEVEL = 10


@dataclass
class UpgradeIntent:
    """Prossimo upgrade proposto dal planner dinamico."""

    kind: str            # storage|grain_safety|bootstrap|focus|role|economy
    slot_id: int
    name: str
    gid: int             # -1 per campi risorsa
    target_level: int
    is_resource_field: bool
    reason: str
    candidates: list[int]  # slot alternativi dello stesso intento
    # slot_id -> gid per le NUOVE costruzioni su area vuota (gid 0):
    # l'esecutore usa dorf2.php?a=<gid>&id=<slot> per il contratto.
    construct: dict[int, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "slot_id": self.slot_id,
            "name": self.name,
            "gid": self.gid,
            "target_level": self.target_level,
            "is_resource_field": self.is_resource_field,
            "reason": self.reason,
            "construct": self.construct,
        }


# ---------------------------------------------------------------------------
# Primitive di selezione
# ---------------------------------------------------------------------------

def is_capital(state: GameState) -> bool:
    """True se nel dorf2 esiste il Palazzo (presente solo in capitale)."""
    return any(f.gid == GID_PALACE for f in state.interior_fields())


def field_max_level(state: GameState, f: Field) -> int:
    """Livello massimo raggiungibile dal field (stima conservativa)."""
    if f.is_resource_field:
        return _FIELD_MAX_CAPITAL if is_capital(state) else _FIELD_MAX_NORMAL
    return _BUILDING_MAX_LEVEL


def field_label(f: Field) -> str:
    """Etichetta leggibile dello slot per log/audit/dashboard.

    I sensori possono riportare ``name`` vuoto o numerico (id/indice
    DOM): in quel caso la label si risolve dal dizionario ``GID_LABELS``
    via ``gid``, con fallback sul generico "slot N".
    """
    name = str(f.name or "").strip()
    if name and not name.isdigit():
        return name
    if f.gid > 0:
        label = GID_LABELS.get(f.gid)
        if label:
            return label
    return name or f"slot {f.slot_id}"


def queued_slot_ids(state: GameState) -> set[int]:
    """Slot gia' occupati da una costruzione in coda."""
    return {q.slot_id for q in state.build_queue if q.slot_id > 0}


def parallel_build_enabled(settings: Settings) -> bool:
    """Costruzione parallela dorf1+dorf2 attiva.

    Priorita' al flag ``allow_parallel_build`` (Travian Plus, True su
    tutti gli account): la doppia coda e' abilitata a prescindere dalla
    tribu', anche se ``settings.tribe`` e' vuoto. Solo con il flag
    spento vale il perk razziale romano (doppia coda senza Plus).
    """
    if bool(getattr(settings, "allow_parallel_build", True)):
        return True
    return (
        str(getattr(settings, "tribe", "") or "").strip().lower()
        == "romans"
    )


def _queue_item_is_outer(item: BuildQueueItem) -> bool:
    """True se l'item in coda occupa il binario campi (dorf1).

    Fonte autorevole: il gid estratto dal parser (1-4 = campi risorsa,
    >=5 = edifici dorf2). Senza gid si ripiega sul flag
    ``is_resource_field``. Mai dedurre il binario dalla posizione
    dell'item nell'array (items[0]/items[1]).
    """
    gid = int(getattr(item, "gid", 0) or 0)
    if gid > 0:
        return gid <= 4
    return bool(getattr(item, "is_resource_field", False))


def queue_slots_free(
    state: GameState, settings: Settings
) -> tuple[bool, bool]:
    """(can_build_outer, can_build_inner): slot coda liberi per vista.

    Con costruzione parallela (tribu' romana / Plus,
    ``allow_parallel_build``) i due binari dorf1/dorf2 condividono il
    tetto ``max_parallel`` (3 con Plus — flag ``allow_parallel_build`` —
    2 altrimenti, perk razziale romano). I conteggi outer/inner si
    calcolano sul gid degli item in coda, non sulla loro posizione:
    con un campo in coda (outer_count=1, inner_count=0, total=1)
    ``inner_free`` resta True e la Coda Prioritaria dorf2 parte.
    Senza parallelismo condividono un unico slot coda.
    """
    total = len(state.build_queue)
    max_q = max(1, int(getattr(settings, "max_build_queue", 1) or 1))
    if parallel_build_enabled(settings):
        has_plus = bool(getattr(settings, "allow_parallel_build", False))
        max_parallel = 3 if has_plus else 2
        outer_count = sum(
            1 for q in state.build_queue if _queue_item_is_outer(q)
        )
        inner_count = total - outer_count
        outer_free = (
            total < max_parallel and outer_count < max_parallel
        )
        inner_free = (
            total < max_parallel and inner_count < max_parallel
        )
        return outer_free, inner_free
    free = total < max_q
    return free, free


# Costi base upgrade campo (livello 1 -> 2) per gid risorsa T4.x; la
# crescita e' esponenziale ~1.67/livello con arrotondamento a multipli
# di 5. Stima +-5%: serve a diagnostica e filtro economico — il costo
# reale resta quello letto dall'esecutore su build.php.
_FIELD_COST_BASE: dict[int, tuple[int, int, int, int]] = {
    GID_WOODCUTTER: (40, 10, 5, 6),
    GID_CLAY_PIT: (80, 40, 80, 50),
    GID_IRON_MINE: (100, 80, 30, 60),
    GID_CROPLAND: (70, 90, 70, 20),
}
_FIELD_COST_GROWTH = 1.67

_RES_TYPE_GID: dict[str, int] = {
    rt.value: gid for gid, rt in FIELD_GID_TYPES.items()
}


def field_gid(f: Field) -> int:
    """gid 1-4 di un campo risorsa (dal gid letto o dal resource_type)."""
    gid = int(getattr(f, "gid", 0) or 0)
    if gid > 0:
        return gid
    rtype = str(
        getattr(getattr(f, "resource_type", None), "value", "") or ""
    )
    return _RES_TYPE_GID.get(rtype, 0)


def field_upgrade_cost(
    gid: int, target_level: int
) -> tuple[int, int, int, int] | None:
    """Costo stimato (legno, argilla, ferro, grano) upgrade a target_level."""
    base = _FIELD_COST_BASE.get(gid)
    if base is None or target_level < 2:
        return None
    mult = _FIELD_COST_GROWTH ** (target_level - 2)
    return tuple(
        max(1, int(round(c * mult / 5.0) * 5)) for c in base
    )


def upgradeable_fields(
    state: GameState, fields: list[Field]
) -> list[Field]:
    """Field upgradabili: non in coda e sotto il livello massimo."""
    queued = queued_slot_ids(state)
    return [
        f for f in fields
        if f.slot_id not in queued and f.level < field_max_level(state, f)
    ]


def storage_needed(
    state: GameState, threshold: float = _STORAGE_WARN_RATIO
) -> list[int]:
    """Gid degli storage da potenziare perche' quasi pieni.

    Uno storage gia' in coda di costruzione non e' piu' 'needed': il
    blocco "Storage al limite" resta silente finche' il lavoro in corso
    non e' completato, lasciando il passo alla Coda Prioritaria invece
    di proporre un secondo upgrade gemello.
    """
    res = state.resources
    needed: list[int] = []
    if res.warehouse_capacity > 0 and (
        max(res.wood, res.clay, res.iron) >= res.warehouse_capacity * threshold
    ):
        needed.append(GID_WAREHOUSE)
    if res.granary_capacity > 0 and res.crop >= res.granary_capacity * threshold:
        needed.append(GID_GRANARY)
    if needed:
        # Upgrade in corso riconosciuto per gid esplicito dell'item,
        # altrimenti dallo slot della coda risalendo al gid del field.
        queued_gids = {q.gid for q in state.build_queue if q.gid > 0}
        slot_gid = {f.slot_id: f.gid for f in state.fields}
        queued_gids.update(
            slot_gid.get(q.slot_id, 0)
            for q in state.build_queue
            if q.slot_id > 0
        )
        needed = [g for g in needed if g not in queued_gids]
    return needed


def crop_threshold(state: GameState, settings: Settings) -> float:
    """Soglia margine netto grano (unita'/ora): per-villaggio > globale."""
    vcfg = settings.village_config_for(state.village_name, state.village_id)
    if vcfg is not None and vcfg.min_net_crop_threshold > 0:
        return float(vcfg.min_net_crop_threshold)
    return float(getattr(settings, "min_net_crop_threshold", 10.0))


def net_crop(state: GameState) -> int | None:
    """Margine netto di grano (produzione - upkeep) letto da production.php.

    Ritorna None quando il bilancio non e' stato letto: la produzione
    lorda NON viene mai usata come approssimazione del netto, perche'
    ignora l'upkeep delle truppe e falserebbe il controllo di sicurezza.
    """
    return state.resources.crop_balance


def crop_safety_needed(state: GameState, settings: Settings) -> bool:
    """True se il margine netto di grano e' sotto la soglia configurata."""
    net = net_crop(state)
    return net is not None and net < crop_threshold(state, settings)


# ---------------------------------------------------------------------------
# Intenti per categoria (liste ordinate di field candidati)
# ---------------------------------------------------------------------------

def _interior_intents(state: GameState, gids: tuple[int, ...]) -> list[Field]:
    """Edifici interni upgradabili coi gid dati, dal livello piu' basso."""
    cand = [
        f for f in upgradeable_fields(state, state.interior_fields())
        if f.gid in gids and f.gid > 0
    ]
    return sorted(cand, key=lambda f: (f.level, f.slot_id))


def empty_slots(state: GameState) -> list[Field]:
    """Aree vuote del dorf2 (gid 0) non occupate da lavori in coda."""
    queued = queued_slot_ids(state)
    return sorted(
        (
            f for f in state.interior_fields()
            if f.is_empty_site and f.slot_id not in queued
        ),
        key=lambda f: f.slot_id,
    )


def new_building_intents(
    state: GameState,
    gids: tuple[int, ...] | list[int],
    extra_occupied: set[int] | None = None,
) -> list[Field]:
    """Pseudo-field per edifici assenti costruibili su aree vuote.

    Un gid e' candidabile solo se non esiste gia' nel villaggio e i
    prerequisiti del catalogo T4.x sono soddisfatti (es. Forno richiede
    Mulino 5 e campi grano 10; Grande Caserma solo in capitale).
    Il Field restituito ha lo slot dell'area vuota, level=0 e il gid
    dell'edificio da costruire: `construct_gids` lo traduce per
    l'esecutore (dorf2.php?a=<gid>&id=<slot>).
    """
    existing = {f.gid for f in state.interior_fields()}
    empties = empty_slots(state)
    used: set[int] = set(extra_occupied or set())
    out: list[Field] = []
    for gid in gids:
        if gid in existing or gid <= 0:
            continue
        # Mutua esclusione amministrativa: niente Palazzo dove esiste
        # gia' la Residenza (e viceversa) — il gioco rifiuterebbe la
        # costruzione generando un loop di errori dell'esecutore.
        if any(
            gid in group
            and any(o != gid and o in existing for o in group)
            for group in _MUTUALLY_EXCLUSIVE_GIDS
        ):
            continue
        ok, _why = prereqs_met(state.fields, gid, is_capital(state))
        if not ok:
            logger.debug(
                "[%s] Nuovo edificio gid %d bloccato: %s",
                state.village_name, gid, _why,
            )
            continue
        slot = next((s for s in empties if s.slot_id not in used), None)
        if slot is None:
            break
        used.add(slot.slot_id)
        out.append(
            Field(
                slot_id=slot.slot_id,
                name=GID_LABELS.get(gid, slot.name or f"slot {slot.slot_id}"),
                level=0,
                resource_type=None,
                gid=gid,
            )
        )
    return out


def construct_gids(state: GameState, fields: list[Field]) -> dict[int, int]:
    """Mappa slot_id -> gid per i candidati che sono nuove costruzioni (level 0).

    Riconosce i pseudo-field di nuove costruzioni su slot_id dove l'edificio
    non esiste ancora o ha level 0.
    """
    real = {f.slot_id: f for f in state.interior_fields()}
    return {
        f.slot_id: f.gid
        for f in fields
        if not f.is_resource_field
        and f.gid > 0
        and (
            f.level == 0
            or (real.get(f.slot_id) is not None and real[f.slot_id].is_empty_site)
        )
    }


def grain_safety_intents(state: GameState) -> list[Field]:
    """Campi di grano piu' bassi, poi Mulino/Forno esistenti."""
    crop_fields = sorted(
        (
            f for f in upgradeable_fields(state, state.resource_fields())
            if f.resource_type is ResourceType.CROP
        ),
        key=lambda f: (f.level, f.slot_id),
    )
    boosters = _interior_intents(state, (GID_GRAIN_MILL, GID_BAKERY))
    # Mulino/Forno mancanti: costruzione su area vuota se i prerequisiti
    # reggono (Forno: Mulino 5 + campo grano 10).
    boosters += new_building_intents(state, (GID_GRAIN_MILL, GID_BAKERY))
    return crop_fields + boosters


def _focus_steps(focus_list: list) -> list[tuple[tuple[int, ...], int]]:
    """Normalizza building_queue_plan in step ordinati (gruppo gid, target).

    Ogni elemento puo' essere:
    - una stringa chiave legacy (es. "barracks", "cropland");
    - un dict {"key": str, "target_level": int, "priority": int} oppure
      {"gid": int, "target_level": int} (Advanced Queue & Priority).

    Gli step dict vengono ordinati per 'priority' crescente (default =
    posizione nella lista); le chiavi legacy mantengono l'ordine dato.
    target_level 0/assente = fino al massimo consentito (comportamento
    legacy).

    Le chiavi che mappano a piu' gid (es. "city_wall" -> le tre mura
    tribali, "residence_palace" -> Residenza/Palazzo) producono UN
    solo step-gruppo: i gid sono ALTERNATIVE, non requisiti multipli.
    """
    indexed: list[tuple[int, object]] = []
    for pos, item in enumerate(focus_list or []):
        prio = pos
        if isinstance(item, dict):
            try:
                prio = int(item.get("priority") or (pos + 1)) - 1
            except (TypeError, ValueError):
                prio = pos
        indexed.append((prio, item))
    indexed.sort(key=lambda t: t[0])

    steps: list[tuple[tuple[int, ...], int]] = []
    for _prio, item in indexed:
        target = 0
        if isinstance(item, dict):
            try:
                # Compatibilita' nuova UI: 'target' (nuovo) con fallback
                # su 'target_level' (legacy); 0 = fino al massimo.
                target = max(
                    0,
                    int(item.get("target") or item.get("target_level") or 0),
                )
            except (TypeError, ValueError):
                target = 0
            try:
                gid = int(item.get("gid") or 0)
            except (TypeError, ValueError):
                gid = 0
            gids = [gid] if gid > 0 else gids_for_focus_key(
                str(item.get("key") or "")
            )
        else:
            gids = gids_for_focus_key(str(item))
        group = tuple(g for g in gids if g > 0)
        if group:
            steps.append((group, target))
    return steps


BUILD_SLOTS_DORF2: list[int] = list(range(19, 39))  # 19..38 inclusi
MULTI_INSTANCE_GIDS: set[int] = {GID_WAREHOUSE, GID_GRANARY, GID_CRANNY, 36, 38, 39}


def building_category(gid: int) -> int:
    """Categoria tab su build.php per la costruzione iniziale di un edificio.
    1 = Infrastrutture
    2 = Militari
    3 = Risorse
    """
    if gid in (13, 14, 16, 19, 20, 21, 22, 29, 30, 31, 32, 33, 36, 41):
        return 2  # Militari
    if gid in (5, 6, 7, 8, 9, 42):
        return 3  # Risorse
    return 1  # Infrastrutture


def _focus_items(focus_list: list) -> list[PriorityQueueItem]:
    """Normalizza building_queue_plan in lista di PriorityQueueItem ordinata per priorita'."""
    indexed: list[tuple[int, PriorityQueueItem]] = []
    for pos, item in enumerate(focus_list or []):
        it = (
            PriorityQueueItem.from_dict(item)
            if not isinstance(item, PriorityQueueItem)
            else item
        )
        prio = pos
        if it.priority:
            prio = it.priority - 1
        indexed.append((prio, it))
    indexed.sort(key=lambda t: t[0])
    return [it for _prio, it in indexed]


def resolve_target_slot(
    item: PriorityQueueItem | dict,
    state_or_snapshot: GameState | dict,
    reserved_slots: set[int] | None = None,
) -> tuple[int | None, int, str]:
    """Risoluzione deterministica dello slot per un item della Coda Prioritaria.

    Ritorna: (slot_id, current_level, status)
    status: 'OK', 'INVALID_SLOT_OCCUPIED', 'TARGET_REACHED', 'NO_FREE_SLOTS', 'INVALID_ITEM'
    """
    item_obj = (
        PriorityQueueItem.from_dict(item)
        if not isinstance(item, PriorityQueueItem)
        else item
    )
    reserved = reserved_slots if reserved_slots is not None else set()

    # Estrai gids dallo step (key o gid diretto)
    if item_obj.gid > 0:
        gids = (item_obj.gid,)
    elif item_obj.key:
        gids = tuple(gids_for_focus_key(item_obj.key))
    else:
        return None, 0, "INVALID_ITEM"

    if not gids:
        return None, 0, "INVALID_ITEM"

    # Campi risorsa dorf1 (slot 1-18)
    if len(gids) == 1 and gids[0] in FIELD_GID_TYPES:
        rtype = FIELD_GID_TYPES[gids[0]]
        if isinstance(state_or_snapshot, GameState):
            res_fields = [
                f for f in state_or_snapshot.resource_fields()
                if f.resource_type is rtype
            ]
        else:
            slots = (
                state_or_snapshot.get("slots")
                or state_or_snapshot.get("fields")
                or []
            )
            res_fields = [
                Field(
                    slot_id=s["slot"],
                    level=s.get("level", 0),
                    resource_type=rtype,
                )
                for s in slots
                if 1 <= s.get("slot", 0) <= 18 and s.get("gid") == gids[0]
            ]
        if item_obj.slot_id is not None and 1 <= item_obj.slot_id <= 18:
            f = next((x for x in res_fields if x.slot_id == item_obj.slot_id), None)
            if f is not None:
                if item_obj.target_level > 0 and f.level >= item_obj.target_level:
                    return f.slot_id, f.level, "TARGET_REACHED"
                if f.slot_id in reserved:
                    return None, f.level, "INVALID_SLOT_OCCUPIED"
                reserved.add(f.slot_id)
                return f.slot_id, f.level, "OK"
            return None, 0, "INVALID_SLOT_OCCUPIED"

        # Auto dorf1: campo piu' basso non al target e non riservato
        cands = [
            f for f in res_fields
            if f.slot_id not in reserved
            and (item_obj.target_level <= 0 or f.level < item_obj.target_level)
        ]
        if not cands:
            return None, 0, "TARGET_REACHED"
        best = min(cands, key=lambda x: (x.level, x.slot_id))
        reserved.add(best.slot_id)
        return best.slot_id, best.level, "OK"

    # Edifici dorf2 (slot 19-40)
    if isinstance(state_or_snapshot, GameState):
        interiors = state_or_snapshot.interior_fields()
        queued = queued_slot_ids(state_or_snapshot)
    else:
        slots = state_or_snapshot.get("slots") or []
        interiors = [
            Field(
                slot_id=s["slot"],
                gid=s.get("gid", 0),
                level=s.get("level", 0),
                name=s.get("name", ""),
            )
            for s in slots
            if s.get("slot", 0) >= 19
        ]
        queued = {
            q.get("slot_id")
            for q in state_or_snapshot.get("building_queue", [])
            if q.get("slot_id")
        }

    # Slot fissi speciali: Punto di Raduno (39) e Mura (40)
    if any(g == GID_RALLY_POINT for g in gids):
        f39 = next((f for f in interiors if f.slot_id == 39), None)
        cur_lvl = f39.level if f39 and f39.gid == GID_RALLY_POINT else 0
        if item_obj.target_level > 0 and cur_lvl >= item_obj.target_level:
            return 39, cur_lvl, "TARGET_REACHED"
        if 39 in reserved:
            return None, cur_lvl, "INVALID_SLOT_OCCUPIED"
        reserved.add(39)
        return 39, cur_lvl, "OK"

    if any(g in WALL_GIDS for g in gids):
        f40 = next((f for f in interiors if f.slot_id == 40), None)
        cur_lvl = f40.level if f40 and f40.gid in WALL_GIDS else 0
        if item_obj.target_level > 0 and cur_lvl >= item_obj.target_level:
            return 40, cur_lvl, "TARGET_REACHED"
        if 40 in reserved:
            return None, cur_lvl, "INVALID_SLOT_OCCUPIED"
        reserved.add(40)
        return 40, cur_lvl, "OK"

    # Edifici normali dorf2: BUILD_SLOTS_DORF2 [19..38]
    existing = [f for f in interiors if f.gid in gids and f.gid > 0]

    # --- Caso 1: Edificio gia' esistente nel villaggio ---
    if item_obj.slot_id is not None and item_obj.slot_id > 0:
        target_f = next((f for f in interiors if f.slot_id == item_obj.slot_id), None)
        if target_f is not None and target_f.gid in gids:
            if item_obj.target_level > 0 and target_f.level >= item_obj.target_level:
                return item_obj.slot_id, target_f.level, "TARGET_REACHED"
            if item_obj.slot_id in reserved:
                return None, target_f.level, "INVALID_SLOT_OCCUPIED"
            reserved.add(item_obj.slot_id)
            return item_obj.slot_id, target_f.level, "OK"
        elif target_f is not None and (target_f.gid <= 0 or target_f.is_empty_site):
            if item_obj.slot_id not in BUILD_SLOTS_DORF2 or item_obj.slot_id in reserved:
                return None, 0, "INVALID_SLOT_OCCUPIED"
            reserved.add(item_obj.slot_id)
            return item_obj.slot_id, 0, "OK"
        else:
            return None, 0, "INVALID_SLOT_OCCUPIED"

    # item.slot_id e' null (auto)
    under_target = [
        f for f in existing
        if f.slot_id not in reserved
        and (item_obj.target_level <= 0 or f.level < item_obj.target_level)
    ]
    if under_target:
        best = min(under_target, key=lambda x: (x.level, x.slot_id))
        reserved.add(best.slot_id)
        return best.slot_id, best.level, "OK"

    if existing:
        is_multi = (
            any(g in MULTI_INSTANCE_GIDS for g in gids)
            or item_obj.allow_duplicate_building
        )
        if not is_multi:
            top = max(existing, key=lambda x: x.level)
            return top.slot_id, top.level, "TARGET_REACHED"

    # --- Caso 2: Edificio non ancora costruito (level == 0) o nuova istanza multi-building ---
    if item_obj.slot_id is not None and item_obj.slot_id > 0:
        if item_obj.slot_id not in BUILD_SLOTS_DORF2 or item_obj.slot_id in reserved:
            return None, 0, "INVALID_SLOT_OCCUPIED"
        target_f = next((f for f in interiors if f.slot_id == item_obj.slot_id), None)
        if target_f is None or not target_f.is_empty_site:
            return None, 0, "INVALID_SLOT_OCCUPIED"
        reserved.add(item_obj.slot_id)
        return item_obj.slot_id, 0, "OK"

    # auto slot: primo slot in BUILD_SLOTS_DORF2 vuoto e non in reserved/queued
    for s in BUILD_SLOTS_DORF2:
        if s in reserved or s in queued:
            continue
        f = next((x for x in interiors if x.slot_id == s), None)
        if f is None or not f.is_empty_site:
            continue
        reserved.add(s)
        return s, 0, "OK"

    return None, 0, "NO_FREE_SLOTS"


def _detect_tribe(state: GameState) -> str:
    """Deduce la tribu' del villaggio ('romans'|'teutons'|'gauls'|'').

    Fonte: icone unita' lette dai sensori in troops_home — chiavi
    'u1'..'u30' (o 't1'..'t30'): 1-10 Romani, 11-20 Teutoni, 21-30 Galli.
    """
    for key in (state.troops_home or {}):
        k = str(key).strip().lower()
        digits = k.lstrip("ut")
        if not digits.isdigit() or not k or k[0] not in "ut":
            continue
        n = int(digits)
        if 1 <= n <= 10:
            return "romans"
        if 11 <= n <= 20:
            return "teutons"
        if 21 <= n <= 30:
            return "gauls"
    return ""


# Mura costruibili per tribu' (la chiave 'city_wall' mappa al gruppo).
_TRIBE_WALL_GID: dict[str, int] = {
    "romans": WALL_GIDS[0],   # mura cittadine
    "teutons": WALL_GIDS[1],  # terrapieno
    "gauls": WALL_GIDS[2],    # staccionata
}


def _pick_group_gid(
    state: GameState, gids: tuple[int, ...], role: str = ""
) -> int:
    """Sceglie il gid da costruire dentro un gruppo di alternative.

    - Mura: dipende dalla tribu' rilevata; se sconosciuta si usa il gid
      romano come default (caso piu' comune) — il contratto di
      costruzione fallira' comunque apertamente se la tribu' non lo
      consente, evitando però di provare tutti i gid a caso.
    - Residenza/Palazzo: quando NESSUNO dei due esiste ancora la scelta
      segue il ruolo del villaggio — capitale rilevata o ruolo
      ``croplog`` -> Palazzo (GID 26), qualsiasi altro ruolo ->
      Residenza (GID 25). Se uno dei due e' gia' presente il chiamante
      non arriva qui (lo step fa match sull'esistente) e la mutua
      esclusione blocca la costruzione dell'altro.
    - Default: primo gid del gruppo i cui prerequisiti reggono.
    """
    if len(gids) == 1:
        return gids[0]
    if set(gids) == set(WALL_GIDS):
        tribe = _detect_tribe(state)
        return _TRIBE_WALL_GID.get(tribe, WALL_GIDS[0])
    if set(gids) == {GID_RESIDENCE, GID_PALACE}:
        if role == "croplog" or is_capital(state):
            return GID_PALACE
        return GID_RESIDENCE
    for gid in gids:
        ok, _why = prereqs_met(state.fields, gid, is_capital(state))
        if ok:
            return gid
    return gids[0]


def _prereq_intent(
    state: GameState, gid: int, slot_id: int
) -> Field | None:
    """Candidato prerequisito per uno step della Coda Prioritaria.

    Quando la nuova costruzione `gid` e' bloccata dai requisiti (es.
    Palazzo senza Ambasciata liv. 1) lo step non viene scartato: il
    primo requisito interno mancante diventa il candidato — upgrade
    dell'edificio esistente se e' solo sotto livello, altrimenti nuova
    costruzione sull'area `slot_id` riservata allo step quando anche i
    SUOI prerequisiti reggono. I requisiti a campo risorsa non sono
    auto-risolvibili su un'area dorf2 e restano bloccanti.
    """
    cap_ok, missing = missing_prereqs(state.fields, gid, is_capital(state))
    if not cap_ok or not missing:
        return None
    queued = queued_slot_ids(state)
    interiors = state.interior_fields()
    existing = {f.gid for f in interiors}
    for req_gid, req_level in missing:
        if req_gid in FIELD_GID_TYPES:
            continue  # campo risorsa: niente auto-costruzione in dorf2
        built = next((f for f in interiors if f.gid == req_gid), None)
        if built is not None:
            # Esiste ma e' sotto il livello richiesto: upgrade del
            # prerequisito al posto della nuova costruzione.
            if (
                built.level < req_level
                and built.slot_id not in queued
                and built.level < field_max_level(state, built)
            ):
                return built
            continue
        # Assente: costruibile sull'area dello step solo se anche i
        # suoi prerequisiti reggono (catena risolta un livello alla
        # volta, es. Palazzo -> Ambasciata -> Centro Villaggio).
        ok, _why = prereqs_met(state.fields, req_gid, is_capital(state))
        if not ok:
            continue
        if any(
            req_gid in group
            and any(o != req_gid and o in existing for o in group)
            for group in _MUTUALLY_EXCLUSIVE_GIDS
        ):
            continue
        return Field(
            slot_id=slot_id,
            name=GID_LABELS.get(req_gid, f"slot {slot_id}"),
            level=0,
            resource_type=None,
            gid=req_gid,
        )
    return None


def focus_candidates(state: GameState, settings: Settings) -> list[Field]:
    """Pool candidati della Coda Prioritaria PRIMA del filtro slot-coda.

    Stessa logica di ``focus_intents`` ma senza il vincolo
    ``queue_slots_free``: il Decision Tracer la usa per mostrare quali
    candidati vengono poi scartati perche' il lato coda pertinente e'
    occupato.
    """
    vcfg = settings.village_config_for(state.village_name, state.village_id)
    plan = []
    if vcfg is not None and getattr(vcfg, "building_focus_enabled", True):
        plan = list(vcfg.building_queue_plan or vcfg.building_focus or [])
    if not plan:
        return []
    queued = queued_slot_ids(state)
    reserved_slots: set[int] = set()
    stuck = focus_stuck_slots(state)
    out: list[Field] = []
    items = _focus_items(plan)
    for it in items:
        slot_id, cur_lvl, status = resolve_target_slot(it, state, reserved_slots)
        if status in (
            "TARGET_REACHED",
            "INVALID_SLOT_OCCUPIED",
            "NO_FREE_SLOTS",
            "INVALID_ITEM",
        ):
            continue
        if slot_id is None:
            continue
        if slot_id in stuck:
            # Circuit-breaker: step gia' "eseguito" 2+ volte senza che
            # il livello sia cresciuto — passa allo step successivo.
            logger.warning(
                "[%s] Step Coda Prioritaria su slot %d marcato STUCK "
                "(EXECUTED senza progresso): saltato.",
                state.village_name,
                slot_id,
            )
            continue
        gids = (it.gid,) if it.gid > 0 else tuple(gids_for_focus_key(it.key))
        if len(gids) == 1 and gids[0] in FIELD_GID_TYPES:
            rtype = FIELD_GID_TYPES[gids[0]]
            if it.slot_id is not None and 1 <= it.slot_id <= 18:
                matches = [
                    f
                    for f in upgradeable_fields(state, state.resource_fields())
                    if f.slot_id == slot_id
                    and (it.target_level <= 0 or f.level < it.target_level)
                ]
            else:
                matches = [
                    f
                    for f in upgradeable_fields(state, state.resource_fields())
                    if f.resource_type is rtype
                    and (it.target_level <= 0 or f.level < it.target_level)
                ]
            out.extend(sorted(matches, key=lambda f: (f.level, f.slot_id)))
        else:
            role = settings.village_role_for(
                state.village_name, state.village_id
            )
            gid = (
                it.gid
                if it.gid > 0
                else _pick_group_gid(state, gids, role)
            )
            if cur_lvl == 0:
                ok, why = prereqs_met(state.fields, gid, is_capital(state))
                if not ok:
                    # Prerequisiti mancanti (spec): lo step non viene
                    # scartato — si propone il primo requisito interno
                    # costruibile (es. Palazzo -> Ambasciata liv. 1).
                    prereq_field = _prereq_intent(state, gid, slot_id)
                    if prereq_field is not None:
                        out.append(prereq_field)
                        continue
                    logger.debug(
                        "[%s] Nuovo edificio gid %d bloccato: %s",
                        state.village_name,
                        gid,
                        why,
                    )
                    continue
                out.append(
                    Field(
                        slot_id=slot_id,
                        name=GID_LABELS.get(gid, f"slot {slot_id}"),
                        level=0,
                        resource_type=None,
                        gid=gid,
                    )
                )
            else:
                f = next(
                    (x for x in state.interior_fields() if x.slot_id == slot_id),
                    None,
                )
                if f and f.slot_id not in queued and f.level < field_max_level(state, f):
                    out.append(f)

    # Dedup preservando l'ordine
    seen: set[int] = set()
    unique: list[Field] = []
    for f in out:
        if f.slot_id not in seen:
            seen.add(f.slot_id)
            unique.append(f)
    return unique


# Circuit-breaker "falso EXECUTED": traccia il livello dello slot al
# momento di ogni ActionResult.success su UPGRADE_FIELD. Se per 2
# esecuzioni consecutive il livello risulta identico allo snapshot
# successivo (e lo slot non e' in coda), lo step viene marcato
# "stuck" e saltato invece di ritentarlo all'infinito. Stato di
# runtime, azzerato a riavvio; un cambio livello reale (build
# completata o intervento manuale) azzera la storia dello slot.
_FOCUS_EXEC_HISTORY: dict[tuple[str, int], list[int]] = {}
_FOCUS_STUCK_MIN_ATTEMPTS = 2


def record_upgrade_exec(village_id: int | str, slot_id: int, level: int) -> None:
    """Registra un EXECUTED su ``slot_id`` con il livello noto all'epoca."""
    if not slot_id or slot_id <= 0:
        return
    key = (str(village_id or "0"), int(slot_id))
    hist = _FOCUS_EXEC_HISTORY.setdefault(key, [])
    hist.append(int(level))
    del hist[:-8]


def focus_stuck_slots(state: GameState) -> set[int]:
    """Slot della Coda Prioritaria bloccati da EXECUTED senza progresso.

    Uno slot e' stuck se: >=2 EXECUTED consecutive sullo stesso
    livello, il livello attuale non e' cresciuto rispetto a quello
    registrato e non compare nella coda costruzioni. Se il livello e'
    variato la storia viene azzerata (progresso reale o mano umana).
    """
    queued = queued_slot_ids(state)
    levels = {f.slot_id: int(f.level) for f in state.fields}
    # La chiave registrata da record_upgrade_exec puo' essere il newdid
    # oppure il nome: entrambe identificano lo stesso villaggio.
    vkeys = {
        str(v) for v in (state.village_id, state.village_name) if v
    }
    stuck: set[int] = set()
    for (vid, slot), hist in list(_FOCUS_EXEC_HISTORY.items()):
        if vid not in vkeys or len(hist) < _FOCUS_STUCK_MIN_ATTEMPTS:
            continue
        if slot not in levels:
            # Slot assente dallo snapshot (lettura parziale): il
            # livello non e' osservabile -> niente marcatura STUCK.
            continue
        cur = levels[slot]
        last = hist[-1]
        if cur != last:
            # Livello cambiato dopo l'ultimo EXECUTED: progresso
            # reale o build manuale -> resetta il contatore.
            del _FOCUS_EXEC_HISTORY[(vid, slot)]
            continue
        if slot in queued:
            continue
        if hist[-_FOCUS_STUCK_MIN_ATTEMPTS:].count(last) >= (
            _FOCUS_STUCK_MIN_ATTEMPTS
        ):
            stuck.add(slot)
    return stuck


def focus_intents(state: GameState, settings: Settings) -> list[Field]:
    """Field candidati dalla coda prioritaria building_queue_plan.

    Ogni step e' (gruppo gid, target_level): il builder lo salta quando
    il target e' raggiunto — campi risorsa: nessun campo sotto target;
    edifici: almeno un'istanza al target. Edificio assente -> nuova
    costruzione se i prerequisiti reggono. I gruppi multi-gid (mura,
    Residenza/Palazzo) sono alternative: basta UNA delle varianti.
    """
    unique = focus_candidates(state, settings)
    # Doppia coda (spec): un cantiere occupato non congela la coda
    # manuale — i candidati restano solo quelli compatibili con gli
    # slot coda effettivamente liberi (dorf1/dorf2 disaccoppiati su
    # tribu' romana/Plus, slot unico condiviso altrimenti).
    can_out, can_in = queue_slots_free(state, settings)
    return [
        f for f in unique
        if (can_out if f.is_resource_field else can_in)
    ]


# Gruppi di edifici mutuamente esclusivi: la presenza di uno rende lo
# step degli altri irrealizzabile (spec T4.6):
# - Residenza (25) <-> Palazzo (26): il gioco rifiuta la costruzione;
# - Mura (31/32/33): un solo tipo per villaggio, dipende dalla tribu'.
# NIENTE esclusioni verso Mercato (17) / Accademia (22): sono edifici
# indipendenti dalla Residenza.
_MUTUALLY_EXCLUSIVE_GIDS = (
    (GID_RESIDENCE, GID_PALACE),
    WALL_GIDS,
)


def focus_skip_reasons(
    state: GameState, settings: Settings
) -> list[str]:
    """Motivi per cui gli step in testa alla Coda Prioritaria non
    producono candidati (Decision Audit Log).

    Scorre la coda come focus_intents: gli step completati (target
    gia' raggiunto) vengono saltati in silenzio; per ogni step
    bloccato viene prodotta una ragione esplicita. La scansione si
    ferma al primo step applicabile o al terzo motivo raccolto.
    """
    vcfg = settings.village_config_for(
        state.village_name, state.village_id
    )
    plan = []
    if vcfg is not None and getattr(vcfg, "building_focus_enabled", True):
        plan = list(
            vcfg.building_queue_plan or vcfg.building_focus or []
        )
    if not plan:
        return []
    queued = queued_slot_ids(state)
    reserved_slots: set[int] = set()
    stuck = focus_stuck_slots(state)
    reasons: list[str] = []
    items = _focus_items(plan)
    for it in items:
        if len(reasons) >= 3:
            break
        gids = (it.gid,) if it.gid > 0 else tuple(gids_for_focus_key(it.key))
        label = (
            GID_LABELS.get(gids[0], str(gids[0])) if gids else (it.key or "?")
        )
        slot_id, cur_lvl, status = resolve_target_slot(
            it, state, reserved_slots
        )
        if status == "TARGET_REACHED":
            continue
        if status == "INVALID_SLOT_OCCUPIED":
            reasons.append(
                f"'{label}' slot #{it.slot_id} occupato da altro edificio (INVALID_SLOT_OCCUPIED)"
            )
            continue
        if status == "NO_FREE_SLOTS":
            reasons.append(f"'{label}' nessun slot dorf2 libero (19-38)")
            continue
        if status != "OK" or slot_id is None:
            continue
        if slot_id in stuck:
            reasons.append(
                f"'{label}' slot {slot_id} bloccato: EXECUTED ripetuto senza aumento livello (STUCK)"
            )
            continue

        if len(gids) == 1 and gids[0] in FIELD_GID_TYPES:
            rtype = FIELD_GID_TYPES[gids[0]]
            below = [
                f
                for f in state.resource_fields()
                if f.resource_type is rtype
                and (it.target_level <= 0 or f.level < it.target_level)
            ]
            if not below:
                continue
            if upgradeable_fields(state, below):
                break
            reasons.append(
                f"'{label}' liv. {it.target_level}: tutti i campi sotto target sono in coda o al livello massimo"
            )
            continue

        if cur_lvl == 0:
            gid = it.gid if it.gid > 0 else _pick_group_gid(state, gids)
            ok, why = prereqs_met(state.fields, gid, is_capital(state))
            if not ok:
                # Step applicabile via prerequisito automatico: nessun
                # motivo di blocco da segnalare.
                if _prereq_intent(state, gid, slot_id) is not None:
                    break
                reasons.append(f"'{label}' nuova costruzione bloccata: {why}")
                continue
            break
        else:
            f = next(
                (x for x in state.interior_fields() if x.slot_id == slot_id),
                None,
            )
            if f and f.slot_id not in queued and f.level < field_max_level(state, f):
                break
            if f and f.slot_id in queued:
                reasons.append(
                    f"'{label}' slot #{slot_id} gia' in coda di costruzione"
                )
                continue
            if f and f.level >= field_max_level(state, f):
                reasons.append(f"'{label}' al livello massimo consentito")
                continue
    return reasons


FOUNDER_MAIN_TARGET = 15
FOUNDER_STORAGE_TARGET = 10
FOUNDER_RESIDENCE_TARGET = 10


def founder_intents(state: GameState, settings: Settings) -> list[Field]:
    """Sequenza dedicata al villaggio fondatore fino a Reggia/Palazzo liv. 10.

    1. Palazzo Pubblico (gid 15) -> liv. 10 (riduce tempi costruzione)
       + Magazzino/Granaio -> liv. 10 (capienza per Reggia 8-10 e 3 coloni);
    2. Reggia/Palazzo -> liv. 10 mantenendo il Palazzo Pubblico 2-3 livelli
       avanti (fino a liv. 15) per velocizzare i livelli alti della Reggia.
    """
    interiors = state.interior_fields()

    def _best(gid: int) -> int:
        return max((f.level for f in interiors if f.gid == gid), default=0)

    res_lvl = max(_best(GID_RESIDENCE), _best(GID_PALACE))
    if res_lvl >= FOUNDER_RESIDENCE_TARGET:
        return []

    out: list[Field] = []

    # Fase 1: Palazzo Pubblico -> 10 e Depositi (Magazzino/Granaio) -> 10
    if _best(GID_MAIN_BUILDING) < 10:
        out.extend(
            f for f in _interior_intents(state, (GID_MAIN_BUILDING,))
            if f.level < 10
        )
    if _best(GID_WAREHOUSE) < FOUNDER_STORAGE_TARGET:
        wh = [
            f for f in _interior_intents(state, (GID_WAREHOUSE,))
            if f.level < FOUNDER_STORAGE_TARGET
        ]
        if not wh and _best(GID_WAREHOUSE) == 0:
            wh = new_building_intents(state, (GID_WAREHOUSE,), {f.slot_id for f in out})
        out.extend(wh)
    if _best(GID_GRANARY) < FOUNDER_STORAGE_TARGET:
        gr = [
            f for f in _interior_intents(state, (GID_GRANARY,))
            if f.level < FOUNDER_STORAGE_TARGET
        ]
        if not gr and _best(GID_GRANARY) == 0:
            gr = new_building_intents(state, (GID_GRANARY,), {f.slot_id for f in out})
        out.extend(gr)
    if out:
        return out

    # Fase 2: Palazzo Pubblico (fino a 15, target = res_lvl + 3) + Reggia -> 10
    desired_main = min(FOUNDER_MAIN_TARGET, res_lvl + 3)
    if _best(GID_MAIN_BUILDING) < desired_main:
        out.extend(
            f for f in _interior_intents(state, (GID_MAIN_BUILDING,))
            if f.level < desired_main
        )

    home_gid = GID_PALACE if _best(GID_PALACE) > _best(GID_RESIDENCE) else GID_RESIDENCE
    home = [
        f for f in _interior_intents(state, (home_gid,))
        if f.level < FOUNDER_RESIDENCE_TARGET
    ]
    if not home and _best(home_gid) == 0:
        home = new_building_intents(state, (home_gid,), {f.slot_id for f in out})
    out.extend(home)

    return out


def role_intents(state: GameState, settings: Settings) -> list[Field]:
    """Infrastrutture coerenti col ruolo, bilanciate per livello.

    La capitale (auto-rilevata) aggiunge granaio/mulino/forno al profilo.
    """
    if is_founder_village(state, settings):
        f_intents = founder_intents(state, settings)
        if f_intents:
            return f_intents

    role = (settings.village_role_for(state.village_name, state.village_id)
            or "standard").lower()
    gids = _ROLE_INTERIORS.get(role, ())
    if is_capital(state):
        gids = tuple(dict.fromkeys(gids + _ROLE_INTERIORS["croplog"]))
    if not gids:
        return []
    existing = _interior_intents(state, gids)
    # Sviluppo coordinato "verso il 10 prima del 20": il sort per
    # livello crescente porta naturalmente tutti gli edifici del ruolo
    # al 10 prima che qualunque di loro superi quella soglia.
    newbies = new_building_intents(
        state, gids, {f.slot_id for f in existing}
    )
    return sorted(existing + newbies, key=lambda f: (f.level, f.slot_id))


def weighted_scarcest(res: "Resources", weights: dict) -> ResourceType:
    """Risorsa da uppare secondo i pesi strategici del villaggio.

    ``VillageConfig.resource_weights`` (Strategy Compiler): punteggio =
    (riempimento corrente / capacita') / peso — a parita' di riempimento
    la risorsa col peso piu' alto vince. Pesi <= 0 escludono la risorsa;
    dict vuoto/tutti nulli -> comportamento standard ``scarcest()``.
    """
    scored: dict[ResourceType, float] = {}
    for rt in ResourceType:
        try:
            w = float((weights or {}).get(rt.value, 0.0) or 0.0)
        except (TypeError, ValueError):
            w = 0.0
        if w <= 0:
            continue
        cap = res.capacity(rt) or 1
        scored[rt] = (res.amount(rt) / cap) / w
    if not scored:
        return res.scarcest()
    return min(scored, key=scored.get)


def village_scarcest(state: GameState, settings: Settings) -> ResourceType:
    """Selettore unico dorf1: croplog -> grano, pesi -> weighted, else scarcest."""
    role = settings.village_role_for(state.village_name, state.village_id)
    if role == "croplog":
        return ResourceType.CROP
    vcfg = settings.village_config_for(state.village_name, state.village_id)
    weights = getattr(vcfg, "resource_weights", None) if vcfg is not None else None
    if weights:
        return weighted_scarcest(state.resources, weights)
    return state.resources.scarcest()


def economy_intents(state: GameState, settings: Settings) -> list[Field]:
    """Campo risorsa piu' debole della risorsa piu' scarsa (logica base)."""
    fields = upgradeable_fields(state, state.resource_fields())
    if not fields:
        return []
    scarcest = village_scarcest(state, settings)
    same_type = [f for f in fields if f.resource_type is scarcest]
    pool = same_type or fields
    return sorted(pool, key=lambda f: (f.level, f.slot_id))


# ---------------------------------------------------------------------------
# New Village Bootstrap (v2.0, spec 2)
# ---------------------------------------------------------------------------

def bootstrap_active(state: GameState, settings: Settings) -> bool:
    """True se il villaggio e' flaggato is_bootstrap in VillageConfig."""
    vcfg = settings.village_config_for(state.village_name, state.village_id)
    return bool(vcfg is not None and vcfg.is_bootstrap)


def bootstrap_complete(state: GameState) -> bool:
    """True se i requisiti minimi di startup sono soddisfatti.

    Requisiti (spec 2): magazzino e granaio >= 5, Edificio Principale
    >= 5, tutti i campi risorsa >= 3, Mercato e Punto di Raduno esistenti.
    """
    interiors = state.interior_fields()

    def _best(gid: int) -> int:
        return max((f.level for f in interiors if f.gid == gid), default=0)

    if _best(GID_WAREHOUSE) < BOOTSTRAP_STORAGE_LEVEL:
        return False
    if _best(GID_GRANARY) < BOOTSTRAP_STORAGE_LEVEL:
        return False
    if _best(GID_MAIN_BUILDING) < BOOTSTRAP_MAIN_LEVEL:
        return False
    if _best(GID_MARKETPLACE) < 1 or _best(GID_RALLY_POINT) < 1:
        return False
    res_fields = state.resource_fields()
    if not res_fields:
        return False
    return all(f.level >= BOOTSTRAP_FIELD_LEVEL for f in res_fields)


def bootstrap_intents(state: GameState, settings: Settings) -> list[Field]:
    """Sequenza costruttiva di startup per il villaggio appena fondato.

    Ordine (spec 2):
    1. Magazzino/Granaio fino al liv. 5 — devono assorbire le spedizioni
       dal mercato senza overflow (incluse le nuove costruzioni);
    2. Edificio Principale fino al liv. 5 (velocita' costruzioni);
    3. Mercato (gid 17) e Punto di Raduno (gid 16) — i prerequisiti del
       Mercato (Municipio 3 + storage 1) rendono naturale l'ordine;
    4. Campi risorsa bilanciati via scarcest() fino al liv. 3.
    """
    interiors = state.interior_fields()

    def _best(gid: int) -> int:
        return max((f.level for f in interiors if f.gid == gid), default=0)

    out: list[Field] = []

    # 1. Storage < 5 (upgrade esistenti o costruzione se assenti)
    if _best(GID_WAREHOUSE) < BOOTSTRAP_STORAGE_LEVEL:
        wh = [
            f for f in _interior_intents(state, (GID_WAREHOUSE,))
            if f.level < BOOTSTRAP_STORAGE_LEVEL
        ]
        if not wh and _best(GID_WAREHOUSE) == 0:
            wh = new_building_intents(
                state, (GID_WAREHOUSE,), {f.slot_id for f in out}
            )
        out.extend(wh)
    if _best(GID_GRANARY) < BOOTSTRAP_STORAGE_LEVEL:
        gr = [
            f for f in _interior_intents(state, (GID_GRANARY,))
            if f.level < BOOTSTRAP_STORAGE_LEVEL
        ]
        if not gr and _best(GID_GRANARY) == 0:
            gr = new_building_intents(
                state, (GID_GRANARY,), {f.slot_id for f in out}
            )
        out.extend(gr)
    if out:
        return out

    # 2. Edificio Principale < 5 (upgrade o costruzione su area vuota)
    if _best(GID_MAIN_BUILDING) < BOOTSTRAP_MAIN_LEVEL:
        main = [
            f for f in _interior_intents(state, (GID_MAIN_BUILDING,))
            if f.level < BOOTSTRAP_MAIN_LEVEL
        ]
        if not main and _best(GID_MAIN_BUILDING) == 0:
            main = new_building_intents(state, (GID_MAIN_BUILDING,))
        if main:
            return main

    # 3. Mercato e Punto di Raduno (costruzione se assenti; upgrade se
    #    presenti a liv. 0 non e' possibile: solo nuove costruzioni)
    missing: list[int] = []
    if _best(GID_MARKETPLACE) < 1:
        missing.append(GID_MARKETPLACE)
    if _best(GID_RALLY_POINT) < 1:
        missing.append(GID_RALLY_POINT)
    if missing:
        newbie = new_building_intents(state, tuple(missing))
        if newbie:
            return newbie
        # Prerequisiti non soddisfatti (es. Municipio < 3): si ricade
        # sui campi, il check del ciclo successivo riprovera'.

    # 4. Tutti i campi risorsa >= 3, bilanciati sulla risorsa scarsa
    weak = [
        f for f in upgradeable_fields(state, state.resource_fields())
        if f.level < BOOTSTRAP_FIELD_LEVEL
    ]
    if weak:
        scarcest = state.resources.scarcest()
        same = [f for f in weak if f.resource_type is scarcest]
        pool = same or weak
        return sorted(pool, key=lambda f: (f.level, f.slot_id))
    return []


# Reclaim & Chiefing: livelli richiesti per produrre e ospitare i nobili
# (Senatore/Comandante/Capotribu' si ricercano in Accademia e si
# addestrano in Residenza/Palazzo; lo slot conquista serve al 20).
RECLAIM_ACADEMY_LEVEL = 20
RECLAIM_RESIDENCE_LEVEL = 20


def reclaim_intents(state: GameState, settings: Settings) -> list[Field]:
    """Infrastrutture del villaggio origine di un treno di riconquista.

    Ordine (spec 2.1):
    1. Accademia fino al liv. 20 — sblocca la ricerca del nobile
       (t9/t19/t29 a seconda della tribu');
    2. Residenza o Palazzo fino al liv. 20 — lo slot conquista piu'
       alto e' necessario per un'ondata di chiefing; se entrambi
       esistono si estende quello gia' piu' alto, altrimenti si
       preferisce la Residenza (costo minore).
    """
    interiors = state.interior_fields()

    def _best(gid: int) -> int:
        return max((f.level for f in interiors if f.gid == gid), default=0)

    out: list[Field] = []
    # 1. Accademia < 20
    if _best(GID_ACADEMY) < RECLAIM_ACADEMY_LEVEL:
        ac = [
            f for f in _interior_intents(state, (GID_ACADEMY,))
            if f.level < RECLAIM_ACADEMY_LEVEL
        ]
        if not ac and _best(GID_ACADEMY) == 0:
            ac = new_building_intents(
                state, (GID_ACADEMY,), {f.slot_id for f in out}
            )
        out.extend(ac)
    if out:
        return out

    # 2. Residenza/Palazzo < 20 (slot conquista)
    home_gid = (
        GID_PALACE
        if _best(GID_PALACE) > _best(GID_RESIDENCE)
        else GID_RESIDENCE
    )
    if _best(home_gid) < RECLAIM_RESIDENCE_LEVEL:
        home = [
            f for f in _interior_intents(state, (home_gid,))
            if f.level < RECLAIM_RESIDENCE_LEVEL
        ]
        if not home and _best(home_gid) == 0:
            home = new_building_intents(state, (home_gid,))
        if home:
            return home
    return []


def is_founder_village(state: GameState, settings: Settings) -> bool:
    """True se il ruolo del villaggio si risolve in 'founder'.

    Gli alias della dashboard ('founder', 'pioneer', 'early_cropper')
    convergono tutti su 'early_cropper_founder' via resolve_role.
    """
    role = settings.village_role_for(state.village_name, state.village_id)
    return resolve_role(role) == FOUNDER_ROLE


# Gate di maturita' economica del Freeze Espansione: sul founder appena
# creato i PC del 2° villaggio sono gia' pronti, ma bloccare le spese
# con campi al liv. 1-2 strozzerebbe la crescita. Il freeze scatta solo
# quando il villaggio regge l'accumulo coloni.
FREEZE_MIN_AVG_FIELD_LEVEL = 4.0
# Chiavi normalizzate di production_per_hour (ResourceType.value o str).
_RESOURCE_KEYS = frozenset(rt.value for rt in ResourceType)


def _freeze_economic_base(state: GameState, settings: Settings) -> bool:
    """True se il villaggio ha una base produttiva sufficiente al freeze.

    Alternativa (basta UNA condizione):
    1. produzione oraria lorda totale (wood+clay+iron+crop) sopra la
       soglia configurabile ``expansion.min_freeze_hourly_prod``
       (default 900/h su server 1x, da scalare ~ speed * 900);
    2. media dei livelli dei campi risorsa >= 4.0;
    3. Residenza o Palazzo gia' al livello >= 8.
    """
    # 1. Produzione oraria lorda totale (chiavi ResourceType o str).
    prod = getattr(state.resources, "production_per_hour", None) or {}
    hourly = sum(
        int(v or 0)
        for k, v in prod.items()
        if str(getattr(k, "value", k)) in _RESOURCE_KEYS
    )
    if hourly > int(getattr(settings.expansion, "min_freeze_hourly_prod", 900) or 0):
        return True

    # 2. Media livelli dei 18 campi risorsa >= soglia.
    fields = state.resource_fields()
    if fields and sum(f.level for f in fields) / len(fields) >= FREEZE_MIN_AVG_FIELD_LEVEL:
        return True

    # 3. Residenza/Palazzo >= 8.
    return max(
        (
            f.level
            for f in state.interior_fields()
            if f.gid in (GID_RESIDENCE, GID_PALACE)
        ),
        default=0,
    ) >= 8


def expansion_freeze(state: GameState, settings: Settings) -> bool:
    """True se sul founder vige il congelamento delle spese extra.

    Attivo finche' mancano coloni (<3 a casa), il villaggio ha una base
    economica matura (``_freeze_economic_base``) e i Punti Cultura sono
    pronti oppure la Residenza/Reggia e' gia' alta (>= 8): ogni risorsa
    deve coprire i coloni e i soli passi critici dell'espansione.
    Condiviso tra brain (salta le regole campi-risorse) e orchestrator
    (declassa a WAIT le spese fuori whitelist). Il toggle dashboard
    `expansion_settings.spending_freeze` spegne del tutto la regola.
    """
    try:
        if not getattr(settings.expansion, "spending_freeze", True):
            return False
        if not is_founder_village(state, settings):
            return False
        if int(getattr(state, "settlers_home", 0) or 0) >= 3:
            return False
        res_lvl = max(
            (
                f.level
                for f in state.interior_fields()
                if f.gid in (GID_RESIDENCE, GID_PALACE)
            ),
            default=0,
        )
        cp_ready = (
            int(getattr(state, "culture_points_required", 0) or 0) > 0
            and int(getattr(state, "culture_points", 0) or 0)
            >= int(state.culture_points_required)
        )
        return bool(
            _freeze_economic_base(state, settings)
            and (cp_ready or res_lvl >= 8)
        )
    except Exception:
        return False


def culture_boost_active(state: GameState, settings: Settings) -> bool:
    """True se il Culture Points Booster deve girare su questo villaggio.

    Condizioni (spec 2): ruolo founder, Residenza/Palazzo >= 10 e
    Punti Cultura ancora insufficienti per il prossimo slot. Si
    disattiva da solo quando cp_current >= cp_required (o quando il
    dato non e' stato ancora letto: booster spento, niente supposizioni).
    """
    if not is_founder_village(state, settings):
        return False
    if state.culture_points_required <= 0:
        return False
    if state.culture_points >= state.culture_points_required:
        return False
    interiors = state.interior_fields()
    return any(
        f.gid in (GID_RESIDENCE, GID_PALACE)
        and f.level >= _CP_BOOST_RESIDENCE_LEVEL
        for f in interiors
    )


def settler_surplus_ok(state: GameState) -> bool:
    """True se tutte le risorse superano la riserva dei 3 coloni.

    Il booster spende solo quando i magazzini coprono gia' il costo
    completo dei coloni (max fra le tre tribu', per sicurezza).
    """
    try:
        from .replenishment import settler_reserve  # no cicli
    except Exception:  # noqa: BLE001
        return False
    reserve = settler_reserve()
    if not reserve:
        return False
    res = state.resources
    available = {
        "wood": res.wood,
        "clay": res.clay,
        "iron": res.iron,
        "crop": res.crop,
    }
    return all(
        int(available.get(k, 0)) >= int(reserve.get(k, 0))
        for k in ("wood", "clay", "iron", "crop")
    )


def culture_boost_intents(state: GameState) -> list[Field]:
    """Interni ad alta resa PC upgradabili, dal livello piu' basso.

    Include la nuova costruzione degli edifici mancanti se i
    prerequisiti reggono (es. Municipio dopo Accademia 10 + Municipio
    (Edificio Principale) 10).
    """
    rank = {gid: i for i, gid in enumerate(_CP_BOOST_GIDS)}
    existing = sorted(
        (
            f
            for f in upgradeable_fields(state, state.interior_fields())
            if f.gid in _CP_BOOST_GIDS and f.gid > 0
        ),
        key=lambda f: (f.level, rank.get(f.gid, 99), f.slot_id),
    )
    newbies = new_building_intents(
        state, _CP_BOOST_GIDS, {f.slot_id for f in existing}
    )
    return existing + newbies


def local_spend_intents(state: GameState) -> list[Field]:
    """Interni upgradabili dal livello piu' alto (costo maggiore).

    Usati dall'overflow di emergenza per smaltire risorse nel villaggio
    prima di ricorrere all'invio via Mercato.
    """
    return sorted(
        upgradeable_fields(state, state.interior_fields()),
        key=lambda f: (-f.level, f.slot_id),
    )


# ---------------------------------------------------------------------------
# Anti-Raid: Depositi Segreti multipli sotto pressione raid
# ---------------------------------------------------------------------------

def _cranny_tribe(state: GameState, settings: Settings) -> str:
    """Tribu' per il calcolo capacita': config esplicita, poi sensori."""
    return (
        (settings.tribe or "").strip().lower()
        or _detect_tribe(state)
    )


def cranny_protected_capacity(
    state: GameState, settings: Settings
) -> int:
    """Capacita' protetta per risorsa dei Nascondigli presenti.

    Somma la tabella livelli T4.x (x2 per i Galli, x0.8 prudenziale
    se gli attaccanti rilevati sono Teutoni).
    """
    mult = 2.0 if _cranny_tribe(state, settings) == "gauls" else 1.0
    if getattr(state, "teuton_raiders", False):
        mult *= _CRANNY_TEUTON_LOOT_FACTOR
    cap = 0
    for f in state.interior_fields():
        if f.gid == GID_CRANNY:
            lvl = max(0, min(_CRANNY_MAX_LEVEL, int(f.level)))
            cap += int(_CRANNY_CAP_BY_LEVEL[lvl] * mult)
    return cap


def cranny_target_protection(
    state: GameState, settings: Settings
) -> int:
    """Risorse da tenere al sicuro per risorsa.

    Il piu' alto tra la soglia di ruolo e il picco di giacenza
    corrente in magazzino (max di legno/argilla/ferro/grano).
    """
    role = settings.village_role_for(state.village_name, state.village_id)
    floor = (
        _CRANNY_TARGET_FOUNDER
        if role == "early_cropper_founder"
        else _CRANNY_TARGET_DEFAULT
    )
    res = state.resources
    peak = max(res.wood, res.clay, res.iron, res.crop)
    return max(floor, int(peak))


def cranny_max_count(state: GameState, settings: Settings) -> int:
    """Tetto Nascondigli: VillageConfig.max_crannies, poi auto."""
    vcfg = settings.village_config_for(
        state.village_name, state.village_id
    )
    cfg_max = int(getattr(vcfg, "max_crannies", 0) or 0)
    if cfg_max > 0:
        return cfg_max
    role = settings.village_role_for(state.village_name, state.village_id)
    if (
        getattr(state, "single_village", False)
        or role == "early_cropper_founder"
    ):
        return 3
    return 2


def anti_raid_intents(
    state: GameState, settings: Settings
) -> list[Field]:
    """Depositi Segreti anti-raid: upgrade esistenti, poi fondazione.

    Attiva solo con ``state.under_raid_pressure`` (alimentato
    dall'orchestrator). Sequenza:
    1. upgrade dei Nascondigli esistenti sotto il 10 (livello piu'
       basso prima, non in coda);
    2. fondazione su area vuota solo con TUTTI i depositi al 10
       (requisito T4.6 per il secondo/terzo), sotto ``max_crannies``
       e conservando almeno ``_CRANNY_MIN_FREE_SLOTS`` aree libere
       (slot 19-38: il 39 e' riservato al Punto di Raduno, il 40
       alle Mura).
    """
    if not getattr(state, "under_raid_pressure", False):
        return []
    protected = cranny_protected_capacity(state, settings)
    target = cranny_target_protection(state, settings)
    if protected >= target:
        return []

    queued = queued_slot_ids(state)
    crannies = sorted(
        (f for f in state.interior_fields() if f.gid == GID_CRANNY),
        key=lambda f: (f.level, f.slot_id),
    )

    # 1. Potenziamento dell'esistente (slot non in coda)
    upgrades = [
        f for f in crannies
        if f.level < _CRANNY_MAX_LEVEL and f.slot_id not in queued
    ]
    if upgrades:
        return upgrades

    # 2. Fondazione: nessun deposito sotto il 10 — neanche con
    #    lavori in corso (requisito T4.6).
    if any(f.level < _CRANNY_MAX_LEVEL for f in crannies):
        return []
    # Conta come deposito potenziale anche ogni nuova costruzione in
    # coda su area vuota: lo slot potrebbe ospitare un Nascondiglio
    # gia' avviato (il gid in coda non e' leggibile).
    new_builds = {
        f.slot_id for f in state.interior_fields()
        if f.is_empty_site and f.slot_id in queued
    }
    if len(crannies) + len(new_builds) >= cranny_max_count(
        state, settings
    ):
        return []
    empties = [
        f for f in empty_slots(state) if 19 <= f.slot_id <= 38
    ]
    if len(empties) < _CRANNY_MIN_FREE_SLOTS:
        return []
    slot = empties[0]
    logger.info(
        "[%s] Anti-Raid: protezione %d < target %d -> fondazione "
        "Nascondiglio #%d (slot %d).",
        state.village_name, protected, target,
        len(crannies) + len(new_builds) + 1, slot.slot_id,
    )
    return [
        Field(
            slot_id=slot.slot_id,
            name=GID_LABELS.get(GID_CRANNY, "Nascondiglio"),
            level=0,
            resource_type=None,
            gid=GID_CRANNY,
        )
    ]


# ---------------------------------------------------------------------------
# Selettore principale + piano
# ---------------------------------------------------------------------------

def select_upgrade(
    state: GameState, settings: Settings
) -> UpgradeIntent | None:
    """Prossimo upgrade secondo la priorita' storage > grano > focus >
    ruolo > economia. Ignora i limiti di coda: e' il brain a verificarli.
    """
    def _mk(
        kind: str, fields: list[Field], reason: str
    ) -> UpgradeIntent | None:
        if not fields:
            return None
        head = fields[0]
        return UpgradeIntent(
            kind=kind,
            slot_id=head.slot_id,
            name=field_label(head),
            gid=head.gid,
            target_level=head.level + 1,
            is_resource_field=head.is_resource_field,
            reason=reason,
            candidates=[f.slot_id for f in fields],
            construct=construct_gids(state, fields),
        )

    # 1. Storage quasi pieno
    needed = storage_needed(state)
    if needed:
        intents = _interior_intents(state, tuple(needed))
        # Magazzino/granaio assenti del tutto: costruzione su area vuota.
        intents += new_building_intents(
            state, needed, {f.slot_id for f in intents}
        )
        it = _mk(
            "storage", intents,
            "Storage quasi pieno: potenziamento magazzino/granaio",
        )
        if it:
            return it

    # 2. Grain Safety Net
    if crop_safety_needed(state, settings):
        net = net_crop(state)
        it = _mk(
            "grain_safety", grain_safety_intents(state),
            f"Margine grano {net}/h sotto soglia "
            f"{crop_threshold(state, settings):g}/h",
        )
        if it:
            return it

    # 3. New Village Bootstrap: sequenza startup finche' i requisiti
    #    minimi non sono soddisfatti (storage 5 / Municipio 5 / campi 3 /
    #    Mercato + Punto di Raduno) — precede focus e ruolo.
    if bootstrap_active(state, settings):
        it = _mk(
            "bootstrap", bootstrap_intents(state, settings),
            "Bootstrap nuovo villaggio: storage/Municipio/campi/Mercato",
        )
        if it:
            return it

    # 3.5 Reclaim & Chiefing: villaggio origine del treno ->
    #     Accademia 20 (ricerca nobile) + Residenza/Palazzo 20 (slot).
    vcfg_reclaim = settings.village_config_for(
        state.village_name, state.village_id
    )
    if vcfg_reclaim is not None and vcfg_reclaim.is_reclaim_source:
        it = _mk(
            "reclaim", reclaim_intents(state, settings),
            "Riconquista: Accademia/Residenza per i nobili",
        )
        if it:
            return it

    # 3.6 Anti-Raid: sotto pressione raid i Nascondigli (gid 23)
    #     precedono il focus manuale e lo sviluppo ordinario —
    #     protezione risorse + resource-dumping difensivo (upkeep 0:
    #     nessun impatto sul bilancio grano, resta dietro alla Grain
    #     Safety Net come tutti gli upgrade non-grano).
    it = _mk(
        "anti_raid", anti_raid_intents(state, settings),
        "Anti-Raid: potenziamento Nascondigli (protezione risorse)",
    )
    if it:
        return it

    # 4. building_queue_plan: override manuale utente
    it = _mk(
        "focus", focus_intents(state, settings),
        "Focus edifici manuale da dashboard",
    )
    if it:
        return it

    # 5. Ruolo (hammer/defensive/croplog/feeder + capitale)
    role = settings.village_role_for(state.village_name, state.village_id)
    it = _mk(
        "role", role_intents(state, settings),
        f"Sviluppo infrastrutture ruolo '{role}'",
    )
    if it:
        return it

    # 6. Economia: campo piu' debole della risorsa scarsa
    it = _mk(
        "economy", economy_intents(state, settings),
        "Upgrade campo risorsa piu' debole",
    )
    return it


def plan_upgrades(
    state: GameState, settings: Settings, depth: int = PLAN_DEPTH
) -> list[dict]:
    """Simula i prossimi `depth` upgrade incrementando i livelli.

    Stima economica (senza costi/risorse): mostra l'ordine logico di
    sviluppo, non una timeline temporale.
    """
    sim_fields = [
        Field(
            slot_id=f.slot_id,
            name=f.name,
            level=f.level,
            resource_type=f.resource_type,
            gid=f.gid,
        )
        for f in state.fields
    ]
    sim_state = GameState(
        resources=state.resources,
        fields=sim_fields,
        build_queue=list(state.build_queue),
        village_name=state.village_name,
        village_id=state.village_id,
        under_raid_pressure=state.under_raid_pressure,
        single_village=state.single_village,
        teuton_raiders=state.teuton_raiders,
    )
    plan: list[dict] = []
    chosen: set[int] = set()
    for _ in range(max(1, depth)):
        intent = select_upgrade(sim_state, settings)
        if intent is None or intent.slot_id in chosen:
            break
        plan.append(intent.to_dict())
        chosen.add(intent.slot_id)
        # Simula lo scatto di livello (e il gid, per le nuove costruzioni
        # su area vuota) per la scelta successiva.
        for f in sim_fields:
            if f.slot_id == intent.slot_id:
                f.level = intent.target_level
                if intent.gid > 0 and f.is_empty_site:
                    f.gid = intent.gid
                    if intent.name:
                        f.name = intent.name
                break
    return plan


# ---------------------------------------------------------------------------
# Persistenza per la dashboard
# ---------------------------------------------------------------------------

def save_build_plan(
    account_id: str, village_name: str, plan: list[dict]
) -> None:
    """Aggiorna la voce di un villaggio nel piano dell'account (kv DB)."""
    try:
        key = f"build_plan_{account_id}.json"
        data: dict = {}
        raw = db.kv_read(key)
        if raw:
            try:
                data = json.loads(raw)
            except ValueError:
                data = {}
        if not isinstance(data, dict):
            data = {}
        data[village_name] = {"updated": time.time(), "items": plan}
        db.kv_write(key, json.dumps(data, indent=2))
    except OSError as exc:
        logger.debug("save_build_plan fallito: %s", exc)


def load_build_plans(account_id: str) -> dict:
    """Piano upgrade per villaggio letto dalla dashboard (kv DB)."""
    raw = db.kv_read(f"build_plan_{account_id}.json")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
