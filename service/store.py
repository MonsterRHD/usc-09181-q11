"""SQLite 持久化层。

- WAL 模式 + busy_timeout，支持多线程读写；
- 进程内写锁串行化写事务，保证"读-改-写"（如重算结论）的原子性；
- 所有状态（含待复核队列、通知、审计）落盘，重启后不丢失。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS partners(
  partner_id TEXT PRIMARY KEY,
  legal_name TEXT NOT NULL,
  reg_number TEXT NOT NULL,
  country TEXT NOT NULL,
  contact_name TEXT,
  contact_email TEXT,
  contact_phone TEXT,
  status TEXT NOT NULL DEFAULT 'ACTIVE',        -- ACTIVE | WITHDRAWN
  subject_revision INTEGER NOT NULL DEFAULT 1,  -- 主体版本：身份字段变化即递增
  signing_state TEXT NOT NULL DEFAULT 'BLOCKED',-- BLOCKED | READY | PAUSED | WITHDRAWN
  created_at TEXT NOT NULL,
  withdrawn_at TEXT
);
CREATE TABLE IF NOT EXISTS subject_revisions(
  partner_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  legal_name TEXT NOT NULL,
  reg_number TEXT NOT NULL,
  country TEXT NOT NULL,
  reason TEXT,
  changed_by TEXT,
  changed_at TEXT NOT NULL,
  PRIMARY KEY(partner_id, revision)
);
CREATE TABLE IF NOT EXISTS documents(
  doc_id TEXT PRIMARY KEY,
  partner_id TEXT NOT NULL,
  subject_revision INTEGER NOT NULL,            -- 证件归属的主体版本
  doc_type TEXT NOT NULL,
  doc_number TEXT NOT NULL,
  content_hash TEXT NOT NULL,                   -- 证件摘要（不存原文）
  stage INTEGER NOT NULL DEFAULT 1,             -- 分阶段收件的阶段号
  holder_name TEXT,
  subject_reg_number TEXT,                      -- 证件所属主体注册号（防串主体）
  channel TEXT NOT NULL DEFAULT 'online',       -- online | offline（离线补传）
  occurred_at TEXT,                             -- 线下业务实际发生时间
  received_at TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  UNIQUE(partner_id, subject_revision, doc_type, doc_number)
);
CREATE TABLE IF NOT EXISTS ubos(
  ubo_id TEXT PRIMARY KEY,
  partner_id TEXT NOT NULL,
  subject_revision INTEGER NOT NULL,
  full_name TEXT NOT NULL,
  id_number TEXT NOT NULL,
  dob TEXT,
  ownership_pct REAL,
  role TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sanction_lists(
  list_name TEXT NOT NULL,
  version TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'FULL',            -- FULL | CORRECTION
  effective_at TEXT NOT NULL,                   -- 名单生效时间（可能早于到达时间=迟到）
  received_at TEXT NOT NULL,
  corrects_version TEXT,
  entries_json TEXT NOT NULL,
  PRIMARY KEY(list_name, version)
);
CREATE TABLE IF NOT EXISTS conclusions(
  conclusion_id TEXT PRIMARY KEY,
  partner_id TEXT NOT NULL,
  subject_revision INTEGER NOT NULL,
  computed_at TEXT NOT NULL,
  trigger TEXT NOT NULL,                        -- 触发原因
  result TEXT NOT NULL,                         -- INCOMPLETE | CLEAR | HIT
  status TEXT NOT NULL,                         -- ACTIVE | SUPERSEDED
  signing_state TEXT NOT NULL,
  inputs_json TEXT NOT NULL                     -- 核验来源：证件摘要、受益人、名单版本
);
CREATE INDEX IF NOT EXISTS idx_conclusions_partner ON conclusions(partner_id, status);
CREATE TABLE IF NOT EXISTS risk_hits(
  hit_id TEXT PRIMARY KEY,
  partner_id TEXT NOT NULL,
  conclusion_id TEXT,
  subject_kind TEXT NOT NULL,                   -- PARTNER | UBO
  subject_name TEXT NOT NULL,
  list_name TEXT NOT NULL,
  list_version TEXT NOT NULL,
  entry_key TEXT NOT NULL,
  entry_json TEXT NOT NULL,
  status TEXT NOT NULL,                         -- ACTIVE | RELEASED | INVALIDATED_BY_CORRECTION
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_hits_partner ON risk_hits(partner_id, status);
CREATE TABLE IF NOT EXISTS conclusion_impacts(
  impact_id TEXT PRIMARY KEY,
  conclusion_id TEXT NOT NULL,
  partner_id TEXT NOT NULL,
  list_name TEXT NOT NULL,
  list_version TEXT NOT NULL,
  impact_type TEXT NOT NULL,                    -- LATE_LIST_HIT | CORRECTION_REMOVED_HIT
  detail TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(conclusion_id, list_name, list_version, impact_type)
);
CREATE TABLE IF NOT EXISTS review_tasks(
  task_id TEXT PRIMARY KEY,
  partner_id TEXT NOT NULL,
  type TEXT NOT NULL,                           -- SANCTIONS_RELEASE
  status TEXT NOT NULL,                         -- PENDING | APPROVED | REJECTED | CANCELLED
  required_approvals INTEGER NOT NULL DEFAULT 2,-- 双人复核
  hit_id TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON review_tasks(status);
CREATE TABLE IF NOT EXISTS review_approvals(
  task_id TEXT NOT NULL,
  actor TEXT NOT NULL,
  decision TEXT NOT NULL,                       -- APPROVE | REJECT
  note TEXT,
  decided_at TEXT NOT NULL,
  PRIMARY KEY(task_id, actor)                   -- 同一复核人只能表决一次
);
CREATE TABLE IF NOT EXISTS notifications(
  notification_id TEXT PRIMARY KEY,
  dedup_key TEXT NOT NULL UNIQUE,               -- 通知去重键
  type TEXT NOT NULL,
  partner_id TEXT,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor TEXT,
  role TEXT,
  action TEXT NOT NULL,
  entity_type TEXT,
  entity_id TEXT,
  partner_id TEXT,
  detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_partner ON audit_events(partner_id);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self._write_lock = threading.RLock()
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        conn = self.connect()
        conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        """每线程一个连接（autocommit 模式，写操作显式开启事务）。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    @contextmanager
    def write(self):
        """写事务：进程内串行 + BEGIN IMMEDIATE，保证重算等复合操作原子提交。"""
        with self._write_lock:
            conn = self.connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def read(self, sql: str, args=()):
        return self.connect().execute(sql, args).fetchall()

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
