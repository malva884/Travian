"""Replay harness per le fixture catturate dall'agente.

``telemetry_capture`` salva coppie ``<ts>_<code>_<host>.html/.meta.json``
in ``tests/fixtures/captured/`` a ogni anomalia strutturale deduplicata.
Questo modulo le carica parametricamente e riparte i guard su HTML e
metadati:

- ``dom_gid``: il parser gid pure-Python deve estrarre dallo stesso
  HTML il gid registrato a capture-time;
- ``slot_url_match``: ``_slot_url_check`` deve riconoscere lo stesso
  URL slot che l'azione riconobbe;
- ``url_is_building``: ``_is_building_url`` deve classificare l'URL
  come pagina edificio nello stesso modo del capture;
- ``sanitized``: i token di sessione/cookie/email non devono comparire
  nel file HTML persistito (regressione privacy).

Il file e' vuoto-safe: in assenza di fixture nessun test viene
generato e la suite resta verde. Ogni fixture aggiunta in futuro viene
raccolta automaticamente al prossimo run.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from travian_agent.actions import _is_building_url, _slot_url_check
from travian_agent.telemetry_capture import detect_gid_in_html

CAPTURED_DIR = Path(__file__).resolve().parent / "fixtures" / "captured"


def _load_pairs() -> list[Path]:
    """Tutti i .meta.json catturati con .html accoppiato, ordinati."""
    if not CAPTURED_DIR.is_dir():
        return []
    return [
        m
        for m in sorted(CAPTURED_DIR.glob("*.meta.json"))
        if _html_for(m).exists()
    ]


def _html_for(meta_path: Path) -> Path:
    return meta_path.with_name(
        meta_path.name[: -len(".meta.json")] + ".html"
    )


_CAPTURED = _load_pairs()


@pytest.mark.skipif(not _CAPTURED, reason="nessuna fixture catturata")
class TestCapturedFixtures:
    @pytest.mark.parametrize(
        "meta_path",
        _CAPTURED,
        ids=[m.name for m in _CAPTURED],
    )
    def test_meta_schema(self, meta_path: Path) -> None:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta.get("error_code"), "meta senza error_code"
        assert meta.get("host"), "meta senza host"
        assert "url" in meta, "meta senza url"
        assert "profile_version" in meta

    @pytest.mark.parametrize(
        "meta_path",
        _CAPTURED,
        ids=[m.name for m in _CAPTURED],
    )
    def test_dom_gid_replay(self, meta_path: Path) -> None:
        """Il gid registrato nel meta deve riemergere dallo stesso HTML."""
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        expected_gid = (meta.get("expected") or {}).get("dom_gid")
        if expected_gid is None:
            pytest.skip("fixture senza dom_gid atteso")
        html = _html_for(meta_path).read_text(
            encoding="utf-8", errors="replace"
        )
        assert detect_gid_in_html(html) == expected_gid, (
            f"gid replay diverso nel fixture {meta_path.name}"
        )

    @pytest.mark.parametrize(
        "meta_path",
        _CAPTURED,
        ids=[m.name for m in _CAPTURED],
    )
    def test_slot_guard_replay(self, meta_path: Path) -> None:
        """I guard URL devono riprodurre il risultato registrato al capture."""
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        url = str(meta.get("url") or "")
        expected = meta.get("expected") or {}
        if "slot_url_match" in expected and meta.get("slot_id") is not None:
            match = bool(
                _slot_url_check(int(meta["slot_id"])).search(url)
            )
            assert match == bool(expected["slot_url_match"]), (
                f"slot_url_check diverso per {meta_path.name}"
            )
        if "url_is_building" in expected:
            assert _is_building_url(url) == bool(
                expected["url_is_building"]
            ), f"_is_building_url diverso per {meta_path.name}"

    @pytest.mark.parametrize(
        "meta_path",
        _CAPTURED,
        ids=[m.name for m in _CAPTURED],
    )
    def test_html_sanitized(self, meta_path: Path) -> None:
        """Nessun token di sessione/cookie/email nel fixture persistito."""
        html = _html_for(meta_path).read_text(
            encoding="utf-8", errors="replace"
        )
        assert "REDACTED" not in meta_path.name
        # Token query-string non offuscati non devono sopravvivere.
        assert not re.search(
            r"sessid=[^&\"'<>\s]{8,}", html, flags=re.IGNORECASE
        )
        # Email reali non devono comparire: solo il placeholder.
        assert not re.search(
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.(com|it|de|fr|org|net)\b",
            html,
        )
