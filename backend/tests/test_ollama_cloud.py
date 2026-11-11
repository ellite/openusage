import unittest

from fetchers.ollama_cloud import parse_usage_report_html, parse_settings_html


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


class OllamaCloudDashboardTests(unittest.TestCase):
    """ollama.com moved the usage card from /settings to /dashboard and made
    /settings a JS-loaded usage report; these use the markup of that redesign."""

    def test_dashboard_markup(self):
        html = """
        <div id="dashboard-meter" class="contents">
          <div class="mb-1 text-sm">Usage credits<span id="dashboard-plan" class="text-xs">Pay as you go</span></div>
          <p class="text-2xl"><span id="extra-usage-balance">$12.50</span></p>
        </div>
        <div class="text-sm">Used this month</div>
        <p id="dashboard-usage" class="mt-3 text-2xl tabular-nums">$3.41</p>
        """
        self.assertEqual(
            parse_settings_html(html),
            {"balance": 12.5, "plan": "Pay As You Go", "cost_month": 3.41},
        )

    def test_usage_report_requests(self):
        html = """
        <dl><div><dt class="text-ui">Total cost</dt><dd class="x">$12.34</dd></div>
            <div><dt class="text-ui">Requests</dt><dd class="x">1,204</dd></div></dl>
        """
        self.assertEqual(parse_usage_report_html(html), {"requests_month": 1204})

    def test_usage_report_without_stats(self):
        self.assertEqual(parse_usage_report_html("<p>Loading usage…</p>"), {})

    def test_unrelated_local_time_is_not_a_reset(self):
        # A recent-requests table also uses `local-time` elements; without a
        # usage meter that timestamp must not be reported as a reset time.
        html = '<td class="h-10 local-time text-muted" data-time="2026-10-04T23:02:36Z">1mo</td>'
        self.assertNotIn("resets_at", parse_settings_html(html))
