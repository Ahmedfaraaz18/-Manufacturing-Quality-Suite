"""Business logic for uploads, background processing and supervisor reviews."""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time

from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from .inference import InferenceError, predict_image
from .models import CorrectiveAction, Inspection, InspectionBatch, InspectionReview, Product
from .validators import ImageValidationError, clean_filename, validate_image_upload

logger = logging.getLogger(__name__)

BROKER_DOWN_REASON = 'Background processing is currently unavailable. Please submit this image again shortly.'


class BatchSizeError(ValueError):
    """The request as a whole is unacceptable (no files, or too many)."""


class ReviewError(Exception):
    def __init__(self, message, status_code=409):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class BatchResult:
    batch: InspectionBatch
    inspections: list = field(default_factory=list)
    duplicate: bool = False


# ---------------------------------------------------------------- uploads

def create_batch(user, product: Product, files, client_token: str = '') -> BatchResult:
    """Validate every file independently, persist one record per file, then queue the valid ones.

    A rejected file becomes a FAILED record (no image is kept and the classifier never sees it);
    it never prevents the remaining files from being queued.
    """
    from django.conf import settings

    files = list(files)
    if not files:
        raise BatchSizeError('No files were submitted. Choose between 1 and 10 images.')
    if len(files) > settings.INSPECTION_MAX_BATCH_FILES:
        raise BatchSizeError(f'A batch may contain at most {settings.INSPECTION_MAX_BATCH_FILES} images; {len(files)} were submitted.')

    client_token = (client_token or '').strip()[:64]
    if client_token:
        existing = InspectionBatch.objects.filter(submitted_by=user, client_token=client_token).first()
        if existing:
            return BatchResult(existing, list(existing.inspections.order_by('id')), duplicate=True)

    saved_names = []
    try:
        with transaction.atomic():
            batch = InspectionBatch.objects.create(
                product=product, submitted_by=user, total_files=len(files), client_token=client_token,
            )
            inspections = []
            for uploaded in files:
                inspection = Inspection(
                    batch=batch, product=product, submitting_user=user,
                    original_filename=clean_filename(getattr(uploaded, 'name', '')),
                )
                try:
                    valid = validate_image_upload(uploaded)
                except ImageValidationError as exc:
                    inspection.status = Inspection.Status.FAILED
                    inspection.failure_reason = str(exc)
                    inspection.completed_at = timezone.now()
                else:
                    inspection.image.save(f'{uuid.uuid4().hex}.{valid.extension}', ContentFile(valid.data), save=False)
                    saved_names.append(inspection.image.name)
                    inspection.image_format = valid.format
                    inspection.image_width, inspection.image_height = valid.width, valid.height
                    inspection.file_size = valid.size
                inspection.save()
                inspections.append(inspection)
            queued_ids = [i.pk for i in inspections if i.status == Inspection.Status.QUEUED]
            # Publishing before commit lets a fast worker read a row which does not yet exist.
            # Keep publishing outside the transaction, but contain every callback failure: an
            # HTTP request must never become a 500 after its inspection rows have committed.
            transaction.on_commit(lambda ids=tuple(queued_ids): _publish_queued(ids))
    except IntegrityError:
        # Two identical submissions raced; the unique token guarantees only one batch exists.
        _discard_files(saved_names)
        existing = InspectionBatch.objects.filter(submitted_by=user, client_token=client_token).first() if client_token else None
        if existing:
            return BatchResult(existing, list(existing.inspections.order_by('id')), duplicate=True)
        raise
    except Exception:
        _discard_files(saved_names)
        raise
    return BatchResult(batch, inspections)


def _discard_files(names):
    from django.core.files.storage import default_storage

    for name in names:
        try:
            default_storage.delete(name)
        except Exception:  # best-effort cleanup of files from a rolled-back transaction
            logger.warning('Could not remove orphaned upload %s', name)


def enqueue_inspection(inspection_id: int) -> bool:
    """Hand an inspection to Celery. If the broker is down the item is failed, not left queued forever."""
    from .tasks import run_inspection

    try:
        run_inspection.delay(inspection_id)
        return True
    except Exception:
        logger.exception('Could not queue inspection %s', inspection_id)
        Inspection.objects.filter(pk=inspection_id, status=Inspection.Status.QUEUED).update(
            status=Inspection.Status.FAILED, failure_reason=BROKER_DOWN_REASON, completed_at=timezone.now(),
        )
        return False


