from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Retired: achievement streaks never backfill historical days.'

    def handle(self, *args, **options):
        raise CommandError('Historical streak backfill is retired. Use initialize_streaks to seed score baselines once.')
