"""海外合作方尽调档案的领域行为测试。

覆盖：分阶段收件与幂等、主体变更后旧结论失效、制裁命中暂停与双人复核、
名单迟到/更正回标历史、撤回/重启语义、通知去重、角色脱敏、审计完整性、
并发现场一致性以及持久化重启不丢队列。
"""
import os
import tempfile
import threading
import unittest

from service import masking
from service.core import Conflict, DueDiligenceService, NotFound, Validation
from service.store import Store

COMPLIANCE = 'compliance'


def make_service():
    return DueDiligenceService(Store())


def app_payload(**overrides):
    payload = {
        'legal_name': 'Atlantic Trading Ltd',
        'registration_no': 'REG-001',
        'country': 'PA',
        'contact': {'name': 'Alice Zhang', 'id_number': 'CID-1001',
                    'email': 'alice@example.com', 'phone': '12345678'},
    }
    payload.update(overrides)
    return payload


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()

    def _create(self, **overrides):
        return self.svc.create_application('clerk-1', COMPLIANCE, app_payload(**overrides))

    def _add_bo(self, app_id, name='Bob Li', pct=60.0, id_number='BO-9001'):
        return self.svc.add_beneficial_owner(
            app_id, 'clerk-1', COMPLIANCE,
            {'name': name, 'ownership_pct': pct, 'id_number': id_number,
             'country': 'PA'})

    def _doc(self, app_id, doc_type, number, content, channel='online'):
        return self.svc.receive_document(
            app_id, 'clerk-1', COMPLIANCE,
            {'doc_type': doc_type, 'doc_number': number,
             'content': content, 'channel': channel})

    def _complete_docs(self, app_id):
        self._doc(app_id, 'registry', 'REG-DOC-1', 'registry content')
        self._doc(app_id, 'ownership_chart', 'OWN-1', 'chart content')
        self._doc(app_id, 'id_document', 'ID-ALICE-1', 'alice id content')

    def _current(self, app_id):
        return [c for c in self.svc.list_conclusions(app_id)
                if c['status'] == 'current'][0]


class IntakeTest(ServiceTestCase):
    def test_staged_intake_until_complete(self):
        app = self._create()
        self.assertEqual(app['status'], 'intake')
        self._doc(app['id'], 'registry', 'REG-DOC-1', 'registry content')
        conclusion = self.svc.verify(app['id'], 'clerk-1', COMPLIANCE)
        self.assertEqual(conclusion['result'], 'insufficient')
        self.assertEqual(conclusion['missing_documents'],
                         ['id_document', 'ownership_chart'])
        self._doc(app['id'], 'ownership_chart', 'OWN-1', 'chart content')
        self._doc(app['id'], 'id_document', 'ID-ALICE-1', 'alice id content')
        conclusion = self.svc.verify(app['id'], 'clerk-1', COMPLIANCE)
        self.assertEqual(conclusion['result'], 'clear')
        status = self.svc.signing_status(app['id'])
        self.assertEqual(status['status'], 'eligible')
        self.assertTrue(status['signing']['eligible'])

    def test_same_document_resent_is_idempotent(self):
        app = self._create()
        doc1, dedup1 = self._doc(app['id'], 'registry', 'REG-1', 'same content')
        self.assertFalse(dedup1)
        before = len(self.svc.list_conclusions(app['id']))
        doc2, dedup2 = self._doc(app['id'], 'registry', 'REG-1', 'same content')
        self.assertTrue(dedup2)
        self.assertEqual(doc1['id'], doc2['id'])
        self.assertEqual(doc2['version'], 1)
        # 幂等重发不产生新结论
        self.assertEqual(len(self.svc.list_conclusions(app['id'])), before)
        actions = [e['action'] for e in self.svc.audit_trail(app['id'])]
        self.assertIn('document_deduplicated', actions)

    def test_document_content_change_supersedes_and_recomputes(self):
        app = self._create()
        doc1, _ = self._doc(app['id'], 'registry', 'REG-1', 'v1')
        before = len(self.svc.list_conclusions(app['id']))
        doc2, dedup = self._doc(app['id'], 'registry', 'REG-1', 'v2')
        self.assertFalse(dedup)
        self.assertEqual(doc2['version'], 2)
        docs = {d['id']: d for d in self.svc.list_documents(app['id'])}
        self.assertEqual(docs[doc1['id']]['status'], 'superseded')
        self.assertEqual(docs[doc2['id']]['status'], 'active')
        self.assertGreater(len(self.svc.list_conclusions(app['id'])), before)

    def test_offline_backfill_channel_accepted(self):
        app = self._create()
        doc, _ = self._doc(app['id'], 'license', 'LIC-1', 'offline content',
                           channel='offline_backfill')
        self.assertEqual(doc['channel'], 'offline_backfill')


