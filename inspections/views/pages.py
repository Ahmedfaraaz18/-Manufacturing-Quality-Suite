import json
import logging
from datetime import datetime, timezone as datetime_timezone
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.core.management import call_command
from django.core.exceptions import PermissionDenied
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from assistant.models import Role, User
from assistant.permissions import role_required

from .. import services
from .. import sop_service
from .. import intelligence
from ..inference import model_metadata, verify_model_integrity, verify_organizer_integrity
from ..models import CorrectiveAction, DatasetSample, Inspection, InspectionBatch, InspectionReview, Product, QualityIncident, ValidationAsset
from ..reports import ReportGenerationError, build_batch_report, build_inspection_report
from ..validators import ALLOWED_FORMATS

ALL_ROLES = (Role.ADMINISTRATOR, Role.ENGINEER, Role.SUPERVISOR)
logger = logging.getLogger(__name__)
HOME_BY_ROLE = {
    Role.ADMINISTRATOR: 'inspection-admin',
    Role.ENGINEER: 'inspection-upload',
    Role.SUPERVISOR: 'inspection-supervisor',
}


@login_required
def inspections_home(request):
    return redirect(HOME_BY_ROLE.get(request.user.role, 'login'))


def _history_context(request, base_qs, *, show_user):
    qs = services.filter_inspections(base_qs, request.GET)
    page = Paginator(qs, 15).get_page(request.GET.get('page'))
    query = request.GET.copy()
    query.pop('page', None)
    return {
        'page': page, 'filters': request.GET, 'querystring': query.urlencode(), 'show_user': show_user,
        'statuses': Inspection.Status.choices, 'classes': Inspection.PredictedClass.choices,
        'products': Product.objects.order_by('product_id'),
    }


# ------------------------------------------------------------------ engineer

@role_required(Role.ENGINEER)
@ensure_csrf_cookie
def upload(request):
    return render(request, 'inspections/upload.html', {
        'products': Product.objects.filter(is_active=True),
        'recent': services.visible_inspections(request.user)[:5],
        'stats': services.engineer_dashboard_stats(request.user),
    })


@role_required(Role.ENGINEER)
def engineer_history(request):
    context = _history_context(request, services.visible_inspections(request.user), show_user=False)
    context.update(title='My inspection history', subtitle='Every image you have submitted, with the original model output.')
    return render(request, 'inspections/history.html', context)


# ---------------------------------------------------------------- supervisor

@role_required(Role.SUPERVISOR)
def supervisor_dashboard(request):
    context = _history_context(request, services.visible_inspections(request.user), show_user=True)
    context.update(
        title='Inspection review queue', subtitle='All inspections. Open a completed record to confirm or correct the prediction.',
        stats=services.dashboard_stats(),
    )
    return render(request, 'inspections/history.html', context)


@role_required(Role.SUPERVISOR)
@require_http_methods(['GET', 'POST'])
def review_submit(request, pk):
    """Form-based review. The decorator rejects every non-supervisor with HTTP 403 before any lookup."""
    if request.method == 'GET':
        return redirect('inspection-detail', pk=pk)
    try:
        review = services.submit_review(pk, request.user, request.POST.get('reviewed_class', ''), request.POST.get('comment', '')[:1000])
    except services.ReviewError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f'Review saved: {review.reviewed_class}.')
    return redirect('inspection-detail', pk=pk)


@role_required(Role.SUPERVISOR)
@require_http_methods(['POST'])
def corrective_action_submit(request, pk):
    """Supervisor-only update of a separate corrective-action record for an existing review."""
    try:
        action, created = services.save_corrective_action(
            pk, request.user, request.POST.get('observed_issue', ''),
            request.POST.get('recommended_action', ''), request.POST.get('status', ''),
        )
    except services.ReviewError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f'Corrective action {"created" if created else "updated"}: {action.get_status_display()}.')
    return redirect('inspection-detail', pk=pk)


# ------------------------------------------------------------- shared views

@role_required(*ALL_ROLES)
def inspection_detail(request, pk):
    inspection = get_object_or_404(services.visible_inspections(request.user), pk=pk)
    try:
        review = inspection.review
    except InspectionReview.DoesNotExist:
        review = None
    try:
        corrective_action = review.corrective_action if review else None
    except CorrectiveAction.DoesNotExist:
        corrective_action = None
    incident = QualityIncident.objects.filter(inspection=inspection).first()
    incident_eligible, incident_eligibility_reason = services.check_inspection_eligibility(inspection)
    return render(request, 'inspections/detail.html', {
        'inspection': inspection, 'review': review, 'corrective_action': corrective_action,
        'corrective_action_statuses': CorrectiveAction.Status.choices,
        'can_review': request.user.is_supervisor and inspection.status == Inspection.Status.COMPLETED and review is None,
        'can_manage_corrective_action': request.user.is_supervisor and review is not None,
        'quality_incident': incident,
        'incident_eligible': incident_eligible,
        'incident_eligibility_reason': incident_eligibility_reason,
    })


