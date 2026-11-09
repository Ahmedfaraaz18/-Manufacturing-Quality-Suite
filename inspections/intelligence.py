"""Database-backed analytics and operational alerts for the Quality Intelligence Centre."""

from datetime import datetime, time

from django.db.models import Count, ExpressionWrapper, F, FloatField, Q, Value
from django.db.models.functions import NullIf, TruncDate
from django.utils import timezone

from .models import Inspection, InspectionBatch, QualityAlert, QualityAlertConfiguration


def _date_bounds(params):
    """Return inclusive timezone-aware date bounds; malformed filters are deliberately ignored."""
    def parse(name, latest):
        try:
            value = datetime.strptime(params.get(name, ''), '%Y-%m-%d').date()
        except (TypeError, ValueError):
            return None
        return timezone.make_aware(datetime.combine(value, time.max if latest else time.min))

    return parse('date_from', False), parse('date_to', True)


def filtered_inspections(params):
    queryset = Inspection.objects.all()
    start, end = _date_bounds(params)
    if start:
        queryset = queryset.filter(created_at__gte=start)
    if end:
        queryset = queryset.filter(created_at__lte=end)
    return queryset


def filtered_batches(params):
    queryset = InspectionBatch.objects.select_related('product', 'submitted_by')
    start, end = _date_bounds(params)
    if start:
        queryset = queryset.filter(created_at__gte=start)
    if end:
        queryset = queryset.filter(created_at__lte=end)
    query = (params.get('batch_q') or '').strip()
    if query:
        condition = Q(product__product_id__icontains=query) | Q(product__name__icontains=query)
        if query.isdigit():
            condition |= Q(pk=int(query))
        queryset = queryset.filter(condition)
    return queryset


def batch_metrics_queryset(params=None):
    """One annotated row per batch. Counts are mutually exclusive inspection states."""
    queryset = filtered_batches(params or {})
    corrected = Q(inspections__review__isnull=False) & ~Q(
        inspections__review__reviewed_class=F('inspections__review__original_predicted_class')
    )
    queryset = queryset.annotate(
        completed_count=Count('inspections', filter=Q(inspections__status=Inspection.Status.COMPLETED)),
        failed_count=Count('inspections', filter=Q(inspections__status=Inspection.Status.FAILED)),
        queued_count=Count('inspections', filter=Q(inspections__status=Inspection.Status.QUEUED)),
        processing_count=Count('inspections', filter=Q(inspections__status=Inspection.Status.PROCESSING)),
        normal_count=Count('inspections', filter=Q(inspections__predicted_class=Inspection.PredictedClass.NORMAL)),
        defective_count=Count('inspections', filter=Q(inspections__predicted_class=Inspection.PredictedClass.DEFECTIVE)),
        reviewed_count=Count('inspections__review'),
        corrected_count=Count('inspections__review', filter=corrected),
    ).annotate(
        defect_rate=ExpressionWrapper(
            Value(100.0) * F('defective_count') / NullIf(F('completed_count'), Value(0)), output_field=FloatField(),
        )
    )
    return queryset


def operational_kpis(params):
    inspections = filtered_inspections(params)
    corrected = Q(review__isnull=False) & ~Q(review__reviewed_class=F('review__original_predicted_class'))
    values = inspections.aggregate(
        total_inspections=Count('id'),
        completed_inspections=Count('id', filter=Q(status=Inspection.Status.COMPLETED)),
        queued_inspections=Count('id', filter=Q(status=Inspection.Status.QUEUED)),
        processing_inspections=Count('id', filter=Q(status=Inspection.Status.PROCESSING)),
        failed_inspections=Count('id', filter=Q(status=Inspection.Status.FAILED)),
        normal_predictions=Count('id', filter=Q(predicted_class=Inspection.PredictedClass.NORMAL)),
        defective_predictions=Count('id', filter=Q(predicted_class=Inspection.PredictedClass.DEFECTIVE)),
        awaiting_review=Count('id', filter=Q(status=Inspection.Status.COMPLETED, review__isnull=True)),
        reviewed_inspections=Count('id', filter=Q(review__isnull=False)),
        corrected_inspections=Count('id', filter=corrected),
    )
    values['total_batches'] = filtered_batches(params).count()
    return values


def agreement_metrics(params):
    reviews = filtered_inspections(params).filter(status=Inspection.Status.COMPLETED, review__isnull=False)
    agreement = Q(review__reviewed_class=F('review__original_predicted_class'))
    values = reviews.aggregate(
        reviewed=Count('id'),
        confirmed=Count('id', filter=agreement),
        corrected=Count('id', filter=~agreement),
    )
    values['disagreement'] = values['corrected']
    values['agreement_rate'] = (100 * values['confirmed'] / values['reviewed']) if values['reviewed'] else None
    values['correction_rate'] = (100 * values['corrected'] / values['reviewed']) if values['reviewed'] else None
    values['completed'] = filtered_inspections(params).filter(status=Inspection.Status.COMPLETED).count()
    return values


