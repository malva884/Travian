"""Unit test per AI Scout Brain e Intelligence Militare."""

import sys
import unittest
import tempfile
import shutil
from pathlib import Path

# Assicura import del modulo travian_agent
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from travian_agent.config import Settings
from travian_agent.models import (
    GameState,
    Resources,
    ResourceType,
    Village,
)
from travian_agent.ai_scout_brain import (
    AIScoutBrain,
    assess_account_power,
    calculate_distance,
    heuristic_evaluate_farming_targets,
    normalize_unit_key,
)


class TestAIScoutBrain(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_normalize_unit_key(self):
        self.assertEqual(normalize_unit_key("u1"), "t1")
        self.assertEqual(normalize_unit_key("unit3"), "t3")
        self.assertEqual(normalize_unit_key("t11"), "t11")

    def test_calculate_distance(self):
        dist = calculate_distance(0, 0, 3, 4)
        self.assertEqual(dist, 5.0)

    def test_assess_account_power(self):
        settings = Settings(account_id="test_warroom", server_url="https://ts1.travian.com")
        villages = [
            Village(village_id=1, name="Hipponion", x=10, y=20),
            Village(village_id=2, name="Vibo Valentia", x=12, y=25),
        ]
        res1 = Resources(
            wood=1000,
            clay=1000,
            iron=1000,
            crop=2000,
            warehouse_capacity=10000,
            granary_capacity=10000,
            production_per_hour={ResourceType.CROP: 500, ResourceType.WOOD: 400},
        )
        res2 = Resources(
            wood=500,
            clay=500,
            iron=500,
            crop=800,
            warehouse_capacity=5000,
            granary_capacity=5000,
            production_per_hour={ResourceType.CROP: 200, ResourceType.WOOD: 200},
        )
        states = {
            "Hipponion": GameState(resources=res1, village_name="Hipponion", troops_home={"t1": 20, "t5": 10}),
            "Vibo Valentia": GameState(resources=res2, village_name="Vibo Valentia", troops_home={"t1": 5}),
        }

        power = assess_account_power(settings, villages, states)

        self.assertGreater(power.military_power, 0)
        self.assertEqual(power.total_resources["wood"], 1500)
        self.assertEqual(power.total_hourly_production["crop"], 700)
        self.assertGreater(power.storage_saturation_pct, 0.0)
        self.assertEqual(len(power.village_summaries), 2)

    def test_heuristic_evaluate_farming_targets(self):
        account_status = {
            "troops_home": {"t1": 50},
            "max_targets_per_wave": 3,
        }
        nearby_targets = [
            {"x": 10, "y": 21, "distance": 1.0, "population": 15, "last_loot_ratio": 1.0, "losses_recorded": 0},
            {"x": 20, "y": 30, "distance": 12.0, "population": 50, "last_loot_ratio": 0.8, "losses_recorded": 0},
            {"x": 10, "y": 25, "distance": 4.0, "population": 30, "last_loot_ratio": 0.9, "losses_recorded": 1},
        ]

        recs = heuristic_evaluate_farming_targets(account_status, nearby_targets)
        self.assertLessEqual(len(recs), 2)
        self.assertEqual(recs[0]["x"], 10)
        self.assertEqual(recs[0]["y"], 21)
        self.assertIn("t1", recs[0]["troops"])

    def test_ai_scout_brain_target_management_and_blacklist(self):
        state_file = Path(self.test_dir) / "scout_test.json"
        brain = AIScoutBrain(account_id="test_acc", state_path=state_file)

        # 1. Aggiorna target da mappa
        tiles = [
            {"x": 15, "y": 20, "village_name": "Grigio 1", "population": 25, "is_oasis": False},
            {"x": 18, "y": 22, "village_name": "Oasi 1", "population": 0, "is_oasis": True},
        ]
        brain.update_targets_from_map(tiles, origin_x=10, origin_y=20)
        self.assertEqual(len(brain.targets), 2)

        # 2. Processa report con perdite su Grigio 1 -> scatta blacklist
        reports = [
            {"x": 15, "y": 20, "target_name": "Grigio 1", "loot_ratio": 0.0, "losses": 2, "defenders": 5},
        ]
        brain.process_combat_reports(reports, blacklist_hours=1.0)
        target = brain.targets.get("15_20")
        self.assertIsNotNone(target)
        self.assertTrue(target.is_blacklisted)
        self.assertEqual(target.losses_recorded, 2)

        # Candidati non devono includere il target blacklistato
        candidates = brain.get_candidate_targets(origin_x=10, origin_y=20, max_distance=20.0)
        self.assertTrue(all(not (c["x"] == 15 and c["y"] == 20) for c in candidates))


if __name__ == "__main__":
    unittest.main()

