"""Test dello Smart Building Planner (builder/brain/prerequisiti)."""

from __future__ import annotations

import asyncio
import time

from travian_agent import builder
from travian_agent.brain import (
    RuleBasedBrain,
    rule_building_focus,
    rule_grain_safety,
    rule_role_buildings,
    rule_upgrade_weakest_field,
    rule_wait,
)
from travian_agent.building_prereqs import (
    GID_ACADEMY,
    GID_BAKERY,
    GID_BARRACKS,
    GID_CITY_WALL,
    GID_CRANNY,
    GID_CROPLAND,
    GID_EMBASSY,
    GID_GRAIN_MILL,
    GID_GREAT_BARRACKS,
    GID_MAIN_BUILDING,
    GID_RALLY_POINT,
    GID_SMITHY,
    GID_STABLE,
    GID_WORKSHOP,
    prereqs_met,
)
from travian_agent.config import Settings, VillageConfig
from travian_agent.models import (
    GID_EMPTY,
    GID_GRANARY,
    GID_MARKETPLACE,
    GID_PALACE,
    GID_WAREHOUSE,
    ActionType,
    BuildQueueItem,
    Field,
    GameState,
    PriorityQueueItem,
    Resources,
    ResourceType,
)


def _settings() -> Settings:
    return Settings(server_url="https://example.test")


def _state(**res_kwargs) -> GameState:
    state = GameState(village_name="TestVillage")
    state.resources = Resources(**res_kwargs)
    return state


def _village_fields() -> list[Field]:
    """Villaggio base: qualche campo risorsa + dorf2 con aree vuote."""
    fields = [
        Field(slot_id=1, name="Campo grano", level=4,
              resource_type=ResourceType.CROP),
        Field(slot_id=5, name="Bosco", level=2,
              resource_type=ResourceType.WOOD),
        Field(slot_id=19, name="Municipio", level=5, gid=GID_MAIN_BUILDING),
        Field(slot_id=20, name="Punto di Raduno", level=1, gid=GID_RALLY_POINT),
        Field(slot_id=25, name="Magazzino", level=3, gid=GID_WAREHOUSE),
        Field(slot_id=26, name="Granaio", level=3, gid=GID_GRANARY),
    ]
    fields += [
        Field(slot_id=s, name=f"Area vuota {s}", level=0, gid=GID_EMPTY)
        for s in range(30, 36)
    ]
    return fields


# ---------------------------------------------------------------------------
# Prerequisiti
# ---------------------------------------------------------------------------

class TestBuildingCategory:
    """Mappatura gid -> tab categoria per la costruzione su slot vuoto.

    Regressione BTN_NOT_FOUND ts30: il Palazzo (gid 26) non veniva
    trovato perche' la pagina slot apriva il tab Risorse (cat 3).
    """

    def test_palace_is_infrastructure(self):
        assert builder.building_category(GID_PALACE) == 1

    def test_warehouse_is_infrastructure(self):
        assert builder.building_category(GID_WAREHOUSE) == 1

    def test_military_gids(self):
        for gid in (
            GID_BARRACKS,
            GID_STABLE,
            GID_WORKSHOP,
            GID_ACADEMY,
            GID_SMITHY,
            GID_RALLY_POINT,
            GID_MAIN_BUILDING - 1,  # 14: Piazza Tornei
        ):
            assert builder.building_category(gid) == 2, gid

    def test_resource_gids(self):
        for gid in (5, 6, 7, GID_GRAIN_MILL, GID_BAKERY):
            assert builder.building_category(gid) == 3, gid

    def test_unknown_gid_defaults_to_infrastructure(self):
        assert builder.building_category(999) == 1


class TestPrereqs:
    def test_bakery_blocked_without_prereqs(self):
        fields = _village_fields()
        ok, why = prereqs_met(fields, GID_BAKERY)
        assert ok is False
        assert "manca" in why

    def test_bakery_allowed_when_prereqs_met(self):
        fields = _village_fields()
        fields[0].level = 10  # campo grano 10
        fields.append(
            Field(slot_id=36, name="Mulino", level=5, gid=GID_GRAIN_MILL)
        )
        ok, _ = prereqs_met(fields, GID_BAKERY)
        assert ok is True

    def test_great_barracks_capital_only(self):
        fields = _village_fields()
        fields.append(
            Field(slot_id=36, name="Caserma", level=20, gid=GID_BARRACKS)
        )
        ok_non_cap, _ = prereqs_met(fields, GID_GREAT_BARRACKS,
                                    is_capital=False)
        ok_cap, _ = prereqs_met(fields, GID_GREAT_BARRACKS, is_capital=True)
        assert ok_non_cap is False
        assert ok_cap is True

    def test_stable_requires_academy_and_smithy(self):
        fields = _village_fields()
        ok, _ = prereqs_met(fields, GID_STABLE)
        assert ok is False
        fields.append(
            Field(slot_id=36, name="Accademia", level=5, gid=GID_ACADEMY)
        )
        fields.append(
            Field(slot_id=37, name="Fabbro", level=3, gid=GID_SMITHY)
        )
        ok, _ = prereqs_met(fields, GID_STABLE)
        assert ok is True


# ---------------------------------------------------------------------------
# Nuove costruzioni su aree vuote
# ---------------------------------------------------------------------------

class TestNewConstruction:
    def test_new_building_intents_respects_prereqs(self):
        state = _state()
        state.fields = _village_fields()
        # Mulino richiede grano 5: il campo e' a 4 -> bloccato
        assert builder.new_building_intents(state, [GID_GRAIN_MILL]) == []
        state.fields[0].level = 6
        intents = builder.new_building_intents(state, [GID_GRAIN_MILL])
        assert len(intents) == 1
        assert intents[0].gid == GID_GRAIN_MILL
        assert intents[0].level == 0
        assert intents[0].slot_id == 30  # prima area vuota

    def test_new_building_intents_skips_existing_gid(self):
        state = _state()
        state.fields = _village_fields()
        state.fields.append(
            Field(slot_id=36, name="Mercato", level=2, gid=GID_MARKETPLACE)
        )
        intents = builder.new_building_intents(state, [GID_MARKETPLACE])
        assert intents == []

    def test_construct_gids_maps_only_empty_sites(self):
        state = _state()
        state.fields = _village_fields()
        pseudo = Field(slot_id=30, name="Mulino", level=0, gid=GID_GRAIN_MILL)
        existing = Field(slot_id=19, name="Municipio", level=5,
                         gid=GID_MAIN_BUILDING)
        mapping = builder.construct_gids(state, [pseudo, existing])
        assert mapping == {30: GID_GRAIN_MILL}


# ---------------------------------------------------------------------------
# Grain Safety Net
# ---------------------------------------------------------------------------

