from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('judge', '0257_problem_invocations'),
    ]

    operations = [
        # Keep the separate problem FK index: MariaDB may otherwise replace it
        # with this unique index and refuse to drop the constraint on rollback.
        migrations.AddConstraint(
            model_name='probleminvocationsource',
            constraint=models.UniqueConstraint(
                fields=('filename', 'problem'), name='invocation_source_unique_filename'),
        ),
    ]
