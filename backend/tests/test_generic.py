import unittest

from fetchers.generic import _extract_fields


class GenericFieldExtractionTests(unittest.TestCase):
    def test_progress_field_extracts_used_and_total_values(self):
        payload = {
            "this_month_usage": 1,
            "searches_per_month": 250,
        }
        fields = [{
            "label": "Monthly searches",
            "path": "this_month_usage",
            "total_path": "searches_per_month",
            "type": "progress",
        }]

        self.assertEqual(_extract_fields(payload, fields), [{
            "label": "Monthly searches",
            "type": "progress",
            "symbol": "",
            "value": 1,
            "total": 250,
        }])

    def test_all_progress_variants_extract_total_values(self):
        payload = {"value": 75, "limit": 100}

        for field_type in (
            "progress",
            "progress_remaining",
            "progress_bar",
            "progress_bar_remaining",
        ):
            with self.subTest(field_type=field_type):
                fields = [{
                    "label": "Quota",
                    "path": "value",
                    "total_path": "limit",
                    "type": field_type,
                }]

                self.assertEqual(_extract_fields(payload, fields), [{
                    "label": "Quota",
                    "type": field_type,
                    "symbol": "",
                    "value": 75,
                    "total": 100,
                }])

    def test_existing_field_types_are_unchanged(self):
        fields = [{"label": "Plan", "path": "plan.name", "type": "text"}]

        self.assertEqual(_extract_fields({"plan": {"name": "Free"}}, fields), [{
            "label": "Plan",
            "type": "text",
            "symbol": "",
            "value": "Free",
        }])


if __name__ == "__main__":
    unittest.main()