class SubjectChangeTest(ServiceTestCase):
    def test_contact_change_retires_old_conclusion_and_contact_docs(self):
        app = self._create()
        self._complete_docs(app['id'])
        con1 = self.svc.verify(app['id'], 'clerk-1', COMPLIANCE)
        self.assertEqual(con1['result'], 'clear')
        self.assertEqual(self.svc.get_application(app['id'])['status'], 'eligible')

        # 先换联系人：旧结论立即失效，联系人证件随旧主体作废
        self.svc.update_contact(app['id'], 'ops', COMPLIANCE,
                                {'name': 'Carol Wang', 'id_number': 'CID-2002'})
        app_after = self.svc.get_application(app['id'])
        self.assertEqual(app_after['status'], 'intake')
        current = self._current(app['id'])
        self.assertNotEqual(current['id'], con1['id'])
        self.assertEqual(current['result'], 'insufficient')
        self.assertGreater(current['snapshot_seq'], con1['snapshot_seq'])
        history = {c['id']: c for c in self.svc.list_conclusions(app['id'])}
        self.assertEqual(history[con1['id']]['status'], 'superseded')
        docs = {d['doc_number']: d for d in self.svc.list_documents(app['id'])
                if d['doc_type'] == 'id_document'}
        self.assertEqual(docs['ID-ALICE-1']['status'], 'superseded')

        # 后补证件：新联系人的证件离线补传后重新核验通过
        self._doc(app['id'], 'id_document', 'ID-CAROL-1', 'carol id',
                  channel='offline_backfill')
        con2 = self.svc.verify(app['id'], 'clerk-1', COMPLIANCE)
        self.assertEqual(con2['result'], 'clear')
        self.assertEqual(self.svc.get_application(app['id'])['status'], 'eligible')

    def test_new_subject_screened_immediately_even_before_docs(self):
        app = self._create()
        self._complete_docs(app['id'])
        self.svc.apply_list_update('lists', COMPLIANCE, {
            'version': 1,
            'entries': [{'entry_id': 'E-1', 'name': 'Carol Wang',
                         'action': 'add'}]})
        self.assertEqual(self.svc.get_application(app['id'])['status'], 'eligible')
        # 新联系人在名单上：证件未补齐也立即命中暂停，不套用旧的 clear 结论
        self.svc.update_contact(app['id'], 'ops', COMPLIANCE,
                                {'name': 'Carol Wang', 'id_number': 'CID-2002'})
        self.assertEqual(self.svc.get_application(app['id'])['status'], 'paused')
        current = self._current(app['id'])
        self.assertEqual(current['result'], 'hit')
        self.assertEqual(current['hits'][0]['matched_kind'], 'contact')

    def test_registration_change_supersedes_partner_bound_docs(self):
        app = self._create()
        self._complete_docs(app['id'])
        self.svc.update_identity(app['id'], 'ops', COMPLIANCE,
                                 {'registration_no': 'REG-999'})
        docs = self.svc.list_documents(app['id'])
        partner_docs = [d for d in docs if d['bound_to'] == 'partner']
        self.assertTrue(partner_docs)
        self.assertTrue(all(d['status'] == 'superseded' for d in partner_docs))
        self.assertEqual(self._current(app['id'])['result'], 'insufficient')


