"""海外合作方尽调档案的领域逻辑。

核心不变量：
- 结论绑定主体版本（subject_revision）：主体身份字段（法定名称/注册号/国家）一旦变化，
  旧结论立即作废，不套用到新主体；证件与受益所有人也按主体版本归属，新主体需重新收件。
- 同一证件（合作方+主体版本+类型+号码）重复发送幂等；内容变化产生新版本并触发重算。
- 命中制裁名单即暂停签约，只能由双人复核（两名不同复核人先后同意）解除。
- 外部名单迟到或更正时，标注受影响的历史结论（conclusion_impacts）。
- 撤回合作申请后停止一切新查询（重算直接跳过），但保留监管所需的档案摘要。
- 所有状态变化写入审计；通知按去重键落库，同一事件不重复推送。
"""
from __future__ import annotations

import json

from .util import entry_key, new_id, normalize_name, normalize_reg, now_iso, parse_ts, sha256_text


class DomainError(Exception):
    status = 400
    code = "BAD_REQUEST"

    def __init__(self, message, code=None, **extra):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.extra = extra


class NotFound(DomainError):
    status = 404
    code = "NOT_FOUND"


class Conflict(DomainError):
    status = 409
    code = "CONFLICT"


class Forbidden(DomainError):
    status = 403
    code = "FORBIDDEN"


IDENTITY_FIELDS = ("legal_name", "reg_number", "country")
CONTACT_FIELDS = ("contact_name", "contact_email", "contact_phone")
REQUIRED_DOC_TYPES = ("REGISTRY_EXTRACT", "OWNERSHIP_CHART")

# 角色与脱敏策略：合规与审计可见明文，其余角色（含客户经理 rm）敏感字段脱敏
UNMASKED_ROLES = {"compliance", "auditor"}
AUDIT_ROLES = {"compliance", "auditor"}
REVIEWER_ROLES = {"compliance"}

PARTNER_MASK_FIELDS = ("contact_email", "contact_phone")
UBO_MASK_FIELDS = ("id_number", "dob")
DOC_MASK_FIELDS = ("doc_number",)


# ---------------------------------------------------------------- 脱敏

def mask_text(value, keep=4) -> str:
    s = str(value)
    if len(s) <= keep:
        return "****"
    return "*" * (len(s) - keep) + s[-keep:]


def mask_email(value: str) -> str:
    if "@" in value:
        local, domain = value.split("@", 1)
        return (local[:1] or "*") + "***@" + domain
    return mask_text(value)


def mask_dob(value: str) -> str:
    parts = str(value).split("-")
    if len(parts) == 3:
        return f"{parts[0]}-**-**"
    return "****"


def mask_value(field: str, value):
    if field == "contact_email":
        return mask_email(value)
    if field == "dob":
        return mask_dob(value)
    return mask_text(value)


def apply_mask(obj: dict, role: str, fields) -> dict:
    if not obj or role in UNMASKED_ROLES:
        return obj
    masked = dict(obj)
    for f in fields:
        if masked.get(f):
            masked[f] = mask_value(f, masked[f])
    return masked


# ---------------------------------------------------------------- 基础

def _row_dict(row):
    return dict(row) if row is not None else None


def audit(conn, actor, role, action, entity_type, entity_id, partner_id=None, detail=None):
    conn.execute(
        "INSERT INTO audit_events(ts, actor, role, action, entity_type, entity_id, partner_id, detail_json)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (now_iso(), actor, role, action, entity_type, entity_id, partner_id,
         json.dumps(detail or {}, ensure_ascii=False)),
    )


def notify(conn, ntype, dedup_key, partner_id, payload) -> bool:
    """写通知，dedup_key 唯一约束保证同一事件只通知一次。返回是否真正产生新通知。"""
    cur = conn.execute(
        "INSERT OR IGNORE INTO notifications(notification_id, dedup_key, type, partner_id, payload_json, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (new_id("N-"), dedup_key, ntype, partner_id,
         json.dumps(payload, ensure_ascii=False), now_iso()),
    )
    return cur.rowcount > 0


def must_partner(conn, partner_id) -> dict:
    row = conn.execute("SELECT * FROM partners WHERE partner_id=?", (partner_id,)).fetchone()
    if row is None:
        raise NotFound(f"合作方不存在: {partner_id}", code="PARTNER_NOT_FOUND")
    return dict(row)


def must_active(conn, partner_id) -> dict:
    p = must_partner(conn, partner_id)
    if p["status"] == "WITHDRAWN":
        raise Conflict("合作申请已撤回，停止接收新数据与新查询", code="PARTNER_WITHDRAWN")
    return p


