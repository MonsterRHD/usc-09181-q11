"""海外合作方尽调档案的核心领域服务。

设计要点：
- 主体（身份 + 联系人 + 受益所有人）每次变化生成新的快照版本，核验结论
  始终绑定「主体快照 × 证件集合 × 名单版本」的指纹；关系变化即重新计算，
  旧结论保留为历史（superseded），绝不套用到新主体。
- 证件分阶段收件；同一证件（类型 + 号码）重复发送且内容一致时幂等去重，
  内容变化则作废旧版本并触发重算；证件默认绑定主体，主体变更时相应证件
  自动作废（旧证件不服务新主体）。
- 制裁名单命中即暂停签约，只能由两名不同复核人双人复核解除；同一命中
  签名（命中条目 × 主体集合）放行后不重复暂停。
- 外部名单迟到或更正时，回标所有受影响的历史结论（affected_by），并按
  当前名单重算在办申请。
- 撤回申请后停止一切新查询与新收件，但保留监管所需摘要与完整历史；
  重启申请不丢失待复核队列，并按当前名单重新计算。
- 所有变更写入只增审计日志；通知按去重键合并（重复事件累加 suppressed）。
"""
from __future__ import annotations

from functools import wraps

from .models import (DEFAULT_DOC_BINDING, DOC_TYPE_BINDING, KNOWN_DOC_TYPES,
                     REQUIRED_APPROVALS, REQUIRED_DOC_TYPES, canonical_hash,
                     norm_text, utcnow)


class NotFound(Exception):
    """档案对象不存在。"""


class Conflict(Exception):
    """当前状态下不允许的操作。"""


class Validation(Exception):
    """请求内容不合法。"""


def _mutation(fn):
    """串行化写操作并在成功后持久化，保证并发现场下状态一致、重启不丢。"""

    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self.store.locked():
            result = fn(self, *args, **kwargs)
            self.store.persist()
            return result

    return wrapper


def _read(fn):
    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self.store.locked():
            return fn(self, *args, **kwargs)

    return wrapper


def _entry_matches(subject: dict, entry: dict) -> bool:
    sid, eid = norm_text(subject.get('id_number')), norm_text(entry.get('id_number'))
    if sid and eid and sid == eid:
        return True
    sname, ename = norm_text(subject.get('name')), norm_text(entry.get('name'))
    return bool(sname and ename and sname == ename)


