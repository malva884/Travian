"""Preset edilizi completi per ruolo strategico.

Smart Role-Based Full-Building Presets: a ogni ruolo corrisponde una
coda prioritaria completa di step ``{"key", "target_level", "priority"}``
compatibile con ``VillageConfig.building_queue_plan`` — il planner
(``builder.focus_intents``) la consuma nell'ordine dato, saltando gli
step gia' raggiunti o con prerequisiti non soddisfatti.

Note di design:

- l'ordine tiene conto dei prerequisiti del catalogo T4.x: gli
  sbloccatori (es. Caserma -> Accademia, Accademia -> Scuderia/Officina)
  precedono sempre i dipendenti, anche quando la priorita' strategica
  suggerirebbe il contrario — gli step irraggiungibili vengono comunque
  saltati dal planner, ma un ordine sensato riduce i cicli a vuoto;
- dove la spec indica un intervallo (es. "18-20") si usa il valore piu'
  alto coerente col ruolo;
- "residence_palace" e "city_wall" sono chiavi a gruppi di gid
  alternativi: il builder sceglie la variante gia' presente o quella
  compatibile con la tribu' (mura) / con la capitale (Palazzo);
- i preset sono template: l'utente resta libero di modificare,
  riordinare o eliminare ogni step dalla dashboard.
"""

from __future__ import annotations

# Ruolo -> lista ordinata di (chiave focus, livello target).
# Chiavi = quelle canoniche della dashboard (FOCUS_KEY_GIDS).
ROLE_PRESETS: dict[str, list[tuple[str, int]]] = {
    # Apertura server / pioniere 15c (Culture Points Booster, spec):
    # Fase 1 - base economica e sblocco Residenza; Fase 2 - edifici ad
    # alto rendimento PC (Ambasciata/Mercato/Municipio) e Municipio per
    # le Piccole Feste; Fase 3 - consolidamento produttivo e difesa.
    # Rally Point/Caserma restano prima dell'Accademia (prerequisiti).
    "early_cropper_founder": [
        # Fase 1 — Base Economica & Sblocco Residenza
        ("woodcutter", 3),
        ("clay_pit", 3),
        ("iron_mine", 3),
        ("cropland", 3),
        ("warehouse", 7),         # storage per il costo dei 3 coloni
        ("granary", 7),
        ("main_building", 5),     # prerequisito Residenza
        ("residence", 10),        # slot addestramento 3 coloni
        # Fase 2 — Boost Punti Cultura passivi (costo/PC ottimo)
        ("embassy", 8),
        ("marketplace", 10),      # PC + ricezione risorse dai feeder
        ("main_building", 10),    # sblocca il Municipio
        ("rally_point", 1),       # prerequisito Caserma
        ("barracks", 3),          # prerequisito Accademia
        ("academy", 10),          # prerequisito Municipio
        ("town_hall", 3),         # sblocca le Piccole Feste
        # Fase 3 — Consolidamento Produttivo e difesa base
        ("woodcutter", 6),
        ("clay_pit", 6),
        ("iron_mine", 6),
        ("cropland", 6),
        ("city_wall", 5),
    ],
    # Villaggio offensivo: infrastrutture militari al massimo, storage
    # capienti per l'economia di guerra, mura solide ma non prioritarie.
    # Piazza del Torneo richiede Punto di Raduno 15 -> il target del
    # rally point e' 15 (non 10) per non bloccare lo step.
    "hammer": [
        ("main_building", 20),
        ("rally_point", 15),
        ("barracks", 20),
        ("academy", 10),          # sblocca Scuderia (5) e Officina (10)
        ("smithy", 20),
        ("stable", 20),
        ("workshop", 15),
        ("tournament_square", 20),
        ("warehouse", 20),
        ("granary", 20),
        ("city_wall", 15),
        ("woodcutter", 10),
        ("clay_pit", 10),
        ("iron_mine", 10),
        ("cropland", 10),
    ],
    # Presidio difensivo: mura al massimo (priorita' assoluta), poi la
    # catena militare difensiva; storage ed economia di supporto.
    "defensive": [
        ("main_building", 15),
        ("rally_point", 1),       # sblocca Caserma
        ("city_wall", 20),
        ("barracks", 20),
        ("academy", 5),           # sblocca Scuderia (con Fucina 3)
        ("smithy", 20),
        ("stable", 20),
        ("warehouse", 16),
        ("granary", 16),
        ("woodcutter", 10),
        ("clay_pit", 10),
        ("iron_mine", 10),
        ("cropland", 10),
    ],
    # Rifornitore economico: campi e mercato al top per spedire il
    # surplus all'impero; difesa e caserma minime di presidio.
    "feeder": [
        ("woodcutter", 12),
        ("clay_pit", 12),
        ("iron_mine", 12),
        ("cropland", 12),
        ("main_building", 10),
        ("warehouse", 18),
        ("granary", 18),
        ("marketplace", 20),
        ("rally_point", 1),       # sblocca Caserma
        ("city_wall", 10),
        ("barracks", 5),
    ],
    # Capitale cerealicola (9c/15c): grano e moltiplicatori al massimo,
    # grandi depositi, mura difensive.
    "croplog": [
        ("cropland", 15),
        ("main_building", 20),
        ("granary", 20),
        ("grain_mill", 5),
        ("bakery", 5),
        ("warehouse", 20),
        ("city_wall", 15),
        ("woodcutter", 10),
        ("clay_pit", 10),
        ("iron_mine", 10),
    ],
    # Espansione / treno di conquista: Accademia e Residenza/Palazzo
    # per la ricerca e l'addestramento dei nobili, Municipio per le
    # feste grandi (punti cultura).
    "chiefing": [
        ("main_building", 15),
        ("rally_point", 1),       # sblocca Caserma
        ("barracks", 3),          # sblocca Accademia
        ("academy", 20),
        ("residence_palace", 20), # Residenza, o Palazzo in capitale
        ("town_hall", 10),
        ("warehouse", 18),
        ("granary", 18),
        ("city_wall", 15),
    ],
}