def cancel_pending_tasks(conn, hit_id, actor, role, reason):
    """命中失效（名单更正/主体变更）时，级联取消其在途复核任务，避免队列出现无对象任务。"""
    for t in conn.execute(
            "SELECT * FROM review_tasks WHERE hit_id=? AND status='PENDING'", (hit_id,)).fetchall():
        conn.execute("UPDATE review_tasks SET status='CANCELLED', decided_at=? WHERE task_id=?",
                     (now_iso(), t["task_id"]))
        audit(conn, actor, role, "review.task_cancelled", "review_task", t["task_id"],
              t["partner_id"], {"hit_id": hit_id, "reason": reason})
        notify(conn, "review_task_cancelled", f"task:{t['task_id']}:cancelled", t["partner_id"],
               {"task_id": t["task_id"], "reason": reason})


# ---------------------------------------------------------------- 名单与匹配

def current_lists(conn) -> list[dict]:
    """每个名单取最新到达的版本作为当前生效内容。"""
    rows = conn.execute(
        """
        SELECT sl.* FROM sanction_lists sl
        JOIN (SELECT list_name, MAX(rowid) AS m FROM sanction_lists GROUP BY list_name) t
          ON t.list_name = sl.list_name AND t.m = sl.rowid
        """
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["entries"] = json.loads(d.pop("entries_json"))
        out.append(d)
    return out


def match_subject(name, id_number, entry) -> bool:
    """匹配规则：证件号一致（双方都有时）或规范化姓名一致。"""
    eid = normalize_reg(entry.get("id_number"))
    if id_number and eid and normalize_reg(id_number) == eid:
        return True
    ename = normalize_name(entry.get("name"))
    return bool(ename) and normalize_name(name) == ename


def compute_missing(docs, ubos) -> list[str]:
    """分阶段收件的缺口：注册摘录、股权结构图必备；至少一名受益人且每人有身份证件。"""
    types = {d["doc_type"] for d in docs}
    missing = [t for t in REQUIRED_DOC_TYPES if t not in types]
    if not ubos:
        missing.append("UBO")
    else:
        for u in ubos:
            if not any(
                d["doc_type"] == "UBO_ID"
                and normalize_name(d.get("holder_name")) == normalize_name(u["full_name"])
                for d in docs
            ):
                missing.append(f"UBO_ID:{u['full_name']}")
    return missing


# ---------------------------------------------------------------- 重算（核心）

def recompute(conn, partner_id, trigger, actor="system", role="system"):
    """按当前主体版本重算核验结论与签约状态。

    - 撤回的合作方直接跳过（停止新查询）；
    - 输入（证件摘要、受益人、名单版本、命中）未变化时不产生新结论（幂等）；
    - 命中名单 -> PAUSED；资料不齐 -> BLOCKED；其余 -> READY。
    """
    p = must_partner(conn, partner_id)
    if p["status"] == "WITHDRAWN":
        return None
    rev = p["subject_revision"]
    docs = [dict(r) for r in conn.execute(
        "SELECT * FROM documents WHERE partner_id=? AND subject_revision=?", (partner_id, rev))]
    ubos = [dict(r) for r in conn.execute(
        "SELECT * FROM ubos WHERE partner_id=? AND subject_revision=? AND active=1", (partner_id, rev))]

    lists = current_lists(conn)
    entries = [(l["list_name"], l["version"], e) for l in lists for e in l["entries"]]

    subjects = [("PARTNER", p["legal_name"], None)]
    subjects += [("UBO", u["full_name"], u["id_number"]) for u in ubos]

    # 当前主体与当前名单的匹配结果（按条目指纹去重）
    matched, seen = [], set()
    for kind, name, idn in subjects:
        for list_name, lver, e in entries:
            ek = entry_key(list_name, e)
            if ek in seen:
                continue
            if match_subject(name, idn, e):
                seen.add(ek)
                matched.append((ek, kind, name, list_name, lver, e))
    matched_keys = {m[0] for m in matched}

    existing_active = {r["entry_key"]: dict(r) for r in conn.execute(
        "SELECT * FROM risk_hits WHERE partner_id=? AND status='ACTIVE'", (partner_id,))}
    released_keys = {r["entry_key"] for r in conn.execute(
        "SELECT entry_key FROM risk_hits WHERE partner_id=? AND status='RELEASED'", (partner_id,))}

    # 清理：不再匹配当前主体/当前名单的命中（名单更正移除、受益人变更等）
    for ek, hit in existing_active.items():
        if ek not in matched_keys:
            conn.execute(
                "UPDATE risk_hits SET status='INVALIDATED_BY_CORRECTION', resolved_at=? WHERE hit_id=?",
                (now_iso(), hit["hit_id"]))
            audit(conn, actor, role, "hit.invalidated", "hit", hit["hit_id"], partner_id,
                  {"entry_key": ek, "reason": "不再匹配当前主体或当前名单"})
            cancel_pending_tasks(conn, hit["hit_id"], actor, role, "命中已失效")

    active_keys, new_hits = [], []
    for ek, kind, name, list_name, lver, e in matched:
        if ek in released_keys:
            continue  # 该条目已经双人复核放行，不再阻断
        active_keys.append(ek)
        if ek not in existing_active:
            new_hits.append((ek, kind, name, list_name, lver, e))

    missing = compute_missing(docs, ubos)
    result = "HIT" if active_keys else ("CLEAR" if not missing else "INCOMPLETE")
    signing = "PAUSED" if active_keys else ("READY" if result == "CLEAR" else "BLOCKED")

    inputs = {
        "subject_revision": rev,
        "subjects": [{"kind": k, "name": n, "id_number": i} for k, n, i in subjects],
        "documents": [
            {"doc_id": d["doc_id"], "doc_type": d["doc_type"], "doc_number": d["doc_number"],
             "content_hash": d["content_hash"], "version": d["version"], "stage": d["stage"]}
            for d in docs
        ],
        "ubos": [
            {"ubo_id": u["ubo_id"], "full_name": u["full_name"], "ownership_pct": u["ownership_pct"]}
            for u in ubos
        ],
        "lists": [{"list_name": l["list_name"], "version": l["version"]} for l in lists],
        "missing": missing,
        "active_hit_keys": sorted(active_keys),
        "released_hit_keys": sorted(released_keys),
    }
    inputs_str = json.dumps(inputs, ensure_ascii=False, sort_keys=True)

    prev = conn.execute(
        "SELECT * FROM conclusions WHERE partner_id=? AND status='ACTIVE'", (partner_id,)).fetchone()
    if prev and prev["result"] == result and prev["inputs_json"] == inputs_str:
        return prev["conclusion_id"]  # 输入与结论均未变化：幂等，不产生新结论

    if prev:
        conn.execute("UPDATE conclusions SET status='SUPERSEDED' WHERE conclusion_id=?",
                     (prev["conclusion_id"],))
    cid = new_id("C-")
    conn.execute(
        "INSERT INTO conclusions(conclusion_id, partner_id, subject_revision, computed_at,"
        " trigger, result, status, signing_state, inputs_json) VALUES(?,?,?,?,?,?,?,?,?)",
        (cid, partner_id, rev, now_iso(), trigger, result, "ACTIVE", signing, inputs_str))

    for ek, kind, name, list_name, lver, e in new_hits:
        hid = new_id("H-")
        conn.execute(
            "INSERT INTO risk_hits(hit_id, partner_id, conclusion_id, subject_kind, subject_name,"
            " list_name, list_version, entry_key, entry_json, status, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (hid, partner_id, cid, kind, name, list_name, lver, ek,
             json.dumps(e, ensure_ascii=False), "ACTIVE", now_iso()))
        audit(conn, actor, role, "hit.created", "hit", hid, partner_id,
              {"subject": name, "list": f"{list_name}@{lver}", "entry_key": ek})
        task_id = new_id("T-")
        conn.execute(
            "INSERT INTO review_tasks(task_id, partner_id, type, status, required_approvals, hit_id, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (task_id, partner_id, "SANCTIONS_RELEASE", "PENDING", 2, hid, now_iso()))
        audit(conn, actor, role, "review.task_created", "review_task", task_id, partner_id,
              {"hit_id": hid, "required_approvals": 2})
        notify(conn, "review_task_created", f"task:{task_id}", partner_id,
               {"task_id": task_id, "hit_id": hid, "subject": name})
        notify(conn, "sanctions_hit", f"hit:{partner_id}:{ek}", partner_id,
               {"hit_id": hid, "subject": name, "list": f"{list_name}@{lver}"})

    audit(conn, actor, role, "conclusion.computed", "conclusion", cid, partner_id,
          {"trigger": trigger, "result": result, "signing": signing,
           "supersedes": prev["conclusion_id"] if prev else None})

    if p["signing_state"] != signing:
        conn.execute("UPDATE partners SET signing_state=? WHERE partner_id=?", (signing, partner_id))
        audit(conn, actor, role, "signing.changed", "partner", partner_id, partner_id,
              {"from": p["signing_state"], "to": signing})
        notify(conn, "signing_state_changed", f"signing:{partner_id}:{cid}:{signing}", partner_id,
               {"from": p["signing_state"], "to": signing, "conclusion_id": cid})
    return cid


# ---------------------------------------------------------------- 合作方与主体

def create_partner(conn, actor, role, data) -> dict:
    for f in ("legal_name", "reg_number", "country"):
        if not str(data.get(f) or "").strip():
            raise DomainError(f"缺少必填字段: {f}", code="MISSING_FIELD", field=f)
    pid = new_id("P-")
    ts = now_iso()
    conn.execute(
        "INSERT INTO partners(partner_id, legal_name, reg_number, country, contact_name,"
        " contact_email, contact_phone, status, subject_revision, signing_state, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (pid, data["legal_name"].strip(), data["reg_number"].strip(), data["country"].strip(),
         data.get("contact_name"), data.get("contact_email"), data.get("contact_phone"),
         "ACTIVE", 1, "BLOCKED", ts))
    conn.execute(
        "INSERT INTO subject_revisions(partner_id, revision, legal_name, reg_number, country,"
        " reason, changed_by, changed_at) VALUES(?,?,?,?,?,?,?,?)",
        (pid, 1, data["legal_name"].strip(), data["reg_number"].strip(), data["country"].strip(),
         "建档", actor, ts))
    audit(conn, actor, role, "partner.created", "partner", pid, pid,
          {"legal_name": data["legal_name"], "reg_number": data["reg_number"]})
    recompute(conn, pid, trigger="PARTNER_CREATED", actor=actor, role=role)
    return must_partner(conn, pid)


