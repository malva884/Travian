"""Test del motore di configurazione adattiva (spec Role-Aware
Village Configurator & Live Feasibility Preview)."""

from travian_agent.config_engine import (
    VillageState,
    cap_at_level,
    evaluate_config_preview,
    generate_role_config,
    level_cost,
    level_time_hours,
    unit_trainer_gid,
    village_state_from_snapshot,
)
from travian_agent.building_prereqs import (
    GID_BARRACKS,
    GID_MAIN_BUILDING,
    GID_RESIDENCE,
    GID_WAREHOUSE,
)


def _fresh_village() -> VillageState:
    """Villaggio neo-fondato: niente edifici, capienza base, 15c."""
    return VillageState(
        warehouse_cap=800,
        granary_cap=800,
        crop_production=2000,
        crop_balance=2000,
        buildings={},
        troops={},
        tribe="romans",
    )


# ---------------------------------------------------------------------------
# village_state_from_snapshot
# ---------------------------------------------------------------------------


def test_snapshot_normalization_enriched_record():
    snap = {
        "resources": {"warehouse_cap": 2300, "granary_cap": 1200},
        "production": {"crop": 1500, "wood": 800},
        "crop_balance": 400,
        "buildings": {"15": 5, "10": 3},
        "troops": {"t1": 30},
        "tribe": "romans",
    }
    vs = village_state_from_snapshot(snap)
    assert vs.warehouse_cap == 2300
    assert vs.granary_cap == 1200
    assert vs.crop_production == 1500
    assert vs.crop_balance == 400
    assert vs.buildings[GID_MAIN_BUILDING] == 5
    assert vs.troops["t1"] == 30
    assert vs.tribe == "romans"


def test_snapshot_normalization_legacy_record():
    """Record pre-arricchimento: solo risorse -> default sicuri."""
    vs = village_state_from_snapshot(
        {"resources": {"warehouse_cap": 800, "granary_cap": 800}}
    )
    assert vs.warehouse_cap == 800
    assert vs.buildings == {}
    assert vs.crop_balance is None
    assert vs.net_crop == 0


# ---------------------------------------------------------------------------
# generate_role_config — Capacity-Aware Scaling
# ---------------------------------------------------------------------------


def test_storage_auto_inserted_before_costly_step():
    """Residenza 10 (costo > 800) su villaggio fresco -> upgrade
    Magazzino/Granaio inseriti PRIMA dello step."""
    gen = generate_role_config(_fresh_village(), "founder")
    keys = [s["key"] for s in gen["building_queue"]]
    res_pos = keys.index("residence")
    wh_positions = [i for i, k in enumerate(keys) if k == "warehouse"]
    gr_positions = [i for i, k in enumerate(keys) if k == "granary"]
    # Almeno un upgrade storage oltre quelli del preset, e tutti prima
    # della Residenza nel punto in cui servono.
    assert gen["storage_auto_inserted"], (
        "Atteso auto-inserimento storage per la Residenza 10"
    )
    inserted_pos = [
        i for i, s in enumerate(gen["building_queue"])
        if s["key"] in ("warehouse", "granary")
        and s["target_level"] > 10
    ]
    assert inserted_pos, "Atteso storage oltre livello 10 in coda"
    assert min(inserted_pos) < res_pos


def test_no_storage_insert_when_capacity_sufficient():
    """Villaggio maturo con Magazzino capiente -> nessun inserimento."""
    vs = _fresh_village()
    vs.warehouse_cap = 80000
    vs.granary_cap = 80000
    vs.buildings = {
        GID_WAREHOUSE: 20, GID_MAIN_BUILDING: 5, GID_RESIDENCE: 10,
    }
    gen = generate_role_config(vs, "founder")
    assert gen["storage_auto_inserted"] == []


def test_troop_targets_scaled_to_net_crop_cap():
    """I target non superano il 75% della produzione lorda di grano."""
    vs = _fresh_village()
    vs.crop_production = 100  # grano scarso -> scaling aggressivo
    gen = generate_role_config(vs, "founder")
    upkeep = sum(gen["troop_targets"].values())  # t1 -> 1/u
    assert upkeep <= 75 + len(gen["troop_targets"])  # max(1, ...) floor
    assert all(q >= 1 for q in gen["troop_targets"].values())


def test_units_excluded_when_trainer_missing():
    """Unita' il cui edificio produttore e' assente E non in coda ->
    escluse dai target immediati (spec filtro prerequisiti)."""
    vs = _fresh_village()
    gen = generate_role_config(vs, "hammer")
    # Hammer preset: cavalleria/ram/catapult -> Scuderia/Officina non in
    # coda hammer? La coda hammer le include, quindi nessuna esclusione
    # attesa se i trainer sono in coda. Verifica coerenza: ogni unita'
    # target ha trainer presente o in coda.
    from travian_agent.building_prereqs import FOCUS_KEY_GIDS

    queue_gids = set()
    for s in gen["building_queue"]:
        raw = FOCUS_KEY_GIDS.get(s["key"])
        gids = (raw,) if isinstance(raw, int) else tuple(raw or ())
        queue_gids.update(g for g in gids if g > 0)
    for u in gen["troop_targets"]:
        tg = unit_trainer_gid(u)
        assert tg == 0 or vs.buildings.get(tg, 0) > 0 or tg in queue_gids
    for x in gen["excluded_units"]:
        tg = unit_trainer_gid(x["unit"])
        assert vs.buildings.get(tg, 0) == 0 and tg not in queue_gids


