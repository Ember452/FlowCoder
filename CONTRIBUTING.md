# 贡献指南

感谢参与 FlowCoder 开发。请遵循以下规范。

## 开发环境

`ash
uv pip install -e ".[dev]"
pre-commit install
`

## 代码规范

- 遵循 ruff（line-length=100）
- 新增功能必须配套单元测试（tests/unit/）
- 提交信息遵循 Conventional Commits 中文规范

## 提交流程

1. 从 main 切出特性分支
2. 实现并补测试
3. pytest -q 全绿、uff check 无报错
4. 提交 PR，描述变更与动机
