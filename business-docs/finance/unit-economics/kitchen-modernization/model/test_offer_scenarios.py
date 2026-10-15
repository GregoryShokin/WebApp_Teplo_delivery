import unittest
from offer_scenarios import Model


class OfferModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = Model()

    def test_full_working_base_reconciles_without_scenario_adjustments(self):
        m = self.m
        r = m.estimate(m.q0, m.check0, m.c[0] / m.r0)
        self.assertAlmostEqual(r['costs_rub'], 3398606.31, places=2)
        self.assertAlmostEqual(r['profit_rub'], -116175.67, places=2)
        self.assertTrue(all(abs(x) < .00001 for x in r['cost_deltas_rub'].values()))

    def test_daily_pool_threshold_is_on_whole_revenue(self):
        self.assertAlmostEqual(self.m.day_pool(140000) - self.m.day_pool(139999), 1400.035)
        self.assertAlmostEqual(self.m.day_pool(190000) - self.m.day_pool(189999), 1900.045)

    def test_food_cost_one_point_includes_losses_not_other_costs(self):
        m = self.m
        a = m.estimate(3000, 1800, .32)
        b = m.estimate(3000, 1800, .33)
        expected = 54000 * (1 + (m.c[1] + m.c[2]) / m.c[0])
        self.assertAlmostEqual(a['profit_rub'] - b['profit_rub'], expected)
        self.assertEqual(a['cost_deltas_rub']['business_tax'], b['cost_deltas_rub']['business_tax'])

    def test_price_policy_and_mixed_line_budget(self):
        m = self.m
        check, _ = m.offer(m.cfg['scenarios'][1])
        self.assertAlmostEqual(check, 1762.4967179769105)
        check, fc = m.offer(m.cfg['scenarios'][2])
        self.assertAlmostEqual(check, .7 * m.check0 + .3 * 1400)
        self.assertAlmostEqual(check * fc, .7 * m.c[0] / m.q0 + .3 * 490)
        self.assertLess(fc, .35)

    def test_extra_worker_does_not_duplicate_percentage_pool(self):
        m = self.m
        a = m.estimate(3000, 1800, .32)
        b = m.estimate(3000, 1800, .32, staff=100000)
        self.assertAlmostEqual(a['profit_rub'] - b['profit_rub'], 100000)
        self.assertEqual(a['cost_deltas_rub']['shared_daily_production_pool'], b['cost_deltas_rub']['shared_daily_production_pool'])

    def test_normalization_and_capacity_scope(self):
        m = self.m
        a = m.estimate(m.q0, m.check0, m.c[0] / m.r0, normalize=True)
        self.assertAlmostEqual(a['profit_rub'], -130869.42)
        load = m.load(m.q0, 2, 1)
        self.assertAlmostEqual(load['partial_manual_capacity_rolls_hour'], 49.354838709677416)
        self.assertFalse(load['whole_kitchen_verified'])


if __name__ == '__main__':
    unittest.main()
