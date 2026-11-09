from django.urls import path

from .views import api, pages

urlpatterns = [
    # Pages
    path('inspections/', pages.inspections_home, name='inspections-home'),
    path('inspections/upload/', pages.upload, name='inspection-upload'),
    path('inspections/history/', pages.engineer_history, name='inspection-history'),
    path('quality-incidents/', pages.quality_incident_list, name='quality-incident-list'),
    path('quality-incidents/<int:pk>/', pages.quality_incident_detail, name='quality-incident-detail'),
    path(
        'quality-incidents/<int:pk>/review/',
        pages.quality_incident_review,
        name='quality-incident-review',
    ),
    path(
        'quality-incidents/sops/<str:document_id>/<str:revision>/page/<int:page_number>/',
        pages.quality_sop_source,
        name='quality-sop-source',
    ),
    path(
        'inspections/<int:pk>/quality-incident/',
        pages.create_quality_incident,
        name='quality-incident-create',
    ),
    path('inspections/review-queue/', pages.supervisor_dashboard, name='inspection-supervisor'),
    path('inspections/manage/', pages.admin_dashboard, name='inspection-admin'),
    path('inspections/intelligence/', pages.quality_intelligence, name='inspection-intelligence'),
    path('inspections/evaluation/', pages.evaluation_dashboard, name='inspection-evaluation'),
    path('inspections/dataset/', pages.dataset_dashboard, name='inspection-dataset'),
    path('inspections/<int:pk>/', pages.inspection_detail, name='inspection-detail'),
    path('inspections/<int:pk>/review/', pages.review_submit, name='inspection-review-form'),
    path('inspections/<int:pk>/corrective-action/', pages.corrective_action_submit, name='inspection-corrective-action'),
    path('inspections/<int:pk>/image/', pages.inspection_image, name='inspection-image'),
    path('inspections/<int:pk>/report/pdf/', pages.inspection_pdf_report, name='inspection-pdf-report'),
    path('inspections/batches/<int:pk>/', pages.batch_detail, name='inspection-batch'),
    path('inspections/batches/<int:pk>/report/pdf/', pages.batch_pdf_report, name='inspection-batch-pdf-report'),

    # REST API
    path('api/products/', api.ProductListCreateView.as_view(), name='api-products'),
    path('api/products/<str:product_id>/', api.ProductDetailView.as_view(), name='api-product-detail'),
    path('api/inspections/upload/', api.SingleUploadView.as_view(), name='api-inspection-upload'),
    path('api/inspections/batch/', api.BatchUploadView.as_view(), name='api-inspection-batch'),
    path('api/inspections/', api.InspectionListView.as_view(), name='api-inspections'),
    path('api/inspections/<int:pk>/', api.InspectionDetailView.as_view(), name='api-inspection-detail'),
    path('api/inspections/<int:pk>/review/', api.ReviewView.as_view(), name='api-inspection-review'),
    path(
        'api/inspections/<int:pk>/incident/',
        api.CreateIncidentAPIView.as_view(),
        name='api-quality-incident-create',
    ),
    path('api/quality-incidents/', api.IncidentListAPIView.as_view(), name='api-quality-incident-list'),
    path(
        'api/quality-incidents/<int:pk>/',
        api.IncidentDetailAPIView.as_view(),
        name='api-quality-incident-detail',
    ),
    path(
        'api/quality-incidents/<int:pk>/review/',
        api.IncidentReviewAPIView.as_view(),
        name='api-quality-incident-review',
    ),
    path('api/batches/<int:pk>/', api.BatchDetailView.as_view(), name='api-batch-detail'),
    path('api/supervisor/inspections/', api.SupervisorInspectionListView.as_view(), name='api-supervisor-inspections'),
]
