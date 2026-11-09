"""Test della logica di batch sizing per l'addestramento di emergenza."""

from __future__ import annotations

from travian_agent.actions import ActionExecutor


def _data(wood=0, clay=0, iron=0, crop=0, units=None) -> dict:
    return {
        "resources": {"wood": wood, "clay": clay, "iron": iron, "crop": crop},
        "units": units or [],
    }


class TestPickTrainingBatch:
    def test_no_units_returns_none(self):
        assert ActionExecutor._pick_training_batch(_data(wood=1000)) is None

    def test_no_affordable_unit_returns_none(self):
        data = _data(
            wood=10, clay=10, iron=10, crop=10,
            units=[{
                "inputName": "t1",
                "label": "Legionario",
                "costs": {"wood": 120, "clay": 100, "iron": 150, "crop": 30},
            }],
        )
        assert ActionExecutor._pick_training_batch(data) is None

    def test_unit_without_costs_skipped(self):
        data = _data(
            wood=5000, clay=5000, iron=5000, crop=5000,
            units=[{"inputName": "t1", "label": "Legionario", "costs": None}],
        )
        assert ActionExecutor._pick_training_batch(data) is None

    def test_batch_size_is_min_quota(self):
        """Quantita' = min(floor(risorse*margine / costo) per risorsa)."""
        data = _data(
            wood=10000, clay=10000, iron=10000, crop=10000,
            units=[{
                "inputName": "t1",
                "label": "Legionario",
                # wood: 9500/120=79, clay: 95, iron: 63, crop: 300
                "costs": {"wood": 120, "clay": 100, "iron": 150, "crop": 30},
            }],
        )
        result = ActionExecutor._pick_training_batch(data)
        assert result is not None
        input_name, qty, _label = result
        assert input_name == "t1"
        assert qty == 63  # limitato dal ferro (9500 // 150)

    def test_crop_margin_protects_granary(self):
        """Il margine grano (90%) deve limitare piu' strettamente del resto."""
        data = _data(
            wood=100000, clay=100000, iron=100000, crop=10000,
            units=[{
                "inputName": "t1",
                "label": "Emissario",
                "costs": {"wood": 10, "clay": 10, "iron": 10, "crop": 30},
            }],
        )
        _name, qty, _label = ActionExecutor._pick_training_batch(data)
        # budget grano = 9000 -> 300 unita' max
        assert qty == 300
        # non deve prosciugare il granaio
        assert qty * 30 <= int(10000 * 0.90)

    def test_picks_unit_maximizing_surplus_consumption(self):
        """Fra due unita' addestrabili vince chi smaltisce piu' risorse."""
        data = _data(
            wood=50000, clay=50000, iron=50000, crop=50000,
            units=[
                {
                    "inputName": "t1",
                    "label": "Legionario",
                    "costs": {"wood": 120, "clay": 100, "iron": 150, "crop": 30},
                },
                {
                    "inputName": "t2",
                    "label": "Equites Caesaris",
                    "costs": {"wood": 550, "clay": 640, "iron": 800, "crop": 180},
                },
            ],
        )
        input_name, qty, label = ActionExecutor._pick_training_batch(data)
        # t2 (cavalleria pesante) consuma molto di piu' per unita'
        assert input_name == "t2"
        assert label == "Equites Caesaris"
        assert qty > 0

    def test_priority_units_filters_out_scouts(self):
        """Se priority_units è impostata, gli esploratori/emissari non autorizzati vengono scartati."""
        data = _data(
            wood=50000, clay=50000, iron=50000, crop=50000,
            units=[
                {
                    "inputName": "t4",
                    "label": "Emissario a cavallo",
                    "costs": {"wood": 140, "clay": 160, "iron": 20, "crop": 40},
                },
                {
                    "inputName": "t3",
                    "label": "Imperiano",
                    "costs": {"wood": 150, "clay": 160, "iron": 210, "crop": 80},
                },
            ],
        )
        # Solo Imperiani consentiti
        result = ActionExecutor._pick_training_batch(
            data, priority_units=["Imperiani", "Cavalieri"]
        )
        assert result is not None
        input_name, qty, label = result
        assert input_name == "t3"
        assert "Imperiano" in label
        assert qty > 0

    def test_priority_units_no_match_returns_none(self):
        """Se nessuna unità corrisponde alla lista priorità, non addestra nulla."""
        data = _data(
            wood=50000, clay=50000, iron=50000, crop=50000,
            units=[
                {
                    "inputName": "t4",
                    "label": "Emissario a cavallo",
                    "costs": {"wood": 140, "clay": 160, "iron": 20, "crop": 40},
                },
            ],
        )
        result = ActionExecutor._pick_training_batch(
            data, priority_units=["Imperiani", "Cavalieri di Cesare"]
        )
        assert result is None

    def test_priority_units_respects_order_precedence(self):
        """Rispetta l'ordine di priorità della lista priority_units."""
        data = _data(
            wood=50000, clay=50000, iron=50000, crop=50000,
            units=[
                {
                    "inputName": "t1",
                    "label": "Legionario",
                    "costs": {"wood": 120, "clay": 100, "iron": 150, "crop": 30},
                },
                {
                    "inputName": "t3",
                    "label": "Imperiano",
                    "costs": {"wood": 150, "clay": 160, "iron": 210, "crop": 80},
                },
            ],
        )
        # Imperiani prima di Legionari
        result = ActionExecutor._pick_training_batch(
            data, priority_units=["Imperiani", "Legionari"]
        )
        assert result is not None
        input_name, _qty, label = result
        assert input_name == "t3"
        assert "Imperiano" in label

    def test_alias_matching_for_equites(self):
        """'Cavalieri' o 'EI' o 'Cavalieri del Generale' matchano Equites Imperatoris (t5)."""
        data = _data(
            wood=50000, clay=50000, iron=50000, crop=50000,
            units=[
                {
                    "inputName": "t4",
                    "label": "Emissario a cavallo",
                    "costs": {"wood": 140, "clay": 160, "iron": 20, "crop": 40},
                },
                {
                    "inputName": "t5",
                    "label": "Equites Imperatoris",
                    "costs": {"wood": 550, "clay": 440, "iron": 320, "crop": 100},
                },
            ],
        )
        result = ActionExecutor._pick_training_batch(
            data, priority_units=["Cavalieri"]
        )
        assert result is not None
        input_name, _qty, label = result
        assert input_name == "t5"
        assert "Equites Imperatoris" in label


class TestVideoBonusSettings:
    def test_auto_video_bonus_default_true(self):
        from travian_agent.config import Settings
        s = Settings(server_url="https://ts1.travian.com")
        assert s.auto_video_bonus is True

    def test_action_type_claim_video_bonus_exists(self):
        from travian_agent.models import Action, ActionType
        act = Action(type=ActionType.CLAIM_VIDEO_BONUS)
        assert act.type == ActionType.CLAIM_VIDEO_BONUS

