"""制裁名单：命中暂停签约、双人复核解除、名单迟到与更正对历史结论的标注。"""
import unittest

from tests.base import ApiTestBase

SANCTIONED = {"name": "Zhang San", "id_number": "ID-1001", "country": "SG"}


class SanctionsFlowTest(ApiTestBase):
    def _partner_with_hit(self):
        pid = self.create_partner()["partner_id"]
        self.complete_dossier(pid)  # UBO: Zhang San / ID-1001
        self.assertEqual(self.signing_state(pid), "READY")
        r = self.apply_list("OFAC-LIKE", "v1", [SANCTIONED])
        self.assertEqual(r.status_code, 201)
        return pid

    def test_hit_pauses_signing_and_creates_review_task(self):
        pid = self._partner_with_hit()
        self.assertEqual(self.signing_state(pid), "PAUSED")

        dossier = self.get(f"/partners/{pid}").json()
        self.assertEqual(dossier["active_conclusion"]["result"], "HIT")
        self.assertEqual(len(dossier["active_hits"]), 1)
        self.assertEqual(dossier["active_hits"][0]["subject_kind"], "UBO")

        tasks = self.get("/review-tasks?status=PENDING").json()["review_tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["type"], "SANCTIONS_RELEASE")
        self.assertEqual(tasks[0]["required_approvals"], 2)

    def test_dual_review_required_to_release(self):
        pid = self._partner_with_hit()
        task_id = self.get("/review-tasks?status=PENDING").json()["review_tasks"][0]["task_id"]

        # 非合规角色无权复核
        r = self.post(f"/review-tasks/{task_id}/decisions", {"decision": "APPROVE"}, role="rm")
        self.assertEqual(r.status_code, 403)

        # 第一人同意：仍暂停
        r = self.post(f"/review-tasks/{task_id}/decisions", {"decision": "APPROVE"}, actor="carol")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["review_task"]["status"], "PENDING")
        self.assertEqual(self.signing_state(pid), "PAUSED")

        # 同一复核人重复表决 -> 409
        r = self.post(f"/review-tasks/{task_id}/decisions", {"decision": "APPROVE"}, actor="carol")
        self.assertEqual(r.status_code, 409)

        # 第二人（不同复核人）同意：双人复核达成 -> 解除暂停
        r = self.post(f"/review-tasks/{task_id}/decisions", {"decision": "APPROVE"}, actor="dave")
        self.assertEqual(r.json()["review_task"]["status"], "APPROVED")
        self.assertEqual(self.signing_state(pid), "READY")

        dossier = self.get(f"/partners/{pid}").json()
        self.assertEqual(dossier["active_hits"], [])
        self.assertEqual(dossier["active_conclusion"]["result"], "CLEAR")
        # 已放行的命中保留记录（RELEASED），监管摘要可见
        summary = self.get(f"/partners/{pid}/regulatory-summary").json()
        self.assertEqual(summary["hits_by_status"].get("RELEASED"), 1)

        # 任务关闭后不可再表决
        r = self.post(f"/review-tasks/{task_id}/decisions", {"decision": "APPROVE"}, actor="erin")
        self.assertEqual(r.status_code, 409)

    def test_reject_keeps_paused(self):
        pid = self._partner_with_hit()
        task_id = self.get("/review-tasks?status=PENDING").json()["review_tasks"][0]["task_id"]
        r = self.post(f"/review-tasks/{task_id}/decisions",
                      {"decision": "REJECT", "note": "确认命中，拒绝放行"}, actor="carol")
        self.assertEqual(r.json()["review_task"]["status"], "REJECTED")
        self.assertEqual(self.signing_state(pid), "PAUSED")

    def test_released_hit_does_not_reblock_on_recompute(self):
        """复核放行后，后续重算（如补充证件）不得因同一条目再次暂停。"""
        pid = self._partner_with_hit()
        task_id = self.get("/review-tasks?status=PENDING").json()["review_tasks"][0]["task_id"]
        self.post(f"/review-tasks/{task_id}/decisions", {"decision": "APPROVE"}, actor="carol")
        self.post(f"/review-tasks/{task_id}/decisions", {"decision": "APPROVE"}, actor="dave")
        self.assertEqual(self.signing_state(pid), "READY")
        # 补充一份新证件触发重算
        self.submit_doc(pid, "CONTACT_ID", "CID-1", stage=1)
        self.assertEqual(self.signing_state(pid), "READY")