class SanctionsFlowTest(ServiceTestCase):
    def _paused_app(self):
        app = self._create()
        self._complete_docs(app['id'])
        self.svc.apply_list_update('lists', COMPLIANCE, {
            'version': 1,
            'entries': [{'entry_id': 'E-1', 'name': 'Alice Zhang',
                         'action': 'add', 'program': 'SDN'}]})
        self.assertEqual(self.svc.get_application(app['id'])['status'], 'paused')
        return app

    def test_hit_pauses_and_dual_review_releases(self):
        app = self._paused_app()
        queue = self.svc.review_queue()
        self.assertEqual(len(queue), 1)
        task = queue[0]
        self.assertEqual(task['required_approvals'], 2)
        # 单人复核不能解除
        self.svc.record_decision(task['id'], 'reviewer-a', COMPLIANCE, 'approve')
        self.assertEqual(self.svc.get_application(app['id'])['status'], 'paused')
        # 同一复核人重复决定无效
        with self.assertRaises(Conflict):
            self.svc.record_decision(task['id'], 'reviewer-a', COMPLIANCE, 'approve')
        # 第二名不同复核人通过后解除
        self.svc.record_decision(task['id'], 'reviewer-b', COMPLIANCE, 'approve')
        self.assertEqual(self.svc.get_application(app['id'])['status'], 'eligible')
        resolved = self.svc.review_queue(status='approved')[0]
        self.assertEqual(len({d['reviewer'] for d in resolved['decisions']}), 2)
        actions = [e['action'] for e in self.svc.audit_trail(app['id'])]
        self.assertIn('signing_released', actions)

    def test_decision_on_resolved_task_rejected(self):
        app = self._paused_app()
        task = self.svc.review_queue()[0]
        self.svc.record_decision(task['id'], 'ra', COMPLIANCE, 'approve')
        self.svc.record_decision(task['id'], 'rb', COMPLIANCE, 'approve')
        with self.assertRaises(Conflict):
            self.svc.record_decision(task['id'], 'rc', COMPLIANCE, 'approve')

    def test_reject_keeps_paused_and_can_be_rerequested(self):
        app = self._paused_app()
        task = self.svc.review_queue()[0]
        self.svc.record_decision(task['id'], 'ra', COMPLIANCE, 'reject',
                                 'name match too strong')
        self.assertEqual(self.svc.get_application(app['id'])['status'], 'paused')
        self.assertEqual(self.svc.review_queue(), [])
        new_task = self.svc.request_review(app['id'], 'ops', COMPLIANCE)
        self.assertNotEqual(new_task['id'], task['id'])
        self.svc.record_decision(new_task['id'], 'ra', COMPLIANCE, 'approve')
        self.svc.record_decision(new_task['id'], 'rb', COMPLIANCE, 'approve')
        self.assertEqual(self.svc.get_application(app['id'])['status'], 'eligible')

    def test_released_hit_signature_not_paused_again(self):
        app = self._paused_app()
        task = self.svc.review_queue()[0]
        self.svc.record_decision(task['id'], 'ra', COMPLIANCE, 'approve')
        self.svc.record_decision(task['id'], 'rb', COMPLIANCE, 'approve')
        self.assertEqual(self.svc.get_application(app['id'])['status'], 'eligible')
        # 名单更正移除后又恢复同一条目：命中签名相同，已放行不再暂停
        self.svc.apply_list_update('lists', COMPLIANCE, {
            'version': 2, 'kind': 'correction',
            'entries': [{'entry_id': 'E-1', 'action': 'remove'}]})
        self.svc.apply_list_update('lists', COMPLIANCE, {
            'version': 3,
            'entries': [{'entry_id': 'E-1', 'name': 'Alice Zhang',
                         'action': 'add'}]})
        self.assertEqual(self.svc.get_application(app['id'])['status'], 'eligible')
        self.assertEqual(self.svc.review_queue(), [])
        actions = [e['action'] for e in self.svc.audit_trail(app['id'])]
        self.assertIn('hit_already_released', actions)

    def test_sign_only_when_eligible(self):
        app = self._create()
        with self.assertRaises(Conflict):
            self.svc.sign(app['id'], 'ops', COMPLIANCE)
        self._complete_docs(app['id'])
        self.svc.sign(app['id'], 'ops', COMPLIANCE)
        self.assertEqual(self.svc.get_application(app['id'])['status'], 'signed')
        with self.assertRaises(Conflict):
            self.svc.sign(app['id'], 'ops', COMPLIANCE)


