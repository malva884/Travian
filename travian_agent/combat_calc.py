"""Combat Calculator puro — Smart Pre-Raid Intelligence (Custom Farm Targets).

Nessuna dipendenza UI/browser: solo matematica di combattimento Travian.

Contiene:
1. ``NATURE_STATS`` — tabella delle bestie della Natura (u31..u40) con
   attacco, difesa vs fanteria e difesa vs cavalleria, piu' gli alias
   multi-lingua usati dal parser della scheda tile mappa.
2. ``min_troops_for_oasis`` — contingente minimo per pulire un'oasi
   mantenendo le perdite stimate <= ``max_loss_pct`` (0 = "nessuna
   perdita intera" secondo la formula Travian winner-losses
   ``perdite = ceil(N * (D/A)^1.5)``).
3. ``troops_for_loot`` — quantita' di unita' raider per asportare un
   bottino in base alla capacita' di trasporto (formula
   ``min(ceil(L/C), disponibili, cap)`` della spec).
4. Mappatura esploratori per tribu' (``scout_unit_key``).

Le statistiche delle truppe giocatore sono riusate da
``ai_scout_brain.TROOP_STATS`` (import ritardato dentro ``_troop_stats``
per evitare cicli di import e mantenere il modulo leggero).
"""

from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bestie della Natura (u31..u40): attacco / difesa vs fanteria / difesa vs
# cavalleria. Valori ufficiali Travian T4.x.
# ---------------------------------------------------------------------------
NATURE_STATS: dict[str, dict[str, int]] = {
    "u31": {"att": 10,  "def_inf": 25,  "def_cav": 20},   # Ratto
    "u32": {"att": 20,  "def_inf": 35,  "def_cav": 40},   # Ragno
    "u33": {"att": 60,  "def_inf": 40,  "def_cav": 60},   # Serpente
    "u34": {"att": 80,  "def_inf": 66,  "def_cav": 70},   # Pipistrello
    "u35": {"att": 50,  "def_inf": 70,  "def_cav": 33},   # Cinghiale
    "u36": {"att": 100, "def_inf": 80,  "def_cav": 70},   # Lupo
    "u37": {"att": 250, "def_inf": 140, "def_cav": 200},  # Orso
    "u38": {"att": 450, "def_inf": 380, "def_cav": 240},  # Coccodrillo
    "u39": {"att": 200, "def_inf": 170, "def_cav": 250},  # Tigre
    "u40": {"att": 600, "def_inf": 440, "def_cav": 520},  # Elefante
}

# Alias nome animale -> chiave 'uN'. Copre italiano/inglese/tedesco
# (le schede tile cambiano lingua col server) e le chiavi 'u3N'/'t3N'
# grezze dei tooltip. I valori ripetuti servono solo a normalizzare:
# duplicati tra lingue non generano ambiguita' (stesso uN).
NATURE_ALIASES: dict[str, str] = {
    "u31": "u31", "t31": "u31",
    "ratto": "u31", "ratti": "u31", "rat": "u31", "rats": "u31",
    "ratte": "u31", "ratten": "u31",
    "u32": "u32", "t32": "u32",
    "ragno": "u32", "ragni": "u32", "spider": "u32", "spiders": "u32",
    "spinne": "u32", "spinnen": "u32",
    "u33": "u33", "t33": "u33",
    "serpente": "u33", "serpenti": "u33", "snake": "u33", "snakes": "u33",
    "schlange": "u33", "schlangen": "u33",
    "u34": "u34", "t34": "u34",
    "pipistrello": "u34", "pipistrelli": "u34", "bat": "u34", "bats": "u34",
    "fledermaus": "u34", "fledermause": "u34", "fledermäuse": "u34",
    "u35": "u35", "t35": "u35",
    "cinghiale": "u35", "cinghiali": "u35", "wild boar": "u35",
    "boar": "u35", "boars": "u35", "wildschwein": "u35",
    "wildschweine": "u35",
    "u36": "u36", "t36": "u36",
    "lupo": "u36", "lupi": "u36", "wolf": "u36", "wolves": "u36",
    "wolfe": "u36", "wölfe": "u36",
    "u37": "u37", "t37": "u37",
    "orso": "u37", "orsi": "u37", "bear": "u37", "bears": "u37",
    "bar": "u37", "bär": "u37", "baren": "u37", "bären": "u37",
    "u38": "u38", "t38": "u38",
    "coccodrillo": "u38", "coccodrilli": "u38", "crocodile": "u38",
    "crocodiles": "u38", "krokodil": "u38", "krokodile": "u38",
    "u39": "u39", "t39": "u39",
    "tigre": "u39", "tigri": "u39", "tiger": "u39", "tigers": "u39",
    "u40": "u40", "t40": "u40",
    "elefante": "u40", "elefanti": "u40", "elephant": "u40",
    "elephants": "u40", "elefant": "u40", "elefanten": "u40",
}