# Alias dei nomi di ruolo accettati -> chiave preset.
_ROLE_ALIASES: dict[str, str] = {
    "capital": "croplog",
    "cropper": "croplog",
    "croplog_capital": "croplog",
    "expansion": "chiefing",
    "reclaim": "chiefing",
    "noble": "chiefing",
    "defence": "defensive",
    "shield": "defensive",
    "anvil": "defensive",
    "economy": "feeder",
    "founder": "early_cropper_founder",
    "pioneer": "early_cropper_founder",
    "early_cropper": "early_cropper_founder",
    "early_founder": "early_cropper_founder",
}


# ---------------------------------------------------------------------------
# Slot truppe astratti -> unita' per tribu' (chiave tN)
# ---------------------------------------------------------------------------

# I preset sono tribu'-agnostici: gli slot astratti vengono risolti nella
# chiave tN corretta a runtime (tribu' da config o rilevata) oppure a
# tempo di applicazione dalla dashboard (seleziona tribu' account).
TRIBE_UNIT_SLOTS: dict[str, dict[str, str]] = {
    # slot           romans      teutons     gauls
    "infantry_base": {"romans": "t1", "teutons": "t11", "gauls": "t21"},
    "infantry_off":  {"romans": "t3", "teutons": "t13", "gauls": "t22"},
    "infantry_def":  {"romans": "t2", "teutons": "t12", "gauls": "t21"},
    "scout":         {"romans": "t4", "teutons": "t14", "gauls": "t23"},
    "cavalry_off":   {"romans": "t6", "teutons": "t16", "gauls": "t26"},
    "cavalry_def":   {"romans": "t5", "teutons": "t15", "gauls": "t25"},
    "ram":           {"romans": "t7", "teutons": "t17", "gauls": "t27"},
    "catapult":      {"romans": "t8", "teutons": "t18", "gauls": "t28"},
    "chief":         {"romans": "t9", "teutons": "t19", "gauls": "t29"},
    "settler":       {"romans": "t10", "teutons": "t20", "gauls": "t30"},
}

# Chiavi del preset replenishment (schema spec) -> slot astratto.
# 'settlers_target' e' gestito dal modulo espansione (TRAIN_SETTLERS in
# Residenza/Palazzo), non dalla caserma: escluso dal merge nei
# troop_targets di replenishment.
_TROOP_PRESET_SLOT_KEYS: dict[str, str | None] = {
    "base_infantry_target": "infantry_base",
    "offensive_infantry_target": "infantry_off",
    "defensive_infantry_target": "infantry_def",
    "offensive_cavalry_target": "cavalry_off",
    "defensive_cavalry_target": "cavalry_def",
    "settlers_target": None,
}

# Preset Smart Troop Replenishment per ruolo (schema JSON della spec).
# Solo il founder ha target numerici espliciti dalla spec.
TROOP_REPLENISHMENT_PRESETS: dict[str, dict[str, int]] = {
    "early_cropper_founder": {
        "base_infantry_target": 80,
        "settlers_target": 3,
    },
}

# Slot truppe sbloccati come allowed/priority units per ruolo.
ALLOWED_UNIT_SLOTS: dict[str, list[str]] = {
    "early_cropper_founder": ["infantry_base"],
    "hammer": ["infantry_off", "cavalry_off", "ram", "catapult"],
    "defensive": ["infantry_def", "cavalry_def"],
    "feeder": ["infantry_base"],
    "croplog": [],
    "chiefing": ["chief", "settler"],
}

# Preset Fucina (smithy_auto_upgrade): slot unita' da potenziare.
SMITHY_PRESETS: dict[str, dict] = {
    "hammer": {
        "enabled": True,
        "target_level": 20,
        "priority_units": ["infantry_off", "cavalry_off", "ram", "catapult"],
    },
    "defensive": {
        "enabled": True,
        "target_level": 20,
        "priority_units": ["infantry_def", "cavalry_def"],
    },
}

