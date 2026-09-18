"""主流程：分阶段收件、幂等、主体变更、脱敏、撤回、离线补传、审计。"""
import unittest

from tests.base import ApiTestBase


class IntakeFlowTest(ApiTestBase):
    def test_staged_intake_until_ready(self):
        p = self.create_partner()
        pid = p["partner_id"]
        self.assertEqual(p["signing_state"], "BLOCKED")

        # 阶段1：只有注册摘录，仍缺材料
        r = self.submit_doc(pid, "REGISTRY_EXTRACT", "RE-1", stage=1)
        self.assertEqual(r.status_code, 201)
        self.assertIn("OWNERSHIP_CHART", r.json()["missing"])
        self.assertEqual(self.signing_state(pid), "BLOCKED")

        # 阶段2：股权结构图到位，但还没有受益人
        r = self.submit_doc(pid, "OWNERSHIP_CHART", "OC-1", stage=2)
        self.assertIn("UBO", r.json()["missing"])

        # 受益人登记后还缺其身份证件
        self.add_ubo(pid, "Zhang San", "ID-1001")
        r = self.get(f"/partners/{pid}").json()
        self.assertIn("UBO_ID:Zhang San", r["active_conclusion"]["inputs"]["missing"])

        # 受益人证件补齐 -> CLEAR + READY
        r = self.submit_doc(pid, "UBO_ID", "UBOID-1", stage=2, holder_name="Zhang San")
        self.assertEqual(r.json()["missing"], [])
        self.assertEqual(self.signing_state(pid), "READY")
        concl = self.get(f"/partners/{pid}").json()["active_conclusion"]
        self.assertEqual(concl["result"], "CLEAR")

    def test_document_idempotent_resend(self):
        pid = self.create_partner()["partner_id"]
        r1 = self.submit_doc(pid, "REGISTRY_EXTRACT", "RE-1", content="same-bytes")
        r2 = self.submit_doc(pid, "REGISTRY_EXTRACT", "RE-1", content="same-bytes")
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.json()["deduplicated"])
        self.assertEqual(r1.json()["document"]["doc_id"], r2.json()["document"]["doc_id"])
        docs = self.get(f"/partners/{pid}").json()["documents"]
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["version"], 1)

    def test_document_new_version_triggers_recompute(self):
        pid = self.create_partner()["partner_id"]
        self.submit_doc(pid, "REGISTRY_EXTRACT", "RE-1", content="v1")
        r = self.submit_doc(pid, "REGISTRY_EXTRACT", "RE-1", content="v2-changed")
        self.assertEqual(r.status_code, 201)
        self.assertFalse(r.json()["deduplicated"])
        doc = self.get(f"/partners/{pid}").json()["documents"][0]
        self.assertEqual(doc["version"], 2)
        actions = [a["action"] for a in self.get(f"/partners/{pid}/audit").json()["audit"]]
        self.assertIn("document.versioned", actions)

    def test_document_subject_mismatch_rejected(self):
        pid = self.create_partner(reg_number="REG-001")["partner_id"]
        r = self.submit_doc(pid, "REGISTRY_EXTRACT", "RE-9", subject_reg_number="REG-OTHER")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "SUBJECT_MISMATCH")

    def test_offline_backfill_accepted_and_idempotent(self):
        pid = self.create_partner()["partner_id"]
        body = {"doc_type": "REGISTRY_EXTRACT", "doc_number": "RE-1", "content": "paper-scan",
                "channel": "offline", "occurred_at": "2026-08-15T09:30:00Z"}
        r1 = self.post(f"/partners/{pid}/documents", body)
        self.assertEqual(r1.status_code, 201)
        doc = r1.json()["document"]
        self.assertEqual(doc["channel"], "offline")
        self.assertEqual(doc["occurred_at"], "2026-08-15T09:30:00.000+00:00")
        r2 = self.post(f"/partners/{pid}/documents", body)
        self.assertTrue(r2.json()["deduplicated"])


