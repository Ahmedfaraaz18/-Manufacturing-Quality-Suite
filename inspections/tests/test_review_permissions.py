"""Supervisor reviews and backend role enforcement for every inspection endpoint."""

from django.urls import reverse

from inspections.models import Inspection, InspectionReview

from .base import InspectionTestCase, upload


def snapshot(inspection):
    inspection.refresh_from_db()
    return (inspection.status, inspection.predicted_class, inspection.confidence, inspection.defect_probability,
            inspection.model_version, inspection.completed_at)


class ReviewTests(InspectionTestCase):
    def setUp(self):
        super().setUp()
        self.inspection = self.make_completed(predicted='Defective')
        self.url = reverse('api-inspection-review', args=[self.inspection.pk])
        self.before = snapshot(self.inspection)

    def review(self, user, payload=None, **kwargs):
        if payload is None:
            payload = {'reviewed_class': 'Normal', 'comment': 'Looks clean'}
        return self.client_for(user).post(self.url, payload, format='json', **kwargs)

    def assert_untouched(self):
        self.assertEqual(snapshot(self.inspection), self.before)
        self.assertEqual(InspectionReview.objects.count(), 0)

    # -- who may review ----------------------------------------------------
    def test_engineer_cannot_review_via_the_api(self):
        response = self.review(self.engineer)
        self.assertEqual(response.status_code, 403)
        self.assert_untouched()

    def test_engineer_cannot_review_their_own_inspection_even_with_a_forged_role(self):
        payload = {'reviewed_class': 'Normal', 'role': 'supervisor', 'reviewer': self.supervisor.pk, 'is_supervisor': True}
        for headers in ({}, {'HTTP_X_ROLE': 'supervisor'}):
            self.assertEqual(self.review(self.engineer, payload, **headers).status_code, 403)
        self.assert_untouched()

    def test_engineer_cannot_open_or_submit_the_review_page(self):
        client = self.client_for(self.engineer)
        page = reverse('inspection-review-form', args=[self.inspection.pk])
        self.assertEqual(client.get(page).status_code, 403)
        self.assertEqual(client.post(page, {'reviewed_class': 'Normal', 'comment': 'x'}).status_code, 403)
        self.assert_untouched()

    def test_administrator_and_anonymous_cannot_review(self):
        self.assertEqual(self.review(self.admin).status_code, 403)
        anonymous = self.client.post(self.url, {'reviewed_class': 'Normal'}, content_type='application/json')
        self.assertIn(anonymous.status_code, (401, 403))
        self.assert_untouched()

    def test_forbidden_attempt_on_a_missing_inspection_reveals_nothing(self):
        url = reverse('api-inspection-review', args=[999999])
        self.assertEqual(self.client_for(self.engineer).post(url, {'reviewed_class': 'Normal'}, format='json').status_code, 403)

    def test_supervisor_can_correct_a_prediction(self):
        response = self.review(self.supervisor)
        self.assertEqual(response.status_code, 201, response.content)
        review = response.json()['review']
        self.assertEqual((review['original_predicted_class'], review['reviewed_class'], review['reviewer'], review['comment'], review['is_correction']),
                         ('Defective', 'Normal', 'sup1', 'Looks clean', True))
        self.assertTrue(response.json()['is_reviewed'])

    def test_supervisor_can_confirm_a_prediction_with_no_comment(self):
        response = self.review(self.supervisor, {'reviewed_class': 'Defective'})
        self.assertEqual(response.status_code, 201)
        stored = InspectionReview.objects.get()
        self.assertEqual((stored.reviewed_class, stored.comment, stored.is_correction), ('Defective', '', False))

    def test_original_prediction_is_unchanged_by_a_correction(self):
        self.review(self.supervisor)
        self.assertEqual(snapshot(self.inspection), self.before)
        self.assertEqual(self.inspection.predicted_class, 'Defective')
        self.assertEqual(InspectionReview.objects.get().reviewed_class, 'Normal')

    def test_reviewer_is_the_session_user_not_a_value_sent_by_the_browser(self):
        self.review(self.supervisor, {'reviewed_class': 'Normal', 'reviewer': self.admin.pk, 'reviewer_id': self.admin.pk})
        self.assertEqual(InspectionReview.objects.get().reviewer, self.supervisor)

    # -- invalid review attempts ---------------------------------------------
    def test_duplicate_review_is_rejected_and_keeps_the_first(self):
        self.assertEqual(self.review(self.supervisor).status_code, 201)
        response = self.review(self.supervisor, {'reviewed_class': 'Defective', 'comment': 'second'})
        self.assertEqual(response.status_code, 409)
        stored = InspectionReview.objects.get()
        self.assertEqual((stored.reviewed_class, stored.comment), ('Normal', 'Looks clean'))

    def test_invalid_class_is_rejected(self):
        for payload in ({'reviewed_class': 'Maybe'}, {'reviewed_class': ''}, {}, {'comment': 'only a comment'}):
            self.assertEqual(self.review(self.supervisor, payload).status_code, 400, payload)
        self.assertEqual(self.review(self.supervisor, {'reviewed_class': 'Normal', 'comment': 'x' * 1001}).status_code, 400)
        self.assert_untouched()

    def test_only_completed_inspections_can_be_reviewed(self):
        for state in ('queued', 'processing', 'failed'):
            other = self.make_completed(name=f'{state}.png')
            fields = {'status': state}
            if state == 'failed':
                fields.update(failure_reason='x', predicted_class=None, confidence=None, defect_probability=None, model_version='')
            else:
                fields.update(predicted_class=None, confidence=None, defect_probability=None, model_version='')
            Inspection.objects.filter(pk=other.pk).update(**fields)
            response = self.client_for(self.supervisor).post(
                reverse('api-inspection-review', args=[other.pk]), {'reviewed_class': 'Normal'}, format='json')
            self.assertEqual(response.status_code, 409, state)
        self.assertEqual(InspectionReview.objects.count(), 0)

    def test_unknown_inspection_returns_404_for_a_supervisor(self):
        response = self.client_for(self.supervisor).post(reverse('api-inspection-review', args=[999999]), {'reviewed_class': 'Normal'}, format='json')
        self.assertEqual(response.status_code, 404)

    def test_review_endpoint_accepts_only_post(self):
        self.assertEqual(self.client_for(self.supervisor).get(self.url).status_code, 405)
        self.assertEqual(self.client_for(self.supervisor).put(self.url, {'reviewed_class': 'Normal'}, format='json').status_code, 405)
        self.assertEqual(self.client_for(self.supervisor).delete(self.url).status_code, 405)

    def test_supervisor_form_review_and_reload_show_both_results(self):
        client = self.client_for(self.supervisor)
        page = reverse('inspection-review-form', args=[self.inspection.pk])
        response = client.post(page, {'reviewed_class': 'Normal', 'comment': 'Surface is clean'})
        self.assertRedirects(response, reverse('inspection-detail', args=[self.inspection.pk]), fetch_redirect_response=False)
        reloaded = client.get(reverse('inspection-detail', args=[self.inspection.pk]))
        html = reloaded.content.decode()
        self.assertIn('Original prediction', html)
        self.assertIn('Reviewed prediction', html)
        self.assertIn('Surface is clean', html)
        self.assertIn('sup1', html)
        self.assertEqual(snapshot(self.inspection), self.before)
        # a second submission through the form is also refused
        client.post(page, {'reviewed_class': 'Defective', 'comment': 'again'})
        self.assertEqual(InspectionReview.objects.count(), 1)

    def test_service_layer_rechecks_the_role(self):
        from django.core.exceptions import PermissionDenied

        from inspections import services

        for user in (self.engineer, self.admin):
            with self.assertRaises(PermissionDenied):
                services.submit_review(self.inspection.pk, user, 'Normal')
        self.assert_untouched()

    def test_inactive_supervisor_cannot_review(self):
        self.supervisor.is_active = False
        self.supervisor.save()
        client = self.client_for(self.supervisor)
        self.assertIn(client.post(self.url, {'reviewed_class': 'Normal'}, format='json').status_code, (401, 403))
        self.assert_untouched()


