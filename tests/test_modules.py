"""Test per i moduli autonomi farming.py / expansion.py e la loro config."""

from __future__ import annotations

import random

from travian_agent.config import (
    ExpansionConfig,
    ExpansionTarget,
    FarmingConfig,
    Settings,
    VillageConfig,
)
from travian_agent.expansion import (
    ExpansionManager,
    count_settlers,
    feeder_surplus,
    find_market,
    find_residence,
)
from travian_agent.farming import FarmingManager, find_rally_point
from travian_agent.models import (
    Field,
    GameState,
    GID_MARKETPLACE,
    GID_PALACE,
    GID_RALLY_POINT,
    GID_RESIDENCE,
    Resources,
)


def _settings() -> Settings:
    return Settings(server_url="https://ts.example.com", account_id="acc_test")


# ----------------------------------------------------------------------
# Config schema
# ----------------------------------------------------------------------

def test_farming_config_defaults():
    cfg = FarmingConfig()
    assert cfg.enabled is False
    assert cfg.interval_min_minutes == 12.0
    assert cfg.interval_max_minutes == 18.0
    assert cfg.farm_list == ""
    assert cfg.unit == ""


def test_expansion_config_defaults_and_targets():
    cfg = ExpansionConfig(
        auto_expand=True,
        founder_village="Bivona",
        target_slots=[
            ExpansionTarget(x=-15, y=72, note="15c"),
            ExpansionTarget(x=-12, y=68, note="hammer"),
        ],
    )
    assert cfg.auto_expand is True
    assert cfg.founder_village == "Bivona"
    assert len(cfg.target_slots) == 2
    assert cfg.target_slots[0].x == -15
    assert cfg.crop_min == 9


def test_settings_has_module_configs():
    s = _settings()
    assert isinstance(s.farming, FarmingConfig)
    assert isinstance(s.expansion, ExpansionConfig)
    assert s.farming.enabled is False
    assert s.expansion.auto_expand is False


def test_village_config_farm_fields():
    vc = VillageConfig(name="Bivona", farm_enabled=False, farm_unit="cavalieri")
    assert vc.farm_enabled is False
    assert vc.farm_unit == "cavalieri"


# ----------------------------------------------------------------------
# FarmingManager: jitter anti-ban e scheduling
# ----------------------------------------------------------------------

def test_farming_interval_is_jittered_within_bounds(tmp_path):
    s = _settings()
    s.farming.interval_min_minutes = 12.0
    s.farming.interval_max_minutes = 18.0
    mgr = FarmingManager("acc_j", state_path=tmp_path / "f.json")
    samples = {mgr.next_interval_seconds(s) for _ in range(200)}
    # Mai intervalli identici millimetrici: almeno 150 valori distinti su 200
    assert len(samples) > 150
    assert all(12.0 * 60 <= v <= 18.0 * 60 for v in samples)


def test_farming_schedule_persists(tmp_path):
    s = _settings()
    path = tmp_path / "farming.json"
    mgr = FarmingManager("acc_p", state_path=path)
    due = mgr.schedule_next("Bivona", s, now=1000.0)
    assert 1000.0 + 12 * 60 <= due <= 1000.0 + 18 * 60
    assert mgr.is_due("Bivona", now=due)
    assert not mgr.is_due("Bivona", now=1000.0)

    # Persistenza: nuova istanza rilegge lo stesso file
    mgr2 = FarmingManager("acc_p", state_path=path)
    assert mgr2.next_due.get("Bivona") == due


def test_farming_has_raiders():
    mgr = FarmingManager("acc_r", state_path=None)
    # Sensor vuoto -> tenta comunque (niente blocco)
    assert mgr._has_raiders({}, "cavalieri") is True
    # Unita' presenti vs alias
    troops = {"u5": 12, "u1": 3}
    assert mgr._has_raiders(troops, "equites") is True      # u5 = equites
    assert mgr._has_raiders(troops, "legionario") is True   # u1
    assert mgr._has_raiders(troops, "paladino") is False    # u15 assente
    # Qualunque truppa leggera
    assert mgr._has_raiders({"u11": 5}, "") is True
    assert mgr._has_raiders({"u99": 5}, "") is False


def test_find_rally_point():
    interior = [
        Field(slot_id=25, name="Mercato", level=5, gid=GID_MARKETPLACE),
        Field(slot_id=39, name="Punto di Raduno", level=1, gid=GID_RALLY_POINT),
    ]
    rp = find_rally_point(interior)
    assert rp is not None and rp.slot_id == 39
    assert find_rally_point([]) is None


# ----------------------------------------------------------------------
# ExpansionManager: helper puri
# ----------------------------------------------------------------------

def test_find_residence_min_level():
    interior = [
        Field(slot_id=26, name="Residenza", level=5, gid=GID_RESIDENCE),
        Field(slot_id=31, name="Palazzo", level=12, gid=GID_PALACE),
    ]
    assert find_residence(interior, min_level=10).name == "Palazzo"
    assert find_residence(interior, min_level=1).name == "Palazzo"  # livello maggiore
    assert find_residence([], min_level=1) is None


def test_count_settlers():
    assert count_settlers({"u10": 3}) == 3
    assert count_settlers({"u20": 1, "u10": 2}) == 3
    assert count_settlers({"u1": 50}) == 0
    assert count_settlers({}) == 0


def test_feeder_surplus_caps_and_threshold():
    state = GameState(
        resources=Resources(
            wood=8000, clay=2000, iron=1000, crop=9500,
            warehouse_capacity=10000, granary_capacity=10000,
        )
    )
    surplus = feeder_surplus(state)
    # wood: 8000 - 5500 = 2500 -> capped a 2000
    assert surplus["wood"] == 2000
    # clay: 2000 < 5500 -> assente
    assert "clay" not in surplus
    # crop: 9500 - 5500 = 4000 -> capped a 2000
    assert surplus["crop"] == 2000


def test_expansion_manager_state_persists(tmp_path):
    path = tmp_path / "exp.json"
    mgr = ExpansionManager("acc_e", state_path=path)
    mgr.feed_target = "Bivona"
    mgr.founded_tiles.append({"x": -15, "y": 72, "ts": 123.0})
    mgr._save()
    mgr2 = ExpansionManager("acc_e", state_path=path)
    assert mgr2.feed_target == "Bivona"
    assert mgr2.founded_tiles[0]["x"] == -15


def test_expansion_pick_founder(tmp_path):
    from travian_agent.models import Village

    s = _settings()
    mgr = ExpansionManager("acc_f", state_path=tmp_path / "e.json")
    villages = [Village(name="Bivona", village_id=1), Village(name="Hipponion", village_id=2)]
    interior_map = {
        "Bivona": [Field(slot_id=26, name="Residenza", level=10, gid=GID_RESIDENCE)],
        "Hipponion": [],
    }
    founder, res = mgr._pick_founder(s, villages, interior_map)
    assert founder is not None and founder.name == "Bivona"
    assert res is not None and res.slot_id == 26

    # Founder configurato esplicitamente
    s.expansion.founder_village = "Hipponion"
    founder2, res2 = mgr._pick_founder(s, villages, interior_map)
    assert founder2 is not None and founder2.name == "Hipponion"
    assert res2 is None  # nessuna residenza negli interni


def test_find_market():
    interior = [
        Field(slot_id=30, name="Piazzale", level=0, gid=0),
        Field(slot_id=34, name="Mercato", level=20, gid=GID_MARKETPLACE),
    ]
    m = find_market(interior)
    assert m is not None and m.slot_id == 34
