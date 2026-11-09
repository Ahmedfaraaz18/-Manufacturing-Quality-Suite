"""Server-rendered pages: role routing, history filters, pagination and output escaping."""

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from inspections import services
from inspections.models import Inspection

from .base import InspectionTestCase, upload


class PageAccessTests(InspectionTestCase):
    def test_role_aware_shell_has_current_navigation_and_keyboard_skip_link(self):
        pages = (
            (self.engineer, 'inspection-upload', 'New inspection'),
            (self.supervisor, 'inspection-supervisor', 'Inspection reviews'),
            (self.admin, 'inspection-admin', 'Products &amp; overview'),
        )
        for user, page_name, current_link in pages:
            response = self.client_for(user).get(reverse(page_name))
            self.assertContains(response, 'Skip to content')
            self.assertContains(response, 'aria-current="page"')
            self.assertContains(response, current_link)
            self.assertContains(response, 'aria-controls="mobile-nav"')

    def test_login_lands_on_the_role_workspace(self):
        expected = {
            'admin1': ('pw-Admin-1!', 'inspection-admin'), 'eng1': ('pw-Eng-1!', 'inspection-upload'),
            'sup1': ('pw-Sup-1!', 'inspection-supervisor'),
        }
        for username, (password, target) in expected.items():
            self.client.logout()
            response = self.client.post(reverse('login'), {'username': username, 'password': password})
            self.assertRedirects(response, reverse('inspections-home'), fetch_redirect_response=False)
            self.assertRedirects(self.client.get(reverse('inspections-home')), reverse(target), fetch_redirect_response=False)
            self.assertEqual(self.client.get(reverse(target)).status_code, 200)
            self.assertEqual(self.client.get(reverse('home')).status_code, 302)   # '/' still routes signed-in users

    def test_each_role_reaches_only_its_own_workspace(self):
        pages = {
            'inspection-admin': self.admin, 'inspection-upload': self.engineer,
            'inspection-history': self.engineer, 'inspection-supervisor': self.supervisor,
        }
        users = (self.admin, self.engineer, self.supervisor)
        for name, owner in pages.items():
            for user in users:
                expected = 200 if user == owner else 403
                self.assertEqual(self.client_for(user).get(reverse(name)).status_code, expected, f'{name} as {user.role}')
        for name in pages:
            self.assertEqual(self.client.get(reverse(name)).status_code, 302)

    def test_round_one_dashboards_are_still_available(self):
        self.assertEqual(self.client_for(self.engineer).get(reverse('engineer-dashboard')).status_code, 200)
        self.assertEqual(self.client_for(self.admin).get(reverse('admin-dashboard')).status_code, 200)
        self.assertEqual(self.client_for(self.supervisor).get(reverse('supervisor-dashboard')).status_code, 200)

    def test_upload_page_lists_active_products_and_handles_no_product(self):
        page = self.client_for(self.engineer).get(reverse('inspection-upload'))
        self.assertContains(page, 'P-PLATE01')
        self.assertContains(page, 'Single image')
        self.product.is_active = False
        self.product.save()
        self.assertContains(self.client_for(self.engineer).get(reverse('inspection-upload')), 'No product is registered')

    def test_review_form_is_shown_only_to_supervisors_for_unreviewed_completed_records(self):
        item = self.make_completed()
        url = reverse('inspection-detail', args=[item.pk])
        self.assertContains(self.client_for(self.supervisor).get(url), 'Confirm: Defective')
        self.assertNotContains(self.client_for(self.engineer).get(url), 'Confirm: Defective')
        self.assertNotContains(self.client_for(self.admin).get(url), 'Confirm: Defective')
        services.submit_review(item.pk, self.supervisor, 'Normal', 'done')
        self.assertNotContains(self.client_for(self.supervisor).get(url), 'Confirm: Defective')

    def test_detail_page_shows_prediction_confidence_and_model_version(self):
        item = self.make_completed()
        html = self.client_for(self.engineer).get(reverse('inspection-detail', args=[item.pk])).content.decode()
        for fragment in ('Defective', '90.00%', 'TEST-MODEL-1.0', 'Original model output'):
            self.assertIn(fragment, html)
        self.assertIn('class="page-header inspection-detail-header"', html)
        self.assertIn('aria-label="Breadcrumb"', html)
        self.assertIn('class="inspection-detail-heading-row"', html)
        self.assertNotIn('class="topbar-title"', html)

    def test_batch_page_renders_for_its_owner(self):
        item = self.make_completed()
        self.assertEqual(self.client_for(self.engineer).get(reverse('inspection-batch', args=[item.batch_id])).status_code, 200)
        self.assertEqual(self.client_for(self.supervisor).get(reverse('inspection-batch', args=[item.batch_id])).status_code, 200)

    def test_user_supplied_file_names_are_escaped(self):
        hostile = '<img src=x onerror=alert(1)>.png'
        with self.worker():
            batch = services.create_batch(self.engineer, self.product, [upload(hostile)]).batch
        item = batch.inspections.get()
        for user, name in ((self.engineer, 'inspection-history'), (self.supervisor, 'inspection-supervisor')):
            html = self.client_for(user).get(reverse(name)).content.decode()
            self.assertNotIn(hostile, html)
            self.assertIn('&lt;img src=x onerror=alert(1)&gt;.png', html)
        detail = self.client_for(self.engineer).get(reverse('inspection-detail', args=[item.pk])).content.decode()
        self.assertNotIn(hostile, detail)

    def test_access_denied_page_is_rendered_for_wrong_role(self):
        response = self.client_for(self.engineer).get(reverse('inspection-admin'))
        self.assertContains(response, 'Access denied', status_code=403)

    def test_dataset_inventory_is_not_exposed_in_normal_admin_ui(self):
        """Technical inventory remains restricted, but is not part of the demo product UI."""
        dashboard = self.client_for(self.admin).get(reverse('inspection-admin'))
        self.assertContains(dashboard, 'Integrity and evaluation')
        self.assertNotContains(dashboard, 'Dataset inventory')
        self.assertNotContains(dashboard, 'Organizer data inventory')
        self.assertNotContains(dashboard, 'Registered samples')
        self.assertEqual(self.client_for(self.admin).get(reverse('inspection-dataset')).status_code, 200)
        self.assertEqual(self.client_for(self.engineer).get(reverse('inspection-dataset')).status_code, 403)
        self.assertEqual(self.client_for(self.supervisor).get(reverse('inspection-dataset')).status_code, 403)


