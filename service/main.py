"""海外合作方尽调档案服务入口。

配置（环境变量）：
- PORT:       监听端口，默认 8000
- DD_DB_PATH: SQLite 数据文件路径，默认 data/duediligence.db
"""
from __future__ import annotations

import os
from http.server import ThreadingHTTPServer

from .app import make_handler
from .store import Store


def create_server(db_path=None, port=None):
    store = Store(db_path or os.getenv("DD_DB_PATH", os.path.join("data", "duediligence.db")))
    port = int(port if port is not None else os.getenv("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(store))
    return server, store


def run():
    server, _ = create_server()
    print(f"尽调档案服务 listening on :{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    run()