class ListUpdateTest(ServiceTestCase):
    def test_late_list_update_marks_historical_conclusions(self):
        app = self._create()
        self._complete_docs(app['id'])
        con1 = self.svc.verify(app['id'], 'clerk-1', COMPLIANCE)
        self.assertEqual(con1['result'], 'clear')
        # 名单迟到：准入结论作出后才到达的条目命中合作方
        self.svc.apply_list_update('lists', COMPLIANCE, {
            'version': 1,
            'entries': [{'entry_id': 'E-9', 'name': 'Atlantic Trading Ltd',
                         'action': 'add', 'effective_from': '2026-08-01'}]})
        history = {c['id']: c for c in self.svc.list_conclusions(app['id'])}
        old = history[con1['id']]
        self.assertEqual(old['status'], 'superseded')
        self.assertEqual(len(old['affected_by']), 1)
        self.assertEqual(old['affected_by'][0]['list_version'], 1)
        self.assertEqual(old['affected_by'][0]['reevaluated_result'], 'hit')
        self.assertEqual(self.svc.get_application(app['id'])['status'], 'paused')
        types = [n['type'] for n in self.svc.list_notifications(app['id'])]
        self.assertIn('conclusion_affected', types)

    def test_list_correction_marks_hit_conclusion_and_restores(self):
        app = self._create()
        self._complete_docs(app['id'])
        self.svc.apply_list_update('lists', COMPLIANCE, {
            'version': 1,
            'entries': [{'entry_id': 'E-1', 'name': 'Alice Zhang',
                         'action': 'add'}]})
        self.assertEqual(self.svc.get_application(app['id'])['status'], 'paused')
        hit_con = self._current(app['id'])
        # 名单更正：误列条目移除，历史命中结论被回标，当前状态自动恢复
        self.svc.apply_list_update('lists', COMPLIANCE, {
            'version': 2, 'kind': 'correction',
            'entries': [{'entry_id': 'E-1', 'action': 'remove'}]})
        history = {c['id']: c for c in self.svc.list_conclusions(app['id'])}
        affected = history[hit_con['id']]['affected_by']
        self.assertEqual(len(affected), 1)
        self.assertEqual(affected[0]['kind'], 'correction')
        self.assertEqual(affected[0]['reevaluated_result'], 'clear')
        self.assertEqual(self.svc.get_application(app['id'])['status'], 'eligible')

    def test_list_version_must_increase(self):
        self.svc.apply_list_update('lists', COMPLIANCE, {
            'version': 2,
            'entries': [{'entry_id': 'E-1', 'name': 'X', 'action': 'add'}]})
        with self.assertRaises(Conflict):
            self.svc.apply_list_update('lists', COMPLIANCE, {
                'version': 2,
                'entries': [{'entry_id': 'E-2', 'name': 'Y', 'action': 'add'}]})

    def test_remove_unknown_entry_rejected(self):
        with self.assertRaises(Validation):
            self.svc.apply_list_update('lists', COMPLIANCE, {
                'version': 1,
                'entries': [{'entry_id': 'E-X', 'action': 'remove'}]})


