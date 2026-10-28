"""Calendar and Meet identity evidence remains visibly qualified in reports."""

import unittest

from meeting_report_context import company_line, combined_people, meeting_time, people_line


ROSTER = {"robert@clarvo.ai": "Robert Holmberg", "juha@clarvo.ai": "Juha Lehto"}


class ReportContextTests(unittest.TestCase):
    def test_joins_and_invites_combine_without_upgrading_anonymous_identity(self):
        evidence = {"calendar_match": "matched", "calendar_event": {"invitees": [
            {"email": "jarkko.dahlstrom@brightlyworks.com", "response_status": "accepted"},
            {"email": "luukas.lavanti@brightlyworks.com", "response_status": "needsAction"},
            {"email": "robert@clarvo.ai", "response_status": "accepted"},
            {"email": "absent@brightlyworks.com", "response_status": "accepted"},
        ]}, "confirmed_meet_participants": [
            {"display_name": "Jarkko Dahlström", "identity_kind": "signed_in"},
            {"display_name": "Luukas", "identity_kind": "anonymous"},
            {"display_name": "Robert Holmberg", "identity_kind": "signed_in"},
        ]}
        people = combined_people(evidence, ROSTER)
        self.assertEqual(len(people), 4)
        self.assertEqual(people[1]["name"], "Luukas Lavanti")
        self.assertTrue(people[1]["likely_identity"])
        self.assertFalse(people[3]["joined"])
        line = people_line(evidence, ROSTER)
        self.assertIn("Luukas Lavanti <luukas.lavanti@brightlyworks.com> "
                      "(joined as Luukas; invitee match unverified)", line)
        self.assertIn("absent@brightlyworks.com (invited only; accepted)", line)
        self.assertEqual(line.count("robert@clarvo.ai"), 1)

    def test_ambiguous_first_name_does_not_gain_an_email(self):
        evidence = {"calendar_match": "matched", "calendar_event": {"invitees": [
            {"email": "luukas.one@example.com"}, {"email": "luukas.two@example.com"}]},
            "confirmed_meet_participants": [
                {"display_name": "Luukas", "identity_kind": "anonymous"}]}
        people = combined_people(evidence, {})
        self.assertIsNone(people[0]["email"])
        self.assertEqual(len(people), 3)

    def test_ad_hoc_joiners_use_roster_and_source_time(self):
        evidence = {"calendar_match": "not_found", "source_artifacts": [
            {"meeting_at": "2026-10-26T10:31:00+00:00"}],
            "confirmed_meet_participants": [
                {"display_name": "Juha Lehto", "identity_kind": "signed_in"}]}
        self.assertEqual(meeting_time(evidence), "2026/10/26 12:31 EET")
        self.assertIn("Juha Lehto <juha@clarvo.ai> (joined)", people_line(evidence, ROSTER))

    def test_calendar_company_uses_domain_and_verified_name(self):
        evidence = {"calendar_match": "matched", "calendar_event": {"invitees": [
            {"email": "person@brightlyworks.com"}, {"email": "robert@clarvo.ai"}],
            "start": "2026-10-27T10:00:00+02:00"}}
        companies = [{"name": "Brightly", "matched_domains": ["brightlyworks.com"]}]
        self.assertEqual(company_line(evidence, companies, ROSTER),
                         "🏢 Calendar company: Brightly (brightlyworks.com)")
        self.assertEqual(meeting_time(evidence), "2026/10/27 10:00 EET")


if __name__ == "__main__":
    unittest.main()
