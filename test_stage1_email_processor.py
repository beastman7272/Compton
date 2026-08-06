import unittest

from stage1_email_processor import (
    extract_colon_project_name,
    extract_project_details,
)


class Stage1ProjectExtractionTests(unittest.TestCase):
    def test_gustavo_one_word_project_ignores_markdown_bid_due(self):
        body = """
        *Hali Maye* from *Buffalo Construction, Inc.* has invited you to bid on
        *Gustavo's*: Roofing
        View this RFP
        Project Details
        *Location: *Louisville, KY
        *Bid Due: *August 14, 2026
        """

        project, _ = extract_project_details(
            body,
            "Fwd: Bid Invite: Gustavo's Project",
        )

        self.assertEqual(project, "Gustavo's")

    def test_wrapped_project_is_joined_and_scope_is_discarded(self):
        body = """
        Estimating Department from JC Curtis Construction Company, LLC has invited you
        to bid on
        *Fort Oglethorpe (Georgia) Warehouse
        Development - Remodel/New Construction*: Siding & Metal Composite
        Panels
        View this RFP
        """

        project, _ = extract_project_details(
            body,
            "Fwd: Bid Invite: Fort Oglethorpe (Georgia) Warehouse Developme...",
        )

        self.assertEqual(
            project,
            "Fort Oglethorpe (Georgia) Warehouse Development - "
            "Remodel/New Construction",
        )

    def test_scope_is_not_part_of_project_name(self):
        body = """
        andrew weddle from Buffalo Construction, Inc. has invited you to bid on
        *Wawa Nicholasville KY #7620 Rebid*: Membrane Roofing
        View this RFP
        """

        project, _ = extract_project_details(
            body,
            "Fwd: Bid Invite: Wawa Nicholasville KY #7620 Rebid Project",
        )

        self.assertEqual(
            project,
            "Wawa Nicholasville KY #7620 Rebid",
        )

    def test_complete_subject_is_used_when_invitation_anchor_is_missing(self):
        project, _ = extract_project_details(
            "The forwarded email formatting was damaged.",
            "Fwd: Bid Invite: Jimmy John's & Liquor Store Project",
        )

        self.assertEqual(project, "Jimmy John's & Liquor Store")

    def test_valid_body_wins_when_subject_disagrees(self):
        body = """
        A Person from A Contractor has invited you to bid on
        *Actual Body Project*: Roofing
        View this RFP
        """

        project, _ = extract_project_details(
            body,
            "Fwd: Bid Invite: Different Subject Project",
        )

        self.assertEqual(project, "Actual Body Project")

    def test_different_scopes_resolve_to_same_project(self):
        roofing, _ = extract_project_details(
            "A Person from A Contractor has invited you to bid on "
            "*Gustavo's*: Roofing View this RFP",
            "Fwd: Bid Invite: Gustavo's Project",
        )
        caulking, _ = extract_project_details(
            "A Person from A Contractor has invited you to bid on "
            "*Gustavo's*: Caulking View this RFP",
            "Fwd: Bid Invite: Gustavo's Project",
        )

        self.assertEqual(roofing, "Gustavo's")
        self.assertEqual(caulking, "Gustavo's")

    def test_generic_colon_fallback_rejects_formatted_metadata(self):
        lines = [
            "*Location: *Louisville, KY",
            "*Bid Due: *August 14, 2026",
            "*Lead: Hali Maye*",
        ]

        self.assertEqual(extract_colon_project_name(lines), "Unknown")


if __name__ == "__main__":
    unittest.main()
