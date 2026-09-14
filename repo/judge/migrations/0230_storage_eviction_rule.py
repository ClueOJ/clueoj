from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0229_storage_usage_samples'),
    ]

    operations = [
        migrations.CreateModel(
            name='StorageEvictionRule',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, unique=True)),
                ('idle_hours', models.PositiveIntegerField(default=24)),
                ('max_size_bytes', models.BigIntegerField(blank=True, null=True)),
                ('enabled', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=django.utils.timezone.now)),
            ],
            options={
                'ordering': ['name'],
            },
        ),
    ]
