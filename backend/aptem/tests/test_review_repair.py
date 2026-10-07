from django.test import SimpleTestCase
from aptem.services.review_storage import validate_replacement
from aptem.services.review_repair import compare_inventory


class RepairTests(SimpleTestCase):
    def test_identical_legacy_duplicate_can_be_replaced_by_correct_inventory(self):
        section = {'section_name': 'Opening', 'raw_text': 'Preserved answer'}
        validate_replacement([section, dict(section)],
                             [{'section_name': 'Previous Meeting Summary', 'raw_text': 'Summary'}, section])

    def test_distinct_duplicate_answers_are_not_discarded(self):
        with self.assertRaisesRegex(ValueError, 'Ambiguous previous'):
            validate_replacement([{'section_name': 'Opening', 'raw_text': 'Answer A'},
                                  {'section_name': 'Opening', 'raw_text': 'Answer B'}],
                                 [{'section_name': 'Opening', 'raw_text': 'Answer A'}])

    def test_reordering_preserves_section_identity(self):
        sections = [{'section_name': 'A', 'raw_text': 'Answer A'},
                    {'section_name': 'B', 'raw_text': 'Answer B'}]
        validate_replacement(sections, list(reversed(sections)))

    def test_repair_cannot_erase_previous_content(self):
        old = [{'section_name': 'Answers', 'raw_text': 'Existing response'}]
        for new in ([], [{'section_name': 'Answers', 'raw_text': ''}],
                    [{'section_name': 'Different', 'raw_text': 'Response'}]):
            with self.subTest(new=new), self.assertRaises(ValueError):
                validate_replacement(old, new)

    def test_repair_can_populate_previous_empty_section(self):
        validate_replacement([{'section_name': 'Answers', 'raw_text': ''}],
                             [{'section_name': 'Answers', 'raw_text': 'Recovered response'}])

    def test_matching_counts_do_not_hide_wrong_ids(self):
        result = compare_inventory({'1': 'Scheduled'}, {'2': {'status': 'Scheduled'}})
        self.assertEqual(result['missing_ids'], ['1'])
        self.assertEqual(result['extra_ids'], ['2'])

    def test_in_progress_requires_loaded_content(self):
        expected = {'1': 'In Progress'}
        stored = {'1': {'status': 'In Progress', 'extraction_status': 'partial', 'sections': 0, 'capture_version': 0}}
        self.assertEqual(compare_inventory(expected, stored)['invalid_ids'], ['1'])
        stored['1'].update(extraction_status='success', sections=10, capture_version=3)
        self.assertFalse(any(compare_inventory(expected, stored).values()))
