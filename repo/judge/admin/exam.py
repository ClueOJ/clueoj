from reversion.admin import VersionAdmin

from judge.models import ExamTag
from judge.utils.views import NoBatchDeleteMixin


class ExamTagAdmin(NoBatchDeleteMixin, VersionAdmin):
    fieldsets = (
        (None, {
            'fields': (
                'slug', 'name', 'expected_count', 'year', 'exam_type',
                'province', 'status_note', 'is_public', 'sort_order',
            ),
        }),
    )
    list_display = (
        'slug', 'name', 'expected_count', 'year', 'exam_type',
        'province', 'is_public', 'sort_order',
    )
    search_fields = ('slug', 'name', 'exam_type', 'province', 'status_note')
    ordering = ('-year', 'sort_order', 'name')
    list_filter = ('is_public', 'year', 'exam_type', 'province')
