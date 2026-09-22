from datetime import date, datetime, timezone as datetime_timezone
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models import Organization
from judge.models.tests.util import CommonDataMixin, create_organization, create_user


class OrganizationExpiryTests(CommonDataMixin, TestCase):
    def setUp(self):
        self.client.defaults['HTTP_HOST'] = 'localhost'
        self.org = create_organization(name='expiry-org', paid_until=date(2026, 9, 22), is_unlisted=False)
        self.org.admins.add(self.users['normal'].profile)

    def test_expiration_at_midnight_utc_plus_seven_without_save(self):
        cases = [
            (datetime(2026, 9, 21, 17, tzinfo=datetime_timezone.utc), True),
            (datetime(2026, 9, 22, 16, 59, 59, tzinfo=datetime_timezone.utc), True),
            (datetime(2026, 9, 22, 17, tzinfo=datetime_timezone.utc), False),
        ]
        for instant, paid in cases:
            with self.subTest(instant=instant), patch('django.utils.timezone.now', return_value=instant), timezone.override('America/New_York'):
                self.assertEqual(self.org.is_paid_plan, paid)
                self.assertEqual(self.org.is_free_plan, not paid)
                self.assertEqual(self.org.plan, 'P' if paid else 'F')
                self.assertEqual(self.org.can_upload_problem(), paid)
                self.assertEqual(Organization.objects.filter(Organization.paid_plan_filter(), pk=self.org.pk).exists(), paid)
                self.assertEqual(Organization.objects.filter(Organization.free_plan_filter(), pk=self.org.pk).exists(), not paid)

    def test_plan_is_read_only_and_new_organizations_are_free(self):
        with self.assertRaises(AttributeError):
            self.org.plan = 'P'
        org = create_organization(name='expiry-new')
        self.assertTrue(org.is_free_plan)

    def test_home_expiry_visible_only_to_admins(self):
        self.client.force_login(self.users['normal'])
        self.assertContains(self.client.get(self.org.get_absolute_url()), '22/09/2026')
        self.client.force_login(create_user(username='expiry-outsider'))
        self.assertNotContains(self.client.get(self.org.get_absolute_url()), 'Paid plan expiration date')
        self.client.force_login(self.users['superuser'])
        self.assertContains(self.client.get(self.org.get_absolute_url()), 'Organization plan')
        self.client.logout()
        self.assertNotContains(self.client.get(self.org.get_absolute_url()), 'Paid plan expiration date')

    def test_paid_lists_and_bulk_access_expire_automatically(self):
        self.client.force_login(self.users['superuser'])
        url = reverse('organization_add_members', args=[self.org.slug])
        for hour, paid in ((16, True), (17, False)):
            with patch('django.utils.timezone.now', return_value=datetime(2026, 9, 22, hour, tzinfo=datetime_timezone.utc)):
                self.assertEqual(self.client.get(url).status_code, 200 if paid else 403)
                response = self.client.get(reverse('organization_list'))
                self.assertEqual(self.org in response.context['organizations'], paid)
                response = self.client.get(reverse('organization_free_list'))
                self.assertEqual(self.org in response.context['organizations'], not paid)


class TemporaryExtensionTests(CommonDataMixin, TestCase):
    def setUp(self):
        self.client.defaults['HTTP_HOST'] = 'localhost'
        self.org = create_organization(name='temporary-org', paid_until=date(2026, 1, 1),
                                       temporary_extension_available=True)
        self.org.admins.add(self.users['normal'].profile)
        self.client.force_login(self.users['normal'])
        self.url = reverse('organization_temporary_extension', args=[self.org.slug])

    def test_one_use_and_three_calendar_days(self):
        instant = datetime(2026, 9, 22, 10, tzinfo=datetime_timezone.utc)
        with patch('django.utils.timezone.now', return_value=instant):
            self.assertContains(self.client.get(self.org.get_absolute_url()), 'Extend for 3 days')
            self.assertEqual(self.client.get(self.url).status_code, 405)
            self.assertEqual(self.client.post(self.url).status_code, 302)
            self.org.refresh_from_db()
            self.assertEqual(self.org.paid_until, date(2026, 1, 1))
            self.assertEqual(self.org.temporary_paid_until, date(2026, 9, 24))
            self.assertFalse(self.org.temporary_extension_available)
            self.assertTrue(self.org.is_paid_plan)
            self.assertTrue(Organization.objects.filter(Organization.paid_plan_filter(), pk=self.org.pk).exists())
            self.assertFalse(Organization.objects.filter(Organization.free_plan_filter(), pk=self.org.pk).exists())
            self.assertEqual(self.client.post(self.url).status_code, 403)
        with patch('django.utils.timezone.now', return_value=datetime(2026, 9, 24, 17, tzinfo=datetime_timezone.utc)):
            self.assertTrue(self.org.is_free_plan)
            self.assertEqual(self.client.post(self.url).status_code, 403)

    def test_unavailable_active_and_unauthorized_are_blocked(self):
        self.org.temporary_extension_available = False
        self.org.save()
        self.assertEqual(self.client.post(self.url).status_code, 403)
        self.org.temporary_extension_available = True
        self.org.paid_until = date(2100, 1, 1)
        self.org.save()
        self.assertEqual(self.client.post(self.url).status_code, 403)
        self.client.force_login(create_user(username='temporary-outsider'))
        self.assertEqual(self.client.post(self.url).status_code, 403)

    def test_admin_date_edit_resets_but_other_edits_do_not(self):
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory
        from types import SimpleNamespace
        from judge.admin.organization import OrganizationAdmin
        admin = OrganizationAdmin(Organization, AdminSite())
        request = RequestFactory().post('/')
        request.user = self.users['superuser']
        self.org.temporary_extension_available = False
        self.org.temporary_paid_until = date(2026, 9, 24)
        admin.save_model(request, self.org, SimpleNamespace(changed_data=['name']), True)
        self.org.refresh_from_db()
        self.assertFalse(self.org.temporary_extension_available)
        self.org.paid_until = date(2100, 1, 1)
        admin.save_model(request, self.org, SimpleNamespace(changed_data=['paid_until']), True)
        self.org.refresh_from_db()
        self.assertTrue(self.org.temporary_extension_available)
        self.assertIsNone(self.org.temporary_paid_until)
        form = admin.get_form(request, self.org)
        self.assertNotIn('temporary_extension_available', form.base_fields)
        self.assertNotIn('temporary_paid_until', form.base_fields)
