import csv
import re
import tempfile
import threading
from pathlib import Path
from unittest import mock

from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from assistant.models import Role, User
from inspections import services, sop_service
from inspections.models import Inspection, InspectionBatch, Product, QualityIncident

from .base import InspectionTestCase


class QualityIncidentTests(InspectionTestCase):
    def create_incident(self, inspection=None, user=None):
        return services.create_quality_incident(
            (inspection or self.inspection).pk,
            user or self.engineer,
        )

    def setUp(self):
        super().setUp()
        self.inspection = self.make_completed(predicted='Defective')

    def test_only_completed_original_defective_prediction_is_eligible(self):
        normal = self.make_completed(predicted='Normal', name='normal.png')
        self.assertEqual(services.check_inspection_eligibility(self.inspection)[0], True)
        eligible, message = services.check_inspection_eligibility(normal)
        self.assertFalse(eligible)
        self.assertIn('original model prediction', message)

        for state in (Inspection.Status.QUEUED, Inspection.Status.PROCESSING, Inspection.Status.FAILED):
            candidate = self.make_completed(name=f'{state}.png')
            candidate.status = state
            candidate.predicted_class = None
            eligible, _ = services.check_inspection_eligibility(candidate)
            self.assertFalse(eligible, state)

    def test_human_correction_does_not_change_original_prediction_eligibility(self):
        services.submit_review(self.inspection.pk, self.supervisor, 'Normal', 'Correction recorded separately')
        self.inspection.refresh_from_db()
        self.assertEqual(self.inspection.predicted_class, 'Defective')
        incident, created = self.create_incident()
        self.assertTrue(created)
        self.assertEqual(incident.original_predicted_class, 'Defective')

        normal = self.make_completed(predicted='Normal', name='original-normal.png')
        services.submit_review(normal.pk, self.supervisor, 'Defective', 'Correction recorded separately')
        with self.assertRaises(services.EligibilityError):
            self.create_incident(normal)
        self.assertFalse(QualityIncident.objects.filter(inspection=normal).exists())

    def test_real_plate_mapping_retrieves_both_required_sops_only(self):
        valid, reason, passages = sop_service.retrieve_sop_evidence('P-PLATE01')
        self.assertTrue(valid, reason)
        self.assertTrue(passages)
        self.assertEqual({p.document_id for p in passages}, {'Q-PLATE-01', 'Q-PLATE-02'})
        self.assertEqual({p.revision for p in passages}, {'1.0'})
        self.assertTrue(all(p.product_id == 'P-PLATE01' and p.page_number in (1, 2) for p in passages))
        self.assertFalse(any('GASKET' in p.document_id for p in passages))

    def test_mapping_rejects_missing_or_incorrect_required_plate_reference(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mapping = Path(temp_dir) / 'product_sop_mapping.csv'
            with mapping.open('w', newline='', encoding='utf-8') as stream:
                writer = csv.writer(stream)
                writer.writerow(['product_id', 'product_name', 'document_id', 'revision', 'pdf_filename', 'required'])
                writer.writerow(['P-PLATE01', 'Plate', 'Q-PLATE-01', '9.0', 'Q_PLATE_01_Containment_Rev1_0.pdf', 'true'])
                writer.writerow(['P-PLATE01', 'Plate', 'Q-PLATE-02', '1.0', 'Q_PLATE_02_Review_Rev1_0.pdf', 'true'])
            with mock.patch.object(sop_service, 'find_mapping_file', return_value=mapping):
                valid, reason, _ = sop_service.retrieve_sop_evidence('P-PLATE01')
        self.assertFalse(valid)
        self.assertIn('Q-PLATE-01', reason)

    def test_no_relevant_passages_is_insufficient(self):
        mapped, _, mappings = sop_service.get_product_sop_mappings('P-PLATE01')
        self.assertTrue(mapped)
        readable, _, paths = sop_service.validate_sop_files(mappings)
        self.assertTrue(readable)
        passages = sop_service.extract_sop_chunks(mappings, paths)
        for passage in passages:
            passage.text = 'unrelated manufacturing reference material with distinct vocabulary'
        with mock.patch.object(sop_service, 'extract_sop_chunks', return_value=passages):
            valid, reason, evidence = sop_service.retrieve_sop_evidence('P-PLATE01')
        self.assertFalse(valid)
        self.assertFalse(evidence)
        self.assertIn('relevant passages', reason)

    def test_insufficient_evidence_keeps_incident_and_blocks_approval(self):
        with mock.patch.object(sop_service, 'find_mapping_file', return_value=None):
            incident, created = self.create_incident()
        self.assertTrue(created)
        self.assertEqual(incident.evidence_status, QualityIncident.EvidenceStatus.INSUFFICIENT)
        self.assertTrue(incident.evidence_failure_reason)
        self.assertEqual(incident.suggested_actions, [])
        with self.assertRaises(services.ReviewError):
            services.submit_incident_review(
                incident.pk, self.supervisor, QualityIncident.ReviewStatus.APPROVED, 'Proceed'
            )
        reviewed = services.submit_incident_review(
            incident.pk, self.supervisor, QualityIncident.ReviewStatus.NEEDS_INFO, 'Required SOP mapping is missing'
        )
        self.assertEqual(reviewed.review_status, QualityIncident.ReviewStatus.NEEDS_INFO)
        self.assertEqual(reviewed.reviewed_by, self.supervisor)
        self.assertIsNotNone(reviewed.reviewed_at)

    def test_missing_or_unreadable_required_sop_creates_manual_review_incident(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            empty_sops = Path(temp_dir)
            with mock.patch.object(sop_service, 'find_sops_dir', return_value=empty_sops):
                valid, reason, _ = sop_service.retrieve_sop_evidence('P-PLATE01')
        self.assertFalse(valid)
        self.assertIn('Required SOP file', reason)

        with tempfile.TemporaryDirectory() as temp_dir:
            sops_dir = Path(temp_dir)
            (sops_dir / 'Q_PLATE_01_Containment_Rev1_0.pdf').write_bytes(b'not a PDF')
            (sops_dir / 'Q_PLATE_02_Review_Rev1_0.pdf').write_bytes(b'not a PDF')
            with mock.patch.object(sop_service, 'find_sops_dir', return_value=sops_dir):
                incident, created = self.create_incident()
        self.assertTrue(created)
        self.assertEqual(incident.evidence_status, QualityIncident.EvidenceStatus.INSUFFICIENT)
        self.assertIn('unreadable', incident.evidence_failure_reason.lower())
        self.assertEqual(incident.suggested_actions, [])

    def test_generation_failure_preserves_incident_as_insufficient(self):
        with mock.patch(
            'inspections.incident_generator.generate_suggested_actions',
            side_effect=RuntimeError('test generation failure'),
        ):
            incident, created = self.create_incident()
        self.assertTrue(created)
        self.assertEqual(incident.evidence_status, QualityIncident.EvidenceStatus.INSUFFICIENT)
        self.assertIn('generation failed', incident.evidence_failure_reason.lower())
        self.assertEqual(incident.suggested_actions, [])

    def test_duplicate_creation_preserves_id_content_and_saved_review(self):
        incident, created = self.create_incident()
        self.assertTrue(created)
        self.assertEqual(incident.evidence_status, QualityIncident.EvidenceStatus.SUFFICIENT)
        saved_actions = incident.suggested_actions
        saved_sources = incident.source_references
        services.submit_incident_review(
            incident.pk, self.supervisor, QualityIncident.ReviewStatus.APPROVED, 'Evidence supports follow-up'
        )

        repeated, repeated_created = self.create_incident()
        repeated.refresh_from_db()
        self.assertFalse(repeated_created)
        self.assertEqual(repeated.pk, incident.pk)
        self.assertEqual(repeated.suggested_actions, saved_actions)
        self.assertEqual(repeated.source_references, saved_sources)
        self.assertEqual(repeated.review_status, QualityIncident.ReviewStatus.APPROVED)
        self.assertEqual(repeated.supervisor_comment, 'Evidence supports follow-up')

    def test_api_duplicate_returns_same_incident_without_creating_another(self):
        url = reverse('api-quality-incident-create', args=[self.inspection.pk])
        client = self.client_for(self.engineer)
        first = client.post(url, {}, format='json')
        second = client.post(url, {}, format='json')
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()['id'], second.json()['id'])
        self.assertEqual(QualityIncident.objects.filter(inspection=self.inspection).count(), 1)

    def test_database_enforces_one_incident_per_inspection(self):
        incident, _ = self.create_incident()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                QualityIncident.objects.create(
                    inspection=self.inspection,
                    product_id=incident.product_id,
                    original_predicted_class=incident.original_predicted_class,
                    original_model_version=incident.original_model_version,
                    summary='duplicate',
                    created_by=self.engineer,
                )
        self.assertEqual(QualityIncident.objects.filter(inspection=self.inspection).count(), 1)

    def test_citations_are_real_and_source_links_open_the_mapped_pdf(self):
        incident, _ = self.create_incident()
        self.assertTrue(incident.suggested_actions)
        self.assertEqual(
            {action['document_id'] for action in incident.suggested_actions},
            {'Q-PLATE-01', 'Q-PLATE-02'},
        )
        for action in incident.suggested_actions:
            self.assertIn(action['document_id'], {'Q-PLATE-01', 'Q-PLATE-02'})
            self.assertEqual(action['revision'], '1.0')
            self.assertIn(action['page_number'], (1, 2))
            self.assertTrue(action['supporting_passage'])
            passage = next(
                source for source in incident.source_references
                if source['document_id'] == action['document_id']
                and source['revision'] == action['revision']
                and source['page_number'] == action['page_number']
            )
            self.assertIn(
                re.sub(r'\s+', ' ', action['supporting_passage']).strip(),
                re.sub(r'\s+', ' ', passage['passage']).strip(),
            )
            response = self.client_for(self.engineer).get(action['source_link'].split('#', 1)[0])
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response['Content-Type'], 'application/pdf')
            self.assertTrue(b''.join(response.streaming_content).startswith(b'%PDF'))

        bad_revision = reverse(
            'quality-sop-source',
            kwargs={'document_id': 'Q-PLATE-01', 'revision': '9.9', 'page_number': 1},
        )
        self.assertEqual(self.client_for(self.engineer).get(bad_revision).status_code, 404)

    def test_engineer_scope_and_supervisor_only_review_are_enforced(self):
        incident, _ = self.create_incident()
        other_inspection = self.make_completed(user=self.engineer2, name='other.png')
        other_incident, _ = self.create_incident(other_inspection, self.engineer2)

        own_list = self.client_for(self.engineer).get(reverse('api-quality-incident-list'))
        self.assertEqual(own_list.status_code, 200)
        data = own_list.json()
        results = data.get('results', data)
        self.assertEqual({item['id'] for item in results}, {incident.pk})
        cross_user = self.client_for(self.engineer).get(
            reverse('api-quality-incident-detail', args=[other_incident.pk])
        )
        self.assertEqual(cross_user.status_code, 404)

        review_url = reverse('api-quality-incident-review', args=[incident.pk])
        payload = {'decision': 'approved', 'comment': 'Reviewed'}
        self.assertEqual(self.client_for(self.engineer).post(review_url, payload, format='json').status_code, 403)
        self.assertEqual(self.client_for(self.admin).post(review_url, payload, format='json').status_code, 403)
        self.assertEqual(self.client_for(self.supervisor).post(review_url, payload, format='json').status_code, 200)

    def test_repeated_supervisor_review_cannot_overwrite_saved_decision(self):
        incident, _ = self.create_incident()
        services.submit_incident_review(
            incident.pk, self.supervisor, QualityIncident.ReviewStatus.NEEDS_INFO, 'Need a clearer image'
        )
        with self.assertRaises(services.ReviewError):
            services.submit_incident_review(
                incident.pk, self.supervisor, QualityIncident.ReviewStatus.APPROVED, 'Changed decision'
            )
        incident.refresh_from_db()
        self.assertEqual(incident.review_status, QualityIncident.ReviewStatus.NEEDS_INFO)
        self.assertEqual(incident.supervisor_comment, 'Need a clearer image')


