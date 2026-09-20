from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.forms import ModelForm
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from reversion.admin import VersionAdmin

from judge.models import ExamCategory, ExamProvince, ExamScoreMilestone, ExamTag, ExamTagProblemPoint
from judge.utils.views import NoBatchDeleteMixin
from judge.widgets import AdminSelect2Widget


def _parse_bulk_names(raw_text):
    names = []
    seen = set()
    for line in (raw_text or '').splitlines():
        name = line.strip()
        if not name:
            continue
        normalized = name.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        names.append(name)
    return names


class ExamProvinceAdminForm(ModelForm):
    name = forms.CharField(required=False, max_length=64, label=_('province name'))
    bulk_names = forms.CharField(
        required=False,
        label=_('Bulk province/region names'),
        widget=forms.Textarea(attrs={
            'rows': 8,
            'placeholder': 'Quang Tri\nDa Nang\nDHQGHN\nToan quoc',
        }),
        help_text=_('One name per line. The first non-empty line is used for this form; '
                    'the remaining lines are created automatically.'),
    )

    class Meta:
        model = ExamProvince
        fields = ('name', 'sort_order', 'is_active')

    def clean(self):
        cleaned_data = super(ExamProvinceAdminForm, self).clean()
        bulk_lines = _parse_bulk_names(cleaned_data.get('bulk_names'))

        name = (cleaned_data.get('name') or '').strip()
        if not name and bulk_lines:
            name = bulk_lines[0]
            cleaned_data['name'] = name

        if not name:
            raise ValidationError(_('Please provide a province/region name or fill the bulk box.'))

        # Allow "add" with existing first line in bulk mode by mapping to existing row.
        if not self.instance.pk and bulk_lines:
            existing_primary = ExamProvince.objects.filter(name__iexact=name).only('id').first()
            if existing_primary is not None:
                self.instance.pk = existing_primary.pk
                self.instance._state.adding = False

        cleaned_data['bulk_lines'] = bulk_lines
        return cleaned_data


class ExamProvinceAdmin(NoBatchDeleteMixin, VersionAdmin):
    form = ExamProvinceAdminForm

    fieldsets = (
        (None, {
            'fields': ('name', 'sort_order', 'is_active'),
        }),
        (_('Bulk add'), {
            'fields': ('bulk_names',),
        }),
    )
    list_display = ('name', 'sort_order', 'is_active')
    search_fields = ('name',)
    ordering = ('sort_order', 'name')
    list_filter = ('is_active',)

    def save_model(self, request, obj, form, change):
        bulk_lines = form.cleaned_data.get('bulk_lines') or []
        if change or not bulk_lines:
            return super(ExamProvinceAdmin, self).save_model(request, obj, form, change)

        primary_name = (form.cleaned_data.get('name') or obj.name or '').strip()
        base_sort_order = int(form.cleaned_data.get('sort_order') or 0)
        base_is_active = bool(form.cleaned_data.get('is_active'))
        existing_primary = ExamProvince.objects.filter(name__iexact=primary_name).first()
        if existing_primary is not None:
            obj.pk = existing_primary.pk
            obj.name = existing_primary.name
            obj._state.adding = False
        else:
            super(ExamProvinceAdmin, self).save_model(request, obj, form, change)

        created_count = 0
        extra_index = 0
        for name in bulk_lines:
            if name.casefold() == primary_name.casefold():
                continue
            _province_row, created = ExamProvince.objects.get_or_create(
                name=name,
                defaults={
                    'sort_order': base_sort_order + extra_index + 1,
                    'is_active': base_is_active,
                },
            )
            extra_index += 1
            if created:
                created_count += 1

        if created_count:
            self.message_user(
                request,
                _('Added %(count)d extra province/region item(s).') % {'count': created_count},
            )


class ExamCategoryAdminForm(ModelForm):
    name = forms.CharField(required=False, max_length=64, label=_('category name'))
    bulk_names = forms.CharField(
        required=False,
        label=_('Bulk category names'),
        widget=forms.Textarea(attrs={
            'rows': 8,
            'placeholder': 'THPT Chuyen\nHSG cap tinh\nHSG cap quoc gia',
        }),
        help_text=_('One name per line. The first non-empty line is used for this form; '
                    'the remaining lines are created automatically.'),
    )

    class Meta:
        model = ExamCategory
        fields = ('name', 'sort_order', 'is_active')

    def clean(self):
        cleaned_data = super(ExamCategoryAdminForm, self).clean()
        bulk_lines = _parse_bulk_names(cleaned_data.get('bulk_names'))

        name = (cleaned_data.get('name') or '').strip()
        if not name and bulk_lines:
            name = bulk_lines[0]
            cleaned_data['name'] = name

        if not name:
            raise ValidationError(_('Please provide a category name or fill the bulk box.'))

        # Allow "add" with existing first line in bulk mode by mapping to existing row.
        if not self.instance.pk and bulk_lines:
            existing_primary = ExamCategory.objects.filter(name__iexact=name).only('id').first()
            if existing_primary is not None:
                self.instance.pk = existing_primary.pk
                self.instance._state.adding = False

        cleaned_data['bulk_lines'] = bulk_lines
        return cleaned_data


