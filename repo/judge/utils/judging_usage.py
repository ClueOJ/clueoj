import math

import pytz
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from judge.models import JudgingUsageDaily, Submission


def accounting_timezone():
    return pytz.timezone(getattr(settings, 'JUDGING_USAGE_TIME_ZONE', settings.DEFAULT_USER_TIME_ZONE))


def valid_seconds(value):
    value = float(value or 0)
    return value if math.isfinite(value) and value >= 0 else 0


class PendingJudgingUsage:
    """Small in-memory accumulator, discarded if the bridge process crashes."""

    def __init__(self, organization_key, organization_name, usage_date, is_rejudge=False):
        self.organization_key = organization_key
        self.organization_name = organization_name
        self.usage_date = usage_date
        self.is_rejudge = is_rejudge
        self.seconds = 0
        self.last_position = 0
        self.started = False
        self.finished = False

    @classmethod
    def for_submission(cls, submission_id):
        row = Submission.objects.values(
            'problem__storage_owner_organization_id', 'problem__storage_owner_organization__name',
            'rejudged_date',
        ).get(pk=submission_id)
        return cls(row['problem__storage_owner_organization_id'] or 0,
                   row['problem__storage_owner_organization__name'] or '',
                   timezone.localtime(timezone.now(), accounting_timezone()).date(),
                   row['rejudged_date'] is not None)

    def begin(self):
        if self.started or self.finished:
            return False
        self.started = True
        return True

    def record(self, cases):
        if not self.started or self.finished:
            return
        # Judge packets arrive in test-position order over TCP. A high-water mark
        # avoids retaining a per-test dictionary and ignores replayed positions.
        for case in sorted(cases, key=lambda case: case['position']):
            if case['position'] > self.last_position:
                self.seconds += valid_seconds(case['time'])
                self.last_position = case['position']

    def finish(self):
        if self.finished:
            return
        # No retry on uncertain DB failure: usage statistics must not block grading.
        self.finished = True
        with transaction.atomic():
            daily, created = JudgingUsageDaily.objects.get_or_create(
                organization_key=self.organization_key, usage_date=self.usage_date,
                defaults={'organization_name': self.organization_name, 'seconds': self.seconds,
                          'attempts': 1, 'rejudges': int(self.is_rejudge)},
            )
            if not created:
                JudgingUsageDaily.objects.filter(pk=daily.pk).update(
                    seconds=F('seconds') + self.seconds, attempts=F('attempts') + 1,
                    rejudges=F('rejudges') + int(self.is_rejudge),
                )