def _publish_queued(inspection_ids) -> None:
    """Best-effort post-commit dispatcher, isolated per item and safe for on_commit()."""
    for inspection_id in inspection_ids:
        try:
            enqueue_inspection(inspection_id)
        except Exception:
            # ``enqueue_inspection`` already handles normal broker failures.  This final guard
            # covers an unforeseen callback/storage error so a committed batch remains visible
            # and actionable rather than returning a misleading server error.
            logger.exception('Unexpected post-commit dispatch failure for inspection %s', inspection_id)
            try:
                fail_inspection(inspection_id, BROKER_DOWN_REASON)
            except Exception:
                logger.exception('Could not mark inspection %s failed after dispatch failure', inspection_id)


# ------------------------------------------------------- background work

def process_inspection(inspection_id: int) -> str:
    """Run the classifier for one inspection. Safe to call repeatedly: a completed result is never rewritten."""
    with transaction.atomic():
        inspection = Inspection.objects.select_for_update().filter(pk=inspection_id).first()
        if inspection is None:
            return 'missing'
        if inspection.status not in (Inspection.Status.QUEUED, Inspection.Status.PROCESSING):
            return inspection.status  # already completed/failed: nothing to do
        inspection.status = Inspection.Status.PROCESSING
        inspection.started_at = inspection.started_at or timezone.now()
        inspection.save(update_fields=['status', 'started_at'])
        image_path = inspection.image.path

    try:
        result = predict_image(image_path)
    except InferenceError as exc:
        return fail_inspection(inspection_id, exc.user_message)
    except Exception:
        logger.exception('Unexpected error while processing inspection %s', inspection_id)
        return fail_inspection(inspection_id, InferenceError.user_message)

    # The conditional UPDATE only succeeds while the row is still PROCESSING, so a retry or a
    # duplicate delivery can never overwrite an original prediction that is already stored.
    updated = Inspection.objects.filter(pk=inspection_id, status=Inspection.Status.PROCESSING).update(
        status=Inspection.Status.COMPLETED, completed_at=timezone.now(), failure_reason='', **result,
    )
    return Inspection.Status.COMPLETED if updated else 'skipped'


def fail_inspection(inspection_id: int, reason: str) -> str:
    updated = Inspection.objects.filter(
        pk=inspection_id, status__in=[Inspection.Status.QUEUED, Inspection.Status.PROCESSING],
    ).update(status=Inspection.Status.FAILED, failure_reason=reason[:500], completed_at=timezone.now())
    return Inspection.Status.FAILED if updated else 'skipped'


# ----------------------------------------------------------------- reviews

def submit_review(inspection_id: int, reviewer, reviewed_class: str, comment: str = '') -> InspectionReview:
    """Record a supervisor's decision. Role is re-checked here so no caller can skip it."""
    if not getattr(reviewer, 'is_supervisor', False):
        raise PermissionDenied('Only supervisors may review inspections.')
    if reviewed_class not in Inspection.PredictedClass.values:
        raise ReviewError('Choose Normal or Defective.', 400)
    try:
        with transaction.atomic():
            inspection = Inspection.objects.select_for_update().get(pk=inspection_id)
            if inspection.status != Inspection.Status.COMPLETED:
                raise ReviewError('Only completed inspections can be reviewed.', 409)
            if InspectionReview.objects.filter(inspection=inspection).exists():
                raise ReviewError('This inspection has already been reviewed.', 409)
            return InspectionReview.objects.create(
                inspection=inspection, reviewer=reviewer, reviewed_class=reviewed_class,
                original_predicted_class=inspection.predicted_class, comment=(comment or '').strip(),
            )
    except Inspection.DoesNotExist:
        raise ReviewError('Inspection not found.', 404) from None
    except IntegrityError:
        raise ReviewError('This inspection has already been reviewed.', 409) from None


