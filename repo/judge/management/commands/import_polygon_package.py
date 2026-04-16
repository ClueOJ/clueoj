from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand
from django.urls import reverse
from django.utils import translation

from judge.utils.polygon_import import import_polygon_package, resolve_profiles


class Command(BaseCommand):
    help = 'import Codeforces Polygon full package'

    def add_arguments(self, parser):
        parser.add_argument('package', help='path to package in zip format')
        parser.add_argument('code', help='problem code')
        parser.add_argument('--authors', help='username of problem author', nargs='*')
        parser.add_argument('--curators', help='username of problem curator', nargs='*')
        parser.add_argument('--do-update', action='store_true', help='update existing problem if code already exists')
        parser.add_argument(
            '--append-main-solution-to-tutorial',
            action='store_true',
            default=None,
            help='append main solution from Polygon package to tutorial',
        )

    def handle(self, *args, **options):
        # Force using English
        translation.activate('en')

        problem = import_polygon_package(
            options['package'],
            problem_code=options['code'],
            authors=resolve_profiles(options['authors'] or []),
            curators=resolve_profiles(options['curators'] or []),
            interactive=True,
            do_update=options['do_update'],
            append_main_solution_to_tutorial=options['append_main_solution_to_tutorial'],
        )

        problem_url = 'https://' + Site.objects.first().domain + reverse('problem_detail', args=[problem.code])
        self.stdout.write(f'Imported successfully. View problem at {problem_url}')