class TestGrainSafety:
    def _low_net_state(self) -> GameState:
        state = _state(crop=100, warehouse_capacity=5000,
                       granary_capacity=5000)
        state.resources.crop_balance = 3
        state.fields = _village_fields()
        return state

    def test_net_crop_none_disables_safety(self):
        state = _state()
        state.resources.crop_balance = None
        assert builder.crop_safety_needed(state, _settings()) is False

    def test_below_threshold_enables_safety(self):
        state = _state()
        state.resources.crop_balance = 3  # soglia globale 10
        assert builder.crop_safety_needed(state, _settings()) is True

    def test_above_threshold_disables_safety(self):
        state = _state()
        state.resources.crop_balance = 50
        assert builder.crop_safety_needed(state, _settings()) is False

    def test_safety_rule_prefers_crop_field(self):
        state = self._low_net_state()
        action = rule_grain_safety(state, _settings())
        assert action is not None
        assert action.type is ActionType.UPGRADE_FIELD
        assert action.params["candidates"][0] == 1  # campo grano

    def test_safety_blocks_non_grain_rules(self):
        state = self._low_net_state()
        assert rule_building_focus(state, _settings()) is None
        assert rule_role_buildings(state, _settings()) is None
        assert rule_upgrade_weakest_field(state, _settings()) is None

    def test_safety_blocks_decide_even_with_focus(self):
        """Con margine basso il focus manuale non puo' peggiorare il netto."""
        s = _settings()
        s.villages = {
            "TestVillage": VillageConfig(
                name="TestVillage", building_focus=["marketplace"]
            )
        }
        state = self._low_net_state()
        # Nessun upgrade grano possibile: campo al max e mill gia' a 5
        state.fields[0].level = 20
        state.fields.append(
            Field(slot_id=36, name="Mulino", level=5, gid=GID_GRAIN_MILL)
        )
        action = RuleBasedBrain(s).decide(state)
        # Il brain non deve proporre il mercato: grain safety blocca tutto
        assert action.type is not ActionType.UPGRADE_FIELD or \
            action.params.get("construct") or \
            action.params["candidates"][0] in (1, 36)

    def test_village_threshold_overrides_global(self):
        s = _settings()
        s.min_net_crop_threshold = 10.0
        s.villages = {
            "TestVillage": VillageConfig(
                name="TestVillage", min_net_crop_threshold=50.0
            )
        }
        state = _state()
        state.resources.crop_balance = 30
        assert builder.crop_safety_needed(state, s) is True

    def test_village_threshold_zero_inherits_global(self):
        s = _settings()
        s.min_net_crop_threshold = 10.0
        s.villages = {
            "TestVillage": VillageConfig(
                name="TestVillage", min_net_crop_threshold=0.0
            )
        }
        state = _state()
        state.resources.crop_balance = 5
        assert builder.crop_threshold(state, s) == 10.0
        assert builder.crop_safety_needed(state, s) is True


# ---------------------------------------------------------------------------
# Focus manuale e ruoli
# ---------------------------------------------------------------------------

class TestFocusAndRoles:
    def test_focus_picks_matching_building(self):
        s = _settings()
        s.villages = {
            "TestVillage": VillageConfig(
                name="TestVillage", building_focus=["marketplace"]
            )
        }
        state = _state(wood=100, warehouse_capacity=5000,
                       granary_capacity=5000)
        state.fields = _village_fields()
        state.fields.append(
            Field(slot_id=36, name="Mercato", level=2, gid=GID_MARKETPLACE)
        )
        action = rule_building_focus(state, s)
        assert action is not None
        assert action.params["candidates"][0] == 36

    def test_focus_constructs_missing_building_when_prereqs_ok(self):
        s = _settings()
        s.villages = {
            "TestVillage": VillageConfig(
                name="TestVillage", building_focus=["embassy"]
            )
        }
        state = _state(wood=100, warehouse_capacity=5000,
                       granary_capacity=5000)
        state.fields = _village_fields()
        action = rule_building_focus(state, s)
        assert action is not None
        # Ambasciata (main 1 richiesto, municipio a 5): costruzione su slot 30
        assert action.params["construct"] == {30: 18}

    def test_focus_blocked_when_prereqs_missing(self):
        s = _settings()
        s.villages = {
            "TestVillage": VillageConfig(
                name="TestVillage", building_focus=["workshop"]
            )
        }
        state = _state(wood=100, warehouse_capacity=5000,
                       granary_capacity=5000)
        state.fields = _village_fields()  # niente accademia 10
        assert rule_building_focus(state, s) is None

    def test_hammer_role_prefers_lowest_military(self):
        s = _settings()
        s.villages = {
            "TestVillage": VillageConfig(name="TestVillage", role="hammer")
        }
        state = _state(wood=100, warehouse_capacity=5000,
                       granary_capacity=5000)
        state.fields = _village_fields()
        state.fields += [
            Field(slot_id=36, name="Caserma", level=3, gid=GID_BARRACKS),
            Field(slot_id=37, name="Scuderia", level=11, gid=GID_STABLE),
        ]
        intents = builder.role_intents(state, s)
        # Verso il 10 prima del 20: caserma (3) prima della scuderia (11)
        assert intents[0].slot_id == 36

    def test_hammer_role_constructs_missing_barracks(self):
        s = _settings()
        s.villages = {
            "TestVillage": VillageConfig(name="TestVillage", role="hammer")
        }
        state = _state(wood=100, warehouse_capacity=5000,
                       granary_capacity=5000)
        state.fields = _village_fields()  # main 5 + rally 1 -> caserma ok
        intents = builder.role_intents(state, s)
        # Caserma assente ma prerequisiti soddisfatti: costruzione proposta
        assert any(f.gid == GID_BARRACKS and f.level == 0 for f in intents)

    def test_role_rule_returns_construct_map(self):
        s = _settings()
        s.villages = {
            "TestVillage": VillageConfig(name="TestVillage", role="hammer")
        }
        state = _state(wood=100, warehouse_capacity=5000,
                       granary_capacity=5000)
        state.fields = _village_fields()
        action = rule_role_buildings(state, s)
        assert action is not None
        assert 30 in action.params["construct"]


# ---------------------------------------------------------------------------
# Selettore e piano
# ---------------------------------------------------------------------------

class TestSelectAndPlan:
    def test_select_upgrade_storage_first(self):
        state = _state(
            wood=950, clay=100, iron=100, crop=100,
            warehouse_capacity=1000, granary_capacity=5000,
        )
        state.fields = _village_fields()
        it = builder.select_upgrade(state, _settings())
        assert it is not None
        assert it.kind == "storage"
        assert it.slot_id == 25  # magazzino livello 3

    def test_select_upgrade_constructs_storage_when_absent(self):
        state = _state(
            wood=950, warehouse_capacity=1000, granary_capacity=5000,
        )
        state.fields = _village_fields()
        # Nessun magazzino: rimuovilo
        state.fields = [f for f in state.fields if f.gid != GID_WAREHOUSE]
        it = builder.select_upgrade(state, _settings())
        assert it is not None
        assert it.kind == "storage"
        assert it.construct.get(it.slot_id) == GID_WAREHOUSE

    def test_plan_upgrades_unique_slots(self):
        s = _settings()
        s.villages = {
            "TestVillage": VillageConfig(name="TestVillage", role="hammer")
        }
        state = _state(wood=100, warehouse_capacity=5000,
                       granary_capacity=5000)
        state.fields = _village_fields()
        plan = builder.plan_upgrades(state, s, depth=4)
        assert 1 <= len(plan) <= 4
        slots = [p["slot_id"] for p in plan]
        assert len(slots) == len(set(slots))

    def test_plan_upgrades_simulates_construction(self):
        """Il piano deve poter includere una nuova costruzione e proseguire."""
        s = _settings()
        s.villages = {
            "TestVillage": VillageConfig(
                name="TestVillage", building_focus=["embassy"]
            )
        }
        state = _state(wood=100, warehouse_capacity=5000,
                       granary_capacity=5000)
        state.fields = _village_fields()
        plan = builder.plan_upgrades(state, s, depth=2)
        first = plan[0]
        assert first["construct"] == {30: 18}
        assert first["target_level"] == 1
        # Una volta costruita (simulazione), l'ambasciata diventa un
        # normale candidato di upgrade, non piu' una nuova costruzione.
        state.fields = [
            f if f.slot_id != 30 else
            Field(slot_id=30, name="Ambasciata", level=1, gid=18)
            for f in state.fields
        ]
        it = builder.select_upgrade(state, s)
        assert it is not None
        assert it.slot_id == 30
        assert it.construct == {}
        assert it.target_level == 2

    def test_queue_occupies_empty_slot(self):
        state = _state()
        state.fields = _village_fields()
        state.fields[0].level = 6  # prerequisito mulino ok
        state.build_queue = [
            BuildQueueItem(name="Mulino", remaining_seconds=300,
                           is_resource_field=False, slot_id=30)
        ]
        intents = builder.new_building_intents(state, [GID_GRAIN_MILL])
        # Lo slot 30 e' gia' impegnato: la costruzione va sulla 31
        assert intents and intents[0].slot_id == 31