class ExamCategoryAdmin(NoBatchDeleteMixin, VersionAdmin):
    form = ExamCategoryAdminForm

    fieldsets = (
        (None, {
            'fields': ('name', 'sort_order', 'is_active'),
        }),
        (_('Bulk add'), {
            'fields': ('bulk_names',),
        }),
    )
    list_display = ('name', 'sort_order', 'is_active')
    search_fields = ('name',)
    ordering = ('sort_order', 'name')
    list_filter = ('is_active',)

    def save_model(self, request, obj, form, change):
        bulk_lines = form.cleaned_data.get('bulk_lines') or []
        if change or not bulk_lines:
            return super(ExamCategoryAdmin, self).save_model(request, obj, form, change)

        primary_name = (form.cleaned_data.get('name') or obj.name or '').strip()
        base_sort_order = int(form.cleaned_data.get('sort_order') or 0)
        base_is_active = bool(form.cleaned_data.get('is_active'))
        existing_primary = ExamCategory.objects.filter(name__iexact=primary_name).first()
        if existing_primary is not None:
            obj.pk = existing_primary.pk
            obj.name = existing_primary.name
            obj._state.adding = False
        else:
            super(ExamCategoryAdmin, self).save_model(request, obj, form, change)

        created_count = 0
        extra_index = 0
        for name in bulk_lines:
            if name.casefold() == primary_name.casefold():
                continue
            _category_row, created = ExamCategory.objects.get_or_create(
                name=name,
                defaults={
                    'sort_order': base_sort_order + extra_index + 1,
                    'is_active': base_is_active,
                },
            )
            extra_index += 1
            if created:
                created_count += 1

        if created_count:
            self.message_user(
                request,
                _('Added %(count)d extra category item(s).') % {'count': created_count},
            )


class ExamTagAdminForm(ModelForm):
    year = forms.TypedChoiceField(
        required=False,
        choices=(),
        coerce=int,
        empty_value=None,
        label=_('year'),
        widget=AdminSelect2Widget(attrs={'style': 'width: 100%;'}),
    )
    province = forms.ChoiceField(
        required=False,
        choices=(),
        label=_('province'),
        widget=AdminSelect2Widget(attrs={'style': 'width: 100%;'}),
    )
    category = forms.ModelChoiceField(
        required=False,
        queryset=ExamCategory.objects.none(),
        label=_('category'),
        widget=AdminSelect2Widget(attrs={'style': 'width: 100%;'}),
    )
    new_category = forms.CharField(
        required=False,
        label=_('new category'),
        widget=forms.TextInput(attrs={'placeholder': _('Type a category name and save to create it')}),
        help_text=_('Optional. If filled, this category will be created automatically and selected.'),
    )

    class Meta:
        model = ExamTag
        fields = (
            'slug', 'name', 'expected_count', 'year', 'exam_date', 'exam_type',
            'province', 'category', 'status_note', 'is_public', 'sort_order',
            'milestone_score_context', 'milestone_note', 'milestone_source_note',
        )

    def __init__(self, *args, **kwargs):
        super(ExamTagAdminForm, self).__init__(*args, **kwargs)

        current_year = timezone.now().year
        existing_years = list(ExamTag.objects.exclude(year__isnull=True).values_list('year', flat=True))
        floor_year = min(existing_years) if existing_years else current_year - 20
        floor_year = min(floor_year, current_year - 20)
        year_values = list(range(current_year, floor_year - 1, -1))
        current_value = getattr(self.instance, 'year', None)
        if current_value and current_value not in year_values:
            year_values.append(current_value)
            year_values.sort(reverse=True)
        self.fields['year'].choices = [('', '---------')] + [(year, str(year)) for year in year_values]

        values = list(
            ExamProvince.objects
            .filter(is_active=True)
            .order_by('sort_order', 'name')
            .values_list('name', flat=True)
        )
        current_province = (getattr(self.instance, 'province', None) or '').strip()
        if current_province and current_province not in values:
            values.append(current_province)
        self.fields['province'].choices = [('', '---------')] + [(name, name) for name in values]
        self.fields['province'].help_text = _(
            'Manage this dropdown at Admin > Judge > Exam provinces.'
        )

        category_filter = Q(is_active=True)
        current_category_id = getattr(self.instance, 'category_id', None)
        if current_category_id:
            category_filter |= Q(pk=current_category_id)
        category_queryset = ExamCategory.objects.filter(category_filter).order_by('sort_order', 'name')
        self.fields['category'].queryset = category_queryset
        self.fields['category'].help_text = _(
            'Manage this dropdown at Admin > Judge > Exam categories.'
        )

    def clean_new_category(self):
        return (self.cleaned_data.get('new_category') or '').strip()

    def clean(self):
        cleaned_data = super(ExamTagAdminForm, self).clean()
        new_category = cleaned_data.get('new_category')
        if new_category:
            max_sort = ExamCategory.objects.order_by('-sort_order').values_list('sort_order', flat=True).first() or 0
            category, _ = ExamCategory.objects.get_or_create(
                name=new_category,
                defaults={'sort_order': max_sort + 1, 'is_active': True},
            )
            cleaned_data['category'] = category
        return cleaned_data


