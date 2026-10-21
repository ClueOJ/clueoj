import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge.models import (ContestParticipation, ContestSubmission, ExamOfflineAttempt, ExamTag, Language,
                          Profile, Submission, SubmissionBatch, SubmissionBatchItem)
from judge.models.tests.util import (CommonDataMixin, create_contest, create_contest_participation,
                                     create_contest_problem, create_problem)
from judge.tasks.batch_submit import run_submission_batch
from judge.utils.batch_submit import QueueFull, admit_item, validate_item
from judge.views import batch_submit


class BatchSubmitTests(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.language, _ = Language.objects.get_or_create(key='CPP20', defaults={
            'name': 'C++20', 'common_name': 'C++', 'ace': 'c_cpp', 'pygments': 'cpp', 'extension': 'cpp'})
        cls.problem = create_problem(code='batch_tet', is_public=True, allowed_languages=('CPP20', 'PYPY3'))
        cls.contest = create_contest(key='batch_live', is_visible=True)
        cls.cp = create_contest_problem(contest=cls.contest, problem=cls.problem)
        cls.profile = cls.users['normal'].profile

    def batch(self, started=True):
        return SubmissionBatch.objects.create(created_by=self.users['superuser'], contest=self.contest,
                                               expected_count=1, started_at=timezone.now() if started else None)

    def item(self, batch=None, **kwargs):
        values = dict(batch=batch or self.batch(), path='sources/normal/tet.cpp', username='normal',
                      problem_code=self.problem.code, language_key='CPP20', source='int main() {}')
        values.update(kwargs)
        return SubmissionBatchItem.objects.create(**values)

    def request(self, view, user='superuser', data=None, method='post', **kwargs):
        factory = RequestFactory()
        req = factory.get('/') if method == 'get' else factory.post('/', json.dumps(data or {}), content_type='application/json')
        req.user = self.users[user]
        return view(req, **kwargs)

    def test_join_and_attribution_and_idempotency(self):
        item = self.item()
        sub = admit_item(item.pk)
        self.assertEqual(sub.user_id, self.profile.pk)
        self.assertEqual(sub.contest_object_id, self.contest.pk)
        self.assertEqual(sub.source.source, 'int main() {}')
        self.assertEqual(sub.contest.participation.virtual, 0)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.current_contest_id, sub.contest.participation_id)
        self.assertIsNone(admit_item(item.pk))
        self.assertEqual(Submission.objects.count(), 1)

    def test_preview_does_not_switch_but_admission_leaves_other_contest(self):
        other = create_contest_participation(contest='other', user='normal')
        Profile.objects.filter(pk=self.profile.pk).update(current_contest=other)
        item = self.item()
        *_, changes = validate_item(self.contest, item)
        self.assertIn('Thoát contest other', changes)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.current_contest_id, other.pk)
        admit_item(item.pk)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.current_contest.contest_id, self.contest.pk)

    def offline(self):
        exam = ExamTag.objects.create(slug='batch-offline', name='Offline')
        return ExamOfflineAttempt.objects.create(user=self.profile, active_user=self.profile, exam=exam,
            exam_name='Offline', started_at=timezone.now(), deadline=timezone.now()+timedelta(hours=1))

    def test_finish_offline_without_revealing(self):
        attempt = self.offline()
        admit_item(self.item().pk)
        attempt.refresh_from_db()
        self.assertEqual(attempt.end_reason, 'early')
        self.assertIsNone(attempt.active_user_id)
        self.assertIsNone(attempt.revealed_at)

    def test_invalid_target_preserves_offline_and_contest(self):
        attempt = self.offline()
        self.contest.is_private = True
        self.contest.save()
        with self.assertRaisesMessage(ValidationError, 'quyền truy cập'):
            admit_item(self.item().pk)
        attempt.refresh_from_db()
        self.assertIsNone(attempt.ended_at)
        self.assertEqual(ContestParticipation.objects.count(), 0)

    def test_missing_banned_inactive_users(self):
        item = self.item(username='does-not-exist')
        with self.assertRaisesMessage(ValidationError, 'không tồn tại'):
            admit_item(item.pk)
        item.username = 'normal'
        item.save()
        self.contest.banned_users.add(self.profile)
        with self.assertRaisesMessage(ValidationError, 'bị cấm'):
            admit_item(item.pk)
        self.contest.banned_users.clear()
        self.users['normal'].is_active = False
        self.users['normal'].save()
        with self.assertRaisesMessage(ValidationError, 'bị khóa'):
            admit_item(item.pk)

    def test_contest_deadline_rechecked_after_preview(self):
        item = self.item()
        validate_item(self.contest, item)
        self.contest.end_time = timezone.now()-timedelta(seconds=1)
        self.contest.save()
        with self.assertRaisesMessage(ValidationError, 'kết thúc'):
            admit_item(item.pk)
        self.assertFalse(Submission.objects.exists())

    def test_personal_deadline_and_registration(self):
        self.contest.time_limit = timedelta(minutes=1)
        self.contest.save()
        p = ContestParticipation.objects.create(contest=self.contest, user=self.profile,
                                                real_start=timezone.now()-timedelta(minutes=2))
        with self.assertRaisesMessage(ValidationError, 'hết giờ'):
            admit_item(self.item().pk)
        p.delete()
        self.contest.registration_end = timezone.now()-timedelta(hours=1)
        self.contest.save()
        with self.assertRaisesMessage(ValidationError, 'đóng đăng ký'):
            admit_item(self.item().pk)

    def test_reuse_participation_and_limit(self):
        p = ContestParticipation.objects.create(contest=self.contest, user=self.profile, score=25)
        self.cp.max_submissions = 1
        self.cp.save()
        first = admit_item(self.item().pk)
        self.assertEqual(first.contest.participation_id, p.pk)
        with self.assertRaisesMessage(ValidationError, 'hết số lần'):
            admit_item(self.item().pk)
        p.refresh_from_db()
        self.assertEqual(p.score, 25)

    @override_settings(DMOJ_SUBMISSION_LIMIT=1)
    def test_full_queue_waits_without_switching(self):
        Submission.objects.create(user=self.profile, problem=self.problem, language=self.language)
        attempt = self.offline()
        with self.assertRaises(QueueFull):
            admit_item(self.item().pk)
        attempt.refresh_from_db()
        self.assertIsNone(attempt.ended_at)

    def test_pypy_and_wrong_problem_or_language(self):
        item = self.item(language_key='PYPY3', path='sources/normal/tet.py', source='print(1)')
        self.assertEqual(admit_item(item.pk).language.key, 'PYPY3')
        with self.assertRaisesMessage(ValidationError, 'không thuộc'):
            admit_item(self.item(problem_code='elsewhere').pk)
        self.problem.allowed_languages.remove(self.language)
        with self.assertRaisesMessage(ValidationError, 'không cho phép'):
            admit_item(self.item().pk)

    def test_all_routes_require_superuser(self):
        batch = self.batch()
        for user in ('normal', 'staff_problem_edit_own'):
            for view, kwargs, method in (
                (batch_submit.index, {}, 'get'), (batch_submit.problems, {}, 'get'),
                (batch_submit.create, {}, 'post'), (batch_submit.upload, {'batch_id':batch.pk}, 'post'),
                (batch_submit.detail, {'batch_id':batch.pk}, 'get'), (batch_submit.start, {'batch_id':batch.pk}, 'post'),
            ):
                with self.subTest(user=user, view=view.__name__), self.assertRaises(PermissionDenied):
                    self.request(view, user=user, method=method, **kwargs)

    def test_upload_preview_idempotency_and_validation(self):
        batch = self.batch(started=False)
        data = {'items':[{'path':'sources/normal/tet.cpp', 'problem':'batch_tet','source':'int main() {}'}]}
        for _ in range(2):
            self.assertEqual(self.request(batch_submit.upload, data=data, batch_id=batch.pk).status_code, 200)
        self.assertEqual(batch.items.count(), 1)
        self.assertFalse(Submission.objects.exists())
        self.assertFalse(ContestParticipation.objects.exists())
        data['items'][0]['path'] = '../normal/tet.cpp'
        self.assertEqual(self.request(batch_submit.upload, data=data, batch_id=batch.pk).status_code, 400)

    @patch('judge.views.batch_submit.run_submission_batch.delay')
    def test_start_incomplete_batch_and_repeat(self, delay):
        batch = self.batch(started=False)
        self.assertEqual(self.request(batch_submit.start, batch_id=batch.pk).status_code, 400)
        self.item(batch=batch)
        for _ in range(2):
            self.assertEqual(self.request(batch_submit.start, batch_id=batch.pk).status_code, 200)
        batch.refresh_from_db()
        self.assertIsNotNone(batch.started_at)

    @patch('judge.models.submission.Submission.judge')
    def test_worker_partial_failure_and_redelivery(self, judge):
        batch = self.batch()
        good = self.item(batch=batch)
        bad = self.item(batch=batch, path='sources/missing/tet.cpp', username='missing')
        for _ in range(2):
            run_submission_batch(str(batch.pk))
        good.refresh_from_db()
        bad.refresh_from_db()
        self.assertEqual(good.state, 'submitted')
        self.assertEqual(bad.state, 'error')
        self.assertEqual(judge.call_count, 1)
        self.assertEqual(ContestSubmission.objects.count(), 1)

    @patch('judge.models.submission.Submission.judge', side_effect=RuntimeError('unavailable'))
    def test_dispatch_failure_keeps_one_submission(self, judge):
        item = self.item()
        with self.assertLogs('judge.tasks.batch_submit', level='ERROR'):
            run_submission_batch(str(item.batch_id))
        run_submission_batch(str(item.batch_id))
        item.refresh_from_db()
        self.assertEqual(item.state, 'dispatch_error')
        self.assertIsNotNone(item.submission_id)
        self.assertEqual(Submission.objects.count(), 1)

    @override_settings(VNOJ_IGNORED_ORGANIZATION_SUBDOMAINS=['testserver'])
    def test_page_templates_and_url(self):
        self.assertEqual(reverse('batch_submit'), '/batch-submit/')
        self.client.force_login(self.users['superuser'])
        self.assertEqual(self.client.get(reverse('batch_submit')).status_code, 200)
        self.assertEqual(self.client.get(reverse('batch_submit_detail', args=[self.batch().pk])).status_code, 200)

    def test_create_idempotency(self):
        data = {'id':str(uuid.uuid4()), 'contest':self.contest.key, 'count':1}
        for _ in range(2):
            self.assertEqual(self.request(batch_submit.create, data=data).status_code, 200)
        self.assertEqual(SubmissionBatch.objects.count(), 1)

    def test_preregistered_participation_is_started(self):
        from datetime import datetime, timezone as tz
        participation = ContestParticipation.objects.create(contest=self.contest, user=self.profile,
            real_start=datetime(1970, 1, 1, tzinfo=tz.utc))
        sub = admit_item(self.item().pk)
        participation.refresh_from_db()
        self.assertEqual(sub.contest.participation_id, participation.pk)
        self.assertGreater(participation.real_start, timezone.now()-timedelta(minutes=1))

    def test_late_admission_rolls_back_offline_finish(self):
        attempt = self.offline()
        item = self.item()
        create = Submission.objects.create
        def late_create(**kwargs):
            sub = create(**kwargs)
            sub.date = self.contest.end_time
            return sub
        with patch('judge.utils.batch_submit.Submission.objects.create', side_effect=late_create):
            with self.assertRaisesMessage(ValidationError, 'hết giờ'):
                admit_item(item.pk)
        attempt.refresh_from_db()
        self.assertIsNone(attempt.ended_at)
        self.assertFalse(ContestParticipation.objects.exists())
        self.assertFalse(Submission.objects.exists())

    @override_settings(VNOJ_IGNORED_ORGANIZATION_SUBDOMAINS=['testserver'],
                       CSRF_FAILURE_VIEW='django.views.csrf.csrf_failure')
    def test_http_authentication_and_csrf(self):
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.get(reverse('batch_submit')).status_code, 302)
        client.force_login(self.users['normal'])
        self.assertEqual(client.get(reverse('batch_submit')).status_code, 403)
        client.force_login(self.users['superuser'])
        response = client.post(reverse('batch_submit_create'), '{}', content_type='application/json')
        self.assertEqual(response.status_code, 403)

    @patch('judge.tasks.batch_submit.run_submission_batch.apply_async')
    @override_settings(DMOJ_SUBMISSION_LIMIT=1)
    def test_worker_requeues_full_user(self, enqueue):
        Submission.objects.create(user=self.profile, problem=self.problem, language=self.language)
        item = self.item()
        run_submission_batch(str(item.batch_id))
        item.refresh_from_db()
        self.assertEqual(item.state, 'pending')
        enqueue.assert_called_once_with(args=[str(item.batch_id)], countdown=3)

    def test_editor_cannot_become_official_participant(self):
        self.contest.authors.add(self.profile)
        with self.assertRaisesMessage(ValidationError, 'spectate'):
            admit_item(self.item().pk)


    def test_upload_normalizes_filename_and_preserves_username(self):
        batch = self.batch(started=False)
        data = {'items': [{'path': 'sources/normal/TET.CPP', 'problem': 'batch_tet',
                           'source': 'int main() {}'}]}
        self.assertEqual(self.request(batch_submit.upload, data=data, batch_id=batch.pk).status_code, 200)
        item = batch.items.get()
        self.assertEqual(item.path, 'sources/normal/tet.cpp')
        self.assertEqual(item.language_key, 'CPP20')
        data['items'][0]['path'] = 'sources/normal/tet.cpp'
        self.assertEqual(self.request(batch_submit.upload, data=data, batch_id=batch.pk).status_code, 200)
        self.assertEqual(batch.items.count(), 1)
        data['items'][0]['source'] = 'int main() { return 1; }'
        self.assertEqual(self.request(batch_submit.upload, data=data, batch_id=batch.pk).status_code, 400)
        item.refresh_from_db()
        self.assertEqual(item.source, 'int main() {}')


    def test_verify_contest_code(self):
        def verify(code):
            request = RequestFactory().get('/', {'contest': code})
            request.user = self.users['superuser']
            return batch_submit.problems(request)

        for code in ('', 'missing-contest'):
            self.assertEqual(verify(code).status_code, 400)
        response = verify('  ' + self.contest.key + '  ')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data['contest']['key'], self.contest.key)
        self.assertEqual(data['problems'][0]['problem__code'], self.problem.code)
        self.contest.start_time = timezone.now() + timedelta(hours=1)
        self.contest.save()
        self.assertIn('chưa bắt đầu', json.loads(verify(self.contest.key).content)['error'])
        self.contest.start_time = timezone.now() - timedelta(hours=2)
        self.contest.end_time = timezone.now() - timedelta(hours=1)
        self.contest.save()
        self.assertIn('kết thúc', json.loads(verify(self.contest.key).content)['error'])
        empty = create_contest(key='batch_empty', is_visible=True)
        self.assertIn('chưa có bài', json.loads(verify(empty.key).content)['error'])
