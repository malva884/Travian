"""Test per le rettifiche persistenti slot->gid (tabella slot_corrections).

Scenario coperto: su Vibo Valentia (newdid 31555) la mappa dorf2
riporta lo slot 25 come gid 25 (Residenza) mentre la pagina edificio
verifica gid 22 (Accademia). La correzione deve sopravvivere al ciclo
e al riavvio: al caricamento successivo ``_apply_slot_corrections``
risolve il gid reale prima delle decisioni di brain/Accademia, senza
ripetere il probe di correzione a ogni iterazione.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from travian_agent import db
from travian_agent.config import Settings
from travian_agent.models import Field, Village
from travian_agent.orchestrator import (
    _apply_slot_corrections,
    _persist_slot_corrections,
)


ACC = "test_acc"
VID = 31555  # Vibo Valentia


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """DB SQLite isolato su tmp_path (niente stato reale dell'agente)."""
    monkeypatch.setattr(db, "STATE_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "travian.db")
    monkeypatch.setattr(db, "LEGACY_DIR", tmp_path / "legacy")
    monkeypatch.setattr(db, "_initialized", False)
    return db


def _settings() -> Settings:
    return Settings(server_url="https://ts1.travian.com", account_id=ACC)


def _village() -> Village:
    return Village(name="Vibo Valentia", village_id=VID)


def _field(slot: int, gid: int, level: int = 1) -> Field:
    return Field(slot_id=slot, name="", level=level, gid=gid)


# ---------------------------------------------------------------------------
# Persistenza DB
# ---------------------------------------------------------------------------

class TestSlotCorrectionsDb:
    def test_upsert_and_load_roundtrip(self, tmp_db):
        db.slot_corrections_upsert(
            ACC,
            VID,
            [
                {
                    "slot_id": 25,
                    "detected_gid": 25,
                    "actual_gid": 22,
                    "source": "academy_probe",
                }
            ],
            resolved_slot=25,
        )
        loaded = db.slot_corrections_load(ACC, VID)
        assert 25 in loaded
        entry = loaded[25]
        assert entry["detected_gid"] == 25
        assert entry["actual_gid"] == 22
        assert entry["source"] == "academy_probe"
        assert entry["resolved"] is True
        assert entry["updated_at"] > 0

    def test_load_is_per_village(self, tmp_db):
        db.slot_corrections_upsert(
            ACC, VID, [{"slot_id": 25, "detected_gid": 25, "actual_gid": 22}]
        )
        assert db.slot_corrections_load(ACC, 99999) == {}
        assert db.slot_corrections_load("other_acc", VID) == {}

    def test_upsert_updates_existing(self, tmp_db):
        db.slot_corrections_upsert(
            ACC, VID, [{"slot_id": 25, "detected_gid": 25, "actual_gid": 22}]
        )
        # Lo slot e' cambiato davvero (nuova verifica su pagina edificio):
        # la voce viene aggiornata, non congelata.
        db.slot_corrections_upsert(
            ACC, VID, [{"slot_id": 25, "detected_gid": 25, "actual_gid": 17}]
        )
        loaded = db.slot_corrections_load(ACC, VID)
        assert loaded[25]["actual_gid"] == 17

    def test_resolved_flag_exclusive_per_village(self, tmp_db):
        db.slot_corrections_upsert(
            ACC,
            VID,
            [
                {"slot_id": 25, "detected_gid": 25, "actual_gid": 22},
                {"slot_id": 30, "detected_gid": 0, "actual_gid": 19},
            ],
            resolved_slot=25,
        )
        # Un nuovo resolved_slot sposta il flag e azzera il precedente.
        db.slot_corrections_upsert(
            ACC,
            VID,
            [{"slot_id": 30, "detected_gid": 0, "actual_gid": 19}],
            resolved_slot=30,
        )
        loaded = db.slot_corrections_load(ACC, VID)
        assert loaded[30]["resolved"] is True
        assert loaded[25]["resolved"] is False

    def test_delete_removes_entries(self, tmp_db):
        db.slot_corrections_upsert(
            ACC,
            VID,
            [
                {"slot_id": 25, "detected_gid": 25, "actual_gid": 22},
                {"slot_id": 30, "detected_gid": 0, "actual_gid": 19},
            ],
        )
        db.slot_corrections_delete(ACC, VID, [25])
        loaded = db.slot_corrections_load(ACC, VID)
        assert 25 not in loaded
        assert 30 in loaded


# ---------------------------------------------------------------------------
# _persist_slot_corrections: salvataggio dal ciclo Accademia
# ---------------------------------------------------------------------------

class TestPersistSlotCorrections:
    def test_saves_detected_and_actual_gid(self, tmp_db):
        # Mappa corrente: slot 25 = gid 25 (sbagliato). Il sensore ha
        # verificato gid 22 sulla pagina edificio.
        fields = [_field(25, gid=25)]
        _persist_slot_corrections(
            _settings(), _village(), fields, {25: 22}, resolved_slot=25
        )
        loaded = db.slot_corrections_load(ACC, VID)
        assert loaded[25]["detected_gid"] == 25
        assert loaded[25]["actual_gid"] == 22
        assert loaded[25]["resolved"] is True

    def test_noop_without_village_id(self, tmp_db):
        v = Village(name="NoId", village_id=0)
        _persist_slot_corrections(_settings(), v, [_field(25, 25)], {25: 22})
        assert db.slot_corrections_load(ACC, 0) == {}


# ---------------------------------------------------------------------------
# _apply_slot_corrections: applicazione in lettura prima delle decisioni
# ---------------------------------------------------------------------------

class TestApplySlotCorrections:
    def test_correction_applied_on_reload(self, tmp_db):
        """Simula il ciclo successivo: la mappa riporta ancora gid 25
        (bug persistente), la rettifica salvata risolve gid 22."""
        _persist_slot_corrections(
            _settings(), _village(), [_field(25, gid=25)], {25: 22},
            resolved_slot=25,
        )
        # Nuovo GameState / nuova scansione dorf2: stessa mappa errata.
        fresh = [_field(25, gid=25)]
        applied = _apply_slot_corrections(_settings(), VID, fresh)
        assert applied == 1
        assert fresh[0].gid == 22
        # L'Accademia e' risolta subito: nessun nuovo probe/correzione.
        academy = next(
            (f for f in fresh if f.gid == 22 and f.level > 0), None
        )
        assert academy is not None and academy.slot_id == 25

    def test_stale_entry_invalidated_when_slot_changed(self, tmp_db):
        """Costruzione/demolizione reale: la mappa ora espone un gid
        diverso sia dal detected sia dall'actual -> voce rimossa."""
        _persist_slot_corrections(
            _settings(), _village(), [_field(25, gid=25)], {25: 22}
        )
        # Lo slot ora ospita davvero un altro edificio (es. mercato 17).
        fresh = [_field(25, gid=17)]
        applied = _apply_slot_corrections(_settings(), VID, fresh)
        assert applied == 0
        assert fresh[0].gid == 17
        assert db.slot_corrections_load(ACC, VID) == {}

    def test_redundant_entry_removed_when_map_fixed(self, tmp_db):
        """La mappa ora concorda col gid reale: rettifica superflua."""
        _persist_slot_corrections(
            _settings(), _village(), [_field(25, gid=25)], {25: 22}
        )
        fresh = [_field(25, gid=22)]  # mappa ora corretta
        applied = _apply_slot_corrections(_settings(), VID, fresh)
        assert applied == 0
        assert fresh[0].gid == 22
        assert db.slot_corrections_load(ACC, VID) == {}

    def test_unreadable_gid_still_corrected(self, tmp_db):
        """gid -1 (markup senza classe gid): la rettifica resta la
        migliore informazione disponibile."""
        _persist_slot_corrections(
            _settings(), _village(), [_field(25, gid=25)], {25: 22}
        )
        fresh = [_field(25, gid=-1)]
        applied = _apply_slot_corrections(_settings(), VID, fresh)
        assert applied == 1
        assert fresh[0].gid == 22

    def test_other_slots_untouched(self, tmp_db):
        _persist_slot_corrections(
            _settings(), _village(), [_field(25, gid=25)], {25: 22}
        )
        fresh = [_field(25, gid=25), _field(30, gid=19)]
        _apply_slot_corrections(_settings(), VID, fresh)
        assert fresh[1].gid == 19

    def test_no_corrections_noop(self, tmp_db):
        fresh = [_field(25, gid=25)]
        assert _apply_slot_corrections(_settings(), VID, fresh) == 0
        assert fresh[0].gid == 25


# ---------------------------------------------------------------------------
# Ciclo completo: persist -> reload -> risoluzione senza re-loop
# ---------------------------------------------------------------------------

class TestEndToEndReload:
    def test_correction_survives_simulated_restart(self, tmp_db):
        """Primo ciclo: la sonda Accademia corregge slot 25 -> gid 22 e
        persiste. Nuovo ciclo (nuovo GameState): la correzione si
        applica in lettura, l'Accademia e' trovata allo slot risolto e
        la voce resta stabile (niente correzione ripetuta)."""
        settings = _settings()
        village = _village()

        # Ciclo 1: sonda su slot 25 (mappa dice gid 25), verifica gid 22.
        cycle1_fields = [_field(25, gid=25), _field(30, gid=0)]
        _persist_slot_corrections(
            settings, village, cycle1_fields, {25: 22}, resolved_slot=25
        )

        # Ciclo 2: nuova scansione, stessa mappa buggy -> la rettifica
        # persistente risolve prima delle decisioni.
        cycle2_fields = [_field(25, gid=25), _field(30, gid=0)]
        assert _apply_slot_corrections(settings, VID, cycle2_fields) == 1
        academy = next(
            (
                f
                for f in cycle2_fields
                if f.gid == 22 and f.level > 0
            ),
            None,
        )
        assert academy is not None
        assert academy.slot_id == 25

        # La voce non e' stata consumata/invalidata: cicli successivi
        # stabili finche' la mappa resta buggy.
        loaded = db.slot_corrections_load(ACC, VID)
        assert loaded[25]["actual_gid"] == 22
        assert loaded[25]["resolved"] is True
