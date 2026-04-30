from django.db import migrations, models
import django.db.models.deletion


def backfill_archive_source_problem(apps, schema_editor):
    Problem = apps.get_model('judge', 'Problem')
    ProblemData = apps.get_model('judge', 'ProblemData')

    for data in ProblemData.objects.select_related('problem').all():
        if not data.zipfile:
            continue

        problem = data.problem
        source_problem_id = problem.id

        if problem.mirror_root_id:
            source_problem_id = problem.mirror_root_id
        elif problem.mirror_of_id:
            source_problem_id = problem.mirror_of_id
            seen = {problem.id}
            current_id = problem.mirror_of_id
            while current_id and current_id not in seen:
                seen.add(current_id)
                row = Problem.objects.filter(pk=current_id).values('id', 'mirror_of_id').first()
                if row is None:
                    break
                source_problem_id = row['id']
                current_id = row['mirror_of_id']

        ProblemData.objects.filter(pk=data.pk).update(archive_source_problem_id=source_problem_id)


class Migration(migrations.Migration):
    dependencies = [
        ('judge', '0219_problem_mirroring'),
    ]

    operations = [
        migrations.AddField(
            model_name='problemdata',
            name='archive_source_problem',
            field=models.ForeignKey(
                blank=True,
                help_text='Original problem that owns this data archive for download permission checks.',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='archive_source_for',
                to='judge.problem',
                verbose_name='archive source problem',
            ),
        ),
        migrations.RunPython(backfill_archive_source_problem, migrations.RunPython.noop),
    ]