def update_identity(conn, partner_id, actor, role, data) -> dict:
    """更新主体信息。

    身份字段（法定名称/注册号/国家）变化 -> 主体版本+1，旧结论作废，重新收件核验；
    仅联系人变化 -> 原地更新并审计（不影响核验结论）。
    """
    p = must_active(conn, partner_id)
    identity_changed = any(
        f in data and normalize_reg(data[f]) != normalize_reg(p[f]) for f in IDENTITY_FIELDS)
    if identity_changed:
        new_rev = p["subject_revision"] + 1
        values = {f: str(data.get(f) or p[f]).strip() for f in IDENTITY_FIELDS}
        contacts = {f: data.get(f, p[f]) for f in CONTACT_FIELDS}
        conn.execute(
            "UPDATE partners SET legal_name=?, reg_number=?, country=?, contact_name=?,"
            " contact_email=?, contact_phone=?, subject_revision=? WHERE partner_id=?",
            (values["legal_name"], values["reg_number"], values["country"],
             contacts["contact_name"], contacts["contact_email"], contacts["contact_phone"],
             new_rev, partner_id))
        conn.execute(
            "INSERT INTO subject_revisions(partner_id, revision, legal_name, reg_number, country,"
            " reason, changed_by, changed_at) VALUES(?,?,?,?,?,?,?,?)",
            (partner_id, new_rev, values["legal_name"], values["reg_number"], values["country"],
             data.get("reason"), actor, now_iso()))
        audit(conn, actor, role, "identity.changed", "partner", partner_id, partner_id,
              {"from_revision": p["subject_revision"], "to_revision": new_rev,
               "old": {f: p[f] for f in IDENTITY_FIELDS}, "new": values,
               "reason": data.get("reason")})
        # 新主体版本：旧证件/受益人不带入，结论重算为 INCOMPLETE
        recompute(conn, partner_id, trigger="IDENTITY_CHANGE", actor=actor, role=role)
    else:
        changed = {f: data[f] for f in CONTACT_FIELDS if f in data and data[f] != p[f]}
        if changed:
            conn.execute(
                "UPDATE partners SET contact_name=?, contact_email=?, contact_phone=? WHERE partner_id=?",
                (data.get("contact_name", p["contact_name"]),
                 data.get("contact_email", p["contact_email"]),
                 data.get("contact_phone", p["contact_phone"]), partner_id))
            audit(conn, actor, role, "contact.updated", "partner", partner_id, partner_id,
                  {"changed": changed})
    return must_partner(conn, partner_id)


