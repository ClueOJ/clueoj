from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from judge.utils.tests.test_batch_scoring_migration import restore_latest_schema


class ConservativeMigrationTests(TransactionTestCase):
    fixtures = ['language_all.json']

    def test_upgrade_from_production_main_preserves_legacy_and_streak_history(self):
        main = [('judge', '0252_streak_offline_reveal_index')]
        latest = [('judge', '0255_flatten_mirrors')]
        try:
            MigrationExecutor(connection).migrate([('judge', '0247_cumulative_subtask_scoring')])
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
            data = old.get_model('judge', 'ProblemData').objects.create(problem=mirror, legacy_batch_type='sum')
            user = old.get_model('auth', 'User').objects.create(username='production_upgrade')
            profile = old.get_model('judge', 'Profile').objects.create(user=user)
            sub = old.get_model('judge', 'Submission').objects.create(problem=mirror, user=profile,
                language=old.get_model('judge', 'Language').objects.first(), status='D', result='WA',
                points=42, case_points=42, case_total=100, batch_scoring=None)
            planned = {migration.name for migration, backwards in executor.migration_plan(latest) if not backwards}
            self.assertNotIn('0251_streak_achievements', planned)
            executor.migrate(latest)
            executor = MigrationExecutor(connection)
            executor.loader.check_consistent_history(connection)
            new = executor.loader.project_state(latest).apps
            current = new.get_model('judge', 'Problem').objects.get(pk=mirror.pk)
            self.assertEqual((current.mirror_of_id, current.mirror_root_id), (root.pk, root.pk))
            self.assertTrue(current.is_locked)
            self.assertEqual(current.conservative_config, {})
            self.assertEqual(new.get_model('judge', 'ProblemData').objects.get(pk=data.pk).legacy_batch_type, 'sum')
            saved = new.get_model('judge', 'Submission').objects.get(pk=sub.pk)
            self.assertEqual((saved.status, saved.points, saved.case_points), ('D', 42, 42))
            self.assertIsNone(saved.batch_scoring)
        finally:
            restore_latest_schema()

    def test_visibility_migration_preserves_existing_access_and_build(self):
        old_target = [('judge', '0248_conservative_ranking')]
        target = [('judge', '0249_conservative_ranking_visibility')]
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(old_target)
            old = executor.loader.project_state(old_target).apps
            from django.utils import timezone
            contest = old.get_model('judge', 'Contest').objects.create(key='cons_visibility_migration',
                name='Visibility', start_time=timezone.now(), end_time=timezone.now(), conservative_state='ON',
                conservative_revision=7, conservative_config={'pinned': 'config'}, conservative_progress={'done': 12})
            executor = MigrationExecutor(connection)
            executor.migrate(target)
            apps = executor.loader.project_state(target).apps
            contest = apps.get_model('judge', 'Contest').objects.get(pk=contest.pk)
            self.assertTrue(contest.show_conservative_ranking)
            self.assertEqual(contest.conservative_state, 'ON')
            self.assertEqual(contest.conservative_revision, 7)
            self.assertEqual(contest.conservative_config, {'pinned': 'config'})
            self.assertEqual(contest.conservative_progress, {'done': 12})
        finally:
            restore_latest_schema()

    def test_existing_contests_are_off_and_no_scores_are_rewritten(self):
        old_target = [('judge', '0247_cumulative_subtask_scoring')]
        target = [('judge', '0248_conservative_ranking')]
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(old_target)
            old = executor.loader.project_state(old_target).apps
            from django.utils import timezone
            contest = old.get_model('judge', 'Contest').objects.create(key='migration_conservative',
                name='Migration', start_time=timezone.now(), end_time=timezone.now())
            executor = MigrationExecutor(connection)
            executor.migrate(target)
            apps = executor.loader.project_state(target).apps
            contest = apps.get_model('judge', 'Contest').objects.get(pk=contest.pk)
            self.assertEqual(contest.conservative_state, 'OFF')
            self.assertEqual(contest.conservative_revision, 0)
            self.assertEqual(contest.conservative_config, {})
            self.assertEqual(contest.conservative_progress, {})
        finally:
            restore_latest_schema()
