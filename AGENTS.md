# vrgen 开发守则

工作区级规范在 `E:\VRAIGC\AGENTS.md`（硬规则 1–7 全部适用），本文只记本仓库要点：

- **定位**：对 mglc/Seedance 的一切交互 + 轮次台账 + 手动注册后端。不认识 case、
  不读 vrstudio 的任何文件；输入全部来自显式 CLI 参数（契约：`docs/case-model.md` §4）。
- **不进本仓库**：几何、投影、拼接、像素处理——那些属于 vrstudio。
- **台账是核心资产**：`runs/<job>/<轮次key>/` 五件套字段集是对外契约（硬规则 2），
  断点续跑逻辑建立在它上面，改动必须带回归测试。
- **mglc 是外部 CLI**：子进程调用 + 解析输出；登录态、限额都通过它，
  不重写 HTTP 客户端（决策 D2）。参考素材随 `--video` / `--image` 交本地路径，
  由平台在 generate 时自动上传登记，本仓库不做 `virtual-ip upload`（决策 75）。
