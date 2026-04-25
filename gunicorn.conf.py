import os

workers     = 2          # 2 個進程，各自有 gthread 線程池
worker_class = 'gthread'
threads     = 6          # 每個 worker 6 條線程 = 共 12 個並發槽（I/O-bound 效果好）
timeout     = 60
keepalive   = 5


def post_fork(server, worker):
    # 只讓 worker #1 啟動 keep-alive / DB keepalive / 年假同步等背景執行緒，
    # 避免多個 worker 重複執行相同工作。
    os.environ['GUNICORN_WORKER_ID'] = str(worker.age)
