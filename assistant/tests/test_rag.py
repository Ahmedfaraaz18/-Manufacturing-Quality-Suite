"""RAG orchestration: refusal, citation validation, persistence, history access, citation metadata."""

from unittest import mock

import groq
import httpx
from django.test import override_settings
from django.urls import reverse

from assistant.models import AnswerSource, QuestionHistory
from assistant.services import llm, rag

from .base import BaseTestCase

OPS_TEXT = 'E17 is generated when pressure stays below 110 bar for more than 5 seconds during forming.'
MNT_TEXT = 'Low hydraulic pressure or E17 Collect these observations in order: reservoir level.'


class RagTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.ops = self.make_manual(self.hp, 'HP-OPS-01', chunks=[(2, OPS_TEXT)])
        self.mnt = self.make_manual(self.hp, 'HP-MNT-01', chunks=[(3, MNT_TEXT)])
        self.make_manual(self.ac, 'AC-OM-01', chunks=[(2, 'E17 high discharge temperature on AC-90')])
        self.client = self.client_for(self.engineer)

    def ask(self, question, machine='M-HP200', llm_payload=None, llm_side_effect=None):
        with mock.patch('assistant.services.llm.generate_answer', return_value=llm_payload,
                        side_effect=llm_side_effect) as fake:
            response = self.client.post(reverse('api-ask'), {'machine': machine, 'question': question}, format='json')
        return response, fake

    def cited(self, evidence, claim='Answer text.'):
        return {
            'answered_status': 'answered',
            'claims': [{'claim': claim, 'evidence': [
                {'source_id': item.source_id, 'quote': item.chunk.chunk_text}
                for item in evidence
            ]}],
        }

    def ask_with(self, question, build_payload):
        return self.ask(question, llm_side_effect=lambda machine, question, evidence: build_payload(evidence))

    def test_insufficient_evidence_refusal_when_nothing_relevant(self):
        response, fake = self.ask('Who won the cricket world cup final?')
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body['status'], 'insufficient_evidence')
        self.assertIn('do not contain enough information', body['answer'])
        self.assertEqual(body['sources'], [])
        fake.assert_not_called()  # no relevant evidence => the LLM is never consulted

    @override_settings(RAG_MIN_SCORE=-1)
    def test_paraphrased_and_weakly_matched_questions_still_reach_the_llm(self):
        for question in ('Why does the press report E17 when hydraulic pressure drops?',
                         'What maintenance should I check?'):
            response, fake = self.ask_with(question, lambda evidence: self.cited(evidence[:1]))
            self.assertEqual(response.json()['status'], 'answered')
            fake.assert_called_once()

    def test_insufficient_status_from_llm_is_honoured_and_has_no_sources(self):
        payload = {'answered_status': 'insufficient_evidence', 'claims': [
            {'claim': 'The pump costs 500 dollars', 'evidence': []}
        ]}
        body = self.ask('E17 pump replacement price', llm_payload=payload)[0].json()
        self.assertEqual(body['status'], 'insufficient_evidence')
        self.assertEqual(body['sources'], [])
        self.assertNotIn('500', body['answer'])

    def test_answer_citing_only_invented_sources_is_downgraded_to_insufficient(self):
        payload = {'answered_status': 'answered', 'claims': [
            {'claim': 'E17', 'evidence': [{'source_id': 'S99', 'quote': OPS_TEXT}]}
        ]}
        body = self.ask('What does E17 mean?', llm_payload=payload)[0].json()
        self.assertEqual(body['status'], 'insufficient_evidence')
        self.assertEqual(body['sources'], [])

    def test_claim_with_unknown_id_is_dropped_but_other_valid_claim_is_kept(self):
        def payload(evidence):
            result = self.cited(evidence[:1])
            result['claims'].append({'claim': 'Invented claim', 'evidence': [
                {'source_id': evidence[0].source_id, 'quote': evidence[0].chunk.chunk_text},
                {'source_id': 'S42', 'quote': 'invented'},
            ]})
            return result
        body = self.ask_with('What does E17 mean?', payload)[0].json()
        self.assertEqual(body['status'], 'answered')
        self.assertEqual(len(body['sources']), 1)
        self.assertNotIn('S42', body['answer'])
        self.assertNotIn('Invented claim', body['answer'])

    @override_settings(RAG_MIN_SCORE=-1)
    def test_citation_metadata_is_correct_and_comes_from_evidence(self):
        question = 'What does E17 mean and what should be checked first?'
        probe, _ = self.ask_with(question, self.cited)
        self.assertEqual(probe.status_code, 201)
        body = probe.json()
        got = {(s['document_id'], s['revision'], s['page_number']) for s in body['sources']}
        self.assertEqual(got, {('HP-OPS-01', '1.0', 2), ('HP-MNT-01', '1.0', 3)})
        for source in body['sources']:
            record = AnswerSource.objects.get(pk=source['id'])
            self.assertEqual(record.manual.document_id, source['document_id'])
            self.assertEqual(record.page_number, source['page_number'])
            self.assertIn(source['passage'], record.chunk.chunk_text)
            self.assertTrue(source['source_url'].startswith(f'/manuals/{record.manual_id}/page/{record.page_number}/'))
            self.assertEqual(self.client.get(source['source_url']).status_code, 200)

    def test_labels_are_rendered_from_verified_metadata(self):
        body = self.ask_with('E17 pressure below 110 bar', lambda evidence: self.cited(evidence[:1]))[0].json()
        self.assertRegex(body['answer'], r'\[HP-(OPS|MNT)-01 rev 1\.0, p\.\d\]')
        self.assertNotIn('[S1]', body['answer'])

    def test_exact_quote_is_used_and_fabricated_quote_is_rejected(self):
        def good(evidence):
            item = next(item for item in evidence if item.chunk.document_id == 'HP-OPS-01')
            return {'answered_status': 'answered', 'claims': [{'claim': 'pressure', 'evidence': [
                {'source_id': item.source_id, 'quote': 'pressure stays below 110 bar'}
            ]}]}
        body = self.ask_with('E17 pressure below 110 bar', good)[0].json()
        self.assertEqual(body['sources'][0]['passage'], 'pressure stays below 110 bar')

        for quote in ('This sentence is not in the manual at all', 'Pressure stays below 110 bar',
                      'pressure  stays below 110 bar', ''):
            def fake(evidence):
                result = good(evidence)
                result['claims'][0]['evidence'][0]['quote'] = quote
                return result
            body = self.ask_with('E17 pressure below 110 bar', fake)[0].json()
            self.assertEqual(body['status'], 'insufficient_evidence', quote)
            self.assertEqual(body['sources'], [])

    def test_model_claim_and_fabricated_metadata_are_never_displayed(self):
        def payload(evidence):
            result = self.cited(evidence[:1], claim='Buy a pump [FAKE-123 rev 9, p.999].')
            result['answer'] = 'See [FAKE-123 rev 9, p.999]'
            result['claims'][0]['evidence'][0].update({
                'document_id': 'FAKE-123', 'revision': '9', 'page_number': 999,
            })
            return result
        body = self.ask_with('What does E17 mean?', payload)[0].json()
        self.assertEqual(body['status'], 'answered')
        self.assertNotIn('FAKE-123', str(body))
        self.assertNotIn('Buy a pump', body['answer'])
        self.assertIn(body['sources'][0]['passage'], body['answer'])

    def test_unsupported_claims_are_rejected(self):
        for claim in (
            {'claim': 'Unsupported', 'evidence': []},
            {'claim': 'Unsupported'},
            {'claim': '', 'evidence': [{'source_id': 'S1', 'quote': OPS_TEXT}]},
            {'claim': 'Unsupported', 'evidence': [{'source_id': 'S1'}]},
            'not a claim object',
        ):
            payload = {'answered_status': 'answered', 'claims': [claim]}
            body = self.ask('What does E17 mean?', llm_payload=payload)[0].json()
            self.assertEqual(body['status'], 'insufficient_evidence', claim)
            self.assertEqual(body['sources'], [])
        body = self.ask('What does E17 mean?', llm_payload={
            'answered_status': 'answered', 'claims': [],
        })[0].json()
        self.assertEqual(body['status'], 'insufficient_evidence')

    def test_valid_quote_does_not_make_model_claim_semantically_correct(self):
        body = self.ask_with('What does E17 mean?', lambda evidence: self.cited(
            evidence[:1], claim='An unrelated invented conclusion.'
        ))[0].json()
        self.assertEqual(body['status'], 'answered')
        self.assertNotIn('unrelated invented conclusion', body['answer'])
        self.assertIn(body['sources'][0]['passage'], body['answer'])

    def test_history_persisted_for_answered_and_insufficient(self):
        body = self.ask_with('What does E17 mean?', lambda evidence: self.cited(evidence[:1]))[0].json()
        self.ask('Who won the cricket world cup final?')
        records = QuestionHistory.objects.order_by('id')
        self.assertEqual([r.status for r in records], ['answered', 'insufficient_evidence'])
        first = records[0]
        self.assertEqual((first.user, first.machine, first.question), (self.engineer, self.hp, 'What does E17 mean?'))
        self.assertTrue(first.answer)
        self.assertIsNotNone(first.created_at)
        self.assertEqual(first.sources.count(), 1)
        history = self.client.get(reverse('api-history-detail', args=[first.pk])).json()
        self.assertEqual(history['answer'], body['answer'])
        self.assertEqual(history['sources'], body['sources'])

    def test_new_answer_does_not_read_pdf_for_source_verification(self):
        with mock.patch.object(AnswerSource, '_snapshot_matches_original_pdf') as fallback:
            body = self.ask_with('What does E17 mean?', lambda evidence: self.cited(evidence[:1]))[0].json()
        self.assertEqual(body['status'], 'answered')
        self.assertTrue(body['sources'])
        fallback.assert_not_called()

    def test_llm_failure_is_handled_and_recorded(self):
        response, _ = self.ask('What does E17 mean?', llm_side_effect=llm.LLMUnavailable('timeout'))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['status'], 'error')
        self.assertEqual(QuestionHistory.objects.get().status, 'error')

    def test_one_embedding_is_computed_per_question(self):
        from assistant.services import embeddings

        with mock.patch.object(embeddings, 'embed_query', wraps=embeddings.embed_query) as embed:
            body = self.ask_with('What does E17 mean?', lambda evidence: self.cited(evidence[:1]))[0].json()
        self.assertEqual(body['status'], 'answered')
        embed.assert_called_once_with('What does E17 mean?')

    def test_groq_timeout_and_api_failure_remain_errors(self):
        request = httpx.Request('POST', 'https://example.invalid')
        response = httpx.Response(503, request=request)
        errors = [
            groq.APITimeoutError(request=request),
            groq.APIStatusError('unavailable', response=response, body=None),
        ]
        for error in errors:
            client = mock.Mock()
            client.chat.completions.create.side_effect = error
            with mock.patch('assistant.services.llm._client', return_value=client):
                with self.assertRaises(llm.LLMUnavailable):
                    llm.generate_answer(self.hp, 'question', [])

    def test_invalid_json_completion_is_reported_as_error_and_saved(self):
        def malformed(machine, question, evidence):
            return llm.parse_json_object('not JSON')
        body = self.ask('What does E17 mean?', llm_side_effect=malformed)[0].json()
        self.assertEqual(body['status'], 'error')
        self.assertEqual(body['sources'], [])
        self.assertEqual(QuestionHistory.objects.get().status, 'error')

    def test_malformed_llm_response_is_handled(self):
        for payload in (None, {}, {'answered_status': 'maybe', 'claims': []},
                        {'answered_status': 'answered', 'claims': 'S1'},
                        {'answered_status': [], 'claims': []}):
            body = self.ask('What does E17 mean?', llm_payload=payload)[0].json()
            self.assertEqual(body['status'], 'error', payload)

    def test_parse_json_object_requires_a_json_object(self):
        self.assertEqual(llm.parse_json_object('{"a": 1}'), {'a': 1})
        for bad in ('', 'no json here', '[1, 2]', '{broken', '```json\n{"a": 1}\n```'):
            with self.assertRaises(llm.LLMBadResponse):
                llm.parse_json_object(bad)

    @override_settings(GROQ_TIMEOUT_SECONDS=5)
    def test_groq_retry_preserves_json_and_uses_remaining_time(self):
        response = httpx.Response(400, request=httpx.Request('POST', 'https://example.invalid'))
        error = groq.BadRequestError('unsupported reasoning effort', response=response, body=None)
        client = mock.Mock()
        client.chat.completions.create.side_effect = [
            error, mock.Mock(choices=[mock.Mock(message=mock.Mock(
                content='{"answered_status":"insufficient_evidence","claims":[]}'
            ))]),
        ]
        with mock.patch('assistant.services.llm._client', return_value=client):
            with mock.patch('assistant.services.llm.time.monotonic', side_effect=[100.0, 100.5, 102.0]):
                payload = llm.generate_answer(self.hp, 'question', [])
        self.assertEqual(payload['answered_status'], 'insufficient_evidence')
        calls = client.chat.completions.create.call_args_list
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(call.kwargs['response_format'], {'type': 'json_object'})
        self.assertAlmostEqual(calls[0].kwargs['timeout'], 4.5)
        self.assertAlmostEqual(calls[1].kwargs['timeout'], 3.0)
        self.assertNotIn('reasoning_effort', calls[1].kwargs)

    @override_settings(GROQ_TIMEOUT_SECONDS=5)
    def test_groq_fallback_is_not_attempted_after_deadline(self):
        response = httpx.Response(400, request=httpx.Request('POST', 'https://example.invalid'))
        client = mock.Mock()
        client.chat.completions.create.side_effect = groq.BadRequestError(
            'unsupported reasoning effort', response=response, body=None
        )
        with mock.patch('assistant.services.llm._client', return_value=client):
            with mock.patch('assistant.services.llm.time.monotonic', side_effect=[100.0, 100.5, 105.0]):
                with self.assertRaises(llm.LLMUnavailable):
                    llm.generate_answer(self.hp, 'question', [])
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_unknown_machine_and_empty_question_are_rejected(self):
        self.assertEqual(self.client.post(reverse('api-ask'), {'machine': 'M-NOPE', 'question': 'x'}, format='json').status_code, 400)
        self.assertEqual(self.client.post(reverse('api-ask'), {'machine': 'M-HP200', 'question': '   '}, format='json').status_code, 400)

    def test_system_prompt_is_strict_and_sent_with_only_the_evidence(self):
        retrieved = []

        def capture(machine, question, evidence):
            retrieved.extend(evidence)
            return self.cited(evidence[:1])

        self.ask('What does E17 mean?', llm_side_effect=capture)
        messages = llm.build_messages(self.hp, 'What does E17 mean?', retrieved)
        self.assertEqual(messages[0]['content'], llm.SYSTEM_PROMPT)
        self.assertIn('Never use external knowledge', messages[0]['content'])
        self.assertIn('ordinary paraphrases and synonyms', messages[0]['content'])
        self.assertIn('do not assume an ambiguous equivalence', messages[0]['content'])
        self.assertNotIn('AC-OM-01', messages[1]['content'])
        self.assertIn('"claims"', messages[1]['content'])


class GroqClientReuseTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        llm._client_for_config.cache_clear()
        self.addCleanup(llm._client_for_config.cache_clear)

    @override_settings(GROQ_API_KEY='synthetic-key', GROQ_TIMEOUT_SECONDS=5, GROQ_MAX_RETRIES=0)
    def test_client_is_reused_and_configuration_changes_are_respected(self):
        with mock.patch('groq.Groq') as constructor:
            first = llm._client()
            self.assertIs(llm._client(), first)
            self.assertEqual(constructor.call_count, 1)
            self.assertEqual(constructor.call_args.kwargs['max_retries'], 0)
            with override_settings(GROQ_TIMEOUT_SECONDS=8):
                llm._client()
            self.assertEqual(constructor.call_count, 2)
            with override_settings(GROQ_API_KEY='other-synthetic-key'):
                llm._client()
            self.assertEqual(constructor.call_count, 3)
            with override_settings(GROQ_MAX_RETRIES=1):
                llm._client()
            self.assertEqual(constructor.call_count, 4)
            self.assertEqual(constructor.call_args.kwargs['max_retries'], 1)

    @override_settings(GROQ_API_KEY='synthetic-key', GROQ_TIMEOUT_SECONDS=5)
    def test_forked_process_gets_a_different_client(self):
        with mock.patch('groq.Groq') as constructor:
            with mock.patch('assistant.services.llm.os.getpid', return_value=1):
                llm._client()
            with mock.patch('assistant.services.llm.os.getpid', return_value=2):
                llm._client()
            self.assertEqual(constructor.call_count, 2)

    @override_settings(GROQ_API_KEY='synthetic-key')
    def test_missing_key_is_not_hidden_by_cached_client(self):
        with mock.patch('groq.Groq'):
            llm._client()
            with override_settings(GROQ_API_KEY=''):
                with self.assertRaises(llm.LLMUnavailable):
                    llm._client()


class HistoryAccessTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.make_manual(self.hp, 'HP-OPS-01', chunks=[(1, 'x')])
        mk = QuestionHistory.objects.create
        mk(user=self.engineer, machine=self.hp, question='mine', answer='a', status='answered')
        mk(user=self.engineer2, machine=self.ac, question='theirs', answer='b', status='insufficient_evidence')

    def results(self, user, **params):
        response = self.client_for(user).get(reverse('api-history'), params)
        self.assertEqual(response.status_code, 200)
        return response.json()['results']

    def test_engineer_sees_only_own_history(self):
        self.assertEqual([r['question'] for r in self.results(self.engineer)], ['mine'])

    def test_engineer_cannot_open_another_users_record(self):
        other = QuestionHistory.objects.get(question='theirs')
        self.assertEqual(self.client_for(self.engineer).get(reverse('api-history-detail', args=[other.pk])).status_code, 404)

    def test_supervisor_sees_everything_and_can_filter(self):
        self.assertEqual(len(self.results(self.supervisor)), 2)
        self.assertEqual([r['question'] for r in self.results(self.supervisor, machine='M-AC90')], ['theirs'])
        self.assertEqual([r['question'] for r in self.results(self.supervisor, status='insufficient_evidence')], ['theirs'])
        self.assertEqual([r['question'] for r in self.results(self.supervisor, unanswered='1')], ['theirs'])

    def test_supervisor_is_read_only_and_admin_has_no_history_access(self):
        self.assertEqual(self.client_for(self.supervisor).post(reverse('api-history'), {}).status_code, 405)
        self.assertEqual(self.client_for(self.admin).get(reverse('api-history')).status_code, 403)

    def test_pages_render_for_the_right_roles(self):
        self.assertEqual(self.client_for(self.supervisor).get(reverse('supervisor-dashboard')).status_code, 200)
        self.assertEqual(self.client_for(self.engineer).get(reverse('supervisor-dashboard')).status_code, 403)
        self.assertEqual(self.client_for(self.engineer).get(reverse('engineer-history')).status_code, 200)
        self.assertEqual(self.client_for(self.engineer).get(reverse('assistant', args=['M-HP200'])).status_code, 200)
        self.assertEqual(self.client_for(self.admin).get(reverse('admin-dashboard')).status_code, 200)
        self.assertEqual(self.client_for(self.admin).get(reverse('machine-registration')).status_code, 200)

    def test_login_redirects_to_role_home(self):
        self.client.logout()
        response = self.client.post(reverse('login'), {'username': 'eng1', 'password': 'pw-Eng-1!'})
        self.assertRedirects(response, reverse('home'), fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse('home')), reverse('engineer-dashboard'), fetch_redirect_response=False)


class LegacyHistorySafetyTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.manual = self.make_manual(self.hp, 'HP-OPS-01', chunks=[(1, OPS_TEXT)])
        self.chunk = self.manual.chunks.get()
        self.record = QuestionHistory.objects.create(
            user=self.engineer, machine=self.hp, question='E17',
            answer='Invented claim [FAKE-123 rev 9, p.999]', status='answered',
        )
        self.source = AnswerSource.objects.create(
            question=self.record, chunk=self.chunk, manual=self.manual,
            document_id=self.chunk.document_id, document_name=self.chunk.document_name,
            revision=self.chunk.revision, page_number=1,
            passage='pressure stays below 110 bar',
        )
        self.client = self.client_for(self.engineer)

    def test_legacy_answer_is_rebuilt_from_exact_source_in_api_and_page(self):
        body = self.client.get(reverse('api-history-detail', args=[self.record.pk])).json()
        self.assertNotIn('FAKE-123', str(body))
        self.assertIn(self.source.passage, body['answer'])
        self.assertIn('[HP-OPS-01 rev 1.0, p.1]', body['answer'])
        page = self.client.get(reverse('engineer-history'))
        self.assertNotContains(page, 'FAKE-123')
        self.assertContains(page, self.source.passage)

    def test_unverifiable_legacy_source_is_hidden_on_all_user_views(self):
        self.source.passage = 'Pressure stays below 110 bar [FAKE-123 rev 9, p.999]'
        self.source.save()
        body = self.client.get(reverse('api-history-detail', args=[self.record.pk])).json()
        self.assertEqual(body['sources'], [])
        self.assertIn('cannot be verified', body['answer'])
        self.assertNotIn('FAKE-123', str(body))
        self.assertNotContains(self.client.get(reverse('engineer-history')), 'FAKE-123')
        page = self.client.get(reverse('manual-page', args=[self.manual.pk, 1]), {'source': self.source.pk})
        self.assertNotContains(page, 'FAKE-123')
        self.assertNotContains(page, 'Cited passage')

    def test_deleted_chunk_does_not_expose_legacy_answer_or_snapshot(self):
        self.chunk.delete()
        body = self.client.get(reverse('api-history-detail', args=[self.record.pk])).json()
        self.assertEqual(body['sources'], [])
        self.assertIn('cannot be verified', body['answer'])
        self.assertNotIn('FAKE-123', str(body))

    def test_altered_source_metadata_is_not_displayed(self):
        self.source.document_id = 'FAKE-123'
        self.source.save()
        body = self.client.get(reverse('api-history-detail', args=[self.record.pk])).json()
        self.assertEqual(body['sources'], [])
        self.assertNotIn('FAKE-123', str(body))

    def test_admin_history_does_not_show_unverified_legacy_text(self):
        self.source.passage = 'Fake passage [FAKE-123 rev 9, p.999]'
        self.source.save()
        self.admin.is_staff = True
        self.admin.is_superuser = True
        self.admin.save()
        client = self.client_for(self.admin)
        response = client.get(reverse('admin:assistant_questionhistory_change', args=[self.record.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'FAKE-123')
        self.assertContains(response, 'Evidence cannot be verified.')
