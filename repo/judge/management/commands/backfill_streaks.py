import time

from django.core.management.base import BaseCommand, CommandError

from judge.models import Profile, StreakProblemState, StreakRebuildRequest
from judge.tasks.streaks import drain_streak_work
from judge.utils.streaks import enabled, queue_scope


class Command(BaseCommand):
    help = 'Queue historical streak rebuilds in bounded profile batches; dry-run by default.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--after-user', type=int, default=0)
        parser.add_argument('--limit', type=int, default=100)
        parser.add_argument('--user', type=int)
        parser.add_argument('--drain', action='store_true', help='Process queued work synchronously with pauses.')
        parser.add_argument('--max-batches', type=int, default=20)
        parser.add_argument('--pause', type=float, default=1.0)

    def handle(self, *args, **options):
        if not 1 <= options['limit'] <= 1000 or options['pause'] < 0 or options['max_batches'] < 1:
            raise CommandError('Use limit 1..1000, a nonnegative pause and positive max-batches.')
        profiles = Profile.objects.filter(pk__gt=options['after_user']).order_by('pk')
        if options['user']:
            profiles = profiles.filter(pk=options['user'])
        ids = list(profiles.values_list('pk', flat=True)[:options['limit']])
        self.stdout.write('Profiles: %d; next --after-user %d' % (len(ids), ids[-1] if ids else options['after_user']))
        if not options['apply']:
            self.stdout.write('Dry run: no streak data changed. Historical eligibility and timezone use current metadata.')
            return
        if not enabled():
            raise CommandError('Apply migrations and enable STREAKS_ENABLED first.')
        for user_id in ids:
            queue_scope('user', user_id)
        if options['drain']:
            for _ in range(options['max_batches']):
                drain_streak_work.run()
                if not StreakRebuildRequest.objects.exists() and not StreakProblemState.objects.filter(pending=True).exists():
                    break
                time.sleep(options['pause'])
        self.stdout.write('Queued. Repeating this command is safe; workers resume durable pending work.')