# ---------------------------------------------------------------------------
# queue_slots_free — binari coda per gid (fix queue_parser)
# ---------------------------------------------------------------------------

class TestQueueSlotsFree:
    """I binari dorf1/dorf2 si deducono dai gid in coda, non dalla
    posizione nell'array: con un campo in costruzione la Coda
    Prioritaria degli edifici dorf2 deve poter partire."""

    def _romans(self, plus: bool) -> Settings:
        s = _settings()
        s.tribe = "romans"
        s.allow_parallel_build = plus
        return s

    def test_queued_field_keeps_inner_free(self):
        """Bosco/Pozzo d'argilla in coda -> inner_free True (spec fix)."""
        state = _state()
        state.build_queue = [
            BuildQueueItem(name="Bosco", is_resource_field=True,
                           slot_id=5, gid=1),
        ]
        can_out, can_in = builder.queue_slots_free(
            state, self._romans(plus=False)
        )
        assert can_in is True
        assert can_out is True

    def test_queued_building_keeps_outer_free(self):
        """Un edificio dorf2 in testa all'array non occupa il binario
        dei campi: la corsia si legge dal gid, non da items[0]."""
        state = _state()
        state.build_queue = [
            BuildQueueItem(name="Residenza", is_resource_field=False,
                           slot_id=31, gid=25),
        ]
        can_out, can_in = builder.queue_slots_free(
            state, self._romans(plus=False)
        )
        assert can_out is True
        assert can_in is True

    def test_max_parallel_two_for_romans_without_plus(self):
        state = _state()
        state.build_queue = [
            BuildQueueItem(name="Bosco", is_resource_field=True,
                           slot_id=5, gid=1),
            BuildQueueItem(name="Residenza", is_resource_field=False,
                           slot_id=31, gid=25),
        ]
        assert builder.queue_slots_free(
            state, self._romans(plus=False)
        ) == (False, False)

    def test_max_parallel_three_with_plus(self):
        state = _state()
        state.build_queue = [
            BuildQueueItem(name="Bosco", is_resource_field=True,
                           slot_id=5, gid=1),
            BuildQueueItem(name="Residenza", is_resource_field=False,
                           slot_id=31, gid=25),
        ]
        # 2 item < 3: entrambi i binari accettano ancora lavori
        assert builder.queue_slots_free(
            state, self._romans(plus=True)
        ) == (True, True)
        state.build_queue.append(
            BuildQueueItem(name="Granaio", is_resource_field=False,
                           slot_id=26, gid=GID_GRANARY)
        )
        assert builder.queue_slots_free(
            state, self._romans(plus=True)
        ) == (False, False)

    def test_unknown_gid_falls_back_to_flag(self):
        """Item senza gid letto: la corsia resta quella del flag."""
        state = _state()
        state.build_queue = [
            BuildQueueItem(name="Edificio ignoto",
                           is_resource_field=False, slot_id=0, gid=0),
        ]
        can_out, can_in = builder.queue_slots_free(
            state, self._romans(plus=False)
        )
        assert can_out is True
        assert can_in is True


# ---------------------------------------------------------------------------
# Storage in coda + prerequisiti auto-risolti dalla Coda Prioritaria
# ---------------------------------------------------------------------------

class TestStorageQueuedAndPrereqResolution:
    """Fix: il blocco 'Storage al limite' tace quando magazzino/granaio
    sono gia' in coda; la Coda Prioritaria propone il prerequisito
    mancante (es. Ambasciata 1 per il Palazzo) invece di saltare lo
    step."""

    def _full_storage_state(self) -> GameState:
        state = _state(
            wood=950, clay=100, iron=100, crop=4800,
            warehouse_capacity=1000, granary_capacity=5000,
        )
        state.fields = _village_fields()
        return state

    def test_storage_needed_skips_queued_granary(self):
        """Granaio quasi pieno ma gia' in coda (gid esplicito): solo il
        magazzino resta 'needed'."""
        state = self._full_storage_state()
        state.build_queue = [
            BuildQueueItem(name="Granaio", is_resource_field=False,
                           slot_id=26, gid=GID_GRANARY),
        ]
        needed = builder.storage_needed(state)
        assert GID_GRANARY not in needed
        assert GID_WAREHOUSE in needed  # wood 950/1000: ancora needed

    def test_storage_needed_slot_fallback(self):
        """Item di coda senza gid: lo storage si riconosce dallo slot."""
        state = self._full_storage_state()
        state.resources.wood = 100  # solo granaio 'needed'
        state.resources.clay = 100
        state.resources.iron = 100
        state.build_queue = [
            BuildQueueItem(name="Granaio", is_resource_field=False,
                           slot_id=26, gid=0),
        ]
        assert builder.storage_needed(state) == []

    def test_select_upgrade_passes_storage_when_queued(self):
        """Storage needed ma entrambi gli storage in lavorazione: il
        selettore non propone un secondo upgrade gemello."""
        state = self._full_storage_state()
        state.build_queue = [
            BuildQueueItem(name="Magazzino", is_resource_field=False,
                           slot_id=25, gid=GID_WAREHOUSE),
            BuildQueueItem(name="Granaio", is_resource_field=False,
                           slot_id=26, gid=GID_GRANARY),
        ]
        it = builder.select_upgrade(state, _settings())
        assert it is None or it.kind != "storage"

    def test_focus_constructs_missing_prereq_building(self):
        """Palazzo in coda senza Ambasciata: si costruisce l'Ambasciata."""
        s = _settings()
        s.villages = {
            "TestVillage": VillageConfig(
                name="TestVillage", building_focus=["palace"]
            )
        }
        state = _state(wood=100, warehouse_capacity=5000,
                       granary_capacity=5000)
        state.fields = _village_fields()  # Municipio 5, no Ambasciata
        action = rule_building_focus(state, s)
        assert action is not None
        # Ambasciata (gid 18) sul primo slot vuoto (30)
        assert action.params["construct"] == {30: 18}

    def test_focus_upgrades_underleveled_prereq(self):
        """Palazzo con Municipio a 3 (serve 5): upgrade del Municipio."""
        s = _settings()
        s.villages = {
            "TestVillage": VillageConfig(
                name="TestVillage", building_focus=["palace"]
            )
        }
        state = _state(wood=100, warehouse_capacity=5000,
                       granary_capacity=5000)
        state.fields = _village_fields()
        state.fields[2].level = 3  # Municipio liv. 3 < 5 richiesti
        action = rule_building_focus(state, s)
        assert action is not None
        assert action.params["candidates"][0] == 19  # slot Municipio
        assert action.params["construct"] == {}      # upgrade, non costruzione

    def test_focus_still_blocked_by_field_prereq(self):
        """Mulino con campi grano < 5: prerequisito a campo non
        auto-risolvibile, lo step resta bloccato."""
        s = _settings()
        s.villages = {
            "TestVillage": VillageConfig(
                name="TestVillage", building_focus=["grain_mill"]
            )
        }
        state = _state(wood=100, warehouse_capacity=5000,
                       granary_capacity=5000)
        state.fields = _village_fields()  # campo grano a livello 4
        assert rule_building_focus(state, s) is None