@role_required(Role.ENGINEER)
@require_http_methods(['POST'])
def create_quality_incident(request, pk):
    try:
        incident, created = services.create_quality_incident(pk, request.user)
    except services.EligibilityError as exc:
        messages.error(request, str(exc))
        return redirect('inspection-detail', pk=pk)
    except PermissionDenied as exc:
        messages.error(request, str(exc))
        return redirect('inspection-history')

    if created:
        messages.success(request, f'Quality incident #{incident.pk} created.')
    else:
        messages.info(request, f'Incident #{incident.pk} already exists; its saved recommendations and review are unchanged.')
    return redirect('quality-incident-detail', pk=incident.pk)


@role_required(*ALL_ROLES)
def quality_incident_list(request):
    all_incidents = services.visible_incidents(request.user)
    incidents = all_incidents
    query = (request.GET.get('q') or '').strip()
    if query:
        search = (
            Q(product_id__icontains=query)
            | Q(inspection__original_filename__icontains=query)
            | Q(created_by__username__icontains=query)
        )
        if query.isdigit() and len(query) <= 18:
            search |= Q(pk=int(query)) | Q(inspection_id=int(query))
        incidents = incidents.filter(search)
    if request.GET.get('evidence') in QualityIncident.EvidenceStatus.values:
        incidents = incidents.filter(evidence_status=request.GET['evidence'])
    if request.GET.get('review') in QualityIncident.ReviewStatus.values:
        incidents = incidents.filter(review_status=request.GET['review'])

    incidents = incidents.annotate(
        review_priority=Case(
            When(review_status=QualityIncident.ReviewStatus.PENDING, then=Value(0)),
            default=Value(1),
            output_field=IntegerField(),
        )
    ).order_by('review_priority', '-created_at', '-id')
    page = Paginator(incidents, 20).get_page(request.GET.get('page'))
    context = {
        'page': page,
        'filters': request.GET,
    }
    if request.user.is_supervisor:
        context.update({
            'incident_stats': all_incidents.aggregate(
                total=Count('id'),
                pending=Count('id', filter=Q(review_status=QualityIncident.ReviewStatus.PENDING)),
                sufficient=Count('id', filter=Q(evidence_status=QualityIncident.EvidenceStatus.SUFFICIENT)),
                insufficient=Count('id', filter=Q(evidence_status=QualityIncident.EvidenceStatus.INSUFFICIENT)),
            ),
            'recently_reviewed': all_incidents.filter(reviewed_at__isnull=False).order_by('-reviewed_at')[:5],
        })
    return render(request, 'inspections/incident_list.html', context)


@role_required(*ALL_ROLES)
def quality_incident_detail(request, pk):
    incident = get_object_or_404(services.visible_incidents(request.user), pk=pk)
    try:
        inspection_review = incident.inspection.review
    except InspectionReview.DoesNotExist:
        inspection_review = None
    return render(request, 'inspections/incident_detail.html', {
        'incident': incident,
        'inspection_review': inspection_review,
        'can_review_incident': (
            request.user.is_supervisor and incident.review_status == QualityIncident.ReviewStatus.PENDING
        ),
    })


@role_required(Role.SUPERVISOR)
@require_http_methods(['POST'])
def quality_incident_review(request, pk):
    try:
        incident = services.submit_incident_review(
            pk, request.user, request.POST.get('decision', ''), request.POST.get('comment', '')
        )
    except services.ReviewError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f'Quality incident review saved: {incident.get_review_status_display()}.')
    return redirect('quality-incident-detail', pk=pk)


