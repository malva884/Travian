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

from .building_prereqs import (
    FIELD_GID_TYPES,
    GID_ACADEMY,
    GID_BAKERY,
    GID_GRAIN_MILL,
    GID_LABELS,
    GID_MAIN_BUILDING,
    WALL_GIDS,
    gids_for_focus_key,
    prereqs_met,
)
from .config import Settings
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
    Field,
    GameState,
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

# New Village Bootstrap (v2.0, spec 2): requisiti minimi del villaggio
# appena fondato prima di passare al ruolo assegnato.
BOOTSTRAP_STORAGE_LEVEL = 5   # magazzino + granaio >= 5
BOOTSTRAP_MAIN_LEVEL = 5      # Edificio Principale >= 5
BOOTSTRAP_FIELD_LEVEL = 3     # tutti i campi risorsa >= 3

# Ruolo -> gid interni sviluppati in modo bilanciato (livello minimo prima).
_ROLE_INTERIORS: dict[str, tuple[int, ...]] = {
    "hammer": (GID_BARRACKS, GID_STABLE, GID_WORKSHOP),
    "defensive": (GID_BARRACKS, GID_RESIDENCE) + WALL_GIDS,
    "croplog": (GID_GRANARY, GID_WAREHOUSE, GID_GRAIN_MILL, GID_BAKERY),
    "feeder": (GID_MARKETPLACE, GID_WAREHOUSE, GID_GRANARY),
}


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


def queued_slot_ids(state: GameState) -> set[int]:
    """Slot gia' occupati da una costruzione in coda."""
    return {q.slot_id for q in state.build_queue if q.slot_id > 0}


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
    """Gid degli storage da potenziare perche' quasi pieni."""
    res = state.resources
    needed: list[int] = []
    if res.warehouse_capacity > 0 and (
        max(res.wood, res.clay, res.iron) >= res.warehouse_capacity * threshold
    ):
        needed.append(GID_WAREHOUSE)
    if res.granary_capacity > 0 and res.crop >= res.granary_capacity * threshold:
        needed.append(GID_GRANARY)
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
    """Mappa slot_id -> gid per i candidati che sono nuove costruzioni.

    Riconosce i pseudo-field di `new_building_intents`: gid>0 su uno
    slot che nel dorf2 reale e' ancora vuoto (gid 0).
    """
    real = {f.slot_id: f for f in state.interior_fields()}
    return {
        f.slot_id: f.gid
        for f in fields
        if not f.is_resource_field
        and f.gid > 0
        and real.get(f.slot_id) is not None
        and real[f.slot_id].is_empty_site
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


def _focus_steps(focus_list: list) -> list[tuple[int, int]]:
    """Normalizza building_queue_plan in step ordinati (gid, target_level).

    Ogni elemento puo' essere:
    - una stringa chiave legacy (es. "barracks", "cropland");
    - un dict {"key": str, "target_level": int, "priority": int} oppure
      {"gid": int, "target_level": int} (Advanced Queue & Priority).

    Gli step dict vengono ordinati per 'priority' crescente (default =
    posizione nella lista); le chiavi legacy mantengono l'ordine dato.
    target_level 0/assente = fino al massimo consentito (comportamento
    legacy).
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

    steps: list[tuple[int, int]] = []
    for _prio, item in indexed:
        target = 0
        if isinstance(item, dict):
            try:
                target = max(0, int(item.get("target_level") or 0))
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
        steps.extend((g, target) for g in gids if g > 0)
    return steps


def focus_intents(state: GameState, settings: Settings) -> list[Field]:
    """Field candidati dalla coda prioritaria building_queue_plan.

    Ogni step e' (gid, target_level): il builder lo salta quando il
    target e' raggiunto — campi risorsa: nessun campo sotto target;
    edifici: almeno un'istanza al target. Edificio assente -> nuova
    costruzione se i prerequisiti reggono.
    """
    vcfg = settings.village_config_for(state.village_name, state.village_id)
    plan = []
    if vcfg is not None:
        plan = list(vcfg.building_queue_plan or vcfg.building_focus or [])
    if not plan:
        return []
    queued = queued_slot_ids(state)
    out: list[Field] = []
    for gid, target in _focus_steps(plan):
        if gid in FIELD_GID_TYPES:
            rtype = FIELD_GID_TYPES[gid]
            matches = [
                f for f in upgradeable_fields(state, state.resource_fields())
                if f.resource_type is rtype
                and (target <= 0 or f.level < target)
            ]
            out.extend(sorted(matches, key=lambda f: (f.level, f.slot_id)))
        else:
            interiors = [
                f for f in state.interior_fields()
                if f.gid == gid and f.gid > 0
            ]
            # Step raggiunto: un'istanza esistente e' gia' al target.
            if target > 0 and any(f.level >= target for f in interiors):
                continue
            matches = [
                f for f in interiors
                if f.slot_id not in queued
                and f.level < field_max_level(state, f)
                and (target <= 0 or f.level < target)
            ]
            out.extend(sorted(matches, key=lambda f: (f.level, f.slot_id)))
            if not interiors:
                # Edificio focus assente: nuova costruzione se possibile.
                out.extend(
                    new_building_intents(
                        state, (gid,), {f.slot_id for f in out}
                    )
                )
    # Dedup preservando l'ordine
    seen: set[int] = set()
    unique: list[Field] = []
    for f in out:
        if f.slot_id not in seen:
            seen.add(f.slot_id)
            unique.append(f)
    return unique


def role_intents(state: GameState, settings: Settings) -> list[Field]:
    """Infrastrutture coerenti col ruolo, bilanciate per livello.

    La capitale (auto-rilevata) aggiunge granaio/mulino/forno al profilo.
    """
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


def economy_intents(state: GameState, settings: Settings) -> list[Field]:
    """Campo risorsa piu' debole della risorsa piu' scarsa (logica base)."""
    fields = upgradeable_fields(state, state.resource_fields())
    if not fields:
        return []
    role = settings.village_role_for(state.village_name, state.village_id)
    scarcest = (
        ResourceType.CROP if role == "croplog" else state.resources.scarcest()
    )
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
            name=head.name or f"slot {head.slot_id}",
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

def _plan_path(account_id: str) -> Path:
    return STATE_DIR / f"build_plan_{account_id}.json"


def save_build_plan(
    account_id: str, village_name: str, plan: list[dict]
) -> None:
    """Aggiorna la voce di un villaggio nel file piano dell'account."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = _plan_path(account_id)
        data: dict = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        data[village_name] = {"updated": time.time(), "items": plan}
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.debug("save_build_plan fallito: %s", exc)


def load_build_plans(account_id: str) -> dict:
    """Piano upgrade per villaggio letto dalla dashboard."""
    path = _plan_path(account_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
