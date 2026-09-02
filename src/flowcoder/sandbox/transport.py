"""文件进出：内存 tar + put_archive（docker cp 等价物）。

不用宿主目录挂载（规避 WSL2 跨文件系统慢 IO），也不用临时卷；
路径必须是工作目录内的相对路径，穿越与绝对路径一律拒绝。
"""

from __future__ import annotations

import asyncio
import io
import tarfile
from pathlib import PurePosixPath
from typing import Mapping

from flowcoder.sandbox.runtime import ContainerRuntime, SandboxError


def _validate_rel_path(name: str) -> None:
    """校验相对路径：拒绝空串、反斜杠、绝对路径与 .. 穿越。"""
    if not name or not name.strip():
        raise SandboxError(f"非法的沙箱文件路径：{name!r}（不能为空）")
    # 反斜杠在 POSIX 是合法文件名字符，但极易造成 Windows 语义混淆，直接拒绝
    if "\\" in name:
        raise SandboxError(f"非法的沙箱文件路径：{name!r}（不允许反斜杠）")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise SandboxError(f"非法的沙箱文件路径：{name!r}（不能是绝对路径或包含 ..）")


def build_tar_bytes(files: Mapping[str, bytes | str]) -> bytes:
    """把 {相对路径: 内容} 打包成内存 tar，供 put_archive 传入容器。"""
    for name in files:
        _validate_rel_path(name)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        added_dirs: set[str] = set()
        for name, content in sorted(files.items()):
            data = content.encode("utf-8") if isinstance(content, str) else content
            # 多级路径需要先补父目录条目，否则 put_archive 解包会失败
            parts = PurePosixPath(name).parts
            for i in range(1, len(parts)):
                parent = "/".join(parts[:i])
                if parent not in added_dirs:
                    info = tarfile.TarInfo(name=f"{parent}/")
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o1777
                    tar.addfile(info)
                    added_dirs.add(parent)
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o664
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def copy_files(
    runtime: ContainerRuntime,
    container_id: str,
    base_dir: str,
    files: Mapping[str, bytes | str],
) -> None:
    """把一批文件传入容器的 base_dir（如 /workspace）。"""
    archive = build_tar_bytes(files)
    await asyncio.to_thread(runtime.put_archive, container_id, base_dir, archive)
