"""Professional, in-memory PDF reports for completed visual inspections."""

import io
from datetime import timezone as datetime_timezone

from django.utils import timezone
from django.db.models import Count, F, Q
from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .inference import model_metadata
from .models import CorrectiveAction, Inspection

NAVY = colors.HexColor('#102A43')
BLUE = colors.HexColor('#1769AA')
PALE_BLUE = colors.HexColor('#EAF3FA')
GREEN = colors.HexColor('#177245')
PALE_GREEN = colors.HexColor('#EAF7EF')
RED = colors.HexColor('#B42318')
PALE_RED = colors.HexColor('#FFF1F0')
SLATE = colors.HexColor('#52616B')
LINE = colors.HexColor('#D9E2EC')


class ReportGenerationError(RuntimeError):
    """A safe, user-facing PDF generation failure."""


def _text(value, fallback='-'):
    return str(value) if value not in (None, '') else fallback


def _timestamp(value):
    if not value:
        return '-'
    return timezone.localtime(value, datetime_timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def _percent(value):
    return '-' if value is None else f'{value * 100:.2f}%'


def _bytes(value):
    if value is None:
        return '-'
    units = ('B', 'KiB', 'MiB', 'GiB')
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f'{size:.1f} {unit}' if unit != 'B' else f'{int(size)} {unit}'
        size /= 1024


def _safe_paragraph(value, style):
    from xml.sax.saxutils import escape

    return Paragraph(escape(_text(value)), style)


def _styles():
    base = getSampleStyleSheet()
    return {
        'title': ParagraphStyle('ReportTitle', parent=base['Title'], fontName='Helvetica-Bold', fontSize=21,
                                leading=25, textColor=NAVY, spaceAfter=5),
        'subtitle': ParagraphStyle('ReportSubtitle', parent=base['Normal'], fontSize=9, leading=13, textColor=SLATE),
        'section': ParagraphStyle('ReportSection', parent=base['Heading2'], fontName='Helvetica-Bold', fontSize=11,
                                  leading=15, textColor=NAVY, spaceBefore=14, spaceAfter=7),
        'body': ParagraphStyle('ReportBody', parent=base['Normal'], fontSize=8.7, leading=12, textColor=NAVY),
        'small': ParagraphStyle('ReportSmall', parent=base['Normal'], fontSize=7.5, leading=10, textColor=SLATE),
        'status': ParagraphStyle('ReportStatus', parent=base['Normal'], alignment=TA_CENTER, fontName='Helvetica-Bold',
                                 fontSize=17, leading=22),
    }


def _section(title, styles):
    return Paragraph(title, styles['section'])


def _table(rows, styles, widths=(48 * mm, 112 * mm)):
    data = [[_safe_paragraph(label, styles['small']), _safe_paragraph(value, styles['body'])] for label, value in rows]
    table = Table(data, colWidths=widths, hAlign='LEFT', repeatRows=0)
    table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('BACKGROUND', (0, 0), (0, -1), PALE_BLUE),
        ('LINEBELOW', (0, 0), (-1, -1), 0.35, LINE), ('LEFTPADDING', (0, 0), (-1, -1), 7),
        ('RIGHTPADDING', (0, 0), (-1, -1), 7), ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    return table


def _header_footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.5)
    canvas.line(doc.leftMargin, A4[1] - 16 * mm, A4[0] - doc.rightMargin, A4[1] - 16 * mm)
    canvas.setFillColor(NAVY)
    canvas.setFont('Helvetica-Bold', 8)
    canvas.drawString(doc.leftMargin, A4[1] - 12 * mm, 'MANUFACTURING QUALITY SUITE')
    canvas.setFillColor(SLATE)
    canvas.setFont('Helvetica', 7)
    canvas.drawRightString(A4[0] - doc.rightMargin, A4[1] - 12 * mm, doc.report_reference)
    canvas.line(doc.leftMargin, 14 * mm, A4[0] - doc.rightMargin, 14 * mm)
    canvas.setFont('Helvetica', 7)
    canvas.setFillColor(SLATE)
    canvas.drawString(doc.leftMargin, 9 * mm, f'Confidential quality record - generated {doc.report_generated_at}')
    canvas.drawRightString(A4[0] - doc.rightMargin, 9 * mm, f'Page {canvas.getPageNumber()}')
    canvas.restoreState()


