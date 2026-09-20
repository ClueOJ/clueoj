from django.core.validators import MaxLengthValidator, MinValueValidator, RegexValidator
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
    exam_date = models.DateField(null=True, blank=True, db_index=True, verbose_name=_('exam date'))
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

    milestone_score_context = models.CharField(_('Ngữ cảnh điểm chung'), max_length=200, blank=True)
    milestone_note = models.TextField(_('Giải thích chung'), blank=True, validators=[MaxLengthValidator(4000)])
    milestone_source_note = models.TextField(
        _('Ghi chú nguồn (nội bộ)'), blank=True, validators=[MaxLengthValidator(4000)],
        help_text=_('Tự ghi nguồn của thông tin, ví dụ link, tên tài liệu hoặc người cung cấp. '
                  'Chỉ hiển thị cho superadmin trong Django admin; không công khai trên website.'),
    )

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


class ExamScoreMilestone(models.Model):
    exam_tag = models.ForeignKey(ExamTag, on_delete=models.CASCADE, related_name='score_milestones')
    label = models.CharField(_('Tên mốc'), max_length=160,
                             help_text=_('Tên mốc tùy ý, ví dụ Vàng, Top 32, Điểm chuẩn chuyên Tin.'))
    score = models.DecimalField(_('Điểm'), max_digits=12, decimal_places=4, validators=[MinValueValidator(0)],
                                help_text=_('Điểm theo nguồn, không nhất thiết cùng thang điểm với các bài trên ClueOJ.'))
    compare_with_practice_score = models.BooleanField(
        _('Tự động đối chiếu với điểm luyện tập'), default=False,
        help_text=_('Bật khi có thể so trực tiếp điểm luyện tập của đề trên ClueOJ với mốc này. '
                  'Điểm luyện tập lớn hơn hoặc bằng mốc sẽ được đánh dấu đã đạt. '
                  'Tắt nếu mốc chỉ cung cấp thông tin, ví dụ tổng điểm xét tuyển gồm nhiều môn.'),
    )
    score_context = models.CharField(_('Ngữ cảnh điểm riêng'), max_length=200, blank=True)
    note = models.TextField(_('Giải thích'), blank=True, validators=[MaxLengthValidator(4000)],
                            help_text=_('Với tuyển sinh, ghi rõ tổng xét tuyển và công thức tính điểm.'))
    sort_order = models.IntegerField(_('Thứ tự'), default=0)
    is_active = models.BooleanField(_('Hiển thị'), default=True, help_text=_('Bỏ chọn để tạm ẩn mốc.'))

    class Meta:
        ordering = ('sort_order', 'id')
        verbose_name = _('Mốc điểm tham khảo')
        verbose_name_plural = _('Mốc điểm tham khảo')
        constraints = [models.CheckConstraint(check=models.Q(score__gte=0), name='exam_milestone_score_nonnegative')]

    def clean(self):
        from django.core.exceptions import ValidationError
        self.label = self.label.strip()
        self.score_context = self.score_context.strip()
        if not self.label:
            raise ValidationError({'label': _('Tên mốc không được để trống.')})

    def __str__(self):
        return self.label
