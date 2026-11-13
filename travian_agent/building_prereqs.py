"""Catalogo dei prerequisiti edilizi Travian T4.x.

Il catalogo serve al planner dinamico (``builder.py``) per due scopi:

1. decidere se un edificio "potenziatore" (es. Forno) e' costruibile
   nell'area vuota solo quando i prerequisiti sono soddisfatti;
2. validare i candidati suggeriti dal ruolo / building_focus prima di
   proporre un upgrade, cosi' il brain non inseque edifici non
   costruibili (es. Grande Caserma fuori capitale).

Nota: per un edificio gia' esistente i prerequisiti sono per definizione
soddisfatti; il check interessa solo le aree vuote (gid == 0).
"""

from __future__ import annotations

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
    ResourceType,
)

# GID edifici interni (dorf2) — quelli non gia' definiti in models.py.
GID_WOODCUTTER = 1
GID_CLAY_PIT = 2
GID_IRON_MINE = 3
GID_CROPLAND = 4
GID_SAWMILL = 5
GID_BRICKYARD = 6
GID_IRON_FOUNDRY = 7
GID_GRAIN_MILL = 8
GID_BAKERY = 9
# 10 Magazzino, 11 Granaio, 16 Punto di Raduno, 17 Mercato,
# 19 Caserma, 20 Scuderia, 21 Officina, 25 Residenza, 26 Palazzo:
# gia' definiti in models.py
GID_SMITHY = 13
GID_TOURNAMENT_SQUARE = 14
GID_MAIN_BUILDING = 15
GID_EMBASSY = 18
GID_ACADEMY = 22
GID_CRANNY = 23
GID_TOWN_HALL = 24
GID_TREASURY = 27
GID_TRADE_OFFICE = 28
GID_GREAT_BARRACKS = 29
GID_GREAT_STABLE = 30
GID_CITY_WALL = 31       # mura romane
GID_EARTH_WALL = 32      # terrapieno teutonico
GID_PALISADE = 33        # staccionata gallica
GID_STONEMASON = 34
GID_BREWERY = 35         # solo Teutoni
GID_TRAPPER = 36         # solo Galli
GID_HERO_MANSION = 37
GID_GREAT_WAREHOUSE = 38  # solo villaggio Meraviglia
GID_GREAT_GRANARY = 39    # solo villaggio Meraviglia
GID_WONDER = 40
GID_HORSE_TROUGH = 41    # solo Romani
GID_WATERWORKS = 42      # solo Egizi (server speciali)

WALL_GIDS = (GID_CITY_WALL, GID_EARTH_WALL, GID_PALISADE)

# Etichette italiane per la preview (dashboard/piano upgrade).
GID_LABELS: dict[int, str] = {
    GID_WOODCUTTER: "Boschivo",
    GID_CLAY_PIT: "Cava di argilla",
    GID_IRON_MINE: "Miniera di ferro",
    GID_CROPLAND: "Campo di grano",
    GID_SAWMILL: "Segheria",
    GID_BRICKYARD: "Fornace",
    GID_IRON_FOUNDRY: "Fonderia",
    GID_GRAIN_MILL: "Mulino",
    GID_BAKERY: "Forno",
    GID_WAREHOUSE: "Magazzino",
    GID_GRANARY: "Granaio",
    GID_SMITHY: "Fabbro",
    GID_TOURNAMENT_SQUARE: "Piazza del Torneo",
    GID_MAIN_BUILDING: "Municipio",
    GID_RALLY_POINT: "Punto di Raduno",
    GID_MARKETPLACE: "Mercato",
    GID_EMBASSY: "Ambasciata",
    GID_BARRACKS: "Caserma",
    GID_STABLE: "Scuderia",
    GID_WORKSHOP: "Officina",
    GID_ACADEMY: "Accademia",
    GID_CRANNY: "Nascondiglio",
    GID_TOWN_HALL: "Municipio Grande",
    GID_RESIDENCE: "Residenza",
    GID_PALACE: "Palazzo",
    GID_TREASURY: "Tesoreria",
    GID_TRADE_OFFICE: "Ufficio Commerciale",
    GID_GREAT_BARRACKS: "Grande Caserma",
    GID_GREAT_STABLE: "Grande Scuderia",
    GID_CITY_WALL: "Mura",
    GID_EARTH_WALL: "Terrapieno",
    GID_PALISADE: "Staccionata",
    GID_STONEMASON: "Laboratorio dello Scultore",
    GID_BREWERY: "Birreria",
    GID_TRAPPER: "Trappolaio",
    GID_HERO_MANSION: "Magione dell'Eroe",
    GID_GREAT_WAREHOUSE: "Grande Magazzino",
    GID_GREAT_GRANARY: "Grande Granaio",
    GID_WONDER: "Meraviglia",
    GID_HORSE_TROUGH: "Abbeveratoio",
    GID_WATERWORKS: "Acquedotto",
}