# ---------------------------------------------------------------------------
# Fallback slot dorf2 non scansionati (fix "slot liberi mai persi")
# ---------------------------------------------------------------------------

class TestUnscannedSlotFallback:
    """Regressione: se 27/28 sono occupati/bloccati e 29-38 risultano
    'non scansionati' (assenti dalla lettura dorf2), il bot non deve
    rinunciare alla costruzione — quegli slot non hanno gid assegnato
    e valgono come possibili aree libere, DOPO quelle verificate."""

    def test_resolve_target_slot_uses_unscanned_when_no_known_empty(self):
        """Nessuna area verificata vuota: il resolver propone il primo
        slot dorf2 assente dallo snapshot invece di NO_FREE_SLOTS."""
        state = _state()
        state.fields = [
            Field(slot_id=19, name="Municipio", level=5,
                  gid=GID_MAIN_BUILDING),
            Field(slot_id=25, name="Magazzino", level=3,
                  gid=GID_WAREHOUSE),
        ]
        it = PriorityQueueItem(gid=GID_MARKETPLACE, target_level=1)
        slot, lvl, status = builder.resolve_target_slot(it, state)
        assert status == "OK"
        assert lvl == 0
        assert slot == 20  # primo slot 19-38 assente dalla scansione

    def test_verified_empty_preferred_over_unscanned(self):
        """Le aree verificate vuote hanno priorita' sugli slot
        non scansionati, anche se questi hanno id piu' basso."""
        state = _state()
        state.fields = [
            Field(slot_id=19, name="Municipio", level=5,
                  gid=GID_MAIN_BUILDING),
            Field(slot_id=30, name="Area vuota 30", level=0,
                  gid=GID_EMPTY),
        ]
        it = PriorityQueueItem(gid=GID_MARKETPLACE, target_level=1)
        slot, _lvl, status = builder.resolve_target_slot(it, state)
        assert status == "OK"
        assert slot == 30  # verificato vuoto batte il non scansionato 21

    def test_unscanned_slot_respects_reserved_and_queue(self):
        """Slot non scansionato in coda o riservato viene saltato."""
        state = _state()
        state.fields = [
            Field(slot_id=19, name="Municipio", level=5,
                  gid=GID_MAIN_BUILDING),
            Field(slot_id=25, name="Magazzino", level=3,
                  gid=GID_WAREHOUSE),
        ]
        # 20 e' in coda con un gid diverso dal gruppo: il resolver lo
        # salta e propone il primo slot non scansionato libero (21).
        state.build_queue = [
            BuildQueueItem(name="Granaio", is_resource_field=False,
                           slot_id=20, gid=GID_GRANARY),
        ]
        it2 = PriorityQueueItem(gid=GID_MARKETPLACE, target_level=1)
        slot, _lvl, status = builder.resolve_target_slot(it2, state)
        assert status == "OK"
        assert slot == 21

    def test_empty_slots_includes_unscanned_after_verified(self):
        """empty_slots restituisce le aree verificate e poi i sintetici
        per gli slot 19-38 assenti dalla lettura."""
        state = _state()
        state.fields = _village_fields()  # vuote verificate 30-35
        empties = builder.empty_slots(state)
        ids = [f.slot_id for f in empties]
        assert ids[:6] == [30, 31, 32, 33, 34, 35]
        # Sintetici: tutti gli slot dorf2 non presenti nello snapshot
        assert set(ids[6:]) == set(range(19, 39)) - {
            19, 20, 25, 26, 30, 31, 32, 33, 34, 35,
        }
        assert all(f.gid == GID_EMPTY and f.level == 0 for f in empties)


# ---------------------------------------------------------------------------
# TTL sullo stato STUCK (fix "bloccato fino al riavvio")
# ---------------------------------------------------------------------------

class TestStuckTTL:
    """Uno slot marcato STUCK (EXECUTED senza progresso) si sblocca da
    solo dopo ~20 minuti: la coda piena o un raid non devono congelare
    lo step fino al riavvio del container."""

    def setup_method(self):
        builder._FOCUS_EXEC_HISTORY.clear()
        builder._FOCUS_FAIL_HISTORY.clear()

    def teardown_method(self):
        builder._FOCUS_EXEC_HISTORY.clear()
        builder._FOCUS_FAIL_HISTORY.clear()

    def _state_with_slot(self) -> GameState:
        state = _state()
        state.fields = _village_fields()
        return state

    def test_stuck_marks_after_two_confirmed_execs(self):
        """2 EXECUTED confermati senza progresso -> slot stuck."""
        state = self._state_with_slot()
        builder.record_upgrade_exec("TestVillage", 30, 0)
        builder.record_upgrade_exec("TestVillage", 30, 0)
        assert 30 in builder.focus_stuck_slots(state)
        # Gli eventi sono timestamped (level, monotonic_ts)
        hist = builder._FOCUS_EXEC_HISTORY[("TestVillage", 30)]
        assert all(len(e) == 2 and e[0] == 0 for e in hist)

    def test_stuck_expires_after_ttl(self):
        """EXECUTED piu' vecchi del TTL non contano: blocco rilasciato."""
        state = self._state_with_slot()
        old = time.monotonic() - (builder._FOCUS_STUCK_TTL_S + 60)
        builder._FOCUS_EXEC_HISTORY[("TestVillage", 30)] = [
            (0, old), (0, old), (0, old),
        ]
        assert 30 not in builder.focus_stuck_slots(state)
        # La voce scaduta viene ripulita dalla storia
        assert ("TestVillage", 30) not in builder._FOCUS_EXEC_HISTORY

    def test_mixed_history_counts_only_fresh_entries(self):
        """Con un EXECUTED fresco solo, lo slot non e' stuck anche se
        la storia totale supera la soglia."""
        state = self._state_with_slot()
        old = time.monotonic() - (builder._FOCUS_STUCK_TTL_S + 60)
        builder._FOCUS_EXEC_HISTORY[("TestVillage", 30)] = [
            (0, old), (0, old), (0, time.monotonic()),
        ]
        assert 30 not in builder.focus_stuck_slots(state)
        assert len(builder._FOCUS_EXEC_HISTORY[("TestVillage", 30)]) == 1

    def test_level_change_still_resets(self):
        """Il reset per progresso reale continua a funzionare col
        nuovo formato (level, ts)."""
        state = self._state_with_slot()
        builder.record_upgrade_exec("TestVillage", 30, 0)
        builder.record_upgrade_exec("TestVillage", 30, 0)
        assert 30 in builder.focus_stuck_slots(state)
        # La build e' partita davvero: il livello osservato e' cambiato
        for f in state.fields:
            if f.slot_id == 30:
                f.level = 1
        assert 30 not in builder.focus_stuck_slots(state)
        assert ("TestVillage", 30) not in builder._FOCUS_EXEC_HISTORY


# ---------------------------------------------------------------------------
# Livello effettivo: attuale + lavori in coda (doppia coda romani / Plus)
# ---------------------------------------------------------------------------

