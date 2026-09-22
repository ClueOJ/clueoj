from datetime import date
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from judge.models.tests.util import CommonDataMixin, create_organization, create_user


class OrganizationAddMembersTests(CommonDataMixin, TestCase):
    def setUp(self):
        self.client.defaults['HTTP_HOST'] = 'localhost'
        self.admin = self.users['normal']
        self.org = create_organization(name='bulk-members', slots=10, paid_until=date(2100, 1, 1))
        self.org.admins.add(self.admin.profile)
        self.target = create_user(username='bulk-target')
        self.url = reverse('organization_add_members', args=[self.org.slug])
        self.client.force_login(self.admin)

    def preview(self, usernames='bulk-target'):
        return self.client.post(self.url, {'usernames': usernames})

    def confirm(self, token):
        return self.client.post(self.url, {'action': 'confirm', 'token': token})

    def test_preview_and_confirmation(self):
        response = self.preview('bulk-target\nmissing-user\nbulk-target\n%s\n\n' % self.admin.username)
        self.assertEqual(response.context['missing'], ['missing-user'])
        self.assertEqual(response.context['existing'], [self.admin.username])
        self.assertEqual(response.context['duplicates'], ['bulk-target'])
        self.assertEqual(self.org.members.count(), 1)
        result = self.confirm(response.context['token'])
        self.assertRedirects(result, self.org.get_users_url(), fetch_redirect_response=False)
        self.assertContains(self.client.get(self.org.get_users_url()), 'Added 1 members: bulk-target')
        self.org.refresh_from_db()
        self.assertEqual(self.org.member_count, 2)
        self.assertTrue(self.org.members.filter(pk=self.target.profile.pk).exists())
        self.assertFalse(self.org.admins.filter(pk=self.target.profile.pk).exists())

    def test_fast_check_precedes_profile_lookup(self):
        self.org.slots = 2
        self.org.save()
        with patch('judge.views.organization.Profile.objects.filter') as lookup:
            response = self.preview('bulk-target\nmissing-user')
            self.assertFalse(any('user__username__in' in call.kwargs for call in lookup.call_args_list))
        self.assertTrue(response.context['form'].errors)
        self.assertNotIn('token', response.context)

    def test_exact_cap_and_repeated_confirmation(self):
        self.org.slots = 2
        self.org.save()
        token = self.preview().context['token']
        self.confirm(token)
        self.confirm(token)
        self.org.refresh_from_db()
        self.assertEqual(self.org.member_count, 2)

    def test_confirmation_rechecks_capacity(self):
        self.org.slots = 2
        self.org.save()
        token = self.preview().context['token']
        self.org.members.add(create_user(username='last-slot').profile)
        self.assertEqual(self.confirm(token).status_code, 200)
        self.assertFalse(self.org.members.filter(pk=self.target.profile.pk).exists())

    def test_invalid_token_and_wrong_organization(self):
        token = self.preview().context['token']
        self.confirm(token + 'tampered')
        other = create_organization(name='other-bulk-org', paid_until=date(2100, 1, 1))
        other.admins.add(self.admin.profile)
        self.client.post(reverse('organization_add_members', args=[other.slug]),
                         {'action': 'confirm', 'token': token})
        self.assertEqual(self.org.members.count(), 1)
        self.assertEqual(other.members.count(), 1)

    def test_permission_and_token_owner(self):
        token = self.preview().context['token']
        self.client.force_login(self.target)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.preview().status_code, 403)
        self.assertEqual(self.confirm(token).status_code, 403)
        self.target.user_permissions.add(Permission.objects.get(codename='edit_all_organization'))
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.confirm(token)
        self.assertEqual(self.org.members.count(), 1)

    def test_unlimited_and_empty_input(self):
        self.org.slots = None
        self.org.save()
        self.assertTrue(self.preview(' \n ').context['form'].errors)
        self.assertEqual(len(self.preview().context['candidates']), 1)

    def test_deleted_candidate(self):
        token = self.preview().context['token']
        self.target.delete()
        self.confirm(token)
        self.assertEqual(self.org.members.count(), 1)


