"""环境初始化脚本：创建 .flowcoder/ 运行时目录与默认配置。"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main() -> None:
    runtime = ROOT / ".flowcoder"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "debug.log").touch()
    print(f"FlowCoder runtime dir ready: {runtime}")


if __name__ == "__main__":
    sys.exit(main())