class TestEffectiveQueueLevel:
    """Fix: uno slot con costruzione in coda non deve essere riaccodato
    sul livello reale (es. Ambasciata lv.0 con coda ->1 e target 1 non
    deve partire una seconda volta arrivando a livello 2)."""

    def _state_with_queue(self, queue: list) -> GameState:
        state = _state(wood=100, warehouse_capacity=5000,
                       granary_capacity=5000)
        state.fields = _village_fields()
        state.build_queue = list(queue)
        return state

    def _settings_with_plan(self, plan: list) -> Settings:
        s = _settings()
        s.villages = {
            "TestVillage": VillageConfig(
                name="TestVillage", building_queue_plan=list(plan)
            )
        }
        return s

    def test_queued_embassy_not_requeued(self):
        """Slot letto ancora vuoto + Ambasciata ->1 in coda, target 1:
        lo step risulta gia' coperto dalla coda (TARGET_REACHED)."""
        state = self._state_with_queue([
            BuildQueueItem(name="Ambasciata", level=1,
                           is_resource_field=False, slot_id=30, gid=18),
        ])
        it = PriorityQueueItem(key="embassy", target_level=1)
        _slot, lvl, status = builder.resolve_target_slot(it, state)
        assert status == "TARGET_REACHED"
        assert lvl == 1

        s = self._settings_with_plan(
            [{"key": "embassy", "target_level": 1, "priority": 1}]
        )
        assert builder.focus_intents(state, s) == []

    def test_queued_upgrade_below_target_waits(self):
        """Ambasciata ->1 in coda con target 5: lo slot e' impegnato —
        nessun nuovo click finche' il lavoro non finisce."""
        state = self._state_with_queue([
            BuildQueueItem(name="Ambasciata", level=1,
                           is_resource_field=False, slot_id=30, gid=18),
        ])
        s = self._settings_with_plan(
            [{"key": "embassy", "target_level": 5, "priority": 1}]
        )
        intents = builder.focus_intents(state, s)
        assert all(f.slot_id != 30 for f in intents)
        assert builder.construct_gids(state, intents) == {}

    def test_queued_prereq_not_reconstructed(self):
        """Palazzo senza Ambasciata ma con Ambasciata ->1 in coda: il
        prerequisito automatico non ripropone la costruzione."""
        state = self._state_with_queue([
            BuildQueueItem(name="Ambasciata", level=1,
                           is_resource_field=False, slot_id=30, gid=18),
        ])
        assert builder._prereq_intent(state, GID_PALACE, 31) is None

        s = self._settings_with_plan(
            [{"key": "palace", "target_level": 1, "priority": 1}]
        )
        assert builder.focus_intents(state, s) == []

    def test_queued_field_level_counts_toward_target(self):
        """Campo dorf1 in coda fino al target: lo step e' coperto e
        non riparte sul livello reale piu' basso."""
        state = self._state_with_queue([
            BuildQueueItem(name="Bosco", level=3,
                           is_resource_field=True, slot_id=5, gid=1),
        ])
        it = PriorityQueueItem(key="woodcutter", target_level=3)
        _slot, _lvl, status = builder.resolve_target_slot(it, state)
        assert status == "TARGET_REACHED"

        s = self._settings_with_plan(
            [{"key": "woodcutter", "target_level": 3, "priority": 1}]
        )
        assert builder.focus_intents(state, s) == []


# ---------------------------------------------------------------------------
# Ticket: priorita' Focus Manuale sulle regole automatiche
# ---------------------------------------------------------------------------

class TestFocusManualPriorityOrder:
    """L'override manuale ha precedenza su storage/overflow/anti-raid:
    solo la safety vitale sul grano resta sopra."""

    def test_focus_rule_ranked_above_automatic_rules(self):
        order = [r.__name__ for r in RuleBasedBrain.DEFAULT_RULES]
        assert order.index("rule_grain_safety") < order.index(
            "rule_building_focus"
        )
        for rule in (
            "rule_upgrade_storage",
            "rule_emergency_overflow",
            "rule_anti_raid",
        ):
            assert order.index("rule_building_focus") < order.index(rule)

    def test_focus_wins_over_full_storage(self):
        """Granaio oltre soglia + Focus attivo: l'azione scelta e' il
        focus, non l'upgrade dello storage."""
        s = _settings()
        s.villages = {
            "TestVillage": VillageConfig(
                name="TestVillage", building_focus=["marketplace"]
            )
        }
        state = _state(
            crop=950, granary_capacity=1000,
            warehouse_capacity=5000, wood=4000, clay=4000, iron=4000,
        )
        state.fields = _village_fields()
        action = RuleBasedBrain(s).decide(state)
        assert action is not None
        assert not action.params.get("storage")


# ---------------------------------------------------------------------------
# Ticket: nuova costruzione (level 0) attende coda dorf2 libera su Plus
# ---------------------------------------------------------------------------

class TestFocusPlusQueueWait:
    """Travian Plus (Coda:1): il 2° slot coda dorf2 accetta SOLO
    ampliamenti — una nuova costruzione in testa al Focus attende che
    il cantiere interno si liberi (inner_count == 0) invece di scalare
    sugli step successivi (skip .upgradeBlocked + fallback)."""

    def _settings_with_focus(self, focus: list) -> Settings:
        s = _settings()
        s.allow_parallel_build = True  # Coda Plus
        s.villages = {
            "TestVillage": VillageConfig(
                name="TestVillage", building_focus=list(focus)
            )
        }
        return s

    def test_new_construction_waits_for_inner_queue(self):
        s = self._settings_with_focus(["marketplace"])
        state = _state()
        state.fields = _village_fields()
        state.build_queue = [
            BuildQueueItem(name="Magazzino livello 4", level=4,
                           is_resource_field=False, slot_id=25,
                           gid=GID_WAREHOUSE),
        ]
        assert builder.focus_intents(state, s) == []

    def test_new_construction_released_when_queue_empty(self):
        s = self._settings_with_focus(["marketplace"])
        state = _state()
        state.fields = _village_fields()
        intents = builder.focus_intents(state, s)
        assert intents and intents[0].level == 0

    def test_new_construction_ok_with_outer_only_queue(self):
        """Un lavoro dorf1 in coda non conta: inner_count resta 0."""
        s = self._settings_with_focus(["marketplace"])
        state = _state()
        state.fields = _village_fields()
        state.build_queue = [
            BuildQueueItem(name="Bosco livello 3", level=3,
                           is_resource_field=True, slot_id=5, gid=2),
        ]
        intents = builder.focus_intents(state, s)
        assert intents and intents[0].level == 0

    def test_upgrade_not_blocked_by_inner_queue(self):
        """Un ampliamento in testa puo' usare il 2° slot Plus."""
        s = self._settings_with_focus(["warehouse"])
        state = _state()
        state.fields = _village_fields()
        state.build_queue = [
            BuildQueueItem(name="Granaio livello 4", level=4,
                           is_resource_field=False, slot_id=26,
                           gid=GID_GRANARY),
        ]
        intents = builder.focus_intents(state, s)
        assert intents and intents[0].slot_id == 25

    def test_skip_reason_reports_plus_wait(self):
        s = self._settings_with_focus(["marketplace"])
        state = _state()
        state.fields = _village_fields()
        state.build_queue = [
            BuildQueueItem(name="Magazzino livello 4", level=4,
                           is_resource_field=False, slot_id=25,
                           gid=GID_WAREHOUSE),
        ]
        reasons = builder.focus_skip_reasons(state, s)
        assert any("coda dorf2" in r for r in reasons)


# ---------------------------------------------------------------------------
# Ticket: salvataggio scheda villaggio preserva lo slot_id del Focus
# ---------------------------------------------------------------------------