def withdraw(conn, partner_id, actor, role) -> dict:
    """撤回合作申请：停止新查询与新收件，保留监管摘要；待复核任务保留在队列中。"""
    p = must_partner(conn, partner_id)
    if p["status"] == "WITHDRAWN":
        raise Conflict("合作申请已撤回", code="ALREADY_WITHDRAWN")
    ts = now_iso()
    conn.execute(
        "UPDATE partners SET status='WITHDRAWN', withdrawn_at=?, signing_state='WITHDRAWN'"
        " WHERE partner_id=?", (ts, partner_id))
    audit(conn, actor, role, "partner.withdrawn", "partner", partner_id, partner_id,
          {"withdrawn_at": ts})
    notify(conn, "partner_withdrawn", f"withdrawn:{partner_id}", partner_id, {"withdrawn_at": ts})
    return must_partner(conn, partner_id)


# ---------------------------------------------------------------- 证件收件

def submit_document(conn, partner_id, actor, role, data):
    """分阶段收件。同一证件（主体版本+类型+号码）重复发送幂等；内容变化升级版本并重算。"""
    p = must_active(conn, partner_id)
    doc_type = str(data.get("doc_type") or "").strip().upper()
    doc_number = str(data.get("doc_number") or "").strip()
    if not doc_type or not doc_number:
        raise DomainError("缺少必填字段: doc_type / doc_number", code="MISSING_FIELD")
    content_hash = data.get("content_hash")
    if not content_hash:
        content = data.get("content")
        if content is None:
            raise DomainError("缺少证件内容: content 或 content_hash", code="MISSING_FIELD")
        content_hash = sha256_text(str(content))
    subj_reg = data.get("subject_reg_number")
    if subj_reg and normalize_reg(subj_reg) != normalize_reg(p["reg_number"]):
        # 证件所属主体与当前主体不一致：拒绝，防止旧主体材料串到新主体
        raise Conflict("证件所属主体与当前合作方主体不一致，已拒绝",
                       code="SUBJECT_MISMATCH",
                       expected=p["reg_number"], received=subj_reg)
    rev = p["subject_revision"]
    existing = conn.execute(
        "SELECT * FROM documents WHERE partner_id=? AND subject_revision=? AND doc_type=? AND doc_number=?",
        (partner_id, rev, doc_type, doc_number)).fetchone()
    if existing and existing["content_hash"] == content_hash:
        return dict(existing), True  # 幂等：同一证件重复发送，不产生新状态

    ts = now_iso()
    occurred_at = parse_ts(data.get("occurred_at"))
    channel = "offline" if data.get("channel") == "offline" or occurred_at else "online"
    if existing:
        conn.execute(
            "UPDATE documents SET content_hash=?, version=version+1, stage=?, holder_name=?,"
            " subject_reg_number=?, channel=?, occurred_at=?, received_at=? WHERE doc_id=?",
            (content_hash, int(data.get("stage") or existing["stage"]),
             data.get("holder_name", existing["holder_name"]), subj_reg or existing["subject_reg_number"],
             channel, occurred_at, ts, existing["doc_id"]))
        doc_id = existing["doc_id"]
        audit(conn, actor, role, "document.versioned", "document", doc_id, partner_id,
              {"doc_type": doc_type, "doc_number": doc_number, "content_hash": content_hash,
               "channel": channel, "occurred_at": occurred_at})
    else:
        doc_id = new_id("D-")
        conn.execute(
            "INSERT INTO documents(doc_id, partner_id, subject_revision, doc_type, doc_number,"
            " content_hash, stage, holder_name, subject_reg_number, channel, occurred_at, received_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, partner_id, rev, doc_type, doc_number, content_hash,
             int(data.get("stage") or 1), data.get("holder_name"), subj_reg,
             channel, occurred_at, ts))
        audit(conn, actor, role, "document.received", "document", doc_id, partner_id,
              {"doc_type": doc_type, "doc_number": doc_number, "content_hash": content_hash,
               "stage": int(data.get("stage") or 1), "channel": channel, "occurred_at": occurred_at})
    recompute(conn, partner_id, trigger="DOC_INTAKE", actor=actor, role=role)
    doc = conn.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
    return dict(doc), False