def _evidence_image(inspection, styles):
    """Embed the stored evidence image when readable; never substitute another image."""
    if not inspection.image:
        return [Paragraph('Image evidence unavailable: no image is stored for this record.', styles['body'])]
    try:
        with inspection.image.open('rb') as handle:
            data = handle.read()
        with PILImage.open(io.BytesIO(data)) as source:
            source.verify()
        with PILImage.open(io.BytesIO(data)) as source:
            width, height = source.size
        picture = Image(io.BytesIO(data))
        max_width, max_height = 160 * mm, 100 * mm
        scale = min(max_width / width, max_height / height, 1)
        picture.drawWidth, picture.drawHeight = width * scale, height * scale
        frame = Table([[picture]], colWidths=[164 * mm], hAlign='LEFT')
        frame.setStyle(TableStyle([
            ('BOX', (0, 0), (-1, -1), 0.75, LINE), ('BACKGROUND', (0, 0), (-1, -1), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        return [frame]
    except Exception:
        return [Paragraph('Image evidence is stored but could not be accessed or decoded when this report was generated.', styles['body'])]


def build_inspection_report(inspection):
    """Return ``(BytesIO, report_reference)`` for a completed Inspection.

    This function does not write a PDF copy to disk and does not mutate the inspection, its
    prediction, or any supervisor review.
    """
    if inspection.status != inspection.Status.COMPLETED:
        raise ReportGenerationError('A final PDF report is available only after an inspection is completed.')
    if not inspection.predicted_class or inspection.confidence is None or inspection.defect_probability is None:
        raise ReportGenerationError('This completed inspection has incomplete model output and cannot be reported.')

    generated_at = timezone.now()
    reference = f'MQS-IR-{inspection.pk}-{generated_at.strftime("%Y%m%d%H%M%S")}'
    output = io.BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm,
                            topMargin=23 * mm, bottomMargin=21 * mm, title='AI Visual Quality Inspection Report',
                            author='Manufacturing Quality Suite')
    doc.report_reference = reference
    doc.report_generated_at = _timestamp(generated_at)
    styles = _styles()
    metadata = model_metadata()
    result_is_defective = inspection.predicted_class == inspection.PredictedClass.DEFECTIVE
    result_color, result_background = (RED, PALE_RED) if result_is_defective else (GREEN, PALE_GREEN)
    story = []

    story.extend([
        Paragraph('AI Visual Quality Inspection Report', styles['title']),
        Paragraph('A factual record of the stored organizer-model result and any supervisor review.', styles['subtitle']),
        Spacer(1, 7),
    ])
    header = Table([
        [_safe_paragraph('REPORT REFERENCE', styles['small']), _safe_paragraph('INSPECTION ID', styles['small']), _safe_paragraph('REPORT STATUS', styles['small'])],
        [_safe_paragraph(reference, styles['body']), _safe_paragraph(f'#{inspection.pk}', styles['body']), _safe_paragraph('Completed', styles['body'])],
        [_safe_paragraph('GENERATED', styles['small']), _safe_paragraph('BATCH ID', styles['small']), _safe_paragraph('PRODUCT', styles['small'])],
        [_safe_paragraph(_timestamp(generated_at), styles['body']), _safe_paragraph(f'#{inspection.batch_id}', styles['body']), _safe_paragraph(inspection.product.product_id, styles['body'])],
    ], colWidths=[54 * mm, 54 * mm, 54 * mm])
    header.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), PALE_BLUE), ('BACKGROUND', (0, 2), (-1, 2), PALE_BLUE),
        ('GRID', (0, 0), (-1, -1), 0.4, LINE), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 7), ('RIGHTPADDING', (0, 0), (-1, -1), 7),
        ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    story.extend([header, _section('Executive summary', styles)])
    result_banner = Table([[Paragraph(inspection.predicted_class.upper(), styles['status'])]], colWidths=[164 * mm])
    result_banner.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), result_background), ('BOX', (0, 0), (-1, -1), 0.8, result_color),
        ('TEXTCOLOR', (0, 0), (-1, -1), result_color), ('TOPPADDING', (0, 0), (-1, -1), 8), ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    story.extend([result_banner, Spacer(1, 6), _table([
        ('Predicted classification', inspection.predicted_class), ('Model confidence', _percent(inspection.confidence)),
        ('Defect probability', _percent(inspection.defect_probability)), ('Product identifier', inspection.product.product_id),
        ('Model version', inspection.model_version), ('Completion status', inspection.get_status_display()),
    ], styles)])

    story.extend([_section('Inspection information', styles), _table([
        ('Inspection ID', f'#{inspection.pk}'), ('Batch ID', f'#{inspection.batch_id}' if inspection.batch_id else '-'),
        ('Product', f'{inspection.product.product_id} - {inspection.product.name}'), ('Original filename', inspection.original_filename),
        ('Submitted by', inspection.submitting_user.username), ('Submitted at', _timestamp(inspection.created_at)),
        ('Completed at', _timestamp(inspection.completed_at)), ('Inspection status', inspection.get_status_display()),
        ('Model version', inspection.model_version),
    ], styles)])

    # Keep the evidence heading, metadata, and frame together.  Otherwise a large evidence
    # image can be pushed alone onto the next page, detached from its context.
    evidence = [_section('Image evidence', styles), Paragraph(
        f'File: {_text(inspection.original_filename)} | Format: {_text(inspection.image_format)} | '
        f'Dimensions: {_text(inspection.image_width)} x {_text(inspection.image_height)} px | Size: {_bytes(inspection.file_size)}', styles['small'])]
    evidence.extend(_evidence_image(inspection, styles))
    story.extend([PageBreak(), KeepTogether(evidence)])

    story.extend([_section('AI classification results', styles), _table([
        ('Predicted class', inspection.predicted_class), ('Model confidence', _percent(inspection.confidence)),
        ('Defect probability', _percent(inspection.defect_probability)), ('Raw defect probability', f'{inspection.defect_probability:.6f}'),
        ('Decision threshold', '0.50'), ('Decision rule', 'Defective when defect probability >= 0.50; Normal when defect probability < 0.50.'),
        ('Model version', inspection.model_version),
    ], styles), Paragraph('Confidence is the model score for the predicted class. Defect probability is the separate raw score used by the decision threshold.', styles['small'])])

    duration = '-'
    if inspection.started_at and inspection.completed_at:
        seconds = max(0, (inspection.completed_at - inspection.started_at).total_seconds())
        duration = f'{seconds:.2f} seconds'
    story.extend([_section('Processing details', styles), _table([
        ('Submission time', _timestamp(inspection.created_at)), ('Processing start', _timestamp(inspection.started_at)),
        ('Completion time', _timestamp(inspection.completed_at)), ('Processing duration', duration),
        ('Processing status', inspection.get_status_display()), ('Failure reason', inspection.failure_reason or '-'),
    ], styles)])

    review = getattr(inspection, 'review', None)
    # Reserve the final page for review/audit conclusions so the review heading is never orphaned.
    story.append(PageBreak())
    story.append(_section('Supervisor review', styles))
    if review:
        outcome = 'Corrected' if review.is_correction else 'Confirmed'
        story.append(_table([
            ('Original AI prediction', review.original_predicted_class), ('Final supervisor-reviewed decision', review.reviewed_class),
            ('Review outcome', outcome), ('Reviewed by', review.reviewer.username), ('Review timestamp', _timestamp(review.reviewed_at)),
            ('Review comment', review.comment or 'No comment'),
        ], styles))
    else:
        story.append(Paragraph('Awaiting Supervisor Review.', styles['body']))

    if review:
        try:
            corrective_action = review.corrective_action
        except CorrectiveAction.DoesNotExist:
            corrective_action = None
        if corrective_action:
            story.extend([_section('Corrective action', styles), _table([
                ('Observed issue', corrective_action.observed_issue),
                ('Recommended corrective action', corrective_action.recommended_action),
                ('Action status', corrective_action.get_status_display()),
                ('Last updated by', corrective_action.updated_by.username),
                ('Last updated', _timestamp(corrective_action.updated_at)),
            ], styles)])

    story.extend([_section('Technical information', styles), _table([
        ('Supplied classifier type', metadata.get('architecture', 'Not available in supplied metadata.')),
        ('Inference method', 'Supplied Model/inference.py predict helper, called with the saved inspection image.'),
        ('Model version', inspection.model_version), ('Decision threshold', '0.50'),
        ('Image processing details', metadata.get('input', 'Not available in supplied metadata.')),
        ('Verified limitations', metadata.get('limitations', 'Not available in supplied metadata.')),
    ], styles)])

    conclusion = f'The stored model result for inspection #{inspection.pk} is {inspection.predicted_class}.'
    if review:
        conclusion += f' The supervisor {"corrected" if review.is_correction else "confirmed"} the result to {review.reviewed_class}.'
    else:
        conclusion += ' No supervisor review has been recorded.'
    story.extend([_section('Conclusion', styles), Paragraph(conclusion, styles['body']), _section('Disclaimer', styles), Paragraph(
        'The model confidence and defect probability are uncalibrated model scores and should not be interpreted as verified probabilities of product safety or real-world manufacturing quality. The supplied model was evaluated on synthetic organiser-provided images. Performance on real-world manufacturing images has not been established by this evaluation.', styles['small'])])
    try:
        doc.build(story, onFirstPage=_header_footer, onLaterPages=_header_footer)
    except Exception as exc:
        raise ReportGenerationError('The PDF report could not be generated.') from exc
    output.seek(0)
    return output, reference


