"""按角色脱敏的视图构造。

- compliance / auditor：全量字段（核验与审计职责所需）；
- business：证件号、证件号码、联系方式等敏感字段掩码；
- regulator：不走本模块，仅可访问监管摘要端点（见 api 层）。
"""
from __future__ import annotations

import copy

from .models import FULL_ACCESS_ROLES


def mask_identifier(value):
    """证件号/注册号类字段：保留首尾各两位，其余掩码。"""
    if not value:
        return value
    text = str(value)
    if len(text) <= 4:
        return '****'
    return text[:2] + '****' + text[-2:]


def mask_email(value):
    if not value:
        return value
    if '@' not in value:
        return '****'
    local, domain = value.split('@', 1)
    return (local[:1] or '*') + '***@' + domain


def mask_phone(value):
    if not value:
        return value
    return '****' + str(value)[-2:]


def bo_view(bo: dict, role: str) -> dict:
    view = copy.deepcopy(bo)
    if role not in FULL_ACCESS_ROLES:
        view['id_number'] = mask_identifier(view.get('id_number'))
    return view


def application_view(app: dict, role: str) -> dict:
    view = copy.deepcopy(app)
    if role in FULL_ACCESS_ROLES:
        return view
    contact = view.get('contact') or {}
    contact['id_number'] = mask_identifier(contact.get('id_number'))
    contact['email'] = mask_email(contact.get('email'))
    contact['phone'] = mask_phone(contact.get('phone'))
    view['registration_no'] = mask_identifier(view.get('registration_no'))
    view['beneficial_owners'] = {
        bid: bo_view(bo, role) for bid, bo in (view.get('beneficial_owners') or {}).items()
    }
    return view


def document_view(doc: dict, role: str) -> dict:
    view = copy.deepcopy(doc)
    if role not in FULL_ACCESS_ROLES:
        view['doc_number'] = mask_identifier(view.get('doc_number'))
    return view


def conclusion_view(conclusion: dict, role: str) -> dict:
    view = copy.deepcopy(conclusion)
    if role not in FULL_ACCESS_ROLES:
        for subject in view.get('screened_subjects') or []:
            subject['id_number'] = mask_identifier(subject.get('id_number'))
    return view