# ---------------------------------------------------------------- 受益所有人

def add_ubo(conn, partner_id, actor, role, data) -> dict:
    p = must_active(conn, partner_id)
    if not str(data.get("full_name") or "").strip() or not str(data.get("id_number") or "").strip():
        raise DomainError("缺少必填字段: full_name / id_number", code="MISSING_FIELD")
    uid = new_id("U-")
    ts = now_iso()
    conn.execute(
        "INSERT INTO ubos(ubo_id, partner_id, subject_revision, full_name, id_number, dob,"
        " ownership_pct, role, active, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,1,?,?)",
        (uid, partner_id, p["subject_revision"], data["full_name"].strip(), data["id_number"].strip(),
         data.get("dob"), data.get("ownership_pct"), data.get("role"), ts, ts))
    audit(conn, actor, role, "ubo.added", "ubo", uid, partner_id,
          {"full_name": data["full_name"], "ownership_pct": data.get("ownership_pct")})
    recompute(conn, partner_id, trigger="UBO_CHANGE", actor=actor, role=role)
    return dict(conn.execute("SELECT * FROM ubos WHERE ubo_id=?", (uid,)).fetchone())


def update_ubo(conn, partner_id, ubo_id, actor, role, data) -> dict:
    must_active(conn, partner_id)
    row = conn.execute(
        "SELECT * FROM ubos WHERE ubo_id=? AND partner_id=? AND active=1", (ubo_id, partner_id)).fetchone()
    if row is None:
        raise NotFound(f"受益所有人不存在: {ubo_id}", code="UBO_NOT_FOUND")
    u = dict(row)
    fields = ("full_name", "id_number", "dob", "ownership_pct", "role")
    new = {f: data.get(f, u[f]) for f in fields}
    changed = {f: {"old": u[f], "new": new[f]} for f in fields if new[f] != u[f]}
    if changed:
        conn.execute(
            "UPDATE ubos SET full_name=?, id_number=?, dob=?, ownership_pct=?, role=?, updated_at=?"
            " WHERE ubo_id=?",
            (new["full_name"], new["id_number"], new["dob"], new["ownership_pct"], new["role"],
             now_iso(), ubo_id))
        audit(conn, actor, role, "ubo.updated", "ubo", ubo_id, partner_id, {"changed": changed})
        recompute(conn, partner_id, trigger="UBO_CHANGE", actor=actor, role=role)
    return dict(conn.execute("SELECT * FROM ubos WHERE ubo_id=?", (ubo_id,)).fetchone())


