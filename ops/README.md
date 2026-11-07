# VPS 同步、发布与备份

本目录替代 GitHub Actions。目标 Linux 服务器使用 Docker Compose 执行一次性任务，由 systemd 每小时同步、每天北京时间 03:00 备份。网站上传到现有 Cloudflare Worker `running-page` 的 `run-dev.clf3.org`，生产域名切换另行处理。

## 已确定的行为

- 默认同步佳明中国区跑步，可通过 `garmin_region` 选择国际区。所有未见过的活动 ID 都会导入，最近 30 天发生的活动自动重新读取摘要及原始轨迹；旧活动修改通过 `refresh --all` 刷新。
- 使用持久化的 Garmin ID → 本地 ID 映射。修改开始时间保留原本的本地 ID，避免重复记录。不会自动删除远端消失的活动。
- 同步在私有暂存快照中进行，整轮下载、解析及 JSON 校验成功后才替换 `current`。失败不改变正式数据。
- 日期相关 SVG 每天重新检查。同样的数据、代码、配置和实际图表内容不会重复构建发布。
- 成功构建和成功发布分别记账。上传结果未知时，先检查线上 `release.json`，避免不必要的重复上传。待发布版本可在没有新活动或佳明暂时不可用时重试。
- 备份独立运行，提交同一仓库的 `data-backup` 分支。源码、依赖锁、原始轨迹、SQLite、导入记录、JSON/SVG、非敏感配置和校验清单一起保存。
- 保留最近一份已验证的完整压缩快照及最近两个成功发布的构建。本项目日志最多 30 天、100 MiB；容器自身日志另限 10 MiB。首版无邮件告警。

## 环境与目录

运行镜像锁定 Python 包和 pnpm/Wrangler 版本。Compose 默认使用 512 MiB RAM、总计 2560 MiB RAM+Swap、1 CPU；Node 堆上限 1024 MiB，因此宿主必须有足够的 Swap。`ops/build` 使用本项目专用的 BuildKit 容器，对安装依赖和构建镜像也施加相同资源限制，避免挤占宿主。上线前必须用真实数据实测。

```text
/opt/running-page/releases/<revision>/   代码和部署脚本
/opt/running-page/current              当前版本符号链接
/etc/running-page/config.json          非敏感配置
/etc/running-page/credentials.env      0600 凭据文件
/etc/running-page/github_ed25519       0600 GitHub 备份 SSH 私钥
/etc/running-page/known_hosts          已核实的 GitHub SSH 主机公钥
/opt/running-page/current/ops/runtime.env  该代码版本的镜像标签及提交 SHA
/var/lib/running-page/current          最近完整数据快照的符号链接
/var/lib/running-page/releases/        待发布及可回滚的构建
/var/lib/running-page/latest-backup.tar.gz
/var/lib/running-page/backup-git/       独立备份 Git 工作目录
/var/lib/running-page/private/         访问令牌及兼容旧会话，不进入备份
```

### violet 的用户级部署

`violet` 使用 `/data/tianyi/running-page` 作为独立部署根目录，沿用已有 Swap。Docker 可由 `tianyi` 使用；Compose 2.32.1 和 Buildx 0.19.3 从 Docker 官方 Release 下载并校验 SHA256，安装在 `~/.docker/cli-plugins`。

用户级部署默认限制为 2 GiB RAM、总计 3 GiB RAM+Swap、1 CPU。实测 512 MiB 构建器在安装前端依赖时出现大量换页，故在 violet 上提高内存限额。可以通过 `RUNNING_PAGE_MEMORY_LIMIT` 和 `RUNNING_PAGE_MEMORY_SWAP_LIMIT` 调整；升级时沿用部署环境的设置。`RUNNING_PAGE_CPUSET` 将构建和任务绑定到宿主允许的一个 CPU，默认自动选择当前进程允许的首个 CPU。pnpm 8 的 `os.cpus()` 仍会读取全部 192 核，因此安装时还显式计算 `PNPM_WORKERS`，将其内部导入 worker 限制为 1。运行时将 Rolldown、Tokio 和 Rayon 的线程池各限制为 2，避免原生工具按宿主核数创建线程、触及容器的任务数量上限。

- 代码：`/data/tianyi/running-page/releases/<revision>`，`current` 指向当前版本。
- 配置：`/data/tianyi/running-page/config`，包括 `config.json`、`credentials.env` 和后续配置的备份密钥。
- 数据：`/data/tianyi/running-page/state`。容器以宿主当前用户的 UID/GID 写入，凭据文件保持 0600。
- 服务：`~/.config/systemd/user/running-page-*`。已核实该用户的 `Linger=yes`，退出 SSH 后仍可定时运行。

