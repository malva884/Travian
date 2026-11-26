"""Regression test: allineamento TROOP_TRAINING / FEEDER_PUSH su feeder
con risorse sbilanciate (scenario Vibo Valentia, newdid 31555).

Copre:
- ``feeder_surplus`` per-risorsa: una risorsa scarsa non blocca l'invio
  di una realmente in eccesso (grano alto, legno/argilla/ferro bassi);
- grano spedibile anche con bilancio netto negativo quando il granaio
  e' comunque in eccesso (riserva prudente piu' alta);
- ``_cap_feed_shipment`` per-risorsa: il destinatario pieno di una
  risorsa non azzera il carico delle altre;
- ``plan_training_detailed`` distingue attese naturali
  (insufficient_resources / grain_net / crop_reserve) da blocchi
  strutturali (frozen_village / building_queue), con diagnosi
  'missing' per la pausa ETA;
- riserva keep_ratio del feeder rispettata dalla caserma
  (coordinamento TROOP_TRAINING <-> FEEDER_PUSH);
- FEEDER_PUSH_IDLE esente dalla pausa anti-detection;
- ``_filter_feed_surplus_by_target_request`` e round-trip di
  ``feed_requested_resources`` su ``save_accounts`` / ``load_accounts``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import travian_agent.orchestrator as orch
from travian_agent.config import (
    ReplenishmentConfig,
    Settings,
    VillageConfig,
    load_accounts,
    save_accounts,
)
from travian_agent.expansion import (
    FEED_CAP_PER_RESOURCE,
    FEEDER_CROP_KEEP_RATIO,
    FEEDER_CROP_NEGATIVE_KEEP_RATIO,
    FEEDER_KEEP_RATIO,
    feeder_surplus,
)
from travian_agent.models import (
    BuildQueueItem,
    GameState,
    Resources,
)
from travian_agent.replenishment import TroopReplenishmentManager


def _settings(**kw) -> Settings:
    s = Settings(server_url="https://example.test", account_id="t")
    s.replenishment = ReplenishmentConfig(
        enabled=True,
        skip_while_building=kw.get("skip_while_building", False),
        crop_reserve_pct=0.25,
    )
    if kw.get("villages"):
        s.villages = kw["villages"]
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


# ---------------------------------------------------------------------------
# feeder_surplus: surplus per-risorsa, niente assunzione di uniformita'
# ---------------------------------------------------------------------------

class TestFeederSurplus:
    def test_unbalanced_stock_only_excess_resource_ships(self):
        """Vibo Valentia: grano abbondante, legno/argilla/ferro bassi.

        Solo il grano supera la propria riserva -> solo il grano parte;
        le risorse scarse non bloccano la risorsa realmente in eccesso.
        """
        state = _state(
            wood=4_000, clay=4_000, iron=4_000, crop=9_000,
            warehouse_capacity=10_000, granary_capacity=10_000,
            crop_balance=500,
        )
        out = feeder_surplus(state)
        # keep 0.55*10000 = 5500 -> W/C/I sotto soglia, nessun surplus;
        # grano: 9000 - 0.25*10000 = 6500 -> cappato al mercante.
        assert out == {"crop": FEED_CAP_PER_RESOURCE}

    def test_scarce_resource_never_blocks_abundant_one(self):
        """Legno/ferro sotto keep non impediscono l'argilla in eccesso."""
        state = _state(
            wood=1_000, clay=7_000, iron=1_000, crop=100,
            warehouse_capacity=10_000, granary_capacity=10_000,
            crop_balance=500,
        )
        out = feeder_surplus(state)
        assert out == {"clay": 1_500}  # 7000 - 0.55*10000

    def test_surplus_never_drops_stock_below_keep(self):
        """L'invio non porta mai una risorsa sotto la soglia minima."""
        state = _state(
            wood=8_000, clay=8_000, iron=8_000, crop=8_000,
            warehouse_capacity=10_000, granary_capacity=10_000,
            crop_balance=100,
        )
        out = feeder_surplus(state)
        res = state.resources
        for key, qty in out.items():
            if key == "crop":
                floor = 10_000 * FEEDER_CROP_KEEP_RATIO
            else:
                floor = 10_000 * FEEDER_KEEP_RATIO
            assert getattr(res, key) - qty >= floor

    def test_negative_crop_balance_still_ships_overflowing_granary(self):
        """Feeder con truppe: bilancio grano netto negativo MA granaio
        quasi pieno -> il surplus oltre la riserva prudente (60%) parte."""
        state = _state(
            wood=100, clay=100, iron=100, crop=8_000,
            warehouse_capacity=10_000, granary_capacity=10_000,
            crop_balance=-50,
        )
        out = feeder_surplus(state)
        assert out == {
            "crop": int(8_000 - 10_000 * FEEDER_CROP_NEGATIVE_KEEP_RATIO)
        }

    def test_negative_crop_balance_blocks_marginal_crop(self):
        """Bilancio negativo E granaio non in eccesso -> grano trattenuto."""
        state = _state(
            wood=100, clay=100, iron=100, crop=5_000,
            warehouse_capacity=10_000, granary_capacity=10_000,
            crop_balance=-50,
        )
        out = feeder_surplus(state)
        assert "crop" not in out

    def test_positive_balance_uses_normal_keep(self):
        """Bilancio >= 0: il grano spedisce oltre la riserva standard 25%."""
        state = _state(
            wood=0, clay=0, iron=0, crop=4_000,
            warehouse_capacity=10_000, granary_capacity=10_000,
            crop_balance=0,
        )
        out = feeder_surplus(state)
        assert out == {"crop": 1_500}  # 4000 - 0.25*10000


