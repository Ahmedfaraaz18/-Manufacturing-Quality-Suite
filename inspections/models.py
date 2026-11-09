"""Round 2 schema: products, batches, inspections and supervisor reviews.

The original model output on an ``Inspection`` is immutable once completed. A supervisor's
decision lives in a separate ``InspectionReview`` row, so both are always available.
"""

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Count, Q


class Product(models.Model):
    product_id = models.CharField(
        max_length=32, unique=True,
        validators=[RegexValidator(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$', 'Use letters, digits, dot, dash or underscore.')],
        help_text='e.g. P-PLATE01',
    )
    name = models.CharField(max_length=128)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['product_id']

    def __str__(self):
        return f'{self.product_id} - {self.name}'


class DatasetSample(models.Model):
    """An organizer-provided evaluation image, deliberately separate from live inspections.

    ``ground_truth`` is operational metadata for the offline evaluator.  It is never joined to
    upload/inference code and is only surfaced to administrators in aggregate.
    """

    image_id = models.CharField(max_length=32, unique=True)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='dataset_samples')
    relative_path = models.CharField(max_length=255, unique=True)
    filename = models.CharField(max_length=255)
    sha256 = models.CharField(max_length=64)
    image_format = models.CharField(max_length=8, blank=True)
    image_width = models.PositiveIntegerField(null=True, blank=True)
    image_height = models.PositiveIntegerField(null=True, blank=True)
    file_size = models.PositiveIntegerField(null=True, blank=True)
    validation_status = models.CharField(max_length=16, default='valid')
    validation_reason = models.CharField(max_length=500, blank=True)
    ground_truth = models.CharField(max_length=16, choices=(('Normal', 'Normal'), ('Defective', 'Defective')))
    imported_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['image_id']

    def __str__(self):
        return f'{self.image_id} ({self.filename})'


class ValidationAsset(models.Model):
    """Inventory of the intentionally invalid organizer files used to test upload validation."""

    filename = models.CharField(max_length=255, unique=True)
    relative_path = models.CharField(max_length=255, unique=True)
    sha256 = models.CharField(max_length=64)
    file_size = models.PositiveIntegerField()
    validation_status = models.CharField(max_length=16, default='invalid')
    validation_reason = models.CharField(max_length=500)
    imported_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['filename']


class InspectionBatchQuerySet(models.QuerySet):
    def with_counts(self):
        """Per-status counts computed in SQL, so they can never drift from the real item rows."""
        S = Inspection.Status
        return self.annotate(
            queued_count=Count('inspections', filter=Q(inspections__status=S.QUEUED)),
            processing_count=Count('inspections', filter=Q(inspections__status=S.PROCESSING)),
            completed_count=Count('inspections', filter=Q(inspections__status=S.COMPLETED)),
            failed_count=Count('inspections', filter=Q(inspections__status=S.FAILED)),
        )


class InspectionBatch(models.Model):
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='batches')
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='inspection_batches')
    total_files = models.PositiveSmallIntegerField()
    client_token = models.CharField(
        max_length=64, blank=True,
        help_text='Browser-generated token that makes a repeated submission idempotent.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = InspectionBatchQuerySet.as_manager()

    class Meta:
        ordering = ['-created_at', '-id']
        verbose_name_plural = 'inspection batches'
        indexes = [models.Index(fields=['submitted_by', '-created_at'], name='batch_user_created_idx')]
        constraints = [
            models.CheckConstraint(condition=Q(total_files__gte=1), name='batch_total_files_gte_1'),
            models.UniqueConstraint(
                fields=['submitted_by', 'client_token'], condition=~Q(client_token=''),
                name='uniq_batch_user_client_token',
            ),
        ]

    def __str__(self):
        return f'Batch {self.pk} ({self.total_files} files)'

    def status_counts(self):
        rows = self.inspections.values('status').annotate(n=Count('id'))
        counts = {s: 0 for s in Inspection.Status.values}
        counts.update({row['status']: row['n'] for row in rows})
        return counts


class ImmutablePredictionError(RuntimeError):
    """Raised when code tries to alter the original output of a completed inspection."""


class Inspection(models.Model):
    class Status(models.TextChoices):
        QUEUED = 'queued', 'Queued'
        PROCESSING = 'processing', 'Processing'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    class PredictedClass(models.TextChoices):
        NORMAL = 'Normal', 'Normal'
        DEFECTIVE = 'Defective', 'Defective'

    # Original model output; frozen once the inspection is COMPLETED.
    IMMUTABLE_FIELDS = ('status', 'predicted_class', 'confidence', 'defect_probability', 'model_version',
                        'completed_at')

    batch = models.ForeignKey(InspectionBatch, on_delete=models.PROTECT, related_name='inspections')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='inspections')
    submitting_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='inspections')
    image = models.FileField(upload_to='inspections/%Y/%m/', blank=True, max_length=255)
    original_filename = models.CharField(max_length=255, blank=True)
    image_format = models.CharField(max_length=8, blank=True)
    image_width = models.PositiveIntegerField(null=True, blank=True)
    image_height = models.PositiveIntegerField(null=True, blank=True)
    file_size = models.PositiveIntegerField(null=True, blank=True)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    predicted_class = models.CharField(max_length=16, choices=PredictedClass.choices, null=True, blank=True)
    confidence = models.FloatField(null=True, blank=True)
    defect_probability = models.FloatField(null=True, blank=True)
    model_version = models.CharField(max_length=64, blank=True)
    failure_reason = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['submitting_user', '-created_at'], name='insp_user_created_idx'),
            models.Index(fields=['status', '-created_at'], name='insp_status_created_idx'),
            models.Index(fields=['predicted_class', '-created_at'], name='insp_class_created_idx'),
            models.Index(fields=['product', '-created_at'], name='insp_product_created_idx'),
            models.Index(fields=['batch', 'id'], name='insp_batch_idx'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(defect_probability__isnull=True) | Q(defect_probability__gte=0, defect_probability__lte=1),
                name='insp_defect_probability_range',
            ),
            models.CheckConstraint(
                condition=Q(confidence__isnull=True) | Q(confidence__gte=0, confidence__lte=1),
                name='insp_confidence_range',
            ),
            # A completed inspection always carries a full prediction and the model version.
            models.CheckConstraint(
                condition=~Q(status='completed') | (
                    Q(predicted_class__isnull=False, confidence__isnull=False, defect_probability__isnull=False,
                      completed_at__isnull=False) & ~Q(model_version='')
                ),
                name='insp_completed_has_prediction',
            ),
            # Queued, processing and failed items never carry a prediction.
            models.CheckConstraint(
                condition=Q(status='completed') | Q(
                    predicted_class__isnull=True, confidence__isnull=True, defect_probability__isnull=True),
                name='insp_incomplete_has_no_prediction',
            ),
            models.CheckConstraint(
                condition=~Q(status='failed') | ~Q(failure_reason=''),
                name='insp_failed_has_reason',
            ),
            # Only failed attempts may lack a stored image (rejected files are never kept).
            models.CheckConstraint(condition=Q(status='failed') | ~Q(image=''), name='insp_image_required'),
        ]

    def __str__(self):
        return f'Inspection {self.pk} ({self.status})'

    @property
    def is_finished(self):
        return self.status in (self.Status.COMPLETED, self.Status.FAILED)

    @property
    def confidence_percent(self):
        return None if self.confidence is None else round(self.confidence * 100, 2)

    @property
    def defect_probability_percent(self):
        return None if self.defect_probability is None else round(self.defect_probability * 100, 2)

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')
        touched = set(self.IMMUTABLE_FIELDS) if update_fields is None else set(self.IMMUTABLE_FIELDS) & set(update_fields)
        if self.pk and not self._state.adding and touched:
            stored = type(self).objects.filter(pk=self.pk).values(*self.IMMUTABLE_FIELDS).first()
            if stored and stored['status'] == self.Status.COMPLETED:
                changed = [f for f in self.IMMUTABLE_FIELDS if stored[f] != getattr(self, f)]
                if changed:
                    raise ImmutablePredictionError(
                        f'The original model output of a completed inspection cannot change ({", ".join(changed)}).'
                    )
        super().save(*args, **kwargs)


