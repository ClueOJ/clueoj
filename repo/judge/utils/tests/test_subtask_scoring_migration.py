from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from judge.utils.tests.test_batch_scoring_migration import restore_latest_schema

class CumulativeScoringMigrationTests(TransactionTestCase):
    def test_existing_problem_defaults_to_off_without_rewriting_scores(self):
        old_target = [('judge', '0246_exam_host_locations')]
        new_target = [('judge', '0247_cumulative_subtask_scoring')]
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(old_target)
            old = executor.loader.project_state(old_target).apps
            group = old.get_model('judge', 'ProblemGroup').objects.create(name='cum-migration', full_name='Migration')
            problem = old.get_model('judge', 'Problem').objects.create(
                code='cum_migration', name='Migration', group=group, time_limit=1, memory_limit=65536, points=70)
            executor = MigrationExecutor(connection)
            executor.migrate(new_target)
            NewProblem = executor.loader.project_state(new_target).apps.get_model('judge', 'Problem')
            new = NewProblem.objects.get(pk=problem.pk)
            self.assertEqual(new.subtask_score_state, 'OFF')
            self.assertEqual(new.subtask_score_revision, 0)
            self.assertEqual(new.subtask_score_config, {})
            self.assertEqual(new.points, 70)
        finally:
            restore_latest_schema()
