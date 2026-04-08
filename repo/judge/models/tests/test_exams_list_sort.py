from datetime import date
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase

from judge.views.exams import ExamsListView


class ExamsListDateSortTestCase(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _sort_items(self, query, items):
        request = self.factory.get('/exams-list/', query)
        view = ExamsListView()
        view.request = request
        return view._sort_items([dict(item) for item in items])

    @patch('judge.views.exams.timezone.localdate', return_value=date(2026, 4, 8))
    def test_date_sort_near_to_far(self, _mock_today):
        items = [
            {'name': 'No date', 'exam_date': ''},
            {'name': 'Near newer', 'exam_date': '2026-04-10'},
            {'name': 'Near older', 'exam_date': '2026-04-06'},
            {'name': 'Nearest', 'exam_date': '2026-04-07'},
            {'name': 'Far', 'exam_date': '2026-04-20'},
        ]

        sorted_items = self._sort_items({'date_sort': 'near_to_far'}, items)
        self.assertEqual(
            [item['name'] for item in sorted_items],
            ['Nearest', 'Near newer', 'Near older', 'Far', 'No date'],
        )

    @patch('judge.views.exams.timezone.localdate', return_value=date(2026, 4, 8))
    def test_date_sort_far_to_near(self, _mock_today):
        items = [
            {'name': 'No date', 'exam_date': None},
            {'name': 'Near newer', 'exam_date': '2026-04-10'},
            {'name': 'Near older', 'exam_date': '2026-04-06'},
            {'name': 'Nearest', 'exam_date': '2026-04-07'},
            {'name': 'Far', 'exam_date': '2026-04-20'},
        ]

        sorted_items = self._sort_items({'date_sort': 'far_to_near'}, items)
        self.assertEqual(
            [item['name'] for item in sorted_items],
            ['Far', 'Near newer', 'Near older', 'Nearest', 'No date'],
        )

    def test_year_sort_kept_for_legacy_queries(self):
        items = [
            {'name': 'No year', 'year': None},
            {'name': '2024 item', 'year': 2024},
            {'name': '2022 item', 'year': 2022},
        ]

        sorted_items = self._sort_items({'date_sort': 'invalid', 'year_sort': 'asc'}, items)
        self.assertEqual(
            [item['name'] for item in sorted_items],
            ['2022 item', '2024 item', 'No year'],
        )
