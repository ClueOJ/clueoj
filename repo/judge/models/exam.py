from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.utils.translation import gettext_lazy as _


class ExamProvince(models.Model):
    name = models.CharField(max_length=64, unique=True, db_index=True, verbose_name=_('province name'))
    sort_order = models.IntegerField(default=0, db_index=True, verbose_name=_('sort order'))
    is_active = models.BooleanField(default=True, db_index=True, verbose_name=_('active'))

    class Meta:
        ordering = ('sort_order', 'name')
        verbose_name = _('exam province')
        verbose_name_plural = _('exam provinces')

    def __str__(self):
        return self.name


class ExamCategory(models.Model):
    name = models.CharField(max_length=64, unique=True, db_index=True, verbose_name=_('category name'))
    sort_order = models.IntegerField(default=0, db_index=True, verbose_name=_('sort order'))
    is_active = models.BooleanField(default=True, db_index=True, verbose_name=_('active'))

    class Meta:
        ordering = ('sort_order', 'name')
        verbose_name = _('exam category')
        verbose_name_plural = _('exam categories')

    def __str__(self):
        return self.name


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
    category = models.ForeignKey(
        ExamCategory,
        null=True,
        blank=True,
        related_name='exam_tags',
        on_delete=models.SET_NULL,
        verbose_name=_('category'),
    )
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


class ExamTagProblemPoint(models.Model):
    exam_tag = models.ForeignKey(
        ExamTag,
        related_name='problem_points',
        on_delete=models.CASCADE,
        verbose_name=_('exam tag'),
    )
    problem = models.ForeignKey(
        'judge.Problem',
        related_name='exam_point_links',
        on_delete=models.CASCADE,
        verbose_name=_('problem'),
    )
    points = models.FloatField(default=0, verbose_name=_('exam points'), validators=[MinValueValidator(0)])
    sort_order = models.IntegerField(default=0, db_index=True, verbose_name=_('sort order'))

    class Meta:
        ordering = ('sort_order', 'problem__code')
        unique_together = ('exam_tag', 'problem')
        verbose_name = _('exam tag problem point')
        verbose_name_plural = _('exam tag problem points')

    def __str__(self):
        return f'{self.exam_tag} - {self.problem} ({self.points})'


class ExamUserProgress(models.Model):
    user = models.ForeignKey(
        'judge.Profile',
        related_name='exam_progress',
        on_delete=models.CASCADE,
        verbose_name=_('user'),
    )
    exam_tag = models.ForeignKey(
        ExamTag,
        related_name='user_progress',
        on_delete=models.CASCADE,
        verbose_name=_('exam tag'),
    )
    earned_points = models.FloatField(default=0, verbose_name=_('earned points'))
    total_points = models.FloatField(default=0, verbose_name=_('total points'))
    percent = models.FloatField(default=0, verbose_name=_('percent'))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_('updated at'))

    class Meta:
        unique_together = ('user', 'exam_tag')
        verbose_name = _('exam user progress')
        verbose_name_plural = _('exam user progress')

    def __str__(self):
        return f'{self.user} - {self.exam_tag}: {self.earned_points}/{self.total_points}'
