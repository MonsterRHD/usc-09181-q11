"""并发场景：现场并发修改受益人、名单更正、离线补传下的状态一致性与通知去重。"""
import unittest
from concurrent.futures import ThreadPoolExecutor

from tests.base import ApiTestBase

SANCTIONED = {"name": "Zhang San", "id_number": "ID-1001", "country": "SG"}


class ConcurrencyTest(ApiTestBase):
    def test_concurrent_document_resend_is_idempotent(self):
        """多线程重复发送同一证件：最终只有一条记录。"""
        pid = self.create_partner()["partner_id"]
        body = {"doc_type": "REGISTRY_EXTRACT", "doc_number": "RE-1", "content": "same"}

        def send(_):
            return self.post(f"/partners/{pid}/documents", body).status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(send, range(8)))
        self.assertEqual(sorted(codes), [200] * 7 + [201])
        docs = self.get(f"/partners/{pid}").json()["documents"]
        self.assertEqual(len(docs), 1)

    def test_mixed_concurrent_operations_keep_invariants(self):
        """并发受益人修改 + 名单更正 + 离线补传：状态机与审计保持一致。"""
        pid = self.create_partner()["partner_id"]
        self.complete_dossier(pid)
        self.apply_list("OFAC-LIKE", "v1", [SANCTIONED])
        self.assertEqual(self.signing_state(pid), "PAUSED")
        ubo_id = self.get(f"/partners/{pid}").json()["ubos"][0]["ubo_id"]

        def patch_ubo(i):
            return self.patch(f"/partners/{pid}/ubos/{ubo_id}",
                              {"ownership_pct": 50 + i}, actor=f"op{i}").status_code

        def correct_list(_):
            return self.apply_list("OFAC-LIKE", "v2", [], kind="CORRECTION",
                                   corrects_version="v1").status_code

        def backfill(i):
            return self.submit_doc(pid, "CONTACT_ID", f"CID-{i}",
                                   channel="offline",
                                   occurred_at="2026-08-01T00:00:00Z").status_code

        with ThreadPoolExecutor(max_workers=9) as pool:
            futures = ([pool.submit(patch_ubo, i) for i in range(3)]
                       + [pool.submit(correct_list, i) for i in range(3)]
                       + [pool.submit(backfill, i) for i in range(3)])
            codes = [f.result() for f in futures]

        # 名单版本只能应用一次：其余并发请求得到 409
        self.assertEqual(sorted(codes).count(201), 4)  # 3 份证件 + 1 次名单更正
        self.assertEqual(sorted(codes).count(409), 2)

        dossier = self.get(f"/partners/{pid}").json()
        # 不变量：同一时刻只有一个生效结论；更正后命中失效、签约恢复
        conclusions = self.get(f"/partners/{pid}/conclusions").json()["conclusions"]
        self.assertEqual(len([c for c in conclusions if c["status"] == "ACTIVE"]), 1)
        self.assertEqual(dossier["active_hits"], [])
        self.assertEqual(self.signing_state(pid), "READY")
        self.assertEqual(len(dossier["documents"]), 6)  # 3 原有 + 3 离线补传

        # 通知去重：同一条目命中只通知一次；审计覆盖全部操作
        notes = self.get(f"/notifications?partner_id={pid}").json()["notifications"]
        self.assertEqual(len([n for n in notes if n["type"] == "sanctions_hit"]), 1)
        audit = self.get(f"/partners/{pid}/audit", role="auditor").json()["audit"]
        self.assertEqual(len([a for a in audit if a["action"] == "document.received"]), 6)
        self.assertTrue(any(a["action"] == "hit.invalidated" for a in audit))

    def test_concurrent_dual_review_decisions(self):
        """多线程同时表决：同一复核人只计一次，达成双人复核后解除暂停。"""
        pid = self.create_partner()["partner_id"]
        self.complete_dossier(pid)
        self.apply_list("OFAC-LIKE", "v1", [SANCTIONED])
        task_id = self.get("/review-tasks?status=PENDING").json()["review_tasks"][0]["task_id"]

        def decide(actor):
            return self.post(f"/review-tasks/{task_id}/decisions",
                             {"decision": "APPROVE"}, actor=actor).status_code

        with ThreadPoolExecutor(max_workers=6) as pool:
            codes = list(pool.map(decide, ["carol", "carol", "dave", "dave", "erin", "erin"]))
        self.assertEqual(codes.count(200), 2)   # 只有前两个不同复核人生效
        self.assertEqual(codes.count(409), 4)   # 其余为重复表决或任务已关闭
        self.assertEqual(self.signing_state(pid), "READY")


if __name__ == "__main__":
    unittest.main()
