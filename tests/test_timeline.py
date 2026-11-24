"""Test della proiezione "Scaletta Costruzioni" (timeline.py)."""

from __future__ import annotations

import pytest

from travian_agent.config import Settings, VillageConfig
from travian_agent.models import (
    BuildQueueItem,
    Field,
    GameState,
    Resources,
    ResourceType,
)
from travian_agent.timeline import project_timeline

NOW = 1_800_000_000.0


def _settings(**kw) -> Settings:
    kw.setdefault("server_url", "https://example.test")
    return Settings(**kw)


def _state(**res_kwargs) -> GameState:
    state = GameState(village_name="TestVillage", timestamp=NOW)
    res_kwargs.setdefault("production_per_hour", {
        "wood": 600, "clay": 600, "iron": 600, "crop": 600,
    })
    state.resources = Resources(**res_kwargs)
    return state


def _fields() -> list[Field]:
    """Villaggio minimo: 4 campi + Municipio 5 + area vuota."""
    return [
        Field(slot_id=1, name="Bosco", level=3,
              resource_type=ResourceType.WOOD),
        Field(slot_id=5, name="Grano", level=2,
              resource_type=ResourceType.CROP),
        Field(slot_id=25, name="Municipio", level=5, gid=15),
        Field(slot_id=30, name="", level=0, gid=0),  # area vuota
    ]


class TestInProgress:
    def test_active_builds_listed_first(self):
        state = _state(wood=10_000, clay=10_000, iron=10_000, crop=10_000)
        state.fields = _fields()
        state.build_queue = [
            BuildQueueItem(name="Mercato", level=2, remaining_seconds=900,
                           is_resource_field=False, slot_id=26),
            BuildQueueItem(name="Bosco", level=4, remaining_seconds=300,
                           is_resource_field=True, slot_id=1),
        ]
        out = project_timeline(state, _settings())
        items = [i for i in out["timeline"] if i["status"] == "in_progress"]
        assert len(items) == 2
        assert {i["queue"] for i in items} == {"dorf1", "dorf2"}
        assert items[0]["remaining_s"] == 900


class TestParallelBins:
    def test_dorf1_and_dorf2_progress_in_parallel(self):
        """Con coda piena il primo dorf1 parte quando finisce il lavoro
        dorf1, senza attendere il lavoro dorf2 (binari separati)."""
        state = _state(wood=10_000, clay=10_000, iron=10_000, crop=10_000)
        state.fields = _fields()
        state.build_queue = [
            BuildQueueItem(name="Bosco", level=4, remaining_seconds=100,
                           is_resource_field=True, slot_id=1),
            BuildQueueItem(name="Mercato", level=2, remaining_seconds=9000,
                           is_resource_field=False, slot_id=26),
        ]
        out = project_timeline(state, _settings())
        d1 = [i for i in out["timeline"]
              if i["status"] == "scheduled" and i["queue"] == "dorf1"]
        assert d1, "manca la proiezione dorf1"
        assert d1[0]["start_ts"] == pytest.approx(NOW + 100)
        assert d1[0]["start_ts"] < NOW + 9000


class TestSharedBin:
    def test_non_roman_single_queue_serializes(self):
        """Senza parallel build i candidati condividono un solo slot."""
        state = _state(wood=10_000, clay=10_000, iron=10_000, crop=10_000)
        state.fields = _fields()
        st = _settings(allow_parallel_build=False, tribe="gauls")
        out = project_timeline(state, st)
        sched = [i for i in out["timeline"] if i["status"] == "scheduled"]
        assert out["parallel_build"] is False
        assert len(sched) >= 2
        # Ogni inizio coincide con la fine del lavoro precedente.
        for prev, cur in zip(sched, sched[1:]):
            assert cur["start_ts"] == pytest.approx(prev["end_ts"])


