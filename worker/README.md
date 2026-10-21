# Cloudflare Worker 触发器

这个 Worker 用 Cloudflare Cron Trigger 定时检查
[PCL-Community/PCL-CE](https://github.com/PCL-Community/PCL-CE) 的最新正式版和 beta 版。
发现新 tag 时，它向本仓库发送 `repository_dispatch`（`upstream-release`），从而触发
[`patch-release.yml`](../.github/workflows/patch-release.yml)。

这样做的原因：GitHub 公共仓库的 `schedule` 工作流在 **60 天没有仓库活动**后会被自动禁用。
Cloudflare Cron 不受这个限制。

## 工作方式

```text
Cloudflare Cron（默认每 15 分钟）
        │
        ├── GET api.github.com/repos/PCL-Community/PCL-CE/releases
        ├── 取最新 stable / beta tag
        ├── 与 KV 中的 upstream-release-state 比较
        └── 有变化 → POST /repos/<本仓库>/dispatches
                        event_type = upstream-release
```

- 第一次运行（KV 为空）会 dispatch 一次。
- 之后只有 stable 或 beta tag 变化才 dispatch。
- dispatch 成功后才写入 KV；失败会在下一个 cron 重试。
- 工作流自己的 `plan_releases.py` 还会再检查一次是否已有完整 patched release，所以重复
  dispatch 不会重复发布。

## 部署

需要 Node.js 18+ 和 Cloudflare 账号。

```bash
cd worker
npm install
npx wrangler login
```

### 1. 创建 KV

```bash
npx wrangler kv namespace create STATE
```

把输出中的 `id` 填到 [`wrangler.toml`](wrangler.toml)：

```toml
[[kv_namespaces]]
binding = "STATE"
id = "你的 namespace id"
```

### 2. 创建 GitHub Token

建议使用 **fine-grained PAT**：

- Repository access：只选 `yumingyuan2/PCL2-CE-patch`
- Permissions：
  - **Contents: Read and write**（`repository_dispatch` 需要）
  - Metadata: Read（自动包含）

不需要给 Actions 权限，因为这里用的是 `repository_dispatch`，不是 `workflow_dispatch`。

把 token 存成 Worker Secret：

```bash
npx wrangler secret put GITHUB_TOKEN
```

不要把 token 写进 `wrangler.toml` 或提交到仓库。

### 3. 部署

```bash
npx wrangler deploy
```

部署后可以访问：

```text
https://pcl2-ce-patch-dispatcher.<你的子域>.workers.dev/health
```

返回 `{"ok":true,...}` 即正常。

### 4. 本地测试 Cron

```bash
npx wrangler dev --test-scheduled
```

另开一个终端：

```bash
curl "http://localhost:8787/__scheduled?cron=*/15+*+*+*+*"
```

第一次应该看到 `Dispatching patched build`，第二次应该看到
`Upstream releases unchanged`。也可以部署后运行：

```bash
npx wrangler tail
```

观察生产环境日志。

## 配置

[`wrangler.toml`](wrangler.toml) 中：

```toml
[triggers]
crons = ["*/15 * * * *"]

[vars]
GITHUB_REPOSITORY = "yumingyuan2/PCL2-CE-patch"
UPSTREAM_REPOSITORY = "PCL-Community/PCL-CE"
DISPATCH_EVENT_TYPE = "upstream-release"
```

- cron 使用 UTC。
- 如果 fork 了本仓库，把 `GITHUB_REPOSITORY` 改成你的 `owner/repo`。
- 修改 cron 后重新 `npx wrangler deploy`。

## 和 GitHub 原生 schedule 的关系

可以两种方式同时保留：

- Cloudflare Worker：主触发，15 分钟检查一次，不受 60 天禁用影响
- GitHub `schedule`：兜底，默认 30 分钟一次

重复触发没有关系，工作流的 concurrency 和 `plan_releases.py` 会去重。确认 Worker
稳定后，如果不想消耗额外的 Actions 分钟，可以把
[`patch-release.yml`](../.github/workflows/patch-release.yml) 里的 `schedule` 改成每天一次，
或者直接删除 `schedule`，只保留 `workflow_dispatch` 和 `repository_dispatch`。

## 费用

- Workers 免费计划：每天 100,000 次请求；每 15 分钟一次 cron 约 96 次/天
- KV 免费计划：足够存一个很小的状态对象
- 正常情况下不会产生费用

## 注意

- `/health` 是公开的，但只返回服务名，不暴露 token 或 release 状态。
- 如果 token 泄露，立即在 GitHub 撤销并重新 `wrangler secret put GITHUB_TOKEN`。
- Worker 只负责触发；实际构建、签名、发布仍由 GitHub Actions 完成。
