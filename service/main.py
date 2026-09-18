"""服务入口：健康检查 + 海外合作方尽调档案 HTTP API。

环境变量：
- PORT           监听端口，默认 8000
- DD_STORE_PATH  档案持久化路径（JSON）；设置后服务重启不丢失待复核队列
"""
import os
from http.server import ThreadingHTTPServer

from .api import make_handler
from .core import DueDiligenceService
from .store import Store


def run():
    store = Store(os.getenv('DD_STORE_PATH') or None)
    service = DueDiligenceService(store)
    port = int(os.getenv('PORT', '8000'))
    ThreadingHTTPServer(('0.0.0.0', port), make_handler(service)).serve_forever()


if __name__ == '__main__':
    run()
