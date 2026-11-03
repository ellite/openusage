import unittest

from thresholds import extract_history_metrics


class HistoryMetricsTests(unittest.TestCase):
    def test_claude_percentages(self):
        m = extract_history_metrics(
            "claude", {"five_hour": {"utilization": 40}, "seven_day": {"utilization": 12.5}}
        )
        self.assertEqual(m["session"]["value"], 40)
        self.assertEqual(m["weekly"]["unit"], "%")

    def test_deepseek_balance_prefers_usd(self):
        m = extract_history_metrics(
            "deepseek",
            {"balance_infos": [{"currency": "CNY", "total_balance": "9"}, {"currency": "USD", "total_balance": "3.50"}]},
        )
        self.assertEqual(m["balance"]["value"], 3.5)

    def test_openai_spend_and_balance(self):
        m = extract_history_metrics(
            "openai", {"currency": "usd", "spend_this_month": 4.2, "credit_balance": {"total_available": 10}}
        )
        self.assertEqual(m["spend"]["value"], 4.2)
        self.assertEqual(m["balance"]["unit"], "USD")

    def test_custom_has_nothing_untracked(self):
        data = {"fields": [{"label": "Bal", "type": "balance", "value": 5, "history": False}]}
        self.assertEqual(extract_history_metrics("custom", data), {})

    def test_custom_tracked_fields_use_their_type(self):
        data = {"fields": [
            {"label": "Bal", "type": "balance", "symbol": "$", "value": "1,234.5", "history": True},
            {"label": "Load", "type": "percentage_fraction", "value": 0.25, "history": True},
            {"label": "Quota", "type": "progress_remaining", "value": 30, "total": 100, "history": True},
            {"label": "Plan", "type": "plan", "value": "pro", "history": True},
            {"label": "Skip", "type": "number", "value": 3},
        ]}
        m = extract_history_metrics("custom", data)
        self.assertEqual(set(m), {"field:Bal", "field:Load", "field:Quota"})
        self.assertEqual((m["field:Bal"]["value"], m["field:Bal"]["unit"]), (1234.5, "$"))
        self.assertEqual((m["field:Load"]["value"], m["field:Load"]["unit"]), (25.0, "%"))
        self.assertEqual(m["field:Quota"]["value"], 70.0)


if __name__ == "__main__":
    unittest.main()
