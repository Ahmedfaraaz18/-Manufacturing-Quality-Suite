from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('inspections', '0002_organizer_dataset'),
    ]

    operations = [
        migrations.CreateModel(
            name='QualityAlertConfiguration',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('batch_defect_rate_threshold', models.FloatField(default=0.25)),
                ('consecutive_high_defect_batches', models.PositiveSmallIntegerField(default=3)),
                ('failure_limit', models.PositiveIntegerField(default=5)),
                ('review_backlog_limit', models.PositiveIntegerField(default=10)),
                ('processing_backlog_limit', models.PositiveIntegerField(default=10)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'verbose_name': 'quality alert configuration'},
        ),
        migrations.CreateModel(
            name='QualityAlert',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('deduplication_key', models.CharField(max_length=128, unique=True)),
                ('kind', models.CharField(choices=[('batch_defect_rate', 'Batch defect rate'), ('consecutive_defects', 'Consecutive high-defect batches'), ('failure_backlog', 'Inspection failure backlog'), ('review_backlog', 'Supervisor review backlog'), ('processing_backlog', 'Processing backlog')], max_length=32)),
                ('severity', models.CharField(choices=[('warning', 'Warning'), ('critical', 'Critical')], max_length=16)),
                ('title', models.CharField(max_length=160)),
                ('message', models.TextField()),
                ('measured_value', models.FloatField(blank=True, null=True)),
                ('threshold_value', models.FloatField(blank=True, null=True)),
                ('is_read', models.BooleanField(default=False)),
                ('read_at', models.DateTimeField(blank=True, null=True)),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('batch', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='quality_alerts', to='inspections.inspectionbatch')),
            ],
            options={'ordering': ['resolved_at', '-created_at']},
        ),
        migrations.AddIndex(
            model_name='qualityalert',
            index=models.Index(fields=['resolved_at', '-created_at'], name='quality_alert_state_idx'),
        ),
    ]