# Requisiti per nuova costruzione: gid -> (prerequisiti, capital_only).
# Prerequisito = (gid_richiesto, livello_minimo); gid 1-4 = campo risorsa.
_PREREQS: dict[int, tuple[list[tuple[int, int]], bool]] = {
    GID_SAWMILL: ([(GID_WOODCUTTER, 10)], False),
    GID_BRICKYARD: ([(GID_CLAY_PIT, 10)], False),
    GID_IRON_FOUNDRY: ([(GID_IRON_MINE, 10)], False),
    GID_GRAIN_MILL: ([(GID_CROPLAND, 5)], False),
    GID_BAKERY: ([(GID_CROPLAND, 10), (GID_GRAIN_MILL, 5)], False),
    GID_SMITHY: ([(GID_MAIN_BUILDING, 3)], False),
    GID_TOURNAMENT_SQUARE: ([(GID_RALLY_POINT, 15)], False),
    GID_MAIN_BUILDING: ([], False),
    GID_MARKETPLACE: ([(GID_MAIN_BUILDING, 3), (GID_GRANARY, 1), (GID_WAREHOUSE, 1)], False),
    GID_EMBASSY: ([(GID_MAIN_BUILDING, 1)], False),
    GID_BARRACKS: ([(GID_MAIN_BUILDING, 3), (GID_RALLY_POINT, 1)], False),
    GID_STABLE: ([(GID_ACADEMY, 5), (GID_SMITHY, 3)], False),
    GID_WORKSHOP: ([(GID_ACADEMY, 10), (GID_MAIN_BUILDING, 5)], False),
    GID_ACADEMY: ([(GID_MAIN_BUILDING, 3), (GID_BARRACKS, 3)], False),
    GID_CRANNY: ([], False),
    GID_TOWN_HALL: ([(GID_ACADEMY, 10), (GID_MAIN_BUILDING, 10)], False),
    GID_RESIDENCE: ([(GID_MAIN_BUILDING, 5)], False),
    GID_PALACE: ([(GID_MAIN_BUILDING, 5), (GID_EMBASSY, 1)], False),
    GID_TREASURY: ([(GID_MAIN_BUILDING, 10)], False),
    GID_TRADE_OFFICE: ([(GID_MARKETPLACE, 20), (GID_STABLE, 10)], False),
    GID_GREAT_BARRACKS: ([(GID_BARRACKS, 20)], True),
    GID_GREAT_STABLE: ([(GID_STABLE, 20)], True),
    GID_STONEMASON: ([(GID_MAIN_BUILDING, 5), (GID_PALACE, 3)], True),
    GID_BREWERY: ([(GID_GRANARY, 10)], False),
    GID_TRAPPER: ([(GID_RALLY_POINT, 1)], False),
    GID_HERO_MANSION: ([(GID_MAIN_BUILDING, 3), (GID_RALLY_POINT, 1)], False),
    GID_GREAT_WAREHOUSE: ([], False),  # solo villaggio Meraviglia
    GID_GREAT_GRANARY: ([], False),    # solo villaggio Meraviglia
    GID_HORSE_TROUGH: ([(GID_STABLE, 20), (GID_RALLY_POINT, 10)], False),
    GID_WATERWORKS: ([(GID_MAIN_BUILDING, 5)], False),
}

FIELD_GID_TYPES: dict[int, ResourceType] = {
    GID_WOODCUTTER: ResourceType.WOOD,
    GID_CLAY_PIT: ResourceType.CLAY,
    GID_IRON_MINE: ResourceType.IRON,
    GID_CROPLAND: ResourceType.CROP,
}

