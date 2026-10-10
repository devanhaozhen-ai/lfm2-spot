# 外部免费健康检查备用方案

状态：代码已验证，尚未连接或部署到 Cloudflare 账户，不应视为已运行的外部告警。

`worker.mjs` 可以在 Cloudflare Workers Free 中独立运行。它只读取公开 JSON，不需要 GitHub 令牌，不采集交易所成交，也不发送订单。

- `GET /latest.json`：每次读取用当前 UTC 重新计算两个数据年龄，保留原有 JSON 结构。
- `GET /health`：完整且新鲜返回 200；任一年龄超过 900 秒、数据不完整或接口失败返回 503，可由 cron-job.org 免费监测 HTTP 状态并配置账户通知。
- 独立 Cron：每五分钟检查并写入 Workers 日志，异常会使该次执行失败。日志本身不等于用户已经收到告警。

手机连接 Cloudflare 账户后，可在 Workers 网页编辑器粘贴 `worker.mjs` 内容，使用 Free 方案，并设置 UTC `4-59/5 * * * *`。`wrangler.toml` 是相同配置的命令行版本。部署后必须实测过期数据的 503 响应和所选通知渠道，才能宣称外部告警已投入运行。

这是独立读取与健康门控方案，不会替代 GitHub 上的采集。若需要外部定时触发 `workflow_dispatch`，还需在外部平台进行安全的 GitHub 应用授权；不要在代码、公开 URL 或聊天中提供令牌。

官方说明：

- https://developers.cloudflare.com/workers/configuration/cron-triggers/
- https://developers.cloudflare.com/workers/platform/limits/
- https://cron-job.org/en/
