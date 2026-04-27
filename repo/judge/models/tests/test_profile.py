import base64
import hmac
import struct

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes

from judge.forms import FREE_ORGANIZATION_PLAN_MESSAGE, OrganizationForm, ProposeContestProblemForm
from judge.models import Organization, Profile
from judge.models.tests.util import CommonDataMixin, create_contest, create_contest_participation, create_contest_problem, \
    create_organization, create_problem, create_user
from judge.views.problem import ProblemDetail


class OrganizationTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        self.profile = self.users['normal'].profile
        self.profile.organizations.add(self.organizations['open'])

    def setUp(self):
        self.client.defaults['HTTP_HOST'] = 'localhost'

    def test_contains(self):
        self.assertIn(self.profile, self.organizations['open'])
        self.assertIn(self.profile.id, self.organizations['open'])

        self.assertNotIn(self.users['superuser'].profile, self.organizations['open'])
        self.assertNotIn(self.users['superuser'].profile.id, self.organizations['open'])

        with self.assertRaisesRegex(TypeError, 'Organization membership test'):
            'aaaa' in self.organizations['open']

    def test_str(self):
        self.assertEqual(str(self.organizations['open']), 'open')

    def test_default_plan_is_free(self):
        organization = create_organization(name='default-free-plan')
        self.assertEqual(organization.plan, Organization.PLAN_FREE)

    def test_plan_helpers(self):
        organization = create_organization(name='plan-helper-free')
        self.assertTrue(organization.is_free_plan)
        self.assertFalse(organization.can_upload_problem())

        organization.plan = Organization.PLAN_PAID
        organization.save(update_fields=['plan'])
        self.assertTrue(organization.is_paid_plan)
        self.assertTrue(organization.can_upload_problem())

    def test_free_plan_can_only_use_public_global_problems(self):
        organization = create_organization(name='free-plan-policy')
        public_problem = create_problem(code='free_plan_public', is_public=True, is_organization_private=False)
        private_problem = create_problem(code='free_plan_private', is_public=False, is_organization_private=False)
        organization_problem = create_problem(
            code='free_plan_org_private',
            is_public=True,
            is_organization_private=True,
            organizations=(organization.name,),
        )

        self.assertTrue(organization.can_use_problem_in_contest(public_problem))
        self.assertFalse(organization.can_use_problem_in_contest(private_problem))
        self.assertFalse(organization.can_use_problem_in_contest(organization_problem))

    def test_paid_plan_can_use_private_problems(self):
        organization = create_organization(name='paid-plan-policy', plan=Organization.PLAN_PAID)
        private_problem = create_problem(code='paid_plan_private', is_public=False)
        self.assertTrue(organization.can_use_problem_in_contest(private_problem))

    def test_organization_form_only_superuser_can_set_plan(self):
        user = create_user(username='organization-form-normal')
        superuser = create_user(username='organization-form-superuser', is_superuser=True, is_staff=True)

        self.assertNotIn('plan', OrganizationForm(user=user).fields)
        self.assertIn('plan', OrganizationForm(user=superuser).fields)

    def test_free_plan_contest_problem_form_rejects_private_problem(self):
        organization = create_organization(name='free-form-policy')
        private_problem = create_problem(code='free_form_private', is_public=False)
        form = ProposeContestProblemForm(
            data={'problem': private_problem.pk, 'points': 100, 'order': 1},
            org_pk=organization.pk,
        )

        self.assertFalse(form.is_valid())
        self.assertIn(str(FREE_ORGANIZATION_PLAN_MESSAGE), form.errors['problem'])

    def test_free_plan_contest_problem_form_rejects_organization_private_problem(self):
        organization = create_organization(name='free-form-org')
        problem = create_problem(
            code='free_form_org_private',
            is_public=True,
            is_organization_private=True,
            organizations=(organization.name,),
        )
        form = ProposeContestProblemForm(
            data={'problem': problem.pk, 'points': 100, 'order': 1},
            org_pk=organization.pk,
        )

        self.assertFalse(form.is_valid())
        self.assertIn(str(FREE_ORGANIZATION_PLAN_MESSAGE), form.errors['problem'])

    def test_free_plan_contest_problem_form_allows_public_global_problem(self):
        organization = create_organization(name='free-form-public')
        public_problem = create_problem(code='free_form_public', is_public=True, is_organization_private=False)
        form = ProposeContestProblemForm(
            data={'problem': public_problem.pk, 'points': 100, 'order': 1},
            org_pk=organization.pk,
        )

        self.assertTrue(form.is_valid(), form.errors)

    def test_paid_plan_contest_problem_form_allows_private_problem(self):
        organization = create_organization(name='paid-form-private', plan=Organization.PLAN_PAID)
        private_problem = create_problem(code='paid_form_private', is_public=False)
        form = ProposeContestProblemForm(
            data={'problem': private_problem.pk, 'points': 100, 'order': 1},
            org_pk=organization.pk,
        )

        self.assertTrue(form.is_valid(), form.errors)

    def test_free_plan_blocks_legacy_private_contest_problem_access(self):
        organization = create_organization(name='downgraded-org', plan=Organization.PLAN_PAID)
        private_problem = create_problem(code='downgraded_private_problem', is_public=False)
        contest = create_contest(
            key='downgraded_org_contest',
            is_visible=True,
            is_organization_private=True,
            organizations=(organization.name,),
        )
        create_contest_problem(contest=contest, problem=private_problem)

        organization.plan = Organization.PLAN_FREE
        organization.save(update_fields=['plan'])
        self.users['normal'].profile.current_contest = create_contest_participation(
            contest=contest,
            user='normal',
        )
        self.users['normal'].profile.save(update_fields=['current_contest'])

        self.assertFalse(private_problem.is_accessible_by(self.users['normal']))

    def test_free_plan_blocks_member_access_to_organization_private_problem(self):
        organization = create_organization(name='down-org-prob', plan=Organization.PLAN_PAID)
        problem = create_problem(
            code='downgradedorgprob',
            is_public=True,
            is_organization_private=True,
            organizations=(organization.name,),
        )
        self.users['normal'].profile.organizations.add(organization)
        self.users['normal'].profile.current_contest = None
        self.users['normal'].profile.save(update_fields=['current_contest'])

        self.assertTrue(problem.is_accessible_by(self.users['normal']))

        organization.plan = Organization.PLAN_FREE
        organization.save(update_fields=['plan'])

        self.assertFalse(problem.is_accessible_by(self.users['normal']))

    def test_free_plan_org_private_problem_detail_shows_plan_message(self):
        organization = create_organization(name='down-org-detail', plan=Organization.PLAN_PAID)
        problem = create_problem(
            code='downgradedorgdetail',
            is_public=True,
            is_organization_private=True,
            organizations=(organization.name,),
        )
        self.users['normal'].profile.organizations.add(organization)
        self.users['normal'].profile.current_contest = None
        self.users['normal'].profile.save(update_fields=['current_contest'])
        organization.plan = Organization.PLAN_FREE
        organization.save(update_fields=['plan'])

        request = RequestFactory().get(problem.get_absolute_url())
        request.user = self.users['normal']
        request.profile = self.users['normal'].profile
        request.LANGUAGE_CODE = settings.LANGUAGE_CODE

        view = ProblemDetail()
        view.request = request
        view.kwargs = {'problem': problem.code}

        with self.assertRaises(PermissionDenied) as context:
            view.get_object()
        self.assertEqual(str(context.exception), str(FREE_ORGANIZATION_PLAN_MESSAGE))

    def test_normal_user_can_create_free_organization(self):
        self.client.force_login(self.users['normal'])
        response = self.client.post(reverse('organization_create'), data={
            'name': 'normal free org',
            'about': 'Created by a normal user.',
            'is_unlisted': 'on',
            'logo_override_image': '',
            'plan': Organization.PLAN_PAID,
            'admins': [self.profile.pk],
        })

        self.assertEqual(response.status_code, 302)
        organization = Organization.objects.get(name='normal free org')
        self.assertEqual(organization.plan, Organization.PLAN_FREE)
        self.assertEqual(organization.creator, self.profile)
        self.assertTrue(organization.admins.filter(pk=self.profile.pk).exists())

    def test_superuser_can_create_paid_organization(self):
        superuser = self.users['superuser']
        self.client.force_login(superuser)
        response = self.client.post(reverse('organization_create'), data={
            'name': 'super paid org',
            'about': 'Created by a superuser.',
            'is_unlisted': '',
            'logo_override_image': '',
            'plan': Organization.PLAN_PAID,
            'admins': [superuser.profile.pk],
        })

        self.assertEqual(response.status_code, 302)
        organization = Organization.objects.get(name='super paid org')
        self.assertEqual(organization.plan, Organization.PLAN_PAID)
        self.assertEqual(organization.creator, superuser.profile)

    def test_free_organization_creation_has_24_hour_cooldown(self):
        create_organization(name='recent-free-org', creator=self.profile, plan=Organization.PLAN_FREE)

        self.client.force_login(self.users['normal'])
        response = self.client.post(reverse('organization_create'), data={
            'name': 'blocked free org',
            'about': 'Should be blocked.',
            'is_unlisted': 'on',
            'logo_override_image': '',
            'admins': [self.profile.pk],
        })

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Organization.objects.filter(name='blocked free org').exists())

    def test_free_organization_creation_allowed_after_cooldown(self):
        organization = create_organization(name='old-free-org', creator=self.profile, plan=Organization.PLAN_FREE)
        Organization.objects.filter(pk=organization.pk).update(
            creation_date=timezone.now() - timezone.timedelta(hours=25),
        )

        self.client.force_login(self.users['normal'])
        response = self.client.post(reverse('organization_create'), data={
            'name': 'next free org',
            'about': 'Created after cooldown.',
            'is_unlisted': 'on',
            'logo_override_image': '',
            'admins': [self.profile.pk],
        })

        self.assertEqual(response.status_code, 302)
        self.assertTrue(Organization.objects.filter(name='next free org', plan=Organization.PLAN_FREE).exists())

    def test_organization_list_shows_paid_organizations_only(self):
        paid = create_organization(name='listed-paid-org', is_unlisted=False, plan=Organization.PLAN_PAID)
        free = create_organization(name='listed-free-org', is_unlisted=False, plan=Organization.PLAN_FREE)

        response = self.client.get(reverse('organization_list'))

        self.assertContains(response, paid.name)
        self.assertNotContains(response, free.name)

    def test_free_organization_list_is_superuser_only_and_shows_all_free_organizations(self):
        superuser = self.users['superuser']
        paid = create_organization(name='free-tab-paid-org', is_unlisted=False, plan=Organization.PLAN_PAID)
        listed_free = create_organization(name='free-tab-listed-org', is_unlisted=False, plan=Organization.PLAN_FREE)
        unlisted_free = create_organization(name='free-tab-hidden', is_unlisted=True, plan=Organization.PLAN_FREE)

        self.client.force_login(self.users['normal'])
        response = self.client.get(reverse('organization_free_list'))
        self.assertEqual(response.status_code, 403)

        self.client.force_login(superuser)
        response = self.client.get(reverse('organization_free_list'))
        self.assertContains(response, listed_free.name)
        self.assertContains(response, unlisted_free.name)
        self.assertNotContains(response, paid.name)


class ProfileTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        self.profile = self.users['normal'].profile
        self.profile.organizations.add(self.organizations['open'])

    def setUp(self):
        # We are doing a LOT of field modifications in this test case.
        # This is to prevent cryptic error messages when a test fails due
        # to modifications in another test. In theory, no two tests should
        # touch the same field, but who knows.
        self.profile.refresh_from_db()

    def test_username(self):
        self.assertEqual(str(self.profile), self.profile.username)

    def test_organization(self):
        self.assertIsNone(self.users['superuser'].profile.organization)
        self.assertEqual(self.profile.organization, self.organizations['open'])

    def test_calculate_points(self):
        self.profile.calculate_points()

        # Test saving
        for attr in ('points', 'problem_count', 'performance_points'):
            with self.subTest(attribute=attr):
                setattr(self.profile, attr, -1000)
                self.assertEqual(getattr(self.profile, attr), -1000)
                self.profile.calculate_points()
                self.assertEqual(getattr(self.profile, attr), 0)

    def test_generate_api_token(self):
        token = self.profile.generate_api_token()

        self.assertIsInstance(token, str)
        self.assertIsInstance(self.profile.api_token, str)

        user_id, raw_token = struct.unpack('>I32s', base64.urlsafe_b64decode(token))

        self.assertEqual(self.users['normal'].id, user_id)
        self.assertEqual(len(raw_token), 32)

        self.assertTrue(
            hmac.compare_digest(
                hmac.new(force_bytes(settings.SECRET_KEY), msg=force_bytes(raw_token), digestmod='sha256').hexdigest(),
                self.profile.api_token,
            ),
        )

    def test_update_contest(self):
        _now = timezone.now()
        for contest in (
            create_contest(
                key='finished_contest',
                start_time=_now - timezone.timedelta(days=100),
                end_time=_now - timezone.timedelta(days=10),
                is_visible=True,
            ),
            create_contest(
                key='inaccessible_contest',
                start_time=_now - timezone.timedelta(days=100),
                end_time=_now + timezone.timedelta(days=10),
            ),
        ):
            with self.subTest(name=contest.name):
                self.profile.current_contest = create_contest_participation(
                    contest=contest,
                    user=self.profile,
                )
                self.assertIsNotNone(self.profile.current_contest)
                self.profile.update_contest()
                self.assertIsNone(self.profile.current_contest)

    def test_css_class(self):
        self.assertEqual(self.profile.css_class, 'rating rate-none user')

    def test_get_user_css_class(self):
        self.assertEqual(
            Profile.get_user_css_class(display_rank='abcdef', rating=None, rating_colors=True),
            'rating rate-none abcdef',
        )
        self.assertEqual(
            Profile.get_user_css_class(display_rank='admin', rating=1300, rating_colors=True),
            'rating rate-pupil admin',
        )
        self.assertEqual(
            Profile.get_user_css_class(display_rank=1111, rating=1700, rating_colors=True),
            'rating rate-expert 1111',
        )
        self.assertEqual(
            Profile.get_user_css_class(display_rank='random', rating=1299, rating_colors=False),
            'random',
        )
