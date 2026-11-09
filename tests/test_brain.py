"""Test delle regole decisionali del RuleBasedBrain."""

from __future__ import annotations

from travian_agent.brain import (
    RuleBasedBrain,
    _storage_needed,
    rule_emergency_overflow,
    rule_upgrade_storage,
)
from travian_agent.config import Settings, VillageConfig
from travian_agent.models import (
    GID_BARRACKS,
    GID_GRANARY,
    GID_MARKETPLACE,
    GID_WAREHOUSE,
    ActionType,
    BuildQueueItem,
    Field,
    GameState,
    Resources,
    ResourceType,
    VillageRole,
)


def _settings() -> Settings:
    return Settings(server_url="https://example.test")


def _state(**res_kwargs) -> GameState:
    state = GameState(village_name="TestVillage")
    state.resources = Resources(**res_kwargs)
    return state


class TestStorageNeeded:
    def test_nothing_needed_when_below_threshold(self):
        state = _state(
            wood=500, clay=500, iron=500, crop=500,
            warehouse_capacity=1000, granary_capacity=1000,
        )
        assert _storage_needed(state) == []

    def test_warehouse_needed_when_one_resource_full(self):
        state = _state(
            wood=950, clay=100, iron=100, crop=100,
            warehouse_capacity=1000, granary_capacity=1000,
        )
        assert _storage_needed(state) == [GID_WAREHOUSE]

    def test_granary_needed_when_crop_full(self):
        state = _state(
            wood=0, clay=0, iron=0, crop=901,
            warehouse_capacity=1000, granary_capacity=1000,
        )
        assert _storage_needed(state) == [GID_GRANARY]

    def test_both_needed(self):
        state = _state(
            wood=999, clay=0, iron=0, crop=999,
            warehouse_capacity=1000, granary_capacity=1000,
        )
        assert _storage_needed(state) == [GID_WAREHOUSE, GID_GRANARY]

    def test_unread_capacity_means_no_rule(self):
        state = _state(wood=10_000, crop=10_000)  # capacita' = 0 (non letta)
        assert _storage_needed(state) == []


class TestRuleUpgradeStorage:
    def test_picks_lowest_level_warehouse_by_gid(self):
        state = _state(
            wood=950, warehouse_capacity=1000, granary_capacity=5000,
        )
        state.fields = [
            Field(slot_id=5, name="Bosco", level=3, resource_type=ResourceType.WOOD),
            Field(slot_id=25, name="Magazzino livello 5", level=5, gid=GID_WAREHOUSE),
            Field(slot_id=26, name="Magazzino livello 2", level=2, gid=GID_WAREHOUSE),
            Field(slot_id=30, name="Granaio livello 1", level=1, gid=GID_GRANARY),
        ]
        action = rule_upgrade_storage(state, _settings())
        assert action is not None
        assert action.type is ActionType.UPGRADE_FIELD
        assert action.params["candidates"][0] == 26  # livello piu' basso
        assert action.params["storage"] is True

    def test_falls_back_to_name_keywords(self):
        state = _state(
            crop=950, warehouse_capacity=5000, granary_capacity=1000,
        )
        state.fields = [
            Field(slot_id=27, name="Granaio livello 4", level=4, gid=-1),
        ]
        action = rule_upgrade_storage(state, _settings())
        assert action is not None
        assert action.params["candidates"] == [27]

    def test_no_storage_found_returns_none(self):
        state = _state(wood=950, warehouse_capacity=1000)
        state.fields = [
            Field(slot_id=25, name="Piazzale Raduni livello 1", level=1, gid=16),
        ]
        assert rule_upgrade_storage(state, _settings()) is None

    def test_queue_full_blocks_rule(self):
        state = _state(wood=950, warehouse_capacity=1000)
        state.fields = [
            Field(slot_id=25, name="Magazzino", level=5, gid=GID_WAREHOUSE),
        ]
        # Edificio interno gia' in coda: blocca l'ulteriore upgrade interno
        state.build_queue = [BuildQueueItem(name="Magazzino", remaining_seconds=60, is_resource_field=False)]
        assert rule_upgrade_storage(state, _settings()) is None

    def test_resource_in_queue_allows_interior_upgrade(self):
        state = _state(wood=950, warehouse_capacity=1000)
        state.fields = [
            Field(slot_id=25, name="Magazzino", level=5, gid=GID_WAREHOUSE),
        ]
        # Un campo risorsa in costruzione NON blocca l'upgrade di un edificio interno
        state.build_queue = [BuildQueueItem(name="Bosco", remaining_seconds=60, is_resource_field=True)]
        action = rule_upgrade_storage(state, _settings())
        assert action is not None
        assert action.type is ActionType.UPGRADE_FIELD
        assert action.params["candidates"] == [25]

    def test_below_threshold_returns_none(self):
        state = _state(
            wood=100, warehouse_capacity=1000, granary_capacity=1000,
        )
        state.fields = [
            Field(slot_id=25, name="Magazzino", level=5, gid=GID_WAREHOUSE),
        ]
        assert rule_upgrade_storage(state, _settings()) is None


