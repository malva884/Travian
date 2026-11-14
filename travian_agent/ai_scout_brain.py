"""Modulo di Intelligence e Attacchi Intelligenti con AI (ai_scout_brain.py).

Fornisce:
1. Global Power Assessment: calcolo della potenza militare, resa economica
   e saturazione dell'intero impero multi-villaggio.
2. Radar & RC Parser Management: indicizzazione dei target vicini,
   storico saccheggi e auto-blacklisting dei villaggi difesi.
3. Gemini Strategic Decision Maker: invio del payload ad AI (con fallback
   euristico) per selezionare le coordinate e il quantitativo esatto di truppe.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

from . import db
from .config import BASE_DIR, Settings
from .hero import is_hero_combat_ready
from .models import (
    AccountPowerAssessment,
    Action,
    ActionType,
    GameState,
    Resources,
    ResourceType,
    ScoutTarget,
    Village,
)

if TYPE_CHECKING:
    from .sensors import GameObserver

logger = logging.getLogger(__name__)

STATE_DIR = BASE_DIR / "state"

# --------------------------------------------------------------------------
# Parametri statici truppe Travian (Attacco, Difesa Fanteria/Cavalleria, Upkeep, Capacità Sacco)
# --------------------------------------------------------------------------
TROOP_STATS: dict[str, dict[str, int]] = {
    # Romani (t1..t10)
    "t1": {"att": 40, "def_inf": 35, "def_cav": 50, "upkeep": 1, "carry": 50, "speed": 6},   # Legionario
    "t2": {"att": 30, "def_inf": 65, "def_cav": 35, "upkeep": 1, "carry": 20, "speed": 5},   # Pretoriano
    "t3": {"att": 70, "def_inf": 40, "def_cav": 25, "upkeep": 1, "carry": 50, "speed": 7},   # Imperiano
    "t4": {"att": 0,  "def_inf": 20, "def_cav": 10, "upkeep": 2, "carry": 0,  "speed": 16},  # Emissario a cavallo
    "t5": {"att": 120, "def_inf": 65, "def_cav": 50, "upkeep": 2, "carry": 100, "speed": 14}, # Equites Imperatoris
    "t6": {"att": 180, "def_inf": 80, "def_cav": 105, "upkeep": 3, "carry": 70, "speed": 10}, # Equites Caesaris
    "t7": {"att": 50, "def_inf": 30, "def_cav": 105, "upkeep": 3, "carry": 0, "speed": 4},   # Ariete
    "t8": {"att": 75, "def_inf": 60, "def_cav": 10, "upkeep": 6, "carry": 0, "speed": 3},   # Catapulta da fuoco
    "t9": {"att": 40, "def_inf": 60, "def_cav": 40, "upkeep": 5, "carry": 0, "speed": 4},   # Senatore
    "t10": {"att": 10, "def_inf": 80, "def_cav": 80, "upkeep": 1, "carry": 3000, "speed": 5}, # Colono

    # Teutoni (t11..t20)
    "t11": {"att": 40, "def_inf": 20, "def_cav": 5, "upkeep": 1, "carry": 60, "speed": 7},   # Combattente (Clubswinger)
    "t12": {"att": 10, "def_inf": 35, "def_cav": 60, "upkeep": 1, "carry": 40, "speed": 7},  # Alabardiere
    "t13": {"att": 60, "def_inf": 30, "def_cav": 30, "upkeep": 1, "carry": 50, "speed": 6},  # Guerriero con ascia
    "t14": {"att": 0, "def_inf": 10, "def_cav": 5, "upkeep": 1, "carry": 0, "speed": 9},     # Esploratore
    "t15": {"att": 55, "def_inf": 100, "def_cav": 40, "upkeep": 2, "carry": 110, "speed": 10}, # Paladino
    "t16": {"att": 150, "def_inf": 50, "def_cav": 75, "upkeep": 3, "carry": 80, "speed": 9}, # Cavaliere teutonico
    "t17": {"att": 65, "def_inf": 30, "def_cav": 80, "upkeep": 3, "carry": 0, "speed": 4},  # Ariete
    "t18": {"att": 50, "def_inf": 60, "def_cav": 10, "upkeep": 6, "carry": 0, "speed": 3},  # Catapulta
    "t19": {"att": 40, "def_inf": 60, "def_cav": 40, "upkeep": 5, "carry": 0, "speed": 4},  # Comandante
    "t20": {"att": 10, "def_inf": 80, "def_cav": 80, "upkeep": 1, "carry": 3000, "speed": 5}, # Colono

    # Galli (t21..t30)
    "t21": {"att": 15, "def_inf": 40, "def_cav": 50, "upkeep": 1, "carry": 35, "speed": 7},  # Falange
    "t22": {"att": 65, "def_inf": 35, "def_cav": 20, "upkeep": 1, "carry": 45, "speed": 6},  # Spadaccino
    "t23": {"att": 0, "def_inf": 20, "def_cav": 10, "upkeep": 2, "carry": 0, "speed": 17},   # Esploratore
    "t24": {"att": 90, "def_inf": 25, "def_cav": 40, "upkeep": 2, "carry": 75, "speed": 19},  # Fulmine di Teutates (TT)
    "t25": {"att": 45, "def_inf": 115, "def_cav": 55, "upkeep": 2, "carry": 35, "speed": 16}, # Cavaliere Druido
    "t26": {"att": 140, "def_inf": 60, "def_cav": 165, "upkeep": 3, "carry": 65, "speed": 13}, # Haeduan
    "t27": {"att": 50, "def_inf": 30, "def_cav": 105, "upkeep": 3, "carry": 0, "speed": 4},  # Ariete
    "t28": {"att": 70, "def_inf": 45, "def_cav": 10, "upkeep": 6, "carry": 0, "speed": 3},  # Trabucco
    "t29": {"att": 40, "def_inf": 50, "def_cav": 50, "upkeep": 5, "carry": 0, "speed": 5},  # Capotribù
    "t30": {"att": 10, "def_inf": 80, "def_cav": 80, "upkeep": 1, "carry": 3000, "speed": 5}, # Colono
}

# Alias di normalizzazione u1..u30 -> t1..t30
_U_TO_T = {f"u{i}": f"t{i}" for i in range(1, 31)}


def normalize_unit_key(key: str) -> str:
    """Normalizza 'u1', 't1', 'unit1' nel formato 't1'."""
    k = key.lower().strip()
    if k in _U_TO_T:
        return _U_TO_T[k]
    if k.startswith("unit"):
        return f"t{k[4:]}"
    return k


def calculate_distance(x1: int, y1: int, x2: int, y2: int) -> float:
    """Calcola la distanza euclidea tra due coordinate mappa."""
    return math.hypot(x2 - x1, y2 - y1)


# --------------------------------------------------------------------------
# 1. Global Power Assessment
# --------------------------------------------------------------------------

def assess_account_power(
    settings: Settings,
    villages: list[Village],
    states_by_village: dict[str, GameState],
    troops_by_village: dict[str, dict[str, int]] | None = None,
) -> AccountPowerAssessment:
    """Calcola l'indice di potenza militare ed economica globale dell'intero impero.

    Aggrega i dati di tutti i villaggi (es. Hipponion, Vibo Valentia, Pizzo, Rovato, Bivona).
    """
    troops_by_village = troops_by_village or {}
    total_troops: dict[str, int] = {}
    military_power = 0
    total_hourly: dict[str, int] = {"wood": 0, "clay": 0, "iron": 0, "crop": 0}
    total_resources: dict[str, int] = {"wood": 0, "clay": 0, "iron": 0, "crop": 0}
    total_capacity: dict[str, int] = {"warehouse": 0, "granary": 0}
    total_crop_upkeep = 0
    summaries: list[dict] = []

    for v in villages:
        v_key = v.name or str(v.village_id)
        state = states_by_village.get(v_key)
        res = state.resources if state else Resources()
        v_troops = troops_by_village.get(v_key, state.troops_home if state else {})

        # Aggrega risorse attuali e capacità
        total_resources["wood"] += res.wood
        total_resources["clay"] += res.clay
        total_resources["iron"] += res.iron
        total_resources["crop"] += res.crop
        total_capacity["warehouse"] += res.warehouse_capacity
        total_capacity["granary"] += res.granary_capacity

        # Aggrega produzione oraria
        for rt, prod in res.production_per_hour.items():
            total_hourly[rt.value] = total_hourly.get(rt.value, 0) + prod

        # Calcolo forza truppe del villaggio
        v_mil_power = 0
        v_upkeep = 0
        for ukey, qty in v_troops.items():
            if qty <= 0:
                continue
            norm = normalize_unit_key(ukey)
            total_troops[norm] = total_troops.get(norm, 0) + qty
            stats = TROOP_STATS.get(norm, {"att": 40, "def_inf": 30, "def_cav": 30, "upkeep": 1})
            # Indice di potenza militare: somma di attacco + media difese pesata
            power_per_unit = stats["att"] + int((stats["def_inf"] + stats["def_cav"]) / 2)
            v_mil_power += power_per_unit * qty
            v_upkeep += stats.get("upkeep", 1) * qty

        military_power += v_mil_power
        total_crop_upkeep += v_upkeep

        summaries.append({
            "name": v.name,
            "id": v.village_id,
            "role": settings.village_role_for(v.name, v.village_id),
            "military_power": v_mil_power,
            "crop_upkeep": v_upkeep,
            "resources": {
                "wood": res.wood,
                "clay": res.clay,
                "iron": res.iron,
                "crop": res.crop,
            },
            "warehouse_capacity": res.warehouse_capacity,
            "granary_capacity": res.granary_capacity,
            "cp_ready": state.culture_points if state else 0,
            "settlers": state.settlers_home if state else 0,
        })

    # Net crop = produzione totale grano - upkeep di tutte le truppe
    net_crop = total_hourly.get("crop", 0) - total_crop_upkeep

    # Saturazione economica: rapporto tra scorte complessive e capienza totale magazzini
    total_stored = sum(total_resources.values())
    total_cap = (total_capacity["warehouse"] * 3) + total_capacity["granary"]
    saturation_pct = round((total_stored / max(1, total_cap)) * 100.0, 1)

    # Pronto per espansione se almeno un villaggio ha 3 coloni o CP richiesti
    ready_for_exp = any(
        s["settlers"] >= 3 or (s["cp_ready"] >= 2000 and s["role"] in ("capital", "croplog"))
        for s in summaries
    )

    return AccountPowerAssessment(
        military_power=military_power,
        total_troops=total_troops,
        net_crop_production=net_crop,
        total_hourly_production=total_hourly,
        total_resources=total_resources,
        total_capacity=total_capacity,
        storage_saturation_pct=saturation_pct,
        ready_for_expansion=ready_for_exp,
        village_summaries=summaries,
    )


# --------------------------------------------------------------------------
# 2. Integrazione Gemini API con Fallback Euristico
# --------------------------------------------------------------------------

def _extract_json_from_text(text: str) -> Any:
    """Estrae un oggetto/lista JSON puro anche se racchiuso in markdown fences."""
    cleaned = text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, re.IGNORECASE)
    if match:
        cleaned = match.group(1).strip()
    return json.loads(cleaned)


async def gemini_evaluate_farming_targets(
    account_status: dict,
    nearby_targets: list[dict],
    api_key: str = "",
    model_name: str = "gemini-3.5-flash-lite",
) -> list[dict]:
    """Usa Gemini per analizzare i target di saccheggio e decidere quali attaccare.

    In caso di indisponibilità dell'API o credenziali assenti, ricade
    istantaneamente su `heuristic_evaluate_farming_targets` per garantire
    continuità operativa al bot.
    """
    effective_api_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""

    if not effective_api_key:
        logger.debug("Nessuna GEMINI_API_KEY configurata: uso il motore euristico di fallback")
        return heuristic_evaluate_farming_targets(account_status, nearby_targets)

    prompt = f"""