def trend_data(params):
    inspections = filtered_inspections(params)
    volume = list(inspections.annotate(day=TruncDate('created_at')).values('day').annotate(
        total=Count('id'), normal=Count('id', filter=Q(predicted_class=Inspection.PredictedClass.NORMAL)),
        defective=Count('id', filter=Q(predicted_class=Inspection.PredictedClass.DEFECTIVE)),
        failed=Count('id', filter=Q(status=Inspection.Status.FAILED)),
        submitted_for_review=Count('id', filter=Q(status=Inspection.Status.COMPLETED)),
    ).order_by('day'))
    review_rows = list(inspections.filter(review__isnull=False).annotate(day=TruncDate('review__reviewed_at')).values('day').annotate(
        reviewed=Count('id'), corrected=Count('id', filter=~Q(review__reviewed_class=F('review__original_predicted_class'))),
    ).order_by('day'))
    reviews_by_day = {row['day']: row for row in review_rows}
    backlog = 0
    results = []
    for row in volume:
        reviewed = reviews_by_day.get(row['day'], {}).get('reviewed', 0)
        corrected = reviews_by_day.get(row['day'], {}).get('corrected', 0)
        backlog = max(0, backlog + row['submitted_for_review'] - reviewed)
        results.append({
            'date': row['day'].isoformat(), 'volume': row['total'], 'normal': row['normal'],
            'defective': row['defective'], 'failed': row['failed'], 'reviewed': reviewed,
            'correction_rate': round(100 * corrected / reviewed, 2) if reviewed else None,
            'review_backlog': backlog,
        })
    return results


def _upsert_alert(*, key, kind, severity, title, message, measured, threshold, batch=None):
    defaults = {
        'kind': kind, 'severity': severity, 'title': title, 'message': message,
        'measured_value': measured, 'threshold_value': threshold, 'batch': batch,
    }
    alert, created = QualityAlert.objects.get_or_create(deduplication_key=key, defaults=defaults)
    if not created:
        for field, value in defaults.items():
            setattr(alert, field, value)
        if alert.resolved_at:
            alert.resolved_at = None
            alert.is_read = False
            alert.read_at = None
        alert.save()
    return alert


def refresh_operational_alerts():
    """Create/update only threshold breaches and resolve alerts whose live condition is gone."""
    configuration = QualityAlertConfiguration.current()
    active_keys = []
    threshold_percent = configuration.batch_defect_rate_threshold * 100
    batches = batch_metrics_queryset().filter(completed_count__gt=0, defect_rate__gt=threshold_percent)
    for batch in batches:
        key = f'batch-defect-rate:{batch.pk}'
        active_keys.append(key)
        severity = QualityAlert.Severity.CRITICAL if batch.defect_rate >= threshold_percent * 1.5 else QualityAlert.Severity.WARNING
        _upsert_alert(
            key=key, kind=QualityAlert.Kind.BATCH_DEFECT_RATE, severity=severity,
            title=f'High defect rate in batch #{batch.pk}',
            message=f'{batch.defective_count} of {batch.completed_count} completed inspections are Defective ({batch.defect_rate:.2f}%).',
            measured=batch.defect_rate, threshold=threshold_percent, batch=batch,
        )

    completed_batches = list(batch_metrics_queryset().filter(
        completed_count__gt=0, queued_count=0, processing_count=0,
    ).order_by('-created_at')[:configuration.consecutive_high_defect_batches])
    if len(completed_batches) == configuration.consecutive_high_defect_batches and all(
        batch.defect_rate > threshold_percent for batch in completed_batches
    ):
        key = 'consecutive-high-defect-batches'
        active_keys.append(key)
        _upsert_alert(
            key=key, kind=QualityAlert.Kind.CONSECUTIVE_DEFECTS, severity=QualityAlert.Severity.CRITICAL,
            title='Consecutive high-defect batches',
            message=f'The latest {len(completed_batches)} completed batches exceed the configured defect-rate threshold.',
            measured=len(completed_batches), threshold=configuration.consecutive_high_defect_batches,
        )

    inspection_counts = Inspection.objects.aggregate(
        failed=Count('id', filter=Q(status=Inspection.Status.FAILED)),
        review_backlog=Count('id', filter=Q(status=Inspection.Status.COMPLETED, review__isnull=True)),
        processing_backlog=Count('id', filter=Q(status__in=[Inspection.Status.QUEUED, Inspection.Status.PROCESSING])),
    )
    for key, kind, title, value, limit in (
        ('inspection-failure-backlog', QualityAlert.Kind.FAILURE_BACKLOG, 'Inspection failure backlog', inspection_counts['failed'], configuration.failure_limit),
        ('supervisor-review-backlog', QualityAlert.Kind.REVIEW_BACKLOG, 'Supervisor review backlog', inspection_counts['review_backlog'], configuration.review_backlog_limit),
        ('processing-backlog', QualityAlert.Kind.PROCESSING_BACKLOG, 'Processing backlog', inspection_counts['processing_backlog'], configuration.processing_backlog_limit),
    ):
        if value > limit:
            active_keys.append(key)
            severity = QualityAlert.Severity.CRITICAL if limit == 0 or value >= limit * 2 else QualityAlert.Severity.WARNING
            _upsert_alert(
                key=key, kind=kind, severity=severity, title=title,
                message=f'Current count is {value}; configured alert limit is {limit}.',
                measured=value, threshold=limit,
            )

    QualityAlert.objects.filter(resolved_at__isnull=True).exclude(deduplication_key__in=active_keys).update(resolved_at=timezone.now())
    return QualityAlert.objects.select_related('batch').filter(resolved_at__isnull=True).order_by('-created_at')