初次安装时指定 `RUNNING_PAGE_ROOT=/data/tianyi/running-page`，例如 `RUNNING_PAGE_ROOT=/data/tianyi/running-page /data/tianyi/running-page/current/ops/install`。安装器会将该路径保存到本版本的 `ops/runtime.env`，后续直接运行 `current/ops/run`、`current/ops/build`、`current/ops/enable`，无需 sudo。对应的服务操作使用 `systemctl --user`。下面的通用部署命令采用系统级路径，在 violet 上替换为上述目录并去掉 sudo。

## 首次部署

先核对最新 `master` 的数据，将代码与数据分开传输。代码目录不要包含 `.git`、`.venv`、`node_modules`、原始轨迹、数据库、生成的 JSON/SVG、任何真实凭据或临时数据库备份。

1. 将代码放入 `/opt/running-page/releases/<revision>`，建立 `current` 链接。
2. 执行 `sudo /opt/running-page/current/ops/install --prepare-swap`。它准备专用 Swap、配置模板和 systemd 单元，**不会启用定时器**。
3. 将完整数据放入 `/var/lib/running-page/seed`，保持仓库中的相对路径。包括 `GPX_OUT`、`run_page/data.db`、`imported.json`、`src/static/activities.json`、`src/static/activity_chunks`、`assets/*.svg`。可选的 TCX/FIT 也会保留。
4. 在代码目录构建镜像：`sudo ops/build`。构建需要 Docker Buildx 插件。Node/Python 基础镜像已经固定到验证过的摘要；需要更换时可设置 `NODE_IMAGE`、`PYTHON_IMAGE`，并重新验收。初次安装完成后，可将 `RUNNING_PAGE_IMAGE_TAG` 和 `RUNNING_PAGE_REVISION` 写入该版本的 `ops/runtime.env`；后续升级命令会自动生成此文件。
5. 执行下面的不需要线上凭据的验收：

```sh
sudo /opt/running-page/current/ops/run bootstrap --from /seed
sudo /opt/running-page/current/ops/run build
sudo /opt/running-page/current/ops/run publish --dry-run
sudo /opt/running-page/current/ops/run backup --local-only
sudo /opt/running-page/current/ops/run restore --to /data/restore-check
sudo /opt/running-page/current/ops/run status
```

恢复目标必须不存在或为空。临时验收目录由操作者核对后清理。`bootstrap` 拒绝覆盖已经初始化的数据。

### 凭据

violet 的配置文件是 `/data/tianyi/running-page/config/credentials.env`，权限保持 0600。系统级部署使用 `/etc/running-page/credentials.env`。填写个人 API Key 和 Cloudflare 变量，GitHub 密钥单独保存。程序不会打印凭据值。

- `GARMIN_PERSONAL_API_KEY`：佳明 Connect 网页设置中创建的 **Personal API Key**。填写 Key 本身，程序自动换取访问令牌。不要将它填入旧的 `GARMIN_SECRET_STRING_CN` 字段。
- `CLOUDFLARE_ACCOUNT_ID` 和 `CLOUDFLARE_API_TOKEN`：允许对目标账户的 Worker 部署及读取 Worker 自定义域名。程序先核对 `run-dev.clf3.org` 已属于配置的 Worker，避免误部署到其他服务。
- GitHub：为备份仓库配置专用写入密钥及核实后的 `known_hosts`，放在上述 `/etc/running-page` 路径；非交互任务不接受未知 SSH 主机公钥。公开仓库的备份包含已有的运动轨迹，与当前仓库相同。

在 `config.json` 中设置 `"garmin_region": "cn"`（默认）或 `"global"`。个人 API 分别使用 `https://personal.connectapi.garmin.cn` 和 `https://personal.connectapi.garmin.com`，始终验证 TLS。中国区已做未授权端点连通性检查，仍需有效 Key 验证实际账户的数据访问。

