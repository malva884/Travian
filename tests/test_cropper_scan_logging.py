"""Test del logging dettagliato del CropperScanner (MapScanner.scan).

Verifica i messaggi introdotti per monitorare la scansione mappa:
- INFO all'avvio con parametri attivi (centri, raggio, filtri);
- DEBUG per il progresso per-tile e i motivi di scarto;
- INFO per il match trovato e il riepilogo finale.

L'observer e' finto: nessun browser, nessuna pagina Travian.
"""

from __future__ import annotations

import asyncio
import logging

from travian_agent.cropper import (
    CropperCandidate,
    CropperStore,
    MapScanner,
    STATUS_APPROVED,
)
from travian_agent.models import Village


class _FakeObserver:
    """GameObserver finto: griglia e dettagli tile prefissati."""

    def __init__(self, tiles: list[dict], details: dict[tuple[int, int], dict]):
        self._tiles = tiles
        self._details = details
        self.detail_calls: list[tuple[int, int]] = []

    async def scan_map_area(self, cx: int, cy: int, radius: int) -> list[dict]:
        return list(self._tiles)

    async def read_map_tile(self, x: int, y: int) -> dict:
        self.detail_calls.append((x, y))
        return dict(
            self._details.get((x, y), {"occupied": True, "crop_fields": 0})
        )


def _run(scanner, observer, **kwargs):
    return asyncio.run(scanner.scan(observer=observer, **kwargs))


def _store(tmp_path) -> CropperStore:
    return CropperStore(path=tmp_path / "candidates.json")


def test_scan_logs_start_progress_match_and_summary(tmp_path, caplog):
    store = _store(tmp_path)
    # Tile (14|10) gia' catalogato come approved: deve essere saltato
    # prima del dettaglio e loggato in DEBUG.
    store.upsert(
        "acc_t",
        CropperCandidate(
            id="c_15_14_10", x=14, y=10, type="15c", status=STATUS_APPROVED
        ),
    )
    scanner = MapScanner(store)

    grid = [
        # libero -> dettaglio rivela 15c -> MATCH
        {"x": 11, "y": 10, "village_name": "", "population": 0,
         "is_oasis": False, "distance": 1.0},
        # libero -> dettaglio rivela occupato -> scarto
        {"x": 12, "y": 10, "village_name": "", "population": 0,
         "is_oasis": False, "distance": 2.0},
        # libero -> dettaglio rivela 6c -> non cropper
        {"x": 13, "y": 10, "village_name": "", "population": 0,
         "is_oasis": False, "distance": 3.0},
        # libero ma gia' catalogato -> salto senza dettaglio
        {"x": 14, "y": 10, "village_name": "", "population": 0,
         "is_oasis": False, "distance": 4.0},
        # occupato sulla griglia -> non entra nei liberi
        {"x": 15, "y": 10, "village_name": "Nemico", "population": 80,
         "is_oasis": False, "distance": 5.0},
        # oasi -> contata a parte
        {"x": 16, "y": 10, "village_name": "Oasi Grano +25%",
         "population": 0, "is_oasis": True, "distance": 6.0},
    ]
    details = {
        (11, 10): {"occupied": False, "crop_fields": 15,
                   "free": True, "settle_link": True},
        (12, 10): {"occupied": True, "crop_fields": 0},
        (13, 10): {"occupied": False, "crop_fields": 6,
                   "free": True, "settle_link": True},
    }
    observer = _FakeObserver(grid, details)
    villages = [Village(name="Bivona", x=10, y=10)]

    with caplog.at_level(logging.DEBUG, logger="travian_agent.cropper"):
        found, tiles_seen = _run(
            scanner, observer, account_id="acc_t",
            villages=villages, radius=15, prefix="[acc_t] ",
        )

    assert (found, tiles_seen) == (1, 6)
    # Il tile catalogato non deve essere ri-dettagliato.
    assert (14, 10) not in observer.detail_calls
    assert (15, 10) not in observer.detail_calls
    assert (16, 10) not in observer.detail_calls
    assert sorted(observer.detail_calls) == [(11, 10), (12, 10), (13, 10)]

    text = caplog.text
    # Config attiva all'avvio (INFO)
    assert "avvio scansione" in text
    assert "Bivona(10|10)" in text
    assert "raggio: 15" in text
    assert "15c/9c" in text
    # Riepilogo per centro (INFO)
    assert "6 tile letti, 4 liberi, 1 oasi" in text
    # Progresso/scarti per-tile (DEBUG)
    assert "dettaglio tile" in text
    assert "(12|10) occupato, scarto" in text
    assert "(13|10) ha 6c" in text
    assert "(14|10) gia' catalogato" in text
    # Match e riepilogo finale (INFO) — formato da spec:
    # [Cropper Finder] Trovato 15c a (X|Y) | Bonus Oasi: +50% | Stato: Pending
    assert "Trovato 15c a (11|10)" in text
    assert "Bonus Oasi:" in text
    assert "Stato: Pending" in text
    assert "scansione completata" in text
    assert "nuovi candidati: 1" in text


def test_scan_without_coordinates_logs_skip(tmp_path, caplog):
    scanner = MapScanner(_store(tmp_path))
    observer = _FakeObserver([], {})
    villages = [Village(name="SenzaCoord")]

    with caplog.at_level(logging.DEBUG, logger="travian_agent.cropper"):
        found, tiles_seen = _run(
            scanner, observer, account_id="acc_t",
            villages=villages, prefix="[acc_t] ",
        )

    assert (found, tiles_seen) == (0, 0)
    assert "scansione saltata" in caplog.text
    assert observer.detail_calls == []


def test_detail_check_limit_is_logged(tmp_path, caplog):
    scanner = MapScanner(_store(tmp_path))
    grid = [
        {"x": 11, "y": 10, "village_name": "", "population": 0,
         "is_oasis": False, "distance": 1.0},
        {"x": 12, "y": 10, "village_name": "", "population": 0,
         "is_oasis": False, "distance": 2.0},
    ]
    details = {
        (11, 10): {"occupied": False, "crop_fields": 9,
                   "free": True, "settle_link": True},
        (12, 10): {"occupied": False, "crop_fields": 15,
                   "free": True, "settle_link": True},
    }
    observer = _FakeObserver(grid, details)

    with caplog.at_level(logging.DEBUG, logger="travian_agent.cropper"):
        found, _ = _run(
            scanner, observer, account_id="acc_t",
            villages=[Village(name="Bivona", x=10, y=10)],
            max_detail_checks=1, prefix="[acc_t] ",
        )

    assert found == 1
    assert observer.detail_calls == [(11, 10)]
    assert "raggiunto limite dettagli" in caplog.text
