"""Modelli dati scambiati tra sensori, cervello ed esecutore."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class ResourceType(str, Enum):
    WOOD = "wood"
    CLAY = "clay"
    IRON = "iron"
    CROP = "crop"


@dataclass
class Resources:
    """Scorte attuali e capacita' dei magazzini (0 = dato non letto)."""

    wood: int = 0
    clay: int = 0
    iron: int = 0
    crop: int = 0
    warehouse_capacity: int = 0
    granary_capacity: int = 0
    # Produzione oraria, se il parser riesce a leggerla
    production_per_hour: dict[ResourceType, int] = field(default_factory=dict)

    def amount(self, res_type: ResourceType) -> int:
        return getattr(self, res_type.value)

    def capacity(self, res_type: ResourceType) -> int:
        return (
            self.granary_capacity
            if res_type is ResourceType.CROP
            else self.warehouse_capacity
        )

    def scarcest(self) -> ResourceType:
        """Risorsa piu' scarsa in rapporto alla capacita' del magazzino."""
        ratios: dict[ResourceType, float] = {}
        for rt in ResourceType:
            cap = self.capacity(rt) or 1
            ratios[rt] = self.amount(rt) / cap
        return min(ratios, key=ratios.get)


class VillageRole(str, Enum):
    """Profilo strategico del villaggio (VillageBrain, architettura macro/micro)."""

    HAMMER = "hammer"            # offensivo: caserma/scuderia/officina, truppe d'attacco
    DEFENSIVE = "defensive"      # scudo: caserma, residenza, mura, truppe difensive
    CROPLOG = "croplog"          # capitale croplog: focus grano, accetta surplus
    FEEDER = "feeder"            # economico: mercato, spedisce surplus ai target
    STANDARD = "standard"        # nessun profilo: comportamento bilanciato

    @classmethod
    def parse(cls, value: str | None) -> "VillageRole":
        """Mappa una stringa config in un ruolo; alias inclusi."""
        normalized = (value or "").strip().lower()
        aliases = {
            "capital": cls.CROPLOG,
            "croplog_capital": cls.CROPLOG,
            "defence": cls.DEFENSIVE,
            "shield": cls.DEFENSIVE,
            "economy": cls.FEEDER,
        }
        if normalized in aliases:
            return aliases[normalized]
        try:
            return cls(normalized)
        except ValueError:
            return cls.STANDARD


# Nomi unita' delle tre tribu' (t1-t10 Romani, t11-t20 Teutoni, t21-t30 Galli)
STATIC_TROOP_LABELS: dict[str, str] = {
    "t1": "Legionario", "t2": "Pretoriano", "t3": "Imperiano",
    "t4": "Emissario a cavallo", "t5": "Equites Imperatoris", "t6": "Equites Caesaris",
    "t7": "Ariete", "t8": "Catapulta da fuoco", "t9": "Senatore", "t10": "Colono",
    "t11": "Combattente", "t12": "Alabardiere", "t13": "Guerriero con ascia",
    "t14": "Esploratore", "t15": "Paladino", "t16": "Cavaliere Teutonico",
    "t17": "Ariete", "t18": "Catapulta", "t19": "Comandante", "t20": "Colono",
    "t21": "Falange", "t22": "Spadaccino", "t23": "Esploratore",
    "t24": "Fulmine di Teutates", "t25": "Cavaliere Druido", "t26": "Haeduan",
    "t27": "Ariete", "t28": "Trabucco", "t29": "Capotribù", "t30": "Colono",
}


# gid degli edifici Travian rilevanti per la logica di storage
GID_EMPTY = 0       # area di costruzione vuota (dorf2)
GID_WAREHOUSE = 10  # magazzino (legno/argilla/ferro)
GID_GRANARY = 11    # granaio (grano)
GID_RALLY_POINT = 16  # punto di raduno (raid, farm list, movimenti truppe)
GID_MARKETPLACE = 17  # mercato (invio risorse ad altri villaggi)
GID_BARRACKS = 19     # caserma (addestramento fanteria)
GID_STABLE = 20       # scuderia (addestramento cavalleria)
GID_WORKSHOP = 21     # officina (arieti/catapulte)
GID_RESIDENCE = 25    # residenza (coloni, punti cultura)
GID_PALACE = 26       # palazzo (coloni, punti cultura, capitale)


@dataclass
class Field:
    """Uno slot del villaggio (risorsa in dorf1, edificio in dorf2)."""

    slot_id: int
    name: str
    level: int
    resource_type: ResourceType | None = None
    gid: int = -1  # tipo edificio Travian (dorf2), -1 = non letto

    @property
    def is_resource_field(self) -> bool:
        return self.slot_id <= 18

    @property
    def is_empty_site(self) -> bool:
        """Area di costruzione vuota nel dorf2 (classe g0)."""
        return not self.is_resource_field and self.gid == GID_EMPTY


