"""Regression test per Smart Troop Replenishment e Defense lock.

Copre:
- la riserva anti-saturazione ``3 x costo colono`` (spec 2.1);
- il clamp del piano al surplus di risorse;
- la guardia grano sul bilancio netto ufficiale ``crop_balance``
  (production.php) invece della produzione lorda;
- il lock temporaneo di ``DefenseManager`` che congela il rimpiazzo
  durante l'emergenza (spec 4.1).
"""

from __future__ import annotations

import time

from travian_agent.config import ReplenishmentConfig, Settings
from travian_agent.defense import DefenseManager
from travian_agent.models import GameState, Resources, ResourceType
from travian_agent.replenishment import (
    TroopReplenishmentManager,
    clamp_plan_to_budget,
    settler_reserve,
)


def _settings() -> Settings:
    s = Settings(server_url="https://example.test", account_id="t")
    s.replenishment = ReplenishmentConfig(
        enabled=True,
        skip_while_building=True,
        crop_reserve_pct=0.25,
    )
    return s


def _state(**res_kw) -> GameState:
    return GameState(
        resources=Resources(**res_kw),
        village_name="V1",
        village_id=1,
    )


def _mgr(tmp_path) -> TroopReplenishmentManager:
    return TroopReplenishmentManager(
        account_id="t", state_path=tmp_path / "rep.json"
    )


def test_settler_reserve_is_three_times_max_cost():
    # Max per risorsa fra t10/t20/t30, moltiplicato per 3 coloni.
    assert settler_reserve() == {
        "wood": 3 * 5800,   # t20
        "clay": 3 * 5600,   # t30
        "iron": 3 * 5800,   # t10
        "crop": 3 * 5200,   # t20
    }


def test_clamp_plan_to_budget_limits_quantities():
    # t1 costa 120/100/150/30: il legno e' il collo di bottiglia.
    out = clamp_plan_to_budget(
        {"t1": 10},
        {"wood": 250, "clay": 10_000, "iron": 10_000, "crop": 10_000},
    )
    assert out == {"t1": 2}


def test_clamp_plan_to_budget_drops_unaffordable_units():
    out = clamp_plan_to_budget(
        {"t1": 5},
        {"wood": 0, "clay": 0, "iron": 0, "crop": 0},
    )
    assert out == {}


def test_plan_training_suspended_when_net_crop_at_or_below_threshold(tmp_path):
    mgr = _mgr(tmp_path)
    state = _state(
        wood=100_000,
        clay=100_000,
        iron=100_000,
        crop=50_000,
        granary_capacity=100_000,
        crop_balance=10,  # <= soglia globale 10 -> Grain Safety Net
    )
    plan = mgr.plan_training(
        {"t1": 50}, state, _settings(), village_name="V1"
    )
    assert plan == {}


def test_plan_training_allowed_when_net_crop_above_threshold(tmp_path):
    mgr = _mgr(tmp_path)
    state = _state(
        wood=100_000,
        clay=100_000,
        iron=100_000,
        crop=50_000,
        granary_capacity=100_000,
        crop_balance=500,
    )
    plan = mgr.plan_training(
        {"t1": 50}, state, _settings(), village_name="V1"
    )
    assert plan == {"t1": 50}


def test_plan_training_falls_back_to_production_when_balance_none(tmp_path):
    mgr = _mgr(tmp_path)
    state = _state(
        wood=100_000,
        clay=100_000,
        iron=100_000,
        crop=50_000,
        granary_capacity=100_000,
        crop_balance=None,
        production_per_hour={ResourceType.CROP: 500},
    )
    plan = mgr.plan_training(
        {"t1": 50}, state, _settings(), village_name="V1"
    )
    assert plan == {"t1": 50}


def test_plan_training_respects_settler_reserve(tmp_path):
    mgr = _mgr(tmp_path)
    # t1 costa ferro 150: con riserva coloni (3x5800=17400 ferro) il budget
    # scende a 20000-17400=2600 -> max 17 legionari.
    state = _state(
        wood=100_000,
        clay=100_000,
        iron=20_000,
        crop=50_000,
        granary_capacity=100_000,
        crop_balance=500,
    )
    plan = mgr.plan_training(
        {"t1": 50}, state, _settings(), village_name="V1",
        reserve_settlers=True,
    )
    assert plan == {"t1": 17}


def test_plan_training_without_reserve_uses_full_stock(tmp_path):
    mgr = _mgr(tmp_path)
    state = _state(
        wood=100_000,
        clay=100_000,
        iron=20_000,
        crop=50_000,
        granary_capacity=100_000,
        crop_balance=500,
    )
    plan = mgr.plan_training(
        {"t1": 50}, state, _settings(), village_name="V1"
    )
    assert plan == {"t1": 50}


def test_defense_emergency_lock_freezes_and_expires(tmp_path):
    mgr = DefenseManager(account_id="t", state_path=tmp_path / "def.json")
    now = time.time()
    assert not mgr.is_emergency_locked(42, now=now)

    mgr._lock_village(42, now + 300)
    assert mgr.is_emergency_locked(42, now=now)
    assert mgr.is_emergency_locked("42", now=now)
    assert not mgr.is_emergency_locked(42, now=now + 400)
    # Altro villaggio non interessato
    assert not mgr.is_emergency_locked(7, now=now)


def test_defense_emergency_lock_persists(tmp_path):
    path = tmp_path / "def.json"
    now = time.time()
    mgr = DefenseManager(account_id="t", state_path=path)
    mgr._lock_village(42, now + 300)

    mgr2 = DefenseManager(account_id="t", state_path=path)
    assert mgr2.is_emergency_locked(42, now=now)
    assert not mgr2.is_emergency_locked(42, now=now + 400)
