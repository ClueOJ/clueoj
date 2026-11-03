from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand

from judge.models import Problem, ProblemDataManifest
from judge.utils.remote_data import publish_manifest
from judge.utils.test_data_lock import locked_problem


class Command(BaseCommand):
    help = 'Publish manifests for locked, managed roots with usable local data. Does not rejudge submissions.'

    def add_arguments(self, parser):
        parser.add_argument('--problem', help='Limit to one problem code.')

    def handle(self, *args, **options):
        roots = Problem.objects.filter(is_locked=True, is_manually_managed=False, mirror_of__isnull=True)
        if options['problem']:
            roots = roots.filter(code=options['problem'])
        published = skipped = 0
        for problem_id in roots.values_list('pk', flat=True).iterator():
            try:
                with locked_problem(problem_id) as problem:
                    if (not problem.is_locked or problem.is_manually_managed or problem.is_mirror or
                            problem.has_external_problem or
                            ProblemDataManifest.objects.filter(problem_id=problem_id).exclude(sha256='').exists()):
                        skipped += 1
                        continue
                    publish_manifest(problem)
                    published += 1
            except (OSError, ValidationError) as exc:
                skipped += 1
                self.stderr.write('Skipped %s: %s' % (problem_id, exc))
        self.stdout.write('Published %s; skipped %s. Existing submission results are unchanged.' % (published, skipped))
