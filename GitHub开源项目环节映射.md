# 影视续集追踪方案 GitHub 开源项目环节映射

更新时间：2026-06-04

## 结论先说

这套方案最稳的做法不是继续把能力堆进 `NextReleaseTracker`，而是让它只保留“中枢路由 + 少量状态 + 规则决策”：

1. `MoviePilot` 继续做宿主和领域入口。
2. `Sonarr/Radarr` 负责“续季/续作发现”。
3. `Prowlarr` 负责索引器聚合。
4. `qBittorrent` / `SABnzbd` 负责下载执行。
5. `PostgreSQL + NocoDB` 负责状态、手工映射、审计面板。
6. `OPA` 负责把你的判定条件做成外部规则。
7. `Grafana + ntfy` 负责观测和通知。
8. “看完才追”不要自己造事件系统，直接接 `Tautulli`（Plex）或 `Jellyfin Webhook Plugin`（Jellyfin）。

这样你这个项目只做编排，不自己重造媒体发现、索引器管理、下载器、后台表格和通知系统。

## 按环节映射

| 环节 | 首选项目 | 备选/补充 | 为什么适合做模块 | 调用方式 | GitHub 热度与成熟度快照 |
| --- | --- | --- | --- | --- | --- |
| 宿主/领域入口 | `jxxghp/MoviePilot` | `n8n-io/n8n`、`temporalio/temporal` 只作为外部编排层 | 你当前链路已经在 MoviePilot 上，且它明确支持插件、REST API、MCP、CLI，不需要推翻现有宿主 | 插件、REST API、MCP、CLI、Docker | MoviePilot `11.1k` stars，Latest `v2.13.4`（2026-06-02）；n8n `191k` stars，Latest `2.23.2`（2026-06-01）；Temporal `20.7k` stars，Latest `v1.31.0`（2026-04-29） |
| 订阅完成/入库完成事件 | `MoviePilot` | 无 | 你当前方案里的 `SubscribeComplete` / `TransferComplete` 本来就是宿主内最短路径，继续从宿主拿事件最省事 | 宿主事件、插件回调、REST API | 沿用 MoviePilot 即可，不建议再加一个重复事件总线 |
| 真实观看完成事件（未来“看完才追”） | `Tautulli/Tautulli`（Plex） | `jellyfin/jellyfin-plugin-webhook`（Jellyfin） | 这类事件最靠谱的来源不是你自己推断，而是媒体服务器的真实播放/最近观看事件 | Webhook、通知、API、模板消息 | Tautulli `6.5k` stars，Latest `v2.17.1`（2026-05-05）；Jellyfin Webhook Plugin `232` stars，Latest `Version 21`（2026-05-05） |
| 历史回填/补数 | `MoviePilot` + `Tautulli` / `Jellyfin` | 无 | 回填应直接从“真实来源”补：订阅/转移历史从 MoviePilot，观看历史从媒体服务器，不要另建虚假历史层 | REST API、事件历史、Webhook 事件归档 | 这一环本质上是“源系统补数”，不建议额外造轮子 |
| TV 新季发现 | `Sonarr/Sonarr` | 直接 TMDB 轮询只作为兜底，不建议首选 | Sonarr 天然就是“持续监控剧集后续集数/后续季”的成熟模块，比自己轮询 TMDB 更稳 | REST API、Docker、Webhook、RSS/索引器联动 | Sonarr `13.9k` stars，Latest `4.0.17.2952`（2026-03-19） |
| Movie 续作发现 | `Radarr/Radarr` | 手工映射保留在你自己的状态层 | 电影续作、上映状态、收藏集相关的生态成熟度上，Radarr 明显比“自己包一层 TMDB wrapper”更强 | REST API、Docker、Webhook、索引器联动 | Radarr `13.7k` stars，Latest `6.1.1.10360`（2026-03-26） |
| 候选判定规则 | `open-policy-agent/opa` | 简单条件可先留在中枢内，复杂后迁出到 OPA | 你文档里的 `grace_days`、`pending`、`air_date`、`是否已存在完整季` 这类条件非常适合外部化为策略 | REST API、Go SDK、Bundle | OPA `11.8k` stars，Latest `v1.16.2`（2026-05-12） |
| 追踪状态/手工映射/动作审计 | `postgres/postgres` + `nocodb/nocodb` | 只要数据库不要 UI 时可仅上 PostgreSQL | 你的 `tracked_tv` / `tracked_movie` / `manual_mappings` / `action_log` 正好适合结构化存储；NocoDB 可以直接给你表格化后台和程序化接口 | SQL、REST API、SDK、Docker | PostgreSQL GitHub mirror `21.1k` stars；NocoDB `63.2k` stars，Latest `2026.05.3`（2026-06-01） |
| 手工维护后台 | `nocodb/nocodb` | 也可继续保留 MoviePilot 插件页作轻量入口 | 这环最怕你自己重复做表格后台；NocoDB 已经自带 CRUD、视图、权限、API | Web UI、REST API、SDK | NocoDB 自带 REST API/SDK，适合把“手工映射”和“待确认候选”外包出去 |
| 索引器聚合/站点搜索入口 | `Prowlarr/Prowlarr` | 无 | 这是最典型不该自己造的环节。Prowlarr 已经负责 tracker/indexer 管理、同步到 Sonarr/Radarr | REST API、Docker、Indexer Sync | Prowlarr `6.6k` stars，Latest `2.3.5.5327`（2026-04-04） |
| 下载执行（Torrent） | `qbittorrent/qBittorrent` | 无 | 成熟、API 清晰、生态大；适合被中枢或 *arr 系统直接推任务 | WebUI API、Docker、Web UI | qBittorrent `37.9k` stars，Latest `v5.2.1`（2026-05-25） |
| 下载执行（Usenet） | `sabnzbd/sabnzbd` | 无 | 如果你走 NZB/Usenet，这个环节直接外包给 SABnzbd，接口成熟 | HTTP API、脚本、Docker | SABnzbd `2.7k` stars，仓库成熟；官方 API 文档可直接用于入队、查队列、改分类 |
| 观测/面板 | `grafana/grafana` | 无 | 你不应该自己再做一套复杂状态页；Grafana 直接吃 PostgreSQL 或日志源即可 | Dashboard、数据源、告警 | Grafana `74k` stars，Latest `13.0.1+security-01`（2026-05-12） |
| 推送通知 | `binwiederhier/ntfy` | 也可走 Jellyfin/Tautulli 自带通知目标 | 这环不值得自己维护消息服务；ntfy 足够轻，HTTP 触发非常顺手 | HTTP PUT/POST、Docker、移动端 App | ntfy `30.6k` stars，Latest `v2.23.0`（2026-05-18） |

