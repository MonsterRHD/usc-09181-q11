"""领域常量与通用工具。

尽调档案中的所有实体以 dict 表示，便于 JSON 持久化与按角色脱敏后序列化。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

# 准入必收证件：注册证明、股权结构图、联系人证件
REQUIRED_DOC_TYPES = frozenset({'registry', 'ownership_chart', 'id_document'})
KNOWN_DOC_TYPES = REQUIRED_DOC_TYPES | {'license', 'tax_certificate', 'bank_reference'}

# 证件默认绑定的主体：联系人证件随联系人失效，其余随合作方主体失效
DOC_TYPE_BINDING = {'id_document': 'contact'}
DEFAULT_DOC_BINDING = 'partner'

APP_STATUSES = ('intake', 'eligible', 'paused', 'signed', 'withdrawn')

# 角色：business 脱敏视图；compliance/auditor 全量；regulator 仅监管摘要
FULL_ACCESS_ROLES = frozenset({'compliance', 'auditor'})
AUDIT_ROLES = frozenset({'compliance', 'auditor'})
REVIEW_ROLES = frozenset({'compliance'})

# 双人复核：制裁命中解除所需的不同复核人数量
REQUIRED_APPROVALS = 2


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def canonical_hash(payload) -> str:
    """对任意可 JSON 化对象生成稳定哈希，用于快照/证件集/结论指纹。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def norm_text(value) -> str:
    """名称/证件号归一化：压缩空白并转小写，用于名单匹配。"""
    if not value:
        return ''
    return ' '.join(str(value).split()).lower()