class TestRulePriority:
    def test_storage_beats_resource_field_when_full(self):
        state = _state(
            wood=950, clay=200, iron=200, crop=100,
            warehouse_capacity=1000, granary_capacity=2000,
        )
        state.fields = [
            Field(slot_id=5, name="Bosco", level=1, resource_type=ResourceType.WOOD),
            Field(slot_id=25, name="Magazzino", level=4, gid=GID_WAREHOUSE),
        ]
        action = RuleBasedBrain(_settings()).decide(state)
        assert action.type is ActionType.UPGRADE_FIELD
        assert action.params.get("storage") is True

    def test_resource_field_upgrade_when_storage_ok(self):
        state = _state(
            wood=10, clay=200, iron=200, crop=100,
            warehouse_capacity=2000, granary_capacity=2000,
        )
        state.fields = [
            Field(slot_id=5, name="Bosco livello 1", level=1, resource_type=ResourceType.WOOD),
            Field(slot_id=25, name="Magazzino", level=4, gid=GID_WAREHOUSE),
        ]
        action = RuleBasedBrain(_settings()).decide(state)
        assert action.type is ActionType.UPGRADE_FIELD
        assert action.params["candidates"] == [5]

    def test_interior_building_in_queue_allows_resource_field_upgrade(self):
        state = _state(
            wood=10, clay=200, iron=200, crop=100,
            warehouse_capacity=2000, granary_capacity=2000,
        )
        state.fields = [
            Field(slot_id=5, name="Bosco livello 1", level=1, resource_type=ResourceType.WOOD),
            Field(slot_id=25, name="Magazzino", level=4, gid=GID_WAREHOUSE),
        ]
        # C'e' un magazzino (edificio interno) in costruzione: il campo risorsa puo' comunque partire!
        state.build_queue = [BuildQueueItem(name="Magazzino", remaining_seconds=120, is_resource_field=False)]
        action = RuleBasedBrain(_settings()).decide(state)
        assert action.type is ActionType.UPGRADE_FIELD
        assert action.params["candidates"] == [5]

    def test_both_queues_occupied_causes_wait(self):
        state = _state(
            wood=10, clay=200, iron=200, crop=100,
            warehouse_capacity=2000, granary_capacity=2000,
        )
        state.fields = [
            Field(slot_id=5, name="Bosco livello 1", level=1, resource_type=ResourceType.WOOD),
            Field(slot_id=25, name="Magazzino", level=4, gid=GID_WAREHOUSE),
        ]
        # Sia risorsa che edificio occupati: attesa
        state.build_queue = [
            BuildQueueItem(name="Bosco", remaining_seconds=60, is_resource_field=True),
            BuildQueueItem(name="Magazzino", remaining_seconds=120, is_resource_field=False),
        ]
        action = RuleBasedBrain(_settings()).decide(state)
        assert action.type is ActionType.WAIT