# ---------------------------------------------------------------------------
# _cap_feed_shipment: capping per-risorsa sul destinatario
# ---------------------------------------------------------------------------

class TestCapFeedShipment:
    def test_target_full_on_one_resource_does_not_block_others(
        self, monkeypatch
    ):
        """Il destinatario pieno di legno non azzera il grano spedibile."""
        monkeypatch.setattr(
            orch, "_inbound_merchant_resources", lambda s, k: {}
        )
        target = Resources(
            wood=9_100, clay=0, iron=0, crop=0,
            warehouse_capacity=10_000, granary_capacity=10_000,
        )
        capped, free, _note = orch._cap_feed_shipment(
            _settings(),
            target_key="t",
            surplus={"wood": 500, "crop": 800},
            target_res=target,
        )
        # wood: 85% di 10000 = 8500 - 9100 = -600 -> 0 spazio -> escluso;
        # crop: 8500 liberi -> 800 passa indipendente dal legno.
        assert "wood" not in capped
        assert capped["crop"] == 800
        assert free["wood"] == 0

    def test_unknown_target_capacity_uses_conservative_free(
        self, monkeypatch
    ):
        """Destinatario mai visitato: cap conservativo per risorsa."""
        monkeypatch.setattr(
            orch, "_inbound_merchant_resources", lambda s, k: {}
        )
        capped, free, _note = orch._cap_feed_shipment(
            _settings(),
            target_key="t",
            surplus={"wood": 1_500},
            target_res=None,
        )
        assert capped["wood"] == orch.FEED_UNKNOWN_FREE


# ---------------------------------------------------------------------------
# plan_training_detailed: diagnosi esplicita skip (attesa vs struttura)
# ---------------------------------------------------------------------------

