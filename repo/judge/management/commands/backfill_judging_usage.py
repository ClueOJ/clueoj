from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from judge.models import JudgingUsageDaily, Submission
from judge.utils.judging_usage import accounting_timezone, valid_seconds


class Command(BaseCommand):
    help = 'Initialize empty daily totals from old local submissions. Stop bridged first.'

    def handle(self, *args, **options):
        if JudgingUsageDaily.objects.exists():
            raise CommandError('Daily totals already exist; refusing to overwrite or double-count usage.')
        totals = {}
        last_id = 0
        while True:
            ids = list(Submission.objects.filter(pk__gt=last_id).order_by('pk').values_list('pk', flat=True)[:2000])
            if not ids:
                break
            rows = Submission.objects.filter(
                pk__in=ids, external_submission__isnull=True, problem__external_problem__isnull=True,
            ).exclude(status__in=('QU', 'P', 'G')).values(
                'problem__storage_owner_organization_id', 'problem__storage_owner_organization__name',
                'judged_date', 'date', 'time',
            )
            for row in rows:
                day = timezone.localtime(row['judged_date'] or row['date'], accounting_timezone()).date()
                org = row['problem__storage_owner_organization_id'] or 0
                usage = totals.setdefault((org, day), JudgingUsageDaily(
                    organization_key=org, usage_date=day,
                    organization_name=row['problem__storage_owner_organization__name'] or '',
                ))
                usage.seconds += valid_seconds(row['time'])
                usage.attempts += 1
            last_id = ids[-1]
        with transaction.atomic():
            if JudgingUsageDaily.objects.exists():
                raise CommandError('Daily totals appeared during import; stop bridged before retrying.')
            JudgingUsageDaily.objects.bulk_create(totals.values(), batch_size=2000)
        self.stdout.write(self.style.SUCCESS('Initialized %s daily totals.' % len(totals)))
