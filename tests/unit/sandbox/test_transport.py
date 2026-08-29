"""文件传输（内存 tar + put_archive）的路径校验与打包逻辑测试。"""

from __future__ import annotations

import io
import tarfile

import pytest

from flowcoder.sandbox.runtime import SandboxError
from flowcoder.sandbox.transport import build_tar_bytes, copy_files


class TestPathValidation:
    @pytest.mark.parametrize(
        "name",
        [
            "",
            "   ",
            "/etc/passwd",
            "../secret.txt",
            "a/../../secret.txt",
            "a/../b.txt",
            "..",
            "C:\\temp\\x.txt",
            "sub\\file.txt",
        ],
    )
    def test_rejects_invalid_paths(self, name: str) -> None:
        with pytest.raises(SandboxError, match="非法的沙箱文件路径"):
            build_tar_bytes({name: "x"})

    @pytest.mark.parametrize("name", ["a.txt", "sub/a.txt", "a/b/c.txt", "a..b.txt"])
    def test_accepts_valid_paths(self, name: str) -> None:
        # 含 ".." 的合法文件名（如 a..b.txt）与多级路径都应通过
        build_tar_bytes({name: "x"})


class TestBuildTarBytes:
    def test_file_content_and_mode(self) -> None:
        data = build_tar_bytes({"hello.py": "print('hi')"})
        with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
            names = tar.getnames()
            assert "hello.py" in names
            info = tar.getmember("hello.py")
            assert info.size == len("print('hi')")
            content = tar.extractfile(info).read()
            assert content == b"print('hi')"

    def test_multi_level_path_adds_parent_dirs(self) -> None:
        data = build_tar_bytes({"pkg/sub/mod.py": "x = 1"})
        with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
            members = {m.name.rstrip("/"): m for m in tar.getmembers()}
            assert members["pkg"].isdir()
            assert members["pkg/sub"].isdir()
            assert not members["pkg/sub/mod.py"].isdir()

    def test_str_content_encoded_utf8(self) -> None:
        data = build_tar_bytes({"n.txt": "中文"})
        with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
            assert tar.extractfile("n.txt").read() == "中文".encode()

    def test_bytes_content_passthrough(self) -> None:
        payload = bytes([0, 1, 2, 255])
        data = build_tar_bytes({"bin.dat": payload})
        with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
            assert tar.extractfile("bin.dat").read() == payload


class TestCopyFiles:
    async def test_puts_archive_at_base_dir(self, fake_runtime) -> None:
        await copy_files(fake_runtime, "cid-1", "/workspace", {"a.txt": "hi"})
        assert len(fake_runtime.archives) == 1
        path, data = fake_runtime.archives[0]
        assert path == "/workspace"
        assert b"a.txt" in data

    async def test_validation_error_prevents_archive(self, fake_runtime) -> None:
        with pytest.raises(SandboxError):
            await copy_files(fake_runtime, "cid-1", "/workspace", {"../x": "hi"})
        assert fake_runtime.archives == []