class LateAndCorrectedListTest(ApiTestBase):
    def test_late_list_marks_historical_conclusion(self):
        """名单生效日早于结论作出时间（迟到）：历史 CLEAR 结论被标注 LATE_LIST_HIT。"""
        pid = self.create_partner()["partner_id"]
        self.complete_dossier(pid)
        self.assertEqual(self.signing_state(pid), "READY")
        clear_before = self.get(f"/partners/{pid}").json()["active_conclusion"]["conclusion_id"]

        r = self.apply_list("EU-LIKE", "v1", [SANCTIONED],
                            effective_at="2026-08-01T00:00:00Z")  # 生效日早于现在 -> 迟到
        self.assertEqual(r.status_code, 201)
        impacts = r.json()["impacts"]
        self.assertTrue(any(i["type"] == "LATE_LIST_HIT" and i["conclusion_id"] == clear_before
                            for i in impacts))
        self.assertEqual(self.signing_state(pid), "PAUSED")

        conclusions = self.get(f"/partners/{pid}/conclusions").json()["conclusions"]
        marked = [c for c in conclusions if c["conclusion_id"] == clear_before][0]
        self.assertEqual(marked["impacts"][0]["impact_type"], "LATE_LIST_HIT")

    def test_correction_invalidates_hit_and_marks_history(self):
        """名单更正移除条目：命中失效、签约恢复，历史 HIT 结论标注 CORRECTION_REMOVED_HIT。"""
        pid = self.create_partner()["partner_id"]
        self.complete_dossier(pid)
        self.apply_list("OFAC-LIKE", "v1", [SANCTIONED])
        self.assertEqual(self.signing_state(pid), "PAUSED")
        hit_conclusion = self.get(f"/partners/{pid}").json()["active_conclusion"]["conclusion_id"]

        r = self.apply_list("OFAC-LIKE", "v2", [], kind="CORRECTION", corrects_version="v1")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["removed"], 1)
        self.assertTrue(any(i["type"] == "CORRECTION_REMOVED_HIT"
                            and i["conclusion_id"] == hit_conclusion
                            for i in r.json()["impacts"]))

        self.assertEqual(self.signing_state(pid), "READY")
        summary = self.get(f"/partners/{pid}/regulatory-summary").json()
        self.assertEqual(summary["hits_by_status"].get("INVALIDATED_BY_CORRECTION"), 1)
        # 待复核队列不再挂起该任务对应的开放命中
        self.assertEqual(self.get(f"/partners/{pid}").json()["active_hits"], [])
        # 命中失效后，其在途复核任务被级联取消，队列不留无对象任务
        self.assertEqual(self.get("/review-tasks?status=PENDING").json()["review_tasks"], [])
        cancelled = self.get("/review-tasks?status=CANCELLED").json()["review_tasks"]
        self.assertEqual(len(cancelled), 1)
        actions = [a["action"] for a in self.get(f"/partners/{pid}/audit").json()["audit"]]
        self.assertIn("review.task_cancelled", actions)

    def test_duplicate_list_version_rejected(self):
        self.apply_list("OFAC-LIKE", "v1", [SANCTIONED])
        r = self.apply_list("OFAC-LIKE", "v1", [SANCTIONED])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "LIST_VERSION_EXISTS")


class NotificationDedupTest(ApiTestBase):
    def test_hit_notification_deduplicated_across_recomputes(self):
        pid = self.create_partner()["partner_id"]
        self.complete_dossier(pid)
        self.apply_list("OFAC-LIKE", "v1", [SANCTIONED])
        # 多次触发重算：补证件、改受益人持股比例
        self.submit_doc(pid, "CONTACT_ID", "CID-1", stage=1)
        ubos = self.get(f"/partners/{pid}").json()["ubos"]
        self.patch(f"/partners/{pid}/ubos/{ubos[0]['ubo_id']}", {"ownership_pct": 55})
        self.post(f"/partners/{pid}/verify")

        notes = self.get(f"/notifications?partner_id={pid}").json()["notifications"]
        hit_notes = [n for n in notes if n["type"] == "sanctions_hit"]
        self.assertEqual(len(hit_notes), 1)  # 同一条目命中只通知一次
        task_notes = [n for n in notes if n["type"] == "review_task_created"]
        self.assertEqual(len(task_notes), 1)


if __name__ == "__main__":
    unittest.main()
