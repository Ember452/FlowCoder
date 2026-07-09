"""性能基准脚本：测量 Agent 循环与 LLM 客户端关键路径耗时。"""
import time


def main() -> None:
    print("FlowCoder benchmark placeholder")
    t0 = time.perf_counter()
    time.sleep(0.01)
    print(f"baseline: {(time.perf_counter() - t0) * 1000:.2f} ms")


if __name__ == "__main__":
    main()