class TestFocusPlan:
    def test_multi_level_expansion(self):
        """Residenza 0 -> 3 espande gli step 0->1, 1->2, 2->3."""
        state = _state(wood=10_000, clay=10_000, iron=10_000, crop=10_000)
        state.fields = _fields()
        st = _settings()
        st.villages["TestVillage"] = VillageConfig(
            name="TestVillage",
            building_queue_plan=[
                {"key": "residence", "target_level": 3, "priority": 1},
            ],
        )
        out = project_timeline(state, st)
        d2 = [i for i in out["timeline"]
              if i["status"] == "scheduled" and i["queue"] == "dorf2"]
        assert [i["to_level"] for i in d2] == [1, 2, 3]
        assert all(i["gid"] == 25 for i in d2)
        # Sequenziale: ogni inizio = fine precedente (risorse abbondanti).
        for prev, cur in zip(d2, d2[1:]):
            assert cur["start_ts"] == pytest.approx(prev["end_ts"])

    def test_disabled_focus_produces_no_dorf2(self):
        state = _state(wood=10_000, clay=10_000, iron=10_000, crop=10_000)
        state.fields = _fields()
        st = _settings()
        st.villages["TestVillage"] = VillageConfig(
            name="TestVillage",
            building_queue_plan=[
                {"key": "residence", "target_level": 3},
            ],
            building_focus_enabled=False,
        )
        out = project_timeline(state, st)
        d2 = [i for i in out["timeline"]
              if i["status"] == "scheduled" and i["queue"] == "dorf2"]
        assert d2 == []


class TestResourceConstraints:
    def test_waiting_resources_when_deficit(self):
        """Costo non coperto -> wait_reason waiting_resources e inizio
        posticipato all'accumulo stimato."""
        state = _state(
            wood=0, clay=0, iron=0, crop=0,
            production_per_hour={
                "wood": 360, "clay": 360, "iron": 360, "crop": 360,
            },
        )
        state.fields = _fields()
        out = project_timeline(state, _settings())
        sched = [i for i in out["timeline"] if i["status"] == "scheduled"]
        assert sched
        first = sched[0]
        assert first["wait_reason"] == "waiting_resources"
        assert first["start_ts"] > NOW
        assert first["missing"]

    def test_storage_cap_exceeded(self):
        """Costo oltre la capienza -> storage_cap_exceeded, nessun orario."""
        state = _state(
            wood=10_000, clay=10_000, iron=10_000, crop=10_000,
            warehouse_capacity=100,   # troppo piccolo per qualsiasi costo
            granary_capacity=100,
        )
        state.fields = _fields()
        out = project_timeline(state, _settings())
        sched = [i for i in out["timeline"] if i["status"] == "scheduled"]
        assert sched
        assert all(
            i["wait_reason"] == "storage_cap_exceeded" for i in sched
        )
        assert all(i["start_ts"] is None for i in sched)


class TestGrainSafety:
    def test_only_crop_fields_under_grain_lock(self):
        """Sotto soglia netto grano il binario dorf1 resta sui campi grano."""
        state = _state(
            wood=10_000, clay=10_000, iron=10_000, crop=10_000,
            crop_balance=-50,  # margine negativo: grain safety attiva
        )
        state.fields = _fields()
        st = _settings(min_net_crop_threshold=10.0)
        out = project_timeline(state, st)
        assert out["grain_lock"] is True
        d1 = [i for i in out["timeline"]
              if i["status"] == "scheduled" and i["queue"] == "dorf1"]
        assert d1
        assert all(
            i["source"] in ("system", "economy") for i in d1
        )
        # tutti i candidati economici sono campi di grano (gid 4)
        econ = [i for i in d1 if i["source"] == "economy"]
        assert econ and all(i["gid"] == 4 for i in econ)


class TestLimitsAndMeta:
    def test_limit_caps_scheduled_items(self):
        state = _state(wood=10_000, clay=10_000, iron=10_000, crop=10_000)
        state.fields = _fields()
        out = project_timeline(state, _settings(), limit=3)
        sched = [i for i in out["timeline"] if i["status"] == "scheduled"]
        assert len(sched) <= 3

    def test_meta_fields(self):
        state = _state(wood=1000, clay=1000, iron=1000, crop=1000)
        state.fields = _fields()
        out = project_timeline(state, _settings(server_speed=3.0))
        assert out["server_speed"] == 3.0
        assert out["main_building_level"] == 5
        assert out["generated_at"].endswith("Z")
