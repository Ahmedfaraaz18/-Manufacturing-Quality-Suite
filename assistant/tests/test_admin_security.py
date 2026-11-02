"""Regression coverage for the separate Django Admin permission boundary."""

from django.contrib.auth.models import Permission
from django.urls import reverse

from assistant.models import DocumentChunk, Manual

from .base import BaseTestCase, pdf_upload


class DjangoAdminSecurityTests(BaseTestCase):
    """Django Admin requires both the application role and narrow model permissions."""

    def grant(self, user, *codenames):
        permissions = Permission.objects.filter(content_type__app_label='assistant', codename__in=codenames)
        self.assertEqual(permissions.count(), len(codenames), codenames)
        user.user_permissions.add(*permissions)

    def prepare_staff_user_with_manual_permissions(self, user):
        """Simulate accidental staff and model-permission assignment in the test DB."""
        user.is_staff = True
        user.save(update_fields=['is_staff'])
        self.grant(
            user,
            'view_machine',
            'view_manual',
            'delete_manual',
            'view_documentchunk',
            'delete_documentchunk',
        )

    def make_upload_payload(self):
        return {
            'machine': 'M-HP200',
            'document_id': 'HP-MNT-01',
            'document_name': 'HP 200 Maintenance Manual',
            'revision': '1.0',
            'file': pdf_upload([
                'HP-MNT-01 Rev 1.0 Page 1\\nMaintenance instructions',
                'HP-MNT-01 Rev 1.0 Page 2\\nInspection schedule',
            ]),
        }

    def test_engineer_cannot_access_admin_or_delete_manual_even_with_staff_and_permissions(self):
        manual = self.make_manual(self.hp, 'HP-MNT-01', chunks=[(1, 'Engineer deletion must be blocked.')])
        self.prepare_staff_user_with_manual_permissions(self.engineer)
        client = self.client_for(self.engineer)

        self.assertEqual(client.get(reverse('admin:assistant_manual_changelist')).status_code, 403)
        self.assertEqual(
            client.post(reverse('admin:assistant_manual_delete', args=[manual.pk]), {'post': 'yes'}).status_code,
            403,
        )
        self.assertTrue(Manual.objects.filter(pk=manual.pk).exists())
        self.assertTrue(DocumentChunk.objects.filter(manual=manual).exists())

    def test_supervisor_cannot_access_admin_or_delete_manual_even_with_staff_and_permissions(self):
        manual = self.make_manual(self.hp, 'HP-MNT-01', chunks=[(1, 'Supervisor deletion must be blocked.')])
        self.prepare_staff_user_with_manual_permissions(self.supervisor)
        client = self.client_for(self.supervisor)

        self.assertEqual(client.get(reverse('admin:assistant_manual_changelist')).status_code, 403)
        self.assertEqual(
            client.post(reverse('admin:assistant_manual_delete', args=[manual.pk]), {'post': 'yes'}).status_code,
            403,
        )
        self.assertTrue(Manual.objects.filter(pk=manual.pk).exists())
        self.assertTrue(DocumentChunk.objects.filter(manual=manual).exists())

    def test_administrator_can_upload_then_delete_manual_and_its_chunks(self):
        # Upload remains on the validated application endpoint, not the raw ModelAdmin add form.
        response = self.client_for(self.admin).post(
            reverse('api-manuals'), self.make_upload_payload(), format='multipart'
        )
        self.assertEqual(response.status_code, 201, response.content)
        manual = Manual.objects.get()
        self.assertGreater(DocumentChunk.objects.filter(manual=manual).count(), 0)

        # Deliberately grant only the permissions Django Admin needs for viewing and
        # the Manual -> DocumentChunk deletion cascade; the test user is not a superuser.
        self.prepare_staff_user_with_manual_permissions(self.admin)
        response = self.client_for(self.admin).post(
            reverse('admin:assistant_manual_delete', args=[manual.pk]), {'post': 'yes'}
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Manual.objects.filter(pk=manual.pk).exists())
        self.assertFalse(DocumentChunk.objects.filter(manual_id=manual.pk).exists())

    def test_regular_administrator_cannot_manage_user_privilege_flags(self):
        self.admin.is_staff = True
        self.admin.save(update_fields=['is_staff'])
        self.grant(self.admin, 'view_user', 'change_user')

        response = self.client_for(self.admin).get(reverse('admin:assistant_user_changelist'))
        self.assertEqual(response.status_code, 403)