## 我给你的首选总装方案

按你现在的代码基线，我推荐的不是“全替换”，而是下面这套：

1. **宿主继续用 `MoviePilot`**
   - 原因：你已经有插件边界、宿主 API、事件接入、现成运行环境。
   - 这里不要推翻。

2. **把“续季/续作发现”迁到 `Sonarr + Radarr`**
   - 你的中枢只负责问它们“有没有新季/新片值得处理”。
   - 不再自己硬扛 TMDB 轮询、collection 关系和发布时间边界。

3. **把“索引器管理”彻底交给 `Prowlarr`**
   - 不在插件里碰站点规则。

4. **把“下载执行”交给 `qBittorrent` 或 `SABnzbd`**
   - 中枢只发任务、查状态、收完成事件。

5. **把“手工映射/待确认候选/动作日志”迁到 `PostgreSQL + NocoDB`**
   - 插件页只保留轻量概览和操作入口。
   - 真正需要人工改的数据，放到表格化后台。

6. **把候选规则抽成 `OPA`**
   - 你的插件只提交输入数据，OPA 返回 `allow/deny` 和命中原因。
   - 这样以后改规则，不用反复改插件逻辑。

7. **“看完才追”单独接事件源**
   - Plex 用户接 `Tautulli`
   - Jellyfin 用户接 `Jellyfin Webhook Plugin`
   - 不要从下载完成去猜“是否真的看完”。

8. **面板和通知不要自己做大**
   - 图表和告警交给 `Grafana`
   - 手机推送交给 `ntfy`

## 哪些不建议你自己做

下面这些环节，自己做长期都会变成维护坑：

1. 索引器管理
2. 下载器任务编排
3. 手工映射后台表格
4. 复杂规则引擎
5. 播放完成事件采集
6. Dashboard 与告警系统

## 几个关键判断

### 1. `TMDB wrapper` 不该是核心模块

你的当前实现围绕 `TMDB collection_id` 和季信息没问题，但如果要满足“成熟而且火”，GitHub 上真正成熟的不是各种 TMDB 包，而是 `Sonarr/Radarr` 这种已经把元数据发现、监控、索引器联动、下载衔接都做透的项目。

结论：**TMDB 适合做底层数据源，不适合做你的核心模块边界。**

### 2. `n8n` 很火，但 license 不是标准 OSI 开源

`n8n` 和 `NocoDB` 都很火，也非常适合当模块；但 license 更接近 source-available / fair-code。

结论：

- 如果你优先要“热度 + 接起来快”，可以用。
- 如果你后面要严格卡“OSI 开源”，这两项需要单独复核许可策略。

### 3. 你现在最值钱的是“规则 + 编排”，不是“功能再造”

你真正应该保留在自己手里的只有：

1. 什么时候算“该追”
2. 哪些来源可信
3. 冲突时谁优先
4. 状态如何落库
5. 人工如何覆写

其余能力都应该外包给现成项目。

## 建议落地顺序

1. 先保留 `MoviePilot` 宿主不动。
2. 接 `Sonarr/Radarr/Prowlarr`，先只做“读状态 + 发请求”。
3. 接 `qBittorrent` 或 `SABnzbd`。
4. 把 `tracked_*` / `manual_mappings` / `action_log` 落到 `PostgreSQL`。
5. 给人工台上 `NocoDB`。
6. 再把判定条件迁到 `OPA`。
7. 最后补 `Grafana + ntfy`，以及 `Tautulli/Jellyfin` 的“看完才追”链路。

## 源链接

- MoviePilot: https://github.com/jxxghp/MoviePilot
- n8n: https://github.com/n8n-io/n8n
- Temporal: https://github.com/temporalio/temporal
- Tautulli: https://github.com/Tautulli/Tautulli
- Jellyfin Webhook Plugin: https://github.com/jellyfin/jellyfin-plugin-webhook
- Sonarr: https://github.com/Sonarr/Sonarr
- Radarr: https://github.com/Radarr/Radarr
- Prowlarr: https://github.com/Prowlarr/Prowlarr
- OPA: https://github.com/open-policy-agent/opa
- PostgreSQL: https://github.com/postgres/postgres
- NocoDB: https://github.com/nocodb/nocodb
- qBittorrent: https://github.com/qbittorrent/qBittorrent
- qBittorrent WebUI API: https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-%28qBittorrent-4.1%29
- SABnzbd: https://github.com/sabnzbd/sabnzbd
- SABnzbd API: https://sabnzbd.org/wiki/configuration/5.0/api
- Grafana: https://github.com/grafana/grafana
- ntfy: https://github.com/binwiederhier/ntfy
