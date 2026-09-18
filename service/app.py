"""HTTP 接口层：路由、鉴权头解析、统一错误响应。

请求头：
- X-Actor: 操作人（复核表决、审计署名）
- X-Role:  角色（compliance / auditor / rm / viewer），决定脱敏与审计可见性
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from . import domain
from .domain import DomainError


def _q(query, key, default=None):
    values = query.get(key)
    return values[0] if values else default


# ---------------------------------------------------------------- 各端点处理

def h_health(store, actor, role, body, query):
    return 200, {"status": "ok"}


def h_create_partner(store, actor, role, body, query):
    with store.write() as conn:
        p = domain.create_partner(conn, actor, role, body)
        return 201, {"partner": p, "signing_state": p["signing_state"]}


def h_list_partners(store, actor, role, body, query):
    rows = store.read(
        "SELECT partner_id, legal_name, reg_number, country, status, subject_revision,"
        " signing_state, created_at, withdrawn_at FROM partners ORDER BY created_at")
    return 200, {"partners": [dict(r) for r in rows]}


def h_get_partner(store, actor, role, body, query, pid):
    return 200, domain.dossier_view(store.connect(), pid, role)


def h_update_identity(store, actor, role, body, query, pid):
    with store.write() as conn:
        p = domain.update_identity(conn, pid, actor, role, body)
        return 200, {"partner": p, "signing_state": p["signing_state"]}


def h_withdraw(store, actor, role, body, query, pid):
    with store.write() as conn:
        p = domain.withdraw(conn, pid, actor, role)
        return 200, {"partner": p, "signing_state": p["signing_state"]}


def h_verify(store, actor, role, body, query, pid):
    with store.write() as conn:
        domain.must_active(conn, pid)  # 撤回后停止新查询
        cid = domain.recompute(conn, pid, trigger="MANUAL", actor=actor, role=role)
        row = conn.execute("SELECT * FROM conclusions WHERE conclusion_id=?", (cid,)).fetchone()
        return 200, {"conclusion": domain.conclusion_view(conn, row, role)}


def h_submit_document(store, actor, role, body, query, pid):
    with store.write() as conn:
        doc, deduplicated = domain.submit_document(conn, pid, actor, role, body)
        active = conn.execute(
            "SELECT * FROM conclusions WHERE partner_id=? AND status='ACTIVE'", (pid,)).fetchone()
        payload = {
            "document": domain.apply_mask(doc, role, domain.DOC_MASK_FIELDS),
            "deduplicated": deduplicated,
            "signing_state": domain.must_partner(conn, pid)["signing_state"],
            "missing": json.loads(active["inputs_json"])["missing"] if active else [],
        }
        return (200 if deduplicated else 201), payload


def h_add_ubo(store, actor, role, body, query, pid):
    with store.write() as conn:
        u = domain.add_ubo(conn, pid, actor, role, body)
        return 201, {"ubo": domain.apply_mask(u, role, domain.UBO_MASK_FIELDS),
                     "signing_state": domain.must_partner(conn, pid)["signing_state"]}


def h_update_ubo(store, actor, role, body, query, pid, uid):
    with store.write() as conn:
        u = domain.update_ubo(conn, pid, uid, actor, role, body)
        return 200, {"ubo": domain.apply_mask(u, role, domain.UBO_MASK_FIELDS),
                     "signing_state": domain.must_partner(conn, pid)["signing_state"]}


def h_delete_ubo(store, actor, role, body, query, pid, uid):
    with store.write() as conn:
        domain.deactivate_ubo(conn, pid, uid, actor, role)
        return 200, {"deactivated": uid,
                     "signing_state": domain.must_partner(conn, pid)["signing_state"]}


def h_conclusions(store, actor, role, body, query, pid):
    conn = store.connect()
    domain.must_partner(conn, pid)
    rows = conn.execute(
        "SELECT * FROM conclusions WHERE partner_id=? ORDER BY computed_at, rowid", (pid,)).fetchall()
    return 200, {"conclusions": [domain.conclusion_view(conn, r, role) for r in rows]}


def h_signing(store, actor, role, body, query, pid):
    conn = store.connect()
    p = domain.must_partner(conn, pid)
    active = conn.execute(
        "SELECT conclusion_id, result, computed_at FROM conclusions"
        " WHERE partner_id=? AND status='ACTIVE'", (pid,)).fetchone()
    return 200, {
        "partner_id": pid,
        "partner_status": p["status"],
        "signing_state": p["signing_state"],
        "active_conclusion": dict(active) if active else None,
    }


def h_audit(store, actor, role, body, query, pid):
    return 200, {"audit": domain.audit_trail(store.connect(), pid, role)}


def h_regulatory_summary(store, actor, role, body, query, pid):
    return 200, domain.regulatory_summary(store.connect(), pid, role)


def h_apply_list(store, actor, role, body, query):
    with store.write() as conn:
        return 201, domain.apply_list_version(conn, actor, role, body)


def h_lists(store, actor, role, body, query):
    rows = store.read(
        "SELECT list_name, version, kind, effective_at, received_at, entries_json"
        " FROM sanction_lists ORDER BY list_name, received_at")
    lists = {}
    for r in rows:
        d = dict(r)
        d["entries"] = len(json.loads(d.pop("entries_json")))
        lists.setdefault(d.pop("list_name"), []).append(d)
    return 200, {"lists": [{"list_name": k, "versions": v} for k, v in lists.items()]}


def h_review_tasks(store, actor, role, body, query):
    conn = store.connect()
    status = _q(query, "status", "PENDING")
    pid = _q(query, "partner_id")
    sql = "SELECT task_id FROM review_tasks WHERE status=?"
    args = [status]
    if pid:
        sql += " AND partner_id=?"
        args.append(pid)
    sql += " ORDER BY created_at"
    tasks = [domain.review_task_view(conn, r["task_id"])
             for r in conn.execute(sql, args).fetchall()]
    return 200, {"review_tasks": tasks}


def h_decide_review(store, actor, role, body, query, tid):
    with store.write() as conn:
        task = domain.decide_review(conn, tid, actor, role,
                                    body.get("decision"), body.get("note"))
        p = domain.must_partner(conn, task["partner_id"])
        return 200, {"review_task": task, "signing_state": p["signing_state"]}


def h_notifications(store, actor, role, body, query):
    pid = _q(query, "partner_id")
    sql = "SELECT * FROM notifications"
    args = []
    if pid:
        sql += " WHERE partner_id=?"
        args.append(pid)
    sql += " ORDER BY created_at, rowid"
    out = []
    for r in store.read(sql, args):
        d = dict(r)
        d["payload"] = json.loads(d.pop("payload_json"))
        out.append(d)
    return 200, {"notifications": out}


ROUTES = [
    ("GET", r"/health", h_health),
    ("POST", r"/partners", h_create_partner),
    ("GET", r"/partners", h_list_partners),
    ("GET", r"/partners/(?P<pid>[^/]+)", h_get_partner),
    ("PATCH", r"/partners/(?P<pid>[^/]+)/identity", h_update_identity),
    ("POST", r"/partners/(?P<pid>[^/]+)/withdraw", h_withdraw),
    ("POST", r"/partners/(?P<pid>[^/]+)/verify", h_verify),
    ("POST", r"/partners/(?P<pid>[^/]+)/documents", h_submit_document),
    ("POST", r"/partners/(?P<pid>[^/]+)/ubos", h_add_ubo),
    ("PATCH", r"/partners/(?P<pid>[^/]+)/ubos/(?P<uid>[^/]+)", h_update_ubo),
    ("DELETE", r"/partners/(?P<pid>[^/]+)/ubos/(?P<uid>[^/]+)", h_delete_ubo),
    ("GET", r"/partners/(?P<pid>[^/]+)/conclusions", h_conclusions),
    ("GET", r"/partners/(?P<pid>[^/]+)/signing", h_signing),
    ("GET", r"/partners/(?P<pid>[^/]+)/audit", h_audit),
    ("GET", r"/partners/(?P<pid>[^/]+)/regulatory-summary", h_regulatory_summary),
    ("POST", r"/sanction-lists", h_apply_list),
    ("GET", r"/sanction-lists", h_lists),
    ("GET", r"/review-tasks", h_review_tasks),
    ("POST", r"/review-tasks/(?P<tid>[^/]+)/decisions", h_decide_review),
    ("GET", r"/notifications", h_notifications),
]


def route(store, method, path, query, body, actor, role):
    for m, pattern, fn in ROUTES:
        if m != method:
            continue
        mo = re.fullmatch(pattern, path)
        if mo:
            return fn(store, actor, role, body, query, **mo.groupdict())
    return 404, {"error": "NOT_FOUND", "message": f"{method} {path}"}


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _handle(self, method):
            try:
                parsed = urlparse(self.path)
                body = {}
                if method in ("POST", "PATCH", "PUT", "DELETE"):
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length else b""
                    if raw:
                        try:
                            body = json.loads(raw)
                        except json.JSONDecodeError:
                            raise DomainError("请求体不是合法 JSON", code="BAD_JSON")
                actor = self.headers.get("X-Actor", "anonymous")
                role = self.headers.get("X-Role", "viewer")
                status, payload = route(store, method, parsed.path,
                                        parse_qs(parsed.query), body, actor, role)
            except DomainError as e:
                status = e.status
                payload = {"error": e.code, "message": e.message, **e.extra}
            except Exception as e:  # pragma: no cover - 兜底
                status = 500
                payload = {"error": "INTERNAL", "message": str(e)}
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = lambda self: self._handle("GET")
        do_POST = lambda self: self._handle("POST")
        do_PATCH = lambda self: self._handle("PATCH")
        do_DELETE = lambda self: self._handle("DELETE")

        def log_message(self, *_):
            pass

    return Handler