class OrganizationRemoveMembersTests(CommonDataMixin, TestCase):
    def setUp(self):
        self.client.defaults['HTTP_HOST'] = 'localhost'
        self.admin = self.users['normal']
        self.org = create_organization(name='remove-members', slots=2, paid_until=date(2100, 1, 1))
        self.org.admins.add(self.admin.profile)
        self.target = create_user(username='remove-target')
        self.org.members.add(self.target.profile)
        self.url = reverse('organization_remove_members', args=[self.org.slug])
        self.client.force_login(self.admin)

    def preview(self, usernames='remove-target'):
        return self.client.post(self.url, {'usernames': usernames})

    def confirm(self, token):
        return self.client.post(self.url, {'action': 'confirm', 'token': token})

    def test_preview_and_confirm_only_remove_membership(self):
        outsider = create_user(username='remove-outsider')
        response = self.preview('remove-target\nremove-target\nmissing-user\nremove-outsider\n%s\n\n' % self.admin.username)
        self.assertEqual(response.context['missing'], ['missing-user'])
        self.assertEqual(response.context['nonmembers'], [outsider.username])
        self.assertEqual(response.context['protected'], [self.admin.username])
        self.assertEqual(response.context['duplicates'], ['remove-target'])
        self.assertEqual(self.org.members.count(), 2)
        result = self.confirm(response.context['token'])
        self.assertRedirects(result, self.org.get_users_url(), fetch_redirect_response=False)
        self.assertContains(self.client.get(self.org.get_users_url()), 'Removed 1 members from the organization: remove-target')
        self.target.refresh_from_db()
        self.org.refresh_from_db()
        self.assertEqual(self.org.member_count, 1)
        self.assertTrue(self.org.members.filter(pk=self.admin.profile.pk).exists())

    def test_admin_promoted_after_preview_is_protected(self):
        token = self.preview().context['token']
        self.org.admins.add(self.target.profile)
        self.confirm(token)
        self.assertTrue(self.org.members.filter(pk=self.target.profile.pk).exists())

    def test_repeated_confirmation_and_deleted_member(self):
        token = self.preview().context['token']
        self.confirm(token)
        self.confirm(token)
        self.org.refresh_from_db()
        self.assertEqual(self.org.member_count, 1)

    def test_confirmation_requires_current_permission(self):
        token = self.preview().context['token']
        self.org.admins.remove(self.admin.profile)
        self.assertEqual(self.confirm(token).status_code, 403)
        self.assertEqual(self.preview().status_code, 403)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.org.members.count(), 2)

    def test_token_is_bound_to_action_actor_and_organization(self):
        token = self.preview().context['token']
        self.confirm(token + 'tampered')
        other = create_organization(name='remove-other', paid_until=date(2100, 1, 1))
        other.admins.add(self.admin.profile)
        other.members.add(self.target.profile)
        self.client.post(reverse('organization_remove_members', args=[other.slug]),
                         {'action': 'confirm', 'token': token})
        self.assertTrue(other.members.filter(pk=self.target.profile.pk).exists())
        add_response = self.client.post(reverse('organization_add_members', args=[other.slug]),
                                        {'usernames': 'not-found'})
        self.confirm(add_response.context['token'])
        self.client.force_login(self.users['superuser'])
        self.confirm(token)
        self.assertEqual(self.org.members.count(), 2)

    def test_expired_token_and_empty_list(self):
        token = self.preview().context['token']
        with patch('django.core.signing.time.time', return_value=9999999999):
            self.confirm(token)
        self.assertEqual(self.org.members.count(), 2)
        self.assertTrue(self.preview(' \n ').context['form'].errors)
        response = self.preview(self.admin.username)
        self.assertNotContains(response, 'name="action" value="confirm"')


