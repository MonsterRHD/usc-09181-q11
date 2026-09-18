"""通用工具：时间、ID、哈希与名单条目指纹。"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone


def now_iso() -> str:
    """当前 UTC 时间，ISO 格式（毫秒）。所有时间戳统一为该格式以便字符串比较。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_ts(value) -> str | None:
    """把外部传入的时间规范化为 UTC ISO 字符串；空值返回 None。"""
    if value is None or value == "":
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_name(name) -> str:
    """姓名规范化：去空白、小写，用于名单比对。"""
    return " ".join(str(name or "").strip().lower().split())


def normalize_reg(value) -> str:
    return str(value or "").strip().upper()


def entry_key(list_name: str, entry: dict) -> str:
    """名单条目指纹：同一条目跨版本保持稳定，用于命中去重与更正比对。"""
    base = "|".join(
        [
            str(list_name or ""),
            normalize_reg(entry.get("id_number")),
            normalize_name(entry.get("name")),
            normalize_reg(entry.get("country")),
        ]
    )
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]
