"""TEST 1 (mandatory): an Engineer cannot upload manuals. Plus other server-side role checks."""

from django.urls import reverse

from assistant.models import DocumentChunk, Manual

from .base import BaseTestCase, pdf_upload

PAGES = ['HP-MNT-01 Rev 1.0 Page 1\nCoverage of the maintenance manual', 'HP-MNT-01 Page 2\nInspection schedule']


class ManualUploadPermissionTests(BaseTestCase):
    def payload(self):
        return {
            'machine': 'M-HP200', 'document_id': 'HP-MNT-01', 'document_name': 'HP 200 Maintenance Manual',
            'revision': '1.0', 'file': pdf_upload(PAGES),
        }

    def test_engineer_upload_is_forbidden_and_creates_nothing(self):
        """TEST 1: authenticated Engineer -> HTTP 403 and no Manual (or chunks) created."""
        response = self.client_for(self.engineer).post(reverse('api-manuals'), self.payload(), format='multipart')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Manual.objects.count(), 0)
        self.assertEqual(DocumentChunk.objects.count(), 0)

    def test_supervisor_upload_is_forbidden(self):
        response = self.client_for(self.supervisor).post(reverse('api-manuals'), self.payload(), format='multipart')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Manual.objects.count(), 0)

    def test_anonymous_upload_is_denied(self):
        response = self.client.post(reverse('api-manuals'), self.payload())
        self.assertIn(response.status_code, (401, 403))
        self.assertEqual(Manual.objects.count(), 0)

    def test_engineer_cannot_reindex_register_machines_or_open_admin_pages(self):
        client = self.client_for(self.engineer)
        manual = self.make_manual(self.hp, 'HP-MNT-01', chunks=[(1, 'x')])
        self.assertEqual(client.post(reverse('api-manual-reindex', args=[manual.pk])).status_code, 403)
        self.assertEqual(client.get(reverse('api-manuals')).status_code, 403)
        machine = {'machine_id': 'M-X', 'model_name': 'X', 'name': 'X'}
        self.assertEqual(client.post(reverse('api-machines'), machine, format='json').status_code, 403)
        self.assertEqual(client.get(reverse('manual-upload')).status_code, 403)
        self.assertEqual(client.get(reverse('admin-dashboard')).status_code, 403)

    def test_administrator_can_upload(self):
        response = self.client_for(self.admin).post(reverse('api-manuals'), self.payload(), format='multipart')
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Manual.objects.count(), 1)
        self.assertEqual(response.json()['status'], 'indexed')

    def test_administrator_can_register_machine(self):
        data = {'machine_id': 'M-NEW1', 'model_name': 'N-1', 'name': 'New machine'}
        response = self.client_for(self.admin).post(reverse('api-machines'), data, format='json')
        self.assertEqual(response.status_code, 201)

    def test_supervisor_and_admin_cannot_ask_questions(self):
        body = {'machine': 'M-HP200', 'question': 'What does E17 mean?'}
        for user in (self.supervisor, self.admin):
            response = self.client_for(user).post(reverse('api-ask'), body, format='json')
            self.assertEqual(response.status_code, 403, user.role)

    def test_pdf_requires_login(self):
        manual = self.make_manual(self.hp, 'HP-MNT-01', chunks=[(1, 'x')])
        self.assertEqual(self.client.get(reverse('manual-file', args=[manual.pk])).status_code, 302)
