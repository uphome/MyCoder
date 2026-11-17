# MyCoder 前端（web/ui）

**来源**：视觉层 fork 自 [deepseek-ai/chat-demo](https://github.com/deepseek-ai/chat-demo)（MIT），
fork 的 commit：`5d5dd754ebc1948e977ddb880a9eb5aa25f37b4c`。

**我们用它做什么**：只取 **UI 层**（组件、主题、Markdown/代码块/公式渲染、多语言）。
后端与协议**完全是我们自己的**（my_coder/）：agent loop、会话日志、压缩、召回、工具、审批、后台任务。
上游的 services/api/（Anthropic 客户端）会被换成解析我们自己的 SSE；services/chat/ 的
浏览器存储会改成"以 .sessions/*.jsonl 为唯一事实源"的投影。

**许可**：上游 LICENSE 原样保留在 LICENSE。修改过的部分同样按 MIT 处理。

## 构建

```sh
pnpm install
pnpm build        # 产物在 dist/，由 FastAPI 在 /ui 托管（运行期零构建、零 Node）
pnpm dev          # 开发：http://localhost:8090（设 MYCODER_API_ORIGIN 指向后端）
```

> M0 阶段：产物尚未入库，/ui 需要先 pnpm build。