# Nomi brevi per i messaggi di log/skip
NATURE_LABELS: dict[str, str] = {
    "u31": "ratti", "u32": "ragni", "u33": "serpenti",
    "u34": "pipistrelli", "u35": "cinghiali", "u36": "lupi",
    "u37": "orsi", "u38": "coccodrilli", "u39": "tigri",
    "u40": "elefanti",
}

# Esploratori per tribu' (chiavi 'uN' del sensore truppe)
SCOUT_UNITS_BY_TRIBE: dict[str, str] = {
    "romans": "u4", "romani": "u4", "roman": "u4",       # Emissario a cavallo
    "teutons": "u14", "teutoni": "u14", "teuton": "u14",  # Esploratore
    "gauls": "u23", "galli": "u23", "gaul": "u23",       # Ricognitore
    # Estendibile: Unni/Egizi/Spartani usano il loro slot esploratore.
}
# Qualunque chiave esploratore, per il fallback senza tribu' nota.
ALL_SCOUT_UNITS: tuple[str, ...] = ("u4", "u14", "u23")

# Unita' a cavallo (indicizzate 'tN'): la difesa natura rilevante e'
# def_cav. Include gli esploratori a cavallo (t4, t23).
_CAVALRY_T = {"t4", "t5", "t6", "t15", "t16", "t23", "t24", "t25", "t26"}

# Risorse contate nel bottino totale (ordine stabile per i log)
_LOOT_KEYS = ("wood", "clay", "iron", "crop")


def _troop_stats() -> dict[str, dict[str, int]]:
    """Tabella statistiche truppe giocatore (da ai_scout_brain)."""
    try:
        from .ai_scout_brain import TROOP_STATS  # import ritardato
        return TROOP_STATS
    except Exception:  # noqa: BLE001
        return {}


def normalize_unit_key(key: str) -> str:
    """'u5'/'unit5'/'t5' -> 't5'."""
    k = str(key or "").lower().strip()
    if k.startswith("unit"):
        k = "u" + k[4:]
    if k.startswith("u") and k[1:].isdigit():
        return "t" + k[1:]
    return k


def normalize_animal(name: str) -> str:
    """Nome animale localizzato ('Ragni', 'spider', 'u32') -> 'u32'."""
    k = str(name or "").strip().lower()
    return NATURE_ALIASES.get(k, k if k in NATURE_STATS else "")


def scout_unit_key(tribe: str) -> str:
    """Chiave 'uN' dell'esploratore per la tribu' ('romans' -> 'u4')."""
    return SCOUT_UNITS_BY_TRIBE.get(str(tribe or "").strip().lower(), "")


def unit_carry(unit_key: str) -> int:
    """Capacita' di trasporto bottino dell'unita' ('t5'/'u5' -> 100)."""
    stats = _troop_stats().get(normalize_unit_key(unit_key)) or {}
    return int(stats.get("carry") or 0)


def unit_attack(unit_key: str) -> int:
    """Attacco base dell'unita' ('t3'/'u3' -> 70)."""
    stats = _troop_stats().get(normalize_unit_key(unit_key)) or {}
    return int(stats.get("att") or 0)


def is_cavalry(unit_key: str) -> bool:
    return normalize_unit_key(unit_key) in _CAVALRY_T


