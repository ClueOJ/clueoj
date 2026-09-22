from datetime import date

from django.test import TestCase
from django.urls import reverse
from django.utils.translation import override, gettext

from judge.models.tests.util import CommonDataMixin, create_organization


class OrganizationTranslationTests(CommonDataMixin, TestCase):
    def setUp(self):
        self.client.defaults['HTTP_HOST'] = 'localhost'
        self.org = create_organization(name='translation-org', paid_until=date(2100, 1, 1), slots=20)
        self.org.admins.add(self.users['normal'].profile)
        self.client.force_login(self.users['normal'])

    def test_vietnamese_and_english_templates(self):
        for language, home, button, missing in [
            ('vi', 'Gói tổ chức', 'Thêm thành viên', 'Không tìm thấy'),
            ('en', 'Organization plan', 'Add members', 'Not found'),
        ]:
            with override(language):
                response = self.client.get(self.org.get_absolute_url(), HTTP_ACCEPT_LANGUAGE=language)
                self.assertContains(response, home)
                self.assertContains(self.client.get(self.org.get_users_url(), HTTP_ACCEPT_LANGUAGE=language), button)
                response = self.client.post(reverse('organization_add_members', args=[self.org.slug]),
                                            {'usernames': 'missing-translation-user'}, HTTP_ACCEPT_LANGUAGE=language)
                self.assertContains(response, missing)

    def test_vietnamese_backend_and_model_labels(self):
        with override('vi'):
            self.assertEqual(gettext('Temporary extension available'), 'Còn quyền gia hạn tạm')
            self.assertEqual(str(self.org._meta.get_field('paid_until').verbose_name), 'Ngày hết hạn gói trả phí')
            response = self.client.post(reverse('organization_add_members', args=[self.org.slug]),
                                        {'usernames': '\n'.join(['missing'] * 20)}, HTTP_ACCEPT_LANGUAGE='vi')
            self.assertContains(response, 'Vui lòng giảm số dòng')

    def test_vietnamese_extension_confirmation_is_rendered(self):
        self.org.paid_until = date(2026, 1, 1)
        self.org.temporary_extension_available = True
        self.org.save()
        response = self.client.get(self.org.get_absolute_url(), HTTP_ACCEPT_LANGUAGE='vi')
        self.assertContains(response, 'Gia hạn tạm 3 ngày')
        self.assertContains(response, 'onsubmit="return confirm(')
