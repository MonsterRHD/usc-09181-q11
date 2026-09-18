"""HTTP API 层测试：健康检查、角色门禁与脱敏视图。"""
import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from service.api import make_handler
from service.core import DueDiligenceService
from service.store import Store
from tests.test_due_diligence import app_payload


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = DueDiligenceService(Store())
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(cls.service))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _req(self, method, path, body=None, role='compliance', actor='tester'):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        headers = {'X-Role': role, 'X-Actor': actor}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers['Content-Type'] = 'application/json'
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, json.loads(raw) if raw else {}

    def test_health(self):
        status, body = self._req('GET', '/health')
        self.assertEqual((status, body.get('status')), (200, 'ok'))

    def test_role_based_views_and_guards(self):
        status, created = self._req('POST', '/applications', app_payload())
        self.assertEqual(status, 201)
        app_id = created['id']

        # business：脱敏视图
        status, view = self._req('GET', f'/applications/{app_id}', role='business')
        self.assertEqual(status, 200)
        self.assertEqual(view['contact']['id_number'], 'CI****01')
        # compliance：全量视图
        status, view = self._req('GET', f'/applications/{app_id}', role='compliance')
        self.assertEqual(view['contact']['id_number'], 'CID-1001')
        # regulator：仅监管摘要
        status, _ = self._req('GET', f'/applications/{app_id}', role='regulator')
        self.assertEqual(status, 403)
        status, summary = self._req('GET', f'/applications/{app_id}/regulatory-summary',
                                    role='regulator')
        self.assertEqual(status, 200)
        self.assertNotIn('REG-001', json.dumps(summary))
        # 审计轨迹仅合规/审计角色可见
        status, _ = self._req('GET', f'/applications/{app_id}/audit', role='business')
        self.assertEqual(status, 403)
        status, audit = self._req('GET', f'/applications/{app_id}/audit', role='auditor')
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(audit['events']), 2)

    def test_document_intake_and_idempotency_over_http(self):
        status, created = self._req('POST', '/applications', app_payload())
        app_id = created['id']
        doc = {'doc_type': 'registry', 'doc_number': 'REG-HTTP-1',
               'content': 'registry content'}
        status, body = self._req('POST', f'/applications/{app_id}/documents', doc)
        self.assertEqual(status, 201)
        self.assertFalse(body['deduplicated'])
        status, body = self._req('POST', f'/applications/{app_id}/documents', doc)
        self.assertEqual(status, 200)
        self.assertTrue(body['deduplicated'])

    def test_review_decision_requires_compliance_role(self):
        status, created = self._req('POST', '/applications', app_payload())
        app_id = created['id']
        for doc_type, number in (('registry', 'R-1'), ('ownership_chart', 'O-1'),
                                 ('id_document', 'I-1')):
            self._req('POST', f'/applications/{app_id}/documents',
                      {'doc_type': doc_type, 'doc_number': number, 'content': number})
        self._req('POST', '/sanctions-list/updates',
                  {'version': 1,
                   'entries': [{'entry_id': 'E-1', 'name': 'Alice Zhang',
                                'action': 'add'}]})
        status, queue = self._req('GET', f'/review-queue?application_id={app_id}')
        self.assertEqual(status, 200)
        task_id = queue['tasks'][0]['id']
        # business 角色无权复核
        status, _ = self._req('POST', f'/review-tasks/{task_id}/decisions',
                              {'decision': 'approve'}, role='business', actor='r1')
        self.assertEqual(status, 403)
        # 双人复核解除
        status, _ = self._req('POST', f'/review-tasks/{task_id}/decisions',
                              {'decision': 'approve'}, actor='r1')
        self.assertEqual(status, 200)
        status, _ = self._req('POST', f'/review-tasks/{task_id}/decisions',
                              {'decision': 'approve'}, actor='r2')
        self.assertEqual(status, 200)
        status, signing = self._req('GET', f'/applications/{app_id}/signing-status')
        self.assertTrue(signing['signing']['eligible'])

    def test_unknown_route_and_missing_application(self):
        status, _ = self._req('GET', '/no-such-route')
        self.assertEqual(status, 404)
        status, _ = self._req('GET', '/applications/app-999')
        self.assertEqual(status, 404)


if __name__ == '__main__':
    unittest.main()
