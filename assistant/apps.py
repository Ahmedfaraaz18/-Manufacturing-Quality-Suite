import logging
import os
import sys

from django.apps import AppConfig
from django.conf import settings

logger = logging.getLogger(__name__)


def should_warm_embeddings(*, argv=None, environ=None, enabled=None, debug=None):
    """Return whether this process is serving requests and should preload embeddings.

    Django invokes ``AppConfig.ready()`` for management commands as well as web
    workers.  Loading a large model for commands such as ``migrate``, ``test``,
    or ``check`` wastes time and makes those workflows depend on model loading.
    ``runserver`` is the sole management-command exception because it serves HTTP
    requests; its autoreloader parent is skipped so only the child worker loads.
    """
    argv = list(sys.argv if argv is None else argv)
    environ = os.environ if environ is None else environ
    enabled = settings.EMBEDDING_WARMUP_ENABLED if enabled is None else enabled
    debug = settings.DEBUG if debug is None else debug

    if not enabled:
        return False

    launcher = os.path.basename(argv[0]) if argv else ''
    is_django_management = launcher in {'manage.py', 'django-admin', 'django-admin.py'}
    command = argv[1] if len(argv) > 1 else ''
    if not is_django_management:
        return True
    if command != 'runserver':
        return False

    # The development autoreloader starts a parent and a child.  The parent is
    # not a request-serving process, so do not load the model there.
    return not (debug and '--noreload' not in argv and environ.get('RUN_MAIN') != 'true')


class AssistantConfig(AppConfig):
    name = 'assistant'

    def ready(self):
        if not should_warm_embeddings():
            return

        from .services import embeddings

        try:
            embeddings.warm_up()
        except Exception as exc:
            # ``warm_up`` handles expected embedding failures.  Keep startup
            # available even if an unexpected third-party-library error occurs.
            logger.error(
                'Unexpected embedding warm-up failure (%s); requests will retry loading the model.',
                type(exc).__name__,
            )
