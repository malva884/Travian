# tests/test_pre_raid.py
"""Smart Pre-Raid Intelligence: combat_calc, farm_store e pipeline farming.

Copre la specifica "Smart Pre-Raid Intelligence per Custom Farm Targets":
- oasi: conteggio bestie Natura, contingente calcolato a perdite nulle,
  micro-raid su oasi vuota, skip senza blacklist permanente;
- villaggi: spiata obbligatoria con TTL, blacklist temporanea su target
  difeso, quantita' raid calcolata su bottino/capacita' di trasporto;
- persistenza: nuovi campi FarmTarget (scout report, nature, pause).
"""

from __future__ import annotations

import asyncio
import time

from travian_agent import combat_calc
from travian_agent.config import Settings
from travian_agent.farm_store import (
    STATUS_APPROVED,
    FarmTarget,
    FarmTargetStore,
    make_target_id,
)
from travian_agent.farming import FarmingManager
from travian_agent.models import ActionResult, Village


# ----------------------------------------------------------------------
# combat_calc: statistiche Natura, clear oasi, loot-based raid
# ----------------------------------------------------------------------


class TestCombatCalc:
    def test_normalize_animal_aliases(self):
        assert combat_calc.normalize_animal("Ragni") == "u32"
        assert combat_calc.normalize_animal("spider") == "u32"
        assert combat_calc.normalize_animal("u37") == "u37"
        assert combat_calc.normalize_animal("sconosciuto") == ""

    def test_nature_defense_totals(self):
        # 10 ragni: def_inf 35x10, def_cav 40x10
        d = combat_calc.nature_defense({"u32": 10})
        assert d == {"inf": 350, "cav": 400}

    def test_scout_unit_key_per_tribe(self):
        assert combat_calc.scout_unit_key("teutons") == "u14"
        assert combat_calc.scout_unit_key("romans") == "u4"
        assert combat_calc.scout_unit_key("gauls") == "u23"
        assert combat_calc.scout_unit_key("") == ""

    def test_total_loot(self):
        res = {"wood": 100, "clay": 200, "iron": 300, "crop": 400}
        assert combat_calc.total_loot(res) == 1000
        assert combat_calc.total_loot({}) == 0
        assert combat_calc.total_loot({"wood": "x"}) == 0

    def test_troops_for_loot_capacity(self):
        # carry conosciuto (>0): qty = ceil(loot / carry), cap su disponibili
        carry = combat_calc.unit_carry("t5")
        if carry <= 0:
            # stats non caricabili in questo env: la funzione deve
            # comunque restituire 0 invece di esplodere
            assert combat_calc.troops_for_loot(5000, "t5", 100) == 0
            return
        need = -(-5000 // carry)  # ceil
        assert combat_calc.troops_for_loot(5000, "t5", need + 50) == need
        assert combat_calc.troops_for_loot(5000, "t5", 10) == 10
        assert combat_calc.troops_for_loot(5000, "t5", 999, max_troops_per_raid=7) == 7
        assert combat_calc.troops_for_loot(0, "t5", 100) == 0

    def test_min_troops_for_oasis_empty(self):
        assert combat_calc.min_troops_for_oasis({}, {"u5": 100}) == ("", 0)
        assert combat_calc.min_troops_for_oasis(None, {"u5": 100}) == ("", 0)

    def test_min_troops_for_oasis_zero_losses(self):
        # Regola zero perdite: con poche bestie il contingente esiste
        nature = {"u31": 2, "u32": 1}
        unit, qty = combat_calc.min_troops_for_oasis(
            nature, {"u5": 5000, "u3": 5000}, "t5", max_loss_pct=0.0
        )
        if combat_calc.unit_attack("t5") <= 0:
            assert (unit, qty) == ("", 0)  # stats non caricabili
            return
        assert unit == "t5"
        assert qty > 0
        # perdite stimate nulle sul contingente scelto
        est = combat_calc.estimate_attacker_losses(
            qty, unit, combat_calc.nature_defense(nature)
        )
        assert est == 0

    def test_min_troops_for_oasis_insufficient(self):
        # Esercito minuscolo vs 20 elefanti: skip, niente invio suicida
        nature = {"u40": 20}
        unit, qty = combat_calc.min_troops_for_oasis(
            nature, {"u1": 10}, "t1", max_loss_pct=0.0
        )
        assert (unit, qty) == ("", 0)


# ----------------------------------------------------------------------
# farm_store: persistenza dei nuovi campi di intelligence
# ----------------------------------------------------------------------


def _store(tmp_path) -> FarmTargetStore:
    return FarmTargetStore("TEST", path=tmp_path / "farm.json")


def _approved(store: FarmTargetStore, x: int, y: int, **kw) -> FarmTarget:
    t = FarmTarget(id=make_target_id(x, y), x=x, y=y, status=STATUS_APPROVED, **kw)
    store.upsert(t)
    return t


class TestFarmStoreIntel:
    def test_scout_roundtrip(self, tmp_path):
        s = _store(tmp_path)
        _approved(s, 10, 20)
        s.record_scout_sent(10, 20)
        t = s.find(make_target_id(10, 20))
        assert t.scouting_pending is True
        assert t.last_scout_req_ts > 0

        s.record_scout_result(
            10, 20,
            {"wood": 100, "clay": 50},
            {},
            wall_level=3,
            report_ts=12345.0,
        )
        t = s.find(make_target_id(10, 20))
        assert t.scouting_pending is False
        assert t.last_scout_ts == 12345.0
        assert t.scouted_resources == {"wood": 100, "clay": 50}
        assert t.scouted_wall == 3
        assert t.last_outcome == "scouted"

    def test_nature_and_blacklist_persist(self, tmp_path):
        s = _store(tmp_path)
        _approved(s, 1, 1)
        s.record_nature_troops(1, 1, {"u36": 4})
        s.set_temp_blacklist_at(1, 1, 24, "difeso")
        t = s.find(make_target_id(1, 1))
        assert t.nature_troops == {"u36": 4}
        assert t.nature_ts > 0
        assert t.temp_blacklist_until > time.time()
        # escluso dai target raidiabili
        assert s.get_approved_targets() == []

    def test_pause_excludes_target(self, tmp_path):
        s = _store(tmp_path)
        _approved(s, 2, 2)
        s.set_pause_at(2, 2, 12, "protezione principianti")
        t = s.find(make_target_id(2, 2))
        assert t.paused_until > time.time()
        assert s.get_approved_targets() == []

    def test_expired_blacklist_returns_to_pool(self, tmp_path):
        s = _store(tmp_path)
        _approved(s, 3, 3)
        s._patch_at(3, 3, {"temp_blacklist_until": time.time() - 60})
        assert len(s.get_approved_targets()) == 1

    def test_rescan_preserves_intel_fields(self, tmp_path):
        s = _store(tmp_path)
        _approved(s, 5, 5, name="Villaggio X")
        s.record_scout_result(5, 5, {"wood": 800}, {"u1": 50}, 7)
        s.register_scanned(
            [{"x": 5, "y": 5, "name": "Villaggio X", "type": "village",
              "population": 100}],
            origin_x=0, origin_y=0,
            nearest_village="v1",
        )
        t = s.find(make_target_id(5, 5))
        assert t.status == STATUS_APPROVED
        assert t.scouted_resources == {"wood": 800}
        assert t.scouted_troops == {"u1": 50}
        assert t.scouted_wall == 7


# ----------------------------------------------------------------------
# farming._send_approved_raids: pipeline pre-raid end-to-end (fake I/O)
# ----------------------------------------------------------------------


class _FakeObserver:
    """Observer minimale: tile details + report spia da dizionari."""

    def __init__(self, tiles=None, reports=None):
        self.tiles = tiles or {}
        self.reports = reports or {}

    async def read_tile_details(self, x, y):
        return self.tiles.get((x, y))

    async def parse_scout_report(self, x, y):
        return self.reports.get((x, y))


class _FakeExecutor:
    def __init__(self):
        self.actions = []

    async def execute(self, action, village_id=0):
        self.actions.append(action)
        coords = [
            [t.get("x"), t.get("y")]
            for t in (action.params.get("targets") or [])
        ]
        return ActionResult(
            success=True, message="ok",
            data={"sent_coords": coords, "failed_targets": []},
        )


def _settings() -> Settings:
    s = Settings(account_id="TEST", server_url="https://example.test")
    s.farming.enabled = True
    s.farming.targets_per_wave = 10
    return s


def _village() -> Village:
    return Village(village_id=1, name="V1", x=0, y=0)


def _manager(tmp_path) -> FarmingManager:
    return FarmingManager("TEST", state_path=tmp_path / "farming.json")


def _run(coro):
    return asyncio.run(coro)


class TestPreRaidPipeline:
    def test_village_with_fresh_scout_raids_by_loot(self, tmp_path):
        store = _store(tmp_path)
        _approved(store, 10, 10, name="FarmVille")
        store.record_scout_result(
            10, 10, {"wood": 1500, "clay": 1500}, {}, 0,
        )
        settings = _settings()
        settings.farming.min_raid_loot = 100
        ex = _FakeExecutor()
        obs = _FakeObserver()
        troops = {"u13": 500, "u14": 3}  # arieti/assoni + esploratori
        sent = _run(
            _manager(tmp_path)._send_approved_raids(
                executor=ex, observer=obs, farm_store=store,
                settings=settings, village=_village(), rally_slot=1,
                troops=troops, unit_pref="", farm_unit_qty=0, prefix="[t] ",
            )
        )
        assert sent is True
        raids = [a for a in ex.actions if a.type.value == "send_custom_raid"]
        assert len(raids) == 1
        tgt = raids[0].params["targets"][0]
        assert (tgt["x"], tgt["y"]) == (10, 10)
        # qty = min(ceil(3000/carry), 500) — positivo e <= 500
        qty = list(tgt["troops"].values())[0]
        assert 0 < qty <= 500

    def test_defended_village_gets_blacklisted(self, tmp_path):
        store = _store(tmp_path)
        _approved(store, 11, 11, name="Castle")
        store.record_scout_result(11, 11, {"wood": 9000}, {"u1": 400}, 10)
        ex = _FakeExecutor()
        sent = _run(
            _manager(tmp_path)._send_approved_raids(
                executor=ex, observer=_FakeObserver(), farm_store=store,
                settings=_settings(), village=_village(), rally_slot=1,
                troops={"u13": 500, "u14": 3}, unit_pref="",
                farm_unit_qty=0, prefix="[t] ",
            )
        )
        assert sent is False
        assert ex.actions == []
        t = store.find(make_target_id(11, 11))
        assert t.temp_blacklist_until > time.time()

    def test_unscouted_village_sends_scout(self, tmp_path):
        store = _store(tmp_path)
        _approved(store, 12, 12, name="Unknown")
        settings = _settings()
        ex = _FakeExecutor()
        sent = _run(
            _manager(tmp_path)._send_approved_raids(
                executor=ex, observer=_FakeObserver(), farm_store=store,
                settings=settings, village=_village(), rally_slot=1,
                troops={"u13": 500, "u14": 3}, unit_pref="",
                farm_unit_qty=0, prefix="[t] ",
            )
        )
        assert sent is True
        scouts = [a for a in ex.actions if a.type.value == "send_scout"]
        assert len(scouts) == 1
        st = scouts[0].params["targets"][0]
        assert (st["x"], st["y"]) == (12, 12)
        t = store.find(make_target_id(12, 12))
        assert t.scouting_pending is True

    def test_pending_scout_not_resent(self, tmp_path):
        store = _store(tmp_path)
        _approved(store, 13, 13, name="Pending")
        store.record_scout_sent(13, 13)
        ex = _FakeExecutor()
        sent = _run(
            _manager(tmp_path)._send_approved_raids(
                executor=ex, observer=_FakeObserver(), farm_store=store,
                settings=_settings(), village=_village(), rally_slot=1,
                troops={"u13": 500, "u14": 3}, unit_pref="",
                farm_unit_qty=0, prefix="[t] ",
            )
        )
        assert sent is False
        assert ex.actions == []  # nessun doppione mentre la spiata e' in corso

    def test_oasis_clear_uses_combat_calc(self, tmp_path):
        store = _store(tmp_path)
        _approved(store, 20, 20, name="Oasi", is_oasis=True)
        tiles = {
            (20, 20): {
                "ok": True, "is_oasis": True, "oasis_annexed": False,
                "protection": False, "nature_troops": {"u31": 2, "u32": 1},
            }
        }
        ex = _FakeExecutor()
        sent = _run(
            _manager(tmp_path)._send_approved_raids(
                executor=ex, observer=_FakeObserver(tiles=tiles),
                farm_store=store, settings=_settings(), village=_village(),
                rally_slot=1, troops={"u5": 5000, "u14": 3}, unit_pref="",
                farm_unit_qty=0, prefix="[t] ",
            )
        )
        # bestie minime: o clear calcolato o skip documentato, mai suicidio
        t = store.find(make_target_id(20, 20))
        assert t.nature_troops.get("u31") == 2  # conteggio persistito
        if combat_calc.unit_attack("t5") <= 0:
            assert sent is False and "forze insufficienti" in t.last_outcome
        else:
            assert sent is True
            raid = ex.actions[-1]
            qty = list(raid.params["targets"][0]["troops"].values())[0]
            assert qty > 0

    def test_empty_oasis_micro_raid(self, tmp_path):
        store = _store(tmp_path)
        _approved(store, 21, 21, name="OasiVuota", is_oasis=True)
        tiles = {
            (21, 21): {
                "ok": True, "is_oasis": True, "oasis_annexed": False,
                "protection": False, "nature_troops": {},
            }
        }
        ex = _FakeExecutor()
        sent = _run(
            _manager(tmp_path)._send_approved_raids(
                executor=ex, observer=_FakeObserver(tiles=tiles),
                farm_store=store, settings=_settings(), village=_village(),
                rally_slot=1, troops={"u5": 100, "u14": 3}, unit_pref="",
                farm_unit_qty=0, prefix="[t] ",
            )
        )
        assert sent is True
        raid = ex.actions[-1]
        qty = list(raid.params["targets"][0]["troops"].values())[0]
        assert 2 <= qty <= 5  # micro-raid 2-5 unita'

    def test_beginner_protection_pauses_target(self, tmp_path):
        store = _store(tmp_path)
        _approved(store, 22, 22, name="OasiP", is_oasis=True)
        tiles = {
            (22, 22): {
                "ok": True, "is_oasis": True, "oasis_annexed": False,
                "protection": True, "nature_troops": {},
            }
        }
        ex = _FakeExecutor()
        sent = _run(
            _manager(tmp_path)._send_approved_raids(
                executor=ex, observer=_FakeObserver(tiles=tiles),
                farm_store=store, settings=_settings(), village=_village(),
                rally_slot=1, troops={"u5": 100, "u14": 3}, unit_pref="",
                farm_unit_qty=0, prefix="[t] ",
            )
        )
        assert sent is False
        assert ex.actions == []
        t = store.find(make_target_id(22, 22))
        assert t.paused_until > time.time()

    def test_scouted_intercepted_blacklists(self, tmp_path):
        store = _store(tmp_path)
        _approved(store, 23, 23, name="Watched")
        reports = {
            (23, 23): {
                "resources": {"wood": 100}, "troops": {}, "wall_level": 0,
                "scouts_lost": True, "ts": time.time(),
            }
        }
        ex = _FakeExecutor()
        sent = _run(
            _manager(tmp_path)._send_approved_raids(
                executor=ex,
                observer=_FakeObserver(reports=reports),
                farm_store=store, settings=_settings(), village=_village(),
                rally_slot=1, troops={"u13": 500, "u14": 3}, unit_pref="",
                farm_unit_qty=0, prefix="[t] ",
            )
        )
        assert sent is False
        t = store.find(make_target_id(23, 23))
        assert t.temp_blacklist_until > time.time()
        assert "intercett" in t.last_outcome.lower()
