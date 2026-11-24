"""Test bugfix: radar anti propri-villaggi, filtro coordinate e
mappa nomi truppe localizzate (spec Bugfix Farming/Scout/Parser)."""

import pytest

from travian_agent.ai_scout_brain import AIScoutBrain
from travian_agent.farm_store import FarmTargetStore
from travian_agent.sensors import _troop_label_map


@pytest.fixture
def brain(tmp_path):
    return AIScoutBrain("TEST", state_path=tmp_path / "scout.json")


@pytest.fixture
def farm(tmp_path):
    return FarmTargetStore("TEST", path=tmp_path / "farm.json")


OWN = {(-13, 70), (-13, 68), (-11, 69), (-30, 74)}


def _tile(x, y, name="", pop=50, oasis=False):
    return {
        "x": x, "y": y, "village_name": name,
        "population": pop, "is_oasis": oasis,
    }


# ---------------------------------------------------------------------------
# Bug 1: filtro coordinate sui propri villaggi
# ---------------------------------------------------------------------------


def test_radar_skips_own_village_coords(brain):
    """update_targets_from_map esclude le coordinate dei propri villaggi."""
    brain.update_targets_from_map(
        [_tile(-13, 70, "Hipponion"), _tile(-14, 71, "Nemico", 80)],
        origin_x=-13, origin_y=70,
        own_coords=OWN,
    )
    assert "-13_70" not in brain.targets
    assert "-14_71" in brain.targets


def test_radar_accepts_tiles_without_own_coords(brain):
    """Senza own_coords il comportamento resta quello legacy."""
    brain.update_targets_from_map(
        [_tile(-13, 70, "Hipponion")], origin_x=-13, origin_y=70
    )
    assert "-13_70" in brain.targets


def test_radar_purge_coords(brain):
    """purge_coords elimina i record storici dei propri villaggi."""
    brain.update_targets_from_map(
        [_tile(-13, 70, "Hipponion"), _tile(-14, 71, "Nemico")],
        origin_x=-13, origin_y=70,
    )
    removed = brain.purge_coords(OWN)
    assert removed == 1
    assert "-13_70" not in brain.targets
    assert "-14_71" in brain.targets
    # Idempotente: secondo giro non rimuove nulla
    assert brain.purge_coords(OWN) == 0


def test_farm_store_skip_coords(farm):
    """register_scanned esclude i tile alle coordinate proprie."""
    new = farm.register_scanned(
        [_tile(-13, 70, "Hipponion", 500), _tile(-14, 71, "Nemico", 80)],
        origin_x=-13, origin_y=70,
        skip_coords=OWN,
    )
    assert new == 1
    keys = {(t.x, t.y) for t in farm.list_targets()}
    assert (-13, 70) not in keys
    assert (-14, 71) in keys


def test_farm_store_purge_coords(farm):
    """purge_coords rimuove qualunque record, anche approved."""
    farm.register_scanned(
        [_tile(-13, 70, "Hipponion", 500), _tile(-14, 71, "Nemico", 80)],
        origin_x=-13, origin_y=70,
    )
    tid = farm.list_targets()[0].id
    farm.approve(tid)  # anche un approvato proprio va rimosso
    removed = farm.purge_coords(OWN)
    assert removed == 1
    keys = {(t.x, t.y) for t in farm.list_targets()}
    assert (-13, 70) not in keys
    assert (-14, 71) in keys
    assert farm.purge_coords(OWN) == 0  # idempotente


# ---------------------------------------------------------------------------
# Bug 3: mappa nomi localizzati -> 'uN'
# ---------------------------------------------------------------------------


def test_troop_label_map_covers_singular_plural():
    m = _troop_label_map()
    assert m["legionario"] == "u1"
    assert m["legionari"] == "u1"
    assert m["imperiano"] == "u3"
    assert m["imperiani"] == "u3"
    assert m["equites caesaris"] == "u6"
    # plurale della prima parola nei nomi composti
    if "catapulta da fuoco" in m:
        assert m["catapulte da fuoco"] == m["catapulta da fuoco"]
    # altre tribu' coperte
    assert m["falange"] == "u21"
    assert m["mazzieri"] == "u11"
