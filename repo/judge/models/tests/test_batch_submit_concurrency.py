"""Exercise batch locks against a real transactional database."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.test import TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from judge.models import ContestParticipation, Language, Submission, SubmissionBatch, SubmissionBatchItem
from judge.models.tests.util import create_contest, create_contest_problem, create_problem, create_user
from judge.utils.batch_submit import admit_item


@skipUnlessDBFeature('has_select_for_update')
class BatchRaceTests(TransactionTestCase):
    def setUp(self):
        self.admin = create_user(username='batch-race-admin', is_superuser=True)
        self.user = create_user(username='batch-race-user')
        Language.objects.create(key='CPP20', name='C++20', common_name='C++')
        self.problem = create_problem(code='batchrace', is_public=True, allowed_languages=('CPP20',))
        self.contest = create_contest(key='batchrace', is_visible=True)
        self.cp = create_contest_problem(contest=self.contest, problem=self.problem, max_submissions=1)

    def item(self):
        batch = SubmissionBatch.objects.create(created_by=self.admin, contest=self.contest,
                                               expected_count=1, started_at=timezone.now())
        return SubmissionBatchItem.objects.create(batch=batch, username=self.user.username,
            path='sources/batch-race-user/main.cpp', problem_code=self.problem.code, language_key='CPP20',
            source='int main() {}')

    def race(self, *items):
        barrier = Barrier(len(items))
        def run(item):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:
                    sub = admit_item(item.pk)
                    return sub.pk if sub else None
                except ValidationError:
                    return None
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=len(items)) as pool:
            return list(pool.map(run, items))

    def test_duplicate_delivery_only_creates_one_submission(self):
        item = self.item()
        results = self.race(item, item)
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(Submission.objects.count(), 1)

    def test_competing_batches_obey_submission_limit_and_reuse_join(self):
        results = self.race(self.item(), self.item())
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(Submission.objects.count(), 1)
        self.assertEqual(ContestParticipation.objects.count(), 1)
