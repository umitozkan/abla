import calendar
import unittest

from app.calculations import calculate_day, scenario


def day(**changes):
    result = {
        "date": "2026-09-24", "menu": "Mercimek, pilav", "requested": 15,
        "made": 15, "delivered": 15, "revenue_cents": 400000,
        "prior_material_cents": 0, "carry_material_cents": 0,
        "material_confirmed": True, "other_confirmed": True,
        "time_confirmed": True, "energy_confirmed": True,
        "labor_source": "tasks", "shopping_minutes": 0,
        "preparation_minutes": 0, "delivery_minutes": 0, "cleaning_minutes": 0,
    }
    result.update(changes)
    return result


def timer(kind, start="10:00:00", end="11:00:00"):
    return {
        "kind": kind, "started_at": f"2026-09-24T{start}+03:00",
        "ended_at": None if end is None else f"2026-09-24T{end}+03:00",
    }


def calculate(record=None, expenses=None, timers=None, settings=None, fixed=None):
    return calculate_day(
        record or day(), expenses or [], timers or [],
        {"labor_hourly_cents": 10000, "stove_hourly_cents": 1000, "oven_hourly_cents": 2000}
        if settings is None else settings,
        fixed or [],
    )


class CalculationTests(unittest.TestCase):
    def test_requested_example_is_before_labor_and_other_costs(self):
        result = calculate(day(preparation_minutes=120), [
            {"category": "material", "amount_cents": 200000},
            {"category": "other", "amount_cents": 10000},
        ])
        self.assertEqual(result["before_labor_other_cents"], 200000)
        self.assertEqual(result["before_labor_cents"], 190000)
        self.assertEqual(result["result_cents"], 170000)
        self.assertTrue(result["estimate"])
        self.assertTrue(result["complete"])

    def test_inventory_adjustment_and_equipment_separation(self):
        result = calculate(day(prior_material_cents=30000, carry_material_cents=50000), [
            {"category": "material", "amount_cents": 200000},
            {"category": "equipment", "amount_cents": 700000},
        ])
        self.assertEqual(result["material_cents"], 180000)
        self.assertEqual(result["equipment_cents"], 700000)
        self.assertEqual(result["result_cents"], 220000)

    def test_impossible_inventory_does_not_inflate_profit(self):
        result = calculate(day(carry_material_cents=200000))
        self.assertIsNone(result["material_cents"])
        self.assertIsNone(result["result_cents"])
        self.assertIsNone(result["total_cost_cents"])
        self.assertFalse(result["complete"])

    def test_labor_clock_and_tasks_are_never_added_together(self):
        record = day(labor_source="clock", preparation_minutes=120)
        self.assertEqual(calculate(record, timers=[timer("work")])["labor_minutes"], 60)
        record["labor_source"] = "tasks"
        result = calculate(record, timers=[timer("work")])
        self.assertEqual(result["labor_minutes"], 120)
        self.assertEqual(result["labor_cents"], 20000)

    def test_all_four_task_durations_count(self):
        result = calculate(day(shopping_minutes=15, preparation_minutes=30, delivery_minutes=10, cleaning_minutes=5))
        self.assertEqual(result["labor_minutes"], 60)

    def test_fractional_minutes_round_money_half_up(self):
        result = calculate(day(labor_source="clock"), timers=[timer("work", end="10:30:00")], settings={"labor_hourly_cents": 101})
        self.assertEqual(result["labor_cents"], 51)
        result = calculate(day(labor_source="clock"), timers=[timer("work", end="10:00:30")])
        self.assertEqual(result["labor_minutes"], 0.5)
        self.assertEqual(result["labor_cents"], 83)

    def test_missing_rates_and_open_timer_are_disclosed(self):
        result = calculate(day(labor_source="clock"), timers=[timer("work", end=None), timer("stove"), timer("oven")], settings={})
        self.assertFalse(result["complete"])
        self.assertEqual(result["labor_minutes"], 0)
        self.assertEqual(result["energy_cents"], 0)
        self.assertTrue(any("sayacı açık" in item for item in result["missing"]))
        self.assertTrue(any("Saatlik emek" in item for item in result["missing"]))
        self.assertTrue(any("Ocak saatlik" in item for item in result["missing"]))
        self.assertTrue(any("Fırın saatlik" in item for item in result["missing"]))

    def test_energy_is_device_time_estimate(self):
        result = calculate(timers=[timer("stove", end="11:30:00"), timer("oven", end="10:30:00")])
        self.assertEqual(result["stove_minutes"], 90)
        self.assertEqual(result["oven_minutes"], 30)
        self.assertEqual(result["energy_cents"], 2500)
        self.assertTrue(result["estimate"])

    def test_fixed_cost_allocation_has_exact_monthly_total(self):
        for month in ["2026-02", "2024-02", "2026-09", "2026-12"]:
            year, month_number = map(int, month.split("-"))
            days = calendar.monthrange(year, month_number)[1]
            values = [calculate(day(date=f"{month}-{number:02}"), fixed=[
                {"month": month, "name": "Kira", "amount_cents": 10001},
                {"month": "2000-01", "name": "Eski gider", "amount_cents": 999999},
            ])["fixed_cents"] for number in range(1, days + 1)]
            self.assertEqual(sum(values), 10001)
            self.assertLessEqual(max(values) - min(values), 1)
            self.assertEqual(values, sorted(values, reverse=True))

    def test_unknown_revenue_does_not_become_zero_sales(self):
        result = calculate(day(revenue_cents=None))
        for key in ["revenue_cents", "before_labor_other_cents", "before_labor_cents", "result_cents", "margin_percent", "price_per_portion_cents"]:
            self.assertIsNone(result[key])
        self.assertFalse(result["complete"])

    def test_unknown_material_and_task_inputs_are_disclosed(self):
        result = calculate(day(prior_material_cents=None, carry_material_cents=None, shopping_minutes=None))
        self.assertFalse(result["complete"])
        self.assertTrue(any("Önceki günlerden" in item for item in result["missing"]))
        self.assertTrue(any("Sonraya kalan" in item for item in result["missing"]))
        self.assertTrue(any("Alışveriş süresi" in item for item in result["missing"]))

    def test_portion_metrics_use_delivered_not_requested(self):
        result = calculate(day(requested=20, made=20, delivered=4), [{"category": "material", "amount_cents": 200000}])
        self.assertEqual(result["price_per_portion_cents"], 100000)
        self.assertEqual(result["cost_per_portion_cents"], 50000)
        self.assertEqual(result["result_per_portion_cents"], 50000)
        self.assertEqual(result["margin_percent"], 50)

    def test_zero_sales_and_portions_have_no_division_by_zero(self):
        result = calculate(day(revenue_cents=0, requested=0, made=0, delivered=0))
        self.assertIsNone(result["margin_percent"])
        self.assertIsNone(result["cost_per_portion_cents"])
        self.assertFalse(result["complete"])

    def test_overlapping_timer_minutes_are_counted_once_and_flagged(self):
        result = calculate(day(labor_source="clock"), timers=[timer("work"), timer("work", "10:30:00", "11:30:00")])
        self.assertEqual(result["labor_minutes"], 90)
        self.assertTrue(any("çakışıyor" in item for item in result["missing"]))

    def test_invalid_timer_is_not_counted(self):
        for invalid_timer in [timer("stove", "12:00:00", "11:00:00"), {"kind": "stove", "started_at": "oops", "ended_at": "oops"}]:
            result = calculate(timers=[invalid_timer])
            self.assertEqual(result["energy_cents"], 0)
            self.assertFalse(result["complete"])

    def test_multiple_expenses_and_half_up_portion_rounding(self):
        result = calculate(day(revenue_cents=101, delivered=2), [
            {"category": "material", "amount_cents": 25},
            {"category": "material", "amount_cents": 26},
        ])
        self.assertEqual(result["purchases_cents"], 51)
        self.assertEqual(result["price_per_portion_cents"], 51)
        self.assertEqual(result["cost_per_portion_cents"], 26)

    def test_negative_or_fractional_money_is_not_silently_truncated(self):
        for value in [-1, "NaN", "1.5", True]:
            result = calculate(day(revenue_cents=value))
            self.assertIsNone(result["revenue_cents"])
            self.assertFalse(result["complete"])

    def test_scenario_keeps_baseline_costs_and_discloses_assumption(self):
        original = calculate(expenses=[{"category": "material", "amount_cents": 200000}])
        result = scenario(original, 20, 30000)
        self.assertEqual(result["revenue_cents"], 600000)
        self.assertEqual(result["total_cost_cents"], 200000)
        self.assertEqual(result["result_cents"], 400000)
        self.assertEqual(result["margin_percent"], 66.67)
        self.assertIn("aynı kalır", result["assumption"])
        self.assertTrue(result["estimate"])
        self.assertEqual(original["revenue_cents"], 400000)

    def test_scenario_preserves_unknown_costs_and_rejects_bad_inputs(self):
        incomplete = calculate(day(carry_material_cents=999999))
        self.assertIsNone(scenario(incomplete, 10, 10000)["result_cents"])
        for portions, price in [(0, 1), (-1, 1), (1, -1), (1.5, 1)]:
            with self.assertRaises(ValueError):
                scenario(calculate(), portions, price)


if __name__ == "__main__":
    unittest.main()
