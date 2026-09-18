"""API 测试基类：每个用例独立的数据库与服务实例。"""
import os
import tempfile
import threading
import unittest

import requests

from service.main import create_server


class ApiTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.server, self.store = create_server(db_path=self.db_path, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.tmp.cleanup()

    # ---------------------------------------------------------- 请求辅助
    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def req(self, method, path, body=None, actor="alice", role="compliance"):
        return requests.request(
            method, self.url(path), json=body,
            headers={"X-Actor": actor, "X-Role": role}, timeout=10)

    def get(self, path, **kw):
        return self.req("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.req("POST", path, body=body or {}, **kw)

    def patch(self, path, body=None, **kw):
        return self.req("PATCH", path, body=body or {}, **kw)

    def delete(self, path, **kw):
        return self.req("DELETE", path, **kw)

    # ---------------------------------------------------------- 业务辅助
    def create_partner(self, **overrides):
        data = {
            "legal_name": "Ocean Trading Ltd",
            "reg_number": "REG-001",
            "country": "SG",
            "contact_name": "Chen Wei",
            "contact_email": "chen.wei@ocean.example",
            "contact_phone": "+6588881234",
        }
        data.update(overrides)
        r = self.post("/partners", data)
        assert r.status_code == 201, r.text
        return r.json()["partner"]

    def submit_doc(self, pid, doc_type, doc_number, content=None, **kw):
        body = {"doc_type": doc_type, "doc_number": doc_number,
                "content": content if content is not None else f"{doc_type}:{doc_number}"}
        body.update(kw)
        return self.post(f"/partners/{pid}/documents", body)

    def add_ubo(self, pid, name, id_number, **kw):
        body = {"full_name": name, "id_number": id_number}
        body.update(kw)
        r = self.post(f"/partners/{pid}/ubos", body)
        assert r.status_code == 201, r.text
        return r.json()["ubo"]

    def complete_dossier(self, pid, ubo_name="Zhang San", ubo_id="ID-1001"):
        """补齐分阶段收件：阶段1 注册摘录，阶段2 股权结构图 + 受益人身份证件。"""
        self.submit_doc(pid, "REGISTRY_EXTRACT", "RE-1", stage=1)
        self.submit_doc(pid, "OWNERSHIP_CHART", "OC-1", stage=2)
        ubo = self.add_ubo(pid, ubo_name, ubo_id, ownership_pct=60)
        self.submit_doc(pid, "UBO_ID", "UBOID-1", stage=2, holder_name=ubo_name)
        return ubo

    def apply_list(self, name, version, entries, **kw):
        body = {"list_name": name, "version": version,
                "effective_at": "2026-09-01T00:00:00Z", "entries": entries}
        body.update(kw)
        return self.post("/sanction-lists", body)

    def signing_state(self, pid):
        return self.get(f"/partners/{pid}/signing").json()["signing_state"]
