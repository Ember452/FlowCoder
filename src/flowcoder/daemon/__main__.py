"""Daemon 入口：python -m flowcoder.daemon。"""

from flowcoder.daemon.server import run_daemon

if __name__ == "__main__":
    run_daemon()