def normalize_nature(nature_troops: dict | None) -> dict[str, int]:
    """Normalizza {'ragno': 19, 'u33': 11} -> {'u32': 19, 'u33': 11}."""
    out: dict[str, int] = {}
    for name, qty in (nature_troops or {}).items():
        try:
            q = int(qty)
        except (TypeError, ValueError):
            continue
        if q <= 0:
            continue
        key = normalize_animal(str(name))
        if key:
            out[key] = out.get(key, 0) + q
    return out


def nature_summary(nature_troops: dict | None) -> str:
    """'19 ragni, 11 serpenti' — per i messaggi di skip in last_outcome."""
    norm = normalize_nature(nature_troops)
    return ", ".join(
        f"{q} {NATURE_LABELS.get(k, k)}" for k, q in sorted(norm.items())
    )


def nature_defense(nature_troops: dict | None) -> dict[str, int]:
    """Forza difensiva totale delle bestie: {'inf': D_inf, 'cav': D_cav}."""
    d_inf = d_cav = 0
    for key, qty in normalize_nature(nature_troops).items():
        st = NATURE_STATS.get(key) or {}
        d_inf += int(st.get("def_inf") or 0) * qty
        d_cav += int(st.get("def_cav") or 0) * qty
    return {"inf": d_inf, "cav": d_cav}


def estimate_attacker_losses(
    n_troops: int,
    unit_key: str,
    defenders: dict[str, int] | float | int,
) -> int:
    """Perdite stimate dell'attaccante vincitore (formula Travian).

    ``perdite = ceil(N * (D/A)^1.5)`` dove D e' la difesa rilevante
    (vs fanteria o vs cavalleria) e A = attacco * N. ``defenders`` puo'
    essere il dict di ``nature_defense`` o uno scalare.
    """
    return min(
        n_troops,
        math.floor(_raw_losses(n_troops, unit_key, defenders) + 0.5),
    )


def _raw_losses(
    n_troops: int,
    unit_key: str,
    defenders: dict[str, int] | float | int,
) -> float:
    """Perdite continue N*(D/A)^1.5 prima dell'arrotondamento a unita'."""
    if n_troops <= 0:
        return 0.0
    att = unit_attack(unit_key)
    if att <= 0:
        return float(n_troops)
    if isinstance(defenders, dict):
        d = float(
            defenders.get("cav" if is_cavalry(unit_key) else "inf") or 0
        )
    else:
        d = float(defenders or 0)
    if d <= 0:
        return 0.0
    a = att * n_troops
    return n_troops * ((d / a) ** 1.5)


def _min_for_zero_losses(unit_key: str, defenders: dict) -> int:
    """N minimo per non perdere nemmeno un'unita' intera.

    Condizione: stima continua < 0.5 unita' (arrotondata a 0 perdite):
    ``N*(D/A)^1.5 < 0.5`` con ``A = N*att``  <=>  ``N > 4*(D/att)^3``.
    La scansione parte dal bound teorico e copre la precisione float.
    """
    d = float(defenders.get("cav" if is_cavalry(unit_key) else "inf") or 0)
    att = unit_attack(unit_key)
    if d <= 0 or att <= 0:
        return 0
    bound = max(2, math.ceil(4.0 * (d / att) ** 3) + 4)
    n = max(1, int(math.floor(4.0 * (d / att) ** 3)) - 1)
    while n <= bound:
        if _raw_losses(n, unit_key, defenders) < 0.5:
            return n
        n += 1
    return 0  # irraggiungibile (losses -> 0 per n -> inf)


def _min_for_loss_pct(unit_key: str, defenders: dict, max_loss_pct: float) -> int:
    """N minimo per perdite stimate <= max_loss_pct% del contingente.

    Per pct >= 1 si usa il bound teorico piu' il +20% di margine e si
    scansiona; per 0 < pct < 1 serve almeno ceil(100/pct) unita'
    (una perdita su N supera comunque la soglia se N troppo piccolo).
    """
    d = float(defenders.get("cav" if is_cavalry(unit_key) else "inf") or 0)
    att = unit_attack(unit_key)
    if d <= 0 or att <= 0:
        return 0
    pct = max(0.01, float(max_loss_pct))
    # vittoria garantita: A > D * 1.05; partenza conservativa
    n = max(1, math.ceil(d * 1.05 / att))
    floor = math.ceil(100.0 / pct)  # 1 perdita su N <= pct%
    n = max(n, min(floor, 10000))
    for _ in range(20000):
        est = estimate_attacker_losses(n, unit_key, defenders)
        if est <= (n * pct / 100.0):
            return n
        if n > 100000:
            break
        n += 1
    return 0