# Chiavi usate da VillageConfig.building_focus (dashboard) -> gid.
# Valori a tupla per chiavi ambigue (le mura variano per tribu').
FOCUS_KEY_GIDS: dict[str, int | tuple[int, ...]] = {
    "main": GID_MAIN_BUILDING,
    "main_building": GID_MAIN_BUILDING,
    "warehouse": GID_WAREHOUSE,
    "granary": GID_GRANARY,
    "cranny": GID_CRANNY,
    "embassy": GID_EMBASSY,
    "rally_point": GID_RALLY_POINT,
    "marketplace": GID_MARKETPLACE,
    "market": GID_MARKETPLACE,
    "residence": GID_RESIDENCE,
    "palace": GID_PALACE,
    # Gruppo alternativo: Residenza, oppure Palazzo (capitale) — usato
    # dai preset ruolo; il builder sceglie il gid presente/costruibile.
    "residence_palace": (GID_RESIDENCE, GID_PALACE),
    "treasury": GID_TREASURY,
    "town_hall": GID_TOWN_HALL,
    "hero_mansion": GID_HERO_MANSION,
    "trade_office": GID_TRADE_OFFICE,
    "barracks": GID_BARRACKS,
    "great_barracks": GID_GREAT_BARRACKS,
    "stable": GID_STABLE,
    "great_stable": GID_GREAT_STABLE,
    "workshop": GID_WORKSHOP,
    "academy": GID_ACADEMY,
    "smithy": GID_SMITHY, "armoury": GID_SMITHY,
    "tournament_square": GID_TOURNAMENT_SQUARE,
    "hospital": -1,  # non presente in T4.6
    "trapper": GID_TRAPPER,
    "city_wall": WALL_GIDS, "wall": WALL_GIDS,
    "earth_wall": GID_EARTH_WALL, "palisade": GID_PALISADE,
    "brickyard": GID_BRICKYARD, "sawmill": GID_SAWMILL,
    "iron_foundry": GID_IRON_FOUNDRY,
    "grain_mill": GID_GRAIN_MILL, "bakery": GID_BAKERY,
    "stonemason_lodge": GID_STONEMASON,
    "horse_drinking_trough": GID_HORSE_TROUGH,
    "great_warehouse": GID_GREAT_WAREHOUSE,
    "great_granary": GID_GREAT_GRANARY,
    "brewery": GID_BREWERY, "waterworks": GID_WATERWORKS,
    "woodcutter": GID_WOODCUTTER, "clay_pit": GID_CLAY_PIT,
    "iron_mine": GID_IRON_MINE, "cropland": GID_CROPLAND,
}


def gids_for_focus_key(key: str) -> list[int]:
    """Traduce una chiave building_focus in gid (vuoto se sconosciuta)."""
    raw = FOCUS_KEY_GIDS.get((key or "").strip().lower())
    if raw is None:
        # Accetta anche il gid numerico diretto.
        try:
            val = int(str(key).strip())
            return [val] if val > 0 else []
        except ValueError:
            return []
    if isinstance(raw, tuple):
        return list(raw)
    return [raw] if raw > 0 else []


def _field_level(fields: list[Field], res_type: ResourceType) -> int:
    """Livello massimo tra i campi del tipo richiesto."""
    return max(
        (f.level for f in fields if f.resource_type is res_type),
        default=0,
    )


def prereqs_met(
    fields: list[Field],
    gid: int,
    is_capital: bool = False,
) -> tuple[bool, str]:
    """Verifica i prerequisiti per costruire `gid` su un'area vuota.

    Ritorna (ok, motivo_blocco). Per un edificio esistente (upgrade) il
    chiamante non dovrebbe invocare questa funzione: l'esistenza stessa
    garantisce i prerequisiti.
    """
    reqs, capital_only = _PREREQS.get(gid, ([], False))
    if capital_only and not is_capital:
        return False, "costruibile solo in capitale"
    missing: list[str] = []
    for req_gid, req_level in reqs:
        if req_gid in FIELD_GID_TYPES:
            have = _field_level(fields, FIELD_GID_TYPES[req_gid])
        else:
            have = max(
                (f.level for f in fields
                 if not f.is_resource_field and f.gid == req_gid),
                default=0,
            )
        if have < req_level:
            missing.append(f"gid {req_gid} liv.{req_level}")
    if missing:
        return False, "manca " + ", ".join(missing)
    return True, ""