_SMITHY_DEFAULT = {"enabled": False, "target_level": 20, "priority_units": []}


_TRIBES = ("romans", "teutons", "gauls")


def detect_tribe_from_keys(keys) -> str:
    """Deduce la tribu' da chiavi unita' 'tN'/'uN' (sensori/config)."""
    for key in keys or []:
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


def detect_tribe_from_labels(labels) -> str:
    """Deduce la tribu' dai nomi italiani delle unita' (label dashboard)."""
    try:
        from .models import STATIC_TROOP_LABELS  # import ritardato: no cicli
    except Exception:  # noqa: BLE001
        return ""
    lookup = {v.lower(): k for k, v in STATIC_TROOP_LABELS.items()}
    for label in labels or []:
        tkey = lookup.get(str(label).strip().lower())
        if tkey:
            found = detect_tribe_from_keys([tkey])
            if found:
                return found
    return ""


def resolve_troop_slot(slot: str, tribe: str) -> str:
    """Risolve uno slot astratto ('infantry_off') in chiave tN per tribu'.

    Accetta anche chiavi gia' risolte ('t5', 'u11'): restituite in forma
    'tN'. Ritorna '' se non risolvibile.
    """
    s = (slot or "").strip().lower()
    if not s:
        return ""
    direct = detect_tribe_from_keys([s])
    if direct and s[0] in "ut" and s.lstrip("ut").isdigit():
        return "t" + s.lstrip("ut")
    mapping = TRIBE_UNIT_SLOTS.get(s)
    if mapping and tribe in _TRIBES:
        return mapping[tribe]
    return ""


def resolve_troop_labels(slots, tribe: str) -> list[str]:
    """Slot astratti -> nomi italiani delle unita' (per allowed_units UI)."""
    try:
        from .models import STATIC_TROOP_LABELS
    except Exception:  # noqa: BLE001
        return [str(s) for s in slots or []]
    out: list[str] = []
    for slot in slots or []:
        tkey = resolve_troop_slot(str(slot), tribe)
        out.append(STATIC_TROOP_LABELS.get(tkey, str(slot)))
    return out


def troop_preset_for_role(role: str) -> dict[str, int]:
    """Preset replenishment del ruolo (schema spec, slot-agnostico)."""
    base = TROOP_REPLENISHMENT_PRESETS.get(resolve_role(role))
    return dict(base) if base else {}


def resolve_troop_preset(
    preset: dict, tribe: str
) -> dict[str, int]:
    """Preset replenishment -> {tN: qty} mergiabile in troop_targets.

    'settlers_target' e le chiavi non mappate vengono ignorate (i coloni
    sono gestiti dal modulo espansione in Residenza/Palazzo).
    """
    out: dict[str, int] = {}
    for key, qty in (preset or {}).items():
        try:
            qty_i = int(qty or 0)
        except (TypeError, ValueError):
            continue
        if qty_i <= 0:
            continue
        slot = _TROOP_PRESET_SLOT_KEYS.get(str(key))
        if slot is None:
            continue  # coloni o chiave sconosciuta
        tkey = resolve_troop_slot(slot, tribe)
        if tkey:
            out[tkey] = qty_i
    return out


def allowed_unit_slots_for_role(role: str) -> list[str]:
    """Slot truppe sbloccate dal ruolo (per allowed_units)."""
    return list(ALLOWED_UNIT_SLOTS.get(resolve_role(role)) or [])


def smithy_preset_for_role(role: str) -> dict:
    """Preset Fucina del ruolo (schema spec smithy_auto_upgrade)."""
    base = SMITHY_PRESETS.get(resolve_role(role))
    return dict(base) if base else dict(_SMITHY_DEFAULT)


def resolve_role(role: str) -> str:
    """Normalizza un nome di ruolo (alias inclusi) verso la chiave preset."""
    norm = (role or "").strip().lower()
    return _ROLE_ALIASES.get(norm, norm)


def preset_for_role(role: str) -> list[dict]:
    """Coda prioritaria completa del ruolo, pronta per building_queue_plan.

    Ritorna una lista di step ``{"key", "target_level", "priority"}``
    (priority 1-based, ordine = posizione nella coda). Lista vuota per
    ruoli senza preset (es. "standard").
    """
    base = ROLE_PRESETS.get(resolve_role(role))
    if not base:
        return []
    return [
        {"key": key, "target_level": int(target), "priority": pos + 1}
        for pos, (key, target) in enumerate(base)
    ]


def roles_with_preset() -> list[str]:
    """Ruoli per cui esiste un preset applicabile."""
    return sorted(ROLE_PRESETS)


# Alias esposti per la dashboard (nomi parlanti nella config salvata).
DEFAULT_SMITHY_PRESET = dict(_SMITHY_DEFAULT)
