"""e2e：沙箱内任务完成（真实容器，无 Docker 自动跳过）。

端到端闭环：池预热 → 传文件 → 容器内执行 → 结果回传 → 归还销毁重建。
待 Docker 环境就绪后此测试即真实验收（P1a 回填项之一）。
"""

from __future__ import annotations

import asyncio

import pytest

from flowcoder.sandbox.pool import SANDBOX_LABEL, SandboxPool
from flowcoder.sandbox.runtime import DockerRuntime, SandboxError


def _docker_available() -> bool:
    try:
        DockerRuntime.from_env()
    except SandboxError:
        return False
    return True


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _docker_available(), reason="本机无 Docker daemon"),
]


async def test_sandbox_task_end_to_end() -> None:
    """传脚本进容器 → 执行（读写工作目录）→ 结果回传 → 归还后无残留。"""
    import docker

    pool = SandboxPool(size=1)
    await pool.start()
    try:
        # ① 传任务脚本：读输入文件、计算、写输出文件、打印结果
        script = (
            "data = open('input.txt').read().strip()\n"
            "nums = [int(x) for x in data.split(',')]\n"
            "total = sum(nums)\n"
            "open('output.txt', 'w').write(str(total))\n"
            "print(f'total={total}')\n"
        )
        result = await pool.execute(
            ["python", "task.py"],
            files={"input.txt": "1,2,3,4,5", "task.py": script},
            timeout_s=30.0,
        )

        # ② 结果回传：退出码与 stdout 正确
        assert result.exit_code == 0
        assert "total=15" in result.stdout
        assert result.timed_out is False

        # ③ 工作目录挂载语义：归还没销毁工作目录（下一次租借环境全新）
        lease = await pool.lease()
        check = await lease.execute("cat output.txt 2>/dev/null || echo ABSENT")
        assert "ABSENT" in check.stdout  # 上一租借的输出文件不应残留
        await lease.release()

        await asyncio.sleep(0.5)
        # ④ 池归还即销毁重建：沙箱容器不留任务残留
        client = docker.from_env()
        running = client.containers.list(filters={"label": SANDBOX_LABEL})
        assert len(running) == pool.snapshot()["pool_size"]
    finally:
        await pool.close()

    # ⑤ close 后无残留容器（"无残留"验收）
    await asyncio.sleep(0.5)
    client = docker.from_env()
    remaining = client.containers.list(filters={"label": SANDBOX_LABEL})
    assert remaining == []


async def test_sandbox_task_timeout_killed() -> None:
    pool = SandboxPool(size=1)
    await pool.start()
    try:
        result = await pool.execute("sleep 60", timeout_s=2.0, kill_grace_s=1.0)
        assert result.timed_out
        assert result.exit_code in (124, 137, None)
    finally:
        await pool.close()