访问令牌保存在 `state/private/personal-api-token.json`（系统部署对应 `/var/lib/running-page/private`），权限 0600。程序在令牌剩余不足一小时、过期或首次收到 401 时重新换取；持续认证失败会结束本轮，不发布部分数据。替换 Key 或切换地区后，旧令牌缓存失效。[官方说明](https://developer.garmin.com/gc-developer-program/personal/personal-faq/)中访问令牌最长有效 24 小时，Key 可选择 30、90、180 天或一年；Key 到期后需要手动更换。

原始活动通过官方文件下载接口获取。ZIP 中的 FIT/TCX 原文件保存在 `FIT_OUT`/`TCX_OUT`，并转换为现有导入流程使用的 GPX；原始文件与 GPX 一起进入备份。没有 GPS 的室内活动使用摘要中的时间、距离和心率。损坏 FIT、CRC 错误、不完整或多活动文件的 ZIP 均会使本轮回退。[官方客户端及接口参考](https://github.com/garmin/connect-personal-api-python)。

`GARMIN_SECRET_STRING_CN` 仅作中国区旧会话兼容；也可以使用 `state/private/garmin-session.txt`。仅在个人 Key 为空时选择旧方式；配置了 Key 但认证失败时不会自动改用旧会话。[Garth](https://github.com/matin/garth) 已停止维护，新增部署应使用 Personal API Key。

填写凭据后运行：

```sh
sudo /opt/running-page/current/ops/run update
sudo /opt/running-page/current/ops/run backup
sudo /opt/running-page/current/ops/run status
sudo /opt/running-page/current/ops/enable
```

`enable` 会检查真实佳明同步、线上发布和 GitHub 推送都已成功，才启用两个定时器。模板未填写时会拒绝启用。

## 日常操作

```sh
# 最近运行状态、连续失败次数和待发布版本
sudo /opt/running-page/current/ops/run status
sudo tail -n 60 /var/lib/running-page/logs/runner.log
systemctl list-timers 'running-page-*'

# 完整刷新历史记录（仍不自动删除），或仅重试已构建的版本
sudo /opt/running-page/current/ops/run refresh --all
sudo /opt/running-page/current/ops/run publish

# 回滚会暂停 update.timer；选择修正后的代码再恢复自动更新。
sudo /opt/running-page/current/ops/run rollback --to <64位已发布input_hash>

# 备份；网络故障不会抹掉上次成功的归档或待推送提交
sudo /opt/running-page/current/ops/run backup

# 从本地完整快照恢复到空目录
sudo /opt/running-page/current/ops/run restore --to /data/recovered
# 从 data-backup 中的完整提交 SHA 恢复
sudo /opt/running-page/current/ops/run restore --backup-ref <40位提交SHA> --to /data/recovered

# 手动升级：先备份，在新镜像中验证构建，通过后切换代码和镜像
sudo /opt/running-page/current/ops/run upgrade --ref <40位代码提交SHA>
```

恢复后，使用恢复出的代码与依赖版本重新部署，将恢复目录作为新的 bootstrap 种子。凭据需要另外重新配置。不要直接覆盖正在运行的 `current`。

远端手动删除活动，或将活动类型改成非跑步，可能使严格同步报错。请先人工核对，并在数据库 `automation_sources` 中显式将该 `source_id` 的 `ignored` 设为 1，以保留历史记录并停止重试它；需要删除展示记录时另行进行有备份的数据维护。不要靠删除 GPX 文件来解决。

## GitHub 与 Cloudflare 设置

三个旧工作流已移到 `.github/archived-workflows`，不再位于 Actions 执行目录。同时在仓库 Settings → Actions → General 禁用 Actions，防止后续误恢复。

关闭 Cloudflare Worker 的 Git 自动构建，让它只接收 VPS 的直接部署。代码升级不会自动跟随 `master`，GitHub 故障也不会阻止既有镜像继续同步和发布。

## 测试

活动类型保留佳明返回的原始 `subtype`，网页 JSON 与 SVG 共用
`run_page/activity_classification.py`：`indoor_running`、`treadmill_running`
及已有来源的明确室内类型标记为 `is_indoor`，不再按轨迹缺失、里程或 GPS
范围推断室内跑。JSON 同时导出独立的 `has_route` 和 `route_status`
（`available`、`missing`、`invalid`、`insufficient`）。无可用轨迹的活动仍计入
统计与日历，但不生成地图线路，也不借用城市坐标。网页直接读取导出的标记；
仅在读取未带标记的旧 JSON 时兼容旧 `indoor` / `treadmill` 标签。

```sh
PYTHONPATH=.:run_page .venv/bin/python -m unittest tests.test_automation -v
PYTHONPATH=.:run_page .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
pnpm test:frontend
```

测试使用假 Garmin 服务、临时数据库和本地 bare Git 仓库，不调用真实佳明、Cloudflare 或 GitHub。覆盖个人 Key 换令牌、缓存与续领、地区及 Key 变更、真实 FIT 编解码、无 GPS 活动、损坏文件整轮回退、旧 ID 迁移、上传结果未知、备份恢复和推送重试。
