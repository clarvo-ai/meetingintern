"""Company spelling is changed only from trusted, source-supported entries."""

import unittest

from company_reference import normalize_report_names, parse_company_references


class CompanyReferenceTests(unittest.TestCase):
    def test_source_aliases_are_corrected_without_touching_evidence(self):
        references = parse_company_references([
            {"name": "Examplewise", "aliases": ["Examplevise"],
             "domains": ["examplewise.fi"]},
        ])
        evidence = {"calendar_match": "not_found", "speakers": ["A"]}
        result = normalize_report_names(
            {"headline": "Examplevise trial", "overview": ["Examplevise asked about search."],
             "meeting_evidence": evidence},
            "Examplevise joined the trial.", evidence, references,
        )
        self.assertEqual(result["headline"], "Examplewise trial")
        self.assertEqual(result["overview"], ["Examplewise asked about search."])
        self.assertNotIn("meeting_evidence", result)
        self.assertEqual(evidence["speakers"], ["A"])

    def test_model_only_name_is_not_treated_as_verified(self):
        references = parse_company_references([
            {"name": "Examplewise", "aliases": ["Examplevise"], "domains": []},
        ])
        result = normalize_report_names({"headline": "Examplevise trial"},
                                        "No company was named.", {}, references)
        self.assertEqual(result["headline"], "Examplevise trial")
        self.assertEqual(result["verified_companies"], [])

    def test_exact_calendar_domain_supports_company_context(self):
        references = parse_company_references([
            {"name": "Brightly", "aliases": [], "domains": ["brightlyworks.com"]},
        ])
        evidence = {"calendar_match": "matched", "calendar_event": {"invitees": [
            {"email": "person@brightlyworks.com"}]}}
        result = normalize_report_names({"headline": "Demo"}, "No company was spoken.",
                                        evidence, references)
        self.assertEqual(result["verified_companies"][0]["name"], "Brightly")
        self.assertEqual(result["verified_companies"][0]["matched_domains"],
                         ["brightlyworks.com"])

    def test_conflicting_aliases_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            parse_company_references([
                {"name": "First", "aliases": ["Same"], "domains": []},
                {"name": "Second", "aliases": ["Same"], "domains": []},
            ])


if __name__ == "__main__":
    unittest.main()
