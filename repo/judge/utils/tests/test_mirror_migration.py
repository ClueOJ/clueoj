from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from judge.utils.tests.test_batch_scoring_migration import restore_latest_schema


class MirrorMigrationTests(TransactionTestCase):
    fixtures = ['language_all.json']

    def test_upgrade_from_production_main_preserves_legacy_and_streak_history(self):
        main = [('judge', '0252_streak_offline_reveal_index')]
        latest = [('judge', '0255_flatten_mirrors')]
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(main)
            executor = MigrationExecutor(connection)
            executor.loader.check_consistent_history(connection)
            old = executor.loader.project_state(main).apps
            Problem = old.get_model('judge', 'Problem')
            group = old.get_model('judge', 'ProblemGroup').objects.create(name='production-upgrade', full_name='Upgrade')
            fields = {'group': group, 'time_limit': 1, 'memory_limit': 65536, 'points': 100}
            root = Problem.objects.create(code='qt_tst_21_phatqua', name='Root', **fields)
            parent = Problem.objects.create(code='production_upgrade_parent', name='Parent',
                                           mirror_of_id=root.pk, mirror_root_id=root.pk, **fields)
            mirror = Problem.objects.create(code='nvnamson_phatqua', name='Mirror',
                                           mirror_of_id=parent.pk, mirror_root_id=root.pk, **fields)
            data = old.get_model('judge', 'ProblemData').objects.create(problem=mirror, legacy_batch_type='sum',
                                                                     batch_scoring_overrides={'full:1': 'sum'})
            user = old.get_model('auth', 'User').objects.create(username='production_upgrade')
            profile = old.get_model('judge', 'Profile').objects.create(user=user)
            sub = old.get_model('judge', 'Submission').objects.create(problem=mirror, user=profile,
                language=old.get_model('judge', 'Language').objects.first(), status='D', result='WA',
                points=42, case_points=42, case_total=100, batch_scoring=None)
            planned = {migration.name for migration, backwards in executor.migration_plan(latest) if not backwards}
            self.assertEqual(planned, {'0253_problem_test_data_lock', '0254_mirror_complete_dataset_help',
                                       '0255_flatten_mirrors'})
            executor.migrate(latest)
            executor = MigrationExecutor(connection)
            executor.loader.check_consistent_history(connection)
            new = executor.loader.project_state(latest).apps
            current = new.get_model('judge', 'Problem').objects.get(pk=mirror.pk)
            self.assertEqual((current.mirror_of_id, current.mirror_root_id), (root.pk, root.pk))
            self.assertTrue(current.is_locked)
            saved_data = new.get_model('judge', 'ProblemData').objects.get(pk=data.pk)
            self.assertEqual((saved_data.legacy_batch_type, saved_data.batch_scoring_overrides),
                             ('sum', {'full:1': 'sum'}))
            saved = new.get_model('judge', 'Submission').objects.get(pk=sub.pk)
            self.assertEqual((saved.status, saved.points, saved.case_points), ('D', 42, 42))
            self.assertIsNone(saved.batch_scoring)
        finally:
            restore_latest_schema()
