"""Test per travian_agent.stealth: pause moduli, jitter intervalli, sonno
notturno e sblocco su cambio risorse."""

from __future__ import annotations

import time
from datetime import datetime

from travian_agent.stealth import (
    ModulePauseRegistry,
    human_cycle_interval,
    night_sleep_remaining,
    resource_snapshot,
    resources_changed,
)


class _Res:
    """Stand-in di models.Resources per i test di snapshot."""

    def __init__(self, wood=1000, clay=800, iron=600, crop=400):
        self.wood = wood
        self.clay = clay
        self.iron = iron
        self.crop = crop


def test_resource_snapshot_from_object_and_dict() -> None:
    snap = resource_snapshot(_Res())
    assert snap == {"wood": 1000, "clay": 800, "iron": 600, "crop": 400}

    snap_dict = resource_snapshot(
        {"wood": 5, "clay": 6, "iron": 7, "crop": 8}
    )
    assert snap_dict == {"wood": 5, "clay": 6, "iron": 7, "crop": 8}

    assert resource_snapshot(None) is None
    assert resource_snapshot({"other": 1}) is None


def test_resources_changed_thresholds() -> None:
    base = _Res(wood=1000)
    # +100 unita' su 1000 = 10% e sotto il floor di 200: niente trigger
    assert not resources_changed(
        resource_snapshot(base), _Res(wood=1099)
    )
    # +300 unita' supera sia il floor che il 15%
    assert resources_changed(
        resource_snapshot(base), _Res(wood=1300)
    )
    # Stesse risorse: nessun cambio
    assert not resources_changed(
        resource_snapshot(base), _Res(wood=1000)
    )
    # Snapshot assente o risorse assenti: nessun confronto -> no cambio
    assert not resources_changed(None, _Res(wood=5000))
    assert not resources_changed(resource_snapshot(base), None)


def test_registry_register_and_remaining() -> None:
    reg = ModulePauseRegistry()
    acc, vkey = "acc1", "42"

    assert reg.remaining(acc, vkey, "farming") == 0.0

    reg.register(
        acc, vkey, "farming", "RAID_SEND",
        resources=_Res(), min_seconds=60, max_seconds=60,
    )

    left = reg.remaining(acc, vkey, "farming")
    assert 0.0 < left <= 60.0
    # decisione specifica matcha
    assert reg.remaining(acc, vkey, "farming", "RAID_SEND") > 0.0
    # altra decisione non matcha
    assert reg.remaining(acc, vkey, "farming", "RAID_TARGET") == 0.0
    # altro modulo / altro villaggio non matchano
    assert reg.remaining(acc, vkey, "scout") == 0.0
    assert reg.remaining(acc, "99", "farming") == 0.0
    assert reg.is_paused(acc, vkey, "farming")


def test_registry_unblocks_on_resource_change() -> None:
    reg = ModulePauseRegistry()
    acc, vkey = "acc1", "42"
    reg.register(
        acc, vkey, "replenishment", "TROOP_TRAINING",
        resources=_Res(wood=1000), min_seconds=3600, max_seconds=3600,
    )
    assert reg.is_paused(acc, vkey, "replenishment")

    # Risorse poco cambiate: ancora in pausa
    assert reg.is_paused(
        acc, vkey, "replenishment", resources=_Res(wood=1050)
    )
    # +5000 legno: sblocco anticipato
    assert not reg.is_paused(
        acc, vkey, "replenishment", resources=_Res(wood=6000)
    )


def test_registry_clear_on_executed() -> None:
    reg = ModulePauseRegistry()
    acc, vkey = "acc1", "7"
    reg.register(
        acc, vkey, "builder", "UPGRADE_FIELD",
        min_seconds=3600, max_seconds=3600,
    )
    assert reg.is_paused(acc, vkey, "builder")

    reg.clear(acc, vkey, "builder", "UPGRADE_FIELD")
    assert not reg.is_paused(acc, vkey, "builder")


def test_registry_expired_entries_pruned() -> None:
    reg = ModulePauseRegistry()
    acc, vkey = "acc1", "7"
    reg.register(
        acc, vkey, "smithy", "UNIT_UPGRADE",
        min_seconds=0.0, max_seconds=0.0,
    )
    # Durata zero: gia' scaduta al primo check
    assert not reg.is_paused(acc, vkey, "smithy")
    assert not reg._pauses


def test_human_cycle_interval_within_reasonable_bounds() -> None:
    lo, hi = 240.0, 840.0
    for _ in range(200):
        val = human_cycle_interval(lo, hi)
        # base triangolare in [lo, hi]; spike fino a hi*3
        assert val >= lo
        assert val <= hi * 3.0


def test_night_sleep_remaining_outside_window() -> None:
    # Con il seed deterministico l'inizio e' randomizzato +-1.5h: usiamo
    # una finestra oraria estremamente stretta per poter affermare che
    # un orario lontano e' fuori dalla finestra.
    now = datetime(2025, 6, 15, 15, 0)  # 15:00
    left = night_sleep_remaining(
        "acc1",
        base_hour=2.0,
        hours_min=0.1,
        hours_max=0.1,
        now=now,
    )
    assert left == 0.0


def test_night_sleep_window_deterministic_per_day() -> None:
    now = datetime(2025, 6, 15, 12, 0)
    # La finestra per lo stesso account/giorno e' deterministica: il
    # "sonno" non cambia imprevedibilmente tra un ciclo e l'altro.
    from travian_agent.stealth import nightly_window

    w1 = nightly_window("acc1", base_hour=1.0, now=now)
    w2 = nightly_window("acc1", base_hour=1.0, now=now)
    assert w1 == w2
    # Account diverso -> finestra diversa (quasi sempre)
    w3 = nightly_window("acc2", base_hour=1.0, now=now)
    assert w1 != w3


def test_night_sleep_inside_window() -> None:
    # Finestra che copre tutto il giorno: sempre in sonno
    now = datetime(2025, 6, 15, 15, 0)
    left = night_sleep_remaining(
        "acc1",
        base_hour=12.0,
        hours_min=24.0,
        hours_max=24.0,
        now=now,
    )
    assert left > 0.0