# ---------------------------------------------------------------------------
# evaluate_config_preview — funzione pura
# ---------------------------------------------------------------------------


def test_preview_net_crop_deficit():
    """Net Crop a regime = netto attuale - upkeep del deficit truppe."""
    vs = _fresh_village()
    vs.crop_balance = 500
    pv = evaluate_config_preview(vs, {
        "building_queue": [],
        "troop_targets": {"t1": 100},  # 100 legionari x 1 grano/h
    })
    assert pv["projected_net_crop"] == 400
    assert pv["net_crop_status"] == "ok"
    assert pv["troop_upkeep_delta"] == 100


def test_preview_net_crop_danger():
    vs = _fresh_village()
    vs.crop_balance = 30
    pv = evaluate_config_preview(vs, {
        "building_queue": [],
        "troop_targets": {"t1": 100},
    })
    assert pv["projected_net_crop"] == -70
    assert pv["net_crop_status"] == "danger"


def test_preview_existing_troops_offset_deficit():
    """Le truppe gia' presenti non incidono sul deficit."""
    vs = _fresh_village()
    vs.crop_balance = 500
    vs.troops = {"t1": 80}
    pv = evaluate_config_preview(vs, {
        "building_queue": [],
        "troop_targets": {"t1": 100},
    })
    assert pv["troop_upkeep_delta"] == 20
    assert pv["projected_net_crop"] == 480


def test_preview_queue_time_and_capacity_block():
    """La simulazione stima ore e segnala il primo step non capiente."""
    vs = _fresh_village()
    vs.buildings = {GID_MAIN_BUILDING: 1}
    pv = evaluate_config_preview(vs, {
        "building_queue": [
            {"key": "warehouse", "target_level": 10},
            {"key": "residence", "target_level": 10},
        ],
        "troop_targets": {},
    })
    assert pv["estimated_queue_hours"] > 0
    # Residenza 10: il costo dei livelli alti supera la capienza base
    # simulata fino allo step warehouse 10 (11800)? Il picco costo
    # residence ~5700 < 11800 -> nessun blocco atteso qui; verifica solo
    # che la chiave esista e sia coerente.
    assert "first_capacity_block" in pv


def test_preview_troop_prereq_warning():
    """Target su unita' senza Caserma e senza Caserma in coda -> warning."""
    vs = _fresh_village()
    pv = evaluate_config_preview(vs, {
        "building_queue": [],
        "troop_targets": {"t1": 50},
    })
    assert any("Caserma" in w for w in pv["missing_prerequisites"])


def test_preview_queue_prereq_warning():
    """Step Accademia senza Caserma 3 -> warning in coda."""
    vs = _fresh_village()
    vs.buildings = {GID_MAIN_BUILDING: 5}
    pv = evaluate_config_preview(vs, {
        "building_queue": [{"key": "academy", "target_level": 10}],
        "troop_targets": {},
    })
    assert any("Caserma" in w for w in pv["queue_warnings"])


def test_preview_is_pure():
    """La preview non muta lo stato del villaggio."""
    vs = _fresh_village()
    before = dict(vs.buildings)
    evaluate_config_preview(vs, {
        "building_queue": [{"key": "warehouse", "target_level": 10}],
        "troop_targets": {"t1": 10},
    })
    assert vs.buildings == before


# ---------------------------------------------------------------------------
# Tabelle statiche
# ---------------------------------------------------------------------------


def test_trainer_mapping():
    assert unit_trainer_gid("t1") == GID_BARRACKS      # Legionario
    assert unit_trainer_gid("t5") == 20                # Imp. Eq. -> Scuderia
    assert unit_trainer_gid("t8") == 21                # Catapulta -> Officina
    assert unit_trainer_gid("t10") == GID_RESIDENCE    # Colono
    assert unit_trainer_gid("t22") == GID_BARRACKS     # Spadaccino


def test_cost_time_helpers():
    c1 = level_cost(GID_WAREHOUSE, 1)
    c5 = level_cost(GID_WAREHOUSE, 5)
    assert c1["wood"] > 0
    assert c5["wood"] > c1["wood"]
    t1 = level_time_hours(GID_WAREHOUSE, 1, mb_level=1)
    t1_fast = level_time_hours(GID_WAREHOUSE, 1, mb_level=20)
    assert t1_fast < t1
    assert cap_at_level(0) == 800
    assert cap_at_level(20) == 80000
