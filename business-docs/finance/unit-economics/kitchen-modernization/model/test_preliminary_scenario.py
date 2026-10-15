import copy
import json
import unittest
from pathlib import Path

from preliminary_scenario import calculate, load_run


ROOT = Path(__file__).resolve().parent


class PreliminaryScenarioTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "inputs-preliminary-2026-10-15.json").read_text())
        self.base = json.loads((ROOT / self.config["sources"]["baseline_inputs"]).read_text())
        self.reconciliation = json.loads((ROOT / self.config["sources"]["baseline_reconciliation"]).read_text())

    def run_model(self, config=None):
        return calculate(config or self.config, self.base, self.reconciliation)

    def test_working_scenario_manual_check_and_no_payroll_conversion(self):
        result = self.run_model()
        working = next(row for row in result["profiles"] if row["id"] == "working")
        # Hand calculation: 7659 * .8 * ((32-12)+(11-6))/3600 - 31*10/60.
        self.assertAlmostEqual(working["net_person_hours_released"], 37.38333333333333)
        self.assertEqual(working["payroll_savings_rub_same_schedule"], 0)
        self.assertIsNone(working["whole_roll_elapsed_seconds_after"])
        self.assertIsNone(working["kitchen_peak_capacity_rolls_hour"])

    def test_new_measurement_flows_without_editing_formula(self):
        first = self.run_model()["profiles"][1]
        changed = copy.deepcopy(self.config)
        changed["profiles"]["working"]["mat_active_seconds"]["value"] = 11
        second = self.run_model(changed)["profiles"][1]
        self.assertAlmostEqual(second["net_person_hours_released"] - first["net_person_hours_released"], 1.702)
        self.assertEqual(second["payroll_savings_rub_same_schedule"], 0)

    def test_no_eligible_rolls_still_costs_extra_service(self):
        changed = copy.deepcopy(self.config)
        changed["parameters"]["mat_applicable_share"]["value"] = 0
        changed["parameters"]["cut_applicable_share"]["value"] = 0
        for row in self.run_model(changed)["profiles"]:
            self.assertAlmostEqual(row["net_person_hours_released"], -31 / 6)

    def test_unknown_costs_not_zero_and_budget_not_double_counted(self):
        result = self.run_model()["financial_handoff"]
        self.assertIsNone(result["profit_same_orders_from_components_rub"])
        self.assertEqual(len(result["missing_equipment_cost_inputs"]), 5)
        self.assertAlmostEqual(result["budget_sensitivities"][1]["profit_same_orders_same_schedule_rub"], -126175.67)
        changed = copy.deepcopy(self.config)
        values = dict(incremental_kwh_month=100, maintenance_rub_month=500,
                      consumables_rub_month=200, capex_landed_rub=120000, useful_life_months=60)
        for key, value in values.items():
            changed["financial"]["equipment_cost_inputs"][key]["value"] = value
        result = self.run_model(changed)["financial_handoff"]
        self.assertAlmostEqual(result["equipment_cost_rub_month_from_components"], 4003)
        self.assertAlmostEqual(result["profit_same_orders_from_components_rub"], -120178.67)
        self.assertAlmostEqual(result["budget_sensitivities"][1]["profit_same_orders_same_schedule_rub"], -126175.67)

    def test_reject_invalid_share_unknown_timing_and_period_mismatch(self):
        for key, value in (("mat_applicable_share", 1.1), ("before_mat_active_seconds", None)):
            changed = copy.deepcopy(self.config)
            changed["parameters"][key]["value"] = value
            with self.assertRaises(ValueError):
                self.run_model(changed)
        self.reconciliation["period"] = "2026-09"
        with self.assertRaises(ValueError):
            self.run_model()

    def test_real_input_loading_includes_source_hashes(self):
        result = load_run(ROOT / "inputs-preliminary-2026-10-15.json")
        self.assertEqual(len(result["source_manifest"]), 3)
        self.assertTrue(all(len(row["sha256"]) == 64 for row in result["source_manifest"]))

    def test_peak_aggregates_shared_resource_and_keeps_other_operations(self):
        working = self.run_model()["profiles"][1]["manual_peak_scenario"]
        self.assertEqual(working["before"]["load_person_seconds_per_roll"]["forming"], 122)
        self.assertAlmostEqual(working["after"]["load_person_seconds_per_roll"]["forming"], 106)
        self.assertAlmostEqual(working["after"]["load_person_seconds_per_roll"]["dispatch"], 62)
        self.assertAlmostEqual(working["before"]["partial_manual_limit_rolls_hour"], 46.3636363636)
        self.assertAlmostEqual(working["after"]["partial_manual_limit_rolls_hour"], 49.3548387097)
        self.assertEqual(working["after"]["limiting_manual_resource"], "dispatch")

    def test_contribution_volume_tradeoff_is_not_percentage_point_change(self):
        rows = self.run_model()["financial_handoff"]["contribution_volume_tradeoffs"]
        self.assertAlmostEqual(rows[0]["order_growth_fraction"], 1 / 9)
        self.assertAlmostEqual(rows[2]["order_growth_fraction"], 0.25)
        self.assertEqual(rows[2]["required_orders_month_rounded_up"], 2274)


if __name__ == "__main__":
    unittest.main()