class SubjectChangeTest(ApiTestBase):
    def test_identity_change_invalidates_old_conclusion(self):
        """换主体后旧结论不得套用：版本+1、旧结论作废、签约回到 BLOCKED。"""
        p = self.create_partner()
        pid = p["partner_id"]
        self.complete_dossier(pid)
        self.assertEqual(self.signing_state(pid), "READY")

        r = self.patch(f"/partners/{pid}/identity",
                       {"reg_number": "REG-002", "reason": "合作方更换签约主体"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["partner"]["subject_revision"], 2)
        self.assertEqual(self.signing_state(pid), "BLOCKED")

        dossier = self.get(f"/partners/{pid}").json()
        # 新主体版本下证件与受益人不带入，需重新收件
        self.assertEqual(dossier["documents"], [])
        self.assertEqual(dossier["ubos"], [])
        self.assertEqual(dossier["active_conclusion"]["result"], "INCOMPLETE")
        self.assertEqual(dossier["active_conclusion"]["subject_revision"], 2)

        conclusions = self.get(f"/partners/{pid}/conclusions").json()["conclusions"]
        old_clear = [c for c in conclusions if c["result"] == "CLEAR"]
        self.assertTrue(old_clear)
        self.assertTrue(all(c["status"] == "SUPERSEDED" for c in old_clear))

    def test_contact_only_change_keeps_conclusion(self):
        """仅换联系人不影响核验结论（但留审计）。"""
        pid = self.create_partner()["partner_id"]
        self.complete_dossier(pid)
        before = self.get(f"/partners/{pid}").json()["active_conclusion"]["conclusion_id"]
        r = self.patch(f"/partners/{pid}/identity", {"contact_name": "Li Na"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["partner"]["subject_revision"], 1)
        after = self.get(f"/partners/{pid}").json()["active_conclusion"]["conclusion_id"]
        self.assertEqual(before, after)
        self.assertEqual(self.signing_state(pid), "READY")
        actions = [a["action"] for a in self.get(f"/partners/{pid}/audit").json()["audit"]]
        self.assertIn("contact.updated", actions)


class MaskingTest(ApiTestBase):
    def test_role_based_masking(self):
        pid = self.create_partner()["partner_id"]
        self.complete_dossier(pid)

        full = self.get(f"/partners/{pid}", role="compliance").json()
        self.assertEqual(full["ubos"][0]["id_number"], "ID-1001")
        self.assertEqual(full["contact_email"], "chen.wei@ocean.example")

        masked = self.get(f"/partners/{pid}", role="rm").json()
        self.assertEqual(masked["ubos"][0]["id_number"], "***1001")
        self.assertEqual(masked["contact_email"], "c***@ocean.example")
        self.assertEqual(masked["contact_phone"], "*******1234")
        # 证件号码同样脱敏（"RE-1" 长度不足 4 位 -> 全掩）
        self.assertEqual(masked["documents"][0]["doc_number"], "****")

        # 未知角色同样脱敏，且不可看审计
        self.assertEqual(self.get(f"/partners/{pid}/audit", role="rm").status_code, 403)
        self.assertEqual(self.get(f"/partners/{pid}/audit", role="auditor").status_code, 200)


class WithdrawTest(ApiTestBase):
    def test_withdraw_stops_queries_but_keeps_summary(self):
        pid = self.create_partner()["partner_id"]
        self.complete_dossier(pid)
        r = self.post(f"/partners/{pid}/withdraw")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["signing_state"], "WITHDRAWN")

        # 停止新收件与新查询
        self.assertEqual(self.submit_doc(pid, "REGISTRY_EXTRACT", "RE-2").status_code, 409)
        self.assertEqual(self.post(f"/partners/{pid}/verify").status_code, 409)
        self.assertEqual(self.add_ubo_expect(pid, "New Ubo", "ID-9").status_code, 409)

        # 监管摘要仍可取，且含主体沿革与证件摘要
        s = self.get(f"/partners/{pid}/regulatory-summary").json()
        self.assertEqual(s["status"], "WITHDRAWN")
        self.assertIsNotNone(s["withdrawn_at"])
        self.assertEqual(len(s["documents"]), 3)
        self.assertEqual(s["latest_conclusion"]["result"], "CLEAR")
        self.assertTrue(s["subject_revisions"])

        # 重复撤回 -> 409
        self.assertEqual(self.post(f"/partners/{pid}/withdraw").status_code, 409)

    def add_ubo_expect(self, pid, name, idn):
        return self.post(f"/partners/{pid}/ubos", {"full_name": name, "id_number": idn})


class AuditTest(ApiTestBase):
    def test_audit_trail_covers_all_mutations(self):
        pid = self.create_partner()["partner_id"]
        self.complete_dossier(pid)
        self.patch(f"/partners/{pid}/identity", {"contact_name": "Li Na"})
        audit = self.get(f"/partners/{pid}/audit", role="auditor").json()["audit"]
        actions = [a["action"] for a in audit]
        for expected in ("partner.created", "document.received", "ubo.added",
                         "conclusion.computed", "signing.changed", "contact.updated"):
            self.assertIn(expected, actions)
        # 审计按序、带操作人与时间
        self.assertTrue(all(a["actor"] and a["ts"] for a in audit))
        seqs = [a["seq"] for a in audit]
        self.assertEqual(seqs, sorted(seqs))


if __name__ == "__main__":
    unittest.main()