class VisibilityTests(InspectionTestCase):
    def setUp(self):
        super().setUp()
        self.mine = self.make_completed(self.engineer, name='mine.png')
        self.theirs = self.make_completed(self.engineer2, name='theirs.png')

    def ids(self, viewer, url_name='api-inspections', **params):
        response = self.client_for(viewer).get(reverse(url_name), params)
        self.assertEqual(response.status_code, 200, response.content)
        return {row['id'] for row in response.json()['results']}

    def test_engineer_sees_only_their_own_inspections(self):
        self.assertEqual(self.ids(self.engineer), {self.mine.pk})
        self.assertEqual(self.ids(self.engineer2), {self.theirs.pk})

    def test_engineer_cannot_open_another_engineers_records(self):
        client = self.client_for(self.engineer)
        self.assertEqual(client.get(reverse('api-inspection-detail', args=[self.theirs.pk])).status_code, 404)
        self.assertEqual(client.get(reverse('api-batch-detail', args=[self.theirs.batch_id])).status_code, 404)
        self.assertEqual(client.get(reverse('inspection-detail', args=[self.theirs.pk])).status_code, 404)
        self.assertEqual(client.get(reverse('inspection-image', args=[self.theirs.pk])).status_code, 404)
        self.assertEqual(client.get(reverse('inspection-batch', args=[self.theirs.batch_id])).status_code, 404)

    def test_filters_cannot_widen_an_engineers_scope(self):
        self.assertEqual(self.ids(self.engineer, user='eng2'), set())
        self.assertEqual(self.ids(self.engineer, q='theirs'), set())
        self.assertEqual(self.ids(self.engineer, batch=self.theirs.batch_id), set())

    def test_supervisor_sees_all_inspections(self):
        self.assertEqual(self.ids(self.supervisor), {self.mine.pk, self.theirs.pk})
        self.assertEqual(self.ids(self.supervisor, 'api-supervisor-inspections'), {self.mine.pk, self.theirs.pk})
        self.assertEqual(self.client_for(self.supervisor).get(reverse('api-inspection-detail', args=[self.theirs.pk])).status_code, 200)

    def test_supervisor_endpoint_is_closed_to_other_roles(self):
        for user in (self.engineer, self.admin):
            self.assertEqual(self.client_for(user).get(reverse('api-supervisor-inspections')).status_code, 403)

    def test_administrator_can_view_but_not_change_records(self):
        self.assertEqual(self.ids(self.admin), {self.mine.pk, self.theirs.pk})
        self.assertEqual(self.client_for(self.admin).post(reverse('api-inspections'), {}).status_code, 405)

    def test_anonymous_users_cannot_read_anything(self):
        for name, args in (('api-inspections', []), ('api-inspection-detail', [self.mine.pk]), ('api-batch-detail', [self.mine.batch_id]),
                           ('api-products', [])):
            self.assertIn(self.client.get(reverse(name, args=args)).status_code, (401, 403), name)
        self.assertEqual(self.client.get(reverse('inspection-image', args=[self.mine.pk])).status_code, 302)
        self.assertEqual(self.client.get(reverse('inspection-detail', args=[self.mine.pk])).status_code, 302)

    def test_image_is_served_only_to_authorised_users_with_safe_headers(self):
        response = self.client_for(self.engineer).get(reverse('inspection-image', args=[self.mine.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(self.client_for(self.supervisor).get(reverse('inspection-image', args=[self.theirs.pk])).status_code, 200)


class ProductPermissionTests(InspectionTestCase):
    payload = {'product_id': 'P-NEW01', 'name': 'New product', 'description': 'd'}

    def test_only_administrators_register_products(self):
        self.assertEqual(self.client_for(self.admin).post(reverse('api-products'), self.payload, format='json').status_code, 201)
        for user in (self.engineer, self.supervisor):
            self.assertEqual(self.client_for(user).post(reverse('api-products'), {**self.payload, 'product_id': 'P-NEW02'}, format='json').status_code, 403)
        self.assertEqual(self.client.post(reverse('api-products'), self.payload, content_type='application/json').status_code, 403)

    def test_only_administrators_edit_products(self):
        url = reverse('api-product-detail', args=['P-PLATE01'])
        for user in (self.engineer, self.supervisor):
            self.assertEqual(self.client_for(user).patch(url, {'name': 'Hacked'}, format='json').status_code, 403)
        self.product.refresh_from_db()
        self.assertEqual(self.product.name, 'Demonstration metal plate')
        response = self.client_for(self.admin).patch(url, {'name': 'Renamed', 'is_active': False}, format='json')
        self.assertEqual(response.status_code, 200)
        self.product.refresh_from_db()
        self.assertEqual((self.product.name, self.product.is_active), ('Renamed', False))

    def test_product_id_cannot_be_changed_and_duplicates_are_rejected(self):
        url = reverse('api-product-detail', args=['P-PLATE01'])
        self.assertEqual(self.client_for(self.admin).patch(url, {'product_id': 'P-OTHER'}, format='json').status_code, 400)
        self.assertEqual(self.client_for(self.admin).post(reverse('api-products'), {'product_id': 'P-PLATE01', 'name': 'dup'}, format='json').status_code, 400)

    def test_other_roles_list_only_active_products(self):
        Product = type(self.product)
        Product.objects.create(product_id='P-OLD', name='Old', is_active=False)
        listed = lambda user: {p['product_id'] for p in self.client_for(user).get(reverse('api-products')).json()}
        self.assertEqual(listed(self.engineer), {'P-PLATE01'})
        self.assertEqual(listed(self.admin), {'P-PLATE01', 'P-OLD'})

    def test_registration_page_actions_are_limited_to_administrators(self):
        self.assertEqual(self.client_for(self.admin).get(reverse('inspection-admin')).status_code, 200)
        for user in (self.engineer, self.supervisor):
            self.assertEqual(self.client_for(user).get(reverse('inspection-admin')).status_code, 403)
