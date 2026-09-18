# 海外合作方尽调档案

银行与海外代理合作前的尽调档案服务：把**主体关系、证件摘要、核验来源、风险命中、复核责任**
串到一条可审计的链路上，支撑合作准入决策与签约状态控制。

纯 Python 标准库实现（`http.server` + `sqlite3`），无第三方运行时依赖。

## 运行

```bash
python3 -m service.main            # 默认 :8000，数据文件 data/duediligence.db
PORT=8080 DD_DB_PATH=/var/dd.db python3 -m service.main
python3 -m unittest discover -s tests   # 测试
python3 scripts/demo.py                 # 端到端现场演示（需服务已启动）
```

请求头：`X-Actor`（操作人）、`X-Role`（角色：`compliance` / `auditor` / `rm` / 其他）。

## 核心设计

**主体版本（防旧结论套新主体）**
合作方经常先换联系人、后补证件，旧结论容易被误套到新主体。本服务将法定名称/注册号/国家
视为身份字段：任一变化即 `subject_revision + 1`，旧结论全部作废（`SUPERSEDED`），证件与受益
所有人按版本归属、不带入新主体，签约回到 `BLOCKED` 重新收件核验。仅换联系人不影响结论（留审计）。
证件可携带 `subject_reg_number`，与当前主体不一致直接 `409 SUBJECT_MISMATCH` 拒绝。

**分阶段收件 + 关系变化重算**
证件按 `stage` 分阶段提交，响应实时返回缺口（`missing`：注册摘录/股权结构图/受益人及其证件）。
任何关系变化（证件、受益人、名单、复核结果）都触发 `recompute`：输入（证件摘要、受益人、
名单版本、命中指纹）未变则幂等跳过，变化则生成新结论并串起完整核验来源（`inputs`）。

**幂等收件**
同一证件（合作方+主体版本+类型+号码）重复发送返回原记录（`deduplicated: true`）；
内容变化（哈希不同）则版本 +1 并触发重算。离线补传支持 `channel=offline` + `occurred_at`。

**制裁命中与双人复核**
命中名单即 `signing_state=PAUSED` 并生成 `SANCTIONS_RELEASE` 复核任务（需 2 名**不同**合规
角色先后同意才可解除；同一人重复表决 `409`）。放行记录为 `RELEASED`，同一条目不再重复阻断；
复核驳回则维持暂停。命中失效（名单更正/受益人变更）时在途任务级联取消（`CANCELLED`）。

**名单迟到与更正**
名单版本带 `effective_at`。新增条目会追溯标注生效日之后作出的 `CLEAR` 历史结论
（`LATE_LIST_HIT`）；更正版本移除条目则命中置为 `INVALIDATED_BY_CORRECTION`，相关历史结论
标注 `CORRECTION_REMOVED_HIT`。影响集中落在 `conclusion_impacts`，随结论历史可查。

**撤回与监管摘要**
撤回后停止一切新收件与新查询（`409`），签约状态 `WITHDRAWN`；
`GET /partners/{id}/regulatory-summary` 仍提供主体沿革、证件摘要、核验来源、
命中统计与复核责任等监管所需信息。

**持久化与并发**
全部状态（含待复核队列、通知、审计）落 SQLite（WAL）；进程内写锁 + `BEGIN IMMEDIATE`
保证复合操作原子性，重启后队列不丢。通知按去重键落库（同一条目命中只通知一次），
所有状态变化写审计（操作人、角色、时间、明细）。

**角色脱敏**
`compliance` / `auditor` 见明文；其余角色证件号、受益人证件号、出生日期、联系方式脱敏
（如 `ID-1001` → `***1001`）。审计接口仅 `compliance` / `auditor` 可见。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/partners` | 建档 |
| GET | `/partners/{id}` | 档案视图（按角色脱敏） |
| PATCH | `/partners/{id}/identity` | 主体/联系人变更（身份字段变化升版本） |
| POST | `/partners/{id}/documents` | 分阶段收件（幂等、支持离线补传） |
| POST/PATCH/DELETE | `/partners/{id}/ubos[/{uid}]` | 受益所有人登记/变更/移除 |
| POST | `/partners/{id}/verify` | 手动重算（撤回后拒绝） |
| POST | `/partners/{id}/withdraw` | 撤回合作申请 |
| GET | `/partners/{id}/signing` | 签约状态 |
| GET | `/partners/{id}/conclusions` | 结论历史（含影响标注） |
| GET | `/partners/{id}/regulatory-summary` | 监管摘要（撤回后可用） |
| GET | `/partners/{id}/audit` | 审计轨迹（合规/审计角色） |
| POST | `/sanction-lists` | 应用名单版本（`kind=CORRECTION` 为更正） |
| GET | `/review-tasks?status=PENDING` | 待复核队列 |
| POST | `/review-tasks/{id}/decisions` | 复核表决（`APPROVE`/`REJECT`，双人） |
| GET | `/notifications` | 通知（已去重） |

## 目录

```
service/
  main.py    # 入口：PORT / DD_DB_PATH
  app.py     # HTTP 路由与错误处理
  domain.py  # 领域逻辑：重算、名单、复核、脱敏、审计
  store.py   # SQLite 模式与写事务
  util.py    # 时间/ID/哈希/条目指纹
tests/       # unittest：主流程、名单、重启、并发
scripts/demo.py  # 现场演示脚本
```
