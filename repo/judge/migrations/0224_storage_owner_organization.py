from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0223_problem_is_official_test'),
    ]

    operations = [
        migrations.AddField(
            model_name='problem',
            name='storage_owner_organization',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.SET_NULL,
                related_name='storage_owned_problems',
                to='judge.organization',
                help_text='Organization that owns this problem for storage quota accounting. '
                          'Null for system problems. Do not use Problem.organizations for quota.',
            ),
        ),
    ]