class WithdrawRestartTest(ServiceTestCase):
    def _paused_app(self):
        app = self._create()
        self._complete_docs(app['id'])
        self.svc.apply_list_update('lists', COMPLIANCE, {
            'version': 1,
            'entries': [{'entry_id': 'E-1', 'name': 'Alice Zhang',
                         'action': 'add'}]})
        return app

    def test_withdraw_stops_queries_but_keeps_summary_and_queue(self):
        app = self._paused_app()
        task_id = self.svc.review_queue()[0]['id']
        self.svc.withdraw(app['id'], 'ops', COMPLIANCE, 'partner walked away')
        self.assertEqual(self.svc.get_application(app['id'])['status'], 'withdrawn')
        # 停止新查询与新收件
        with self.assertRaises(Conflict):
            self.svc.verify(app['id'], 'clerk-1', COMPLIANCE)
        with self.assertRaises(Conflict):
            self._doc(app['id'], 'license', 'LIC-9', 'x')
        with self.assertRaises(Conflict):
            self.svc.update_contact(app['id'], 'ops', COMPLIANCE, {'name': 'Zed'})
        # 撤回期间复核决定暂停受理，但任务不丢
        with self.assertRaises(Conflict):
            self.svc.record_decision(task_id, 'ra', COMPLIANCE, 'approve')
        # 监管摘要保留
        summary = self.svc.regulatory_summary(app['id'])
        self.assertTrue(summary['withdrawn'])
        self.assertEqual(summary['status'], 'withdrawn')
        self.assertGreaterEqual(len(summary['conclusion_history']), 2)
        self.assertEqual(len(summary['review_tasks']), 1)
        # 待复核队列未丢失
        self.assertEqual([t['id'] for t in self.svc.review_queue()], [task_id])
        # 重启：队列原样保留，按当前名单重算后回到暂停
        self.svc.restart(app['id'], 'ops', COMPLIANCE)
        self.assertEqual(self.svc.get_application(app['id'])['status'], 'paused')
        self.assertEqual([t['id'] for t in self.svc.review_queue()], [task_id])

    def test_restart_recomputes_under_current_list(self):
        app = self._create()
        self._complete_docs(app['id'])
        self.svc.withdraw(app['id'], 'ops', COMPLIANCE, None)
        self.svc.restart(app['id'], 'ops', COMPLIANCE)
        self.assertEqual(self.svc.get_application(app['id'])['status'], 'eligible')

    def test_withdraw_twice_and_restart_guards(self):
        app = self._create()
        with self.assertRaises(Conflict):
            self.svc.restart(app['id'], 'ops', COMPLIANCE)
        self.svc.withdraw(app['id'], 'ops', COMPLIANCE, None)
        with self.assertRaises(Conflict):
            self.svc.withdraw(app['id'], 'ops', COMPLIANCE, None)


class NotificationTest(ServiceTestCase):
    def test_notifications_are_deduplicated(self):
        app = self._create()
        self._complete_docs(app['id'])
        self.svc.apply_list_update('lists', COMPLIANCE, {
            'version': 1,
            'entries': [{'entry_id': 'E-1', 'name': 'Alice Zhang',
                         'action': 'add'}]})
        self.assertEqual(self.svc.get_application(app['id'])['status'], 'paused')
        # 更正解除暂停（未经双人复核）
        self.svc.apply_list_update('lists', COMPLIANCE, {
            'version': 2, 'kind': 'correction',
            'entries': [{'entry_id': 'E-1', 'action': 'remove'}]})
        self.assertEqual(self.svc.get_application(app['id'])['status'], 'eligible')
        # 同一条目再次到达：同一命中签名再次暂停，但通知不重复
        self.svc.apply_list_update('lists', COMPLIANCE, {
            'version': 3,
            'entries': [{'entry_id': 'E-1', 'name': 'Alice Zhang',
                         'action': 'add'}]})
        self.assertEqual(self.svc.get_application(app['id'])['status'], 'paused')
        paused = [n for n in self.svc.list_notifications(app['id'])
                  if n['type'] == 'signing_paused']
        self.assertEqual(len(paused), 1)
        self.assertEqual(paused[0]['suppressed'], 1)
        # 待复核任务同样不重复
        self.assertEqual(len(self.svc.review_queue()), 1)
        keys = [n['dedup_key'] for n in self.svc.list_notifications(app['id'])]
        self.assertEqual(len(keys), len(set(keys)))


