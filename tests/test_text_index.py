import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reels import store, text_index
from reels.instagram import CdnError


class TextIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for name, value in (('DB_PATH', root / 'library.db'), ('FILES_DIR', root)):
            patcher = patch.object(store, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        with store.connect() as conn:
            conn.executescript(store.SCHEMA)
        media = [dict.fromkeys(store.MEDIA_COLUMNS) for _ in range(3)]
        media[0].update(pk='1', id='1_9', video_url='https://cdn/1.mp4')
        media[1].update(pk='2', id='2_9', video_url='https://cdn/expired.mp4')
        media[2].update(pk='3', id='3_9', slides=json.dumps([
            {'thumb_url': None, 'image_url': 'https://cdn/3a.jpg', 'video_url': None},
            {'thumb_url': None, 'image_url': 'https://cdn/3b.jpg', 'video_url': None},
        ]))
        store.replace_library(media, [], {})
        (root / '1.mp4').write_bytes(b'cached')
        self.downloads = []

        def fetch(url, dest):
            if 'expired' in url:
                raise CdnError('HTTP 403')
            self.downloads.append(url)
            dest.write_bytes(b'downloaded')

        class FakeReader:
            def read(self, path):
                return f'text of {path.name}'

            def close(self):
                pass

        for target, replacement in (('fetch_cdn', fetch), ('Reader', FakeReader)):
            patcher = patch(f'reels.text_index.{target}', replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.job = text_index.TextIndexJob()

    def test_reads_cached_and_downloaded_posts_and_retries_expired_links_later(self):
        self.job._run()
        texts = {m['pk']: m['screenText'] for m in store.library()['media']}
        self.assertEqual(texts, {'1': 'text of 1.mp4', '2': '', '3': 'text of 3.jpg\ntext of 3_1.jpg'})
        self.assertEqual(self.downloads, ['https://cdn/3a.jpg', 'https://cdn/3b.jpg'])
        self.assertEqual([row['pk'] for row in store.media_without_screen_text()], ['2'])
        self.assertEqual(self.job.snapshot(1)['read'], [{'pk': '3', 'text': 'text of 3.jpg\ntext of 3_1.jpg'}])
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).iterdir()), ['1.mp4', 'library.db'])


if __name__ == '__main__':
    unittest.main()
