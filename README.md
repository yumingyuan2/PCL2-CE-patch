# PCL2-CE-patch

自动跟踪 [PCL-Community/PCL-CE](https://github.com/PCL-Community/PCL-CE) 的 release，
在源码上应用补丁后重新构建 x64 / ARM64 版本，并以 `_patched` 后缀发布到本仓库的 Releases。

> 这是非官方构建，与 PCL-Community 无关。使用前请自行判断风险。

## 自动构建的补丁

1. **默认隐藏启动页右侧的 PCL CE 版本提示**
   - 强制把 `PageLaunchRight` 中的 `PanHint` 设为 `Collapsed`
   - 同时把 `UiLauncherCEHint` 的持久化默认值改为 `false`

2. **隐藏更新后的“PCL CE 版本说明”弹窗**
   - 把 `UpdateManager.ShowCEAnnounce()` 改为空实现
   - 这个弹窗和启动页右侧的提示不是同一个东西，之前只 patch 了右侧卡片

3. **中国大陆仍可使用 Minecraft 下载功能**
   - 关闭 `RegionAssessment.IsRestricted` 区域限制
   - 同时修补 `RestrictedFeaturePolicy` 作为后备，确保下载、补全、整合包安装等入口不会被拦截
   - 旧版 beta 没有区域策略时自动跳过，不会因此构建失败

4. **默认更新指向本仓库的 patched 版本**
   - 新增 `UpdatesPclCePatchModel`，通过 GitHub Releases API 读取本仓库的
     `PCL2_CE_<Release|Beta>_<x64|ARM64>_patched.exe`
   - 把 `UpdateManager.remoteServer` 替换为只使用该更新源
   - 版本号从本仓库的 `v<version>-patched` 标签还原，并校验 GitHub API 返回的
     `sha256` digest

5. **不登录微软账户也能创建其他类型档案**
   - `ProfileUi.CanCreateOtherProfile()` 改为直接返回 `true`
   - 新建档案时始终可以选择 Microsoft、第三方（Authlib）或离线验证
   - 同时隐藏“需要先进行一次正版验证才能使用其他验证方式”的提示

补丁脚本位于 [`scripts/apply_patches.py`](scripts/apply_patches.py)。它使用明确的
代码锚点；如果未来上游重构导致锚点消失，工作流会失败，而不是静默发布一个没有打补丁的版本。

## 触发方式

GitHub Actions 不能直接监听另一个仓库的 `release` 事件，因此：

- GitHub `schedule` 每 30 分钟运行一次 `scripts/plan_releases.py` 轮询上游 release
- 同时构建「最新的正式版」和「最新的 beta 版」中尚未构建过的版本；beta 即使比
  stable 旧也会保留，方便 beta 通道用户
- 支持手动 `workflow_dispatch`（可指定 tag，`force=true` 可强制重建已有 release）
- 支持 `repository_dispatch`（`upstream-release`），方便用外部 webhook/中转实现即时触发

> GitHub 公共仓库的 `schedule` 在 60 天没有仓库活动后会被自动禁用。为了不依赖
> keepalive commit，可以使用 [`worker/`](worker/README.md) 里的 Cloudflare Worker：
> 它通过 Cloudflare Cron 每 15 分钟检查上游 release，然后发送 `repository_dispatch`，
> 不受 GitHub 的 60 天规则影响。

如果希望缩短延迟，可以修改 `.github/workflows/patch-release.yml` 中的 cron，或调整
[`worker/wrangler.toml`](worker/wrangler.toml) 里的 Cloudflare Cron。

## 发布产物

对于上游 `v2.15.1`：

- `PCL2_CE_Release_x64_patched.exe`
- `PCL2_CE_Release_x64_patched.exe.sigstore.json`
- `PCL2_CE_Release_ARM64_patched.exe`
- `PCL2_CE_Release_ARM64_patched.exe.sigstore.json`

上游 beta 版本对应 `PCL2_CE_Beta_*_patched.exe`。

本仓库的 release tag 为 `v<version>-patched`，例如 `v2.15.1-patched`。

## 使用前设置

1. 把本仓库的 Actions 权限设为 **Read and write**（Settings → Actions → General →
   Workflow permissions）。
2. 如需完整的微软登录 / CurseForge 等功能，在仓库 Secrets 中配置上游构建使用的可选密钥：
   `CLIENT_ID`、`CURSEFORGE_API_KEY`、`SENTRY_DSN`、`NAID_CLIENT_ID`、
   `NAID_CLIENT_SECRET`、`LINK_SERVER_ROOTS`、`LOBBY_DEFAULT_SECRET`。
   不配置也能构建，只是对应功能保持禁用。
3. 在 Actions 页面手动运行一次 `Build patched PCL2 CE`，或等待定时任务。

## 更新逻辑说明

patched 版本内置的更新器会请求：

```text
https://api.github.com/repos/<owner>/<repo>/releases?per_page=100
```

然后按当前通道（stable / beta）和架构（x64 / ARM64）选择最新的
`_patched.exe`。因此：

- 上游发布新版本后，本仓库会生成新的 patched release
- 已安装的 patched 版本会从本仓库更新，不会回退到上游未打补丁的版本
- 更新文件的 SHA-256 使用 GitHub Release asset 的 digest 校验

如果所在网络无法访问 `api.github.com`，更新检查会失败；Minecraft 下载功能本身不受影响。
