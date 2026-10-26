from django.db import migrations, models


def reset_legacy_batch_weights(apps, schema_editor):
    # Before weighted batches, the compiler ignored child points without clearing
    # them in the DB. Preserve the effective equal weights on the first upgrade.
    Case = apps.get_model('judge', 'ProblemTestCase')
    cases = Case.objects.using(schema_editor.connection.alias)
    dataset = None
    in_batch = False
    pending = []
    for pk, dataset_id, kind in cases.order_by('dataset_id', 'order', 'pk').values_list(
            'pk', 'dataset_id', 'type').iterator(chunk_size=1000):
        if dataset_id != dataset:
            dataset = dataset_id
            in_batch = False
        if kind == 'S':
            in_batch = True
        elif kind == 'E':
            in_batch = False
        elif kind == 'C' and in_batch:
            pending.append(pk)
        if len(pending) == 1000:
            cases.filter(pk__in=pending).update(points=None)
            pending = []
    if pending:
        cases.filter(pk__in=pending).update(points=None)


class Migration(migrations.Migration):
    dependencies = [('judge', '0244_per_batch_scoring')]

    operations = [
        migrations.RunPython(reset_legacy_batch_weights, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='problemtestcase',
            name='batch_type',
            field=models.CharField(choices=[('min', 'Min'), ('sum', 'Sum')], default='sum',
                                   max_length=3, verbose_name='batch scoring'),
        ),
    ]
