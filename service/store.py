"""线程安全的内存档案库，可选 JSON 文件持久化。

设置持久化路径后，每次写操作落盘（临时文件 + 原子替换），
服务重启后档案、结论历史与待复核队列完整恢复。
"""
from __future__ import annotations

import json
import os
import threading


def _empty_state() -> dict:
    return {
        'counters': {},
        'applications': {},
        'documents': {},
        'conclusions': {},
        'review_tasks': {},
        'notifications': {},
        'audit': [],
        'sanctions': {'current_version': 0, 'entries': {}, 'updates': []},
    }


class Store:
    def __init__(self, path: str | None = None):
        self._path = path
        self._lock = threading.RLock()
        self.data = _empty_state()
        if path and os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as fh:
                loaded = json.load(fh)
            state = _empty_state()
            state.update(loaded)
            self.data = state

    def locked(self) -> threading.RLock:
        """所有读写都在同一把可重入锁下串行化，保证并发现场的一致性。"""
        return self._lock

    def next_id(self, prefix: str) -> str:
        counters = self.data['counters']
        counters[prefix] = counters.get(prefix, 0) + 1
        return f'{prefix}-{counters[prefix]}'

    def persist(self) -> None:
        if not self._path:
            return
        tmp = self._path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(self.data, fh, ensure_ascii=False)
        os.replace(tmp, self._path)
