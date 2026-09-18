"""重启持久化：待复核队列、结论、通知、审计在重启后不丢失。"""
import os
import tempfile
import threading
import unittest

import requests

from service.main import create_server
from tests.base import ApiTestBase


class RestartTest(ApiTestBase):
    def _restart(self):
        """关闭当前服务并用同一数据文件重新启动。"""
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self.server, self.store = create_server(db_path=self.db_path, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def test_pending_review_queue_survives_restart(self):
        pid = self.create_partner()["partner_id"]
        self.complete_dossier(pid)
        self.apply_list("OFAC-LIKE", "v1",
                        [{"name": "Zhang San", "id_number": "ID-1001", "country": "SG"}])
        self.assertEqual(self.signing_state(pid), "PAUSED")
        tasks_before = self.get("/review-tasks?status=PENDING").json()["review_tasks"]
        self.assertEqual(len(tasks_before), 1)

        self._restart()

        # 待复核队列不丢失，状态完整恢复
        tasks_after = self.get("/review-tasks?status=PENDING").json()["review_tasks"]
        self.assertEqual([t["task_id"] for t in tasks_after],
                         [t["task_id"] for t in tasks_before])
        self.assertEqual(self.signing_state(pid), "PAUSED")
        dossier = self.get(f"/partners/{pid}").json()
        self.assertEqual(dossier["active_conclusion"]["result"], "HIT")
        self.assertEqual(len(dossier["active_hits"]), 1)

        # 重启后复核流程可继续：双人复核解除暂停
        task_id = tasks_after[0]["task_id"]
        self.post(f"/review-tasks/{task_id}/decisions", {"decision": "APPROVE"}, actor="carol")
        r = self.post(f"/review-tasks/{task_id}/decisions", {"decision": "APPROVE"}, actor="dave")
        self.assertEqual(r.json()["review_task"]["status"], "APPROVED")
        self.assertEqual(self.signing_state(pid), "READY")

    def test_audit_and_notifications_survive_restart(self):
        pid = self.create_partner()["partner_id"]
        self.complete_dossier(pid)
        audit_before = self.get(f"/partners/{pid}/audit").json()["audit"]
        notes_before = self.get(f"/notifications?partner_id={pid}").json()["notifications"]
        self._restart()
        audit_after = self.get(f"/partners/{pid}/audit").json()["audit"]
        notes_after = self.get(f"/notifications?partner_id={pid}").json()["notifications"]
        self.assertEqual([a["seq"] for a in audit_after], [a["seq"] for a in audit_before])
        self.assertEqual([n["dedup_key"] for n in notes_after],
                         [n["dedup_key"] for n in notes_before])


if __name__ == "__main__":
    unittest.main()