def deactivate_ubo(conn, partner_id, ubo_id, actor, role):
    must_active(conn, partner_id)
    row = conn.execute(
        "SELECT * FROM ubos WHERE ubo_id=? AND partner_id=? AND active=1", (ubo_id, partner_id)).fetchone()
    if row is None:
        raise NotFound(f"受益所有人不存在: {ubo_id}", code="UBO_NOT_FOUND")
    conn.execute("UPDATE ubos SET active=0, updated_at=? WHERE ubo_id=?", (now_iso(), ubo_id))
    audit(conn, actor, role, "ubo.deactivated", "ubo", ubo_id, partner_id,
          {"full_name": row["full_name"]})
    recompute(conn, partner_id, trigger="UBO_CHANGE", actor=actor, role=role)


# ---------------------------------------------------------------- 外部名单

def apply_list_version(conn, actor, role, data) -> dict:
    """应用名单新版本（全量或更正）。

    - 移除的条目：相关命中置为 INVALIDATED_BY_CORRECTION，并标注受影响的历史结论；
    - 新增的条目：对生效时间之后作出的 CLEAR 历史结论标注 LATE_LIST_HIT（名单迟到）；
    - 所有在办合作方按最新名单重算（撤回方不再查询）。
    """
    list_name = str(data.get("list_name") or "").strip()
    version = str(data.get("version") or "").strip()
    entries = data.get("entries")
    effective_at = parse_ts(data.get("effective_at"))
    if not list_name or not version or not isinstance(entries, list) or not effective_at:
        raise DomainError("缺少必填字段: list_name / version / effective_at / entries",
                          code="MISSING_FIELD")
    kind = "CORRECTION" if data.get("kind") == "CORRECTION" else "FULL"
    if conn.execute("SELECT 1 FROM sanction_lists WHERE list_name=? AND version=?",
                    (list_name, version)).fetchone():
        raise Conflict(f"名单版本已存在: {list_name}@{version}", code="LIST_VERSION_EXISTS")

    prev = conn.execute(
        "SELECT * FROM sanction_lists WHERE list_name=? ORDER BY rowid DESC LIMIT 1",
        (list_name,)).fetchone()
    prev_entries = json.loads(prev["entries_json"]) if prev else []
    old_keys = {entry_key(list_name, e) for e in prev_entries}
    new_keys = {entry_key(list_name, e) for e in entries}
    added = [e for e in entries if entry_key(list_name, e) not in old_keys]
    removed_keys = old_keys - new_keys

    received_at = now_iso()
    conn.execute(
        "INSERT INTO sanction_lists(list_name, version, kind, effective_at, received_at,"
        " corrects_version, entries_json) VALUES(?,?,?,?,?,?,?)",
        (list_name, version, kind, effective_at, received_at, data.get("corrects_version"),
         json.dumps(entries, ensure_ascii=False)))
    audit(conn, actor, role, "list.applied", "sanction_list", f"{list_name}@{version}", None,
          {"kind": kind, "entries": len(entries), "effective_at": effective_at,
           "received_late": effective_at < received_at,
           "added": len(added), "removed": len(removed_keys)})

    impacts = []

    def mark_impact(conclusion_id, pid, impact_type, detail):
        cur = conn.execute(
            "INSERT OR IGNORE INTO conclusion_impacts(impact_id, conclusion_id, partner_id,"
            " list_name, list_version, impact_type, detail, created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (new_id("I-"), conclusion_id, pid, list_name, version, impact_type, detail, now_iso()))
        if cur.rowcount:
            impacts.append({"conclusion_id": conclusion_id, "partner_id": pid, "type": impact_type})
            audit(conn, actor, role, "conclusion.impacted", "conclusion", conclusion_id, pid,
                  {"impact_type": impact_type, "list": f"{list_name}@{version}", "detail": detail})
            notify(conn, "conclusion_affected", f"impact:{conclusion_id}:{list_name}:{version}:{impact_type}",
                   pid, {"conclusion_id": conclusion_id, "impact_type": impact_type,
                         "list": f"{list_name}@{version}"})

    # 1) 更正移除条目：命中失效 + 标注所有包含该条目的历史结论（含已撤回方的档案记录）
    if removed_keys:
        placeholders = ",".join("?" for _ in removed_keys)
        hits = conn.execute(
            f"SELECT * FROM risk_hits WHERE list_name=? AND entry_key IN ({placeholders})",
            (list_name, *removed_keys)).fetchall()
        for h in hits:
            if h["status"] == "ACTIVE":
                conn.execute(
                    "UPDATE risk_hits SET status='INVALIDATED_BY_CORRECTION', resolved_at=?"
                    " WHERE hit_id=?", (now_iso(), h["hit_id"]))
                audit(conn, actor, role, "hit.invalidated", "hit", h["hit_id"], h["partner_id"],
                      {"entry_key": h["entry_key"], "reason": f"名单更正 {list_name}@{version} 移除条目"})
                cancel_pending_tasks(conn, h["hit_id"], actor, role,
                                     f"名单更正 {list_name}@{version} 移除条目")
            if h["conclusion_id"]:
                mark_impact(h["conclusion_id"], h["partner_id"], "CORRECTION_REMOVED_HIT",
                            f"条目 {h['entry_key']} 被名单更正移除")

    # 2) 新增条目：对在办合作方标注迟到的历史结论并重算
    for p in conn.execute("SELECT * FROM partners WHERE status='ACTIVE'").fetchall():
        pid = p["partner_id"]
        if added:
            rows = conn.execute(
                "SELECT * FROM conclusions WHERE partner_id=? AND result='CLEAR' AND computed_at>=?",
                (pid, effective_at)).fetchall()
            for c in rows:
                subjects = json.loads(c["inputs_json"]).get("subjects", [])
                if any(match_subject(s.get("name"), s.get("id_number"), e)
                       for s in subjects for e in added):
                    mark_impact(c["conclusion_id"], pid, "LATE_LIST_HIT",
                                f"名单 {list_name}@{version} 生效于 {effective_at}，该结论作出时条目已生效")
        recompute(conn, pid, trigger="CORRECTION" if kind == "CORRECTION" else "LIST_UPDATE",
                  actor=actor, role=role)

    return {"list_name": list_name, "version": version, "kind": kind,
            "added": len(added), "removed": len(removed_keys), "impacts": impacts}


# ---------------------------------------------------------------- 复核

def decide_review(conn, task_id, actor, role, decision, note=None) -> dict:
    """复核表决。制裁命中解除须双人复核：两名不同合规角色先后 APPROVE。"""
    if role not in REVIEWER_ROLES:
        raise Forbidden("仅合规角色可执行复核", code="REVIEWER_ROLE_REQUIRED")
    row = conn.execute("SELECT * FROM review_tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        raise NotFound(f"复核任务不存在: {task_id}", code="TASK_NOT_FOUND")
    task = dict(row)
    if task["status"] != "PENDING":
        raise Conflict("复核任务已关闭", code="TASK_CLOSED")
    if decision not in ("APPROVE", "REJECT"):
        raise DomainError("decision 必须为 APPROVE 或 REJECT", code="BAD_DECISION")
    if conn.execute("SELECT 1 FROM review_approvals WHERE task_id=? AND actor=?",
                    (task_id, actor)).fetchone():
        raise Conflict("同一复核人不可重复表决", code="DUPLICATE_DECISION")

    conn.execute(
        "INSERT INTO review_approvals(task_id, actor, decision, note, decided_at) VALUES(?,?,?,?,?)",
        (task_id, actor, decision, note, now_iso()))
    audit(conn, actor, role, "review.decision", "review_task", task_id, task["partner_id"],
          {"decision": decision, "note": note})

    if decision == "REJECT":
        conn.execute("UPDATE review_tasks SET status='REJECTED', decided_at=? WHERE task_id=?",
                     (now_iso(), task_id))
        audit(conn, actor, role, "review.rejected", "review_task", task_id, task["partner_id"], {})
        notify(conn, "review_task_decided", f"task:{task_id}:decided", task["partner_id"],
               {"task_id": task_id, "status": "REJECTED"})
    else:
        approvals = conn.execute(
            "SELECT COUNT(*) AS n FROM review_approvals WHERE task_id=? AND decision='APPROVE'",
            (task_id,)).fetchone()["n"]
        if approvals >= task["required_approvals"]:
            conn.execute("UPDATE review_tasks SET status='APPROVED', decided_at=? WHERE task_id=?",
                         (now_iso(), task_id))
            if task["hit_id"]:
                conn.execute("UPDATE risk_hits SET status='RELEASED', resolved_at=? WHERE hit_id=?",
                             (now_iso(), task["hit_id"]))
                audit(conn, actor, role, "hit.released", "hit", task["hit_id"], task["partner_id"],
                      {"task_id": task_id, "approvals": approvals})
            audit(conn, actor, role, "review.approved", "review_task", task_id, task["partner_id"],
                  {"approvals": approvals})
            notify(conn, "review_task_decided", f"task:{task_id}:decided", task["partner_id"],
                   {"task_id": task_id, "status": "APPROVED"})
            recompute(conn, task["partner_id"], trigger="REVIEW_RELEASE", actor=actor, role=role)
    return review_task_view(conn, task_id)


def review_task_view(conn, task_id) -> dict:
    task = dict(conn.execute("SELECT * FROM review_tasks WHERE task_id=?", (task_id,)).fetchone())
    task["approvals"] = [dict(r) for r in conn.execute(
        "SELECT actor, decision, note, decided_at FROM review_approvals WHERE task_id=? ORDER BY decided_at",
        (task_id,))]
    return task


# ---------------------------------------------------------------- 查询视图

def conclusion_view(conn, row, role) -> dict:
    c = dict(row)
    c["inputs"] = json.loads(c.pop("inputs_json"))
    c["impacts"] = [dict(r) for r in conn.execute(
        "SELECT impact_id, list_name, list_version, impact_type, detail, created_at"
        " FROM conclusion_impacts WHERE conclusion_id=? ORDER BY created_at",
        (c["conclusion_id"],))]
    if role not in UNMASKED_ROLES:
        for s in c["inputs"].get("subjects", []):
            if s.get("id_number"):
                s["id_number"] = mask_text(s["id_number"])
        for d in c["inputs"].get("documents", []):
            if d.get("doc_number"):
                d["doc_number"] = mask_text(d["doc_number"])
    return c


def dossier_view(conn, partner_id, role) -> dict:
    p = apply_mask(must_partner(conn, partner_id), role, PARTNER_MASK_FIELDS)
    rev = p["subject_revision"]
    docs = [apply_mask(d, role, DOC_MASK_FIELDS) for d in (dict(r) for r in conn.execute(
        "SELECT * FROM documents WHERE partner_id=? AND subject_revision=? ORDER BY received_at",
        (partner_id, rev)))]
    ubos = [apply_mask(u, role, UBO_MASK_FIELDS) for u in (dict(r) for r in conn.execute(
        "SELECT * FROM ubos WHERE partner_id=? AND subject_revision=? AND active=1 ORDER BY created_at",
        (partner_id, rev)))]
    active = conn.execute(
        "SELECT * FROM conclusions WHERE partner_id=? AND status='ACTIVE'", (partner_id,)).fetchone()
    hits = [dict(r) for r in conn.execute(
        "SELECT hit_id, subject_kind, subject_name, list_name, list_version, entry_key, status, created_at"
        " FROM risk_hits WHERE partner_id=? AND status='ACTIVE'", (partner_id,))]
    tasks = [review_task_view(conn, r["task_id"]) for r in conn.execute(
        "SELECT task_id FROM review_tasks WHERE partner_id=? AND status='PENDING'", (partner_id,))]
    return {
        **p,
        "documents": docs,
        "ubos": ubos,
        "active_conclusion": conclusion_view(conn, active, role) if active else None,
        "active_hits": hits,
        "pending_review_tasks": tasks,
    }


def regulatory_summary(conn, partner_id, role) -> dict:
    """监管摘要：撤回后仍可获取，保留主体沿革、证件摘要、核验来源与复核责任。"""
    p = apply_mask(must_partner(conn, partner_id), role, PARTNER_MASK_FIELDS)
    revisions = [dict(r) for r in conn.execute(
        "SELECT revision, legal_name, reg_number, country, reason, changed_by, changed_at"
        " FROM subject_revisions WHERE partner_id=? ORDER BY revision", (partner_id,))]
    docs = [apply_mask(d, role, DOC_MASK_FIELDS) for d in (dict(r) for r in conn.execute(
        "SELECT doc_id, subject_revision, doc_type, doc_number, content_hash, stage, version,"
        " channel, occurred_at, received_at FROM documents WHERE partner_id=? ORDER BY received_at",
        (partner_id,)))]
    ubos = [apply_mask(u, role, UBO_MASK_FIELDS) for u in (dict(r) for r in conn.execute(
        "SELECT ubo_id, subject_revision, full_name, id_number, dob, ownership_pct, role, active"
        " FROM ubos WHERE partner_id=? ORDER BY created_at", (partner_id,)))]
    latest = conn.execute(
        "SELECT * FROM conclusions WHERE partner_id=? ORDER BY computed_at DESC, rowid DESC LIMIT 1",
        (partner_id,)).fetchone()
    hit_rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM risk_hits WHERE partner_id=? GROUP BY status",
        (partner_id,)).fetchall()
    tasks = [review_task_view(conn, r["task_id"]) for r in conn.execute(
        "SELECT task_id FROM review_tasks WHERE partner_id=? ORDER BY created_at", (partner_id,))]
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM conclusions WHERE partner_id=?", (partner_id,)).fetchone()["n"]
    return {
        "partner_id": p["partner_id"],
        "legal_name": p["legal_name"],
        "reg_number": p["reg_number"],
        "country": p["country"],
        "status": p["status"],
        "withdrawn_at": p["withdrawn_at"],
        "subject_revision": p["subject_revision"],
        "subject_revisions": revisions,
        "documents": docs,
        "ubos": ubos,
        "latest_conclusion": conclusion_view(conn, latest, role) if latest else None,
        "conclusions_total": total,
        "hits_by_status": {r["status"]: r["n"] for r in hit_rows},
        "review_tasks": tasks,
        "generated_at": now_iso(),
    }


def audit_trail(conn, partner_id, role) -> list[dict]:
    if role not in AUDIT_ROLES:
        raise Forbidden("仅合规或审计角色可查看审计轨迹", code="AUDIT_ROLE_REQUIRED")
    must_partner(conn, partner_id)
    out = []
    for r in conn.execute(
            "SELECT * FROM audit_events WHERE partner_id=? ORDER BY seq", (partner_id,)):
        d = dict(r)
        d["detail"] = json.loads(d.pop("detail_json") or "{}")
        out.append(d)
    return out