class ExamTagAdmin(NoBatchDeleteMixin, VersionAdmin):
    form = ExamTagAdminForm

    fieldsets = (
        (None, {
            'fields': (
                'slug', 'name', 'expected_count', 'year', 'exam_date', 'exam_type',
                'province', 'category', 'new_category', 'status_note', 'is_public', 'sort_order',
            ),
        }),
    )
    milestone_fields = ('milestone_score_context', 'milestone_note', 'milestone_source_note')

    def get_fieldsets(self, request, obj=None):
        fields = super().get_fieldsets(request, obj)
        if _is_superadmin(request):
            return fields + ((_('Mốc điểm tham khảo'), {'fields': self.milestone_fields}),)
        return fields

    def get_form(self, request, obj=None, **kwargs):
        if not _is_superadmin(request):
            kwargs['exclude'] = tuple(kwargs.get('exclude') or ()) + self.milestone_fields
        return super().get_form(request, obj, **kwargs)

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        if request.method == 'POST' and not _is_superadmin(request):
            if any(key in request.POST for key in self.milestone_fields) or any(
                    key.startswith('score_milestones-') for key in request.POST):
                raise PermissionDenied
        return super().changeform_view(request, object_id, form_url, extra_context)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and not _is_superadmin(request):
            if obj.score_milestones.exists() or any(getattr(obj, name) for name in self.milestone_fields):
                return False
        return super().has_delete_permission(request, obj)

    def delete_queryset(self, request, queryset):
        if not _is_superadmin(request) and any(not self.has_delete_permission(request, obj) for obj in queryset):
            raise PermissionDenied
        return super().delete_queryset(request, queryset)

    def history_view(self, request, object_id, extra_context=None):
        # Revisions may contain private sources even after the current source was cleared.
        if not _is_superadmin(request):
            raise PermissionDenied
        return super().history_view(request, object_id, extra_context)

    def revision_view(self, request, object_id, version_id, extra_context=None):
        if not _is_superadmin(request):
            raise PermissionDenied
        return super().revision_view(request, object_id, version_id, extra_context)

    def recover_view(self, request, version_id, extra_context=None):
        if not _is_superadmin(request):
            raise PermissionDenied
        return super().recover_view(request, version_id, extra_context)

    def recoverlist_view(self, request, extra_context=None):
        if not _is_superadmin(request):
            raise PermissionDenied
        return super().recoverlist_view(request, extra_context)

    list_display = (
        'slug', 'name', 'expected_count', 'year', 'exam_date', 'exam_type',
        'province', 'category', 'is_public', 'sort_order',
    )
    search_fields = ('slug', 'name', 'exam_type', 'province', 'category__name', 'status_note')
    ordering = ('-year', 'sort_order', 'name', 'slug')
    list_filter = ('is_public', 'year', 'exam_date', 'exam_type', 'province', 'category')


class ExamTagProblemPointInline(admin.TabularInline):
    model = ExamTagProblemPoint
    extra = 0
    autocomplete_fields = ('problem',)
    fields = ('problem', 'points', 'sort_order')
    ordering = ('sort_order', 'problem__code')


def _is_superadmin(request):
    return request.user.is_authenticated and request.user.is_active and request.user.is_superuser


class MilestoneDecimalField(forms.DecimalField):
    def to_python(self, value):
        if isinstance(value, str):
            value = value.strip().replace(',', '.')
        return super().to_python(value)


class ExamScoreMilestoneForm(ModelForm):
    score = MilestoneDecimalField(label=_('Điểm'), max_digits=12, decimal_places=4, min_value=0,
                                  widget=forms.TextInput(attrs={'inputmode': 'decimal'}))

    class Meta:
        model = ExamScoreMilestone
        exclude = ('score_context',)
        help_texts = {
            'note': _('Không bắt buộc. Hiện khi xem biểu tượng ⓘ cạnh tên mốc. Ví dụ: Điểm xét tuyển = Toán chung + Môn chuyên × 2.'),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.score_context:
            self.initial['note'] = '\n'.join(filter(None, (self.instance.score_context, self.instance.note)))

    def save(self, commit=True):
        # Preserve legacy context in the single editable explanation field.
        self.instance.score_context = ''
        return super().save(commit=commit)


class ExamScoreMilestoneInline(admin.StackedInline):
    model = ExamScoreMilestone
    form = ExamScoreMilestoneForm
    extra = 0
    fields = (('label', 'score'), 'compare_with_practice_score', 'note',
              ('sort_order', 'is_active'))

    def has_view_permission(self, request, obj=None):
        return _is_superadmin(request)

    def has_add_permission(self, request, obj=None):
        return _is_superadmin(request)

    def has_change_permission(self, request, obj=None):
        return _is_superadmin(request)

    def has_delete_permission(self, request, obj=None):
        return _is_superadmin(request)


ExamTagAdmin.inlines = (ExamTagProblemPointInline, ExamScoreMilestoneInline)
