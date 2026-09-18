"""HTTP API 层：路由、角色门禁与脱敏视图输出。

角色（X-Role 头）：
- business   业务方：脱敏视图，可发起收件/变更；
- compliance 合规：全量视图，可执行复核决定；
- auditor    审计：全量视图 + 审计轨迹；
- regulator  监管：仅 /health 与监管摘要（只读）。
操作者通过 X-Actor 头标识，写入审计日志。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from . import masking
from .core import Conflict, NotFound, Validation
from .models import AUDIT_ROLES, REVIEW_ROLES


class Forbidden(Exception):
    """角色无权访问。"""


ROUTES = [
    ('GET', r'/health', 'health'),
    ('POST', r'/applications', 'create_application'),
    ('GET', r'/applications', 'list_applications'),
    ('GET', r'/applications/(?P<app>[^/]+)', 'get_application'),
    ('PATCH', r'/applications/(?P<app>[^/]+)', 'update_identity'),
    ('POST', r'/applications/(?P<app>[^/]+)/contact', 'update_contact'),
    ('POST', r'/applications/(?P<app>[^/]+)/beneficial-owners', 'add_bo'),
    ('PATCH', r'/applications/(?P<app>[^/]+)/beneficial-owners/(?P<bo>[^/]+)', 'update_bo'),
    ('DELETE', r'/applications/(?P<app>[^/]+)/beneficial-owners/(?P<bo>[^/]+)', 'remove_bo'),
    ('POST', r'/applications/(?P<app>[^/]+)/documents', 'receive_document'),
    ('GET', r'/applications/(?P<app>[^/]+)/documents', 'list_documents'),
    ('POST', r'/applications/(?P<app>[^/]+)/verify', 'verify'),
    ('GET', r'/applications/(?P<app>[^/]+)/conclusions', 'list_conclusions'),
    ('GET', r'/applications/(?P<app>[^/]+)/signing-status', 'signing_status'),
    ('POST', r'/applications/(?P<app>[^/]+)/sign', 'sign'),
    ('POST', r'/applications/(?P<app>[^/]+)/withdraw', 'withdraw'),
    ('POST', r'/applications/(?P<app>[^/]+)/restart', 'restart'),
    ('POST', r'/applications/(?P<app>[^/]+)/review-requests', 'request_review'),
    ('GET', r'/applications/(?P<app>[^/]+)/regulatory-summary', 'regulatory_summary'),
    ('GET', r'/applications/(?P<app>[^/]+)/audit', 'app_audit'),
    ('POST', r'/sanctions-list/updates', 'apply_list_update'),
    ('GET', r'/sanctions-list', 'sanctions_state'),
    ('GET', r'/review-queue', 'review_queue'),
    ('POST', r'/review-tasks/(?P<task>[^/]+)/decisions', 'record_decision'),
    ('GET', r'/notifications', 'notifications'),
    ('GET', r'/audit', 'global_audit'),
]


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        server_version = 'DueDiligence/0.1'

        def log_message(self, *args):
            pass

        # ---------------- 基础 ----------------
        def _send(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status, code, message):
            self._send(status, {'error': {'code': code, 'message': message}})

        def _body(self):
            length = int(self.headers.get('Content-Length') or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                raise Validation('request body must be valid JSON')
            if not isinstance(data, dict):
                raise Validation('request body must be a JSON object')
            return data

        def _context(self):
            role = (self.headers.get('X-Role') or 'business').strip().lower()
            actor = (self.headers.get('X-Actor') or 'anonymous').strip()
            return actor, role

        # ---------------- 路由 ----------------
        def _dispatch(self, method):
            parsed = urlparse(self.path)
            path = parsed.path if parsed.path == '/' else parsed.path.rstrip('/')
            query = parse_qs(parsed.query)
            actor, role = self._context()
            try:
                if role == 'regulator' and not (
                        method == 'GET' and (path == '/health'
                                             or path.endswith('/regulatory-summary'))):
                    raise Forbidden('regulator role may only read regulatory summaries')
                status, payload = self._route(method, path, query, actor, role)
                self._send(status, payload)
            except NotFound as exc:
                self._error(404, 'not_found', str(exc))
            except Conflict as exc:
                self._error(409, 'conflict', str(exc))
            except Validation as exc:
                self._error(400, 'validation', str(exc))
            except Forbidden as exc:
                self._error(403, 'forbidden', str(exc))

        def _route(self, method, path, query, actor, role):
            for route_method, pattern, handler_name in ROUTES:
                if route_method != method:
                    continue
                match = re.fullmatch(pattern, path)
                if match:
                    handler = getattr(self, f'_h_{handler_name}')
                    return handler(actor, role, query, **match.groupdict())
            raise NotFound(f'{method} {path}')

        do_GET = lambda self: self._dispatch('GET')
        do_POST = lambda self: self._dispatch('POST')
        do_PATCH = lambda self: self._dispatch('PATCH')
        do_DELETE = lambda self: self._dispatch('DELETE')

        # ---------------- 各端点 ----------------
        def _h_health(self, actor, role, query):
            return 200, {'status': 'ok'}

        def _h_create_application(self, actor, role, query):
            app = service.create_application(actor, role, self._body())
            return 201, masking.application_view(app, role)

        def _h_list_applications(self, actor, role, query):
            apps = service.list_applications()
            return 200, {'applications': [masking.application_view(a, role) for a in apps]}

        def _h_get_application(self, actor, role, query, app):
            return 200, masking.application_view(service.get_application(app), role)

        def _h_update_identity(self, actor, role, query, app):
            updated, changed = service.update_identity(app, actor, role, self._body())
            return 200, {'application': masking.application_view(updated, role),
                         'changed': changed}

        def _h_update_contact(self, actor, role, query, app):
            updated, changed = service.update_contact(app, actor, role, self._body())
            return 200, {'application': masking.application_view(updated, role),
                         'changed': changed}

        def _h_add_bo(self, actor, role, query, app):
            bo = service.add_beneficial_owner(app, actor, role, self._body())
            return 201, {'beneficial_owner': masking.bo_view(bo, role)}

        def _h_update_bo(self, actor, role, query, app, bo):
            updated, changed = service.update_beneficial_owner(app, bo, actor, role,
                                                               self._body())
            return 200, {'beneficial_owner': masking.bo_view(updated, role),
                         'changed': changed}

        def _h_remove_bo(self, actor, role, query, app, bo):
            service.remove_beneficial_owner(app, bo, actor, role)
            return 200, {'removed': bo}

        def _h_receive_document(self, actor, role, query, app):
            doc, deduplicated = service.receive_document(app, actor, role, self._body())
            status = 200 if deduplicated else 201
            return status, {'document': masking.document_view(doc, role),
                            'deduplicated': deduplicated}

        def _h_list_documents(self, actor, role, query, app):
            docs = service.list_documents(app)
            return 200, {'documents': [masking.document_view(d, role) for d in docs]}

        def _h_verify(self, actor, role, query, app):
            conclusion = service.verify(app, actor, role)
            return 200, {'conclusion': masking.conclusion_view(conclusion, role)}

        def _h_list_conclusions(self, actor, role, query, app):
            conclusions = service.list_conclusions(app)
            return 200, {'conclusions': [masking.conclusion_view(c, role)
                                         for c in conclusions]}

        def _h_signing_status(self, actor, role, query, app):
            status = service.signing_status(app)
            if status['current_conclusion']:
                status['current_conclusion'] = masking.conclusion_view(
                    status['current_conclusion'], role)
            return 200, status

        def _h_sign(self, actor, role, query, app):
            updated = service.sign(app, actor, role)
            return 200, {'application': masking.application_view(updated, role)}

        def _h_withdraw(self, actor, role, query, app):
            reason = self._body().get('reason')
            updated = service.withdraw(app, actor, role, reason)
            return 200, {'application': masking.application_view(updated, role)}

        def _h_restart(self, actor, role, query, app):
            updated = service.restart(app, actor, role)
            return 200, {'application': masking.application_view(updated, role)}

        def _h_request_review(self, actor, role, query, app):
            task = service.request_review(app, actor, role)
            return 201, {'task': task}

        def _h_regulatory_summary(self, actor, role, query, app):
            return 200, service.regulatory_summary(app)

        def _h_app_audit(self, actor, role, query, app):
            if role not in AUDIT_ROLES:
                raise Forbidden('audit trail requires compliance or auditor role')
            return 200, {'events': service.audit_trail(app)}

        def _h_global_audit(self, actor, role, query):
            if role not in AUDIT_ROLES:
                raise Forbidden('audit trail requires compliance or auditor role')
            return 200, {'events': service.audit_trail()}

        def _h_apply_list_update(self, actor, role, query):
            return 200, service.apply_list_update(actor, role, self._body())

        def _h_sanctions_state(self, actor, role, query):
            return 200, service.sanctions_state()

        def _h_review_queue(self, actor, role, query):
            status = query.get('status', ['pending'])[0] or None
            app_id = query.get('application_id', [None])[0]
            return 200, {'tasks': service.review_queue(status=status, app_id=app_id)}

        def _h_record_decision(self, actor, role, query, task):
            if role not in REVIEW_ROLES:
                raise Forbidden('review decisions require compliance role')
            body = self._body()
            updated = service.record_decision(task, actor, role,
                                              body.get('decision'), body.get('note'))
            return 200, {'task': updated}

        def _h_notifications(self, actor, role, query):
            app_id = query.get('application_id', [None])[0]
            return 200, {'notifications': service.list_notifications(app_id)}

    return Handler
