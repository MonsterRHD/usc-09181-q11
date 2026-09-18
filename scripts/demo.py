#!/usr/bin/env python3
"""现场演示脚本：走完"建档 -> 分阶段收件 -> 命中暂停 -> 双人复核 ->
名单迟到/更正 -> 撤回 -> 重启队列恢复"的完整链路。

用法：
    python3 scripts/demo.py [base_url]      # 默认 http://127.0.0.1:8000
"""
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
COMPLIANCE = {"X-Actor": "demo-lead", "X-Role": "compliance"}


def call(method, path, body=None, headers=COMPLIANCE, expect=None):
    r = requests.request(method, BASE + path, json=body, headers=headers, timeout=10)
    tag = "OK " if expect is None or r.status_code == expect else "!!!"
    print(f"  [{tag}] {method:6s} {path:55s} -> {r.status_code}")
    if expect is not None and r.status_code != expect:
        print(r.text)
        sys.exit(1)
    return r


def show(title):
    print(f"\n=== {title} ===")


def signing(pid):
    return call("GET", f"/partners/{pid}/signing").json()["signing_state"]


def main():
    show("1. 建档 + 分阶段收件（阶段1 注册摘录 / 阶段2 股权图+受益人证件）")
    pid = call("POST", "/partners", {
        "legal_name": "Ocean Trading Ltd", "reg_number": "REG-001", "country": "SG",
        "contact_name": "Chen Wei", "contact_email": "chen.wei@ocean.example",
        "contact_phone": "+6588881234"}, expect=201).json()["partner"]["partner_id"]
    call("POST", f"/partners/{pid}/documents",
         {"doc_type": "REGISTRY_EXTRACT", "doc_number": "RE-1", "content": "registry", "stage": 1}, expect=201)
    call("POST", f"/partners/{pid}/documents",
         {"doc_type": "OWNERSHIP_CHART", "doc_number": "OC-1", "content": "chart", "stage": 2}, expect=201)
    call("POST", f"/partners/{pid}/ubos",
         {"full_name": "Zhang San", "id_number": "ID-1001", "ownership_pct": 60}, expect=201)
    call("POST", f"/partners/{pid}/documents",
         {"doc_type": "UBO_ID", "doc_number": "UBOID-1", "content": "passport",
          "stage": 2, "holder_name": "Zhang San"}, expect=201)
    print(f"  签约状态: {signing(pid)}  (资料齐备 -> READY)")

    show("2. 同一证件重复发送（幂等）+ 离线补传")
    r = call("POST", f"/partners/{pid}/documents",
             {"doc_type": "REGISTRY_EXTRACT", "doc_number": "RE-1", "content": "registry"}, expect=200)
    print(f"  deduplicated = {r.json()['deduplicated']}")
    call("POST", f"/partners/{pid}/documents",
         {"doc_type": "CONTACT_ID", "doc_number": "CID-1", "content": "paper",
          "channel": "offline", "occurred_at": "2026-08-15T09:30:00Z"}, expect=201)

    show("3. 制裁名单命中 -> 暂停签约 -> 双人复核解除")
    call("POST", "/sanction-lists", {
        "list_name": "OFAC-LIKE", "version": "v1", "effective_at": "2026-09-01T00:00:00Z",
        "entries": [{"name": "Zhang San", "id_number": "ID-1001", "country": "SG"}]}, expect=201)
    print(f"  签约状态: {signing(pid)}  (命中 -> PAUSED)")
    task_id = call("GET", "/review-tasks?status=PENDING").json()["review_tasks"][0]["task_id"]
    call("POST", f"/review-tasks/{task_id}/decisions", {"decision": "APPROVE"},
         headers={"X-Actor": "carol", "X-Role": "compliance"}, expect=200)
    print(f"  第一人同意后: {signing(pid)}  (仍 PAUSED，需双人)")
    call("POST", f"/review-tasks/{task_id}/decisions", {"decision": "APPROVE"},
         headers={"X-Actor": "dave", "X-Role": "compliance"}, expect=200)
    print(f"  第二人同意后: {signing(pid)}  (双人复核达成 -> READY)")

    show("4. 并发现场操作：改受益人 / 名单更正 / 离线补传（观察去重与一致性）")
    call("POST", "/sanction-lists", {
        "list_name": "EU-LIKE", "version": "v1", "effective_at": "2026-09-10T00:00:00Z",
        "entries": [{"name": "Zhang San", "id_number": "ID-1001", "country": "SG"}]}, expect=201)
    print(f"  新名单再次命中: {signing(pid)}")
    ubo_id = call("GET", f"/partners/{pid}").json()["ubos"][0]["ubo_id"]

    def concurrent_ops():
        results = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            results.append(pool.submit(call, "PATCH", f"/partners/{pid}/ubos/{ubo_id}",
                                       {"ownership_pct": 55}))
            results.append(pool.submit(call, "POST", "/sanction-lists", {
                "list_name": "EU-LIKE", "version": "v2", "kind": "CORRECTION",
                "effective_at": "2026-09-12T00:00:00Z", "entries": []}))
            results.append(pool.submit(call, "POST", f"/partners/{pid}/documents",
                                       {"doc_type": "CONTACT_ID", "doc_number": "CID-2",
                                        "content": "paper2", "channel": "offline",
                                        "occurred_at": "2026-08-20T00:00:00Z"}))
            return [f.result() for f in results]

    t0 = threading.Thread(target=concurrent_ops)
    t0.start()
    t0.join()
    print(f"  并发后签约状态: {signing(pid)}  (名单更正移除条目 -> READY)")
    pending = call("GET", "/review-tasks?status=PENDING").json()["review_tasks"]
    print(f"  待复核队列: {len(pending)} 条  (命中失效的任务已级联取消)")
    notes = call("GET", f"/notifications?partner_id={pid}").json()["notifications"]
    hit_notes = [n for n in notes if n["type"] == "sanctions_hit"]
    print(f"  sanctions_hit 通知数: {len(hit_notes)}  (同一条目去重)")

    show("5. 历史结论影响标注（迟到名单/更正）与完整审计")
    conclusions = call("GET", f"/partners/{pid}/conclusions").json()["conclusions"]
    impacted = [c for c in conclusions if c["impacts"]]
    print(f"  受影响历史结论: {len(impacted)} 条")
    for c in impacted:
        for i in c["impacts"]:
            print(f"    - {c['conclusion_id']} [{c['result']}] <- {i['impact_type']} ({i['list_name']}@{i['list_version']})")
    audit = call("GET", f"/partners/{pid}/audit",
                 headers={"X-Actor": "demo", "X-Role": "auditor"}).json()["audit"]
    print(f"  审计事件: {len(audit)} 条，动作覆盖: {sorted({a['action'] for a in audit})}")

    show("6. 角色脱敏（rm 视角 vs compliance 视角）")
    masked = call("GET", f"/partners/{pid}", headers={"X-Actor": "rm1", "X-Role": "rm"}).json()
    full = call("GET", f"/partners/{pid}").json()
    print(f"  rm 看到受益人证件号: {masked['ubos'][0]['id_number']}  |  compliance 看到: {full['ubos'][0]['id_number']}")

    show("7. 撤回合作申请：停止新查询，保留监管摘要")
    call("POST", f"/partners/{pid}/withdraw", expect=200)
    call("POST", f"/partners/{pid}/verify", expect=409)
    summary = call("GET", f"/partners/{pid}/regulatory-summary").json()
    print(f"  状态: {summary['status']} | 结论总数: {summary['conclusions_total']} | "
          f"证件摘要: {len(summary['documents'])} 份 | 命中记录: {summary['hits_by_status']}")

    print("\n演示完成。重启服务后可用 GET /review-tasks?status=PENDING 验证待复核队列未丢失。")


if __name__ == "__main__":
    main()
