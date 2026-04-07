from django.core.validators import RegexValidator
from django.db import models
from django.utils.translation import gettext_lazy as _


class ExamTag(models.Model):
    slug = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        verbose_name=_('exam slug'),
        validators=[
            RegexValidator(
                r'^[a-z0-9-]+$',
                _('Exam slug must contain lowercase letters, numbers, and hyphens only.'),
            ),
        ],
    )
    name = models.CharField(max_length=200, db_index=True, verbose_name=_('exam name'))
    expected_count = models.PositiveIntegerField(default=0, verbose_name=_('expected problems'))
    year = models.PositiveIntegerField(null=True, blank=True, db_index=True, verbose_name=_('year'))
    exam_type = models.CharField(max_length=64, blank=True, db_index=True, verbose_name=_('exam type'))
    province = models.CharField(max_length=64, blank=True, db_index=True, verbose_name=_('province'))
    status_note = models.CharField(max_length=128, blank=True, verbose_name=_('status note'))
    is_public = models.BooleanField(default=True, db_index=True, verbose_name=_('public'))
    sort_order = models.IntegerField(default=0, db_index=True, verbose_name=_('sort order'))

    class Meta:
        ordering = ('-year', 'sort_order', 'name', 'slug')
        verbose_name = _('exam tag')
        verbose_name_plural = _('exam tags')

    def __str__(self):
        return self.name