def save_corrective_action(inspection_id: int, supervisor, observed_issue: str, recommended_action: str, status: str):
    """Create or update optional follow-up information without modifying the review or AI output."""
    if not getattr(supervisor, 'is_supervisor', False):
        raise PermissionDenied('Only supervisors may manage corrective actions.')
    observed_issue, recommended_action = (observed_issue or '').strip(), (recommended_action or '').strip()
    if not observed_issue or not recommended_action:
        raise ReviewError('Observed issue and recommended corrective action are required.', 400)
    if len(observed_issue) > 1000 or len(recommended_action) > 1000:
        raise ReviewError('Corrective-action fields may contain at most 1000 characters.', 400)
    if status not in CorrectiveAction.Status.values:
        raise ReviewError('Choose a valid corrective-action status.', 400)
    try:
        with transaction.atomic():
            review = InspectionReview.objects.select_for_update().select_related('inspection').get(inspection_id=inspection_id)
            action, created = CorrectiveAction.objects.update_or_create(
                review=review,
                defaults={
                    'observed_issue': observed_issue, 'recommended_action': recommended_action,
                    'status': status, 'updated_by': supervisor,
                },
            )
            return action, created
    except InspectionReview.DoesNotExist:
        raise ReviewError('A supervisor review is required before corrective action can be recorded.', 409) from None


# --------------------------------------------------------------- querying

def visible_inspections(user):
    """Row-level scoping: engineers see only their own records."""
    qs = Inspection.objects.select_related('product', 'submitting_user', 'batch', 'review', 'review__reviewer')
    return qs.filter(submitting_user=user) if user.is_engineer else qs


def visible_batches(user):
    qs = InspectionBatch.objects.select_related('product', 'submitted_by').with_counts()
    return qs.filter(submitted_by=user) if user.is_engineer else qs


