"""Test unitari per le funzioni di parsing dei sensori.

Coprono le utility pure (nessun browser richiesto): estrazione numeri,
capacita' magazzini, timer, tipologia risorsa e regex di identificazione
slot/villaggio (newdid).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from travian_agent.models import Field, ResourceType, Resources, Village
from travian_agent.sensors import (
    _NEWDID,
    _SLOT_CLASS,
    _SLOT_ID,
    guess_resource_type,
    parse_capacity,
    parse_int,
    parse_timer,
)


# ---------------------------------------------------------------------------
# parse_int
# ---------------------------------------------------------------------------

class TestParseInt:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("1 234", 1234),
            ("1 234 / 8 000", 1234),
            ("62 901", 62901),
            ("8000", 8000),
            ("Livello 5", 5),
        ],
    )
    def test_numeric_strings(self, text: str, expected: int) -> None:
        assert parse_int(text) == expected

    @pytest.mark.parametrize("text", ["", None, "abc", "---"])
    def test_empty_or_invalid(self, text) -> None:
        assert parse_int(text) == 0


# ---------------------------------------------------------------------------
# parse_capacity
# ---------------------------------------------------------------------------

class TestParseCapacity:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("3300||8000", 8000),
            ("3300 / 8000", 8000),
            ("8000", 8000),
            ("1 200||45 000", 45000),
        ],
    )
    def test_tooltip_formats(self, text: str, expected: int) -> None:
        assert parse_capacity(text) == expected

    def test_empty(self) -> None:
        assert parse_capacity(None) == 0
        assert parse_capacity("") == 0


# ---------------------------------------------------------------------------
# parse_timer
# ---------------------------------------------------------------------------

class TestParseTimer:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("0:12:34", 754),          # HH:MM:SS
            ("12:34", 754),            # MM:SS
            ("01:00:00", 3600),
            ("0:00:45", 45),
            ("1-02:30:45", 95445),     # giorni-ore:min:sec
        ],
    )
    def test_timer_formats(self, text: str, expected: int) -> None:
        assert parse_timer(text) == expected

    def test_empty(self) -> None:
        assert parse_timer(None) == 0
        assert parse_timer("") == 0
        assert parse_timer("no timer") == 0


# ---------------------------------------------------------------------------
# guess_resource_type
# ---------------------------------------------------------------------------

class TestGuessResourceType:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("Woodcutter Level 5", ResourceType.WOOD),
            ("Campo di Legno", ResourceType.WOOD),
            ("Holzfäller", ResourceType.WOOD),
            ("Clay Pit Level 3", ResourceType.CLAY),
            ("Pozzo d'Argilla", ResourceType.CLAY),
            ("Iron Mine Level 7", ResourceType.IRON),
            ("Miniera di Ferro", ResourceType.IRON),
            ("Wheat Field", ResourceType.CROP),
            ("Campo di Grano", ResourceType.CROP),
        ],
    )
    def test_multilanguage(self, name: str, expected: ResourceType) -> None:
        assert guess_resource_type(name) == expected

    def test_unknown(self) -> None:
        assert guess_resource_type("Main Building") is None


# ---------------------------------------------------------------------------
# Regex identificazione slot e newdid
# ---------------------------------------------------------------------------

class TestRegexes:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("build.php?id=12", 12),
            ("https://ts1.travian.com/build.php?id=3&x=1", 3),
            ("build.php?gid=1&id=18", 18),
        ],
    )
    def test_slot_id_from_href(self, url: str, expected: int) -> None:
        assert int(_SLOT_ID.search(url).group(1)) == expected

    def test_slot_id_no_match(self) -> None:
        assert _SLOT_ID.search("dorf1.php") is None

    @pytest.mark.parametrize(
        "cls,expected",
        [
            ("buildingSlot a4", 4),
            ("a17 level0", 17),
        ],
    )
    def test_slot_class(self, cls: str, expected: int) -> None:
        assert int(_SLOT_CLASS.search(cls).group(1)) == expected

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("dorf1.php?newdid=12345", 12345),
            ("https://ts11.x1.travian.com/dorf2.php?newdid=987&x=1", 987),
            ("dorf1.php?newdid=54321&c=abc", 54321),
            ("build.php?id=12&newdid=1001", 1001),
        ],
    )
    def test_newdid(self, url: str, expected: int) -> None:
        assert int(_NEWDID.search(url).group(1)) == expected

    def test_newdid_absent(self) -> None:
        assert _NEWDID.search("dorf1.php") is None


# ---------------------------------------------------------------------------
# Resources.scarcest
# ---------------------------------------------------------------------------

class TestResources:
    def test_scarcest_picks_lowest_ratio(self) -> None:
        res = Resources(
            wood=8000, clay=4000, iron=2000, crop=1000,
            warehouse_capacity=8000, granary_capacity=8000,
        )
        # wood 1.0, clay 0.5, iron 0.25, crop 0.125 -> crop e' il piu' scarso
        assert res.scarcest() is ResourceType.CROP

    def test_scarcest_equal_ratios(self) -> None:
        res = Resources(wood=100, clay=100, iron=100, crop=100)
        # tutti 0 capacita' -> cap=1 -> ratio uguali; il min() prende il primo
        assert res.scarcest() in list(ResourceType)

    def test_amount_and_capacity(self) -> None:
        res = Resources(wood=10, warehouse_capacity=500, granary_capacity=200)
        assert res.amount(ResourceType.WOOD) == 10
        assert res.capacity(ResourceType.CROP) == 200
        assert res.capacity(ResourceType.IRON) == 500


# ---------------------------------------------------------------------------
# Field / Village dataclass
# ---------------------------------------------------------------------------

class TestModels:
    def test_field_is_resource(self) -> None:
        assert Field(slot_id=18, name="x", level=1).is_resource_field
        assert not Field(slot_id=19, name="y", level=0).is_resource_field

    def test_village_defaults(self) -> None:
        v = Village(name="Capitale")
        assert v.role == "standard"
        assert v.village_id == 0
        assert not v.is_active


# ---------------------------------------------------------------------------
# Regressione ts30: read_troops_home / _read_troops_dom (nuova UI)
# ---------------------------------------------------------------------------

class TestTroopsDomParser:
    """Il parser deve coprire la nuova UI di Travian Legends
    (slot?id=N&tt=1, icone troopIcon/svg/data-unit): sulla classe
    'unit uN' legacy restituiva 0 truppe (bug dodge + farming)."""

    def _src(self) -> str:
        import inspect
        from travian_agent import sensors
        return inspect.getsource(sensors.TravianParser._read_troops_dom)

    def test_new_ui_icon_selectors(self):
        src = self._src()
        assert "troopIcon" in src
        assert "data-unit" in src
        assert 'svg[class*="unit"' in src or "svg[class*=\"unit\"" in src

    def test_quantity_from_attrs_and_labels(self):
        src = self._src()
        assert "data-count" in src
        assert "aria-label" in src
        assert "title" in src
        # Risoluzione uN anche via nome unita' localizzato.
        assert "keyOf" in src

    def test_innermost_dedup(self):
        src = self._src()
        # Wrapper + icona annidata non devono contare due volte.
        assert "contains(o)" in src


class TestRallyMovementsParser:
    """Regressione ts30: panoramica gid16 (slot?id=N&tt=1) con sezioni
    truppe ma 0 movimenti parse — table.troop_details non esiste nella
    nuova UI: i movimenti sono card/div con countdown."""

    def _src(self) -> str:
        import inspect
        from travian_agent import sensors
        return inspect.getsource(
            sensors.TravianParser.get_troop_movements
        )

    def test_card_fallback_present(self):
        src = self._src()
        # Pass alternativo oltre a table.troop_details.
        assert "pushRow" in src
        assert '[class*="card" i]' in src
        assert '[class*="movement" i]' in src

    def test_new_ui_timer_and_direction(self):
        src = self._src()
        # Countdown da data-* oltre a .timer.
        assert "data-countdown" in src
        assert "data-seconds" in src
        # Direzione da sezioni class-based incoming/outgoing.
        assert '[class*="incoming" i]' in src
        assert '[class*="outgoing" i]' in src

    def test_diagnostic_dumps_inner_section(self):
        src = self._src()
        # Snippet diagnostico sulla sezione piu' piccola (non il tag
        # esterno <div id="build">) e con budget maggiore.
        assert "slice(0, 1600)" in src
