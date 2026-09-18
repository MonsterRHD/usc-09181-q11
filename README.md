# 海外合作方尽调档案

银行与海外代理合作前的准入尽调服务：把主体关系、证件摘要、核验来源、风险命中与复核责任串成一份可审计的档案，支撑签约状态观察与监管追溯。

## 运行与测试

```bash
python -m service.main                 # 启动服务（PORT 默认 8000）
DD_STORE_PATH=/data/dd.json python -m service.main   # 开启落盘，重启不丢待复核队列
python -m unittest discover -s tests   # 运行测试
```

请求通过 `X-Role`（business / compliance / auditor / regulator）与 `X-Actor` 头标识操作者。

## 目录职责

- `service/models.py` — 领域常量（必收证件、角色、双人复核人数）与哈希工具
- `service/store.py` — 线程安全内存档案库，可选 JSON 落盘（原子写）
- `service/core.py` — 领域服务：快照、收件、核验、名单、复核、撤回/重启、通知、审计
- `service/masking.py` — 按角色脱敏的视图构造
- `service/api.py` — HTTP 路由与角色门禁；`service/main.py` — 服务入口

## 关键业务规则

- **结论绑定主体**：身份/联系人/受益所有人任一变化生成新主体快照，核验结论绑定「快照 × 证件集 × 名单版本」指纹；关系变化即重算，旧结论保留为历史（superseded），绝不套用到新主体。主体变更时绑定旧主体的证件自动作废。
- **分阶段收件 + 幂等**：同一证件（类型+号码）重复发送且内容一致 → 幂等去重；内容变化 → 旧版本作废、版本号 +1 并重算。
- **制裁命中**：命中即暂停签约并生成复核任务；只能由两名不同复核人双人复核解除；同一命中签名放行后不重复暂停。
- **名单迟到/更正**：回标所有受影响的历史结论（`affected_by`，含重估结果），在办申请按新名单重算；已撤回申请不再发起新查询。
- **撤回/重启**：撤回后停止新查询与新收件，监管摘要与待复核队列保留；重启不丢队列并按当前名单重算。
- **通知去重**：按去重键合并，重复事件累加 `suppressed`；所有变更写入连续序号的审计日志。

## 主要端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/applications` | 新建合作申请 |
| PATCH | `/applications/{id}` | 主体身份变更（注册号变化作废主体证件） |
| POST | `/applications/{id}/contact` | 更换联系人（旧联系人证件作废） |
| POST/PATCH/DELETE | `/applications/{id}/beneficial-owners[/{bo}]` | 受益所有人维护 |
| POST | `/applications/{id}/documents` | 分阶段收件（支持 `offline_backfill` 离线补传） |
| POST | `/applications/{id}/verify` | 主动核验 |
| GET | `/applications/{id}/signing-status` | 签约状态观察点 |
| POST | `/applications/{id}/sign|withdraw|restart` | 签约 / 撤回 / 重启 |
| GET | `/applications/{id}/regulatory-summary` | 监管摘要（撤回后仍可用，regulator 可访问） |
| POST | `/sanctions-list/updates` | 名单更新/更正（版本单调递增） |
| GET | `/review-queue` · POST `/review-tasks/{id}/decisions` | 待复核队列与双人复核 |
| GET | `/applications/{id}/conclusions` `/audit` `/notifications` | 结论历史 / 审计 / 通知 |

敏感配置请放在本地环境文件中，不要提交。
