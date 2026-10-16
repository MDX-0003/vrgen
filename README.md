# vrgen — 视频生成服务

> 状态：**M0 脚手架**。CLI 命令面已按契约声明（stub），实现未开始。
> 架构与路线：`E:\VRAIGC\docs\`（architecture / case-model / roadmap）。

对平台（mglc / Seedance）的一切交互归本仓库：登录态、素材登记、上传缩档、
提交、轮询、下载、轮次台账；另有"手动注册本地文件"后端，两种后端写同构台账。

上游是 vrstudio（流程仓库）：它组装输入包（视频 + mask + 提示词 + 分辨率档），
以 CLI 子进程调用本仓库，"采用"（adopt）时把结果拷回 case。
**两仓库只走 CLI 契约，禁止互相 import**（AGENTS.md 硬规则 1）。

## 快速开始

```bash
uv sync --extra dev          # 装环境
uv run vrgen --help          # 命令面（契约见 docs/case-model.md）
uv run pytest                # 本地门禁（等价于 CI 的 test job）
uv run ruff check .          # lint
```

## 目录

```
src/vrgen/cli.py      # 入口（当前为契约 stub）
tests/                # 冒烟：断言命令面完整
config/default.toml   # 平台参数（M1 落地）
templates/            # 提示词模板（M1 迁入）
runs/                 # 轮次台账（运行期数据，gitignore）
```
