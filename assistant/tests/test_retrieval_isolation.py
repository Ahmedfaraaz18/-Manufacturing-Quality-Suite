"""TEST 2 (mandatory): retrieval for HP-200 excludes every AC-90 passage, even when AC-90 is more similar."""

from unittest import mock

import numpy as np
from django.test import override_settings
from django.urls import reverse

from assistant.models import QuestionHistory
from assistant.services import retrieval

from .base import BaseTestCase, fake_vector

QUESTION = 'What does error E17 mean on this machine?'
AC_EXACT = 'What does error E17 mean on this machine?'  # identical wording => cosine similarity 1.0
HP_TEXT_1 = 'E17 low hydraulic pressure alarm below 110 bar during forming'
HP_TEXT_2 = 'Inspection schedule reservoir sight glass every shift'


class RetrievalIsolationTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.hp_ops = self.make_manual(self.hp, 'HP-OPS-01', chunks=[(2, HP_TEXT_1)])
        self.hp_mnt = self.make_manual(self.hp, 'HP-MNT-01', chunks=[(2, HP_TEXT_2)])
        self.ac_om = self.make_manual(self.ac, 'AC-OM-01', chunks=[(2, AC_EXACT), (3, 'AC-90 compressor E17 high discharge temperature')])

    def test_ac90_passage_is_more_similar_than_any_hp200_passage(self):
        """Guards the premise of the isolation test: the AC-90 chunk really is the best raw match."""
        q = fake_vector(QUESTION)
        ac = max(float(fake_vector(t) @ q) for t in (AC_EXACT, 'AC-90 compressor E17 high discharge temperature'))
        hp = max(float(fake_vector(t) @ q) for t in (HP_TEXT_1, HP_TEXT_2))
        self.assertGreater(ac, hp)
        self.assertAlmostEqual(ac, 1.0, places=4)

    def test_hp200_retrieval_excludes_all_ac90_passages(self):
        results = retrieval.retrieve(self.hp, QUESTION, min_score=-1, top_k=50)
        self.assertTrue(results)
        self.assertEqual({r.chunk.machine_id for r in results}, {self.hp.pk})
        self.assertEqual({r.chunk.document_id for r in results}, {'HP-OPS-01', 'HP-MNT-01'})
        self.assertFalse(any('AC-90' in r.chunk.chunk_text or r.chunk.chunk_text == AC_EXACT for r in results))

    def test_ac90_retrieval_excludes_all_hp200_passages(self):
        results = retrieval.retrieve(self.ac, QUESTION, min_score=-1, top_k=50)
        self.assertEqual({r.chunk.document_id for r in results}, {'AC-OM-01'})

    def test_both_hp200_manuals_can_contribute(self):
        results = retrieval.retrieve(self.hp, 'E17 inspection schedule', min_score=-1)
        self.assertEqual({r.chunk.document_id for r in results}, {'HP-OPS-01', 'HP-MNT-01'})

    def test_query_vector_matching_only_ac90_still_returns_no_ac90(self):
        exact_ac = fake_vector(AC_EXACT)
        results = retrieval.retrieve(self.hp, QUESTION, min_score=-1, query_vector=exact_ac)
        self.assertTrue(all(r.chunk.machine_id == self.hp.pk for r in results))

    @override_settings(RAG_MIN_SCORE=-1)
    def test_engineer_asking_about_hp200_never_sends_ac90_evidence_to_the_llm(self):
        """End to end via the API: evidence handed to the LLM and returned sources are HP-200 only."""
        seen = {}

        def fake_llm(machine, question, evidence):
            seen['evidence'] = evidence
            return {
                'answered_status': 'answered',
                'claims': [{'claim': 'E17 information', 'evidence': [
                    {'source_id': 'S1', 'quote': evidence[0].chunk.chunk_text}
                ]}],
            }

        with mock.patch('assistant.services.llm.generate_answer', side_effect=fake_llm):
            response = self.client_for(self.engineer).post(
                reverse('api-ask'), {'machine': 'M-HP200', 'question': QUESTION}, format='json'
            )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(seen['evidence'])
        self.assertTrue(all(item.chunk.machine_id == self.hp.pk for item in seen['evidence']))
        self.assertFalse(any(item.chunk.document_id == 'AC-OM-01' for item in seen['evidence']))
        body = response.json()
        self.assertTrue(body['sources'])
        self.assertTrue(all(s['document_id'].startswith('HP-') for s in body['sources']))
        self.assertFalse(QuestionHistory.objects.get().sources.filter(document_id='AC-OM-01').exists())