class TestPlanTrainingDetailed:
    def test_reason_insufficient_resources_with_missing(self, tmp_path):
        """Scorte corte: motivo 'insufficient_resources' + deficit per
        risorsa dell'unita' piu' vicina all'addestrabilita' (ETA pause)."""
        mgr = _mgr(tmp_path)
        state = _state(
            wood=100, clay=100, iron=100, crop=5_000,
            granary_capacity=10_000, crop_balance=500,
        )
        plan, reason, ctx = mgr.plan_training_detailed(
            {"t1": 50}, state, _settings(), village_name="V1"
        )
        assert plan == {}
        assert reason == "insufficient_resources"
        # t1 costa 120/100/150/30: budget legno 100 -> manca 20, ferro 100 -> 50.
        assert ctx["unit"] == "t1"
        assert ctx["missing"]["wood"] == 20
        assert ctx["missing"]["iron"] == 50
        assert ctx["budget"]["crop"] == 2_500  # 5000 - riserva 25%

    def test_reason_grain_net(self, tmp_path):
        mgr = _mgr(tmp_path)
        state = _state(
            wood=100_000, clay=100_000, iron=100_000, crop=50_000,
            granary_capacity=100_000, crop_balance=10,  # <= soglia 10
        )
        plan, reason, ctx = mgr.plan_training_detailed(
            {"t1": 50}, state, _settings(), village_name="V1"
        )
        assert plan == {}
        assert reason == "grain_net"
        assert ctx["net_crop"] == 10

    def test_reason_crop_reserve(self, tmp_path):
        mgr = _mgr(tmp_path)
        state = _state(
            wood=100_000, clay=100_000, iron=100_000, crop=2_000,
            granary_capacity=10_000, crop_balance=500,
        )
        plan, reason, ctx = mgr.plan_training_detailed(
            {"t1": 50}, state, _settings(), village_name="V1"
        )
        assert plan == {}
        assert reason == "crop_reserve"
        assert ctx["crop_reserve"] == 2_500

    def test_reason_frozen_village_is_structural(self, tmp_path):
        mgr = _mgr(tmp_path)
        mgr.frozen_villages["V1"] = "Sotto assedio"
        state = _state(
            wood=100_000, clay=100_000, iron=100_000, crop=50_000,
            granary_capacity=100_000, crop_balance=500,
        )
        plan, reason, _ctx = mgr.plan_training_detailed(
            {"t1": 50}, state, _settings(), village_name="V1"
        )
        assert plan == {}
        assert reason == "frozen_village"

    def test_reason_building_queue(self, tmp_path):
        mgr = _mgr(tmp_path)
        state = _state(
            wood=100_000, clay=100_000, iron=100_000, crop=50_000,
            granary_capacity=100_000, crop_balance=500,
        )
        state.build_queue = [
            BuildQueueItem(name="Campo", level=2, is_resource_field=True)
        ]
        plan, reason, _ctx = mgr.plan_training_detailed(
            {"t1": 50}, state,
            _settings(skip_while_building=True), village_name="V1",
        )
        assert plan == {}
        assert reason == "building_queue"

    def test_plan_ok_returns_empty_reason(self, tmp_path):
        mgr = _mgr(tmp_path)
        state = _state(
            wood=100_000, clay=100_000, iron=100_000, crop=50_000,
            granary_capacity=100_000, crop_balance=500,
        )
        plan, reason, _ctx = mgr.plan_training_detailed(
            {"t1": 50}, state, _settings(), village_name="V1"
        )
        assert plan == {"t1": 50}
        assert reason == ""


# ---------------------------------------------------------------------------
# Coordinamento: la caserma rispetta la riserva keep_ratio del feeder
# ---------------------------------------------------------------------------

