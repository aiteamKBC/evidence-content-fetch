from unittest.mock import patch
from django.test import SimpleTestCase
from aptem.services.review_storage import save_review


class ReviewStorageValidationTests(SimpleTestCase):
    def test_non_active_review_never_reaches_database(self):
        for status in ('Completed', 'Withdrawn', 'Paused', '', None, 'active'):
            with self.subTest(status=status), patch('aptem.services.review_storage.destination_learner') as lookup:
                with self.assertRaises(ValueError):
                    save_review({'programme_status': status, 'status': 'Completed'})
                lookup.assert_not_called()

    def test_unfinished_review_never_reaches_database(self):
        with self.assertRaises(ValueError):
            save_review({'programme_status': 'Active', 'status': 'Scheduled'})

    def test_section_without_name_rejected_before_database_write(self):
        with self.assertRaises(ValueError):
            save_review({'programme_status':'Active', 'status':'Completed',
                         'sections':[{'section_name':'', 'raw_text':''}]})
