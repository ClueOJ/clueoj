from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from judge.utils.tests.test_batch_scoring_migration import restore_latest_schema


class ConservativeMigrationTests(TransactionTestCase):
    fixtures = ['language_all.json']

    def test_upgrade_from_production_main_preserves_legacy_and_streak_history(self):
        main = [('judge', '0252_streak_offline_reveal_index')]
        latest = [('judge', '0256_conservative_ranking')]
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
            data = old.get_model('judge', 'ProblemData').objects.create(problem=mirror, legacy_batch_type='sum')
            user = old.get_model('auth', 'User').objects.create(username='production_upgrade')
            profile = old.get_model('judge', 'Profile').objects.create(user=user)
            sub = old.get_model('judge', 'Submission').objects.create(problem=mirror, user=profile,
                language=old.get_model('judge', 'Language').objects.first(), status='D', result='WA',
                points=42, case_points=42, case_total=100, batch_scoring=None)
            planned = {migration.name for migration, backwards in executor.migration_plan(latest) if not backwards}
            self.assertEqual(planned, {'0253_problem_test_data_lock', '0254_mirror_complete_dataset_help',
                                       '0255_flatten_mirrors', '0256_conservative_ranking'})
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

    def test_upgrade_after_pr25_preserves_submission_snapshots_and_scores(self):
        old_target = [('judge', '0255_flatten_mirrors')]
        target = [('judge', '0256_conservative_ranking')]
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(old_target)
            old = executor.loader.project_state(old_target).apps
            group = old.get_model('judge', 'ProblemGroup').objects.create(name='cons-upgrade', full_name='Upgrade')
            problem = old.get_model('judge', 'Problem').objects.create(code='cons_upgrade', name='Upgrade', group=group,
                time_limit=1, memory_limit=65536, points=100, partial=True)
            data = old.get_model('judge', 'ProblemData').objects.create(problem=problem, legacy_batch_type='sum',
                batch_scoring_overrides={'full:1': 'sum'})
            user = old.get_model('auth', 'User').objects.create(username='cons_upgrade')
            profile = old.get_model('judge', 'Profile').objects.create(user=user)
            snapshot = {'version': 1, 'default': 'sum', 'full': [{'batch': 1, 'count': 2, 'mode': 'sum', 'total': 100}],
                        'pretests': [], 'overrides': {}, 'dispatch_id': 'prior-run'}
            sub = old.get_model('judge', 'Submission').objects.create(problem=problem, user=profile,
                language=old.get_model('judge', 'Language').objects.first(), status='D', result='WA',
                points=75, case_points=75, case_total=100, batch_scoring=snapshot)
            summary = old.get_model('judge', 'SubmissionBatchResult').objects.create(submission=sub, batch=1,
                scoring_type='sum', points=75, total=100)
            contest = old.get_model('judge', 'Contest').objects.create(key='cons_upgrade', name='Upgrade',
                start_time=timezone.now(), end_time=timezone.now())
            cp = old.get_model('judge', 'ContestProblem').objects.create(contest=contest, problem=problem,
                points=200, order=1, partial=True)
            part = old.get_model('judge', 'ContestParticipation').objects.create(contest=contest, user=profile)
            cs = old.get_model('judge', 'ContestSubmission').objects.create(submission=sub, problem=cp,
                participation=part, points=150)
            executor = MigrationExecutor(connection)
            planned = {migration.name for migration, backwards in executor.migration_plan(target) if not backwards}
            self.assertEqual(planned, {'0256_conservative_ranking'})
            executor.migrate(target)
            MigrationExecutor(connection).loader.check_consistent_history(connection)
            apps = executor.loader.project_state(target).apps
            contest = apps.get_model('judge', 'Contest').objects.get(pk=contest.pk)
            self.assertTrue(contest.show_conservative_ranking)
            self.assertEqual(contest.conservative_state, 'OFF')
            saved = apps.get_model('judge', 'Submission').objects.get(pk=sub.pk)
            self.assertEqual((saved.status, saved.points, saved.case_points, saved.batch_scoring), ('D', 75, 75, snapshot))
            saved_summary = apps.get_model('judge', 'SubmissionBatchResult').objects.get(pk=summary.pk)
            self.assertEqual((saved_summary.points, saved_summary.scoring_type), (75, 'sum'))
            saved_cs = apps.get_model('judge', 'ContestSubmission').objects.get(pk=cs.pk)
            self.assertEqual(saved_cs.points, 150)
            self.assertIsNone(saved_cs.conservative_points)
            self.assertEqual(saved_cs.conservative_data, {})
            saved_data = apps.get_model('judge', 'ProblemData').objects.get(pk=data.pk)
            self.assertEqual((saved_data.legacy_batch_type, saved_data.batch_scoring_overrides), ('sum', {'full:1': 'sum'}))
            self.assertEqual(apps.get_model('judge', 'Problem').objects.get(pk=problem.pk).conservative_config, {})
        finally:
            restore_latest_schema()

    def test_existing_contests_are_off_and_no_scores_are_rewritten(self):
        old_target = [('judge', '0255_flatten_mirrors')]
        target = [('judge', '0256_conservative_ranking')]
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(old_target)
            old = executor.loader.project_state(old_target).apps
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
            self.assertTrue(contest.show_conservative_ranking)
            new_contest = apps.get_model('judge', 'Contest').objects.create(key='new_conservative', name='New',
                start_time=timezone.now(), end_time=timezone.now())
            self.assertEqual((new_contest.conservative_state, new_contest.conservative_config), ('OFF', {}))
        finally:
            restore_latest_schema()
