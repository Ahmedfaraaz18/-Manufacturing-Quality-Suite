import os
import secrets

from django.core.management.base import BaseCommand

from assistant.models import Role, User

DEMO_USERS = [
    ('admin_demo', Role.ADMINISTRATOR, True),
    ('engineer_demo', Role.ENGINEER, False),
    ('supervisor_demo', Role.SUPERVISOR, False),
]


class Command(BaseCommand):
    help = 'Create demo users (administrator, engineer, supervisor). Password: --password, DEMO_USER_PASSWORD, or random.'

    def add_arguments(self, parser):
        parser.add_argument('--password', help='Password for all demo users')

    def handle(self, *args, **options):
        password = options['password'] or os.environ.get('DEMO_USER_PASSWORD') or secrets.token_urlsafe(12)
        generated = not (options['password'] or os.environ.get('DEMO_USER_PASSWORD'))
        for username, role, is_staff in DEMO_USERS:
            user, created = User.objects.get_or_create(username=username, defaults={'role': role})
            user.role, user.is_staff = role, is_staff
            user.set_password(password)
            user.save()
            self.stdout.write(f'{"Created" if created else "Updated"} {username} ({role})')
        if generated:
            self.stdout.write(self.style.WARNING(f'Generated password for all demo users: {password}'))