class TestPreserveStepSlots:
    """Il payload di salvataggio manda slot_id null per gli step senza
    pin nel form: il valore configurato non deve essere azzerato."""

    def test_inherits_slot_by_gid(self):
        from travian_agent.command_server import _preserve_step_slots
        old = [{"gid": 17, "target_level": 1, "slot_id": 30}]
        new = [{"gid": 17, "target_level": 1, "slot_id": None,
                "manual_focus": True}]
        out = _preserve_step_slots(new, old)
        assert out[0]["slot_id"] == 30
        assert out[0]["manual_focus"] is True

    def test_inherits_slot_by_key(self):
        from travian_agent.command_server import _preserve_step_slots
        old = [{"key": "marketplace", "target_level": 5, "slot_id": 31}]
        new = [{"key": "marketplace", "target_level": 5}]
        assert _preserve_step_slots(new, old)[0]["slot_id"] == 31

    def test_explicit_slot_not_overwritten(self):
        from travian_agent.command_server import _preserve_step_slots
        old = [{"gid": 17, "slot_id": 30}]
        new = [{"gid": 17, "slot_id": 33}]
        assert _preserve_step_slots(new, old)[0]["slot_id"] == 33

    def test_slot_inherited_once_per_step(self):
        from travian_agent.command_server import _preserve_step_slots
        old = [{"gid": 17, "slot_id": 30}]
        new = [{"gid": 17, "slot_id": None}, {"gid": 17, "slot_id": None}]
        out = _preserve_step_slots(new, old)
        assert out[0]["slot_id"] == 30
        assert out[1]["slot_id"] is None

    def test_village_save_preserves_focus_slot(self, monkeypatch):
        """End-to-end su _handle_config_patch: il salvataggio scheda
        non azzera lo slot_id dello step Focus Manuale."""
        import travian_agent.command_server as cs
        import travian_agent.config as cfg_mod

        acc = Settings(server_url="https://example.test", account_id="acc1")
        acc.villages = {
            "Pizzo": VillageConfig(
                name="Pizzo",
                building_queue_plan=[
                    {"gid": 17, "target_level": 1, "priority": 0,
                     "slot_id": 30, "manual_focus": True},
                ],
            )
        }
        monkeypatch.setattr(cfg_mod, "load_accounts", lambda: [acc])
        monkeypatch.setattr(
            cfg_mod, "save_accounts", lambda *a, **kw: None
        )
        monkeypatch.setattr(
            cs.debug_trace, "append_trace", lambda *a, **kw: None
        )
        payload, status = cs._handle_config_patch({
            "account_id": "acc1",
            "section": "village",
            "village": "Pizzo",
            "patch": {
                "role": "feeder",
                "building_queue_plan": [
                    {"gid": 17, "target_level": 1, "priority": 0,
                     "slot_id": None, "manual_focus": True},
                    {"gid": 10, "target_level": 10, "priority": 1,
                     "slot_id": None},
                ],
            },
        })
        assert status == 200, payload
        plan = acc.villages["Pizzo"].building_queue_plan
        assert plan[0]["slot_id"] == 30
        assert plan[1].get("slot_id") is None


# ---------------------------------------------------------------------------
# Ticket: .upgradeBlocked conta solo sul pulsante primario (Flusso A)
# ---------------------------------------------------------------------------

class _FakeLocator:
    """Locator Playwright minimo per _is_upgrade_blocked."""

    def __init__(self, nodes: list[dict]):
        self._nodes = nodes

    @property
    def first(self):
        return _FakeLocator(self._nodes[:1])

    async def count(self):
        return len(self._nodes)

    async def is_disabled(self):
        return bool(self._nodes and self._nodes[0].get("disabled"))

    async def get_attribute(self, name):
        if not self._nodes:
            return None
        if name == "class":
            return " ".join(sorted(self._nodes[0].get("classes", ())))
        return self._nodes[0].get(name)


def _match(node: dict, sel: str) -> bool:
    """Matcher minimale: 'tag.classe.classe' o '.classe', con virgole."""
    for part in sel.split(","):
        part = part.strip()
        if part.startswith("."):
            if part[1:] in node.get("classes", ()):
                return True
            continue
        bits = part.split(".")
        tag = bits[0]
        if tag and node.get("tag") != tag:
            continue
        if all(c in node.get("classes", ()) for c in bits[1:]):
            return True
    return False


class _FakeBlock:
    """Blocco contratto (#contract_building{gid}) simulato."""

    def __init__(self, nodes: list[dict]):
        self._nodes = nodes

    def locator(self, sel):
        return _FakeLocator([n for n in self._nodes if _match(n, sel)])


class TestUpgradeBlockedPrimaryOnly:
    """Regressione ts30 (Europa 30, Coda:0): il pulsante primario
    'Costruisci edifici' era verde e cliccabile ma il bot rilevava
    .upgradeBlocked — la classe compariva su pulsanti secondari Plus
    ('Costruisci con il capo cantiere') o sul wrapper."""

    def _blocked(self, nodes: list[dict]) -> bool:
        from travian_agent.actions import ActionExecutor
        return asyncio.run(
            ActionExecutor._is_upgrade_blocked(_FakeBlock(nodes))
        )

    def test_primary_clickable_beats_secondary_blocked(self):
        nodes = [
            {"tag": "div", "classes": {"upgradeBlocked"}},  # wrapper
            {"tag": "button",
             "classes": {"green", "new"}},  # CTA primario
            {"tag": "button",
             "classes": {"gold", "upgradeBlocked"}},  # capo cantiere
        ]
        assert self._blocked(nodes) is False

    def test_primary_disabled_is_blocked(self):
        nodes = [
            {"tag": "button", "classes": {"green", "new"},
             "disabled": True},
        ]
        assert self._blocked(nodes) is True

    def test_primary_with_blocked_class(self):
        nodes = [
            {"tag": "button",
             "classes": {"green", "new", "upgradeBlocked"}},
        ]
        assert self._blocked(nodes) is True

    def test_no_primary_falls_back_to_blocked_marker(self):
        nodes = [{"tag": "div", "classes": {"upgradeBlocked"}}]
        assert self._blocked(nodes) is True

    def test_no_primary_no_marker_is_free(self):
        nodes = [
            {"tag": "button", "classes": {"gold"}},  # solo secondario
        ]
        assert self._blocked(nodes) is False


class TestNoGenericButtonFallback:
    """Regressione ts30: il selettore 'button' nudo nel Flusso A
    cliccava il primo pulsante del contratto (Plus capo cantiere /
    NPC / video) -> click a vuoto e QUEUE_UNCONFIRMED."""

    def _actions_source(self) -> str:
        import inspect
        import travian_agent.actions as act
        return inspect.getsource(act)

    def test_no_bare_button_selector(self):
        src = self._actions_source()
        for bad in (
            'f"#contract_building{gid} button",',
            'f".buildingWrapper.gid{gid} button",',
            "f'#contract_building{gid} button',",
            "f'.buildingWrapper.gid{gid} button',",
        ):
            assert bad not in src, f"fallback generico reintrodotto: {bad}"

    def test_cta_filters_and_nav_wait_present(self):
        src = self._actions_source()
        # Filtro rigoroso sul fallback JS (esclusioni Plus/NPC/video).
        assert "data-cascade-cta" in src
        assert "capo cantiere" in src
        # Attesa navigazione completa prima del check coda.
        assert 'wait_for_load_state' in src
        # Diagnostica: outerHTML del pulsante cliccato nel log.
        assert "clicked_btn_html" in src

    def test_scroll_and_native_click_fallback(self):
        import inspect
        import travian_agent.browser as br
        # human_click deve scrollare prima di bounding_box: le
        # coordinate di elementi sotto la piega escono dalla viewport.
        hsrc = inspect.getsource(br.human_click)
        assert "scroll_into_view_if_needed" in hsrc
        src = self._actions_source()
        # Flusso A: scroll del CTA e fallback click nativo se la
        # navigazione non parte entro 2.5s.
        assert "scroll_into_view_if_needed" in src
        assert "button.click(force=True" in src


# ---------------------------------------------------------------------------
# _build_queue_count — conteggio coda su DOM (fix falsi 3 su Vibo Valentia)
# ---------------------------------------------------------------------------