Sei il cervello militare strategico di un account Travian Legends.
Stato attuale dell'account e delle truppe libere a casa:
{json.dumps(account_status, indent=2, ensure_ascii=False)}

Lista dei villaggi/oasi candidati nelle vicinanze con storico saccheggi e distanza:
{json.dumps(nearby_targets, indent=2, ensure_ascii=False)}

Compito:
1. Seleziona i target ottimali per massimizzare il ritorno di risorse senza rischiare perdite.
2. Escludi categoricamente target con difese accertate o con ratio bottino < 0.2.
3. Assegna il quantitativo esatto di truppe da inviare per ciascun target (es. truppe veloci o da sacco).
4. Restituisci ESCLUSIVAMENTE un array JSON puro conforme alla seguente struttura:
[
  {{
    "x": 12,
    "y": -34,
    "target_name": "Villaggio Abbandonato",
    "troops": {{"t1": 15}},
    "priority": 1,
    "expected_loot_ratio": 1.0,
    "include_hero": false,
    "hero_reason": "Pulizia oasi con belve | Alto bottino | Bonus attacco onda principale | vuoto",
    "reasoning": "Inattivo vicino, zero difese storiche, bottino pieno"
  }}
]

5. Se 'hero_available' e' true, valuta l'impiego dell'eroe (include_hero=true) SOLO
   per: oasi con difensori/belve (enemy_units > 0), villaggi ad alto bottino
   stimato o con mura deboli. Mai sui micro-raid ripetitivi standard.