class MaskingTest(ServiceTestCase):
    def test_business_role_sees_masked_pii(self):
        app = self._create()
        self._add_bo(app['id'])
        stored = self.svc.get_application(app['id'])
        view = masking.application_view(stored, 'business')
        self.assertEqual(view['contact']['id_number'], 'CI****01')
        self.assertEqual(view['contact']['email'], 'a***@example.com')
        self.assertEqual(view['contact']['phone'], '****78')
        self.assertEqual(view['registration_no'], 'RE****01')
        bo = next(iter(view['beneficial_owners'].values()))
        self.assertEqual(bo['id_number'], 'BO****01')
        # 掩码不污染存储
        self.assertEqual(stored['contact']['id_number'], 'CID-1001')
        full = masking.application_view(stored, 'compliance')
        self.assertEqual(full['contact']['id_number'], 'CID-1001')
        full = masking.application_view(stored, 'auditor')
        self.assertEqual(full['contact']['id_number'], 'CID-1001')

    def test_regulatory_summary_has_no_raw_identifiers(self):
        app = self._create()
        self._add_bo(app['id'])
        self._complete_docs(app['id'])
        summary = self.svc.regulatory_summary(app['id'])
        import json
        blob = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn('REG-001', blob)
        self.assertNotIn('CID-1001', blob)
        self.assertNotIn('BO-9001', blob)
        self.assertEqual(summary['beneficial_owner_count'], 1)


class AuditTest(ServiceTestCase):
    def test_audit_trail_covers_all_mutations(self):
        app = self._create()
        self._add_bo(app['id'])
        self._complete_docs(app['id'])
        self.svc.update_contact(app['id'], 'ops', COMPLIANCE,
                                {'name': 'Carol', 'id_number': 'C-2'})
        actions = [e['action'] for e in self.svc.audit_trail(app['id'])]
        for expected in ('application_created', 'bo_added', 'document_received',
                         'verification_completed', 'contact_updated',
                         'document_superseded', 'signing_eligible'):
            self.assertIn(expected, actions)
        trail = self.svc.audit_trail()
        self.assertEqual([e['seq'] for e in trail],
                         list(range(1, len(trail) + 1)))
        self.assertTrue(all(e['actor'] and e['role'] for e in trail))


class ValidationTest(ServiceTestCase):
    def test_create_requires_identity_and_contact(self):
        with self.assertRaises(Validation):
            self.svc.create_application('a', COMPLIANCE, {'legal_name': 'X'})
        with self.assertRaises(Validation):
            self.svc.create_application('a', COMPLIANCE, app_payload(contact={}))

    def test_bo_validation_and_ownership_cap(self):
        app = self._create()
        with self.assertRaises(Validation):
            self.svc.add_beneficial_owner(app['id'], 'a', COMPLIANCE,
                                          {'name': '', 'ownership_pct': 10})
        with self.assertRaises(Validation):
            self.svc.add_beneficial_owner(app['id'], 'a', COMPLIANCE,
                                          {'name': 'X', 'ownership_pct': 0})
        self._add_bo(app['id'], pct=60.0)
        with self.assertRaises(Validation):
            self._add_bo(app['id'], name='Carol', pct=50.0, id_number='BO-2')

    def test_unknown_application_and_bo(self):
        with self.assertRaises(NotFound):
            self.svc.get_application('app-999')
        app = self._create()
        with self.assertRaises(NotFound):
            self.svc.remove_beneficial_owner(app['id'], 'bo-999', 'a', COMPLIANCE)

    def test_unknown_doc_type_rejected(self):
        app = self._create()
        with self.assertRaises(Validation):
            self._doc(app['id'], 'essay', 'X-1', 'content')


