"""Authentication regression tests for the login, role-routing, and demo-seeding workflow."""

import os
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from assistant.models import Role, User


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class AuthenticationWorkflowTests(TestCase):
    password = 'A-valid-test-password-42!'

    def setUp(self):
        self.users = {
            'admin_demo': User.objects.create_user(
                'admin_demo', password=self.password, role=Role.ADMINISTRATOR, is_staff=True
            ),
            'engineer_demo': User.objects.create_user(
                'engineer_demo', password=self.password, role=Role.ENGINEER
            ),
            'supervisor_demo': User.objects.create_user(
                'supervisor_demo', password=self.password, role=Role.SUPERVISOR
            ),
        }

    def test_each_demo_role_logs_in_and_reaches_its_own_round_two_workspace(self):
        expected = {
            'admin_demo': 'inspection-admin',
            'engineer_demo': 'inspection-upload',
            'supervisor_demo': 'inspection-supervisor',
        }
        for username, target in expected.items():
            with self.subTest(username=username):
                client = Client()
                response = client.post(reverse('login'), {'username': username, 'password': self.password})
                self.assertRedirects(response, reverse('inspections-home'), fetch_redirect_response=False)
                response = client.get(reverse('inspections-home'))
                self.assertRedirects(response, reverse(target), fetch_redirect_response=False)
                self.assertEqual(client.get(reverse(target)).status_code, 200)
                self.assertEqual(client.get(reverse('home')).status_code, 302)

    def test_unknown_username_incorrect_password_and_inactive_user_are_rejected(self):
        inactive = User.objects.create_user('inactive_demo', password=self.password, role=Role.ENGINEER, is_active=False)
        for username, password in (
            ('missing_demo', self.password),
            ('engineer_demo', 'wrong-password'),
            (inactive.username, self.password),
        ):
            with self.subTest(username=username):
                client = Client()
                response = client.post(reverse('login'), {'username': username, 'password': password})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'Incorrect username or password.')
                self.assertNotIn('_auth_user_id', client.session)

    def test_protected_pages_role_boundaries_logout_and_session_persistence(self):
        anonymous = Client()
        response = anonymous.get(reverse('inspection-upload'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response['Location'])

        engineer = Client()
        engineer.force_login(self.users['engineer_demo'])
        self.assertEqual(engineer.get(reverse('inspection-admin')).status_code, 403)
        self.assertEqual(engineer.get(reverse('inspection-supervisor')).status_code, 403)
        self.assertEqual(engineer.get(reverse('inspection-upload')).status_code, 200)
        self.assertEqual(engineer.get(reverse('engineer-dashboard')).status_code, 200)  # Round 1 compatibility
        self.assertEqual(engineer.post(reverse('logout')).status_code, 302)
        self.assertNotIn('_auth_user_id', engineer.session)
        self.assertEqual(engineer.get(reverse('inspection-upload')).status_code, 302)

    def test_login_post_requires_a_valid_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(reverse('login'), {'username': 'engineer_demo', 'password': self.password})
        self.assertEqual(response.status_code, 403)

        response = client.get(reverse('login'))
        token = response.cookies['csrftoken'].value
        response = client.post(
            reverse('login'), {'username': 'engineer_demo', 'password': self.password, 'csrfmiddlewaretoken': token},
            HTTP_REFERER='http://testserver/login/',
        )
        self.assertRedirects(response, reverse('inspections-home'), fetch_redirect_response=False)
        self.assertIn('_auth_user_id', client.session)


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class DemoSeedCommandTests(TestCase):
    password = 'A-valid-test-password-42!'

    def run_command(self, *args):
        output = StringIO()
        call_command('seed_demo', *args, stdout=output)
        return output.getvalue()

    def test_seed_creates_missing_users_with_an_authorized_password_without_printing_it(self):
        output = self.run_command('--password', self.password)
        self.assertNotIn(self.password, output)
        for username, role, is_staff in (
            ('admin_demo', Role.ADMINISTRATOR, True),
            ('engineer_demo', Role.ENGINEER, False),
            ('supervisor_demo', Role.SUPERVISOR, False),
        ):
            user = User.objects.get(username=username)
            self.assertEqual((user.role, user.is_staff, user.is_active), (role, is_staff, True))
            self.assertTrue(user.check_password(self.password))

    def test_repeat_seed_preserves_existing_password_and_privileges(self):
        user = User.objects.create_user(
            'admin_demo', password='existing-secret', role=Role.ADMINISTRATOR, is_staff=True, is_superuser=True
        )
        self.run_command('--password', self.password)
        user.refresh_from_db()
        self.assertTrue(user.check_password('existing-secret'))
        self.assertTrue(user.is_superuser)

    def test_reset_requires_explicit_password_and_is_opt_in(self):
        user = User.objects.create_user('admin_demo', password='existing-secret', role=Role.ADMINISTRATOR, is_staff=True)
        with patch.dict(os.environ, {'DEMO_USER_PASSWORD': ''}, clear=False):
            with self.assertRaises(CommandError):
                self.run_command('--reset-password')
        user.refresh_from_db()
        self.assertTrue(user.check_password('existing-secret'))
        self.run_command('--reset-password', '--password', self.password)
        user.refresh_from_db()
        self.assertTrue(user.check_password(self.password))
