"""One-time, explicit cutover. Run with submission admission/judging paused."""
import gzip
from itertools import chain
from pathlib import Path

from django.core import serializers
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Max, Q, Case, When, F, FloatField
from django.utils import timezone

from judge.models import (Problem, Submission, StreakEpoch, StreakProgress, StreakEvent,
                          StreakSummary, StreakContribution, StreakDay, StreakRun,
                          StreakProblemState, StreakRebuildRequest)
from judge.utils.streaks import baseline_progress, progress_generation, score_decimal


class Command(BaseCommand):
    help = 'Initialize achievement streaks at zero while preserving historical score baselines (dry-run by default).'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--archive', help='Required new .json.gz archive path for the old streak data.')

    def handle(self, *args, **options):
        if StreakEpoch.objects.exists():
            raise CommandError('Achievement streaks are already initialized. Refusing to reset earned days.')
        cutoff = Submission.objects.aggregate(last=Max('id'))['last'] or 0
        self.stdout.write('Cutoff submission ID: %d. All current/record streaks will start at zero.' % cutoff)
        if not options['apply']:
            self.stdout.write('Dry run. Stop admission, judging, old backfill and streak workers before --apply.')
            return
        if not options['archive']:
            raise CommandError('--archive /path/to/new-streak-backup.json.gz is required.')
        archive = Path(options['archive'])
        if archive.exists():
            raise CommandError('Archive already exists; choose a new path.')
        started_at = timezone.now()
        # Keep the old derived tables and queued work recoverable without
        # touching account scores/submissions. The explicit cutover is separate
        # from the additive schema migration.
        models = [StreakSummary, StreakContribution, StreakDay, StreakRun,
                  StreakProblemState, StreakRebuildRequest]
        with archive.open('xb') as raw:
            archive.chmod(0o600)
            with gzip.open(raw, 'wt', encoding='utf-8') as output:
                serializers.serialize('json', chain.from_iterable(model.objects.all().iterator() for model in models),
                                      stream=output)
        with transaction.atomic():
            # An empty new table plus the missing singleton is the safe retry
            # state after a rolled back baseline build.
            if StreakProgress.objects.exists() or StreakEvent.objects.exists():
                raise CommandError('Unexpected achievement data exists before cutover.')
            problems = {p.pk: p for p in Problem.objects.all().only('id', 'points', 'partial',
                'subtask_score_state', 'subtask_score_revision', 'subtask_score_config')}
            generations = {pk: progress_generation(p) for pk, p in problems.items()}
            all_rows = Submission.objects.filter(pk__lte=cutoff, date__lte=started_at)
            full_published = (Q(status='D', offline_hidden=False) &
                (Q(is_pretested=False) | Q(batch_scoring__pretests=[])) &
                ~Q(contest_object__offline_mode=True) & ~Q(contest_object__has_hidden_subtasks=True) &
                (Q(contest_object__isnull=True) | Q(contest_object__end_time__lte=started_at)))
            rows = all_rows.filter(full_published)
            cumulative_ids = [pk for pk, p in problems.items() if p.subtask_score_state == 'ON']
            normal = (all_rows.exclude(problem_id__in=cumulative_ids).values('user_id', 'problem_id')
                      .annotate(points=Max(Case(When(full_published, then=F('points')),
                                               output_field=FloatField())))
                      .order_by('user_id', 'problem_id'))
            batch, total = [], 0
            for row in normal.iterator(chunk_size=2000):
                points = score_decimal(row['points'])
                # Even zero/failed/withheld pairs need a frozen zero baseline:
                # a later rejudge must not silently seed a higher initial mark.
                batch.append(StreakProgress(user_id=row['user_id'], problem_id=row['problem_id'],
                    generation=generations[row['problem_id']], best_points=points))
                if len(batch) == 1000:
                    StreakProgress.objects.bulk_create(batch, batch_size=1000)
                    total += len(batch)
                    batch = []
            StreakProgress.objects.bulk_create(batch, batch_size=1000)
            total += len(batch)
            for problem_id in cumulative_ids:
                problem = problems[problem_id]
                for user_id in all_rows.filter(problem_id=problem_id).order_by('user_id').values_list('user_id', flat=True).distinct():
                    points, subtasks = baseline_progress(user_id, problem, submissions=rows)
                    StreakProgress.objects.create(user_id=user_id, problem_id=problem_id,
                        generation=generations[problem_id], best_points=points, subtasks=subtasks)
                    total += 1
            for model in (StreakContribution, StreakDay, StreakRun, StreakSummary, StreakRebuildRequest):
                model.objects.all().delete()
            StreakProblemState.objects.update(pending=False)
            StreakEpoch.objects.create(pk=1, submission_id=cutoff, started_at=started_at)
        self.stdout.write(self.style.SUCCESS('Initialized %d baselines; zero earned days. Archive: %s' % (total, archive)))