class HistoryFilterTests(InspectionTestCase):
    def setUp(self):
        super().setUp()
        self.defective = self.make_completed(self.engineer, 'Defective', 'scratch_a.png')
        self.normal = self.make_completed(self.engineer, 'Normal', 'clean_b.png')
        self.other = self.make_completed(self.engineer2, 'Defective', 'other_c.png')
        with self.worker():
            self.failed = services.create_batch(self.engineer, self.product, [upload('broken.png', b'nope')]).batch.inspections.get()
        services.submit_review(self.defective.pk, self.supervisor, 'Normal', 'fine')

    def ids(self, user, **params):
        response = self.client_for(user).get(reverse('api-inspections'), params)
        self.assertEqual(response.status_code, 200)
        return {row['id'] for row in response.json()['results']}

    def test_prediction_filter(self):
        self.assertEqual(self.ids(self.supervisor, predicted_class='Defective'), {self.defective.pk, self.other.pk})
        self.assertEqual(self.ids(self.supervisor, predicted_class='Normal'), {self.normal.pk})
        self.assertEqual(self.ids(self.engineer, predicted_class='Defective'), {self.defective.pk})

    def test_status_filter(self):
        self.assertEqual(self.ids(self.supervisor, status='failed'), {self.failed.pk})
        self.assertEqual(self.ids(self.supervisor, status='completed'), {self.defective.pk, self.normal.pk, self.other.pk})

    def test_review_status_filter(self):
        self.assertEqual(self.ids(self.supervisor, reviewed='yes'), {self.defective.pk})
        self.assertEqual(self.ids(self.supervisor, reviewed='no'), {self.normal.pk, self.other.pk, self.failed.pk})

    def test_search_by_filename_user_and_id(self):
        self.assertEqual(self.ids(self.supervisor, q='scratch'), {self.defective.pk})
        self.assertEqual(self.ids(self.supervisor, q='eng2'), {self.other.pk})
        # Numeric search always includes the matching inspection ID.  Do not assume a
        # particular auto-increment value (and therefore accidental overlap with ``eng2``).
        self.assertIn(self.normal.pk, self.ids(self.supervisor, q=str(self.normal.pk)))

    def test_date_filters(self):
        today = timezone.localdate()
        self.assertEqual(len(self.ids(self.supervisor, date_from=today.isoformat(), date_to=today.isoformat())), 4)
        tomorrow = (today + timedelta(days=1)).isoformat()
        self.assertEqual(self.ids(self.supervisor, date_from=tomorrow), set())
        old = Inspection.objects.get(pk=self.normal.pk)
        Inspection.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=10))
        self.assertNotIn(self.normal.pk, self.ids(self.supervisor, date_from=(today - timedelta(days=2)).isoformat()))
        self.assertEqual(self.ids(self.supervisor, date_to=(today - timedelta(days=5)).isoformat()), {self.normal.pk})
        self.assertEqual(len(self.ids(self.supervisor, date_from='not-a-date')), 4)   # malformed values are ignored

    def test_combined_filters(self):
        self.assertEqual(self.ids(self.supervisor, predicted_class='Defective', reviewed='no'), {self.other.pk})

    def test_history_pages_apply_the_same_filters(self):
        html = self.client_for(self.supervisor).get(reverse('inspection-supervisor'), {'predicted_class': 'Normal'}).content.decode()
        self.assertIn('clean_b.png', html)
        self.assertNotIn('scratch_a.png', html)
        mine = self.client_for(self.engineer).get(reverse('inspection-history')).content.decode()
        self.assertNotIn('other_c.png', mine)
        self.assertIn('scratch_a.png', mine)

    def test_pagination(self):
        for i in range(17):
            Inspection.objects.create(
                batch=self.normal.batch, product=self.product, submitting_user=self.engineer, original_filename=f'bulk_{i}.png',
                image='x.png', status='queued')
        page2 = self.client_for(self.engineer).get(reverse('inspection-history'), {'page': 2})
        self.assertEqual(page2.status_code, 200)
        self.assertContains(page2, 'Page 2 of 2')
        api = self.client_for(self.engineer).get(reverse('api-inspections'), {'page_size': 5}).json()
        self.assertEqual((len(api['results']), api['count'] > 5, api['next'] is not None), (5, True, True))
