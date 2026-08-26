import unittest

from buildingconnected_playwright import (
    BidRow,
    detail_page_matches_project,
    likely_match,
    row_matches_project,
)


TARGET = "Crossroads of Gallatin - Gallatin, TN"


class _HeadingPage:
    def __init__(self, heading: str, *, panel_open: bool = False):
        self.heading = heading
        self.panel_open = panel_open
        self.url = (
            "https://app.buildingconnected.com/opportunities/pipeline"
            if panel_open
            else "https://app.buildingconnected.com/opportunities/0123456789abcdef/overview"
        )

    def evaluate(self, script):
        if "labels.some" in script:
            return self.panel_open
        return [self.heading]

    def wait_for_timeout(self, _milliseconds):
        return None


class BuildingConnectedProjectMatchingTests(unittest.TestCase):
    def test_truncated_target_row_still_matches(self):
        row = BidRow(
            text="Crossroads of Gallatin - Gallatin,... Fences & Gates 9/11/2026 Bidding Decline",
            tag="DIV",
            role="",
            href="",
            x=180,
            y=500,
        )
        self.assertTrue(row_matches_project(TARGET, row))

    def test_shorter_similarly_named_row_does_not_match(self):
        row = BidRow(
            text="Crossroads of Gallatin Fences & Gates 9/11/2026 Bidding Decline",
            tag="DIV",
            role="",
            href="",
            x=180,
            y=500,
        )
        self.assertFalse(row_matches_project(TARGET, row))

    def test_repeated_target_word_must_be_represented(self):
        self.assertFalse(likely_match(TARGET, "Crossroads of Gallatin Fences & Gates"))
        self.assertTrue(likely_match(TARGET, "Crossroads of Gallatin - Gallatin,..."))

    def test_earlier_truncation_still_matches_by_prefix(self):
        self.assertTrue(likely_match("Tower Renovation - Louisville, KY", "Tower Renovation..."))

    def test_visible_wrong_heading_is_rejected(self):
        page = _HeadingPage("Brooks Bartrum Inpatient Rehabilitation Hospital 48 Bed Addition")
        self.assertFalse(detail_page_matches_project(page, TARGET, timeout_ms=0))

    def test_visible_target_heading_is_accepted(self):
        page = _HeadingPage("Crossroads of Gallatin - Gallatin, TN")
        self.assertTrue(detail_page_matches_project(page, TARGET, timeout_ms=0))

    def test_visible_target_panel_is_accepted_without_url_change(self):
        page = _HeadingPage("Crossroads of Gallatin - Gallatin, TN", panel_open=True)
        self.assertTrue(detail_page_matches_project(page, TARGET, timeout_ms=0))


if __name__ == "__main__":
    unittest.main()