@dataclass
class BuildQueueItem:
    name: str = ""
    level: int = 0
    remaining_seconds: int = 0
    is_resource_field: bool = False
    slot_id: int = 0  # slot dorf1/dorf2 da build.php?id=N, 0 = non letto


@dataclass
class Village:
    """Un villaggio dell'account (estratto dalla barra laterale)."""

    name: str
    url: str = ""
    village_id: int = 0  # parametro newdid estratto dall'URL
    is_active: bool = False
    role: str = "standard"  # capital|croplog|standard (da accounts.json)
    x: int | None = None  # coordinate mappa, se note
    y: int | None = None


@dataclass
class GameState:
    """Fotografia del mondo di gioco prodotta dai sensori."""

    resources: Resources = field(default_factory=Resources)
    fields: list[Field] = field(default_factory=list)
    build_queue: list[BuildQueueItem] = field(default_factory=list)
    incoming_attacks: int = 0
    village_name: str = ""
    village_id: int = 0  # newdid del villaggio attivo durante la lettura
    # snapshot risorse/capacita' degli altri villaggi (nome -> Resources)
    peer_villages: dict[str, Resources] = field(default_factory=dict)
    # Modulo espansione: punti cultura e coloni del villaggio corrente
    culture_points: int = 0          # CP attuali letti in residenza/palazzo
    culture_points_required: int = 0  # CP richiesti per il prossimo villaggio
    settlers_home: int = 0           # coloni pronti nel villaggio
    # Modulo farming: truppe attualmente a casa (alias normalizzato -> qta)
    troops_home: dict[str, int] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def resource_fields(self) -> list[Field]:
        return [f for f in self.fields if f.is_resource_field]

    def interior_fields(self) -> list[Field]:
        """Edifici interni del dorf2 (slot 19-40)."""
        return [f for f in self.fields if not f.is_resource_field]

    def resource_queue(self) -> list[BuildQueueItem]:
        """Costruzioni in corso di campi risorsa (dorf1)."""
        return [item for item in self.build_queue if item.is_resource_field]

    def interior_queue(self) -> list[BuildQueueItem]:
        """Costruzioni in corso di edifici interni (dorf2)."""
        return [item for item in self.build_queue if not item.is_resource_field]

    def free_warehouse(self) -> int:
        """Spazio libero nel magazzino (legno/argilla/ferro)."""
        cap = self.resources.warehouse_capacity
        if cap <= 0:
            return 0
        return max(0, cap - max(self.resources.wood, self.resources.clay, self.resources.iron))

    def free_granary(self) -> int:
        """Spazio libero nel granaio (grano)."""
        cap = self.resources.granary_capacity
        if cap <= 0:
            return 0
        return max(0, cap - self.resources.crop)


class ActionType(str, Enum):
    WAIT = "wait"
    UPGRADE_FIELD = "upgrade_field"
    SEND_RESOURCES = "send_resources"  # invio risorse ad un altro villaggio
    TRAIN_TROOPS = "train_troops"      # addestramento truppe
    CLAIM_VIDEO_BONUS = "claim_video_bonus"  # riscatto bonus +15% per 8 ore (video ad)
    SEND_RAID = "send_raid"          # invio raid da farm list (Punto di Raduno)
    SEND_CUSTOM_RAID = "send_custom_raid"  # invio raid dinamico a coordinate libere (AI Scout Brain)
    FOUND_VILLAGE = "found_village"  # fondazione nuovo villaggio via mappa
    ALERT = "alert"          # es. attacco in arrivo: notifica senza agire
    LOGIN = "login"
    NOOP = "noop"


@dataclass
class ScoutTarget:
    """Target di saccheggio rilevato dal radar mappa o storico report."""

    x: int
    y: int
    village_name: str = ""
    player_name: str = ""
    alliance: str = ""
    population: int = 0
    distance: float = 0.0
    is_oasis: bool = False
    last_loot_ratio: float = 1.0  # 1.0 = pieno (100%), 0.0 = vuoto
    last_scout_ts: float = 0.0
    losses_recorded: int = 0
    enemy_units: int = 0
    is_blacklisted: bool = False
    blacklist_until: float = 0.0
    notes: str = ""


@dataclass
class AccountPowerAssessment:
    """Valutazione globale della potenza dell'intero account (macro-strategia)."""

    military_power: int = 0
    total_troops: dict[str, int] = field(default_factory=dict)
    net_crop_production: int = 0
    total_hourly_production: dict[str, int] = field(default_factory=dict)
    total_resources: dict[str, int] = field(default_factory=dict)
    total_capacity: dict[str, int] = field(default_factory=dict)
    storage_saturation_pct: float = 0.0
    ready_for_expansion: bool = False
    village_summaries: list[dict] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class Action:
    type: ActionType
    params: dict = field(default_factory=dict)
    reason: str = ""


@dataclass
class ActionResult:
    success: bool
    message: str = ""
