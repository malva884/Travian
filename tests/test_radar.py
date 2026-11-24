"""Test Radar Store — vettori movimento live del RADAR SETTORE.

Copre il modello dati (normalize_movement), la persistenza/dedup di
RadarStore, il sync passivo (sync_categories), il pruning ETA e la
telemetria (_radar_payload) che compone il payload villages[vid].
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from travian_agent.models import IncomingAttack, Village
from travian_agent.radar_store import (
    CAT_HERO_ADVENTURE,
    CAT_INCOMING_ATTACK,
    CAT_MERCHANT,
    CAT_OUTGOING_ATTACK,
    CAT_OUTGOING_RAID,
    VALID_CATEGORIES,
    RadarStore,
    make_move_id,
    merge_village_movements,
    normalize_movement,
)
from travian_agent import telemetry


@pytest.fixture()
def store(tmp_path: Path) -> RadarStore:
    return RadarStore("acc_test", path=tmp_path / "radar_test.json")


# ---------------------------------------------------------------------------
# Modello dati
# ---------------------------------------------------------------------------

class TestNormalizeMovement:
    def test_valid_outbound(self):
        mv = normalize_movement(
            "Vibo",
            {
                "category": CAT_MERCHANT,
                "direction": "outbound",
                "from_x": 78,
                "from_y": -7,
                "to_x": 97,
                "to_y": -22,
                "label": "Pizzo (97|-22)",
                "started_at": 1000.0,
                "arrives_at": 2800.0,
                "details": {"resources": {"crop": 800}},
            },
        )
        assert mv is not None
        assert mv["category"] == "merchant"
        assert mv["direction"] == "outbound"
        assert mv["from_x"] == 78
        assert mv["to_y"] == -22
        assert mv["details"]["resources"]["crop"] == 800
        assert mv["id"].startswith("mov_")

    def test_missing_arrives_at_rejected(self):
        assert normalize_movement(
            "Vibo", {"category": CAT_MERCHANT}
        ) is None

    def test_unknown_category_rejected(self):
        assert normalize_movement(
            "Vibo", {"category": "dragon", "arrives_at": 999.0}
        ) is None

    def test_default_direction_by_category(self):
        mv = normalize_movement(
            "Vibo",
            {"category": CAT_INCOMING_ATTACK, "arrives_at": 5000.0},
        )
        assert mv["direction"] == "inbound"
        mv2 = normalize_movement(
            "Vibo",
            {"category": CAT_OUTGOING_RAID, "arrives_at": 5000.0},
        )
        assert mv2["direction"] == "outbound"

    def test_coords_none_preserved(self):
        mv = normalize_movement(
            "Vibo",
            {"category": CAT_INCOMING_ATTACK, "arrives_at": 5000.0},
        )
        assert mv["from_x"] is None
        assert mv["to_x"] is None

    def test_id_deterministic_bucket(self):
        a = make_move_id("Vibo", CAT_MERCHANT, "Pizzo", 1000.0)
        b = make_move_id("Vibo", CAT_MERCHANT, "Pizzo", 1030.0)
        c = make_move_id("Vibo", CAT_MERCHANT, "Pizzo", 2000.0)
        assert a == b          # stesso bucket 60s
        assert a != c


# ---------------------------------------------------------------------------
# RadarStore: register / dedup / sync / prune
# ---------------------------------------------------------------------------

class TestRadarStore:
    def test_register_and_read(self, store: RadarStore):
        now = time.time()
        store.register(
            "Vibo",
            {
                "category": CAT_MERCHANT,
                "direction": "outbound",
                "from_x": 78, "from_y": -7,
                "to_x": 97, "to_y": -22,
                "label": "Pizzo (97|-22)",
                "started_at": now,
                "arrives_at": now + 1800,
            },
        )
        moves = store.movements_for("Vibo")
        assert len(moves) == 1
        assert moves[0]["label"] == "Pizzo (97|-22)"

    def test_dedup_same_bucket(self, store: RadarStore):
        # Base allineata al confine del bucket (60s): drift 0-30s restano
        # nello stesso bucket indipendentemente da time.time() corrente.
        base = float(int(time.time() // 60) * 60 + 120)
        for drift in (0, 15, 25):
            store.register(
                "Vibo",
                {
                    "category": CAT_INCOMING_ATTACK,
                    "direction": "inbound",
                    "counterpart": "Attaccante",
                    "arrives_at": base + drift,
                },
            )
        moves = store.movements_for("Vibo")
        assert len(moves) == 1

    def test_started_at_not_overwritten_by_sync(self, store: RadarStore):
        now = time.time()
        store.register(
            "Vibo",
            {
                "category": CAT_OUTGOING_RAID,
                "counterpart": "T1",
                "started_at": now - 300,
                "arrives_at": now + 600,
            },
        )
        # Sync passivo senza started_at: non azzera il valore noto.
        store.register(
            "Vibo",
            {
                "category": CAT_OUTGOING_RAID,
                "counterpart": "T1",
                "arrives_at": now + 610,
            },
        )
        mv = store.movements_for("Vibo")[0]
        assert mv["started_at"] == pytest.approx(now - 300, abs=2)

    def test_sync_categories_removes_stale(self, store: RadarStore):
        now = time.time()
        store.register(
            "Vibo",
            {
                "category": CAT_INCOMING_ATTACK,
                "counterpart": "Att1",
                "arrives_at": now + 500,
            },
        )
        store.register(
            "Vibo",
            {
                "category": CAT_MERCHANT,
                "counterpart": "Merc1",
                "arrives_at": now + 500,
            },
        )
        # Il sync dei movimenti truppe non vede piu' Att1: rimosso,
        # il mercante (altra categoria) resta.
        store.sync_categories(
            "Vibo",
            [
                {
                    "category": CAT_OUTGOING_RAID,
                    "counterpart": "T-out",
                    "arrives_at": now + 700,
                }
            ],
            frozenset({CAT_INCOMING_ATTACK, CAT_OUTGOING_ATTACK, CAT_OUTGOING_RAID}),
        )
        moves = store.movements_for("Vibo")
        cats = {m["category"] for m in moves}
        assert CAT_INCOMING_ATTACK not in cats
        assert CAT_MERCHANT in cats
        assert CAT_OUTGOING_RAID in cats

    def test_prune_expired(self, store: RadarStore):
        now = time.time()
        store.register(
            "Vibo",
            {
                "category": CAT_MERCHANT,
                "counterpart": "Old",
                "arrives_at": now - 10,
            },
        )
        store.register(
            "Vibo",
            {
                "category": CAT_MERCHANT,
                "counterpart": "New",
                "arrives_at": now + 600,
            },
        )
        # arrives_at <= now non viene registrato come movimento attivo:
        # register lo normalizza comunque (id) ma movements_for filtra.
        assert store.prune() >= 0
        moves = store.movements_for("Vibo")
        assert all(m["counterpart"] == "New" for m in moves)

    def test_persistence_across_instances(self, tmp_path: Path):
        path = tmp_path / "radar_persist.json"
        s1 = RadarStore("acc_x", path=path)
        now = time.time()
        s1.register(
            "Vibo",
            {
                "category": CAT_OUTGOING_ATTACK,
                "counterpart": "Nemico",
                "arrives_at": now + 1200,
            },
        )
        s2 = RadarStore("acc_x", path=path)
        moves = s2.movements_for("Vibo")
        assert len(moves) == 1
        assert moves[0]["counterpart"] == "Nemico"

    def test_merge_dedup_with_live(self, store: RadarStore):
        now = time.time()
        store.register(
            "Vibo",
            {
                "category": CAT_MERCHANT,
                "counterpart": "M1",
                "arrives_at": now + 300,
            },
        )
        merged = merge_village_movements(
            store,
            "Vibo",
            [
                {
                    "category": CAT_HERO_ADVENTURE,
                    "counterpart": "Adv",
                    "arrives_at": now + 400,
                }
            ],
        )
        assert len(merged) == 2
        assert merged[0]["category"] == CAT_MERCHANT


# ---------------------------------------------------------------------------
# Telemetria: payload villages[vid]["radar_movements"]
# ---------------------------------------------------------------------------

class TestTelemetryRadarPayload:
    def test_fills_inbound_coords(self, monkeypatch, tmp_path: Path):
        store = RadarStore("acc_tel", path=tmp_path / "radar_tel.json")
        now = time.time()
        store.register(
            "Vibo",
            {
                "category": CAT_INCOMING_ATTACK,
                "counterpart": "Att",
                "arrives_at": now + 800,
            },
        )
        monkeypatch.setattr(
            "travian_agent.radar_store._stores", {"acc_tel": store}
        )
        v = Village(name="Vibo", x=78, y=-7)
        moves = telemetry._radar_payload("acc_tel", v)
        assert len(moves) == 1
        # inbound senza coordinate: to_* = villaggio radar.
        assert moves[0]["to_x"] == 78
        assert moves[0]["to_y"] == -7

    def test_fills_outbound_origin(self, monkeypatch, tmp_path: Path):
        store = RadarStore("acc_tel2", path=tmp_path / "r2.json")
        now = time.time()
        store.register(
            "Vibo",
            {
                "category": CAT_OUTGOING_RAID,
                "to_x": 50, "to_y": 60,
                "counterpart": "T",
                "arrives_at": now + 800,
            },
        )
        monkeypatch.setattr(
            "travian_agent.radar_store._stores", {"acc_tel2": store}
        )
        v = Village(name="Vibo", x=78, y=-7)
        moves = telemetry._radar_payload("acc_tel2", v)
        assert moves[0]["from_x"] == 78
        assert moves[0]["from_y"] == -7
        assert moves[0]["to_x"] == 50

    def test_empty_without_store(self, monkeypatch):
        monkeypatch.setattr(
            "travian_agent.radar_store._stores", {}
        )
        v = Village(name="Vibo", x=78, y=-7)
        # Nessuno store istanziato: viene creato vuoto (file state):
        # il risultato dev'essere comunque una lista.
        res = telemetry._radar_payload("__nonexistent__", v)
        assert isinstance(res, list)


# ---------------------------------------------------------------------------
# Modello IncomingAttack esteso
# ---------------------------------------------------------------------------

class TestIncomingAttackCoords:
    def test_counterpart_fields_default(self):
        a = IncomingAttack(movement_type="attack", seconds_remaining=100)
        assert a.counterpart_x is None
        assert a.counterpart_y is None
        assert a.is_threat

    def test_counterpart_coords(self):
        a = IncomingAttack(
            movement_type="raid",
            arrival_ts=1000.0,
            counterpart_x=-15,
            counterpart_y=40,
        )
        assert a.counterpart_x == -15
        assert a.counterpart_y == 40


def test_valid_categories_cover_spec():
    assert VALID_CATEGORIES == {
        "merchant",
        "hero_adventure",
        "outgoing_raid",
        "outgoing_attack",
        "incoming_attack",
        "reinforcement",
        "troop_return",
    }