class OrganizationCapacityTests(CommonDataMixin, TestCase):
    def test_limits_apply_to_all_plans_and_membership_directions(self):
        from django.core.exceptions import PermissionDenied
        from django.db import transaction
        for is_open in (True, False):
            for plan in ('F', 'P'):
                org = create_organization(name='cap-%s-%s' % (is_open, plan), slots=1, is_open=is_open, paid_until=date(2100 if plan == 'P' else 2026, 1, 1))
                first = create_user(username='first-%s-%s' % (is_open, plan)).profile
                extra = create_user(username='extra-%s-%s' % (is_open, plan)).profile
                org.members.add(first)
                # Existing members are not counted as new additions.
                first.organizations.add(org)
                for add in (lambda: org.members.add(extra), lambda: extra.organizations.add(org),
                            lambda: org.admins.add(extra), lambda: extra.admin_of.add(org)):
                    with self.assertRaises(PermissionDenied), transaction.atomic():
                        add()
                self.assertEqual(org.members.count(), 1)
                self.assertFalse(org.admins.filter(pk=extra.pk).exists())

    def test_batch_addition_rolls_back_and_removal_releases_capacity(self):
        from django.core.exceptions import PermissionDenied
        from django.db import transaction
        org = create_organization(name='cap-batch', slots=1)
        first = create_user(username='cap-first').profile
        second = create_user(username='cap-second').profile
        with self.assertRaises(PermissionDenied), transaction.atomic():
            org.members.add(first, second)
        self.assertEqual(org.members.count(), 0)
        org.members.add(first)
        org.members.remove(first)
        second.organizations.add(org)
        org.refresh_from_db()
        self.assertEqual(org.member_count, 1)

    def test_multi_organization_add_is_atomic(self):
        from django.core.exceptions import PermissionDenied
        from django.db import transaction
        full = create_organization(name='cap-full', slots=0)
        available = create_organization(name='cap-available', slots=2)
        profile = create_user(username='cap-multiple').profile
        with self.assertRaises(PermissionDenied), transaction.atomic():
            profile.organizations.add(available, full)
        self.assertEqual(profile.organizations.count(), 0)

    def test_admin_addition_creates_membership_in_both_directions(self):
        for reverse in (True, False):
            org = create_organization(name='cap-admin-%s' % reverse, slots=1)
            profile = create_user(username='cap-admin-%s' % reverse).profile
            if reverse:
                profile.admin_of.add(org)
            else:
                org.admins.add(profile)
            self.assertTrue(org.members.filter(pk=profile.pk).exists())
            self.assertEqual(org.members.count(), 1)

    def test_approval_of_existing_member_does_not_consume_another_slot(self):
        from judge.models import OrganizationRequest
        org = create_organization(name='cap-approved', slots=1)
        admin = self.users['normal']
        org.admins.add(admin.profile)
        pending = OrganizationRequest.objects.create(user=admin.profile, organization=org, state='P', reason='join')
        self.client.defaults['HTTP_HOST'] = 'localhost'
        self.client.force_login(admin)
        response = self.client.post(reverse('organization_requests_pending', args=[org.slug]), {
            'form-TOTAL_FORMS': '1', 'form-INITIAL_FORMS': '1', 'form-MAX_NUM_FORMS': '1000',
            'form-0-id': str(pending.pk), 'form-0-state': 'A',
        })
        self.assertEqual(response.status_code, 302)
        pending.refresh_from_db()
        self.assertEqual(pending.state, 'A')
        self.assertEqual(org.members.count(), 1)

    def test_superuser_form_cannot_add_admin_over_capacity(self):
        from judge.forms import OrganizationForm
        org = create_organization(name='cap-superuser', slots=0)
        form = OrganizationForm(instance=org, user=self.users['superuser'], data={
            'name': org.name, 'about': 'test', 'paid_until': '2026-01-01', 'admins': [self.users['normal'].profile.pk],
        })
        self.assertFalse(form.is_valid())
        self.assertIn('admins', form.errors)


class OrganizationBulkPlanTests(CommonDataMixin, TestCase):
    def test_free_blocks_get_preview_confirmation_and_hides_buttons(self):
        self.client.defaults['HTTP_HOST'] = 'localhost'
        org = create_organization(name='bulk-plan', paid_until=date(2100, 1, 1), slots=10)
        admin = self.users['normal']
        org.admins.add(admin.profile)
        target = create_user(username='bulk-plan-target')
        member = create_user(username='bulk-plan-member')
        org.members.add(member.profile)
        self.client.force_login(admin)
        tokens = {}
        for name, username in [('organization_add_members', target.username),
                               ('organization_remove_members', member.username)]:
            url = reverse(name, args=[org.slug])
            self.assertContains(self.client.get(org.get_users_url()), url)
            tokens[url] = self.client.post(url, {'usernames': username}).context['token']
        org.paid_until = date(2026, 1, 1)
        org.save(update_fields=['paid_until'])
        for user in (admin, self.users['superuser']):
            self.client.force_login(user)
            page = self.client.get(org.get_users_url())
            for url, token in tokens.items():
                self.assertNotContains(page, url)
                self.assertContains(self.client.get(url), 'only available to paid organizations', status_code=403)
                self.assertEqual(self.client.post(url, {'usernames': target.username}).status_code, 403)
                self.assertEqual(self.client.post(url, {'action': 'confirm', 'token': token}).status_code, 403)
        self.assertFalse(org.members.filter(pk=target.profile.pk).exists())
        self.assertTrue(org.members.filter(pk=member.profile.pk).exists())
