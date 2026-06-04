# 追剧助手

MoviePilot V2 插件。

只追你明确加入名单的剧集和电影。插件会按你设置的周期，把检查量均摊到每分钟推进；发现新一季或同系列下一部后提醒一次，然后结束这条追踪。

## 当前规则

- 只有加入名单的内容才会继续追踪。
- 剧集：根据 TMDB 的季信息判断后续新季。
- 电影：默认只根据 **TMDB collection** 判断同系列下一部。
- 如果电影在 TMDB 里没有 collection，插件**不会自动猜**续作，必须在“电影手动关联”里自己补关系。
- 命中新季或下一部后：
  - 发送通知
  - 不自动补订阅
  - 自动结束本条追踪

## 扫描节奏

- `cron` 不再表示“某个时刻一次性扫完整个名单”。
- `cron` 现在表示“希望多久完成一轮检查”。
- 插件内部每分钟执行一次扫描节拍，并按这个周期把条目均摊推进。
- TMDB 调用上限固定为 **每分钟最多 5 次**；配置项只能调低，不能高于 5。

示例：

- `0 3 * * 1`：希望一周内扫完一轮
- `0 3 * * *`：希望一天内扫完一轮
- `0 * * * *`：希望一小时内扫完一轮

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

## 安装方式

### 方式 1：作为 GitHub 插件仓库安装

仓库根目录必须保留：

- `package.v2.json`
- `plugins.v2/nextreleasetracker/`
- `icons/nextreleasetracker.png`

如果通过 GitHub 仓库给 MoviePilot 安装，还需要提供符合规则的 GitHub Release 资产包。

### 方式 2：作为本地插件仓库安装

1. 将本仓库放到本机某个目录
2. 设置 `PLUGIN_LOCAL_REPO_PATHS` 指向该目录
3. 在 MoviePilot 中安装 `追剧助手`

## 配置说明

### 名单

- `长期追踪剧集 TMDB ID 列表`
- `长期追踪电影 TMDB ID 列表`

这是你的追更名单。只有这里的条目会进入长期追踪。

如果在界面里操作，优先直接搜片名后加入；只有少数搜不到的情况，才需要手填 TMDB 编号。

### 关键配置

- `cron`：希望多久完成一轮检查
- `grace_days`：提前判定窗口
- `history_days`：历史回填窗口
- `max_tmdb_calls_per_minute`：每分钟 TMDB 调用上限，默认 5，最高 5
- `backfill_on_enable`：首次启用时是否回填本地历史

### 电影手动关联

当 TMDB 没给某部电影挂 collection，或者你想强制指定续作链时，可以在配置页里补：

```text
源电影TMDB=目标电影TMDB1,目标电影TMDB2 # 备注
```

示例：

```text
603=604,605 # 黑客帝国(603) 后面继续关注 黑客帝国2：重装上阵(604)、黑客帝国3：矩阵革命(605)
```

## API

- `GET /tracks`
- `POST /rescan`
- `POST /track/add`
- `POST /track/remove`
- `POST /import/transfer_history`
- `POST /manual_map/add`
- `POST /manual_map/remove`

## 本地验证

```powershell
& 'C:\Users\yang\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  tools\validate_local_release.py `
  --workspace 'C:\Users\yang\Desktop\影视追剧助手' `
  --moviepilot-source 'C:\tmp\MoviePilot'
```

校验内容：

- Python 语法
- 单元测试
- `package.v2.json` 元数据
- `plugin_version` 与发布元数据一致
- 与本地 MoviePilot V2 插件装载约定兼容

## 发布前检查

1. `package.v2.json` 版本号与 `plugin_version` 一致
2. `python -m unittest tests.test_nextreleasetracker_logic` 全部通过
3. `tools/validate_local_release.py` 全部通过
4. 仓库里不包含临时日志、远端调试残留和 `__pycache__`