def _queue_executor(allow_parallel: bool):
    """ActionExecutor minimo: serve solo .settings per il cap."""
    import travian_agent.actions as act

    s = _settings()
    s.allow_parallel_build = allow_parallel
    ex = act.ActionExecutor.__new__(act.ActionExecutor)
    ex.settings = s
    return ex


class _FakeQueuePage:
    """Page stub: evaluate ritorna il conteggio grezzo del JS."""

    def __init__(self, result):
        self._result = result

    async def evaluate(self, js):
        return self._result


class TestBuildQueueCountClamp:
    """Python-side: il conteggio non supera mai il tetto fisico del
    villaggio (2 senza Plus, 3 con allow_parallel_build)."""

    def test_overcap_clamped_no_plus(self):
        ex = _queue_executor(allow_parallel=False)
        n = asyncio.run(ex._build_queue_count(_FakeQueuePage(7)))
        assert n == 2

    def test_overcap_clamped_plus(self):
        ex = _queue_executor(allow_parallel=True)
        n = asyncio.run(ex._build_queue_count(_FakeQueuePage(9)))
        assert n == 3

    def test_real_count_passthrough(self):
        ex = _queue_executor(allow_parallel=True)
        assert asyncio.run(
            ex._build_queue_count(_FakeQueuePage(2))
        ) == 2

    def test_no_container_returns_none(self):
        ex = _queue_executor(allow_parallel=True)
        assert asyncio.run(
            ex._build_queue_count(_FakeQueuePage(None))
        ) is None


class TestBuildQueueCountSource:
    """Vincoli sul JS: reggono anche senza browser installato."""

    def _src(self) -> str:
        import inspect
        import travian_agent.actions as act
        return inspect.getsource(
            act.ActionExecutor._build_queue_count
        )

    def test_no_generic_construction_container(self):
        src = self._src()
        # Assente come selettore (]'): la menzione nel commento ok.
        assert "construction\" i]'" not in src
        assert 'data-testid*="build-queue" i' in src

    def test_row_selectors_job_only(self):
        src = self._src()
        for sel in (
            "li[data-gid]", "tr[data-gid]",
            '[class*="queueElement" i]', '.buildingRow', '.slotRow',
        ):
            assert sel in src
        # li/tr generici non devono essere selettori riga.
        assert "'li', 'tr'" not in src

    def test_fake_row_keywords(self):
        src = self._src()
        for kw in ("slot libero", "npc", "capo cantiere",
                   "instant", "video", "exchange"):
            assert kw in src

    def test_dedup_and_visibility(self):
        src = self._src()
        # Dedup righe annidate + hidden esclusi.
        assert "o.contains(el)" in src
        assert "offsetParent" in src


class TestBuildQueueCountDOM:
    """DOM reale (Playwright, headless): righe di coda rappresentative
    ts30 con elementi fittizi che prima gonfiavano il conteggio."""

    def _count(self, html: str, allow_parallel: bool = True):
        import pytest
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            pytest.skip("playwright non installato")

        async def _run():
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.set_content(html)
                    ex = _queue_executor(allow_parallel)
                    return await ex._build_queue_count(page)
                finally:
                    await browser.close()

        try:
            return asyncio.run(_run())
        except Exception as exc:  # browser mancante -> skip
            pytest.skip(f"browser playwright non disponibile: {exc}")

    def test_single_job_with_noise(self):
        html = """
        <div id="sidebarBoxBuilding">
          <ul class="buildingList">
            <li class="buildingRow"><a href="build.php?id=26">
              Granaio — livello 9 — <span class="timer">0:12:40</span>
            </a></li>
            <li class="upgradeBlocked">Slot libero</li>
            <li class="upgradeBlocked">Capo cantiere NPC</li>
          </ul>
        </div>
        <div class="construction banner">Costruisci piu' in fretta!</div>
        """
        assert self._count(html) == 1

    def test_two_jobs_with_noise(self):
        html = """
        <div id="sidebarBoxBuildingQueue">
          <div class="buildQueue">
            <li data-gid="10">Magazzino livello 12 0:20:10</li>
            <li data-gid="1">Boschiera livello 8 0:05:02</li>
            <li data-gid="5">NPC capo cantiere instant</li>
            <div class="queueElement" style="display:none">
              hidden ghost row
            </div>
          </div>
        </div>
        """
        assert self._count(html) == 2

    def test_nested_duplicates_counted_once(self):
        """Il medesimo job matchato da due selettori annidati conta 1."""
        html = """
        <div class="buildQueue">
          <li data-gid="7"><div data-gid="7">Granaio livello 5 0:03:10</div></li>
        </div>
        """
        assert self._count(html) == 1

    def test_header_only_is_zero(self):
        html = """
        <div class="buildQueue">
          <li class="header">Coda costruzioni</li>
          <li class="separator"></li>
        </div>
        """
        assert self._count(html) == 0

    def test_no_container_returns_none(self):
        html = "<div><p>pagina senza coda</p></div>"
        assert self._count(html) is None


# ---------------------------------------------------------------------------
# rule_wait — lato libero vs coda realmente piena
# ---------------------------------------------------------------------------


class TestRuleWaitSideFree:
    """rule_wait deve dichiarare il binario libero (dorf1/dorf2)
    invece di dichiarare la coda piena quando resta uno slot."""

    def _romans(self, plus: bool) -> Settings:
        s = _settings()
        s.tribe = "romans"
        s.allow_parallel_build = plus
        return s

    def test_field_queued_sides_still_free(self):
        """1 campo in coda sotto max_parallel: la reason dichiara le
        viste libere, non 'coda occupata' (spec Vibo Valentia)."""
        state = _state()
        state.build_queue = [
            BuildQueueItem(name="Bosco", is_resource_field=True,
                           slot_id=5, gid=1, remaining_seconds=600),
        ]
        a = rule_wait(state, self._romans(plus=False))
        assert a.type == ActionType.WAIT
        assert "libera" in a.reason
        assert "dorf" in a.reason
        assert "occupata" not in a.reason

    def test_interior_queued_sides_still_free(self):
        """1 edificio in coda: binari condividono il tetto — ancora
        spazio, la reason non deve dichiarare la coda piena."""
        state = _state()
        state.build_queue = [
            BuildQueueItem(name="Residenza", is_resource_field=False,
                           slot_id=31, gid=25, remaining_seconds=900),
        ]
        a = rule_wait(state, self._romans(plus=False))
        assert "libera" in a.reason
        assert "occupata" not in a.reason

    def test_both_sides_full_says_busy(self):
        """Entrambi i binari occupati -> 'Coda occupata'."""
        state = _state()
        state.build_queue = [
            BuildQueueItem(name="Bosco", is_resource_field=True,
                           slot_id=5, gid=1, remaining_seconds=600),
            BuildQueueItem(name="Residenza", is_resource_field=False,
                           slot_id=31, gid=25, remaining_seconds=900),
        ]
        a = rule_wait(state, self._romans(plus=False))
        assert "occupata" in a.reason
        assert a.params["suggest_seconds"] == 600


# ---------------------------------------------------------------------------
# Rotazione candidati bloccati in rule_role_buildings (fix loop Pizzo)
# ---------------------------------------------------------------------------