class QualityIncidentPageTests(InspectionTestCase):
    def setUp(self):
        super().setUp()
        self.inspection = self.make_completed(predicted='Defective')

    def test_engineer_can_create_and_reopen_incident_from_inspection(self):
        client = self.client_for(self.engineer)
        detail = client.get(reverse('inspection-detail', args=[self.inspection.pk]))
        self.assertContains(detail, 'Create Quality Incident')
        response = client.post(reverse('quality-incident-create', args=[self.inspection.pk]))
        incident = QualityIncident.objects.get(inspection=self.inspection)
        self.assertRedirects(response, reverse('quality-incident-detail', args=[incident.pk]))
        self.assertContains(client.get(reverse('quality-incident-detail', args=[incident.pk])), 'Incident summary')
        self.assertContains(client.get(reverse('quality-incident-detail', args=[incident.pk])), 'Q-PLATE-02')

    def test_normal_inspection_displays_reason_and_does_not_create_incident(self):
        normal = self.make_completed(predicted='Normal', name='normal-display.png')
        response = self.client_for(self.engineer).get(reverse('inspection-detail', args=[normal.pk]))
        self.assertContains(response, 'only completed Defective inspections are eligible')
        self.assertNotContains(response, 'Create Quality Incident')
        self.assertEqual(QualityIncident.objects.count(), 0)

    def test_incident_list_and_supervisor_review_survive_page_reload(self):
        incident, _ = services.create_quality_incident(self.inspection.pk, self.engineer)
        engineer = self.client_for(self.engineer)
        self.assertContains(engineer.get(reverse('quality-incident-list')), f'#{incident.pk}')
        self.assertContains(
            engineer.get(reverse('quality-incident-detail', args=[incident.pk])),
            'Only a Supervisor may review this incident.',
        )

        supervisor = self.client_for(self.supervisor)
        detail_url = reverse('quality-incident-detail', args=[incident.pk])
        self.assertContains(supervisor.get(detail_url), 'Save supervisor decision')
        response = supervisor.post(
            reverse('quality-incident-review', args=[incident.pk]),
            {'decision': 'approved', 'comment': 'Evidence supports documented follow-up'},
        )
        self.assertRedirects(response, detail_url)
        reloaded = supervisor.get(detail_url)
        self.assertContains(reloaded, 'Approved for follow-up')
        self.assertContains(reloaded, 'Evidence supports documented follow-up')
        self.assertNotContains(reloaded, 'Save supervisor decision')

    def test_engineer_cannot_open_another_engineers_incident(self):
        incident, _ = services.create_quality_incident(self.inspection.pk, self.engineer)
        other = self.client_for(self.engineer2).get(reverse('quality-incident-detail', args=[incident.pk]))
        self.assertEqual(other.status_code, 404)

    def test_supervisor_workspace_shows_database_summary_and_filters(self):
        sufficient, _ = services.create_quality_incident(self.inspection.pk, self.engineer)
        other_inspection = self.make_completed(user=self.engineer2, name='insufficient-evidence.png')
        with mock.patch.object(sop_service, 'find_mapping_file', return_value=None):
            insufficient, _ = services.create_quality_incident(other_inspection.pk, self.engineer2)

        client = self.client_for(self.supervisor)
        page = client.get(reverse('quality-incident-list'))
        self.assertContains(page, 'Total incidents')
        self.assertContains(page, 'Pending review')
        self.assertContains(page, 'Sufficient evidence')
        self.assertContains(page, 'Manual review required')
        self.assertContains(page, f'#{sufficient.pk}')
        self.assertContains(page, f'#{insufficient.pk}')

        filtered = client.get(reverse('quality-incident-list'), {'q': str(insufficient.pk), 'evidence': 'insufficient'})
        self.assertContains(filtered, f'#{insufficient.pk}')
        self.assertNotContains(filtered, f'#{sufficient.pk}')
        self.assertContains(filtered, 'Recently reviewed')

    def test_engineer_dashboard_and_history_show_real_eligibility(self):
        upload_page = self.client_for(self.engineer).get(reverse('inspection-upload'))
        self.assertContains(upload_page, 'Incident eligible')
        self.assertContains(upload_page, 'Completed · original Defective')
        self.assertContains(upload_page, 'Open incidents')
        history = self.client_for(self.engineer).get(reverse('inspection-history'))
        self.assertContains(history, 'Quality incident')
        self.assertContains(history, 'Eligible')
        self.assertNotContains(history, 'Not eligible')

    def test_admin_dashboard_shows_incident_and_user_counts_without_review_actions(self):
        incident, _ = services.create_quality_incident(self.inspection.pk, self.engineer)
        response = self.client_for(self.admin).get(reverse('inspection-admin'))
        self.assertContains(response, 'Quality incident oversight')
        self.assertContains(response, 'Total incidents')
        self.assertContains(response, 'Engineers')
        self.assertContains(response, 'Supervisors')
        self.assertContains(response, f'#{incident.pk}')
        self.assertContains(response, 'Read-only operational visibility')
        self.assertNotContains(response, 'Save supervisor decision')


class QualityIncidentConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.engineer = User.objects.create_user('parallel-engineer', password='pw', role=Role.ENGINEER)
        self.product = Product.objects.create(product_id='P-PLATE01', name='Demonstration metal plate')
        batch = InspectionBatch.objects.create(
            product=self.product,
            submitted_by=self.engineer,
            total_files=1,
        )
        self.inspection = Inspection.objects.create(
            batch=batch,
            product=self.product,
            submitting_user=self.engineer,
            image='inspections/concurrent-test.png',
            original_filename='concurrent-test.png',
            status=Inspection.Status.COMPLETED,
            predicted_class=Inspection.PredictedClass.DEFECTIVE,
            confidence=0.9,
            defect_probability=0.9,
            model_version='TEST-MODEL-1.0',
            completed_at=timezone.now(),
        )

    def test_simultaneous_requests_return_one_persistent_incident(self):
        barrier = threading.Barrier(2)
        results = []
        failures = []

        def create_incident():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                incident, created = services.create_quality_incident(self.inspection.pk, self.engineer)
                results.append((incident.pk, created))
            except Exception as exc:
                failures.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=create_incident) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertFalse(any(thread.is_alive() for thread in threads), 'Concurrent incident requests did not finish.')
        self.assertEqual(failures, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len({incident_id for incident_id, _ in results}), 1)
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(QualityIncident.objects.filter(inspection=self.inspection).count(), 1)
