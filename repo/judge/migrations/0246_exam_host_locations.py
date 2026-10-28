from django.db import migrations, models


def copy_scope_options(apps, schema_editor):
    # Copy the option catalog only. Exam assignments remain blank until edited.
    scopes = apps.get_model('judge', 'ExamProvince').objects.using(schema_editor.connection.alias)
    hosts = apps.get_model('judge', 'ExamHostLocation').objects.using(schema_editor.connection.alias)
    for option in scopes.values('name', 'sort_order', 'is_active').iterator():
        name = option.pop('name')
        hosts.get_or_create(name=name, defaults=option)


class Migration(migrations.Migration):
    dependencies = [('judge', '0245_default_sum_batches')]

    operations = [
        migrations.CreateModel(
            name='ExamHostLocation',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(db_index=True, max_length=255, unique=True, verbose_name='host location name')),
                ('sort_order', models.IntegerField(db_index=True, default=0, verbose_name='sort order')),
                ('is_active', models.BooleanField(db_index=True, default=True, verbose_name='active')),
            ],
            options={
                'verbose_name': 'exam host location',
                'verbose_name_plural': 'exam host locations',
                'ordering': ('sort_order', 'name'),
            },
        ),
        migrations.AddField(
            model_name='examtag', name='host_location',
            field=models.CharField(blank=True, default='', max_length=255, verbose_name='host location'),
        ),
        migrations.AlterField(
            model_name='examtag', name='province',
            field=models.CharField(blank=True, db_index=True, max_length=64, verbose_name='participant scope'),
        ),
        migrations.AlterField(
            model_name='examprovince', name='name',
            field=models.CharField(db_index=True, max_length=64, unique=True, verbose_name='scope name'),
        ),
        migrations.AlterModelOptions(
            name='examprovince',
            options={'ordering': ('sort_order', 'name'), 'verbose_name': 'exam participant scope',
                     'verbose_name_plural': 'exam participant scopes'},
        ),
        migrations.RunPython(copy_scope_options, migrations.RunPython.noop),
    ]