def _parse_date(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def filter_inspections(qs, params):
    """Apply search/filter query parameters. Unknown or malformed values are ignored."""
    q = (params.get('q') or '').strip()
    if q:
        cond = Q(original_filename__icontains=q) | Q(submitting_user__username__icontains=q)
        if q.isdigit():
            cond |= Q(pk=int(q)) | Q(batch_id=int(q))
        qs = qs.filter(cond)
    if params.get('predicted_class') in Inspection.PredictedClass.values:
        qs = qs.filter(predicted_class=params['predicted_class'])
    if params.get('status') in Inspection.Status.values:
        qs = qs.filter(status=params['status'])
    reviewed = (params.get('reviewed') or '').lower()
    if reviewed in ('yes', 'true', '1'):
        qs = qs.filter(review__isnull=False)
    elif reviewed in ('no', 'false', '0'):
        qs = qs.filter(review__isnull=True)
    if params.get('product'):
        qs = qs.filter(product__product_id=params['product'])
    if str(params.get('batch') or '').isdigit():
        qs = qs.filter(batch_id=int(params['batch']))
    if params.get('user'):
        qs = qs.filter(submitting_user__username=params['user'])
    start, end = _parse_date(params.get('date_from')), _parse_date(params.get('date_to'))
    tz = timezone.get_current_timezone()
    if start:
        qs = qs.filter(created_at__gte=timezone.make_aware(datetime.combine(start, time.min), tz))
    if end:
        qs = qs.filter(created_at__lte=timezone.make_aware(datetime.combine(end, time.max), tz))
    return qs


def dashboard_stats():
    totals = Inspection.objects.aggregate(
        total=Count('id'),
        completed=Count('id', filter=Q(status=Inspection.Status.COMPLETED)),
        failed=Count('id', filter=Q(status=Inspection.Status.FAILED)),
        queued=Count('id', filter=Q(status=Inspection.Status.QUEUED)),
        processing=Count('id', filter=Q(status=Inspection.Status.PROCESSING)),
        active=Count('id', filter=Q(status__in=[Inspection.Status.QUEUED, Inspection.Status.PROCESSING])),
        normal=Count('id', filter=Q(predicted_class=Inspection.PredictedClass.NORMAL)),
        defective=Count('id', filter=Q(predicted_class=Inspection.PredictedClass.DEFECTIVE)),
        awaiting_review=Count('id', filter=Q(status=Inspection.Status.COMPLETED, review__isnull=True)),
    )
    totals['reviewed'] = InspectionReview.objects.count()
    totals['corrected'] = InspectionReview.objects.exclude(reviewed_class=F('original_predicted_class')).count()
    return totals


def engineer_dashboard_stats(user):
    """Counts scoped to one Engineer; never leaks other submitters' activity."""
    return visible_inspections(user).aggregate(
        total=Count('id'),
        completed=Count('id', filter=Q(status=Inspection.Status.COMPLETED)),
        processing=Count('id', filter=Q(status=Inspection.Status.PROCESSING)),
        queued=Count('id', filter=Q(status=Inspection.Status.QUEUED)),
        failed=Count('id', filter=Q(status=Inspection.Status.FAILED)),
        normal=Count('id', filter=Q(predicted_class=Inspection.PredictedClass.NORMAL)),
        defective=Count('id', filter=Q(predicted_class=Inspection.PredictedClass.DEFECTIVE)),
        incident_eligible=Count(
            'id',
            filter=Q(status=Inspection.Status.COMPLETED, predicted_class=Inspection.PredictedClass.DEFECTIVE),
        ),
        pending_incidents=Count('incident__id', filter=Q(incident__review_status='pending')),
    )


# ----------------------------------------------------------- Round 3 Quality Incidents

class EligibilityError(Exception):
    """Raised when an inspection is ineligible for Quality Incident creation."""
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def check_inspection_eligibility(inspection: Inspection) -> tuple[bool, str]:
    """Validate whether an inspection can create a Quality Incident.

    Only completed inspections with the IMMUTABLE ORIGINAL model prediction 'Defective'
    are eligible. Supervisor corrections on review records do NOT alter eligibility.
    Normal, queued, processing, and failed inspections are ineligible.
    """
    if inspection.status != Inspection.Status.COMPLETED:
        return False, f"Inspection #{inspection.pk} is {inspection.get_status_display().lower()}; only completed inspections are eligible."
    if inspection.predicted_class != Inspection.PredictedClass.DEFECTIVE:
        return False, f"Inspection #{inspection.pk} has original model prediction '{inspection.predicted_class}'; only completed Defective inspections are eligible."
    return True, "Eligible for quality incident creation."


def visible_incidents(user):
    """Row-level scoping for Quality Incidents: Engineers see only their own records."""
    from .models import QualityIncident

    qs = QualityIncident.objects.select_related(
        'inspection', 'inspection__product', 'inspection__batch', 'inspection__submitting_user',
        'created_by', 'reviewed_by',
    )
    if user.is_engineer:
        return qs.filter(inspection__submitting_user=user)
    return qs


def create_quality_incident(inspection_id: int, user) -> tuple['QualityIncident', bool]:
    """Create a persistent Quality Incident from an eligible Defective inspection.

    Enforces exactly one incident per inspection through database constraints and transactions.
    Repeated or concurrent requests safely return the existing incident without regenerating
    or overwriting saved recommendations, citations, or supervisor reviews.
    """
    from assistant.models import Role
    from .models import QualityIncident
    from . import sop_service, incident_generator

    if not (user.is_authenticated and user.is_active):
        raise PermissionDenied("Authentication required.")
    if user.role != Role.ENGINEER:
        raise PermissionDenied("Only engineers can create quality incidents from their inspections.")

    inspection = Inspection.objects.select_related('product', 'submitting_user').filter(pk=inspection_id).first()
    if not inspection:
        raise EligibilityError(f"Inspection #{inspection_id} does not exist.", 404)

    if inspection.submitting_user_id != user.pk:
        raise PermissionDenied("You can only create quality incidents for your own inspections.")

    eligible, reason = check_inspection_eligibility(inspection)
    if not eligible:
        raise EligibilityError(reason, 400)

    # Fast check before acquiring locks
    existing = QualityIncident.objects.filter(inspection_id=inspection_id).first()
    if existing:
        return existing, False

    try:
        with transaction.atomic():
            # Lock the inspection row to serialize concurrent creation requests
            Inspection.objects.select_for_update().get(pk=inspection_id)
            existing = QualityIncident.objects.filter(inspection_id=inspection_id).first()
            if existing:
                return existing, False

            product_id = inspection.product.product_id

            failures = []
            try:
                summary = incident_generator.generate_incident_summary(inspection, product_id)
            except Exception as exc:
                logger.exception('Could not generate summary for inspection %s', inspection_id)
                summary = (
                    f"Quality incident for product {product_id}, inspection #{inspection.pk}. "
                    f"The original model prediction was {inspection.predicted_class}; manual review is required."
                )
                failures.append(f'Incident summary generation failed: {exc}')

            try:
                valid_sop, sop_reason, passages = sop_service.retrieve_sop_evidence(product_id)
            except Exception as exc:
                logger.exception('Quality SOP retrieval failed for inspection %s', inspection_id)
                valid_sop, sop_reason, passages = False, f'SOP retrieval failed: {exc}', []

            source_refs = incident_generator.format_source_references(passages)
            actions = []
            if not valid_sop:
                failures.append(sop_reason)
            elif not failures:
                try:
                    valid_actions, action_reason, actions = incident_generator.generate_suggested_actions(
                        product_id, passages
                    )
                except Exception as exc:
                    logger.exception('Suggested action generation failed for inspection %s', inspection_id)
                    valid_actions, action_reason, actions = False, f'Suggested action generation failed: {exc}', []
                if not valid_actions:
                    failures.append(action_reason)

            if failures:
                evidence_status = QualityIncident.EvidenceStatus.INSUFFICIENT
                failure_reason = ' '.join(reason.strip() for reason in failures if reason and reason.strip())
                suggested_actions = []
            else:
                evidence_status = QualityIncident.EvidenceStatus.SUFFICIENT
                failure_reason = ''
                suggested_actions = actions

            incident = QualityIncident.objects.create(
                inspection=inspection,
                product_id=product_id,
                original_predicted_class=inspection.predicted_class,
                original_model_version=inspection.model_version or 'BIT-PLATE-CV-1.0',
                confidence=inspection.confidence,
                defect_probability=inspection.defect_probability,
                summary=summary,
                suggested_actions=suggested_actions,
                source_references=source_refs,
                evidence_status=evidence_status,
                evidence_failure_reason=failure_reason,
                created_by=user,
                review_status=QualityIncident.ReviewStatus.PENDING,
            )
            return incident, True
    except IntegrityError:
        # Concurrent race caught by database UNIQUE constraint on inspection_id
        existing = QualityIncident.objects.filter(inspection_id=inspection_id).first()
        if existing:
            return existing, False
        raise


def submit_incident_review(incident_id: int, user, decision: str, comment: str) -> 'QualityIncident':
    """Supervisor review for a Quality Incident.

    Requires supervisor role, valid decision, and mandatory comment.
    Insufficient-evidence incidents cannot be approved for follow-up.
    """
    from assistant.models import Role
    from .models import QualityIncident

    if not (user.is_authenticated and user.is_active and user.role == Role.SUPERVISOR):
        raise PermissionDenied("Only supervisors may review quality incidents.")

    decision = (decision or '').strip()
    if decision not in (QualityIncident.ReviewStatus.APPROVED, QualityIncident.ReviewStatus.NEEDS_INFO):
        raise ReviewError(f"Invalid review decision: '{decision}'. Must be 'approved' or 'needs_info'.", 400)

    comment = (comment or '').strip()
    if not comment:
        raise ReviewError("A review comment is mandatory.", 400)

    with transaction.atomic():
        incident = QualityIncident.objects.select_for_update().filter(pk=incident_id).first()
        if not incident:
            raise ReviewError(f"Incident #{incident_id} not found.", 404)
        if incident.review_status != QualityIncident.ReviewStatus.PENDING:
            raise ReviewError("This quality incident already has a saved review decision.", 409)
        if (
            decision == QualityIncident.ReviewStatus.APPROVED
            and incident.evidence_status != QualityIncident.EvidenceStatus.SUFFICIENT
        ):
            raise ReviewError(
                "Incidents with insufficient evidence cannot be approved for follow-up. "
                "Choose 'Needs more information'.",
                400,
            )
        incident.review_status = decision
        incident.reviewed_by = user
        incident.reviewed_at = timezone.now()
        incident.supervisor_comment = comment[:1000]
        incident.save(update_fields=['review_status', 'reviewed_by', 'reviewed_at', 'supervisor_comment', 'updated_at'])

    return incident
