from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from judge.utils.tests.test_batch_scoring_migration import restore_latest_schema


class InvocationMigrationTests(TransactionTestCase):
    fixtures = ['language_all.json']

    def test_upgrade_after_pr26_preserves_scores_and_conservative_configuration(self):
        before = [('judge', '0256_conservative_ranking')]
        after = [('judge', '0258_invocation_source_unique_filename')]
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(before)
            old = executor.loader.project_state(before).apps
            group = old.get_model('judge', 'ProblemGroup').objects.create(name='inv-upgrade', full_name='Upgrade')
            config = {'groups': {'1': {'dependencies': []}}}
            problem = old.get_model('judge', 'Problem').objects.create(
                code='inv_upgrade', name='Upgrade', group=group, time_limit=1, memory_limit=65536,
                points=100, partial=True, conservative_config=config)
            user = old.get_model('auth', 'User').objects.create(username='inv_upgrade')
            profile = old.get_model('judge', 'Profile').objects.create(user=user)
            snapshot = {'version': 1, 'default': 'sum', 'full': [
                {'batch': 1, 'count': 2, 'mode': 'sum', 'total': 100}], 'dispatch_id': 'existing-run'}
            submission = old.get_model('judge', 'Submission').objects.create(
                problem=problem, user=profile, language=old.get_model('judge', 'Language').objects.first(),
                status='D', result='WA', points=75, case_points=75, case_total=100, batch_scoring=snapshot)
            summary = old.get_model('judge', 'SubmissionBatchResult').objects.create(
                submission=submission, batch=1, scoring_type='sum', points=75, total=100)
            contest = old.get_model('judge', 'Contest').objects.create(
                key='inv_upgrade', name='Upgrade', start_time=timezone.now(), end_time=timezone.now(),
                conservative_state='ON', conservative_revision=3, conservative_config=config)
            executor = MigrationExecutor(connection)
            planned = [migration.name for migration, backwards in executor.migration_plan(after) if not backwards]
            self.assertEqual(planned, ['0257_problem_invocations', '0258_invocation_source_unique_filename'])
            executor.migrate(after)
            MigrationExecutor(connection).loader.check_consistent_history(connection)
            apps = executor.loader.project_state(after).apps
            saved = apps.get_model('judge', 'Submission').objects.get(pk=submission.pk)
            self.assertEqual((saved.status, saved.points, saved.case_points, saved.batch_scoring),
                             ('D', 75, 75, snapshot))
            saved_summary = apps.get_model('judge', 'SubmissionBatchResult').objects.get(pk=summary.pk)
            self.assertEqual((saved_summary.points, saved_summary.total, saved_summary.scoring_type), (75, 100, 'sum'))
            saved_contest = apps.get_model('judge', 'Contest').objects.get(pk=contest.pk)
            self.assertEqual((saved_contest.conservative_state, saved_contest.conservative_revision,
                              saved_contest.conservative_config), ('ON', 3, config))
            self.assertEqual(apps.get_model('judge', 'Problem').objects.get(pk=problem.pk).conservative_config, config)
            self.assertEqual(apps.get_model('judge', 'Submission').objects.count(), 1)
            self.assertFalse(apps.get_model('judge', 'ProblemInvocationSource').objects.exists())
            self.assertFalse(apps.get_model('judge', 'ProblemInvocation').objects.exists())
        finally:
            restore_latest_schema()