class TestFeederReserveCoordination:
    def test_feeder_keep_reserve_bounds_training_budget(self, tmp_path):
        """Su Vibo Valentia (feeder): la caserma spende solo sopra la
        riserva keep del push — il surplus di FEEDER_PUSH non si drena."""
        mgr = _mgr(tmp_path)
        settings = _settings(
            villages={
                "31555": VillageConfig(
                    name="Vibo Valentia",
                    role="feeder",
                    target_village="Hipponion",
                )
            }
        )
        state = GameState(
            resources=Resources(
                wood=6_000, clay=6_000, iron=6_000, crop=5_000,
                warehouse_capacity=10_000, granary_capacity=10_000,
                crop_balance=500,
            ),
            village_name="Vibo Valentia",
            village_id=31555,
        )
        plan, reason, _ctx = mgr.plan_training_detailed(
            {"t1": 50}, state, settings, village_name="Vibo Valentia"
        )
        # budget W/C/I = 6000 - 0.55*10000 = 500 -> ferro limita a 3.
        assert plan == {"t1": 3}
        assert reason == ""

    def test_standard_village_uses_full_stock(self, tmp_path):
        """Stesso stock su villaggio standard: nessuna riserva keep."""
        mgr = _mgr(tmp_path)
        state = _state(
            wood=6_000, clay=6_000, iron=6_000, crop=5_000,
            warehouse_capacity=10_000, granary_capacity=10_000,
            crop_balance=500,
        )
        plan, _reason, _ctx = mgr.plan_training_detailed(
            {"t1": 50}, state, _settings(), village_name="V1"
        )
        # budget ferro pieno 6000 -> 6000//150 = 40 legionari.
        assert plan == {"t1": 40}

    def test_feeder_below_keep_reports_insufficient_resources(self, tmp_path):
        """Feeder con stock sotto la riserva keep: deficit segnalato come
        attesa naturale (insufficient_resources), non blocco strutturale."""
        mgr = _mgr(tmp_path)
        settings = _settings(
            villages={
                "31555": VillageConfig(
                    name="Vibo Valentia",
                    role="feeder",
                    target_village="Hipponion",
                )
            }
        )
        state = GameState(
            resources=Resources(
                wood=5_000, clay=5_000, iron=5_000, crop=5_000,
                warehouse_capacity=10_000, granary_capacity=10_000,
                crop_balance=500,
            ),
            village_name="Vibo Valentia",
            village_id=31555,
        )
        plan, reason, ctx = mgr.plan_training_detailed(
            {"t1": 50}, state, settings, village_name="Vibo Valentia"
        )
        assert plan == {}
        assert reason == "insufficient_resources"
        # budget W/C/I = 5000-5500 -> 0: manca l'intero costo di un t1.
        assert ctx["missing"]["wood"] == 120
        assert ctx["missing"]["iron"] == 150


# ---------------------------------------------------------------------------
# Compat & pause registry
# ---------------------------------------------------------------------------

def test_plan_training_wrapper_keeps_dict_api(tmp_path):
    """plan_training resta il wrapper {tN: qty} usato dai test esistenti."""
    mgr = _mgr(tmp_path)
    state = _state(
        wood=100_000, clay=100_000, iron=100_000, crop=50_000,
        granary_capacity=100_000, crop_balance=500,
    )
    plan = mgr.plan_training(
        {"t1": 5}, state, _settings(), village_name="V1"
    )
    assert plan == {"t1": 5}


def test_feeder_push_idle_is_pause_exempt():
    """'Nessun surplus' e' uno stato benigno: non innesca la pausa
    anti-detection 15-30 min (niente churn SKIPPED -> pausa -> retry)."""
    assert "FEEDER_PUSH_IDLE" in orch._PAUSE_EXEMPT_DECISIONS


# ---------------------------------------------------------------------------
# feed_requested_resources: filtro per target e persistenza accounts.json
# ---------------------------------------------------------------------------

class TestFeedRequestedResources:
    def test_filter_none_keeps_all_positive_resources(self):
        out = orch._filter_feed_surplus_by_target_request(
            {"wood": 500, "clay": 300, "iron": 0},
            None,
        )
        assert out == {"wood": 500, "clay": 300}

    def test_filter_explicit_list_keeps_only_requested(self):
        out = orch._filter_feed_surplus_by_target_request(
            {"wood": 500, "clay": 300, "iron": 250, "crop": 900},
            ["clay", "iron"],
        )
        assert out == {"clay": 300, "iron": 250}

    def test_filter_empty_list_blocks_all_resources(self):
        out = orch._filter_feed_surplus_by_target_request(
            {"wood": 500, "clay": 300, "iron": 250, "crop": 900},
            [],
        )
        assert out == {}

    def test_save_and_load_accounts_roundtrip_feed_requested_resources(
        self, tmp_path, monkeypatch
    ):
        acc_file = tmp_path / "accounts.json"
        monkeypatch.setattr(
            "travian_agent.config.ACCOUNTS_FILE", acc_file, raising=False
        )
        s = _settings(
            villages={
                "pizzo": VillageConfig(
                    name="Pizzo",
                    role="croplog",
                    feed_requested_resources=["clay", "iron"],
                )
            }
        )
        save_accounts([s])
        loaded = load_accounts()
        assert len(loaded) == 1
        assert (
            loaded[0].villages["pizzo"].feed_requested_resources
            == ["clay", "iron"]
        )