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
    day_count = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)], verbose_name=_('Số ngày thi'))
    duration_minutes = models.PositiveIntegerField(null=True, blank=True, validators=[MinValueValidator(1)])
    virtual_offline_enabled = models.BooleanField(default=False)

    def clean(self):
        super().clean()
        if self.virtual_offline_enabled and not self.duration_minutes:
            from django.core.exceptions import ValidationError
            raise ValidationError({'duration_minutes': 'Cần thời lượng lớn hơn 0 để bật thi offline.'})

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
    day_number = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)], verbose_name=_('Ngày thi'))
    points = models.FloatField(default=0, verbose_name=_('exam points'), validators=[MinValueValidator(0)])

    def clean(self):
        super().clean()
        if self.exam_tag_id and self.exam_tag.day_count and self.day_number and self.day_number > self.exam_tag.day_count:
            from django.core.exceptions import ValidationError
            raise ValidationError({'day_number': 'Ngày thi vượt quá số ngày của đề.'})
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


class ExamOfflineAttempt(models.Model):
    user = models.ForeignKey('judge.Profile', on_delete=models.PROTECT, related_name='offline_attempts')
    # NULL for finished attempts; a database constraint also prevents duplicate active attempts.
    active_user = models.OneToOneField('judge.Profile', null=True, blank=True,
                                      on_delete=models.PROTECT, related_name='active_offline_attempt')
    exam = models.ForeignKey(ExamTag, on_delete=models.PROTECT, related_name='offline_attempts')
    day_number = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)])
    exam_name = models.CharField(max_length=200)
    started_at = models.DateTimeField()
    deadline = models.DateTimeField(db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    revealed_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(max_length=16, blank=True)

    class Meta:
        ordering = ('-id',)
        indexes = [models.Index(fields=['user', 'exam', '-id'], name='offline_user_exam_idx')]


class ExamOfflineAttemptProblem(models.Model):
    attempt = models.ForeignKey(ExamOfflineAttempt, on_delete=models.CASCADE, related_name='problems')
    problem = models.ForeignKey('judge.Problem', on_delete=models.PROTECT)
    points = models.FloatField()
    partial = models.BooleanField()
    sort_order = models.PositiveIntegerField()
    final_submission = models.ForeignKey('judge.Submission', null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        ordering = ('sort_order', 'id')
        constraints = [models.UniqueConstraint(fields=['attempt', 'problem'], name='offline_attempt_problem_unique')]


class ExamOfflineSubmission(models.Model):
    attempt_problem = models.ForeignKey(ExamOfflineAttemptProblem, on_delete=models.CASCADE, related_name='submissions')
    submission = models.OneToOneField('judge.Submission', on_delete=models.PROTECT, related_name='offline_entry')
