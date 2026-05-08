import os

# ── Worker 數量與模式 ──────────────────────────────────────────────
# Render Starter: 1 vCPU, 512 MB RAM
# gthread: I/O-bound 最佳，一個 worker 多條 thread 共享記憶體
workers      = 2
worker_class = 'gthread'
threads      = 4        # 2 workers × 4 threads = 8 並發槽（降 4 是為留記憶體空間給 Redis 客戶端）

# ── 穩定性設定 ────────────────────────────────────────────────────
timeout           = 60
keepalive         = 5
max_requests      = 500          # 每個 worker 處理 500 請求後自動重啟，防止記憶體洩漏
max_requests_jitter = 50         # 錯開重啟時間，避免所有 worker 同時重啟

# ── 效能優化 ──────────────────────────────────────────────────────
preload_app       = True         # fork 前載入 app，worker 間共享程式碼 copy-on-write
worker_tmp_dir    = '/dev/shm'   # worker heartbeat 寫到記憶體 tmpfs，減少磁碟 I/O


def post_fork(server, worker):
    """每個 worker fork 後重新建立 DB 連線池，避免父子進程共享 socket。"""
    os.environ['GUNICORN_WORKER_ID'] = str(worker.age)

    # 重新初始化連線池：preload_app=True 時父進程已建立池，fork 後子進程必須重建
    import db as _db
    if _db._db_pool is not None:
        try:
            _db._db_pool.close()
        except Exception:
            pass
        _db._db_pool = None
    _db._init_db_pool()

    # 重置 Redis 快取連線（各 worker 建立自己的連線）
    import cache as _cache
    _cache._redis_client = None
    _cache._redis_ok     = None