"""

    # Modelli EOL (API v1beta deprecata): rimappati su un modello supportato
    _EOL_MODELS = {
        "gemini-1.5-flash", "gemini-1.5-flash-latest",
        "gemini-1.5-pro", "gemini-1.5-pro-latest", "gemini-pro",
    }
    if model_name in _EOL_MODELS:
        logger.info(
            "Modello Gemini '%s' non piu' supportato: rimappato su gemini-3.5-flash-lite",
            model_name,
        )
        model_name = "gemini-3.5-flash-lite"

    try:
        response_text = ""
        try:
            # SDK attuale (google-genai): supporta i modelli 2.x
            from google import genai as _genai  # type: ignore

            client = _genai.Client(api_key=effective_api_key)
            response = await client.aio.models.generate_content(
                model=model_name, contents=prompt
            )
            response_text = response.text or ""
        except ImportError:
            # Fallback legacy (google-generativeai, deprecato)
            import google.generativeai as _legacy  # type: ignore

            _legacy.configure(api_key=effective_api_key)
            model = _legacy.GenerativeModel(model_name)
            response = await model.generate_content_async(prompt)
            response_text = response.text or ""

        recommendations = _extract_json_from_text(response_text)
        if isinstance(recommendations, list):
            logger.info("Gemini AI Scout ha restituito %d raccomandazioni di raid", len(recommendations))
            return recommendations
    except Exception as e:
        logger.warning("Errore nella chiamata strategica a Gemini (%s): ricado sull'euristica", e)

    return heuristic_evaluate_farming_targets(account_status, nearby_targets)


def heuristic_evaluate_farming_targets(
    account_status: dict,
    nearby_targets: list[dict],
) -> list[dict]:
    """Algoritmo euristico deterministico: calcola il punteggio (rendimento/distanza/rischio).

    Usato come fallback infallibile o quando le chiamate AI sono spente.
    """
    troops_available = dict(account_status.get("troops_home") or {})
    max_targets = int(account_status.get("max_targets_per_wave", 5))

    # Individua l'unità da saccheggio preferita
    preferred_unit = "t1"
    for u in ("t11", "t24", "t5", "t1", "t22", "t13", "t15", "t6"):
        if troops_available.get(u, 0) > 0:
            preferred_unit = u
            break

    total_raiders = troops_available.get(preferred_unit, 0)
    if total_raiders <= 0:
        # Prende qualsiasi unità combattente disponibile
        for u, qty in troops_available.items():
            if qty > 0 and TROOP_STATS.get(normalize_unit_key(u), {}).get("att", 0) > 0:
                preferred_unit = normalize_unit_key(u)
                total_raiders = qty
                break

    if total_raiders <= 0:
        return []

    valid_targets: list[tuple[float, dict]] = []
    for t in nearby_targets:
        if t.get("is_blacklisted", False):
            continue
        dist = max(1.0, float(t.get("distance", 1.0)))
        loot_ratio = float(t.get("last_loot_ratio", 1.0))
        pop = max(2, int(t.get("population", 9)))
        losses = int(t.get("losses_recorded", 0))

        if losses > 0:
            continue

        # Punteggio = (rendimento stimato * popolazione) / (distanza ^ 1.2)
        score = (loot_ratio * pop) / (dist ** 1.2)
        valid_targets.append((score, t))

    valid_targets.sort(key=lambda x: x[0], reverse=True)
    selected = valid_targets[:max_targets]

    if not selected:
        return []

    # Distribuzione truppe proporzionale
    troops_per_raid = max(3, total_raiders // max(1, len(selected)))
    carry_cap = TROOP_STATS.get(preferred_unit, {}).get("carry", 50)

    recommendations: list[dict] = []
    remaining_troops = total_raiders

    for prio, (_, tgt) in enumerate(selected, start=1):
        if remaining_troops < 2:
            break
        # Calibra truppe in base alla popolazione del villaggio (stimando produzione oraria)
        est_res = int(tgt.get("population", 10)) * 60
        needed_troops = max(3, min(est_res // max(1, carry_cap), troops_per_raid))
        allocated = min(remaining_troops, needed_troops)
        remaining_troops -= allocated

        recommendations.append({
            "x": int(tgt["x"]),
            "y": int(tgt["y"]),
            "target_name": str(tgt.get("village_name", f"Target ({tgt['x']}|{tgt['y']})")),
            "troops": {preferred_unit: allocated},
            "priority": prio,
            "expected_loot_ratio": tgt.get("last_loot_ratio", 1.0),
            "reasoning": f"Target euristico: dist {tgt.get('distance', 0):.1f}, ratio {tgt.get('last_loot_ratio', 1.0):.2f}",
        })

    return recommendations


# --------------------------------------------------------------------------
# 2b. Smart Hero Combat Evaluator: matrice scenari per l'uso tattico dell'eroe
# --------------------------------------------------------------------------

# Soglia di velocita' (campi/h) che distingue cavalleria/esploratori dalla
# fanteria: usata per rilevare i "raid veloci" (gate 4 is_mounted).
_CAV_SPEED_MIN = 9

# Bottino stimato minimo (popolazione * produzione oraria grezza) oltre il
# quale un villaggio inattivo rientra nello Scenario C (raid ad alto valore).
_HIGH_LOOT_MIN_EST = 3000


def _is_fast_raid(troops: dict) -> bool:
    """True se la wave e' composta solo da unita' a cavallo (raid veloce)."""
    any_troop = False
    for key, qty in (troops or {}).items():
        if int(qty or 0) <= 0:
            continue
        any_troop = True
        stats = TROOP_STATS.get(normalize_unit_key(key))
        if not stats or int(stats.get("speed", 0)) < _CAV_SPEED_MIN:
            return False
    return any_troop


def evaluate_hero_use(
    target_info: dict,
    troops: dict,
    hero_state: dict,
    hero_cfg,
) -> tuple[bool, str]:
    """Decide se aggregare l'eroe a un singolo raid (matrice Scenari).

    - Scenario A: oasi con belve (enemy_units > 0) -> assorbe i danni.
    - Scenario C: villaggio inattivo ad alto bottino/difeso -> trasporto
      extra + abbattimento resistenza passiva.
    - Scenario D: micro-raid ripetitivo su farm pulita -> mai (default).

    I 4 hard gate di ``is_hero_combat_ready`` (casa, salute, avventure,
    cavalcatura su raid veloci) restano bloccanti in ogni scenario.

    ``target_info`` accetta sia le voci ``get_candidate_targets`` sia i
    record ``ScoutTarget`` serializzati. Ritorna ``(include, reason)``:
    su rifiuto ``reason`` spiega il gate/scenario che ha escluso l'eroe.
    """
    t = target_info or {}
    ready, why = is_hero_combat_ready(
        hero_state, hero_cfg, fast_raid=_is_fast_raid(troops)
    )
    if not ready:
        return False, f"gate: {why}"

    enemy_units = int(t.get("enemy_units") or 0)
    is_oasis = bool(t.get("is_oasis"))

    # Scenario A — pulizia oasi con belve: l'eroe assorbe i danni e fa XP.
    if is_oasis and enemy_units > 0:
        return True, "Pulizia oasi con belve"

    # Scenario C — target ad alto valore: difensori noti oppure bottino
    # stimato elevato / mura basse (villaggio inattivo maturo).
    est_loot = float(t.get("estimated_loot") or 0)
    if est_loot <= 0:
        est_loot = int(t.get("population") or 0) * 60
    if enemy_units > 0:
        return True, "Difensori rilevati: assorbimento danni"
    if not is_oasis and est_loot >= _HIGH_LOOT_MIN_EST:
        return True, "Alto bottino: trasporto extra dell'eroe"

    # Scenario D — raid di massa ripetitivo: l'eroe resta a casa.
    return False, "raid standard: eroe non impiegato"


# --------------------------------------------------------------------------
# 3. AIScoutBrain: Manager dello Stato, Radar e Blacklist
# --------------------------------------------------------------------------

@dataclass
class AIScoutBrain:
    """Manager di Intelligence per il raid dinamico e analisi report."""

    account_id: str
    state_path: Path | None = None
    targets: dict[str, ScoutTarget] = field(default_factory=dict)
    last_assessment: AccountPowerAssessment | None = None
    last_ai_recommendations: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._kv_key: str | None = None
        if self.state_path is None:
            safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in self.account_id)
            self._kv_key = f"scout_{safe}.json"
            self.state_path = STATE_DIR / self._kv_key
        self._load()

    def _target_key(self, x: int, y: int) -> str:
        return f"{x}_{y}"

    def _read_doc(self) -> dict | None:
        if self._kv_key is not None:
            text = db.kv_read(self._kv_key)
            return json.loads(text) if text else None
        if self.state_path and self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return None

    def _load(self) -> None:
        try:
            data = self._read_doc()
            if data:
                for item in data.get("targets", []):
                    t = ScoutTarget(
                        x=int(item["x"]),
                        y=int(item["y"]),
                        village_name=item.get("village_name", ""),
                        player_name=item.get("player_name", ""),
                        alliance=item.get("alliance", ""),
                        population=int(item.get("population", 0)),
                        distance=float(item.get("distance", 0.0)),
                        is_oasis=bool(item.get("is_oasis", False)),
                        last_loot_ratio=float(item.get("last_loot_ratio", 1.0)),
                        last_scout_ts=float(item.get("last_scout_ts", 0.0)),
                        losses_recorded=int(item.get("losses_recorded", 0)),
                        enemy_units=int(item.get("enemy_units", 0)),
                        is_blacklisted=bool(item.get("is_blacklisted", False)),
                        blacklist_until=float(item.get("blacklist_until", 0.0)),
                        notes=item.get("notes", ""),
                    )
                    self.targets[self._target_key(t.x, t.y)] = t
                self.last_ai_recommendations = list(data.get("last_ai_recommendations", []))
        except Exception:
            logger.debug("Caricamento stato scout fallito", exc_info=True)

    def _save(self) -> None:
        try:
            data = {
                "targets": [asdict(t) for t in self.targets.values()],
                "last_ai_recommendations": self.last_ai_recommendations,
                "updated_at": time.time(),
            }
            payload = json.dumps(data, indent=2, ensure_ascii=False)
            if self._kv_key is not None:
                db.kv_write(self._kv_key, payload)
                return
            assert self.state_path is not None
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(payload, encoding="utf-8")
        except Exception:
            logger.debug("Salvataggio stato scout fallito", exc_info=True)

    def update_targets_from_map(
        self,
        scanned_tiles: list[dict],
        origin_x: int,
        origin_y: int,
    ) -> None:
        """Aggiorna il database dei target con i tile rilevati dal sensore mappa."""
        now = time.time()
        for tile in scanned_tiles:
            x = int(tile.get("x", 0))
            y = int(tile.get("y", 0))
            k = self._target_key(x, y)
            dist = calculate_distance(origin_x, origin_y, x, y)

            if k in self.targets:
                existing = self.targets[k]
                existing.population = int(tile.get("population", existing.population))
                existing.village_name = tile.get("village_name", existing.village_name)
                existing.player_name = tile.get("player_name", existing.player_name)
                existing.alliance = tile.get("alliance", existing.alliance)
                existing.distance = dist
                existing.last_scout_ts = now
            else:
                self.targets[k] = ScoutTarget(
                    x=x,
                    y=y,
                    village_name=tile.get("village_name", f"Tile ({x}|{y})"),
                    player_name=tile.get("player_name", ""),
                    alliance=tile.get("alliance", ""),
                    population=int(tile.get("population", 0)),
                    distance=dist,
                    is_oasis=bool(tile.get("is_oasis", False)),
                    last_scout_ts=now,
                )
        self._save()

    def process_combat_reports(self, reports: list[dict], blacklist_hours: float = 12.0) -> None:
        """Elabora i report di combattimento: aggiorna il bottino o applica blacklist."""
        now = time.time()
        blacklist_ttl = blacklist_hours * 3600.0

        for r in reports:
            x = r.get("x")
            y = r.get("y")
            if x is None or y is None:
                continue
            k = self._target_key(int(x), int(y))
            target = self.targets.get(k)
            if not target:
                target = ScoutTarget(x=int(x), y=int(y), village_name=r.get("target_name", ""))
                self.targets[k] = target

            losses = int(r.get("losses", 0))
            defenders = int(r.get("defenders", 0))
            loot_ratio = float(r.get("loot_ratio", 1.0))

            target.last_loot_ratio = loot_ratio
            target.losses_recorded += losses
            target.enemy_units = defenders
            target.last_scout_ts = now

            # Regola di sicurezza: se compaiono difese o perdite, blacklist automatica
            if losses > 0 or defenders > 0:
                target.is_blacklisted = True
                target.blacklist_until = now + blacklist_ttl
                target.notes = f"Blacklistato per perdite ({losses}) / difese ({defenders}) il {time.strftime('%H:%M:%S')}"
                logger.warning("Target (%d|%d) BLACKLISTATO fino a +%.1fh: %s", target.x, target.y, blacklist_hours, target.notes)
            else:
                # Ripristina se la blacklist è scaduta e l'ultimo raid è andato liscio
                if target.is_blacklisted and now >= target.blacklist_until:
                    target.is_blacklisted = False
                    target.notes = "Blacklist rimossa dopo esito positivo"

        self._save()

    def get_candidate_targets(
        self,
        origin_x: int,
        origin_y: int,
        max_distance: float = 15.0,
        min_loot_ratio: float = 0.3,
    ) -> list[dict]:
        """Restituisce la lista di target validi non blacklistati ordinati per distanza."""
        now = time.time()
        candidates: list[dict] = []

        for t in self.targets.values():
            dist = calculate_distance(origin_x, origin_y, t.x, t.y)
            t.distance = dist

            # Verifica blacklist scaduta
            if t.is_blacklisted and now >= t.blacklist_until:
                t.is_blacklisted = False
                t.notes = ""

            if t.is_blacklisted:
                continue
            if dist > max_distance:
                continue
            if t.last_loot_ratio < min_loot_ratio and t.last_scout_ts > 0:
                continue

            candidates.append({
                "x": t.x,
                "y": t.y,
                "village_name": t.village_name,
                "player_name": t.player_name,
                "population": t.population,
                "distance": round(dist, 1),
                "is_oasis": t.is_oasis,
                "enemy_units": t.enemy_units,
                "last_loot_ratio": round(t.last_loot_ratio, 2),
                "losses_recorded": t.losses_recorded,
            })

        candidates.sort(key=lambda item: item["distance"])
        return candidates

    async def decide_raid_plan(
        self,
        origin_village: Village,
        troops_home: dict[str, int],
        settings: Settings,
    ) -> list[dict]:
        """Genera il piano di raid AI per il villaggio attivo."""
        vx = origin_village.x or 0
        vy = origin_village.y or 0
        cfg = settings.scout_brain

        candidates = self.get_candidate_targets(
            origin_x=vx,
            origin_y=vy,
            max_distance=float(cfg.scan_radius) * 2.0,
            min_loot_ratio=cfg.min_loot_ratio,
        )

        if not candidates:
            logger.info("[%s] AI Scout: nessun target candidato valido nel raggio %d", origin_village.name, cfg.scan_radius)
            return []

        account_status = {
            "origin_village": origin_village.name,
            "origin_coords": {"x": vx, "y": vy},
            "troops_home": troops_home,
            "max_travel_time_minutes": cfg.max_travel_time_minutes,
            "max_targets_per_wave": cfg.max_targets_per_wave,
        }

        recommendations = await gemini_evaluate_farming_targets(
            account_status=account_status,
            nearby_targets=candidates,
            api_key=cfg.gemini_api_key,
            model_name=cfg.gemini_model,
        )

        self.last_ai_recommendations = recommendations
        self._save()
        return recommendations

    def _hero_state(self) -> dict:
        """Snapshot eroe condiviso (kv hero_<acc>.json) per i hard gate.

        Scritto da HeroManager.run dopo ogni check DOM; restituisce {}
        se il modulo eroe non ha ancora salvato nulla.
        """
        try:
            safe = "".join(
                c if c.isalnum() or c in "._-" else "_" for c in self.account_id
            )
            raw = db.kv_read(f"hero_{safe}.json")
            data = json.loads(raw) if raw else {}
            return data if isinstance(data, dict) else {}
        except Exception:
            logger.debug("Lettura snapshot eroe fallita", exc_info=True)
            return {}