class ConcurrencyTest(ServiceTestCase):
    def test_concurrent_bo_list_and_backfill(self):
        """负责人在现场并发：改受益人、名单更正、离线补传，状态须一致。"""
        app = self._create()
        self._complete_docs(app['id'])
        errors = []
        barrier = threading.Barrier(3)

        def guard(fn):
            try:
                barrier.wait(timeout=10)
                fn()
            except Exception as exc:  # noqa: BLE001 - 测试需收集一切异常
                errors.append(exc)

        def bo_worker():
            for i in range(15):
                self.svc.add_beneficial_owner(
                    app['id'], f'ops-{i}', COMPLIANCE,
                    {'name': f'Holder {i}', 'ownership_pct': 1.0,
                     'id_number': f'BO-X-{i}'})

        def list_worker():
            for i in range(1, 11):
                self.svc.apply_list_update('lists', COMPLIANCE, {
                    'version': i,
                    'entries': [{'entry_id': f'E-{i}', 'name': f'Person {i}',
                                 'action': 'add'}]})

        def backfill_worker():
            for i in range(15):
                self.svc.receive_document(
                    app['id'], f'offsite-{i}', COMPLIANCE,
                    {'doc_type': 'license', 'doc_number': f'LIC-{i % 3}',
                     'content': f'license v{i}', 'channel': 'offline_backfill'})

        threads = [threading.Thread(target=guard, args=(fn,))
                   for fn in (bo_worker, list_worker, backfill_worker)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [])
        # 审计序号连续完整
        trail = self.svc.audit_trail()
        self.assertEqual([e['seq'] for e in trail],
                         list(range(1, len(trail) + 1)))
        # 恰好一个当前结论，且与一次主动核验幂等一致
        currents = [c for c in self.svc.list_conclusions(app['id'])
                    if c['status'] == 'current']
        self.assertEqual(len(currents), 1)
        again = self.svc.verify(app['id'], 'clerk-1', COMPLIANCE)
        self.assertEqual(again['id'], currents[0]['id'])
        # 通知去重键全局唯一
        keys = [n['dedup_key'] for n in self.svc.list_notifications()]
        self.assertEqual(len(keys), len(set(keys)))
        # 同一（类型, 号码）的证件至多一个生效版本
        active = {}
        for doc in self.svc.list_documents(app['id']):
            if doc['status'] == 'active':
                key = (doc['doc_type'], doc['doc_number'])
                self.assertNotIn(key, active)
                active[key] = doc
        # 签约状态观察点结构完整
        status = self.svc.signing_status(app['id'])
        self.assertIn(status['status'],
                      ('intake', 'eligible', 'paused', 'signed'))


class PersistenceTest(unittest.TestCase):
    def test_store_persistence_keeps_review_queue_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'store.json')
            svc1 = DueDiligenceService(Store(path))
            app = svc1.create_application('ops', COMPLIANCE, app_payload())
            for doc_type, number in (('registry', 'R-1'), ('ownership_chart', 'O-1'),
                                     ('id_document', 'I-1')):
                svc1.receive_document(app['id'], 'clerk', COMPLIANCE,
                                      {'doc_type': doc_type, 'doc_number': number,
                                       'content': number})
            svc1.apply_list_update('lists', COMPLIANCE, {
                'version': 1,
                'entries': [{'entry_id': 'E-1', 'name': 'Alice Zhang',
                             'action': 'add'}]})
            task_id = svc1.review_queue()[0]['id']

            # 模拟服务重启：从同一文件恢复
            svc2 = DueDiligenceService(Store(path))
            self.assertEqual([t['id'] for t in svc2.review_queue()], [task_id])
            self.assertEqual(svc2.get_application(app['id'])['status'], 'paused')
            self.assertTrue(svc2.list_conclusions(app['id']))
            # 计数器延续，新档案不覆盖既有档案
            app2 = svc2.create_application('ops', COMPLIANCE,
                                           app_payload(legal_name='Other Ltd',
                                                       registration_no='REG-2'))
            self.assertNotEqual(app2['id'], app['id'])


if __name__ == '__main__':
    unittest.main()