def min_troops_for_oasis(
    nature_troops: dict | None,
    available: dict[str, int],
    preferred_unit: str = "",
    max_loss_pct: float = 0.0,
    cap: int = 0,
) -> tuple[str, int]:
    """Contingente minimo per pulire l'oasi con perdite <= soglia.

    ``available``: truppe a casa {'uN': qta} (dal sensore).
    ``preferred_unit``: 'tN'/'uN' gia' normalizzabile; vuoto = migliore
    unita' offensiva disponibile (minimo contingente, a parita' la piu'
    capiente). ``cap``: limite superiore opzionale di unita' per raid.

    Ritorna ``(unit_key_tN, qty)``; ``("", 0)`` se nessuna unita'
    disponibile copre le bestie entro la soglia di perdite.
    """
    defenders = nature_defense(nature_troops)
    if not any(defenders.values()):
        return "", 0  # oasi vuota: nessun contingente richiesto

    stats = _troop_stats()
    candidates: list[str] = []
    if preferred_unit:
        t = normalize_unit_key(preferred_unit)
        if t in stats:
            candidates.append(t)
    else:
        for t, st in stats.items():
            u = "u" + t[1:]
            if int((st or {}).get("att") or 0) > 0 and int(
                (available or {}).get(u, 0) or 0
            ) > 0:
                candidates.append(t)

    best: tuple[int, int, str, int] | None = None  # (qty, -carry, t, losses)
    for t in candidates:
        u = "u" + t[1:]
        have = int((available or {}).get(u, 0) or 0)
        if have <= 0:
            continue
        if float(max_loss_pct) <= 0.0:
            need = _min_for_zero_losses(t, defenders)
        else:
            need = _min_for_loss_pct(t, defenders, max_loss_pct)
        if need <= 0:
            continue
        if cap > 0:
            need_cap = min(need, cap)
            # se il cap taglia il contingente, le perdite stimate devono
            # restare entro soglia altrimenti l'unita' non e' candidata
            if need_cap < need:
                est = estimate_attacker_losses(need_cap, t, defenders)
                if float(max_loss_pct) <= 0.0:
                    if est > 0:
                        continue
                elif est > need_cap * float(max_loss_pct) / 100.0:
                    continue
            need = need_cap
        if need > have:
            continue  # truppe insufficienti per questa unita'
        losses = estimate_attacker_losses(need, t, defenders)
        rank = (need, -unit_carry(t), t, losses)
        if best is None or rank < (best[0], best[1], best[2], best[3]):
            best = rank

    if best is None:
        return "", 0
    return best[2], best[0]


def total_loot(scouted_resources: dict | None) -> int:
    """Bottino totale asportabile: legno+argilla+ferro+grano."""
    if not scouted_resources:
        return 0
    tot = 0
    for k in _LOOT_KEYS:
        try:
            tot += int(scouted_resources.get(k) or 0)
        except (TypeError, ValueError):
            continue
    return max(0, tot)


def troops_for_loot(
    loot_total: int,
    unit_key: str,
    available: int,
    max_troops_per_raid: int = 0,
) -> int:
    """Unita' raider necessarie per asportare ``loot_total``.

    Formula spec: ``min(ceil(L / C_unit), truppe_disponibili, cap)``.
    Ritorna 0 se l'unita' non trasporta (carry=0), se il bottino e'
    nullo o se non ci sono unita' disponibili.
    """
    carry = unit_carry(unit_key)
    if carry <= 0 or loot_total <= 0 or available <= 0:
        return 0
    need = math.ceil(loot_total / carry)
    n = min(need, int(available))
    if max_troops_per_raid > 0:
        n = min(n, int(max_troops_per_raid))
    return max(0, n)
