"""Embedding loading and startup warm-up behaviour."""

from unittest import mock
from concurrent.futures import ThreadPoolExecutor
import sys
from types import ModuleType

import numpy as np
from django.apps import apps
from django.test import SimpleTestCase, override_settings

from assistant import apps as assistant_apps
from assistant.services import embeddings


class EmbeddingLoadingTests(SimpleTestCase):
    def setUp(self):
        self.original_model = embeddings._model
        embeddings._model = None

    def tearDown(self):
        embeddings._model = self.original_model

    def sentence_transformers_module(self, loader):
        """Avoid importing the heavyweight ML package in loader-behaviour tests."""
        module = ModuleType('sentence_transformers')
        module.SentenceTransformer = loader
        return module

    def test_empty_embedding_request_remains_lazy(self):
        loader = mock.Mock()
        with mock.patch.dict(sys.modules, {'sentence_transformers': self.sentence_transformers_module(loader)}):
            result = embeddings.embed_texts([])

        self.assertEqual(result.shape, (0, 0))
        loader.assert_not_called()
        self.assertIsNone(embeddings._model)

    def test_warm_up_loads_once_and_warm_embedding_reuses_model(self):
        model = mock.Mock()
        model.encode.return_value = np.array([[0.25, 0.75]], dtype=np.float32)
        loader = mock.Mock(return_value=model)
        with mock.patch.dict(sys.modules, {'sentence_transformers': self.sentence_transformers_module(loader)}):
            self.assertTrue(embeddings.warm_up())
            self.assertTrue(embeddings.warm_up())
            result = embeddings.embed_query('warm request')

        loader.assert_called_once_with('sentence-transformers/all-MiniLM-L6-v2')
        model.encode.assert_called_once_with(['warm request'], normalize_embeddings=True, show_progress_bar=False)
        np.testing.assert_array_equal(result, np.array([0.25, 0.75], dtype=np.float32))

    def test_concurrent_requests_share_one_model(self):
        model = mock.Mock()
        loader = mock.Mock(return_value=model)
        with mock.patch.dict(sys.modules, {'sentence_transformers': self.sentence_transformers_module(loader)}):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: embeddings._get_model(), range(16)))
        self.assertTrue(all(result is model for result in results))
        loader.assert_called_once()

    def test_warm_up_logs_failure_and_preserves_lazy_retry(self):
        loader = mock.Mock(side_effect=OSError('model unavailable'))
        with mock.patch.dict(sys.modules, {'sentence_transformers': self.sentence_transformers_module(loader)}):
            with self.assertLogs('assistant.services.embeddings', level='ERROR') as logs:
                self.assertFalse(embeddings.warm_up())

        self.assertIsNone(embeddings._model)
        self.assertIn('Embedding model warm-up failed', logs.output[0])
        self.assertNotIn('model unavailable', logs.output[0])


class EmbeddingWarmupStartupTests(SimpleTestCase):
    def test_management_commands_never_warm_the_model(self):
        for command in ('migrate', 'makemigrations', 'test', 'check', 'shell', 'collectstatic'):
            self.assertFalse(
                assistant_apps.should_warm_embeddings(
                    argv=['manage.py', command], environ={}, enabled=True, debug=True
                ),
                command,
            )

    def test_runserver_autoreloader_parent_is_skipped_but_child_warms(self):
        self.assertFalse(
            assistant_apps.should_warm_embeddings(
                argv=['manage.py', 'runserver'], environ={}, enabled=True, debug=True
            )
        )
        self.assertTrue(
            assistant_apps.should_warm_embeddings(
                argv=['manage.py', 'runserver'], environ={'RUN_MAIN': 'true'}, enabled=True, debug=True
            )
        )

    def test_web_worker_warms_unless_disabled(self):
        self.assertTrue(
            assistant_apps.should_warm_embeddings(
                argv=['gunicorn', 'config.wsgi:application'], environ={}, enabled=True, debug=False
            )
        )
        self.assertFalse(
            assistant_apps.should_warm_embeddings(
                argv=['gunicorn', 'config.wsgi:application'], environ={}, enabled=False, debug=False
            )
        )

    @override_settings(EMBEDDING_WARMUP_ENABLED=True)
    def test_ready_invokes_warm_up_only_when_guard_allows_it(self):
        config = apps.get_app_config('assistant')
        with mock.patch('assistant.apps.should_warm_embeddings', return_value=True):
            with mock.patch('assistant.services.embeddings.warm_up') as warm_up:
                config.ready()

        warm_up.assert_called_once_with()

    def test_ready_skips_warm_up_for_management_command_guard(self):
        config = apps.get_app_config('assistant')
        with mock.patch('assistant.apps.should_warm_embeddings', return_value=False):
            with mock.patch('assistant.services.embeddings.warm_up') as warm_up:
                config.ready()

        warm_up.assert_not_called()
