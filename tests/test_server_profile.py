"""Unit test per server_profile + telemetry_capture (self-healing)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from travian_agent import telemetry_capture as cap
from travian_agent.server_profile import (
    ServerProfile,
    calibrate_routing,
    host_from_url,
    profile_path,
)


# ---------------------------------------------------------------------------
# host_from_url / profile_path
# ---------------------------------------------------------------------------

class TestHostParsing:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://ts30.x3.europe.travian.com", "ts30.x3.europe.travian.com"),
            ("https://ts30.x3.europe.travian.com/dorf1.php", "ts30.x3.europe.travian.com"),
            ("ts1.x1.travian.com", "ts1.x1.travian.com"),
            ("", ""),
            (None, ""),
        ],
    )
    def test_host_extraction(self, url, expected):
        assert host_from_url(url) == expected


class TestProfilePath:
    def test_filename_sanitized(self):
        p = profile_path("ts30.x3.europe.travian.com")
        assert p.name == "server_profile_ts30.x3.europe.travian.com.json"

    def test_weird_host_chars(self):
        p = profile_path("weird:host/name")
        assert "/" not in p.name and ":" not in p.name


# ---------------------------------------------------------------------------
# Persistenza
# ---------------------------------------------------------------------------

class TestProfilePersistence:
    def test_defaults_clean(self):
        p = ServerProfile.load("missing-host.test.invalid")
        assert p.routing_mode == "unknown"
        assert p.empty_slot_gids == [None, -1]
        assert p.preferred_selectors == {}
        assert p.last_calibrated_at is None

    def test_roundtrip_survives_restart(self, tmp_path, monkeypatch):
        from travian_agent import server_profile as sp

        monkeypatch.setattr(sp, "STATE_DIR", tmp_path)
        p = ServerProfile(host="t.example.com")
        p.set_routing_mode("modern")
        p.calibrate_empty_gid(0)
        p.promote_selector("market_target_input", "input#enterVillageName")
        p.save()

        loaded = ServerProfile.load("t.example.com")
        assert loaded.routing_mode == "modern"
        assert 0 in loaded.empty_slot_gids
        assert loaded.preferred_selectors["market_target_input"] == (
            "input#enterVillageName"
        )
        assert loaded.last_calibrated_at is not None

    def test_corrupt_file_returns_clean(self, tmp_path, monkeypatch):
        from travian_agent import server_profile as sp

        monkeypatch.setattr(sp, "STATE_DIR", tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        path = sp.profile_path("bad.example.com")
        path.write_text("{not json", encoding="utf-8")
        p = ServerProfile.load("bad.example.com")
        assert p.routing_mode == "unknown"


# ---------------------------------------------------------------------------
# Calibrazioni
# ---------------------------------------------------------------------------

class TestEmptyGidCalibration:
    def test_gid0_calibrated_once(self, tmp_path, monkeypatch):
        from travian_agent import server_profile as sp

        monkeypatch.setattr(sp, "STATE_DIR", tmp_path)
        p = ServerProfile(host="h.test")
        assert not p.is_empty_gid(0)
        assert p.calibrate_empty_gid(0) is True
        assert p.is_empty_gid(0)
        # Seconda calibrazione: no-op
        assert p.calibrate_empty_gid(0) is False
        assert p.empty_slot_gids == [None, -1, 0]

    def test_unknown_gid_not_empty(self):
        p = ServerProfile(host="h.test")
        assert not p.is_empty_gid(17)
        assert p.is_empty_gid(-1)
        assert p.is_empty_gid(None)


class TestSelectorPromotion:
    def test_preferred_first(self):
        p = ServerProfile(host="h.test")
        sels = ["input[name='dname']", "input#enterVillageName"]
        p.promote_selector("k", "input#enterVillageName")
        assert p.ordered_selectors("k", sels) == [
            "input#enterVillageName",
            "input[name='dname']",
        ]

    def test_preferred_not_in_list_noop(self):
        p = ServerProfile(host="h.test")
        sels = ["a", "b"]
        p.promote_selector("k", "not-in-list")
        assert p.ordered_selectors("k", sels) == ["a", "b"]

    def test_no_preference_passthrough(self):
        p = ServerProfile(host="h.test")
        assert p.ordered_selectors("k", ["x", "y"]) == ["x", "y"]


# ---------------------------------------------------------------------------
# Routing probe
# ---------------------------------------------------------------------------

class _FakePage:
    def __init__(self, result):
        self._result = result
        self.url = "https://ts.test/dorf1.php"

    async def evaluate(self, js):
        return self._result


class TestRoutingCalibration:
    def test_modern_detected(self, tmp_path, monkeypatch):
        from travian_agent import server_profile as sp

        monkeypatch.setattr(sp, "STATE_DIR", tmp_path)
        prof = ServerProfile(host="ts.test")
        asyncio.run(calibrate_routing(_FakePage("modern"), prof))
        assert prof.routing_mode == "modern"

    def test_legacy_detected(self, tmp_path, monkeypatch):
        from travian_agent import server_profile as sp

        monkeypatch.setattr(sp, "STATE_DIR", tmp_path)
        prof = ServerProfile(host="ts2.test")
        asyncio.run(calibrate_routing(_FakePage("legacy"), prof))
        assert prof.routing_mode == "legacy"

    def test_known_mode_not_reprobed(self):
        prof = ServerProfile(host="ts.test", routing_mode="legacy")
        result = asyncio.run(
            calibrate_routing(_FakePage("modern"), prof)
        )
        assert result == "legacy"
        assert prof.routing_mode == "legacy"


# ---------------------------------------------------------------------------
# telemetry_capture: sanitize + dedup + write
# ---------------------------------------------------------------------------

class TestSanitize:
    def test_session_query_token_redacted(self):
        html = '<a href="dorf1.php?sessid=abc123def456ghi789">x</a>'
        out = cap.sanitize_html(html)
        assert "abc123def456" not in out
        assert "sessid=" in out  # nome parametro preservato

    def test_email_redacted(self):
        out = cap.sanitize_html("contatta admin@test.com ora")
        assert "admin@test.com" not in out
        assert "[EMAIL_REDACTED]" in out

    def test_json_token_redacted(self):
        out = cap.sanitize_html('{"session_id":"supersecret","x":1}')
        assert "supersecret" not in out

    def test_coords_scrub_opt_in(self):
        html = "villaggio a (-12|34) qui"
        assert cap.sanitize_html(html, scrub_coords=True) == "villaggio a (0|0) qui"
        assert cap.sanitize_html(html, scrub_coords=False) == html


class TestDetectGidInHtml:
    def test_contract_building_gid(self):
        html = '<div id="contract_building17" class="gid17 contractLink">x</div>'
        assert cap.detect_gid_in_html(html) == 17

    def test_build_id_gid(self):
        html = '<div id="build" class="g10 building">y</div>'
        assert cap.detect_gid_in_html(html) == 10

    def test_no_gid_returns_none(self):
        assert cap.detect_gid_in_html("<html><body></body></html>") is None
        assert cap.detect_gid_in_html("") is None

    def test_gid0_detected(self):
        html = '<div id="build" class="g0">empty</div>'
        assert cap.detect_gid_in_html(html) == 0


class TestWriteCapture:
    def _write(self, tmp_path, monkeypatch, **kwargs):
        monkeypatch.setattr(cap, "FIXTURES_DIR", tmp_path / "fx")
        monkeypatch.setattr(cap, "_DEDUP_PATH", tmp_path / "dedup.json")
        return cap.write_capture(
            '<html><body id="build" class="g0"></body></html>',
            error_code="BTN_NOT_FOUND",
            url="https://h.test/build.php?id=30",
            slot_id=30,
            expected_gid=17,
            **kwargs,
        )

    def test_writes_pair(self, tmp_path, monkeypatch):
        meta_path = self._write(tmp_path, monkeypatch)
        assert meta_path is not None
        assert meta_path.name.endswith(".meta.json")
        html = meta_path.with_name(
            meta_path.name.replace(".meta.json", ".html")
        )
        assert html.exists()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["error_code"] == "BTN_NOT_FOUND"
        assert meta["slot_id"] == 30
        assert meta["expected_gid"] == 17
        assert meta["host"] == "h.test"
        assert meta["detected_gid"] == 0
        assert meta["expected"]["dom_gid"] == 0

    def test_dedup_6h_window(self, tmp_path, monkeypatch):
        first = self._write(tmp_path, monkeypatch, now=1000.0)
        second = self._write(tmp_path, monkeypatch, now=1000.0 + 3600)
        assert first is not None and second is None
        # Dopo la finestra: nuovo dump permesso
        third = self._write(
            tmp_path, monkeypatch,
            now=1000.0 + cap.DEDUP_WINDOW_SECONDS + 10,
        )
        assert third is not None and third != first

    def test_disabled_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRAVIAN_CAPTURE_FIXTURES", "0")
        assert self._write(tmp_path, monkeypatch) is None

    def test_dedup_survives_reload(self, tmp_path, monkeypatch):
        """L'indice e' su disco: un nuovo processo rispetta il rate limit."""
        first = self._write(tmp_path, monkeypatch, now=2000.0)
        # Seconda chiamata senza cache: ricarica dedup.json da disco.
        second = self._write(tmp_path, monkeypatch, now=2000.0 + 60)
        assert first is not None and second is None
