"""Test per l'Auto-Ricerca Truppe in Accademia (gid 22).

Copre:
- ``normalize_academy_state``: classificazione delle righe grezze dello
  snapshot JS (researchable / blocked / in_progress / researched),
  merge per stesso uN e flag ``research_active``;
- ``_academy_pending_units``: confronto allowed_units vs stato
  Accademia con tie-breaker sul trainer;
- ``trainer_gid_for_unit``: mappa posizione unita' -> edificio;
- ``_research_unit``: validazione parametri (slot/unit mancanti).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from travian_agent.actions import ActionExecutor
from travian_agent.config import Settings
from travian_agent.models import Action, ActionType
from travian_agent.orchestrator import _academy_pending_units
from travian_agent.presets import trainer_gid_for_unit
from travian_agent.sensors import normalize_academy_state


# ---------------------------------------------------------------------------
# normalize_academy_state
# ---------------------------------------------------------------------------

def _row(u, *, timer=False, controls=None, costs=None):
    return {
        "u": u,
        "text": "unit row",
        "timer": timer,
        "controls": controls or [],
        "costs": costs or {},
    }


_RESEARCH_BTN = {"text": "Ricerca", "href": "", "disabled": False}
_RESEARCH_LINK = {"text": "", "href": "build.php?id=33&a=3", "disabled": False}
_DISABLED_BTN = {"text": "Ricerca", "href": "", "disabled": True}


class TestNormalizeAcademyState:
    def test_empty_inputs(self):
        assert normalize_academy_state(None) == {}
        assert normalize_academy_state({}) == {}
        assert normalize_academy_state({"rows": []}) == {}
        assert normalize_academy_state("non-un-dict") == {}

    def test_researchable_via_button_text(self):
        raw = {"active": False, "rows": [
            _row(3, controls=[_RESEARCH_BTN],
                 costs={"wood": 1000, "clay": 700, "iron": 600, "crop": 900}),
        ]}
        st = normalize_academy_state(raw)
        assert st["research_active"] is False
        assert st["units"]["t3"]["status"] == "researchable"
        assert st["units"]["t3"]["label"] == "Imperiano"
        assert st["units"]["t3"]["costs"]["iron"] == 600

    def test_researchable_via_href_contract(self):
        raw = {"rows": [_row(5, controls=[_RESEARCH_LINK])]}
        st = normalize_academy_state(raw)
        assert st["units"]["t5"]["status"] == "researchable"

    def test_blocked_when_all_controls_disabled(self):
        raw = {"rows": [_row(6, controls=[_DISABLED_BTN])]}
        st = normalize_academy_state(raw)
        assert st["units"]["t6"]["status"] == "blocked"

    def test_researched_when_no_controls(self):
        raw = {"rows": [_row(1)]}
        st = normalize_academy_state(raw)
        assert st["units"]["t1"]["status"] == "researched"

    def test_in_progress_sets_active(self):
        raw = {"active": False, "rows": [_row(2, timer=True)]}
        st = normalize_academy_state(raw)
        assert st["units"]["t2"]["status"] == "in_progress"
        assert st["research_active"] is True
        assert st["researching"] == ["t2"]

    def test_active_from_page_timer_without_rows(self):
        st = normalize_academy_state({"active": True, "rows": []})
        assert st["research_active"] is True

    def test_merge_prefers_informative_status(self):
        raw = {"rows": [
            _row(3),  # researched (icona nella sezione 'fatte')
            _row(3, timer=True),  # stessa unita' in coda ricerca
        ]}
        st = normalize_academy_state(raw)
        assert st["units"]["t3"]["status"] == "in_progress"

    def test_out_of_range_units_ignored(self):
        raw = {"rows": [_row(0), _row(31), _row("x")]}
        assert normalize_academy_state(raw) == {}


# ---------------------------------------------------------------------------
# _academy_pending_units
# ---------------------------------------------------------------------------

def _ac(units, *, active=False):
    return {
        "research_active": active,
        "researching": [
            k for k, u in units.items()
            if u.get("status") == "in_progress"
        ],
        "units": units,
    }


class TestAcademyPendingUnits:
    def test_researchable_is_actionable(self):
        pend = _academy_pending_units(
            ["t3"],
            _ac({"t3": {"status": "researchable",
                        "costs": {"wood": 1000}}}),
            academy_level=10,
        )
        assert len(pend) == 1
        assert pend[0]["actionable"] is True
        assert pend[0]["unit"] == "t3"
        assert pend[0]["costs"]["wood"] == 1000

    def test_researched_units_skipped(self):
        pend = _academy_pending_units(
            ["t1", "t2"],
            _ac({"t2": {"status": "researched"}}),
            academy_level=10,
        )
        # t1 non richiede ricerca (non in UNIT_ACADEMY_LEVELS) e t2 e'
        # gia' ricercata: lista vuota.
        assert pend == []

    def test_order_follows_whitelist(self):
        pend = _academy_pending_units(
            ["t5", "t3"],
            _ac({
                "t3": {"status": "researchable"},
                "t5": {"status": "researchable"},
            }),
            academy_level=10,
        )
        assert [p["unit"] for p in pend] == ["t5", "t3"]

    def test_blocked_with_low_academy_level(self):
        pend = _academy_pending_units(
            ["t8"],
            _ac({"t8": {"status": "blocked"}}),
            academy_level=3,
        )
        assert pend[0]["actionable"] is False
        assert "15" in pend[0]["reason"]  # livello richiesto

    def test_in_progress_not_actionable(self):
        pend = _academy_pending_units(
            ["t3"],
            _ac({"t3": {"status": "in_progress"}}, active=True),
            academy_level=10,
        )
        assert pend[0]["actionable"] is False
        assert "in corso" in pend[0]["reason"]

    def test_absent_unit_in_table_uses_trainable(self):
        # UI che nasconde le ricercate: t3 assente dal DOM ma addestrabile
        # -> sbloccata (nessun pending); t5 assente e non addestrabile
        # -> pending non actionable.
        pend = _academy_pending_units(
            ["t3", "t5"],
            _ac({}),
            academy_level=10,
            trainable={"t1", "t3"},
        )
        assert len(pend) == 1
        assert pend[0]["unit"] == "t5"
        assert pend[0]["actionable"] is False

    def test_absent_unit_outside_table_is_unlocked(self):
        # t10 (Colono) non e' soggetta a ricerca in Accademia.
        pend = _academy_pending_units(
            ["t10"], _ac({}), academy_level=1,
        )
        assert pend == []

    def test_invalid_keys_ignored(self):
        pend = _academy_pending_units(
            ["", "non-esiste"], _ac({}), academy_level=10,
        )
        assert pend == []


# ---------------------------------------------------------------------------
# trainer_gid_for_unit
# ---------------------------------------------------------------------------

class TestTrainerGidForUnit:
    @pytest.mark.parametrize(
        "tkey,gid",
        [
            ("t1", 19), ("t3", 19), ("t11", 19), ("t22", 19),   # fanteria
            ("t4", 20), ("t6", 20), ("t15", 20), ("t26", 20),   # cavalleria
            ("t7", 21), ("t8", 21), ("t17", 21), ("t28", 21),   # macchine
            ("t9", 25), ("t10", 25), ("t19", 25), ("t30", 25),  # capo/colono
        ],
    )
    def test_mapping(self, tkey, gid):
        assert trainer_gid_for_unit(tkey) == gid

    def test_invalid_keys(self):
        assert trainer_gid_for_unit("") == 0
        assert trainer_gid_for_unit("abc") == 0


# ---------------------------------------------------------------------------
# _research_unit — validazione parametri (senza browser)
# ---------------------------------------------------------------------------

class TestResearchUnitAction:
    def _executor(self):
        return ActionExecutor(
            browser=None,  # type: ignore[arg-type]
            settings=Settings(server_url="https://ts1.travian.com"),
        )

    def test_action_type_exists(self):
        act = Action(type=ActionType.RESEARCH_UNIT)
        assert act.type == ActionType.RESEARCH_UNIT

    def test_missing_slot_fails(self):
        res = asyncio.run(
            self._executor()._research_unit(
                Action(
                    type=ActionType.RESEARCH_UNIT,
                    params={"unit": "t3"},
                )
            )
        )
        assert res.success is False
        assert "academy_slot" in res.message

    def test_missing_unit_fails(self):
        res = asyncio.run(
            self._executor()._research_unit(
                Action(
                    type=ActionType.RESEARCH_UNIT,
                    params={"academy_slot": 33},
                )
            )
        )
        assert res.success is False
        assert "unit" in res.message

    def test_building_path_routing_aware(self):
        ex = self._executor()
        # Profilo unknown/legacy -> path legacy build.php.
        assert ex._building_path_for(33) == "build.php?id=33"