def build_batch_report(batch):
    """Return a factual quality summary for one authorised batch, without storing a PDF copy."""
    inspections = batch.inspections.select_related('product', 'submitting_user', 'review').order_by('id')
    corrected = Q(review__isnull=False) & ~Q(review__reviewed_class=F('review__original_predicted_class'))
    counts = inspections.aggregate(
        completed=Count('id', filter=Q(status=Inspection.Status.COMPLETED)),
        failed=Count('id', filter=Q(status=Inspection.Status.FAILED)),
        queued=Count('id', filter=Q(status=Inspection.Status.QUEUED)),
        processing=Count('id', filter=Q(status=Inspection.Status.PROCESSING)),
        normal=Count('id', filter=Q(predicted_class=Inspection.PredictedClass.NORMAL)),
        defective=Count('id', filter=Q(predicted_class=Inspection.PredictedClass.DEFECTIVE)),
        reviewed=Count('id', filter=Q(review__isnull=False)),
        corrected=Count('id', filter=corrected),
    )
    defect_rate = None if not counts['completed'] else 100 * counts['defective'] / counts['completed']
    generated_at = timezone.now()
    reference = f'MQS-BR-{batch.pk}-{generated_at.strftime("%Y%m%d%H%M%S")}'
    output = io.BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm,
                            topMargin=23 * mm, bottomMargin=21 * mm, title='Batch Quality Summary Report',
                            author='Manufacturing Quality Suite')
    doc.report_reference = reference
    doc.report_generated_at = _timestamp(generated_at)
    styles = _styles()
    active = counts['queued'] + counts['processing']
    status = 'In progress' if active else 'Final batch summary'
    story = [
        Paragraph('Batch Quality Summary Report', styles['title']),
        Paragraph('An operational summary of stored inspection results. It does not establish a manufacturing root cause.', styles['subtitle']),
        Spacer(1, 7),
        _table([
            ('Report reference', reference), ('Batch ID', f'#{batch.pk}'), ('Report status', status),
            ('Product', f'{batch.product.product_id} - {batch.product.name}'),
            ('Submitted by', batch.submitted_by.username), ('Submission date', _timestamp(batch.created_at)),
        ], styles),
        _section('Batch quality overview', styles),
        _table([
            ('Total submitted files', batch.total_files), ('Completed files', counts['completed']),
            ('Failed files', counts['failed']), ('Queued files', counts['queued']),
            ('Processing files', counts['processing']), ('Normal predictions', counts['normal']),
            ('Defective predictions', counts['defective']),
            ('Completed-item defect rate', 'Not Available' if defect_rate is None else f'{defect_rate:.2f}%'),
            ('Supervisor-reviewed items', counts['reviewed']), ('Supervisor-corrected items', counts['corrected']),
        ], styles),
        Paragraph('Defect rate is Defective completed inspections divided by all completed inspections. Failed, queued, and processing records are excluded.', styles['small']),
        _section('Individual inspection references', styles),
    ]
    rows = []
    for inspection in inspections:
        review = getattr(inspection, 'review', None)
        final = review.reviewed_class if review else 'Awaiting supervisor review' if inspection.status == Inspection.Status.COMPLETED else '-'
        rows.extend([
            (f'Inspection #{inspection.pk}', inspection.original_filename or '-'),
            ('Status / original AI result', f'{inspection.get_status_display()} / {inspection.predicted_class or "-"}'),
            ('Final reviewed decision', final),
        ])
    story.append(_table(rows or [('Inspections', 'No inspection records are associated with this batch.')], styles))
    story.extend([
        _section('Technical disclaimer', styles),
        Paragraph('Model confidence and defect probability are uncalibrated model scores. This batch summary is an operational decision-support record and must not be interpreted as proof of product safety, manufacturing root cause, or real-world quality performance.', styles['small']),
    ])
    try:
        doc.build(story, onFirstPage=_header_footer, onLaterPages=_header_footer)
    except Exception as exc:
        raise ReportGenerationError('The batch PDF report could not be generated.') from exc
    output.seek(0)
    return output, reference
