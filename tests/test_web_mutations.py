import json
import unittest
from urllib.parse import parse_qs
from unittest.mock import patch

import httpx

from reels.instagram import Instagram, InstagramError, SessionExpired


HOME = '''<script nonce="page-nonce">
["DTSGInitialData",[],{"token":"test-dtsg"},1]
["LSD",[],{"token":"test-lsd"},2]
["CurrentUserInitialData",[],{"NON_FACEBOOK_USER_ID":"web-actor"},3]
["SiteData",[],{"client_revision":123,"hsi":"456"},4]
</script>'''


class WebMutationTests(unittest.TestCase):
    def setUp(self):
        patcher = patch('reels.instagram.MIN_REQUEST_INTERVAL_SECONDS', 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ig = Instagram({'sessionid': 'test-session', 'csrftoken': 'test-csrf', 'ds_user_id': 'rest-actor'})
        self.addCleanup(self.ig.http.close)
        self.requests = []
        self.payload = {'data': {'xig_media_collection_add_media': {'collection': {'id': '10'}}}}
        self.home = HOME
        def respond(request):
            self.requests.append(request)
            if request.method == 'GET':
                return httpx.Response(200, text=self.home)
            return httpx.Response(200, text='for (;;);' + json.dumps(self.payload))
        self.ig.http.close()
        self.ig.http = httpx.Client(headers=self.ig.http.headers, transport=httpx.MockTransport(respond))

    def test_add_matches_captured_web_request(self):
        self.ig.edit_collection('10', add=['1_99'])
        request = self.requests[-1]
        self.assertEqual(str(request.url), 'https://www.instagram.com/api/graphql')
        form = parse_qs(request.content.decode())
        self.assertEqual(form['doc_id'], ['27261476466806134'])
        self.assertEqual(form['fb_api_req_friendly_name'], ['PolarisAPIEditCollectionAddMediaMutation'])
        self.assertEqual(form['av'], ['web-actor'])
        self.assertEqual(form['__d'], ['www'])
        self.assertEqual(form['fb_dtsg'], ['test-dtsg'])
        inputs = json.loads(form['variables'][0])['input']
        self.assertEqual(inputs['collection_id'], '10')
        self.assertEqual(inputs['media_id'], '1')
        self.assertEqual(inputs['actor_id'], 'web-actor')
        self.assertTrue(inputs['client_mutation_id'])
        self.assertEqual(request.headers['X-FB-Entry-Nonce'], 'page-nonce')
        self.assertEqual(request.headers['Sec-Fetch-Site'], 'same-origin')
        self.assertEqual(request.headers['X-ASBD-ID'], '359341')
        self.assertNotIn('X-Requested-With', request.headers)
        self.ig.edit_collection('10', add=['2'])
        self.assertEqual(sum(r.method == 'GET' for r in self.requests), 1)

    def test_create_empty_collection_uses_web_mutation(self):
        self.payload = {'data': {'xig_media_collection_create': {'collection': {'id': '30', 'name': 'AI Memes'}}}}
        self.assertEqual(self.ig.create_collection('AI Memes', []), {'id': '30', 'name': 'AI Memes'})
        form = parse_qs(self.requests[-1].content.decode())
        self.assertEqual(form['doc_id'], ['27305499509043563'])
        inputs = json.loads(form['variables'][0])['input']
        self.assertEqual(inputs['name'], 'AI Memes')
        self.assertEqual(inputs['added_media_ids'], [])
        self.assertEqual(inputs['actor_id'], 'web-actor')

    def test_create_with_selection_uses_one_mutation(self):
        self.payload = {'data': {'xig_media_collection_create': {'collection': {'id': '30'}}}}
        self.ig.create_collection('AI Memes', ['1_99', '2_99', '1_99'])
        form = parse_qs(self.requests[-1].content.decode())
        self.assertEqual(json.loads(form['variables'][0])['input']['added_media_ids'], ['1', '2'])
        self.assertEqual(sum(r.method == 'POST' for r in self.requests), 1)

    def test_remove_uses_collection_mutation_not_unsave(self):
        self.payload = {'data': {'xig_media_collection_remove_media': {'collection': {'id': '10'}}}}
        self.ig.unsave_media('1', '10')
        form = parse_qs(self.requests[-1].content.decode())
        self.assertEqual(form['doc_id'], ['27067931769494869'])
        self.assertEqual(json.loads(form['variables'][0])['input']['collection_id'], '10')

    def test_unsave_uses_global_mutation(self):
        self.payload = {'data': {'xig_media_unsave': {'media': {'id': '1'}}}}
        self.ig.unsave_media('1')
        form = parse_qs(self.requests[-1].content.decode())
        self.assertEqual(form['doc_id'], ['27203919972607144'])
        self.assertNotIn('collection_id', json.loads(form['variables'][0])['input'])

    def test_http_200_errors_are_not_success(self):
        for payload in [
            {'error': 1357054, 'errorSummary': 'Request rejected'},
            {'errors': [{'message': 'Permission denied'}]},
            {'data': {'xig_media_collection_add_media': None}},
            {'data': {'xig_media_collection_add_media': {'collection': None}}},
        ]:
            with self.subTest(payload=payload):
                self.payload = payload
                with self.assertRaises(InstagramError):
                    self.ig.edit_collection('10', add=['1'])

    def test_missing_session_does_not_send_mutation(self):
        self.home = '<html>Log in</html>'
        with self.assertRaises(SessionExpired):
            self.ig.edit_collection('10', add=['1'])
        self.assertEqual([r.method for r in self.requests], ['GET'])
