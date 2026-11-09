import unittest
from unittest.mock import patch

import httpx

from reels.instagram import API_BASE, Instagram


def comment(pk, **extra):
    return {'pk': pk, 'text': f'text {pk}', 'created_at': 1793692682, 'user': {'username': f'user{pk}'}, **extra}


class CommentTests(unittest.TestCase):
    def setUp(self):
        patcher = patch('reels.instagram.MIN_REQUEST_INTERVAL_SECONDS', 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ig = Instagram({'sessionid': 'test-session', 'csrftoken': 'test-csrf'})
        self.requests = []
        self.payload = {}

        def respond(request):
            self.requests.append(request)
            return httpx.Response(200, json={'status': 'ok', **self.payload})

        self.ig.http.close()
        self.ig.http = httpx.Client(base_url=API_BASE, transport=httpx.MockTransport(respond))
        self.addCleanup(self.ig.http.close)

    def test_first_page_and_cursor(self):
        self.payload = {
            'comment_count': 169,
            'comments': [comment(7, comment_like_count=8, child_comment_count=2)],
            'has_more_headload_comments': True,
            'next_min_id': '{"cursor":"x"}',
        }
        page = self.ig.comments('42')
        self.assertEqual(page, {
            'count': 169,
            'comments': [{'pk': '7', 'username': 'user7', 'text': 'text 7', 'createdAt': 1793692682, 'likeCount': 8, 'replyCount': 2}],
            'cursor': '{"cursor":"x"}',
        })
        self.assertEqual(self.requests[0].url.path, '/api/v1/media/42/comments/')

        self.payload = {'comments': [], 'has_more_headload_comments': False, 'next_min_id': 'stale'}
        page = self.ig.comments('42', page['cursor'])
        self.assertIsNone(page['cursor'])
        self.assertEqual(self.requests[1].url.params['min_id'], '{"cursor":"x"}')

    def test_replies(self):
        self.payload = {'child_comments': [comment(8)], 'has_more_tail_child_comments': False}
        page = self.ig.replies('42', '7')
        self.assertEqual([c['pk'] for c in page['comments']], ['8'])
        self.assertIsNone(page['cursor'])
        self.assertEqual(self.requests[0].url.path, '/api/v1/media/42/comments/7/child_comments/')


if __name__ == '__main__':
    unittest.main()