class DueDiligenceService:
    def __init__(self, store):
        self.store = store

    # ------------------------------------------------------------------
    # 内部工具：审计 / 通知 / 查询
    # ------------------------------------------------------------------
    def _audit(self, actor, role, action, app_id=None, entity=None,
               entity_id=None, detail=None):
        trail = self.store.data['audit']
        trail.append({
            'seq': len(trail) + 1,
            'at': utcnow(),
            'actor': actor,
            'role': role,
            'action': action,
            'application_id': app_id,
            'entity': entity,
            'entity_id': entity_id,
            'detail': detail or {},
        })

    def _notify(self, ntype, app_id, dedup_key, payload):
        """按去重键合并通知：重复事件不新增通知，只在原通知上累加 suppressed。"""
        notifications = self.store.data['notifications']
        for existing in notifications.values():
            if existing['dedup_key'] == dedup_key:
                existing['suppressed'] += 1
                return existing, False
        nid = self.store.next_id('ntf')
        notification = {
            'id': nid,
            'type': ntype,
            'application_id': app_id,
            'dedup_key': dedup_key,
            'payload': payload,
            'created_at': utcnow(),
            'suppressed': 0,
        }
        notifications[nid] = notification
        return notification, True

    def _get_app(self, app_id):
        app = self.store.data['applications'].get(app_id)
        if not app:
            raise NotFound(f'application {app_id} not found')
        return app

    @staticmethod
    def _ensure_not_withdrawn(app):
        if app['status'] == 'withdrawn':
            raise Conflict('application withdrawn: new queries are stopped, '
                           'regulatory summaries remain available')

    # ------------------------------------------------------------------
    # 主体快照：身份 + 联系人 + 受益所有人
    # ------------------------------------------------------------------
    def _snapshot_payload(self, app):
        contact = app['contact']
        owners = sorted(
            ({'bo_id': bo['id'], 'name': bo['name'], 'id_number': bo.get('id_number'),
              'ownership_pct': bo['ownership_pct'], 'country': bo.get('country')}
             for bo in app['beneficial_owners'].values()),
            key=lambda item: item['bo_id'])
        return {
            'legal_name': app['legal_name'],
            'registration_no': app['registration_no'],
            'country': app['country'],
            'contact': {'name': contact.get('name'), 'id_number': contact.get('id_number'),
                        'email': contact.get('email'), 'phone': contact.get('phone')},
            'beneficial_owners': owners,
        }

    def _new_snapshot(self, app, reason):
        payload = self._snapshot_payload(app)
        digest = canonical_hash(payload)
        snapshots = app['snapshots']
        if snapshots and snapshots[-1]['hash'] == digest:
            return snapshots[-1], False
        snapshot = {'seq': len(snapshots) + 1, 'hash': digest,
                    'reason': reason, 'at': utcnow()}
        snapshots.append(snapshot)
        return snapshot, True

    def _screened_subjects(self, app):
        subjects = [{'kind': 'partner', 'ref': app['id'],
                     'name': app['legal_name'], 'id_number': app['registration_no']}]
        contact = app['contact']
        if contact.get('name') or contact.get('id_number'):
            subjects.append({'kind': 'contact', 'ref': 'contact',
                             'name': contact.get('name'),
                             'id_number': contact.get('id_number')})
        for bo in app['beneficial_owners'].values():
            subjects.append({'kind': 'bo', 'ref': bo['id'],
                             'name': bo['name'], 'id_number': bo.get('id_number')})
        return subjects

    # ------------------------------------------------------------------
    # 名单筛查
    # ------------------------------------------------------------------
    @staticmethod
    def _screen(subjects, entries):
        hits = []
        active = [entry for entry in entries.values() if entry.get('active')]
        for subject in subjects:
            for entry in active:
                if _entry_matches(subject, entry):
                    hits.append({'entry_id': entry['entry_id'],
                                 'entry_name': entry.get('name'),
                                 'program': entry.get('program'),
                                 'matched_kind': subject['kind'],
                                 'matched_ref': subject['ref'],
                                 'matched_name': subject.get('name')})
        hits.sort(key=lambda hit: (hit['entry_id'], hit['matched_ref']))
        return hits

    @staticmethod
    def _hit_signature(hits):
        """命中签名：命中条目 × 被命中主体的集合，与名单版本号无关。"""
        if not hits:
            return None
        return canonical_hash(sorted({(h['entry_id'], h['matched_ref']) for h in hits}))

    # ------------------------------------------------------------------
    # 证件
    # ------------------------------------------------------------------
    def _active_documents(self, app_id):
        return [doc for doc in self.store.data['documents'].values()
                if doc['application_id'] == app_id and doc['status'] == 'active']

    @staticmethod
    def _doc_set_hash(docs):
        return canonical_hash(sorted(
            ({'type': d['doc_type'], 'number': d['doc_number'],
              'hash': d['content_hash'], 'version': d['version']} for d in docs),
            key=lambda item: (item['type'], item['number'])))

    def _supersede_bound_documents(self, app, bound_to, reason, actor, role):
        superseded = []
        for doc in self.store.data['documents'].values():
            if (doc['application_id'] == app['id'] and doc['status'] == 'active'
                    and doc.get('bound_to') == bound_to):
                doc['status'] = 'superseded'
                doc['superseded_reason'] = reason
                superseded.append(doc['id'])
                self._audit(actor, role, 'document_superseded', app['id'],
                            'document', doc['id'],
                            {'reason': reason, 'bound_to': bound_to,
                             'doc_type': doc['doc_type']})
        return superseded

    # ------------------------------------------------------------------
    # 核验结论：绑定 主体快照 × 证件集合 × 名单版本
    # ------------------------------------------------------------------
    def _current_conclusion(self, app_id):
        for conclusion in self.store.data['conclusions'].values():
            if conclusion['application_id'] == app_id and conclusion['status'] == 'current':
                return conclusion
        return None

    def _recompute(self, app, actor, role, reason):
        """按当前主体、证件与名单重新计算结论；指纹不变则幂等返回。"""
        if app['status'] == 'withdrawn':
            return self._current_conclusion(app['id']), False
        docs = self._active_documents(app['id'])
        present = {doc['doc_type'] for doc in docs}
        missing = sorted(REQUIRED_DOC_TYPES - present)
        subjects = self._screened_subjects(app)
        hits = self._screen(subjects, self.store.data['sanctions']['entries'])
        list_version = self.store.data['sanctions']['current_version']
        result = 'hit' if hits else ('insufficient' if missing else 'clear')
        snapshot = app['snapshots'][-1]
        doc_set_hash = self._doc_set_hash(docs)
        fingerprint = canonical_hash({
            'snapshot': snapshot['hash'], 'docs': doc_set_hash,
            'list_version': list_version, 'result': result,
            'missing': missing, 'hits': hits})
        current = self._current_conclusion(app['id'])
        if current and current['fingerprint'] == fingerprint:
            return current, False
        if current:
            current['status'] = 'superseded'
        cid = self.store.next_id('con')
        conclusion = {
            'id': cid,
            'application_id': app['id'],
            'snapshot_seq': snapshot['seq'],
            'snapshot_hash': snapshot['hash'],
            'doc_set_hash': doc_set_hash,
            'list_version': list_version,
            'result': result,
            'missing_documents': missing,
            'hits': hits,
            'hit_signature': self._hit_signature(hits),
            'screened_subjects': subjects,
            'fingerprint': fingerprint,
            'status': 'current',
            'affected_by': [],
            'reason': reason,
            'created_at': utcnow(),
        }
        self.store.data['conclusions'][cid] = conclusion
        self._audit(actor, role, 'verification_completed', app['id'],
                    'conclusion', cid,
                    {'result': result, 'reason': reason,
                     'supersedes': current['id'] if current else None,
                     'snapshot_seq': snapshot['seq'], 'list_version': list_version,
                     'hit_count': len(hits), 'missing_documents': missing})
        self._apply_transitions(app, conclusion, actor, role)
        return conclusion, True

    def _apply_transitions(self, app, conclusion, actor, role):
        previous = app['status']
        if conclusion['result'] == 'hit':
            signature = conclusion['hit_signature']
            if signature in app['released_hit_signatures']:
                # 同一命中签名已经双人复核放行，不重复暂停
                self._audit(actor, role, 'hit_already_released', app['id'],
                            'conclusion', conclusion['id'],
                            {'hit_signature': signature})
                if previous == 'paused':
                    app['status'] = 'eligible'
                return
            app['status'] = 'paused'
            task = self._ensure_review_task(app, conclusion, actor, role)
            self._notify('signing_paused', app['id'],
                         f"paused:{app['id']}:{signature}",
                         {'conclusion_id': conclusion['id'],
                          'review_task_id': task['id'],
                          'hits': conclusion['hits']})
            if previous != 'paused':
                self._audit(actor, role, 'signing_paused', app['id'],
                            'conclusion', conclusion['id'],
                            {'previous_status': previous,
                             'review_task_id': task['id']})
        elif conclusion['result'] == 'clear':
            if previous in ('intake', 'paused'):
                app['status'] = 'eligible'
                self._notify('signing_eligible', app['id'],
                             f"eligible:{app['id']}:{conclusion['fingerprint']}",
                             {'conclusion_id': conclusion['id']})
                self._audit(actor, role, 'signing_eligible', app['id'],
                            'conclusion', conclusion['id'],
                            {'previous_status': previous})
        else:  # insufficient：证件未收齐，签约资格不成立
            if previous in ('eligible', 'paused'):
                app['status'] = 'intake'
                self._notify('signing_eligibility_lost', app['id'],
                             f"ineligible:{app['id']}:{conclusion['fingerprint']}",
                             {'conclusion_id': conclusion['id'],
                              'missing_documents': conclusion['missing_documents']})
                self._audit(actor, role, 'signing_eligibility_lost', app['id'],
                            'conclusion', conclusion['id'],
                            {'previous_status': previous,
                             'missing_documents': conclusion['missing_documents']})

    def _ensure_review_task(self, app, conclusion, actor, role):
        """同一命中签名的待复核任务不重复创建。"""
        for task in self.store.data['review_tasks'].values():
            if (task['application_id'] == app['id'] and task['status'] == 'pending'
                    and task['hit_signature'] == conclusion['hit_signature']):
                return task
        tid = self.store.next_id('rev')
        task = {
            'id': tid,
            'application_id': app['id'],
            'conclusion_id': conclusion['id'],
            'hit_signature': conclusion['hit_signature'],
            'type': 'sanctions_hit_release',
            'status': 'pending',
            'required_approvals': REQUIRED_APPROVALS,
            'decisions': [],
            'created_at': utcnow(),
            'resolved_at': None,
        }
        self.store.data['review_tasks'][tid] = task
        self._audit(actor, role, 'review_task_created', app['id'],
                    'review_task', tid,
                    {'conclusion_id': conclusion['id'], 'hits': conclusion['hits']})
        self._notify('review_task_created', app['id'], f'review_task:{tid}',
                     {'task_id': tid, 'conclusion_id': conclusion['id']})
        return task

    # ------------------------------------------------------------------
    # 合作申请
    # ------------------------------------------------------------------
    @_mutation
    def create_application(self, actor, role, payload):
        legal_name = (payload.get('legal_name') or '').strip()
        registration_no = (payload.get('registration_no') or '').strip()
        country = (payload.get('country') or '').strip()
        contact = payload.get('contact') or {}
        if not legal_name or not registration_no or not country:
            raise Validation('legal_name, registration_no and country are required')
        if not (contact.get('name') or '').strip():
            raise Validation('contact.name is required')
        app_id = self.store.next_id('app')
        app = {
            'id': app_id,
            'legal_name': legal_name,
            'registration_no': registration_no,
            'country': country,
            'contact': {'name': contact['name'].strip(),
                        'id_number': contact.get('id_number'),
                        'email': contact.get('email'),
                        'phone': contact.get('phone')},
            'beneficial_owners': {},
            'status': 'intake',
            'snapshots': [],
            'released_hit_signatures': [],
            'withdrawals': [],
            'created_at': utcnow(),
            'created_by': actor,
            'signed_at': None,
            'restarted_at': None,
        }
        self.store.data['applications'][app_id] = app
        self._new_snapshot(app, 'application_created')
        self._audit(actor, role, 'application_created', app_id, 'application', app_id,
                    {'legal_name': legal_name, 'registration_no': registration_no,
                     'country': country})
        self._recompute(app, actor, role, 'application_created')
        return app

    @_mutation
    def update_identity(self, app_id, actor, role, payload):
        """主体身份变更：注册号变化时，绑定旧主体的证件全部作废并重算。"""
        app = self._get_app(app_id)
        self._ensure_not_withdrawn(app)
        changed = {}
        for field in ('legal_name', 'registration_no', 'country'):
            value = payload.get(field)
            if value is not None and str(value).strip() and str(value).strip() != app[field]:
                changed[field] = {'old': app[field], 'new': str(value).strip()}
                app[field] = str(value).strip()
        if not changed:
            return app, False
        superseded = []
        if 'registration_no' in changed:
            superseded = self._supersede_bound_documents(app, 'partner',
                                                         'subject_changed', actor, role)
        snapshot, _ = self._new_snapshot(app, 'identity_updated')
        self._audit(actor, role, 'identity_updated', app_id, 'application', app_id,
                    {'changed': changed, 'snapshot_seq': snapshot['seq'],
                     'superseded_documents': superseded})
        self._recompute(app, actor, role, 'identity_updated')
        return app, True

    @_mutation
    def update_contact(self, app_id, actor, role, payload):
        """更换联系人：旧联系人证件立即作废，新主体证件补齐前结论为 insufficient。"""
        app = self._get_app(app_id)
        self._ensure_not_withdrawn(app)
        name = (payload.get('name') or '').strip()
        if not name:
            raise Validation('contact.name is required')
        old = dict(app['contact'])
        new = {'name': name, 'id_number': payload.get('id_number'),
               'email': payload.get('email'), 'phone': payload.get('phone')}
        if new == old:
            return app, False
        app['contact'] = new
        superseded = self._supersede_bound_documents(app, 'contact',
                                                     'contact_changed', actor, role)
        snapshot, _ = self._new_snapshot(app, 'contact_updated')
        self._audit(actor, role, 'contact_updated', app_id, 'application', app_id,
                    {'old': old, 'new': new, 'snapshot_seq': snapshot['seq'],
                     'superseded_documents': superseded})
        self._recompute(app, actor, role, 'contact_updated')
        return app, True

    # ------------------------------------------------------------------
    # 受益所有人
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_ownership(app, pct, exclude_bo_id=None):
        total = sum(bo['ownership_pct'] for bid, bo in app['beneficial_owners'].items()
                    if bid != exclude_bo_id)
        if total + pct > 100 + 1e-9:
            raise Validation('total beneficial ownership exceeds 100%')

    @_mutation
    def add_beneficial_owner(self, app_id, actor, role, payload):
        app = self._get_app(app_id)
        self._ensure_not_withdrawn(app)
        name = (payload.get('name') or '').strip()
        if not name:
            raise Validation('name is required')
        try:
            pct = float(payload.get('ownership_pct'))
        except (TypeError, ValueError):
            raise Validation('ownership_pct must be a number in (0, 100]')
        if not 0 < pct <= 100:
            raise Validation('ownership_pct must be in (0, 100]')
        self._validate_ownership(app, pct)
        bo_id = self.store.next_id('bo')
        bo = {'id': bo_id, 'name': name, 'id_number': payload.get('id_number'),
              'ownership_pct': pct, 'country': payload.get('country')}
        app['beneficial_owners'][bo_id] = bo
        snapshot, _ = self._new_snapshot(app, 'bo_added')
        self._audit(actor, role, 'bo_added', app_id, 'bo', bo_id,
                    {'name': name, 'ownership_pct': pct,
                     'snapshot_seq': snapshot['seq']})
        self._recompute(app, actor, role, 'bo_added')
        return bo

    @_mutation
    def update_beneficial_owner(self, app_id, bo_id, actor, role, payload):
        app = self._get_app(app_id)
        self._ensure_not_withdrawn(app)
        bo = app['beneficial_owners'].get(bo_id)
        if not bo:
            raise NotFound(f'beneficial owner {bo_id} not found')
        before = dict(bo)
        if 'name' in payload and payload['name']:
            bo['name'] = str(payload['name']).strip()
        if 'id_number' in payload:
            bo['id_number'] = payload['id_number']
        if 'country' in payload:
            bo['country'] = payload['country']
        if 'ownership_pct' in payload:
            try:
                pct = float(payload['ownership_pct'])
            except (TypeError, ValueError):
                raise Validation('ownership_pct must be a number in (0, 100]')
            if not 0 < pct <= 100:
                raise Validation('ownership_pct must be in (0, 100]')
            self._validate_ownership(app, pct, exclude_bo_id=bo_id)
            bo['ownership_pct'] = pct
        if bo == before:
            return bo, False
        snapshot, _ = self._new_snapshot(app, 'bo_updated')
        self._audit(actor, role, 'bo_updated', app_id, 'bo', bo_id,
                    {'before': before, 'after': dict(bo),
                     'snapshot_seq': snapshot['seq']})
        self._recompute(app, actor, role, 'bo_updated')
        return bo, True

    @_mutation
    def remove_beneficial_owner(self, app_id, bo_id, actor, role):
        app = self._get_app(app_id)
        self._ensure_not_withdrawn(app)
        bo = app['beneficial_owners'].pop(bo_id, None)
        if not bo:
            raise NotFound(f'beneficial owner {bo_id} not found')
        snapshot, _ = self._new_snapshot(app, 'bo_removed')
        self._audit(actor, role, 'bo_removed', app_id, 'bo', bo_id,
                    {'removed': bo, 'snapshot_seq': snapshot['seq']})
        self._recompute(app, actor, role, 'bo_removed')
        return bo

    # ------------------------------------------------------------------
    # 证件收件（分阶段、幂等）
    # ------------------------------------------------------------------
    @_mutation
    def receive_document(self, app_id, actor, role, payload):
        app = self._get_app(app_id)
        self._ensure_not_withdrawn(app)
        doc_type = (payload.get('doc_type') or '').strip()
        doc_number = (payload.get('doc_number') or '').strip()
        if doc_type not in KNOWN_DOC_TYPES:
            raise Validation(f'doc_type must be one of {sorted(KNOWN_DOC_TYPES)}')
        if not doc_number:
            raise Validation('doc_number is required')
        content_hash = payload.get('content_hash')
        if content_hash is None:
            content = payload.get('content')
            if content is None:
                raise Validation('content or content_hash is required')
            content_hash = canonical_hash({'content': content})
        channel = payload.get('channel') or 'online'
        if channel not in ('online', 'offline_backfill'):
            raise Validation("channel must be 'online' or 'offline_backfill'")
        existing = None
        for doc in self.store.data['documents'].values():
            if (doc['application_id'] == app_id and doc['doc_type'] == doc_type
                    and doc['doc_number'] == doc_number and doc['status'] == 'active'):
                existing = doc
                break
        if existing and existing['content_hash'] == content_hash:
            # 同一证件重复发送：幂等，不产生新版本、不触发重算
            self._audit(actor, role, 'document_deduplicated', app_id,
                        'document', existing['id'],
                        {'doc_type': doc_type, 'doc_number': doc_number,
                         'channel': channel})
            return existing, True
        version = 1
        if existing:
            existing['status'] = 'superseded'
            existing['superseded_reason'] = 'content_replaced'
            version = existing['version'] + 1
            self._audit(actor, role, 'document_superseded', app_id,
                        'document', existing['id'],
                        {'reason': 'content_replaced', 'doc_type': doc_type,
                         'doc_number': doc_number})
        doc_id = self.store.next_id('doc')
        doc = {
            'id': doc_id,
            'application_id': app_id,
            'doc_type': doc_type,
            'doc_number': doc_number,
            'content_hash': content_hash,
            'version': version,
            'status': 'active',
            'bound_to': DOC_TYPE_BINDING.get(doc_type, DEFAULT_DOC_BINDING),
            'channel': channel,
            'received_at': utcnow(),
            'received_by': actor,
            'summary': payload.get('summary'),
        }
        self.store.data['documents'][doc_id] = doc
        self._audit(actor, role, 'document_received', app_id, 'document', doc_id,
                    {'doc_type': doc_type, 'doc_number': doc_number,
                     'version': version, 'channel': channel})
        self._recompute(app, actor, role, 'document_received')
        return doc, False

    @_mutation
    def verify(self, app_id, actor, role):
        """主动触发一次核验；撤回后的申请拒绝新查询。"""
        app = self._get_app(app_id)
        self._ensure_not_withdrawn(app)
        conclusion, _ = self._recompute(app, actor, role, 'manual_verify')
        return conclusion

    # ------------------------------------------------------------------
    # 签约 / 撤回 / 重启
    # ------------------------------------------------------------------
    @_mutation
    def sign(self, app_id, actor, role):
        app = self._get_app(app_id)
        if app['status'] != 'eligible':
            raise Conflict(f"cannot sign while status is {app['status']}")
        app['status'] = 'signed'
        app['signed_at'] = utcnow()
        self._audit(actor, role, 'application_signed', app_id, 'application', app_id,
                    {'signed_at': app['signed_at']})
        self._notify('application_signed', app_id,
                     f"signed:{app_id}:{app['signed_at']}", {})
        return app

    @_mutation
    def withdraw(self, app_id, actor, role, reason=None):
        """撤回：停止新查询与新收件；保留监管摘要、历史结论与待复核队列。"""
        app = self._get_app(app_id)
        if app['status'] == 'withdrawn':
            raise Conflict('application already withdrawn')
        previous = app['status']
        app['status'] = 'withdrawn'
        app['withdrawals'].append({'at': utcnow(), 'by': actor,
                                   'reason': reason, 'previous_status': previous})
        pending = [t['id'] for t in self.store.data['review_tasks'].values()
                   if t['application_id'] == app_id and t['status'] == 'pending']
        self._audit(actor, role, 'application_withdrawn', app_id, 'application', app_id,
                    {'previous_status': previous, 'reason': reason,
                     'pending_review_tasks': pending})
        self._notify('application_withdrawn', app_id,
                     f"withdrawn:{app_id}:{len(app['withdrawals'])}",
                     {'reason': reason})
        return app

    @_mutation
    def restart(self, app_id, actor, role):
        """重启申请：待复核队列原样保留，按当前名单与证件重新计算。"""
        app = self._get_app(app_id)
        if app['status'] != 'withdrawn':
            raise Conflict('only a withdrawn application can be restarted')
        app['status'] = 'intake'
        app['restarted_at'] = utcnow()
        pending = [t['id'] for t in self.store.data['review_tasks'].values()
                   if t['application_id'] == app_id and t['status'] == 'pending']
        self._audit(actor, role, 'application_restarted', app_id, 'application', app_id,
                    {'pending_review_tasks': pending})
        self._notify('application_restarted', app_id,
                     f"restarted:{app_id}:{app['restarted_at']}", {})
        conclusion, changed = self._recompute(app, actor, role, 'application_restarted')
        if not changed and conclusion:
            # 撤回期间无任何变化时重算幂等，仍需让状态收敛到当前结论
            self._apply_transitions(app, conclusion, actor, role)
        return app

    # ------------------------------------------------------------------
    # 制裁名单：迟到与更正
    # ------------------------------------------------------------------
    @_mutation
    def apply_list_update(self, actor, role, payload):
        sanctions = self.store.data['sanctions']
        version = payload.get('version')
        if version is None:
            raise Validation('version is required')
        if not isinstance(version, int) or version <= sanctions['current_version']:
            raise Conflict(f"list version must increase monotonically "
                           f"(current {sanctions['current_version']})")
        kind = payload.get('kind') or 'update'
        if kind not in ('update', 'correction'):
            raise Validation("kind must be 'update' or 'correction'")
        entries_in = payload.get('entries') or []
        if not entries_in:
            raise Validation('entries must be a non-empty list')
        applied = []
        for item in entries_in:
            action = item.get('action')
            entry_id = (item.get('entry_id') or '').strip()
            if action not in ('add', 'remove') or not entry_id:
                raise Validation('each entry needs action add|remove and entry_id')
            if action == 'add':
                name = (item.get('name') or '').strip()
                if not name and not item.get('id_number'):
                    raise Validation('entry needs name or id_number')
                sanctions['entries'][entry_id] = {
                    'entry_id': entry_id, 'name': name,
                    'id_number': item.get('id_number'),
                    'program': item.get('program'),
                    'active': True, 'list_version': version,
                    'effective_from': item.get('effective_from'),
                }
            else:
                target = sanctions['entries'].get(entry_id)
                if not target or not target.get('active'):
                    raise Validation(f'cannot remove unknown or inactive entry {entry_id}')
                target['active'] = False
                target['removed_in_version'] = version
            applied.append({'action': action, 'entry_id': entry_id})
        sanctions['current_version'] = version
        sanctions['updates'].append({'version': version, 'kind': kind,
                                     'applied_at': utcnow(), 'applied_by': actor,
                                     'entries': applied})
        # 回标受影响的历史结论（含已撤回申请的历史，供监管追溯）
        affected = self._mark_affected_conclusions(version, kind, actor, role)
        # 在办申请按新名单重算；已撤回申请不再发起新查询
        recomputed = []
        for app in self.store.data['applications'].values():
            if app['status'] == 'withdrawn':
                continue
            _, changed = self._recompute(app, actor, role, f'list_update:{version}')
            if changed:
                recomputed.append(app['id'])
        self._audit(actor, role, 'sanctions_list_applied', None,
                    'sanctions_list', str(version),
                    {'version': version, 'kind': kind, 'entries': applied,
                     'affected_conclusions': affected,
                     'recomputed_applications': recomputed})
        return {'version': version, 'kind': kind,
                'affected_conclusions': affected,
                'recomputed_applications': recomputed}

    def _mark_affected_conclusions(self, version, kind, actor, role):
        """名单迟到/更正：重估历史结论，命中差异的标记 affected_by。"""
        affected = []
        entries = self.store.data['sanctions']['entries']
        for conclusion in list(self.store.data['conclusions'].values()):
            if conclusion['list_version'] >= version:
                continue
            if any(mark['list_version'] == version
                   for mark in conclusion['affected_by']):
                continue
            new_hits = self._screen(conclusion['screened_subjects'], entries)
            old_keys = {(h['entry_id'], h['matched_ref']) for h in conclusion['hits']}
            new_keys = {(h['entry_id'], h['matched_ref']) for h in new_hits}
            if old_keys == new_keys:
                continue
            reevaluated = ('hit' if new_hits else
                           'insufficient' if conclusion['missing_documents'] else 'clear')
            conclusion['affected_by'].append({
                'list_version': version, 'kind': kind, 'at': utcnow(),
                'previous_result': conclusion['result'],
                'reevaluated_result': reevaluated,
            })
            affected.append(conclusion['id'])
            self._audit(actor, role, 'conclusion_marked_affected',
                        conclusion['application_id'], 'conclusion', conclusion['id'],
                        {'list_version': version, 'kind': kind,
                         'previous_result': conclusion['result'],
                         'reevaluated_result': reevaluated})
            self._notify('conclusion_affected', conclusion['application_id'],
                         f"affected:{conclusion['id']}:{version}",
                         {'conclusion_id': conclusion['id'],
                          'list_version': version,
                          'reevaluated_result': reevaluated})
        return affected

    # ------------------------------------------------------------------
    # 双人复核
    # ------------------------------------------------------------------
    @_mutation
    def record_decision(self, task_id, actor, role, decision, note=None):
        task = self.store.data['review_tasks'].get(task_id)
        if not task:
            raise NotFound(f'review task {task_id} not found')
        if task['status'] != 'pending':
            raise Conflict(f"review task already {task['status']}")
        if decision not in ('approve', 'reject'):
            raise Validation("decision must be 'approve' or 'reject'")
        app = self._get_app(task['application_id'])
        if app['status'] == 'withdrawn':
            raise Conflict('application withdrawn: review decisions are suspended')
        if any(d['reviewer'] == actor for d in task['decisions']):
            raise Conflict(f'reviewer {actor} already decided on this task')
        task['decisions'].append({'reviewer': actor, 'decision': decision,
                                  'note': note, 'at': utcnow()})
        self._audit(actor, role, 'review_decision_recorded', app['id'],
                    'review_task', task_id, {'decision': decision, 'note': note})
        if decision == 'reject':
            task['status'] = 'rejected'
            task['resolved_at'] = utcnow()
            self._audit(actor, role, 'review_task_rejected', app['id'],
                        'review_task', task_id, {'reviewer': actor})
            self._notify('review_task_resolved', app['id'],
                         f'review_resolved:{task_id}',
                         {'task_id': task_id, 'status': 'rejected'})
            return task
        approvers = {d['reviewer'] for d in task['decisions']
                     if d['decision'] == 'approve'}
        if len(approvers) >= task['required_approvals']:
            task['status'] = 'approved'
            task['resolved_at'] = utcnow()
            app['released_hit_signatures'].append(task['hit_signature'])
            self._audit(actor, role, 'review_task_approved', app['id'],
                        'review_task', task_id,
                        {'approvers': sorted(approvers),
                         'hit_signature': task['hit_signature']})
            self._notify('review_task_resolved', app['id'],
                         f'review_resolved:{task_id}',
                         {'task_id': task_id, 'status': 'approved'})
            current = self._current_conclusion(app['id'])
            if (current and current['hit_signature'] == task['hit_signature']
                    and current['result'] == 'hit' and app['status'] == 'paused'):
                app['status'] = 'eligible'
                self._audit(actor, role, 'signing_released', app['id'],
                            'conclusion', current['id'],
                            {'released_by': sorted(approvers),
                             'review_task_id': task_id})
                self._notify('signing_released', app['id'],
                             f"released:{app['id']}:{task['hit_signature']}",
                             {'review_task_id': task_id,
                              'released_by': sorted(approvers)})
        return task

    @_mutation
    def request_review(self, app_id, actor, role):
        """复核被拒后，针对当前命中重新发起复核任务。"""
        app = self._get_app(app_id)
        self._ensure_not_withdrawn(app)
        conclusion = self._current_conclusion(app_id)
        if not conclusion or conclusion['result'] != 'hit':
            raise Conflict('no current sanctions hit to review')
        return self._ensure_review_task(app, conclusion, actor, role)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @_read
    def get_application(self, app_id):
        return self._get_app(app_id)

    @_read
    def list_applications(self):
        return list(self.store.data['applications'].values())

    @_read
    def list_documents(self, app_id):
        self._get_app(app_id)
        docs = [d for d in self.store.data['documents'].values()
                if d['application_id'] == app_id]
        return sorted(docs, key=lambda d: d['received_at'])

    @_read
    def list_conclusions(self, app_id):
        self._get_app(app_id)
        conclusions = [c for c in self.store.data['conclusions'].values()
                       if c['application_id'] == app_id]
        return sorted(conclusions, key=lambda c: c['created_at'])

    @_read
    def review_queue(self, status='pending', app_id=None):
        tasks = [t for t in self.store.data['review_tasks'].values()
                 if (status is None or t['status'] == status)
                 and (app_id is None or t['application_id'] == app_id)]
        return sorted(tasks, key=lambda t: t['created_at'])

    @_read
    def list_notifications(self, app_id=None):
        notifications = [n for n in self.store.data['notifications'].values()
                         if app_id is None or n['application_id'] == app_id]
        return sorted(notifications, key=lambda n: n['created_at'])

    @_read
    def audit_trail(self, app_id=None):
        return [e for e in self.store.data['audit']
                if app_id is None or e['application_id'] == app_id]

    @_read
    def sanctions_state(self):
        sanctions = self.store.data['sanctions']
        return {
            'current_version': sanctions['current_version'],
            'active_entries': sorted(
                (e for e in sanctions['entries'].values() if e.get('active')),
                key=lambda e: e['entry_id']),
            'updates': list(sanctions['updates']),
        }

    @_read
    def signing_status(self, app_id):
        """签约状态观察点：状态机、当前结论与待复核任务一屏呈现。"""
        app = self._get_app(app_id)
        current = self._current_conclusion(app_id)
        pending = [t['id'] for t in self.review_queue(status='pending', app_id=app_id)]
        status = app['status']
        return {
            'application_id': app_id,
            'status': status,
            'signing': {
                'eligible': status == 'eligible',
                'paused': status == 'paused',
                'signed': status == 'signed',
                'withdrawn': status == 'withdrawn',
            },
            'current_conclusion': current,
            'pending_review_tasks': pending,
            'released_hit_count': len(app['released_hit_signatures']),
        }

    @_read
    def regulatory_summary(self, app_id):
        """监管所需摘要：撤回后仍保留；不含证件号等原始敏感字段。"""
        app = self._get_app(app_id)
        docs = [d for d in self.store.data['documents'].values()
                if d['application_id'] == app_id]
        conclusions = sorted(
            (c for c in self.store.data['conclusions'].values()
             if c['application_id'] == app_id),
            key=lambda c: c['created_at'])
        current = self._current_conclusion(app_id)
        tasks = [t for t in self.store.data['review_tasks'].values()
                 if t['application_id'] == app_id]
        return {
            'application_id': app_id,
            'legal_name': app['legal_name'],
            'registration_no_hash': canonical_hash(app['registration_no'])[:16],
            'country': app['country'],
            'status': app['status'],
            'withdrawn': app['status'] == 'withdrawn',
            'withdrawals': list(app['withdrawals']),
            'snapshot_seq': app['snapshots'][-1]['seq'],
            'current_conclusion': current and {
                'id': current['id'], 'result': current['result'],
                'list_version': current['list_version'],
                'snapshot_seq': current['snapshot_seq'],
                'hit_count': len(current['hits']),
                'created_at': current['created_at'],
            },
            'conclusion_history': [
                {'id': c['id'], 'result': c['result'],
                 'list_version': c['list_version'], 'status': c['status'],
                 'affected_by': c['affected_by'], 'created_at': c['created_at']}
                for c in conclusions
            ],
            'documents': {
                'active': sum(1 for d in docs if d['status'] == 'active'),
                'superseded': sum(1 for d in docs if d['status'] == 'superseded'),
                'types': sorted({d['doc_type'] for d in docs
                                 if d['status'] == 'active'}),
            },
            'beneficial_owner_count': len(app['beneficial_owners']),
            'review_tasks': [
                {'id': t['id'], 'type': t['type'], 'status': t['status'],
                 'decision_count': len(t['decisions']),
                 'created_at': t['created_at'], 'resolved_at': t['resolved_at']}
                for t in sorted(tasks, key=lambda t: t['created_at'])
            ],
            'generated_at': utcnow(),
        }
