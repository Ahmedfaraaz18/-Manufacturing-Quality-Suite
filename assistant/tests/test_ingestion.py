"""PDF ingestion: page numbering, validation, duplicate prevention, chunking."""

from django.urls import reverse

from assistant.models import DocumentChunk, Manual
from assistant.services.chunking import chunk_page

from .base import BaseTestCase, pdf_upload

PAGES = [
    'HP-MNT-01 Page 1\nCoverage marker ALPHA',
    'HP-MNT-01 Page 2\nInspection schedule marker BRAVO',
    'HP-MNT-01 Page 3\nTroubleshooting marker CHARLIE',
]


class IngestionTests(BaseTestCase):
    def upload(self, pages=PAGES, **overrides):
        data = {
            'machine': 'M-HP200', 'document_id': 'HP-MNT-01', 'document_name': 'HP 200 Maintenance Manual',
            'revision': '1.0', 'file': pdf_upload(pages), **overrides,
        }
        return self.client_for(self.admin).post(reverse('api-manuals'), data, format='multipart')

    def test_page_numbers_are_one_based_and_match_pdf_pages(self):
        self.assertEqual(self.upload().status_code, 201)
        manual = Manual.objects.get()
        self.assertEqual(manual.page_count, 3)
        for page, marker in [(1, 'ALPHA'), (2, 'BRAVO'), (3, 'CHARLIE')]:
            chunks = DocumentChunk.objects.filter(original_page_number=page)
            self.assertTrue(chunks.exists())
            self.assertTrue(all(marker in c.chunk_text for c in chunks), f'page {page} chunk has wrong text')
        self.assertEqual(set(DocumentChunk.objects.values_list('original_page_number', flat=True)), {1, 2, 3})

    def test_chunks_carry_machine_and_document_metadata(self):
        self.upload()
        for chunk in DocumentChunk.objects.all():
            self.assertEqual(chunk.machine, self.hp)
            self.assertEqual((chunk.document_id, chunk.revision), ('HP-MNT-01', '1.0'))
            self.assertEqual(chunk.document_name, 'HP 200 Maintenance Manual')
            self.assertEqual(len(chunk.vector), 512)

    def test_blank_pages_are_skipped_not_fatal(self):
        response = self.upload(pages=['HP-MNT-01 content', '', 'HP-MNT-01 more content'])
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(set(DocumentChunk.objects.values_list('original_page_number', flat=True)), {1, 3})
        self.assertEqual(Manual.objects.get().page_count, 3)

    def test_rejects_non_pdf(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        bad = SimpleUploadedFile('notes.pdf', b'this is not a pdf at all', content_type='application/pdf')
        response = self.upload(file=bad)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Manual.objects.count(), 0)

    def test_rejects_wrong_extension(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        txt = SimpleUploadedFile('notes.txt', b'%PDF-1.4 fake', content_type='text/plain')
        self.assertEqual(self.upload(file=txt).status_code, 400)

    def test_rejects_document_id_not_in_pdf(self):
        response = self.upload(document_id='AC-OM-01')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Manual.objects.count(), 0)

    def test_rejects_duplicate_file_and_duplicate_document_revision(self):
        self.assertEqual(self.upload().status_code, 201)
        data = {'machine': 'M-AC90', 'document_id': 'HP-MNT-01', 'document_name': 'copy', 'revision': '2.0'}
        same_file = self.client_for(self.admin)
        manual = Manual.objects.get()
        with manual.file.open('rb') as handle:
            from django.core.files.uploadedfile import SimpleUploadedFile

            dup = SimpleUploadedFile('copy.pdf', handle.read(), content_type='application/pdf')
        response = same_file.post(reverse('api-manuals'), {**data, 'file': dup}, format='multipart')
        self.assertEqual(response.status_code, 400)  # same bytes cannot be attached to another machine
        self.assertEqual(self.upload(pages=PAGES + ['HP-MNT-01 extra']).status_code, 400)  # same id + revision
        self.assertEqual(Manual.objects.count(), 1)

    def test_reindex_replaces_chunks_without_duplicates(self):
        self.upload()
        manual = Manual.objects.get()
        before = DocumentChunk.objects.count()
        response = self.client_for(self.admin).post(reverse('api-manual-reindex', args=[manual.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DocumentChunk.objects.count(), before)


class ChunkingTests(BaseTestCase):
    def test_chunks_keep_page_number_and_overlap(self):
        text = '\n'.join(f'Line number {i} with some words to fill space' for i in range(30))
        chunks = chunk_page(7, text, size=200, overlap=60)
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(c.page_number == 7 for c in chunks))
        self.assertEqual([c.chunk_index for c in chunks], list(range(len(chunks))))
        self.assertTrue(all(len(c.text) <= 200 for c in chunks))
        last_line_of_first = chunks[0].text.splitlines()[-1]
        self.assertIn(last_line_of_first, chunks[1].text)

    def test_empty_page_yields_no_chunks(self):
        self.assertEqual(chunk_page(1, '  \n \n', size=200, overlap=20), [])

    def test_page_number_must_be_one_based(self):
        with self.assertRaises(ValueError):
            chunk_page(0, 'text', size=200, overlap=20)