class InspectionReview(models.Model):
    """A supervisor's decision, stored apart from the immutable model output."""

    inspection = models.OneToOneField(Inspection, on_delete=models.PROTECT, related_name='review')
    original_predicted_class = models.CharField(
        max_length=16, choices=Inspection.PredictedClass.choices,
        help_text='Snapshot of the model prediction that was reviewed.',
    )
    reviewed_class = models.CharField(max_length=16, choices=Inspection.PredictedClass.choices)
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='inspection_reviews')
    reviewed_at = models.DateTimeField(auto_now_add=True, db_index=True)
    comment = models.TextField(blank=True)

    class Meta:
        ordering = ['-reviewed_at', '-id']
        constraints = [
            models.CheckConstraint(condition=Q(reviewed_class__in=['Normal', 'Defective']), name='review_class_valid'),
        ]

    def __str__(self):
        return f'Review of inspection {self.inspection_id}: {self.reviewed_class}'

    @property
    def is_correction(self):
        return self.reviewed_class != self.original_predicted_class


class CorrectiveAction(models.Model):
    """Optional supervisor-owned follow-up for an existing inspection review."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        IN_PROGRESS = 'in_progress', 'In Progress'
        COMPLETED = 'completed', 'Completed'

    review = models.OneToOneField(InspectionReview, on_delete=models.PROTECT, related_name='corrective_action')
    observed_issue = models.TextField(max_length=1000)
    recommended_action = models.TextField(max_length=1000)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='corrective_actions')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at', '-id']

    def __str__(self):
        return f'Corrective action for inspection {self.review.inspection_id}'


class QualityAlertConfiguration(models.Model):
    """Singleton, administrator-managed thresholds for operational quality alerts."""

    batch_defect_rate_threshold = models.FloatField(default=0.25)
    consecutive_high_defect_batches = models.PositiveSmallIntegerField(default=3)
    failure_limit = models.PositiveIntegerField(default=5)
    review_backlog_limit = models.PositiveIntegerField(default=10)
    processing_backlog_limit = models.PositiveIntegerField(default=10)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'quality alert configuration'

    @classmethod
    def current(cls):
        configuration, _created = cls.objects.get_or_create(pk=1)
        return configuration


class QualityAlert(models.Model):
    """Persisted operational alert state; source values are calculated from live records."""

    class Kind(models.TextChoices):
        BATCH_DEFECT_RATE = 'batch_defect_rate', 'Batch defect rate'
        CONSECUTIVE_DEFECTS = 'consecutive_defects', 'Consecutive high-defect batches'
        FAILURE_BACKLOG = 'failure_backlog', 'Inspection failure backlog'
        REVIEW_BACKLOG = 'review_backlog', 'Supervisor review backlog'
        PROCESSING_BACKLOG = 'processing_backlog', 'Processing backlog'

    class Severity(models.TextChoices):
        WARNING = 'warning', 'Warning'
        CRITICAL = 'critical', 'Critical'

    deduplication_key = models.CharField(max_length=128, unique=True)
    kind = models.CharField(max_length=32, choices=Kind.choices)
    severity = models.CharField(max_length=16, choices=Severity.choices)
    title = models.CharField(max_length=160)
    message = models.TextField()
    measured_value = models.FloatField(null=True, blank=True)
    threshold_value = models.FloatField(null=True, blank=True)
    batch = models.ForeignKey(InspectionBatch, null=True, blank=True, on_delete=models.SET_NULL, related_name='quality_alerts')
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['resolved_at', '-created_at']
        indexes = [models.Index(fields=['resolved_at', '-created_at'], name='quality_alert_state_idx')]

    @property
    def is_active(self):
        return self.resolved_at is None


class QualityIncident(models.Model):
    """Round 3 Quality Incident linked to an eligible completed Defective Inspection.

    Enforces exactly one incident per inspection through a database-level unique constraint
    (OneToOneField). Preserves immutable original AI prediction, product metadata, grounded
    summary, suggested actions, verifiable SOP citations, evidence status, and supervisor review.
    """

    class EvidenceStatus(models.TextChoices):
        SUFFICIENT = 'sufficient', 'Sufficient evidence'
        INSUFFICIENT = 'insufficient', 'Insufficient evidence'

    class ReviewStatus(models.TextChoices):
        PENDING = 'pending', 'Pending review'
        APPROVED = 'approved', 'Approved for follow-up'
        NEEDS_INFO = 'needs_info', 'Needs more information'

    inspection = models.OneToOneField(Inspection, on_delete=models.PROTECT, related_name='incident')
    product_id = models.CharField(max_length=32, db_index=True)
    original_predicted_class = models.CharField(max_length=16)
    original_model_version = models.CharField(max_length=64, blank=True)
    confidence = models.FloatField(null=True, blank=True)
    defect_probability = models.FloatField(null=True, blank=True)

    summary = models.TextField(help_text='Factual incident summary from inspection metadata')
    suggested_actions = models.JSONField(default=list, blank=True, help_text='List of grounded suggested actions')
    source_references = models.JSONField(default=list, blank=True, help_text='Retrieved SOP sources and citations')

    evidence_status = models.CharField(
        max_length=24, choices=EvidenceStatus.choices, default=EvidenceStatus.INSUFFICIENT, db_index=True
    )
    evidence_failure_reason = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='created_quality_incidents'
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    review_status = models.CharField(
        max_length=24, choices=ReviewStatus.choices, default=ReviewStatus.PENDING, db_index=True
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name='reviewed_quality_incidents'
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    supervisor_comment = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['created_by', '-created_at'], name='incident_user_created_idx'),
            models.Index(fields=['review_status', '-created_at'], name='incident_rev_status_idx'),
            models.Index(fields=['evidence_status', '-created_at'], name='incident_evi_status_idx'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(evidence_status__in=['sufficient', 'insufficient']),
                name='incident_evidence_status_valid',
            ),
            models.CheckConstraint(
                condition=Q(review_status__in=['pending', 'approved', 'needs_info']),
                name='incident_review_status_valid',
            ),
            # Insufficient-evidence incidents cannot receive 'approved' review status
            models.CheckConstraint(
                condition=~Q(review_status='approved') | Q(evidence_status='sufficient'),
                name='incident_approved_requires_sufficient',
            ),
            # A review decision requires a non-empty supervisor comment
            models.CheckConstraint(
                condition=Q(review_status='pending') | ~Q(supervisor_comment=''),
                name='incident_reviewed_has_comment',
            ),
        ]

    def __str__(self):
        return f'Incident #{self.pk} (Inspection #{self.inspection_id} - {self.product_id})'

    @property
    def is_reviewed(self):
        return self.review_status != self.ReviewStatus.PENDING

    @property
    def can_approve(self):
        return self.evidence_status == self.EvidenceStatus.SUFFICIENT

    @property
    def confidence_percent(self):
        return None if self.confidence is None else round(self.confidence * 100, 2)

    @property
    def defect_probability_percent(self):
        return None if self.defect_probability is None else round(self.defect_probability * 100, 2)
