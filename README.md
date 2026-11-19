# vrgen — 视频生成服务

> 状态：生成链路已实现并接真实 mglc 调用。CLI 命令面是对外契约，逐条定义在
> 工作区 `docs/case-model.md` §4。
> 架构与路线：`E:\VRAIGC\docs\`（architecture / case-model / roadmap）。

对平台（mglc / Seedance）的一切交互归本仓库：登录态、上传件缩放与准备、提交、轮询、
下载、轮次台账；另有「手动注册本地文件」后端，两种后端写同构台账。
参考素材不再预登记 SD 合规资产：平台在 generate 时自动上传登记（决策 75）。

上游是 vrstudio（流程仓库）：它组装输入包（视频 + mask + 提示词 + 分辨率档），
以 CLI 子进程调用本仓库，「采用」（adopt）时把结果拷回 case。
**两仓库只走 CLI 契约，禁止互相 import**（AGENTS.md 硬规则 1）。

## 快速开始

```bash
uv sync --extra dev          # 装环境
uv run vrgen --help          # 命令面（契约见 docs/case-model.md）
uv run pytest                # 本地门禁（等价于 CI 的 test job）
uv run ruff check .          # lint
```

mglc 是外部命令行，登录态与额度都由它保管：`vrgen login` 原样透传 mglc 的登录流程，
`vrgen models` / `vrgen sessions list` 只读查询。

## 目录

```
src/vrgen/cli.py         # 单入口多子命令，参数面就是对外契约
src/vrgen/platform/      # mglc 发现与调用、模型清单、平台媒体探测
src/vrgen/prep/          # 预算校验、提示词渲染、上传件缩放与准备（prep 命令）
src/vrgen/round/         # 素材清单、限额闸门、台账读写、轮次流水线、任务状态
src/vrgen/manual.py      # 手动注册本地文件的后端，写与 mglc 后端同构的台账
src/vrgen/jobs.py        # 按 case 名组织提示词草稿与生成参数草稿
src/vrgen/sessions.py    # 只读的项目、剧集与生成会话树
templates/               # 每条 flow 对应的提示词模板
config/default.toml      # 平台参数、模型、分辨率档、超时与登录态路径
packaging/               # PyInstaller 目录式打包入口与 spec（决策 102）
tests/                   # 契约、幂等、台账与平台交互的回归测试
runs/                    # 轮次台账（运行期数据，gitignore）
```

## 打包

`packaging/vrgen.spec` 把 `templates/`、`config/default.toml` 与 `roles.json` 收进
`_internal`，打成目录式 `vrgen.exe`。冻结后资源根由 `src/vrgen/paths.py` 按
`sys._MEIPASS` 解析；安装版里由 vrstudio 注入 `VRGEN_BIN_DIR` 与 `VRGEN_MGLC_BIN`，
不依赖系统 PATH。构建与分发见工作区 `docs/deployment-packaged.md`。
