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

    def test_auto_sort_by_exam_date_near_to_far(self):
        items = [
            {'name': 'No date', 'exam_date': ''},
            {'name': '2026-04-10', 'exam_date': '2026-04-10'},
            {'name': '2026-04-06', 'exam_date': '2026-04-06'},
            {'name': '2026-04-07', 'exam_date': '2026-04-07'},
            {'name': '2025-03-01', 'exam_date': '2025-03-01'},
        ]

        sorted_items = self._sort_items({}, items)
        self.assertEqual(
            [item['name'] for item in sorted_items],
            ['2026-04-10', '2026-04-07', '2026-04-06', '2025-03-01', 'No date'],
        )

    def test_auto_sort_ignores_legacy_sort_queries(self):
        items = [
            {'name': 'No date', 'exam_date': None},
            {'name': 'A', 'exam_date': '2026-04-10'},
            {'name': 'B', 'exam_date': '2026-04-06'},
            {'name': 'C', 'exam_date': '2026-04-07'},
        ]

        sorted_items = self._sort_items({'date_sort': 'far_to_near', 'year_sort': 'asc'}, items)
        self.assertEqual(
            [item['name'] for item in sorted_items],
            ['A', 'C', 'B', 'No date'],
        )
