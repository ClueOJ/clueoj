from django.test import TestCase

from judge.models import ExamTag, ExamTagProblemPoint, ExamUserProgress, Language, Submission
from judge.models.tests.util import CommonDataMixin, create_problem
from judge.tasks.exams import sync_exam_progress_for_user_problem


class ExamProgressTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.profile = cls.users['normal'].profile
        cls.language = Language.get_python3()

    def test_sync_exam_progress_uses_exam_points_and_best_submission(self):
        exam_tag = ExamTag.objects.create(slug='exam-progress-a', name='Exam Progress A', is_public=True)
        problem = create_problem(code='exam_progress_a', is_public=True, partial=True)
        problem.exam_tags.add(exam_tag)
        ExamTagProblemPoint.objects.filter(exam_tag=exam_tag, problem=problem).update(points=5)

        Submission.objects.create(
            user=self.profile,
            problem=problem,
            language=self.language,
            status='D',
            result='AC',
            case_points=4,
            case_total=10,
        )
        Submission.objects.create(
            user=self.profile,
            problem=problem,
            language=self.language,
            status='D',
            result='AC',
            case_points=9,
            case_total=10,
        )

        updated = sync_exam_progress_for_user_problem.run(user_id=self.profile.id, problem_id=problem.id)
        self.assertEqual(updated, 1)

        progress = ExamUserProgress.objects.get(user=self.profile, exam_tag=exam_tag)
        self.assertAlmostEqual(progress.earned_points, 4.5, places=3)
        self.assertAlmostEqual(progress.total_points, 5, places=3)
        self.assertAlmostEqual(progress.percent, 90.0, places=1)

    def test_sync_exam_progress_respects_non_partial_rule(self):
        exam_tag = ExamTag.objects.create(slug='exam-progress-b', name='Exam Progress B', is_public=True)
        problem = create_problem(code='exam_progress_b', is_public=True, partial=False)
        problem.exam_tags.add(exam_tag)
        ExamTagProblemPoint.objects.filter(exam_tag=exam_tag, problem=problem).update(points=7)

        Submission.objects.create(
            user=self.profile,
            problem=problem,
            language=self.language,
            status='D',
            result='AC',
            case_points=9,
            case_total=10,
        )

        sync_exam_progress_for_user_problem.run(user_id=self.profile.id, problem_id=problem.id)
        progress = ExamUserProgress.objects.get(user=self.profile, exam_tag=exam_tag)
        self.assertAlmostEqual(progress.earned_points, 0, places=3)
        self.assertAlmostEqual(progress.total_points, 7, places=3)
        self.assertAlmostEqual(progress.percent, 0.0, places=1)

    def test_sync_exam_progress_recovers_legacy_point_only_relation(self):
        exam_tag = ExamTag.objects.create(slug='exam-progress-c', name='Exam Progress C', is_public=True)
        problem = create_problem(code='exam_progress_c', is_public=True, partial=True, points=6)
        through_model = problem.exam_tags.through

        ExamTagProblemPoint.objects.create(exam_tag=exam_tag, problem=problem, points=8)
        through_model.objects.filter(problem_id=problem.id, examtag_id=exam_tag.id).delete()

        Submission.objects.create(
            user=self.profile,
            problem=problem,
            language=self.language,
            status='D',
            result='AC',
            case_points=5,
            case_total=10,
        )

        updated = sync_exam_progress_for_user_problem.run(user_id=self.profile.id, problem_id=problem.id)
        self.assertEqual(updated, 1)
        self.assertTrue(through_model.objects.filter(problem_id=problem.id, examtag_id=exam_tag.id).exists())

        progress = ExamUserProgress.objects.get(user=self.profile, exam_tag=exam_tag)
        self.assertAlmostEqual(progress.earned_points, 4, places=3)
        self.assertAlmostEqual(progress.total_points, 8, places=3)
        self.assertAlmostEqual(progress.percent, 50.0, places=1)

    def test_sync_exam_progress_recovers_legacy_m2m_only_relation(self):
        exam_tag = ExamTag.objects.create(slug='exam-progress-d', name='Exam Progress D', is_public=True)
        problem = create_problem(code='exam_progress_d', is_public=True, partial=True, points=6)
        through_model = problem.exam_tags.through
        through_model.objects.create(problem_id=problem.id, examtag_id=exam_tag.id)

        Submission.objects.create(
            user=self.profile,
            problem=problem,
            language=self.language,
            status='D',
            result='AC',
            case_points=10,
            case_total=10,
        )

        updated = sync_exam_progress_for_user_problem.run(user_id=self.profile.id, problem_id=problem.id)
        self.assertEqual(updated, 1)

        point_row = ExamTagProblemPoint.objects.get(exam_tag=exam_tag, problem=problem)
        self.assertAlmostEqual(point_row.points, 6, places=3)
        progress = ExamUserProgress.objects.get(user=self.profile, exam_tag=exam_tag)
        self.assertAlmostEqual(progress.earned_points, 6, places=3)
        self.assertAlmostEqual(progress.total_points, 6, places=3)
        self.assertAlmostEqual(progress.percent, 100.0, places=1)

    def test_exam_tag_problem_point_signal_keeps_m2m_in_sync(self):
        exam_tag = ExamTag.objects.create(slug='exam-progress-e', name='Exam Progress E', is_public=True)
        problem = create_problem(code='exam_progress_e', is_public=True, partial=True)
        through_model = problem.exam_tags.through

        row = ExamTagProblemPoint.objects.create(exam_tag=exam_tag, problem=problem, points=3)
        self.assertTrue(through_model.objects.filter(problem_id=problem.id, examtag_id=exam_tag.id).exists())

        row.delete()
        self.assertFalse(through_model.objects.filter(problem_id=problem.id, examtag_id=exam_tag.id).exists())