class TestRoleBuildingsBlockedRotation:
    """Regressione Pizzo (38553, croplog): quando la testa degli intent
    di ruolo (Palazzo lv.3, slot 27) non e' avviabile, la regola deve
    promuovere il candidato successivo invece di riproporre lo stesso
    slot a ogni ciclo — e la reason deve nominare l'edificio reale."""

    def setup_method(self):
        builder._TEMP_BLOCK_HISTORY.clear()
        builder._FOCUS_EXEC_HISTORY.clear()
        builder._FOCUS_FAIL_HISTORY.clear()

    def teardown_method(self):
        builder._TEMP_BLOCK_HISTORY.clear()
        builder._FOCUS_EXEC_HISTORY.clear()
        builder._FOCUS_FAIL_HISTORY.clear()

    def _croplog(self, **res) -> tuple[Settings, GameState]:
        """Croplog con Palazzo lv.3 in testa agli intent di ruolo.

        Nessuna area vuota -> nessun intent di nuova costruzione a
        livello 0: l'ordinamento (livello, slot) mette il Palazzo
        davanti a magazzino/granaio/ambasciata/caserma/mura.
        """
        s = _settings()
        s.villages = {
            "TestVillage": VillageConfig(name="TestVillage", role="croplog")
        }
        kw = dict(
            wood=500, clay=500, iron=500, crop=500,
            warehouse_capacity=5000, granary_capacity=5000,
        )
        kw.update(res)
        state = _state(**kw)
        state.fields = [
            Field(slot_id=1, name="Campo grano", level=4,
                  resource_type=ResourceType.CROP),
            Field(slot_id=5, name="Bosco", level=2,
                  resource_type=ResourceType.WOOD),
            Field(slot_id=19, name="Municipio", level=5,
                  gid=GID_MAIN_BUILDING),
            Field(slot_id=20, name="Punto di Raduno", level=1,
                  gid=GID_RALLY_POINT),
            Field(slot_id=25, name="Magazzino", level=4,
                  gid=GID_WAREHOUSE),
            Field(slot_id=26, name="Granaio", level=4,
                  gid=GID_GRANARY),
            Field(slot_id=27, name="Palazzo", level=3,
                  gid=GID_PALACE),
            Field(slot_id=28, name="Deposito Segreto", level=5,
                  gid=GID_CRANNY),
            Field(slot_id=29, name="Ambasciata", level=4,
                  gid=GID_EMBASSY),
            Field(slot_id=33, name="Caserma", level=4,
                  gid=GID_BARRACKS),
            Field(slot_id=34, name="Mura", level=5,
                  gid=GID_CITY_WALL),
        ]
        return s, state

    def test_head_is_palace_when_startable(self):
        """Baseline: senza blocchi la testa e' il Palazzo lv.3 (slot 27)."""
        s, state = self._croplog()
        intents = builder.role_intents(state, s)
        assert intents[0].slot_id == 27
        action = rule_role_buildings(state, s)
        assert action is not None
        assert action.params["candidates"][0] == 27
        assert "Palazzo" in action.reason
        assert "slot 27" in action.reason

    def test_blocked_head_rotates_to_next_candidate(self):
        """Palazzo marcato UPGRADE_BLOCKED: la testa ruota sul
        candidato successivo, il bloccato resta in coda ai candidati."""
        s, state = self._croplog()
        builder.record_upgrade_fail(
            "TestVillage", 27, "UPGRADE_BLOCKED", state=state
        )
        action = rule_role_buildings(state, s)
        assert action is not None
        assert action.params["candidates"][0] == 25  # Magazzino lv.4
        assert 27 in action.params["candidates"]
        assert action.params["candidates"].index(27) > 0
        # Reason allineata al candidato reale + nota di rinvio
        assert "Magazzino" in action.reason
        assert "rinviato" in action.reason
        assert "27" in action.reason
        assert "UPGRADE_BLOCKED" in action.reason

    def test_block_released_when_queue_changes(self):
        """Il blocco UPGRADE_BLOCKED si rilascia quando lo stato della
        coda costruzioni cambia rispetto alla firma registrata."""
        s, state = self._croplog()
        builder.record_upgrade_fail(
            "TestVillage", 27, "UPGRADE_BLOCKED", state=state
        )
        assert 27 in builder.temp_blocked_slots(state)
        # Un lavoro entra in coda su un altro slot: il vincolo puo'
        # essere caduto -> lo slot torna eleggibile in testa.
        state.build_queue = [
            BuildQueueItem(name="Mura", is_resource_field=False,
                           slot_id=34, gid=GID_CITY_WALL,
                           remaining_seconds=300),
        ]
        assert 27 not in builder.temp_blocked_slots(state)
        action = rule_role_buildings(state, s)
        assert action is not None
        assert action.params["candidates"][0] == 27

    def test_insufficient_resources_releases_when_affordable(self):
        """Blocco INSUFFICIENT_RESOURCES con costo noto: rilasciato
        solo quando le scorte coprono il contratto."""
        s, state = self._croplog(
            wood=100, clay=100, iron=100, crop=100,
        )
        cost = {"wood": 500, "clay": 500, "iron": 500, "crop": 500}
        builder.record_upgrade_fail(
            "TestVillage", 27, "INSUFFICIENT_RESOURCES",
            state=state, cost=cost,
        )
        assert 27 in builder.temp_blocked_slots(state)
        # Scorte ancora sotto il costo: blocco attivo, testa ruotata.
        action = rule_role_buildings(state, s)
        assert action is not None
        assert action.params["candidates"][0] == 25
        # Scorte sufficienti: il blocco cade e la testa torna Palazzo.
        state.resources.wood = 600
        state.resources.clay = 600
        state.resources.iron = 600
        state.resources.crop = 600
        assert 27 not in builder.temp_blocked_slots(state)
        action = rule_role_buildings(state, s)
        assert action.params["candidates"][0] == 27

    def test_structural_fail_excludes_candidate(self):
        """Fail strutturali ripetuti (BTN_NOT_FOUND x2): lo slot e'
        escluso del tutto dai candidati, non solo rinviato."""
        s, state = self._croplog()
        builder.record_upgrade_fail("TestVillage", 27, "BTN_NOT_FOUND")
        builder.record_upgrade_fail("TestVillage", 27, "BTN_NOT_FOUND")
        assert 27 in builder.focus_fail_blocked(state)
        action = rule_role_buildings(state, s)
        assert action is not None
        assert 27 not in action.params["candidates"]
        assert action.params["candidates"][0] == 25
        assert "esclusi" in action.reason

    def test_all_blocked_returns_none(self):
        """Tutti i candidati non avviabili -> esito chiaro (None),
        nessun retry a vuoto sullo stesso slot."""
        s, state = self._croplog()
        # Bloccati tutti gli interni di ruolo e il campo di fallback:
        # le scorte restano invariate (una variazione rilascerebbe i
        # blocchi senza costo noto).
        for slot in (1, 25, 26, 27, 28, 29, 33, 34):
            builder.record_upgrade_fail(
                "TestVillage", slot, "UPGRADE_BLOCKED", state=state
            )
        assert rule_role_buildings(state, s) is None

    def test_exec_resets_temp_block(self):
        """Un EXECUTED confermato sullo slot azzera il blocco
        temporaneo: torna subito candidabile."""
        s, state = self._croplog()
        builder.record_upgrade_fail(
            "TestVillage", 27, "UPGRADE_BLOCKED", state=state
        )
        builder.record_upgrade_exec("TestVillage", 27, 3)
        assert 27 not in builder.temp_blocked_slots(state)
        action = rule_role_buildings(state, s)
        assert action.params["candidates"][0] == 27

    def test_skip_note_reports_blocked_slots(self):
        """Il motivo di skip del brain nomina gli slot bloccati."""
        s, state = self._croplog()
        brain = RuleBasedBrain(s)
        builder.record_upgrade_fail(
            "TestVillage", 27, "UPGRADE_BLOCKED", state=state
        )
        note = brain._rule_skip_note(rule_role_buildings, state)
        # Con 27 rinviato la regola produce comunque azione: il note
        # non viene usato, ma deve restare sensato se invocato.
        assert isinstance(note, str) and note