@role_required(*ALL_ROLES)
def quality_sop_source(request, document_id, revision, page_number):
    valid, reason, path = sop_service.find_mapped_sop_file(document_id, revision)
    if not valid or not path or not path.is_file():
        raise Http404(reason or 'Mapped SOP source is unavailable.')
    try:
        import fitz

        with fitz.open(str(path)) as document:
            if not 1 <= page_number <= len(document):
                raise Http404('The requested page is not present in this SOP.')
    except Http404:
        raise
    except Exception:
        logger.exception('Could not inspect mapped SOP source %s revision %s', document_id, revision)
        raise Http404('Mapped SOP source is unavailable.')
    try:
        source = path.open('rb')
    except OSError:
        logger.exception('Could not open mapped SOP source %s revision %s', document_id, revision)
        raise Http404('Mapped SOP source is unavailable.')

    response = FileResponse(source, content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{path.name}"'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = 'private, no-store'
    return response


@role_required(*ALL_ROLES)
def inspection_pdf_report(request, pk):
    """Stream a completed inspection report to an authorised user without storing a PDF copy."""
    inspection = get_object_or_404(services.visible_inspections(request.user), pk=pk)
    try:
        report, _reference = build_inspection_report(inspection)
    except ReportGenerationError as exc:
        messages.error(request, str(exc))
        return redirect('inspection-detail', pk=inspection.pk)
    filename = f'Inspection_Report_{inspection.pk}_{inspection.product.product_id}.pdf'
    response = FileResponse(report, as_attachment=True, filename=filename, content_type='application/pdf')
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = 'private, no-store'
    return response


@role_required(Role.ADMINISTRATOR, Role.SUPERVISOR)
@require_http_methods(['GET', 'POST'])
def quality_intelligence(request):
    """Live, role-scoped analytics derived from inspections, batches, and reviews."""
    configuration = intelligence.QualityAlertConfiguration.current()
    if request.method == 'POST':
        if request.user.role != Role.ADMINISTRATOR:
            raise PermissionDenied
        try:
            threshold = float(request.POST.get('batch_defect_rate_threshold', ''))
            consecutive = int(request.POST.get('consecutive_high_defect_batches', ''))
            failure_limit = int(request.POST.get('failure_limit', ''))
            review_limit = int(request.POST.get('review_backlog_limit', ''))
            processing_limit = int(request.POST.get('processing_backlog_limit', ''))
            if not 0 <= threshold <= 1 or consecutive < 1 or min(failure_limit, review_limit, processing_limit) < 0:
                raise ValueError
        except (TypeError, ValueError):
            messages.error(request, 'Use a defect-rate threshold from 0 to 1, at least one consecutive batch, and non-negative limits.')
        else:
            configuration.batch_defect_rate_threshold = threshold
            configuration.consecutive_high_defect_batches = consecutive
            configuration.failure_limit = failure_limit
            configuration.review_backlog_limit = review_limit
            configuration.processing_backlog_limit = processing_limit
            configuration.save()
            messages.success(request, 'Operational alert thresholds updated.')
        return redirect('inspection-intelligence')

    batch_metrics = intelligence.batch_metrics_queryset(request.GET)
    batch_page = Paginator(batch_metrics.order_by('-created_at', '-id'), 10).get_page(request.GET.get('page'))
    comparison = intelligence.batch_metrics_queryset(request.GET).filter(completed_count__gt=0)
    highest = list(comparison.order_by('-defect_rate', '-created_at')[:5])
    lowest = list(comparison.order_by('defect_rate', '-created_at')[:5])
    return render(request, 'inspections/intelligence.html', {
        'kpis': intelligence.operational_kpis(request.GET),
        'agreement': intelligence.agreement_metrics(request.GET),
        'trends': intelligence.trend_data(request.GET),
        'batches': batch_page,
        'highest_defect_batches': highest,
        'lowest_defect_batches': lowest,
        'batch_chart': [{'label': f'#{batch.pk}', 'value': round(batch.defect_rate, 2)} for batch in highest],
        'alerts': intelligence.refresh_operational_alerts()[:12],
        'configuration': configuration,
        'filters': request.GET,
        'is_alert_administrator': request.user.role == Role.ADMINISTRATOR,
    })


@role_required(*ALL_ROLES)
def batch_detail(request, pk):
    batch = get_object_or_404(services.visible_batches(request.user), pk=pk)
    return render(request, 'inspections/batch.html', {'batch': batch})


@role_required(*ALL_ROLES)
def batch_pdf_report(request, pk):
    """Stream an authorised batch quality summary without retaining a second copy on disk."""
    batch = get_object_or_404(services.visible_batches(request.user), pk=pk)
    try:
        report, _reference = build_batch_report(batch)
    except ReportGenerationError as exc:
        messages.error(request, str(exc))
        return redirect('inspection-batch', pk=batch.pk)
    filename = f'Batch_Quality_Report_{batch.pk}_{batch.product.product_id}.pdf'
    response = FileResponse(report, as_attachment=True, filename=filename, content_type='application/pdf')
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = 'private, no-store'
    return response


@login_required
def inspection_image(request, pk):
    """Authenticated image delivery with the same row-level scoping as the API."""
    if request.user.role not in ALL_ROLES or not request.user.is_active:
        raise Http404
    inspection = get_object_or_404(services.visible_inspections(request.user), pk=pk)
    if not inspection.image or inspection.image_format not in ALLOWED_FORMATS:
        raise Http404('No image stored for this inspection.')
    try:
        handle = inspection.image.open('rb')
    except FileNotFoundError:
        raise Http404('The image file is missing.') from None
    response = FileResponse(handle, content_type=ALLOWED_FORMATS[inspection.image_format][1])
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = 'private, max-age=3600'
    return response


# ------------------------------------------------------------ administrator

@role_required(Role.ADMINISTRATOR)
@ensure_csrf_cookie
def admin_dashboard(request):
    try:
        integrity = verify_model_integrity()
    except OSError:
        integrity = {}
    products = Product.objects.order_by('product_id')
    products = products.annotate(inspection_count=Count('inspections'))
    organizer_integrity = verify_organizer_integrity()
    return render(request, 'inspections/admin.html', {
        'stats': services.dashboard_stats(), 'products': products,
        'product_count': products.count(),
        'recent': services.visible_inspections(request.user)[:8],
        'model': model_metadata(), 'model_ok': bool(integrity) and all(integrity.values()),
        'organizer_verified_count': sum(1 for valid in organizer_integrity.values() if valid),
        'organizer_mismatch_count': sum(1 for valid in organizer_integrity.values() if not valid),
        'organizer_ok': bool(organizer_integrity) and all(organizer_integrity.values()),
        'evaluation_updated': _evaluation_updated(),
        'quality_incident_stats': QualityIncident.objects.aggregate(
            total=Count('id'),
            pending=Count('id', filter=Q(review_status=QualityIncident.ReviewStatus.PENDING)),
            sufficient=Count('id', filter=Q(evidence_status=QualityIncident.EvidenceStatus.SUFFICIENT)),
            insufficient=Count('id', filter=Q(evidence_status=QualityIncident.EvidenceStatus.INSUFFICIENT)),
        ),
        'role_counts': {
            row['role']: row['count']
            for row in User.objects.values('role').annotate(count=Count('id'))
        },
        'recent_incidents': QualityIncident.objects.select_related(
            'inspection', 'created_by', 'reviewed_by'
        ).order_by('-created_at')[:5],
    })


def _evaluation_path():
    return Path(settings.BASE_DIR) / 'evaluation_reports' / 'round2_evaluation.json'


def _evaluation_updated():
    try:
        return datetime.fromtimestamp(_evaluation_path().stat().st_mtime, tz=datetime_timezone.utc)
    except OSError:
        return None


@role_required(Role.ADMINISTRATOR)
@require_http_methods(['GET', 'POST'])
def evaluation_dashboard(request):
    """Display the latest offline evaluation report; it never writes live inspection data."""
    if request.method == 'POST':
        try:
            # This uses the unmodified organizer helper for all 60 samples.  It does not
            # create inspections and labels are only read after predictions are collected.
            call_command('evaluate_model')
        except Exception:
            messages.error(request, 'Evaluation could not be completed. Check the server log and organizer assets.')
        else:
            messages.success(request, 'Model evaluation completed using all organizer samples.')
        return redirect('inspection-evaluation')
    report_path = _evaluation_path()
    report = None
    error = ''
    try:
        candidate = json.loads(report_path.read_text(encoding='utf-8'))
        if not isinstance(candidate, dict) or not isinstance(candidate.get('metrics'), dict):
            raise ValueError('invalid report structure')
        report = candidate
    except FileNotFoundError:
        error = 'No evaluation report has been generated yet. Run manage.py evaluate_model.'
    except (OSError, ValueError, json.JSONDecodeError):
        error = 'The saved evaluation report could not be read. Run manage.py evaluate_model again.'
    return render(request, 'inspections/evaluation.html', {
        'report': report, 'error': error, 'sample_count': DatasetSample.objects.count(),
        'validation_count': ValidationAsset.objects.count(), 'updated_at': _evaluation_updated(),
        'failed_rows': [row for row in (report or {}).get('rows', []) if row.get('predicted') is None],
    })


@role_required(Role.ADMINISTRATOR)
@require_http_methods(['GET', 'POST'])
def dataset_dashboard(request):
    """Administrator-only dataset inventory; labels are intentionally not rendered."""
    if request.method == 'POST':
        try:
            call_command('import_round2_data')
        except Exception:
            messages.error(request, 'Dataset import failed. Check the server log and organizer assets.')
        else:
            messages.success(request, 'Organizer dataset inventory was verified and imported safely.')
        return redirect('inspection-dataset')
    integrity = verify_organizer_integrity()
    samples = DatasetSample.objects.select_related('product').order_by('image_id')
    page = Paginator(samples, 20).get_page(request.GET.get('page'))
    return render(request, 'inspections/dataset.html', {
        'page': page, 'integrity': integrity, 'integrity_ok': bool(integrity) and all(integrity.values()),
        'invalid_assets': ValidationAsset.objects.order_by('filename'),
    })
