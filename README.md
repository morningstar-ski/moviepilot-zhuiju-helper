# 追剧助手

MoviePilot V2 插件。先把你想继续追的剧或电影加入名单，插件再帮你留意后面的新一季或下一部；发现后提醒一次，然后结束这条追踪。

## 给用户看的理解

- 剧集：把想追的剧加入名单后，插件会继续帮你关注后面的新一季。
- 电影：把想追的电影加入名单后，插件会尽量继续帮你关注这个系列后面的新片。
- 大多数电影系列都能自动识别；少数没认出来的情况，再手动补一次关联就行。
- 发现新一季或下一部后，只提醒一次，不会自动替你补订阅。

## 当前行为

- 默认**不**因为订阅完成、入库完成、历史回填而自动建立长期追踪。
- 只有插件配置里的 `长期追踪剧集 TMDB ID 列表` / `长期追踪电影 TMDB ID 列表` 中明确填写的 ID 才会进入长期追踪。
- 扫描命中新季或续作后：
  - 只发通知
  - 不自动补订阅
  - 自动结束该条追踪
- 历史回填和事件监听也只对已加入名单的条目生效。

## 仓库结构

```text
.
├── icons/
│   └── nextreleasetracker.png
├── package.v2.json
├── plugins.v2/
│   └── nextreleasetracker/
│       ├── __init__.py
│       ├── logic.py
│       └── state.py
└── tests/
    └── test_nextreleasetracker_logic.py
```

## 安装要求

- MoviePilot `>= 2.12.0`
- 使用 V2 插件目录结构

## 发布到 GitHub 前应保持的仓库形态

仓库根目录必须保留以下内容：

- `package.v2.json`
- `plugins.v2/nextreleasetracker/`
- `icons/nextreleasetracker.png`

MoviePilot 的插件装载器会按 `package.v2.json` 和 `plugins.v2/<plugin_id_lowercase>` 读取插件。

## 安装方式

### 方式 1：作为 GitHub 插件仓库安装

将本仓库上传到 GitHub 后，在 MoviePilot 的插件仓库配置中填入该仓库地址，随后安装 `追剧助手`（插件 ID 仍是 `NextReleaseTracker`）。

仓库必须满足：

- 根目录存在 `package.v2.json`
- 插件目录为 `plugins.v2/nextreleasetracker`
- 图标路径与 `package.v2.json` 一致

### 方式 2：作为本地插件仓库安装

1. 将本仓库放到本机某个目录。
2. 设置 `PLUGIN_LOCAL_REPO_PATHS` 指向该目录。
3. 在 MoviePilot 中安装 `追剧助手`（插件 ID 仍是 `NextReleaseTracker`）。

## 配置说明

### 必填白名单

- `长期追踪剧集 TMDB ID 列表`
- `长期追踪电影 TMDB ID 列表`

这两栏就是你的“追更名单”。

- 剧集名单：用来继续追后面的新一季
- 电影名单：用来继续追这个系列后面的新片

如果你是在界面里操作，优先直接搜索片名后加入；只有少数找不到的情况，才需要手动填写编号。

技术格式上仍然是每行一个 TMDB ID。

示例：

```text
60625
1399
```

```text
603
1891
```

### 其他配置

- `notify`：是否发送通知
- `enable_tv`：是否启用剧集扫描
- `enable_movie`：是否启用电影扫描
- `cron`：定时扫描计划
- `grace_days`：提前判定窗口
- `history_days`：历史回填窗口
- `backfill_on_enable`：启用时是否对白名单条目做历史回填

### 电影手动关联（高级）

当某个电影系列没有被自动识别成“同一套电影”时，可以在配置页的 `电影手动关联（高级）` 里手动补。

格式是每行一条，左右两边都填 **TMDB 编号**：

```text
源电影TMDB=目标电影TMDB1,目标电影TMDB2 # 可选备注
```

示例：

```text
603=604,605 # 黑客帝国(603) 后面继续关注 黑客帝国2：重装上阵(604)、黑客帝国3：矩阵革命(605)
```

## 主要 API

- `GET /tracks`
- `POST /rescan`
- `POST /track/add`
- `POST /track/remove`
- `POST /import/transfer_history`
- `POST /manual_map/add`
- `POST /manual_map/remove`

说明：

- `track/add` 会把目标加入白名单并建立追踪状态。
- `track/remove` 会同时移除追踪状态和白名单。
- `rescan` 只会对白名单条目生效。

## 通知模型

命中新季或续作后，通知正文会带上：

- 标题
- TMDB ID
- 命中的季号或续作列表
- 命中状态（可处理 / 已在库 / 已存在订阅）
- “已结束本条追踪”标记

## 本地验证

仓库内自带本地发布校验脚本：

```powershell
& 'C:\Users\yang\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  tools\validate_local_release.py `
  --workspace 'C:\Users\yang\Desktop\影视追剧助手' `
  --moviepilot-source 'C:\tmp\MoviePilot'
```

该脚本会检查：

- Python 语法
- 单元测试
- `package.v2.json` 元数据
- 插件版本一致性
- 与本地 MoviePilot 源码的 V2 装载约定是否匹配

## 发布前检查清单

1. `package.v2.json` 版本号与 `plugin_version` 一致
2. `tools/validate_local_release.py` 全部通过
3. `tests/test_nextreleasetracker_logic.py` 全部通过
4. 仓库中不包含 `__pycache__`、临时日志和远端调试残留
5. `package.v2.json`、图标和插件目录路径一致
