"""Smart Building & Upgrade Planning.

Generatore dinamico di upgrade: sostituisce la selezione statica con
un'unica funzione `select_upgrade` che applica, in ordine di priorita':

1. **Storage** — magazzino/granaio quasi pieni (overflow prevention);
2. **Grain Safety Net** — margine netto di grano sotto soglia -> campi
   di grano, poi Mulino/Forno;
3. **building_focus** — override manuale utente (priorita' sul ruolo);
4. **Ruolo** — hammer/defensive/croplog/feeder: infrastrutture militari
   o economiche bilanciate per livello;
5. **Economia** — campo risorsa piu' debole della risorsa piu' scarsa.

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
    GID_BAKERY,
    GID_GRAIN_MILL,
    GID_LABELS,
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

    kind: str            # storage|grain_safety|focus|role|economy
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
    """Normalizza building_focus in step ordinati (gid, target_level).

    Ogni elemento puo' essere:
    - una stringa chiave legacy (es. "barracks", "cropland");
    - un dict {"key": str, "target_level": int} oppure
      {"gid": int, "target_level": int} (Advanced Queue & Priority).

    target_level 0/assente = fino al massimo consentito (comportamento
    legacy). L'ordine della lista e' la priorita'.
    """
    steps: list[tuple[int, int]] = []
    for item in focus_list or []:
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
    """Field candidati dalla coda prioritaria building_focus (per step).

    Ogni step e' (gid, target_level): il builder lo salta quando il
    target e' raggiunto — campi risorsa: nessun campo sotto target;
    edifici: almeno un'istanza al target. Edificio assente -> nuova
    costruzione se i prerequisiti reggono.
    """
    vcfg = settings.village_config_for(state.village_name, state.village_id)
    if vcfg is None or not vcfg.building_focus:
        return []
    queued = queued_slot_ids(state)
    out: list[Field] = []
    for gid, target in _focus_steps(vcfg.building_focus):
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

    # 3. building_focus: override manuale utente
    it = _mk(
        "focus", focus_intents(state, settings),
        "Focus edifici manuale da dashboard",
    )
    if it:
        return it

    # 4. Ruolo (hammer/defensive/croplog/feeder + capitale)
    role = settings.village_role_for(state.village_name, state.village_id)
    it = _mk(
        "role", role_intents(state, settings),
        f"Sviluppo infrastrutture ruolo '{role}'",
    )
    if it:
        return it

    # 5. Economia: campo piu' debole della risorsa scarsa
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
