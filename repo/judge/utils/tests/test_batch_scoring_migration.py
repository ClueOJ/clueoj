from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class BatchScoringMigrationTests(TransactionTestCase):
    def test_legacy_modes_are_copied_before_problem_column_is_dropped(self):
        old_target = [('judge', '0243_submission_batch')]
        new_target = [('judge', '0244_per_batch_scoring')]
        executor = MigrationExecutor(connection)
        try:
            executor.migrate(old_target)
            old = executor.loader.project_state(old_target).apps
            Group = old.get_model('judge', 'ProblemGroup')
            Problem = old.get_model('judge', 'Problem')
            Case = old.get_model('judge', 'ProblemTestCase')
            group = Group.objects.create(name='scoring-migration', full_name='Scoring migration')
            problem_ids = []
            for mode in ('min', 'sum', 'Sum'):
                problem = Problem.objects.create(code='migration_' + mode.lower() + str(len(problem_ids)),
                    name='Migration test', group=group, time_limit=1, memory_limit=65536, points=100, batch_type=mode)
                problem_ids.append(problem.pk)
                Case.objects.create(dataset=problem, type='S', order=1, points=100, is_pretest=False)
            # Include a manual dataset with no DB testcase structure or ProblemData row.
            manual = Problem.objects.create(code='migration_manual', name='Manual', group=group,
                time_limit=1, memory_limit=65536, points=100, batch_type='sum', is_manually_managed=True)
            executor = MigrationExecutor(connection)
            executor.migrate(new_target)
            new = executor.loader.project_state(new_target).apps
            NewProblem = new.get_model('judge', 'Problem')
            NewCase = new.get_model('judge', 'ProblemTestCase')
            Data = new.get_model('judge', 'ProblemData')
            self.assertNotIn('batch_type', [field.name for field in NewProblem._meta.fields])
            for problem_id, mode in zip(problem_ids, ('min', 'sum', 'sum')):
                self.assertEqual(NewCase.objects.get(dataset_id=problem_id).batch_type, mode)
                self.assertEqual(Data.objects.get(problem_id=problem_id).legacy_batch_type, mode)
            self.assertEqual(Data.objects.get(problem_id=manual.pk).legacy_batch_type, 'sum')
        finally:
            MigrationExecutor(connection).migrate([('judge', '0245_default_sum_batches')])

    def test_default_sum_migration_preserves_existing_batch_policies(self):
        old_target = [('judge', '0244_per_batch_scoring')]
        new_target = [('judge', '0245_default_sum_batches')]
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(old_target)
            old = executor.loader.project_state(old_target).apps
            Group = old.get_model('judge', 'ProblemGroup')
            Problem = old.get_model('judge', 'Problem')
            Case = old.get_model('judge', 'ProblemTestCase')
            group = Group.objects.create(name='defsum', full_name='Default sum')
            problem = Problem.objects.create(code='defaultsum', name='Default sum', group=group,
                                              time_limit=1, memory_limit=65536, points=100)
            previous = Case.objects.create(dataset=problem, type='S', order=1, points=100, is_pretest=False)
            executor = MigrationExecutor(connection)
            executor.migrate(new_target)
            Case = executor.loader.project_state(new_target).apps.get_model('judge', 'ProblemTestCase')
            self.assertEqual(Case.objects.get(pk=previous.pk).batch_type, 'min')
            new = Case.objects.create(dataset_id=problem.pk, type='S', order=2, points=100, is_pretest=False)
            self.assertEqual(new.batch_type, 'sum')
        finally:
            MigrationExecutor(connection).migrate(new_target)
