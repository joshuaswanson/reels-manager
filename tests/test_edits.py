import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from reels import server, store
from reels.instagram import Instagram, InstagramError


class EditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(store, 'DB_PATH', Path(self.tmp.name) / 'library.db')
        patcher.start()
        self.addCleanup(patcher.stop)
        with store.connect() as conn:
            conn.executescript(store.SCHEMA)
        media = [dict.fromkeys(store.MEDIA_COLUMNS) for _ in range(2)]
        for i, item in enumerate(media, 1):
            item.update(pk=str(i), id=f'{i}_99')
        store.replace_library(media, [{'id': '10', 'name': 'A'}, {'id': '20', 'name': 'B'}], {'10': ['1', '2']})
        self.remote = Mock()
        patcher = patch.object(server, 'client', return_value=self.remote)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = TestClient(server.app)

    def post(self, path, **body):
        return self.api.post('/api/' + path, json=body)

    def members(self, cid):
        return next(c['pks'] for c in store.library()['collections'] if c['id'] == cid)

    def test_add_remove_keeps_saved_post(self):
        self.assertEqual(self.post('collections/20/add', pks=['1', '1']).status_code, 200)
        self.remote.edit_collection.assert_called_once_with('20', add=['1_99'])
        self.remote.unsave_media.assert_not_called()
        self.assertEqual(self.members('10'), ['1', '2'])
        self.assertEqual(self.members('20'), ['1'])
        self.assertEqual(self.post('collections/20/remove', pks=['1']).status_code, 200)
        self.remote.unsave_media.assert_called_once_with('1', '20')
        self.assertEqual(self.members('20'), [])
        self.assertIsNotNone(store.media('1'))
        self.assertEqual(self.members('10'), ['1', '2'])

    def test_unsave_removes_all_memberships(self):
        store.add_membership('20', ['1'])
        self.assertEqual(self.post('media/unsave', pks=['1']).status_code, 200)
        self.remote.unsave_media.assert_called_once_with('1')
        self.assertIsNone(store.media('1'))
        self.assertEqual(self.members('10'), ['2'])
        self.assertEqual(self.members('20'), [])

    def test_move_and_same_destination(self):
        self.assertEqual(self.post('collections/10/move', pks=['1'], to_id='10').status_code, 200)
        self.assertEqual(self.remote.mock_calls, [])
        self.assertEqual(self.post('collections/10/move', pks=['1'], to_id='20').status_code, 200)
        self.assertEqual(self.members('10'), ['2'])
        self.assertEqual(self.members('20'), ['1'])
        self.assertEqual([c[0] for c in self.remote.mock_calls], ['edit_collection', 'unsave_media'])

    def test_partial_add_does_not_remove_source(self):
        self.remote.edit_collection.side_effect = [None, InstagramError('failed')]
        self.assertEqual(self.post('collections/10/move', pks=['1', '2'], to_id='20').status_code, 502)
        self.remote.unsave_media.assert_not_called()
        self.assertEqual(self.members('10'), ['1', '2'])
        self.assertEqual(self.members('20'), ['1'])

    def test_rejected_add_leaves_both_collections_unchanged(self):
        self.remote.edit_collection.side_effect = InstagramError('HTTP 404')
        self.assertEqual(self.post('collections/20/add', pks=['1']).status_code, 502)
        self.assertEqual(self.members('10'), ['1', '2'])
        self.assertEqual(self.members('20'), [])
        self.remote.unsave_media.assert_not_called()

    def test_partial_unsave(self):
        self.remote.unsave_media.side_effect = [None, InstagramError('failed')]
        self.assertEqual(self.post('media/unsave', pks=['1', '2']).status_code, 502)
        self.assertIsNone(store.media('1'))
        self.assertIsNotNone(store.media('2'))

    def test_validation_before_remote_changes(self):
        for pks, status in [([], 400), (['1', 'missing'], 404)]:
            self.assertEqual(self.post('media/unsave', pks=pks).status_code, status)
        with patch.dict(server.sync_job.status, running=True):
            self.assertEqual(self.post('media/unsave', pks=['1']).status_code, 409)
        self.assertEqual(self.post('collections/10/move', pks=['1'], to_id='missing').status_code, 404)
        self.assertEqual(self.remote.mock_calls, [])

    def test_create_empty_and_populated_collection(self):
        self.remote.create_collection.return_value = {'id': '30', 'name': 'AI Memes'}
        response = self.post('collections', name='  AI Memes  ', pks=[])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(store.collection('30')['name'], 'AI Memes')
        self.remote.create_collection.assert_called_with('AI Memes', [])
        self.assertEqual(self.members('30'), [])
        response = self.post('collections', name='AI Memes', pks=['1', '1', '2'])
        self.assertEqual(response.status_code, 200)
        self.remote.create_collection.assert_called_with('AI Memes', ['1_99', '2_99'])
        self.assertEqual(self.members('30'), ['1', '2'])
        self.assertEqual(self.members('10'), ['1', '2'])

    def test_create_validates_before_remote_write(self):
        self.assertEqual(self.post('collections', name=' ', pks=[]).status_code, 400)
        self.assertEqual(self.post('collections', name='AI Memes', pks=['missing']).status_code, 404)
        with patch.dict(server.sync_job.status, running=True):
            self.assertEqual(self.post('collections', name='AI Memes', pks=[]).status_code, 409)
        self.remote.create_collection.assert_not_called()

    def test_create_failure_does_not_insert_collection(self):
        self.remote.create_collection.side_effect = InstagramError('Rejected')
        self.assertEqual(self.post('collections', name='AI Memes', pks=['1']).status_code, 502)
        self.assertEqual(len(store.library()['collections']), 2)

    def test_instagram_scoped_and_global_requests(self):
        ig = Instagram({'sessionid': 'test'})
        self.addCleanup(ig.http.close)
        with patch.object(ig, '_web_mutation') as request:
            ig.edit_collection('10', add=['1_99'])
            request.assert_called_with('add', {'collection_id': '10', 'media_id': '1'})
            ig.unsave_media('1', '10')
            request.assert_called_with('remove', {'collection_id': '10', 'media_id': '1'})
            ig.unsave_media('1')
            request.assert_called_with('unsave', {'media_id': '1'})


if __name__ == '__main__':
    unittest.main()
