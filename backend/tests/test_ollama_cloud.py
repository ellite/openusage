import unittest

from fetchers.ollama_cloud import parse_settings_html


class OllamaCloudParserTests(unittest.TestCase):
    def test_current_settings_markup(self):
        html = """
            <h2>Usage credits<span class="capitalize">free</span></h2>
            <span id="extra-usage-balance" class="tabular-nums">$1,234.50</span>
            <div aria-label="Free usage: 12.5%"></div>
            <div class="text-xs local-time" data-time="2026-11-08T12:00:09Z"></div>
        """

        self.assertEqual(parse_settings_html(html), {
            "balance": 1234.5,
            "plan": "Free",
            "percent_used": 12.5,
            "resets_at": "2026-11-08T12:00:09Z",
        })

    def test_legacy_percentage_markup(self):
        html = """
            <span id="extra-usage-balance">$4.20</span>
            <span>Free usage</span><span class="text-sm">37% used</span>
        """

        self.assertEqual(parse_settings_html(html), {
            "balance": 4.2,
            "percent_used": 37.0,
        })


if __name__ == "__main__":
    unittest.main()