class TestEmergencyOverflow:
    def _full_state(self) -> GameState:
        state = _state(
            wood=2000, clay=1800, iron=2000, crop=2000,
            warehouse_capacity=2000, granary_capacity=2000,
        )
        # Coda interna occupata: rule_upgrade_storage non puo' agire
        state.build_queue = [
            BuildQueueItem(name="Magazzino", remaining_seconds=3600, is_resource_field=False)
        ]
        return state

    def test_no_emergency_when_not_full(self):
        state = _state(
            wood=100, warehouse_capacity=2000, granary_capacity=2000,
        )
        assert rule_emergency_overflow(state, _settings()) is None

    def test_no_emergency_when_upgrade_possible(self):
        """Coda libera + storage candidato: la regola upgrade gestisce."""
        state = _state(
            wood=2000, warehouse_capacity=2000, granary_capacity=5000,
        )
        state.fields = [
            Field(slot_id=25, name="Magazzino", level=5, gid=GID_WAREHOUSE),
        ]
        assert rule_emergency_overflow(state, _settings()) is None

    def test_send_resources_to_peer_with_most_space(self):
        state = self._full_state()
        state.fields = [
            Field(slot_id=30, name="Mercato", level=3, gid=GID_MARKETPLACE),
        ]
        state.peer_villages = {
            "Vuoto": Resources(
                wood=10, clay=10, iron=10, crop=10,
                warehouse_capacity=5000, granary_capacity=5000,
            ),
            "Pieno": Resources(
                wood=4000, clay=4000, iron=4000, crop=4000,
                warehouse_capacity=4000, granary_capacity=4000,
            ),
        }
        action = rule_emergency_overflow(state, _settings())
        assert action is not None
        assert action.type is ActionType.SEND_RESOURCES
        assert action.params["target"] == "Vuoto"
        assert action.params["market_slot"] == 30
        # 15% di 2000 = 300 per le risorse a tappo (clay a 1800 esclusa)
        assert action.params["resources"] == {"wood": 300, "iron": 300, "crop": 300}

    def test_train_troops_when_no_market(self):
        state = self._full_state()
        state.village_name = "Hipponion"
        state.fields = [
            Field(slot_id=28, name="Caserma", level=5, gid=GID_BARRACKS),
        ]
        s = _settings()
        s.villages = {
            "Hipponion": VillageConfig(
                name="Hipponion",
                role="hammer",
                priority_units=["Imperiani", "Cavalieri"],
            )
        }
        action = rule_emergency_overflow(state, s)
        assert action is not None
        assert action.type is ActionType.TRAIN_TROOPS
        assert action.params["trainer_slot"] == 28
        assert action.params["trainer_gid"] == GID_BARRACKS
        assert action.params["priority_units"] == ["Imperiani", "Cavalieri"]

    def test_alert_when_nothing_possible(self):
        state = self._full_state()
        state.fields = [
            Field(slot_id=25, name="Piazzale", level=1, gid=16),
        ]
        action = rule_emergency_overflow(state, _settings())
        assert action is not None
        assert action.type is ActionType.ALERT
        assert action.params.get("storage_full") is True

    def test_emergency_in_default_rules_chain(self):
        """Con coda piena e mercato disponibile, decide() produce SEND_RESOURCES."""
        state = self._full_state()
        state.fields = [
            Field(slot_id=5, name="Bosco", level=2, resource_type=ResourceType.WOOD),
            Field(slot_id=30, name="Mercato", level=3, gid=GID_MARKETPLACE),
        ]
        state.peer_villages = {
            "Peer": Resources(wood=0, warehouse_capacity=5000),
        }
        action = RuleBasedBrain(_settings()).decide(state)
        assert action.type is ActionType.SEND_RESOURCES


class TestVillageRoles:
    def test_role_parse_aliases(self):
        assert VillageRole.parse("croplog_capital") is VillageRole.CROPLOG
        assert VillageRole.parse("capital") is VillageRole.CROPLOG
        assert VillageRole.parse("hammer") is VillageRole.HAMMER
        assert VillageRole.parse("defence") is VillageRole.DEFENSIVE
        assert VillageRole.parse("economy") is VillageRole.FEEDER
        assert VillageRole.parse("ignoto") is VillageRole.STANDARD
        assert VillageRole.parse("") is VillageRole.STANDARD

    def test_village_config_by_name_and_id(self):
        s = _settings()
        s.villages = {
            "12345": VillageConfig(name="Hipponion", role="hammer"),
            "Vibo Valentia": VillageConfig(name="Vibo Valentia", role="feeder", target_village="Hipponion"),
        }
        assert s.village_config_for(name="Hipponion").role == "hammer"
        assert s.village_config_for(village_id=12345).role == "hammer"
        assert s.village_config_for(name="Vibo Valentia").target_village == "Hipponion"
        assert s.village_config_for(name="Sconosciuto") is None

    def test_role_resolver_precedence_and_fallback(self):
        s = _settings()
        s.villages = {"Pizzo": VillageConfig(name="Pizzo", role="croplog")}
        s.village_roles = {"Vecchio": "capital"}
        assert s.village_role_for("Pizzo") == "croplog"
        assert s.village_role_for("Vecchio") == "capital"  # fallback legacy
        assert s.village_role_for("Niente") == "standard"

    def test_croplog_role_reaches_field_rule(self):
        """Un villaggio croplog (via villages) potenzia i campi grano."""
        s = _settings()
        s.villages = {"Pizzo": VillageConfig(name="Pizzo", role="croplog")}
        state = _state(wood=0, clay=0, iron=0, crop=100,
                       warehouse_capacity=5000, granary_capacity=5000)
        state.village_name = "Pizzo"
        state.fields = [
            Field(slot_id=5, name="Bosco", level=1, resource_type=ResourceType.WOOD),
            Field(slot_id=7, name="Campo grano", level=3, resource_type=ResourceType.CROP),
        ]
        brain = RuleBasedBrain(s)
        action = brain.decide(state)
        assert action.type is ActionType.UPGRADE_FIELD
        assert action.params["candidates"][0] == 7
