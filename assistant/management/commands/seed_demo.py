import os

from django.core.management.base import BaseCommand, CommandError

from assistant.models import Role, User

DEMO_USERS = [
    ('admin_demo', Role.ADMINISTRATOR, True),
    ('engineer_demo', Role.ENGINEER, False),
    ('supervisor_demo', Role.SUPERVISOR, False),
]


class Command(BaseCommand):
    help = (
        'Create missing demo users without changing existing accounts. '
        'Use --reset-password explicitly to replace existing demo passwords.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--password', help='Password for newly-created demo users (or all users with --reset-password).')
        parser.add_argument(
            '--reset-password', action='store_true',
            help='Explicitly replace passwords for existing demo users. Requires --password or DEMO_USER_PASSWORD.',
        )
        parser.add_argument(
            '--sync-roles', action='store_true',
            help='Explicitly restore the documented demo roles without removing existing staff or superuser privileges.',
        )

    def handle(self, *args, **options):
        password = options['password'] or os.environ.get('DEMO_USER_PASSWORD')
        missing = [username for username, _, _ in DEMO_USERS if not User.objects.filter(username=username).exists()]

        if (missing or options['reset_password']) and not password:
            raise CommandError(
                'Set DEMO_USER_PASSWORD or pass --password when creating or resetting demo-user credentials. '
                'No accounts were changed.'
            )

        for username, role, is_staff in DEMO_USERS:
            user = User.objects.filter(username=username).first()
            if user is None:
                User.objects.create_user(username=username, password=password, role=role, is_staff=is_staff)
                self.stdout.write(self.style.SUCCESS(f'Created {username} ({role}).'))
                continue

            changed = []
            if options['sync_roles'] and user.role != role:
                user.role = role
                changed.append('role')
            # Syncing the administrator must not strip a pre-existing staff flag from another account.
            if options['sync_roles'] and is_staff and not user.is_staff:
                user.is_staff = True
                changed.append('staff access')
            if options['reset_password']:
                user.set_password(password)
                changed.append('password')
            if changed:
                user.save()
                self.stdout.write(self.style.WARNING(f'Updated {username}: {", ".join(changed)}.'))
            else:
                self.stdout.write(f'Left existing {username} unchanged.')
