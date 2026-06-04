from __future__ import annotations

from datetime import datetime, timedelta
import json
import re
from threading import Lock
import time
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.triggers.cron import CronTrigger
from fastapi import Body

try:
    from app.chain.mediaserver import MediaServerChain
except ImportError:
    from app.chain import MediaServerChain
from app.chain.subscribe import SubscribeChain
from app.chain.tmdb import TmdbChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.event import Event, eventmanager
from app.db.subscribe_oper import SubscribeOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, MediaType, NotificationType

from .logic import (
    coerce_int,
    normalize_tmdb_id_list,
    parse_season_token,
    season_fully_exists,
    select_ready_collection_movies,
    select_ready_tv_seasons,
)
from .state import TrackerStateStore


class NextReleaseTracker(_PluginBase):
    plugin_name = "追剧助手"
    plugin_desc = (
        "Track only user-selected TV shows and movies, notify once when the next "
        "season or sequel appears, then finish tracking."
    )
    plugin_icon = "nextreleasetracker.png"
    plugin_version = "1.1.7"
    plugin_author = "Codex"
    author_url = "https://openai.com"
    plugin_config_prefix = "nextreleasetracker_"
    plugin_order = 30
    auth_level = 1
    diagnostic_tv_tmdb_id = 9900001
    diagnostic_tv_title = "NRT Diagnostic Series"
    FORM_CANDIDATE_LIMIT = 120
    CONFIG_FIELDS = (
        "enabled",
        "notify",
        "enable_tv",
        "enable_movie",
        "backfill_on_enable",
        "onlyonce",
        "cron",
        "grace_days",
        "history_days",
        "log_retention",
        "tracked_tv_ids",
        "tracked_movie_ids",
        "manual_movie_mappings",
    )

    _enabled = False
    _notify = True
    _enable_tv = True
    _enable_movie = False
    _backfill_on_enable = False
    _onlyonce = False
    _cron = "0 3 * * 1"
    _grace_days = 3
    _history_days = 365
    _log_retention = 200
    _selected_tv_ids: List[int] = []
    _selected_movie_ids: List[int] = []

    _state_store: Optional[TrackerStateStore] = None
    _tmdb_chain: Optional[TmdbChain] = None
    _subscribe_chain: Optional[SubscribeChain] = None
    _media_server_chain: Optional[MediaServerChain] = None
    _scan_lock: Lock = Lock()

    def init_plugin(self, config: dict = None):
        config = config or {}
        normalized_config = self._normalize_config(config)
        self._enabled = normalized_config["enabled"]
        self._notify = normalized_config["notify"]
        self._enable_tv = normalized_config["enable_tv"]
        self._enable_movie = normalized_config["enable_movie"]
        self._backfill_on_enable = normalized_config["backfill_on_enable"]
        self._onlyonce = normalized_config["onlyonce"]
        self._cron = normalized_config["cron"]
        self._grace_days = normalized_config["grace_days"]
        self._history_days = normalized_config["history_days"]
        self._log_retention = normalized_config["log_retention"]
        self._selected_tv_ids = self._parse_track_selection(normalized_config["tracked_tv_ids"])
        self._selected_movie_ids = self._parse_track_selection(normalized_config["tracked_movie_ids"])

        self._state_store = TrackerStateStore(
            self.get_data,
            self.save_data,
            self.del_data,
            log_limit=self._log_retention,
        )
        self._tmdb_chain = TmdbChain()
        self._subscribe_chain = SubscribeChain()
        self._media_server_chain = MediaServerChain()
        if "manual_movie_mappings" in config:
            self._sync_manual_mappings_from_text(normalized_config["manual_movie_mappings"])
        else:
            normalized_config["manual_movie_mappings"] = self._serialize_manual_mapping_text(
                self._state_store.get_manual_mappings()
            )
        self._sync_selected_track_state()
        self._bootstrap_selected_tracks_from_local_catalog()
        if config and self._config_needs_cleanup(config, normalized_config):
            self.update_config(normalized_config)

        if self._backfill_on_enable and self._enabled and not self._state_store.has_tracks():
            runtime = self._state_store.get_runtime_state()
            if not runtime.get("bootstrap_history_imported"):
                summary = self._import_transfer_history(days=self._history_days, reason="bootstrap")
                self._state_store.update_runtime(
                    {
                        "bootstrap_history_imported": True,
                        "bootstrap_history_summary": summary,
                    }
                )

    def get_state(self) -> bool:
        return bool(self._enabled or self._onlyonce)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/tracks",
                "endpoint": self.api_tracks,
                "methods": ["GET"],
                "summary": "Get tracker state",
            },
            {
                "path": "/rescan",
                "endpoint": self.api_rescan,
                "methods": ["POST"],
                "summary": "Run tracker scan immediately",
            },
            {
                "path": "/track/add",
                "endpoint": self.api_track_add,
                "methods": ["POST"],
                "summary": "Add a tracker entry manually",
            },
            {
                "path": "/track/remove",
                "endpoint": self.api_track_remove,
                "methods": ["POST"],
                "summary": "Remove a tracker entry",
            },
            {
                "path": "/import/transfer_history",
                "endpoint": self.api_import_transfer_history,
                "methods": ["POST"],
                "summary": "Backfill from transfer history",
            },
            {
                "path": "/diagnostic/event",
                "endpoint": self.api_diagnostic_event,
                "methods": ["POST"],
                "summary": "Dispatch a synthetic runtime diagnostic event",
            },
            {
                "path": "/manual_map/add",
                "endpoint": self.api_manual_map_add,
                "methods": ["POST"],
                "summary": "Add movie manual mapping",
            },
            {
                "path": "/manual_map/remove",
                "endpoint": self.api_manual_map_remove,
                "methods": ["POST"],
                "summary": "Remove movie manual mapping",
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        state = self._ensure_state_store().snapshot()
        tv_tracks = state.get(TrackerStateStore.KEY_TRACKED_TV, {})
        movie_tracks = state.get(TrackerStateStore.KEY_TRACKED_MOVIE, {})
        tv_candidates, hidden_tv_candidates = self._sorted_form_candidates(MediaType.TV.value)
        movie_candidates, hidden_movie_candidates = self._sorted_form_candidates(MediaType.MOVIE.value)
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": (
                                "这里就是你的追更名单设置。"
                                "把想继续追的剧或电影加到下面后，"
                                "插件会帮你留意后面的新一季或下一部；提醒一次后，这条追踪就会自动结束。"
                            ),
                        },
                    },
                    self._form_section_card(
                        title="基础开关与定时任务",
                        subtitle="先打开插件，再设置多久检查一次。下面的添加和删除只是先改当前页面，记得点“保存”后才会真正生效。",
                        content=[
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "success",
                                    "variant": "tonal",
                                    "text": (
                                        f"当前已加入：剧集 {len(self._selected_tv_ids)} 项 / 电影 {len(self._selected_movie_ids)} 项；"
                                        f"正在追踪：剧集 {len(tv_tracks)} 条 / 电影 {len(movie_tracks)} 条。"
                                    ),
                                },
                            },
                            {
                                "component": "VRow",
                                "content": [
                                    self._col(3, self._switch("enabled", "启用插件")),
                                    self._col(3, self._switch("notify", "发送通知")),
                                    self._col(3, self._switch("enable_tv", "启用剧集追踪")),
                                    self._col(3, self._switch("enable_movie", "启用电影追踪")),
                                ],
                            },
                            {
                                "component": "VRow",
                                "content": [
                                    self._col(3, self._switch("onlyonce", "立即执行一次")),
                                    self._col(3, self._textfield("grace_days", "提前天数", "默认 3")),
                                    self._col(3, self._textfield("cron", "定时扫描 Cron", "0 3 * * 1")),
                                    self._col(3, self._textfield("history_days", "历史回填天数", "默认 365")),
                                ],
                            },
                        ],
                    ),
                    self._form_selection_editor(
                        title="剧集追更名单",
                        subtitle="在这里添加你想继续追的剧。加入后，插件会自动帮你关注后面的新一季。可以直接搜剧名后加入，不需要自己查复杂编号。",
                        media_label="剧集",
                        model_key="tracked_tv_ids",
                        add_model="tv_candidate_id",
                        remove_model="tv_remove_id",
                        search_model="tv_search_text",
                        placeholder="60625",
                        saved_summary=self._selection_snapshot_alert("剧集", self._selected_tv_ids, tv_tracks, False),
                        candidates=tv_candidates,
                        hidden_candidate_count=hidden_tv_candidates,
                        candidate_note="加入后，插件会继续关注这部剧后面的新一季。特别篇、番外、重启版这类内容，一般不会算成同一部剧的下一季。",
                        show_expr="{{ enable_tv }}",
                    ),
                    self._form_selection_editor(
                        title="电影追更名单",
                        subtitle="在这里添加你想继续追的电影。加入后，插件会自动帮你关注这个系列后面的新片。可以直接搜电影名后加入。",
                        media_label="电影",
                        model_key="tracked_movie_ids",
                        add_model="movie_candidate_id",
                        remove_model="movie_remove_id",
                        search_model="movie_search_text",
                        placeholder="550",
                        saved_summary=self._selection_snapshot_alert("电影", self._selected_movie_ids, movie_tracks, True),
                        candidates=movie_candidates,
                        hidden_candidate_count=hidden_movie_candidates,
                        candidate_note="大多数电影系列都能自动识别；如果某一部没认出来，再手动补一次关联就行。",
                        show_expr="{{ enable_movie }}",
                    ),
                    self._form_section_card(
                        title="高级设置（一般不用动）",
                        subtitle="只有在你想补扫旧记录，或者某个电影系列没自动认出来时，才需要来这里设置。",
                        content=[
                            {
                                "component": "VRow",
                                "content": [
                                    self._col(6, self._switch("backfill_on_enable", "首次启用时回填历史")),
                                    self._col(6, self._textfield("log_retention", "日志保留条数", "默认 200")),
                                ],
                            },
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "warning",
                                    "variant": "tonal",
                                    "text": (
                                        "只有加入追更名单的内容，插件才会继续帮你关注。"
                                        "大多数电影系列都能自动识别，少数没认出来的再手动补一次关联就行。"
                                    ),
                                },
                            },
                        ],
                    ),
                    self._form_section_card(
                        title="电影手动关联（高级）",
                        subtitle="如果某个电影系列没自动认出来，可以在这里手动告诉插件它后面还要继续关注哪些电影。",
                        content=[
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "text": (
                                        "每行一条，左边填你加入名单的源电影 TMDB 编号，右边填想继续关注的目标电影 TMDB 编号。"
                                        "空行会自动忽略，保存后生效。"
                                    ),
                                },
                            },
                            self._textarea(
                                "manual_movie_mappings",
                                "每行一条电影关联",
                                "603=604,605 # 示例：黑客帝国(603) 后面继续关注 重装上阵(604)、矩阵革命(605)",
                            ),
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "warning",
                                    "variant": "tonal",
                                    "text": (
                                        "格式示例：603=604,605。这里的数字都是 TMDB 编号；例如黑客帝国(603) -> 黑客帝国2：重装上阵(604)、黑客帝国3：矩阵革命(605)。"
                                        "这只对电影追踪生效，电视剧不用填。"
                                    ),
                                },
                            },
                        ],
                    ),
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "enable_tv": True,
            "enable_movie": False,
            "backfill_on_enable": False,
            "onlyonce": False,
            "cron": "0 3 * * 1",
            "grace_days": 3,
            "history_days": 365,
            "log_retention": 200,
            "tracked_tv_ids": "",
            "tracked_movie_ids": "",
            "manual_movie_mappings": "",
            "tv_search_text": "",
            "tv_candidate_id": "",
            "tv_remove_id": "",
            "movie_search_text": "",
            "movie_candidate_id": "",
            "movie_remove_id": "",
        }

    def get_page(self) -> List[dict]:
        state = self._ensure_state_store().snapshot()
        tv_tracks = state.get(TrackerStateStore.KEY_TRACKED_TV, {})
        movie_tracks = state.get(TrackerStateStore.KEY_TRACKED_MOVIE, {})
        mappings = state.get(TrackerStateStore.KEY_MANUAL_MAPPINGS, {})
        logs = list(reversed(state.get(TrackerStateStore.KEY_ACTION_LOG, [])))[:20]
        runtime = state.get(TrackerStateStore.KEY_RUNTIME_STATE, {})
        last_scan = runtime.get("last_scan_summary") or {}
        last_history = runtime.get("last_history_import") or {}
        last_diagnostic = runtime.get("last_diagnostic") or {}
        scan_finished_at = runtime.get("last_scan_finished_at") or "未执行"
        scan_scope = runtime.get("last_scan_scope") or "all"
        scan_reason = runtime.get("last_scan_reason") or "-"
        selected_tv_count = len(self._selected_tv_ids)
        selected_movie_count = len(self._selected_movie_ids)
        history_summary = "未执行"
        if last_history:
            history_summary = (
                f"最近一次回填：{last_history.get('records', 0)} 条记录"
                f"（TV {last_history.get('tv_imported', 0)} / 电影 {last_history.get('movie_imported', 0)} / "
                f"错误 {last_history.get('errors', 0)}）"
            )

        tv_pending = sum(len(track.get("pending_seasons") or []) for track in tv_tracks.values())
        movie_pending = sum(len(track.get("pending_tmdb_ids") or []) for track in movie_tracks.values())
        selected_total = selected_tv_count + selected_movie_count
        active_total = len(tv_tracks) + len(movie_tracks)
        pending_total = tv_pending + movie_pending
        plugin_mode = "TV + 电影" if self._enable_tv and self._enable_movie else "TV-only" if self._enable_tv else "电影-only" if self._enable_movie else "未启用追踪"
        diagnostic_rows = []
        if last_diagnostic:
            diagnostic_rows = [
                ["最近校验时间", last_diagnostic.get("checked_at") or "-"],
                ["事件类型", last_diagnostic.get("event") or "-"],
                ["样本剧集", last_diagnostic.get("title") or "-"],
                ["TMDB / 季", f"{last_diagnostic.get('tmdb_id') or '-'} / S{int(last_diagnostic.get('season') or 0):02d}"],
                ["事件总线命中", "是" if last_diagnostic.get("track_detected") else "否"],
                ["自动清理", "已执行" if last_diagnostic.get("cleanup_performed") else ("已跳过" if last_diagnostic.get("cleanup") else "保留样本")],
            ]
        selected_tv_rows = self._selected_tv_rows(tv_tracks)
        selected_movie_rows = self._selected_movie_rows(movie_tracks)
        recent_signal_rows = [
            [
                log_entry.get("time") or "-",
                log_entry.get("action") or "-",
                self._log_message(log_entry),
            ]
            for log_entry in logs
            if (log_entry.get("action") or "") in {"tv_detected", "movie_detected", "track_remove"}
        ][:8]

        content: List[dict] = [
            self._page_style(),
            {
                "component": "div",
                "props": {"class": "nrt-page"},
                "content": [
                    self._page_toolbar(),
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "这里用来看你已经加入的内容、当前追踪状态，以及手动重扫。想新增或修改追更名单，请回到配置页保存。",
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "warning",
                            "variant": "tonal",
                            "text": "只有加入追更名单的内容，插件才会继续帮你关注。发现新一季或下一部后，会提醒你一次，然后结束这条追踪。",
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "success" if self._enabled else "warning",
                            "variant": "tonal",
                            "text": (
                                f"当前模式：{plugin_mode} | 定时任务：{self._cron or '未配置'} | "
                                f"通知：{'开启' if self._notify else '关闭'} | "
                                f"已加入名单：{selected_total} 项 | 正在追踪：{active_total} 条"
                            ) if self._enabled else (
                                "插件当前未启用。你现在还能手动重扫或清理列表，但自动盯更新暂时不会运行。"
                            ),
                        },
                    },
                    {
                        "component": "div",
                        "props": {"class": "nrt-summary"},
                        "content": [
                            self._stat_card("已加入名单", selected_total, f"剧集 {selected_tv_count} / 电影 {selected_movie_count}"),
                            self._stat_card("追踪中条目", active_total, f"剧集 {len(tv_tracks)} / 电影 {len(movie_tracks)}"),
                            self._stat_card("待处理候选", pending_total, f"新季 {tv_pending} / 续作 {movie_pending}"),
                            self._stat_card("最近扫描通知", last_scan.get("notifications_sent", 0), f"本轮结束 {last_scan.get('tracks_completed', 0)} 条"),
                        ],
                    },
                    self._section_card(
                        "运行概览",
                        [
                            self._status_chips(
                                [
                                    self._chip("插件已启用" if self._enabled else "插件未启用", "success" if self._enabled else "warning", "mdi-power"),
                                    self._chip(f"模式 {plugin_mode}", "primary", "mdi-view-dashboard"),
                                    self._chip(f"最近扫描 {scan_scope}", "info", "mdi-radar"),
                                    self._chip(f"错误 {last_scan.get('errors', 0)}", "error" if last_scan.get("errors", 0) else "success", "mdi-alert-circle-outline"),
                                ]
                            ),
                            self._simple_table(
                                ["项目", "值"],
                                [
                                    ["最近扫描时间", scan_finished_at],
                                    ["扫描来源", scan_reason],
                                    ["发送通知", last_scan.get("notifications_sent", 0)],
                                    ["结束追踪", last_scan.get("tracks_completed", 0)],
                                    ["更新追踪状态", last_scan.get("tracks_updated", 0)],
                                    ["命中已存在订阅", last_scan.get("existing_subscriptions", 0)],
                                    ["命中媒体库", last_scan.get("existing_in_library", 0)],
                                    ["历史回填摘要", history_summary],
                                ],
                            ),
                        ],
                    ),
                    self._section_card(
                        "已加入的剧集",
                        [
                            self._simple_table(
                                ["剧名", "TMDB", "状态", "当前进度", "操作"],
                                selected_tv_rows,
                                empty_text="你还没把任何剧加入追更名单。去配置页搜索后加入，这里就会显示。",
                            ),
                        ],
                    ),
                    self._section_card(
                        "已加入的电影",
                        [
                            self._simple_table(
                                ["标题", "TMDB", "状态", "当前进度", "操作"],
                                selected_movie_rows,
                                empty_text="你还没把任何电影加入追更名单。去配置页搜索后加入，这里就会显示。",
                            )
                        ],
                    ),
                    self._section_card(
                        "当前追踪中的剧集",
                        [
                            self._simple_table(
                                ["剧名", "TMDB", "最新季", "待处理季", "来源", "更新时间", "操作"],
                                [
                                    [
                                        track.get("title") or f"TMDB-{track.get('tmdb_id')}",
                                        track.get("tmdb_id"),
                                        track.get("latest_season"),
                                        self._join_values(track.get("pending_seasons")),
                                        track.get("source") or "-",
                                        track.get("updated_at") or track.get("added_at") or "-",
                                        self._page_track_remove_button(MediaType.TV.value, coerce_int(track.get("tmdb_id"))),
                                    ]
                                    for track in sorted(
                                        tv_tracks.values(),
                                        key=lambda item: (
                                            str(item.get("title") or ""),
                                            coerce_int(item.get("tmdb_id"), 0) or 0,
                                        ),
                                    )
                                ],
                                empty_text="暂无剧集追踪项。",
                            )
                        ],
                    ),
                    self._section_card(
                        "当前追踪中的电影",
                        [
                            self._simple_table(
                                ["标题", "锚点 TMDB", "Collection", "已知条目", "待处理条目", "来源", "更新时间", "操作"],
                                [
                                    [
                                        track.get("title") or f"TMDB-{track.get('anchor_tmdb_id')}",
                                        track.get("anchor_tmdb_id"),
                                        track.get("collection_id") or "-",
                                        len(track.get("known_tmdb_ids") or []),
                                        self._join_values(track.get("pending_tmdb_ids")),
                                        track.get("source") or "-",
                                        track.get("updated_at") or track.get("added_at") or "-",
                                        self._page_track_remove_button(
                                            MediaType.MOVIE.value,
                                            coerce_int(track.get("anchor_tmdb_id")),
                                            coerce_int(track.get("collection_id")),
                                        ),
                                    ]
                                    for track in sorted(
                                        movie_tracks.values(),
                                        key=lambda item: (
                                            str(item.get("title") or ""),
                                            str(item.get("track_key") or ""),
                                        ),
                                    )
                                ],
                                empty_text="暂无电影追踪项。",
                            )
                        ],
                    ),
                    self._section_card(
                        "最近命中与结束",
                        [
                            self._simple_table(
                                ["时间", "动作", "消息"],
                                recent_signal_rows,
                                empty_text="最近还没有发现新季/续作，或没有手动移除操作。",
                            )
                        ],
                    ),
                    self._section_card(
                        "运行态诊断",
                        [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": (
                                        "info"
                                        if not last_diagnostic
                                        else ("success" if last_diagnostic.get("success") else "warning")
                                    ),
                                    "variant": "tonal",
                                    "text": (
                                        "通过诊断按钮可在运行进程内发送合成 TV 完结事件，默认写入后立即清理，仅用于校验事件总线、状态落库和页面反馈。"
                                        if not last_diagnostic
                                        else (
                                            f"最近一次诊断：{last_diagnostic.get('event') or '-'} / "
                                            f"{last_diagnostic.get('title') or '-'} / "
                                            f"{'通过' if last_diagnostic.get('success') else '未通过'}"
                                        )
                                    ),
                                },
                            },
                            self._simple_table(
                                ["项目", "值"],
                                diagnostic_rows,
                                empty_text="尚未执行运行态诊断。",
                            ),
                        ],
                    ),
                    self._section_card(
                        "手工映射",
                        [
                            self._simple_table(
                                ["源 TMDB", "目标 TMDB 列表", "备注", "更新时间", "操作"],
                                [
                                    [
                                        mapping.get("source_tmdb_id"),
                                        self._join_values(mapping.get("target_tmdb_ids")),
                                        mapping.get("note") or "-",
                                        mapping.get("updated_at") or "-",
                                        self._page_manual_map_remove_button(coerce_int(mapping.get("source_tmdb_id"))),
                                    ]
                                    for mapping in sorted(
                                        mappings.values(),
                                        key=lambda item: coerce_int(item.get("source_tmdb_id"), 0) or 0,
                                    )
                                ],
                                empty_text="暂无手工映射。",
                            )
                        ],
                    ),
                    self._section_card(
                        "最近动作日志",
                        [
                            self._simple_table(
                                ["时间", "级别", "动作", "消息"],
                                [
                                    [
                                        log_entry.get("time") or "-",
                                        self._chip_cell(
                                            str(log_entry.get("level") or "info").upper(),
                                            self._level_color(log_entry.get("level")),
                                            self._level_icon(log_entry.get("level")),
                                        ),
                                        log_entry.get("action") or "-",
                                        self._log_message(log_entry),
                                    ]
                                    for log_entry in logs
                                ],
                                empty_text="暂无动作日志，执行一次手动重扫后会在这里显示。",
                            )
                        ],
                    ),
                ],
            },
        ]
        return content

    def get_service(self) -> List[Dict[str, Any]]:
        services: List[Dict[str, Any]] = []
        if self._enabled and (self._enable_tv or self._enable_movie) and self._cron:
            services.append(
                {
                    "id": "scan",
                    "name": "Next release tracker scan",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.service_scan,
                    "func_kwargs": {"scope": "all", "reason": "cron", "notify": None},
                    "kwargs": {},
                }
            )
        if self._onlyonce:
            services.append(
                {
                    "id": "once",
                    "name": "Next release tracker run once",
                    "trigger": "date",
                    "func": self.run_once_scan,
                    "func_kwargs": {},
                    "kwargs": {
                        "run_date": datetime.now(pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    },
                }
            )
        return services

    @eventmanager.register(EventType.SubscribeComplete)
    def on_subscribe_complete(self, event: Event):
        if not self._enabled:
            return

        event_data = event.event_data or {}
        subscribe_info = event_data.get("subscribe_info") or {}
        mediainfo = event_data.get("mediainfo") or {}
        tmdb_id = coerce_int(self._media_value(mediainfo, "tmdb_id"))
        if not tmdb_id:
            return

        media_type = self._media_type_name(mediainfo)
        if media_type == MediaType.TV.value and self._enable_tv:
            if not self._is_selected_track(media_type, tmdb_id):
                return
            season = coerce_int(subscribe_info.get("season") or self._media_value(mediainfo, "season"), 1) or 1
            track = self._ensure_state_store().acknowledge_tv_completion(
                tmdb_id=tmdb_id,
                title=str(self._media_value(mediainfo, "title") or f"TMDB-{tmdb_id}"),
                year=self._as_str(self._media_value(mediainfo, "year")),
                season=season,
                source="subscribe_complete",
            )
            self._log("success", "subscribe_complete", f"Recorded TV completion: {track['title']} season {season}", {"tmdb_id": tmdb_id})
            return

        if media_type == MediaType.MOVIE.value and self._enable_movie:
            if not self._is_selected_track(media_type, tmdb_id):
                return
            self._track_movie_event(
                tmdb_id=tmdb_id,
                title=str(self._media_value(mediainfo, "title") or f"TMDB-{tmdb_id}"),
                year=self._as_str(self._media_value(mediainfo, "year")),
                collection_id=coerce_int(self._media_value(mediainfo, "collection_id")),
                source="subscribe_complete",
            )

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event):
        if not self._enabled:
            return

        event_data = event.event_data or {}
        mediainfo = event_data.get("mediainfo")
        if not mediainfo:
            return

        tmdb_id = coerce_int(self._media_value(mediainfo, "tmdb_id"))
        if not tmdb_id:
            return

        media_type = self._media_type_name(mediainfo)
        history_id = coerce_int(event_data.get("transfer_history_id"))
        if media_type == MediaType.TV.value and self._enable_tv:
            if not self._is_selected_track(media_type, tmdb_id):
                return
            meta = event_data.get("meta")
            season = parse_season_token(meta) or coerce_int(self._media_value(mediainfo, "season"), 1) or 1
            track = self._ensure_state_store().acknowledge_tv_completion(
                tmdb_id=tmdb_id,
                title=str(self._media_value(mediainfo, "title") or f"TMDB-{tmdb_id}"),
                year=self._as_str(self._media_value(mediainfo, "year")),
                season=season,
                source="transfer_complete",
                last_transfer_history_id=history_id,
            )
            self._log("success", "transfer_complete", f"Recorded TV transfer: {track['title']} season {season}", {"tmdb_id": tmdb_id})
            return

        if media_type == MediaType.MOVIE.value and self._enable_movie:
            if not self._is_selected_track(media_type, tmdb_id):
                return
            self._track_movie_event(
                tmdb_id=tmdb_id,
                title=str(self._media_value(mediainfo, "title") or f"TMDB-{tmdb_id}"),
                year=self._as_str(self._media_value(mediainfo, "year")),
                collection_id=coerce_int(self._media_value(mediainfo, "collection_id")),
                source="transfer_complete",
                last_transfer_history_id=history_id,
            )

    def service_scan(self, scope: str = "all", reason: str = "cron", notify: Optional[bool] = None):
        return self._run_rescan(scope=scope, reason=reason, notify=notify)

    def run_once_scan(self):
        try:
            return self._run_rescan(scope="all", reason="once", notify=self._notify)
        finally:
            if self._onlyonce:
                self._onlyonce = False
                self._save_current_config()

    def stop_service(self):
        pass

    def api_tracks(self) -> Dict[str, Any]:
        return self._ok("tracker state loaded", self._ensure_state_store().snapshot())

    def api_rescan(self, payload: Optional[dict] = Body(default=None)) -> Dict[str, Any]:
        payload = payload or {}
        scope = str(payload.get("scope") or "all").lower()
        notify = payload.get("notify")
        summary = self._run_rescan(scope=scope, reason="api", notify=notify)
        return self._ok("scan completed" if summary.get("success") else "scan failed", summary)

    def api_track_add(self, payload: Optional[dict] = Body(default=None)) -> Dict[str, Any]:
        payload = payload or {}
        tmdb_id = coerce_int(payload.get("tmdb_id"))
        media_type = self._normalize_media_type(payload.get("media_type") or payload.get("type"))
        if not tmdb_id or media_type not in {MediaType.TV.value, MediaType.MOVIE.value}:
            return self._error("tmdb_id and media_type(tv/movie) are required")

        if media_type == MediaType.TV.value:
            season = coerce_int(payload.get("season"))
            if not season:
                return self._error("season is required for TV manual tracking")
            self._select_track_id(media_type, tmdb_id)
            media = self._recognize_media(tmdb_id=tmdb_id, mtype=MediaType.TV)
            title = str(payload.get("title") or self._media_value(media, "title") or f"TMDB-{tmdb_id}")
            year = self._as_str(payload.get("year") or self._media_value(media, "year"))
            track = self._ensure_state_store().acknowledge_tv_completion(
                tmdb_id=tmdb_id,
                title=title,
                year=year,
                season=season,
                source="manual",
                note=self._as_str(payload.get("note")),
            )
            self._log("success", "track_add", f"Added TV track manually: {title} season {season}", {"tmdb_id": tmdb_id})
            return self._ok("tv track added", track)

        self._select_track_id(media_type, tmdb_id)
        media = self._recognize_media(tmdb_id=tmdb_id, mtype=MediaType.MOVIE)
        title = str(payload.get("title") or self._media_value(media, "title") or f"TMDB-{tmdb_id}")
        year = self._as_str(payload.get("year") or self._media_value(media, "year"))
        collection_id = coerce_int(payload.get("collection_id") or self._media_value(media, "collection_id"))
        track = self._ensure_state_store().upsert_movie_track(
            anchor_tmdb_id=tmdb_id,
            title=title,
            year=year,
            source="manual",
            collection_id=collection_id,
            known_tmdb_ids=[tmdb_id],
            note=self._as_str(payload.get("note")),
        )
        track = self._ensure_state_store().acknowledge_movie_completion(
            track_key=track["track_key"],
            tmdb_id=tmdb_id,
            title=title,
            year=year,
            collection_id=collection_id,
            source="manual",
        )
        self._log("success", "track_add", f"Added movie track manually: {title}", {"tmdb_id": tmdb_id, "collection_id": collection_id})
        return self._ok("movie track added", track)

    def api_track_remove(self, payload: Optional[dict] = Body(default=None)) -> Dict[str, Any]:
        payload = payload or {}
        media_type = self._normalize_media_type(payload.get("media_type") or payload.get("type"))
        tmdb_id = coerce_int(payload.get("tmdb_id"))
        collection_id = coerce_int(payload.get("collection_id"))

        if media_type == MediaType.TV.value and tmdb_id:
            selection_removed = self._deselect_track_id(media_type, tmdb_id)
            removed = self._ensure_state_store().remove_tv_track(tmdb_id)
            if not removed and not selection_removed:
                return self._error("tv track not found")
            title = (removed or {}).get("title") or f"TMDB-{tmdb_id}"
            self._log("info", "track_remove", f"Removed TV tracking: {title}", {"tmdb_id": tmdb_id})
            return self._ok("tv tracking removed", {"track": removed, "selection_removed": selection_removed})

        if media_type == MediaType.MOVIE.value and (tmdb_id or collection_id):
            selection_removed = self._deselect_track_id(media_type, tmdb_id) if tmdb_id else False
            removed = self._ensure_state_store().remove_movie_track(anchor_tmdb_id=tmdb_id, collection_id=collection_id)
            removed_anchor_tmdb_id = coerce_int((removed or {}).get("anchor_tmdb_id"))
            if removed_anchor_tmdb_id:
                selection_removed = self._deselect_track_id(media_type, removed_anchor_tmdb_id) or selection_removed
            mapping_source_tmdb_id = tmdb_id or removed_anchor_tmdb_id
            if mapping_source_tmdb_id:
                self._ensure_state_store().remove_manual_mapping(mapping_source_tmdb_id)
                self._save_current_config()
            if not removed and not selection_removed:
                return self._error("movie track not found")
            title = (removed or {}).get("title") or f"TMDB-{tmdb_id or collection_id}"
            self._log("info", "track_remove", f"Removed movie tracking: {title}", {"tmdb_id": tmdb_id, "collection_id": collection_id})
            return self._ok("movie tracking removed", {"track": removed, "selection_removed": selection_removed})

        return self._error("media_type and tmdb_id/collection_id are required")

    def api_import_transfer_history(self, payload: Optional[dict] = Body(default=None)) -> Dict[str, Any]:
        payload = payload or {}
        days = max(coerce_int(payload.get("days"), self._history_days) or self._history_days, 0)
        summary = self._import_transfer_history(days=days, reason="api")
        return self._ok("transfer history imported", summary)

    def api_diagnostic_event(self, payload: Optional[dict] = Body(default=None)) -> Dict[str, Any]:
        payload = payload or {}
        if not self._enabled:
            return self._error("plugin must be enabled before runtime diagnostics can run")

        media_type = self._normalize_media_type(payload.get("media_type") or "tv")
        if media_type != MediaType.TV.value:
            return self._error("runtime diagnostics currently support TV only")

        event_name = str(payload.get("event") or payload.get("event_type") or "transfer_complete").strip().lower()
        if event_name not in {"subscribe_complete", "transfer_complete"}:
            return self._error("event must be subscribe_complete or transfer_complete")

        tmdb_id = coerce_int(payload.get("tmdb_id"), self.diagnostic_tv_tmdb_id) or self.diagnostic_tv_tmdb_id
        season = max(coerce_int(payload.get("season"), 1) or 1, 1)
        title = str(payload.get("title") or self.diagnostic_tv_title)
        year = self._as_str(payload.get("year") or str(datetime.now().year))
        cleanup = bool(payload.get("cleanup", True))
        transfer_history_id = coerce_int(payload.get("transfer_history_id"), 9900001)

        store = self._ensure_state_store()
        store.remove_tv_track(tmdb_id)
        temporary_selection = not self._is_selected_track(MediaType.TV.value, tmdb_id)
        original_selected_tv_ids = list(self._selected_tv_ids)
        if temporary_selection:
            self._selected_tv_ids = normalize_tmdb_id_list([*self._selected_tv_ids, tmdb_id])

        try:
            event_data = self._build_diagnostic_event_payload(
                event_name=event_name,
                tmdb_id=tmdb_id,
                title=title,
                year=year,
                season=season,
                transfer_history_id=transfer_history_id,
            )
            self._dispatch_diagnostic_event(event_name=event_name, event_data=event_data)

            track = None
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                track = store.get_tv_tracks().get(str(tmdb_id))
                if track and int(track.get("latest_season") or 0) >= season:
                    break
                time.sleep(0.05)
            success = bool(track) and int(track.get("latest_season") or 0) >= season
            cleanup_performed = False
            if cleanup and track:
                cleanup_performed = bool(store.remove_tv_track(tmdb_id))
        finally:
            if temporary_selection:
                self._selected_tv_ids = original_selected_tv_ids
                if not cleanup:
                    store.remove_tv_track(tmdb_id)

        result = {
            "success": success,
            "event": event_name,
            "media_type": media_type,
            "tmdb_id": tmdb_id,
            "title": title,
            "year": year,
            "season": season,
            "cleanup": cleanup,
            "cleanup_performed": cleanup_performed,
            "track_detected": bool(track),
            "latest_season": track.get("latest_season") if track else None,
            "checked_at": self._now(),
        }
        if event_name == "transfer_complete":
            result["transfer_history_id"] = transfer_history_id

        store.update_runtime({"last_diagnostic": result})
        context = {
            "tmdb_id": tmdb_id,
            "season": season,
            "cleanup": cleanup,
            "track_detected": bool(track),
            "diagnostic": True,
        }
        if success:
            self._log(
                "success",
                f"diagnostic_{event_name}",
                f"Runtime diagnostic verified: {title} season {season}",
                context,
            )
            return self._ok("runtime diagnostic verified", result)

        self._log(
            "warning",
            f"diagnostic_{event_name}",
            f"Runtime diagnostic did not create the expected track: {title} season {season}",
            context,
        )
        return {
            "success": False,
            "message": "runtime diagnostic did not create the expected track",
            "data": result,
        }

    def api_manual_map_add(self, payload: Optional[dict] = Body(default=None)) -> Dict[str, Any]:
        payload = payload or {}
        source_tmdb_id = coerce_int(payload.get("source_tmdb_id"))
        target_tmdb_ids = payload.get("target_tmdb_ids") or []
        if not source_tmdb_id or not isinstance(target_tmdb_ids, list):
            return self._error("source_tmdb_id and list target_tmdb_ids are required")
        mapping = self._ensure_state_store().set_manual_mapping(
            source_tmdb_id=source_tmdb_id,
            target_tmdb_ids=target_tmdb_ids,
            note=self._as_str(payload.get("note")) or "",
        )
        self._save_current_config()
        self._log("info", "manual_map_add", "movie manual mapping updated", {"source_tmdb_id": source_tmdb_id})
        return self._ok("manual mapping saved", mapping)

    def api_manual_map_remove(self, payload: Optional[dict] = Body(default=None)) -> Dict[str, Any]:
        payload = payload or {}
        source_tmdb_id = coerce_int(payload.get("source_tmdb_id"))
        if not source_tmdb_id:
            return self._error("source_tmdb_id is required")
        mapping = self._ensure_state_store().remove_manual_mapping(source_tmdb_id)
        if not mapping:
            return self._error("manual mapping not found")
        self._save_current_config()
        self._log("info", "manual_map_remove", "movie manual mapping removed", {"source_tmdb_id": source_tmdb_id})
        return self._ok("manual mapping removed", mapping)

    def _run_rescan(self, *, scope: str, reason: str, notify: Optional[bool]) -> Dict[str, Any]:
        scope = scope if scope in {"all", "tv", "movie"} else "all"
        if not self._scan_lock.acquire(blocking=False):
            return {
                "success": False,
                "message": "scan is already running",
                "scope": scope,
                "reason": reason,
            }

        notify_flag = self._notify if notify is None else bool(notify)
        summary: Dict[str, Any] = {
            "success": True,
            "scope": scope,
            "reason": reason,
            "started_at": self._now(),
            "scanned_tv": 0,
            "scanned_movie": 0,
            "tv_candidates": 0,
            "movie_candidates": 0,
            "notifications_sent": 0,
            "tracks_completed": 0,
            "tracks_updated": 0,
            "existing_in_library": 0,
            "existing_subscriptions": 0,
            "errors": 0,
        }

        self._ensure_state_store().update_runtime(
            {
                "last_scan_started_at": summary["started_at"],
                "last_scan_scope": scope,
                "last_scan_reason": reason,
            }
        )

        try:
            if scope in {"all", "tv"} and self._enable_tv:
                self._scan_tv(summary, notify_flag)
            if scope in {"all", "movie"} and self._enable_movie:
                self._scan_movies(summary, notify_flag)
        except Exception as exc:
            logger.exception("[NextReleaseTracker] scan failed")
            summary["success"] = False
            summary["errors"] += 1
            summary["message"] = f"scan failed: {exc}"
            self._log("error", "scan", summary["message"])
        finally:
            summary["finished_at"] = self._now()
            self._ensure_state_store().update_runtime(
                {
                    "last_scan_finished_at": summary["finished_at"],
                    "last_scan_summary": summary,
                }
            )
            self._scan_lock.release()

        if notify_flag and summary["errors"]:
            self._notify_summary(summary)
        return summary

    def _scan_tv(self, summary: Dict[str, Any], notify_flag: bool) -> None:
        tv_tracks = self._ensure_state_store().get_tv_tracks()
        for track in list(tv_tracks.values()):
            tmdb_id = coerce_int(track.get("tmdb_id"))
            if not tmdb_id:
                continue

            summary["scanned_tv"] += 1
            seasons = (self._tmdb_chain or TmdbChain()).tmdb_seasons(tmdb_id) or []
            candidates = select_ready_tv_seasons(
                latest_season=coerce_int(track.get("latest_season"), 0) or 0,
                pending_seasons=track.get("pending_seasons"),
                seasons=seasons,
                grace_days=self._grace_days,
            )
            summary["tv_candidates"] += len(candidates)
            if not candidates:
                continue

            media = self._recognize_media(tmdb_id=tmdb_id, mtype=MediaType.TV)
            exists_info = (self._media_server_chain or MediaServerChain()).media_exists(media) if media else None
            title = track.get("title") or self._media_value(media, "title") or f"TMDB-{tmdb_id}"
            year = self._as_str(track.get("year") or self._media_value(media, "year"))
            current_latest_season = coerce_int(track.get("latest_season"), 0) or 0
            detected_seasons = [candidate.season_number for candidate in candidates]
            resolved_seasons: List[int] = []
            available_seasons: List[int] = []
            status: Optional[str] = None

            for candidate in candidates:
                if season_fully_exists(exists_info, candidate.season_number, candidate.episode_count):
                    self._log("info", "tv_scan", f"Season already in library: {title} season {candidate.season_number}", {"tmdb_id": tmdb_id})
                    summary["existing_in_library"] += 1
                    status = self._merge_release_status(status, "library")
                    resolved_seasons.append(candidate.season_number)
                    continue

                if self._subscribe_exists(tmdb_id=tmdb_id, season=candidate.season_number):
                    self._log("info", "tv_scan", f"Season already subscribed: {title} season {candidate.season_number}", {"tmdb_id": tmdb_id})
                    summary["existing_subscriptions"] += 1
                    status = self._merge_release_status(status, "subscription")
                    resolved_seasons.append(candidate.season_number)
                    continue

                self._log("success", "tv_detected", f"Detected next TV season: {title} season {candidate.season_number}", {"tmdb_id": tmdb_id})
                status = self._merge_release_status(status, "available")
                available_seasons.append(candidate.season_number)

            if not detected_seasons:
                continue
            if notify_flag:
                self._notify_tv_release(
                    title=title,
                    year=year,
                    tmdb_id=tmdb_id,
                    seasons=detected_seasons,
                    status=status or "available",
                )
                summary["notifications_sent"] += 1
                self._complete_tv_tracking(tmdb_id)
                summary["tracks_completed"] += 1
                continue

            if available_seasons:
                first_available_season = min(available_seasons)
                resolved_before_pending = [
                    season for season in resolved_seasons
                    if season < first_available_season
                ]
                completed_latest_season = max(resolved_before_pending, default=current_latest_season)
                if completed_latest_season > current_latest_season:
                    self._ensure_state_store().acknowledge_tv_completion(
                        tmdb_id=tmdb_id,
                        title=title,
                        year=year,
                        season=completed_latest_season,
                        source="scan",
                    )
                for season in available_seasons:
                    self._ensure_state_store().mark_tv_pending(tmdb_id, season)
                summary["tracks_updated"] += 1
                continue

            self._complete_tv_tracking(tmdb_id)
            summary["tracks_completed"] += 1

    def _scan_movies(self, summary: Dict[str, Any], notify_flag: bool) -> None:
        movie_tracks = self._ensure_state_store().get_movie_tracks()
        for track_key, track in list(movie_tracks.items()):
            summary["scanned_movie"] += 1
            item_map: Dict[int, MediaInfo] = {}

            collection_id = coerce_int(track.get("collection_id"))
            if collection_id:
                for item in (self._tmdb_chain or TmdbChain()).tmdb_collection(collection_id) or []:
                    item_tmdb_id = coerce_int(self._media_value(item, "tmdb_id"))
                    if item_tmdb_id:
                        item_map[item_tmdb_id] = item

            manual_mapping = self._ensure_state_store().get_manual_mapping(coerce_int(track.get("anchor_tmdb_id"), 0) or 0)
            for target_tmdb_id in (manual_mapping or {}).get("target_tmdb_ids", []):
                target_tmdb_id = coerce_int(target_tmdb_id)
                if not target_tmdb_id or target_tmdb_id in item_map:
                    continue
                media = self._recognize_media(tmdb_id=target_tmdb_id, mtype=MediaType.MOVIE)
                if media:
                    item_map[target_tmdb_id] = media

            candidates = select_ready_collection_movies(
                known_tmdb_ids=track.get("known_tmdb_ids"),
                pending_tmdb_ids=track.get("pending_tmdb_ids"),
                items=item_map.values(),
                grace_days=self._grace_days,
            )
            summary["movie_candidates"] += len(candidates)
            if not candidates:
                continue

            detected_candidates = []
            resolved_candidates = []
            available_candidates = []
            status: Optional[str] = None
            for candidate in candidates:
                media = item_map.get(candidate.tmdb_id)
                title = candidate.title or track.get("title") or f"TMDB-{candidate.tmdb_id}"
                year = candidate.year or self._as_str(track.get("year"))
                exists_info = (self._media_server_chain or MediaServerChain()).media_exists(media) if media else None
                detected_candidates.append(candidate)

                if exists_info:
                    self._log("info", "movie_scan", f"Movie already in library: {title}", {"tmdb_id": candidate.tmdb_id})
                    summary["existing_in_library"] += 1
                    status = self._merge_release_status(status, "library")
                    resolved_candidates.append(candidate)
                    continue

                if self._subscribe_exists(tmdb_id=candidate.tmdb_id):
                    self._log("info", "movie_scan", f"Movie already subscribed: {title}", {"tmdb_id": candidate.tmdb_id})
                    summary["existing_subscriptions"] += 1
                    status = self._merge_release_status(status, "subscription")
                    resolved_candidates.append(candidate)
                    continue

                self._log("success", "movie_detected", f"Detected next movie release: {title}", {"tmdb_id": candidate.tmdb_id})
                status = self._merge_release_status(status, "available")
                available_candidates.append(candidate)

            if not detected_candidates:
                continue
            if notify_flag:
                self._notify_movie_release(
                    title=track.get("title") or f"TMDB-{track.get('anchor_tmdb_id')}",
                    anchor_tmdb_id=coerce_int(track.get("anchor_tmdb_id"), 0) or 0,
                    collection_id=collection_id,
                    candidates=detected_candidates,
                    status=status or "available",
                )
                summary["notifications_sent"] += 1
                self._complete_movie_tracking(track_key=track_key, anchor_tmdb_id=coerce_int(track.get("anchor_tmdb_id"), 0) or 0)
                summary["tracks_completed"] += 1
                continue

            if available_candidates:
                current_track_key = track_key
                for candidate in resolved_candidates:
                    current_track = self._ensure_state_store().acknowledge_movie_completion(
                        track_key=current_track_key,
                        tmdb_id=candidate.tmdb_id,
                        title=candidate.title,
                        year=candidate.year,
                        source="scan",
                        collection_id=candidate.collection_id or collection_id,
                    )
                    current_track_key = current_track["track_key"]
                for candidate in available_candidates:
                    self._ensure_state_store().mark_movie_pending(current_track_key, candidate.tmdb_id)
                summary["tracks_updated"] += 1
                continue

            self._complete_movie_tracking(track_key=track_key, anchor_tmdb_id=coerce_int(track.get("anchor_tmdb_id"), 0) or 0)
            summary["tracks_completed"] += 1

    def _import_transfer_history(self, *, days: int, reason: str) -> Dict[str, Any]:
        cutoff = datetime.now() - timedelta(days=max(days, 0))
        histories = TransferHistoryOper().list_by_date(cutoff.strftime("%Y-%m-%d %H:%M:%S")) or []
        selected_tv = set(self._selected_tv_ids)
        selected_movie = set(self._selected_movie_ids)
        summary = {
            "days": days,
            "reason": reason,
            "records": 0,
            "tv_imported": 0,
            "movie_imported": 0,
            "errors": 0,
        }

        for history in histories:
            if not getattr(history, "status", False):
                continue
            tmdb_id = coerce_int(getattr(history, "tmdbid", None))
            media_type = str(getattr(history, "type", "") or "")
            if not tmdb_id:
                continue

            try:
                if media_type == MediaType.TV.value and self._enable_tv:
                    if tmdb_id not in selected_tv:
                        continue
                    season = parse_season_token(getattr(history, "seasons", None)) or 1
                    self._ensure_state_store().acknowledge_tv_completion(
                        tmdb_id=tmdb_id,
                        title=str(getattr(history, "title", "") or f"TMDB-{tmdb_id}"),
                        year=self._as_str(getattr(history, "year", None)),
                        season=season,
                        source="history_import",
                        last_transfer_history_id=coerce_int(getattr(history, "id", None)),
                    )
                    summary["records"] += 1
                    summary["tv_imported"] += 1
                    continue

                if media_type == MediaType.MOVIE.value and self._enable_movie:
                    if tmdb_id not in selected_movie:
                        continue
                    media = self._recognize_media(tmdb_id=tmdb_id, mtype=MediaType.MOVIE)
                    title = self._as_str(getattr(history, "title", None)) or self._media_value(media, "title") or f"TMDB-{tmdb_id}"
                    year = self._as_str(getattr(history, "year", None)) or self._as_str(self._media_value(media, "year"))
                    collection_id = coerce_int(self._media_value(media, "collection_id"))
                    track = self._ensure_state_store().upsert_movie_track(
                        anchor_tmdb_id=tmdb_id,
                        title=title,
                        year=year,
                        source="history_import",
                        collection_id=collection_id,
                        known_tmdb_ids=[tmdb_id],
                        last_transfer_history_id=coerce_int(getattr(history, "id", None)),
                    )
                    self._ensure_state_store().acknowledge_movie_completion(
                        track_key=track["track_key"],
                        tmdb_id=tmdb_id,
                        title=title,
                        year=year,
                        collection_id=collection_id,
                        source="history_import",
                    )
                    summary["records"] += 1
                    summary["movie_imported"] += 1
            except Exception as exc:
                logger.exception("[NextReleaseTracker] history import failed")
                self._log("error", "history_import", f"Failed to import transfer history: {exc}", {"tmdb_id": tmdb_id})
                summary["errors"] += 1

        self._ensure_state_store().update_runtime({"last_history_import": summary})
        self._log("info", "history_import", f"Transfer history import completed: tv={summary['tv_imported']}, movie={summary['movie_imported']}, errors={summary['errors']}", {"days": days, "reason": reason})
        return summary

    def _track_movie_event(
        self,
        *,
        tmdb_id: int,
        title: str,
        year: Optional[str],
        collection_id: Optional[int],
        source: str,
        last_transfer_history_id: Optional[int] = None,
    ) -> dict:
        track = self._ensure_state_store().upsert_movie_track(
            anchor_tmdb_id=tmdb_id,
            title=title,
            year=year,
            source=source,
            collection_id=collection_id,
            known_tmdb_ids=[tmdb_id],
            last_transfer_history_id=last_transfer_history_id,
        )
        track = self._ensure_state_store().acknowledge_movie_completion(
            track_key=track["track_key"],
            tmdb_id=tmdb_id,
            title=title,
            year=year,
            collection_id=collection_id,
            source=source,
        )
        self._log("success", source, f"Recorded movie anchor: {title}", {"tmdb_id": tmdb_id, "collection_id": collection_id})
        return track

    def _notify_summary(self, summary: Dict[str, Any]) -> None:
        text = (
            f"scope={summary.get('scope')} | "
            f"notifications={summary.get('notifications_sent', 0)} | "
            f"completed={summary.get('tracks_completed', 0)} | "
            f"updated={summary.get('tracks_updated', 0)} | "
            f"in_library={summary.get('existing_in_library', 0)} | "
            f"existing_subscriptions={summary.get('existing_subscriptions', 0)} | "
            f"errors={summary.get('errors', 0)}"
        )
        self.post_message(mtype=NotificationType.Plugin, title="追剧助手", text=text)

    @staticmethod
    def _coerce_bool(value: Any, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if not text:
            return default
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return default

    def _normalize_cron_expr(self, value: Any) -> str:
        default = "0 3 * * 1"
        text = str(value or default).strip() or default
        try:
            CronTrigger.from_crontab(text)
        except Exception:
            logger.warning(f"[NextReleaseTracker] invalid cron expression ignored: {text}")
            return default
        return text

    def _normalize_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "enabled": self._coerce_bool(config.get("enabled"), False),
            "notify": self._coerce_bool(config.get("notify"), True),
            "enable_tv": self._coerce_bool(config.get("enable_tv"), True),
            "enable_movie": self._coerce_bool(config.get("enable_movie"), False),
            "backfill_on_enable": self._coerce_bool(config.get("backfill_on_enable"), False),
            "onlyonce": self._coerce_bool(config.get("onlyonce"), False),
            "cron": self._normalize_cron_expr(config.get("cron")),
            "grace_days": max(coerce_int(config.get("grace_days"), 3) or 3, 0),
            "history_days": max(coerce_int(config.get("history_days"), 365) or 365, 0),
            "log_retention": max(coerce_int(config.get("log_retention"), 200) or 200, 20),
            "tracked_tv_ids": self._serialize_track_selection(self._parse_track_selection(config.get("tracked_tv_ids"))),
            "tracked_movie_ids": self._serialize_track_selection(self._parse_track_selection(config.get("tracked_movie_ids"))),
            "manual_movie_mappings": self._serialize_manual_mapping_text(
                self._parse_manual_mapping_text(config.get("manual_movie_mappings"))
            ),
        }

    def _current_config_snapshot(self) -> Dict[str, Any]:
        current_mappings = self._state_store.get_manual_mappings() if self._state_store else {}
        return {
            "enabled": self._enabled,
            "notify": self._notify,
            "enable_tv": self._enable_tv,
            "enable_movie": self._enable_movie,
            "backfill_on_enable": self._backfill_on_enable,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "grace_days": self._grace_days,
            "history_days": self._history_days,
            "log_retention": self._log_retention,
            "tracked_tv_ids": self._serialize_track_selection(self._selected_tv_ids),
            "tracked_movie_ids": self._serialize_track_selection(self._selected_movie_ids),
            "manual_movie_mappings": self._serialize_manual_mapping_text(current_mappings),
        }

    def _config_needs_cleanup(self, config: Dict[str, Any], normalized_config: Dict[str, Any]) -> bool:
        if any(key not in self.CONFIG_FIELDS for key in config.keys()):
            return True
        return any(config.get(key) != value for key, value in normalized_config.items())

    def _save_current_config(self) -> None:
        self.update_config(self._current_config_snapshot())

    def _sync_manual_mappings_from_text(self, value: Any) -> None:
        store = self._ensure_state_store()
        parsed_mappings = self._parse_manual_mapping_text(value)
        existing_mappings = store.get_manual_mappings()

        existing_source_ids = {
            coerce_int(mapping.get("source_tmdb_id"), 0) or coerce_int(source_key, 0) or 0
            for source_key, mapping in existing_mappings.items()
        }
        for source_tmdb_id in sorted(source_id for source_id in existing_source_ids if source_id and source_id not in parsed_mappings):
            store.remove_manual_mapping(source_tmdb_id)

        for source_tmdb_id, mapping in parsed_mappings.items():
            current = store.get_manual_mapping(source_tmdb_id) or {}
            current_targets = self._parse_track_selection(current.get("target_tmdb_ids"))
            current_note = self._normalize_manual_mapping_note(current.get("note"))
            if current_targets == mapping["target_tmdb_ids"] and current_note == mapping["note"]:
                continue
            store.set_manual_mapping(
                source_tmdb_id=source_tmdb_id,
                target_tmdb_ids=mapping["target_tmdb_ids"],
                note=mapping["note"],
            )

    @staticmethod
    def _normalize_manual_mapping_note(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @classmethod
    def _parse_manual_mapping_text(cls, value: Any) -> Dict[int, Dict[str, Any]]:
        mappings: Dict[int, Dict[str, Any]] = {}
        for raw_line in str(value or "").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            note = ""
            if "#" in line:
                line, note = line.split("#", 1)
            match = re.match(r"^\s*(\d+)\s*(?:=|=>|->|:|：)\s*(.+?)\s*$", line.strip())
            if not match:
                continue

            source_tmdb_id = coerce_int(match.group(1))
            target_tmdb_ids = cls._parse_track_selection(match.group(2))
            if not source_tmdb_id or not target_tmdb_ids:
                continue

            mappings[int(source_tmdb_id)] = {
                "source_tmdb_id": int(source_tmdb_id),
                "target_tmdb_ids": target_tmdb_ids,
                "note": cls._normalize_manual_mapping_note(note),
            }
        return mappings

    @classmethod
    def _serialize_manual_mapping_text(cls, mappings: Dict[Any, dict]) -> str:
        lines: List[str] = []
        for mapping in sorted(
            mappings.values(),
            key=lambda item: coerce_int(item.get("source_tmdb_id"), 0) or 0,
        ):
            source_tmdb_id = coerce_int(mapping.get("source_tmdb_id"))
            target_tmdb_ids = cls._parse_track_selection(mapping.get("target_tmdb_ids"))
            if not source_tmdb_id or not target_tmdb_ids:
                continue

            line = f"{source_tmdb_id}={','.join(str(item) for item in target_tmdb_ids)}"
            note = cls._normalize_manual_mapping_note(mapping.get("note"))
            if note:
                line = f"{line} # {note}"
            lines.append(line)
        return "\n".join(lines)

    def _sync_selected_track_state(self) -> None:
        store = self._ensure_state_store()
        selected_tv = set(self._selected_tv_ids)
        for track in list(store.get_tv_tracks().values()):
            tmdb_id = coerce_int(track.get("tmdb_id"))
            if tmdb_id and tmdb_id not in selected_tv:
                store.remove_tv_track(tmdb_id)

        selected_movie = set(self._selected_movie_ids)
        for track_key, track in list(store.get_movie_tracks().items()):
            anchor_tmdb_id = coerce_int(track.get("anchor_tmdb_id"))
            if anchor_tmdb_id and anchor_tmdb_id not in selected_movie:
                store.remove_movie_track(track_key=track_key)

    def _is_selected_track(self, media_type: str, tmdb_id: int) -> bool:
        if media_type == MediaType.TV.value:
            return int(tmdb_id) in set(self._selected_tv_ids)
        if media_type == MediaType.MOVIE.value:
            return int(tmdb_id) in set(self._selected_movie_ids)
        return False

    def _select_track_id(self, media_type: str, tmdb_id: int) -> bool:
        if media_type == MediaType.TV.value:
            if tmdb_id in self._selected_tv_ids:
                return False
            self._selected_tv_ids = normalize_tmdb_id_list([*self._selected_tv_ids, tmdb_id])
            self._save_current_config()
            return True
        if media_type == MediaType.MOVIE.value:
            if tmdb_id in self._selected_movie_ids:
                return False
            self._selected_movie_ids = normalize_tmdb_id_list([*self._selected_movie_ids, tmdb_id])
            self._save_current_config()
            return True
        return False

    def _deselect_track_id(self, media_type: str, tmdb_id: Optional[int]) -> bool:
        if not tmdb_id:
            return False
        if media_type == MediaType.TV.value and tmdb_id in self._selected_tv_ids:
            self._selected_tv_ids = [candidate for candidate in self._selected_tv_ids if candidate != int(tmdb_id)]
            self._save_current_config()
            return True
        if media_type == MediaType.MOVIE.value and tmdb_id in self._selected_movie_ids:
            self._selected_movie_ids = [candidate for candidate in self._selected_movie_ids if candidate != int(tmdb_id)]
            self._save_current_config()
            return True
        return False

    def _bootstrap_selected_tracks_from_local_catalog(self) -> None:
        if not (self._selected_tv_ids or self._selected_movie_ids):
            return
        store = self._ensure_state_store()
        tv_lookup, movie_lookup = self._local_candidate_lookup()

        if self._enable_tv:
            existing_tv_tracks = store.get_tv_tracks()
            for tmdb_id in self._selected_tv_ids:
                if str(tmdb_id) in existing_tv_tracks:
                    continue
                candidate = tv_lookup.get(int(tmdb_id))
                season = coerce_int((candidate or {}).get("latest_season"), 0) or 0
                if season <= 0:
                    continue
                store.acknowledge_tv_completion(
                    tmdb_id=tmdb_id,
                    title=str((candidate or {}).get("title") or f"TMDB-{tmdb_id}"),
                    year=self._as_str((candidate or {}).get("year")),
                    season=season,
                    source="selection_bootstrap",
                )

        if self._enable_movie:
            existing_movie_tracks = store.get_movie_tracks()
            for tmdb_id in self._selected_movie_ids:
                if any(coerce_int(track.get("anchor_tmdb_id")) == int(tmdb_id) for track in existing_movie_tracks.values()):
                    continue
                candidate = movie_lookup.get(int(tmdb_id))
                if not candidate:
                    continue
                media = self._recognize_media(tmdb_id=tmdb_id, mtype=MediaType.MOVIE)
                title = self._as_str((candidate or {}).get("title")) or self._as_str(self._media_value(media, "title")) or f"TMDB-{tmdb_id}"
                year = self._as_str((candidate or {}).get("year")) or self._as_str(self._media_value(media, "year"))
                collection_id = coerce_int(self._media_value(media, "collection_id"))
                track = store.upsert_movie_track(
                    anchor_tmdb_id=tmdb_id,
                    title=title,
                    year=year,
                    source="selection_bootstrap",
                    collection_id=collection_id,
                    known_tmdb_ids=[tmdb_id],
                )
                store.acknowledge_movie_completion(
                    track_key=track["track_key"],
                    tmdb_id=tmdb_id,
                    title=title,
                    year=year,
                    collection_id=collection_id,
                    source="selection_bootstrap",
                )

    def _complete_tv_tracking(self, tmdb_id: int) -> None:
        self._ensure_state_store().remove_tv_track(tmdb_id)
        self._deselect_track_id(MediaType.TV.value, tmdb_id)

    def _complete_movie_tracking(self, *, track_key: str, anchor_tmdb_id: int) -> None:
        self._ensure_state_store().remove_movie_track(track_key=track_key)
        self._deselect_track_id(MediaType.MOVIE.value, anchor_tmdb_id)

    def _notify_tv_release(
        self,
        *,
        title: str,
        year: Optional[str],
        tmdb_id: int,
        seasons: List[int],
        status: str,
    ) -> None:
        season_text = ", ".join(f"S{int(season):02d}" for season in normalize_tmdb_id_list(seasons))
        label = f"{title} ({year})" if year else title
        text = (
            f"{label} | TMDB {tmdb_id} | 新季 {season_text} | "
            f"{self._release_status_text(status)} | 已结束本条追踪"
        )
        self.post_message(mtype=NotificationType.Plugin, title="追剧助手", text=text)

    def _notify_movie_release(
        self,
        *,
        title: str,
        anchor_tmdb_id: int,
        collection_id: Optional[int],
        candidates: List[Any],
        status: str,
    ) -> None:
        candidate_text = ", ".join(
            f"{candidate.title}({candidate.year or '-'})"
            for candidate in candidates
        )
        collection_text = f" | Collection {collection_id}" if collection_id else ""
        text = (
            f"{title} | 锚点 TMDB {anchor_tmdb_id}{collection_text} | 续作 {candidate_text} | "
            f"{self._release_status_text(status)} | 已结束本条追踪"
        )
        self.post_message(mtype=NotificationType.Plugin, title="追剧助手", text=text)

    @staticmethod
    def _merge_release_status(current: Optional[str], candidate: str) -> str:
        priority = {"library": 0, "subscription": 1, "available": 2}
        if current is None:
            return candidate
        return candidate if priority.get(candidate, -1) >= priority.get(current, -1) else current

    @staticmethod
    def _release_status_text(status: str) -> str:
        if status == "library":
            return "已在媒体库中发现"
        if status == "subscription":
            return "已存在订阅"
        return "发现可处理的新条目"

    @staticmethod
    def _parse_track_selection(value: Any) -> List[int]:
        if value is None or value == "":
            return []
        if isinstance(value, (list, tuple, set)):
            return normalize_tmdb_id_list(list(value))
        parts = re.split(r"[\s,;|]+", str(value).strip())
        return normalize_tmdb_id_list(parts)

    @staticmethod
    def _serialize_track_selection(values: List[int]) -> str:
        return "\n".join(str(value) for value in normalize_tmdb_id_list(values))

    @staticmethod
    def _form_section_card(title: str, subtitle: str, content: List[dict], show_expr: Optional[str] = None) -> dict:
        props: Dict[str, Any] = {"variant": "outlined", "class": "mb-4"}
        if show_expr:
            props["show"] = show_expr
        return {
            "component": "VCard",
            "props": props,
            "content": [
                {
                    "component": "VCardItem",
                    "content": [
                        {"component": "VCardTitle", "text": title},
                        {"component": "VCardSubtitle", "text": subtitle},
                    ],
                },
                {"component": "VCardText", "content": content},
            ],
        }

    @staticmethod
    def _form_action_button(text: str, color: str, icon: str, on_click: str, button_class: str = "") -> dict:
        props: Dict[str, Any] = {
            "color": color,
            "variant": "tonal",
            "prepend-icon": icon,
            "block": True,
            "onClick": on_click,
        }
        if button_class:
            props["class"] = button_class
        return {"component": "VBtn", "props": props, "text": text}

    def _form_selection_editor(
        self,
        *,
        title: str,
        subtitle: str,
        media_label: str,
        model_key: str,
        add_model: str,
        remove_model: str,
        search_model: str,
        placeholder: str,
        saved_summary: dict,
        candidates: List[Dict[str, Any]],
        hidden_candidate_count: int,
        candidate_note: str,
        show_expr: str,
    ) -> dict:
        candidate_content: List[dict] = [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": f"下表按最近时间排序。你可以在表头搜片名、年份或编号，快速找到想加的{media_label}。",
                },
            },
            {
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "variant": "tonal",
                    "text": candidate_note,
                },
            },
            self._candidate_selection_table(
                media_label=media_label,
                model_key=model_key,
                search_model=search_model,
                candidates=candidates,
            ),
        ]
        if hidden_candidate_count:
            candidate_content.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": f"当前先显示最近 {len(candidates)} 项；如果你要找的内容不在这里，也可以直接手动补编号。",
                    },
                }
            )
        return self._form_section_card(
            title=title,
            subtitle=subtitle,
            show_expr=show_expr,
            content=[
                saved_summary,
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": self._selection_summary_expr(model_key, media_label),
                    },
                },
                *candidate_content,
                {
                    "component": "VRow",
                    "content": [
                        self._col(
                            8,
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": add_model,
                                    "label": f"手动输入{media_label}编号（高级）",
                                    "placeholder": placeholder,
                                    "clearable": True,
                                },
                            },
                        ),
                        self._col(
                            4,
                            self._form_action_button(
                                f"手动加入{media_label}名单",
                                "primary",
                                "mdi-plus",
                                self._selection_add_expr(model_key, add_model),
                                "mt-md-6",
                            ),
                        ),
                    ],
                },
                {
                    "component": "VRow",
                    "content": [
                        self._col(
                            6,
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": remove_model,
                                    "label": f"移除{media_label}编号（高级）",
                                    "placeholder": placeholder,
                                    "clearable": True,
                                },
                            },
                        ),
                        self._col(
                            3,
                            self._form_action_button(
                                "移除这个编号",
                                "warning",
                                "mdi-minus",
                                self._selection_remove_expr(model_key, remove_model),
                                "mt-md-6",
                            ),
                        ),
                        self._col(
                            3,
                            self._form_action_button(
                                "清空白名单",
                                "error",
                                "mdi-delete-outline",
                                self._selection_clear_expr(model_key, add_model, remove_model),
                                "mt-md-6",
                            ),
                        ),
                    ],
                },
                {
                    "component": "VTextarea",
                    "props": {
                        "model": model_key,
                        "label": f"已选{media_label}编号（高级）",
                        "placeholder": f"一行一个，例如 {placeholder}",
                        "rows": 5,
                        "auto-grow": True,
                        "messages": self._selection_messages_expr(model_key, media_label),
                    },
                },
                {
                    "component": "VAlert",
                    "props": {
                        "type": "warning",
                        "variant": "tonal",
                        "text": "这里的点选和按钮只是先改当前表单；记得点“保存”，名单才会真正更新。",
                    },
                },
            ],
        )

    def _selection_snapshot_alert(
        self,
        media_label: str,
        selected_ids: List[int],
        track_lookup: Dict[str, Any],
        is_movie: bool,
    ) -> dict:
        preview_lines: List[str] = []
        for tmdb_id in selected_ids[:5]:
            track = track_lookup.get(str(tmdb_id)) or {}
            if is_movie:
                if track:
                    pending_count = len(track.get("pending_tmdb_ids") or [])
                    pending_text = f"，待留意 {pending_count} 部" if pending_count else ""
                    preview_lines.append(
                        f"{track.get('title') or f'编号 {tmdb_id}'}（已找到系列{pending_text}）"
                    )
                else:
                    preview_lines.append(f"编号 {tmdb_id}（已加入，等待第一次命中）")
            else:
                if track:
                    latest_season = coerce_int(track.get("latest_season"), 0) or 0
                    season_text = f"S{latest_season:02d}" if latest_season else "-"
                    pending_count = len(track.get("pending_seasons") or [])
                    pending_text = f"，待留意 {pending_count} 季" if pending_count else ""
                    preview_lines.append(
                        f"{track.get('title') or f'编号 {tmdb_id}'}（当前到 {season_text}{pending_text}）"
                    )
                else:
                    preview_lines.append(f"编号 {tmdb_id}（已加入，等待第一次命中）")

        if not selected_ids:
            text = f"当前还没加入任何{media_label}。把想追的内容加到这里后，插件才会开始帮你留意更新。"
            alert_type = "info"
        else:
            extra = f"；另有 {len(selected_ids) - len(preview_lines)} 项" if len(selected_ids) > len(preview_lines) else ""
            text = f"当前已加入 {len(selected_ids)} 个{media_label}：{'；'.join(preview_lines)}{extra}"
            alert_type = "success"
        return {"component": "VAlert", "props": {"type": alert_type, "variant": "tonal", "text": text}}

    @staticmethod
    def _selection_model_parse_js(model_key: str) -> str:
        return (
            f"Array.from(new Set(String(model.{model_key} || '')"
            ".split(/[\\s,;|]+/)"
            ".map((item) => Number(String(item).trim()))"
            ".filter((item) => Number.isInteger(item) && item > 0)))"
        )

    @staticmethod
    def _js_value(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    @classmethod
    def _selection_summary_expr(cls, model_key: str, media_label: str) -> str:
        parsed = cls._selection_model_parse_js(model_key)
        return (
            "{{ (() => { "
            f"const ids = {parsed}; "
            f"return ids.length ? `当前已选 ${{ids.length}} 个{media_label}：${{ids.join(', ')}}` : "
            f"`当前还没加入任何{media_label}。`; "
            "})() }}"
        )

    @classmethod
    def _selection_messages_expr(cls, model_key: str, media_label: str) -> str:
        parsed = cls._selection_model_parse_js(model_key)
        return (
            "{{ (() => { "
            f"const ids = {parsed}; "
            f"return ids.length ? [`已识别 ${{ids.length}} 个有效{media_label}编号`] : ['一行填一个编号；点保存后才会真正生效']; "
            "})() }}"
        )

    @classmethod
    def _selection_contains_expr(cls, model_key: str, tmdb_id: int) -> str:
        parsed = cls._selection_model_parse_js(model_key)
        return "{{ (() => { " f"const ids = {parsed}; " f"return ids.includes({int(tmdb_id)}); " "})() }}"

    @classmethod
    def _selection_add_expr(cls, model_key: str, add_model: str) -> str:
        parsed = cls._selection_model_parse_js(model_key)
        return (
            "(event) => { "
            f"const candidate = Number(String(model.{add_model} || '').trim()); "
            "if (!Number.isInteger(candidate) || candidate <= 0) { return; } "
            f"const ids = {parsed}; "
            "if (!ids.includes(candidate)) { ids.push(candidate); } "
            "model."
            f"{model_key}"
            " = ids.join('\\n'); "
            "model."
            f"{add_model}"
            " = ''; "
            "}"
        )

    @classmethod
    def _selection_add_fixed_expr(cls, model_key: str, tmdb_id: int) -> str:
        parsed = cls._selection_model_parse_js(model_key)
        return (
            "(event) => { "
            f"const ids = {parsed}; "
            f"if (!ids.includes({int(tmdb_id)})) {{ ids.push({int(tmdb_id)}); }} "
            f"model.{model_key} = ids.join('\\n'); "
            "}"
        )

    @classmethod
    def _selection_remove_expr(cls, model_key: str, remove_model: str) -> str:
        parsed = cls._selection_model_parse_js(model_key)
        return (
            "(event) => { "
            f"const candidate = Number(String(model.{remove_model} || '').trim()); "
            "if (!Number.isInteger(candidate) || candidate <= 0) { return; } "
            f"const ids = {parsed}.filter((item) => item !== candidate); "
            "model."
            f"{model_key}"
            " = ids.join('\\n'); "
            "model."
            f"{remove_model}"
            " = ''; "
            "}"
        )

    @staticmethod
    def _selection_clear_expr(model_key: str, add_model: str, remove_model: str) -> str:
        return (
            "(event) => { "
            f"model.{model_key} = ''; "
            f"model.{add_model} = ''; "
            f"model.{remove_model} = ''; "
            "}"
        )

    def _selected_tv_rows(self, tv_tracks: Dict[str, Any]) -> List[List[Any]]:
        rows: List[List[Any]] = []
        for tmdb_id in self._selected_tv_ids:
            track = tv_tracks.get(str(tmdb_id)) or {}
            latest_season = coerce_int(track.get("latest_season"), 0) or 0
            latest_text = f"S{latest_season:02d}" if latest_season else "-"
            progress = (
                f"当前到 {latest_text} / 待留意 {self._join_values(track.get('pending_seasons'))}"
                if track
                else "等待第一次记录"
            )
            rows.append(
                [
                    track.get("title") or f"TMDB-{tmdb_id}",
                    tmdb_id,
                    self._selection_status_cell(bool(track)),
                    progress,
                    self._page_track_remove_button(MediaType.TV.value, tmdb_id),
                ]
            )
        return rows

    def _selected_movie_rows(self, movie_tracks: Dict[str, Any]) -> List[List[Any]]:
        rows: List[List[Any]] = []
        for tmdb_id in self._selected_movie_ids:
            track = movie_tracks.get(f"movie:{tmdb_id}") or {}
            if not track:
                for candidate in movie_tracks.values():
                    if coerce_int(candidate.get("anchor_tmdb_id")) == tmdb_id:
                        track = candidate
                        break
            progress = (
                f"已找到系列 / 待留意 {len(track.get('pending_tmdb_ids') or [])} 部"
                if track
                else "等待第一次记录"
            )
            rows.append(
                [
                    track.get("title") or f"TMDB-{tmdb_id}",
                    tmdb_id,
                    self._selection_status_cell(bool(track)),
                    progress,
                    self._page_track_remove_button(
                        MediaType.MOVIE.value,
                        tmdb_id,
                        coerce_int(track.get("collection_id")) if track else None,
                    ),
                ]
            )
        return rows

    @staticmethod
    def _selection_status_cell(active: bool) -> dict:
        return (
            NextReleaseTracker._chip_cell("追踪中", "success", "mdi-radar")
            if active
            else NextReleaseTracker._chip_cell("已加入待命", "info", "mdi-playlist-plus")
        )

    def _page_track_remove_button(
        self,
        media_type: str,
        tmdb_id: Optional[int],
        collection_id: Optional[int] = None,
    ) -> Any:
        if not tmdb_id and not collection_id:
            return "-"
        plugin_api = f"plugin/{self.__class__.__name__}"
        payload: Dict[str, Any] = {"media_type": media_type}
        if tmdb_id:
            payload["tmdb_id"] = tmdb_id
        if collection_id:
            payload["collection_id"] = collection_id
        return self._action_button(
            text="移出白名单",
            api=f"{plugin_api}/track/remove?apikey={settings.API_TOKEN}",
            payload=payload,
            color="error",
        )

    def _page_manual_map_remove_button(self, source_tmdb_id: Optional[int]) -> Any:
        if not source_tmdb_id:
            return "-"
        plugin_api = f"plugin/{self.__class__.__name__}"
        return self._action_button(
            text="删除映射",
            api=f"{plugin_api}/manual_map/remove?apikey={settings.API_TOKEN}",
            payload={"source_tmdb_id": source_tmdb_id},
            color="warning",
        )

    def _subscribe_exists(self, *, tmdb_id: int, season: Optional[int] = None) -> bool:
        return SubscribeOper().exists(tmdbid=tmdb_id, season=season)

    def _recognize_media(self, *, tmdb_id: int, mtype: MediaType) -> Optional[MediaInfo]:
        try:
            return self.chain.recognize_media(tmdbid=tmdb_id, mtype=mtype, cache=False)
        except Exception as exc:
            logger.warning(f"[NextReleaseTracker] media recognition failed for {tmdb_id}: {exc}")
            return None

    def _ensure_state_store(self) -> TrackerStateStore:
        if not self._state_store:
            self._state_store = TrackerStateStore(
                self.get_data,
                self.save_data,
                self.del_data,
                log_limit=self._log_retention,
            )
        return self._state_store

    def _log(self, level: str, action: str, message: str, context: Optional[dict] = None) -> None:
        self._ensure_state_store().append_action(
            level=level,
            action=action,
            message=message,
            context=context or {},
        )
        if level == "error":
            logger.error(f"[NextReleaseTracker] {action}: {message}")
        elif level == "warning":
            logger.warning(f"[NextReleaseTracker] {action}: {message}")
        else:
            logger.info(f"[NextReleaseTracker] {action}: {message}")

    @staticmethod
    def _ok(message: str, data: Any = None) -> Dict[str, Any]:
        return {"success": True, "message": message, "data": data}

    @staticmethod
    def _error(message: str) -> Dict[str, Any]:
        return {"success": False, "message": message}

    @staticmethod
    def _media_value(media: Any, key: str) -> Any:
        if media is None:
            return None
        if isinstance(media, dict):
            return media.get(key)
        return getattr(media, key, None)

    @staticmethod
    def _media_type_name(media: Any) -> str:
        media_type = NextReleaseTracker._media_value(media, "type")
        try:
            if isinstance(media_type, MediaType):
                return media_type.value
        except TypeError:
            pass
        return str(media_type or "")

    @staticmethod
    def _normalize_media_type(value: Any) -> str:
        text = str(value or "").strip().lower()
        if text == "tv":
            return MediaType.TV.value
        if text == "movie":
            return MediaType.MOVIE.value
        return str(value or "").strip()

    @staticmethod
    def _as_str(value: Any) -> Optional[str]:
        if value is None or value == "":
            return None
        return str(value)

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _build_diagnostic_event_payload(
        self,
        *,
        event_name: str,
        tmdb_id: int,
        title: str,
        year: Optional[str],
        season: int,
        transfer_history_id: Optional[int],
    ) -> Dict[str, Any]:
        mediainfo = {
            "tmdb_id": tmdb_id,
            "type": MediaType.TV.value,
            "title": title,
            "year": year,
            "season": season,
        }
        if event_name == "subscribe_complete":
            return {
                "subscribe_id": 0,
                "subscribe_info": {
                    "season": season,
                    "name": title,
                    "type": MediaType.TV.value,
                },
                "mediainfo": mediainfo,
            }
        return {
            "meta": f"S{season:02d}",
            "mediainfo": mediainfo,
            "transfer_history_id": transfer_history_id,
            "transferinfo": {"success": True, "synthetic": True},
            "fileitem": {"path": f"/diagnostics/{tmdb_id}/S{season:02d}.mkv"},
        }

    @staticmethod
    def _dispatch_diagnostic_event(event_name: str, event_data: Dict[str, Any]) -> None:
        if event_name == "subscribe_complete":
            eventmanager.send_event(EventType.SubscribeComplete, event_data)
            return
        eventmanager.send_event(EventType.TransferComplete, event_data)

    def _sorted_form_candidates(self, media_type: str) -> Tuple[List[Dict[str, Any]], int]:
        tv_lookup, movie_lookup = self._local_candidate_lookup()
        lookup = tv_lookup if media_type == MediaType.TV.value else movie_lookup
        rows = sorted(
            lookup.values(),
            key=lambda item: (
                str(item.get("last_seen_at") or ""),
                coerce_int(item.get("latest_season"), 0) or 0,
                coerce_int(item.get("tmdb_id"), 0) or 0,
            ),
            reverse=True,
        )
        limited = rows[: self.FORM_CANDIDATE_LIMIT]
        return limited, max(len(rows) - len(limited), 0)

    def _local_candidate_lookup(self) -> Tuple[Dict[int, Dict[str, Any]], Dict[int, Dict[str, Any]]]:
        tv_lookup: Dict[int, Dict[str, Any]] = {}
        movie_lookup: Dict[int, Dict[str, Any]] = {}

        cutoff = datetime.now() - timedelta(days=max(self._history_days, 0))
        histories: List[Any] = []
        subscriptions: List[Any] = []

        try:
            histories = TransferHistoryOper().list_by_date(cutoff.strftime("%Y-%m-%d %H:%M:%S")) or []
        except Exception as exc:
            logger.warning(f"[NextReleaseTracker] candidate history load failed: {exc}")
        try:
            subscriptions = SubscribeOper().list() or []
        except Exception as exc:
            logger.warning(f"[NextReleaseTracker] candidate subscribe load failed: {exc}")

        for history in histories:
            if not getattr(history, "status", False):
                continue
            tmdb_id = coerce_int(getattr(history, "tmdbid", None))
            media_type = self._normalize_media_type(getattr(history, "type", None))
            if not tmdb_id or media_type not in {MediaType.TV.value, MediaType.MOVIE.value}:
                continue
            self._merge_candidate_lookup(
                tv_lookup if media_type == MediaType.TV.value else movie_lookup,
                tmdb_id=tmdb_id,
                title=self._as_str(getattr(history, "title", None)) or f"TMDB-{tmdb_id}",
                year=self._as_str(getattr(history, "year", None)),
                last_seen_at=self._as_str(getattr(history, "date", None)),
                source_label="最近入库",
                latest_season=parse_season_token(getattr(history, "seasons", None)) if media_type == MediaType.TV.value else None,
            )

        for subscription in subscriptions:
            tmdb_id = coerce_int(getattr(subscription, "tmdbid", None))
            media_type = self._normalize_media_type(getattr(subscription, "type", None))
            if not tmdb_id or media_type not in {MediaType.TV.value, MediaType.MOVIE.value}:
                continue
            self._merge_candidate_lookup(
                tv_lookup if media_type == MediaType.TV.value else movie_lookup,
                tmdb_id=tmdb_id,
                title=self._as_str(getattr(subscription, "name", None)) or f"TMDB-{tmdb_id}",
                year=self._as_str(getattr(subscription, "year", None)),
                last_seen_at=self._as_str(getattr(subscription, "last_update", None))
                or self._as_str(getattr(subscription, "date", None)),
                source_label="最近订阅",
                latest_season=coerce_int(getattr(subscription, "season", None)) if media_type == MediaType.TV.value else None,
            )

        for candidate in self._tmdb_discover_candidates(MediaType.TV.value):
            tmdb_id = coerce_int(self._media_value(candidate, "tmdb_id"))
            if not tmdb_id:
                continue
            self._merge_candidate_lookup(
                tv_lookup,
                tmdb_id=tmdb_id,
                title=self._as_str(self._media_value(candidate, "title")) or f"TMDB-{tmdb_id}",
                year=self._as_str(self._media_value(candidate, "year")),
                last_seen_at=self._as_str(self._media_value(candidate, "release_date")),
                source_label="TMDB发现",
                latest_season=coerce_int(self._media_value(candidate, "number_of_seasons")),
            )

        for candidate in self._tmdb_discover_candidates(MediaType.MOVIE.value):
            tmdb_id = coerce_int(self._media_value(candidate, "tmdb_id"))
            if not tmdb_id:
                continue
            self._merge_candidate_lookup(
                movie_lookup,
                tmdb_id=tmdb_id,
                title=self._as_str(self._media_value(candidate, "title")) or f"TMDB-{tmdb_id}",
                year=self._as_str(self._media_value(candidate, "year")),
                last_seen_at=self._as_str(self._media_value(candidate, "release_date")),
                source_label="TMDB发现",
            )

        return tv_lookup, movie_lookup

    def _tmdb_discover_candidates(self, media_type: str) -> List[Any]:
        chain = self._tmdb_chain or TmdbChain()
        discover = getattr(chain, "tmdb_discover", None)
        if not callable(discover):
            return []

        items: List[Any] = []
        page_limit = 2
        for page in range(1, page_limit + 1):
            try:
                result = discover(
                    mtype=MediaType.TV if media_type == MediaType.TV.value else MediaType.MOVIE,
                    sort_by="popularity.desc",
                    with_genres="",
                    with_original_language="zh|en|ja|ko" if media_type == MediaType.TV.value else "",
                    with_keywords="",
                    with_watch_providers="",
                    vote_average=0.0,
                    vote_count=0,
                    release_date="",
                    page=page,
                ) or []
                items.extend(result)
            except Exception as exc:
                logger.warning(f"[NextReleaseTracker] TMDB discover candidates failed for {media_type}: {exc}")
                break
        return items

    @staticmethod
    def _merge_candidate_lookup(
        lookup: Dict[int, Dict[str, Any]],
        *,
        tmdb_id: int,
        title: str,
        year: Optional[str],
        last_seen_at: Optional[str],
        source_label: str,
        latest_season: Optional[int] = None,
    ) -> None:
        entry = lookup.get(int(tmdb_id)) or {
            "tmdb_id": int(tmdb_id),
            "title": title or f"TMDB-{tmdb_id}",
            "year": year,
            "last_seen_at": last_seen_at,
            "latest_season": latest_season,
            "sources": [],
        }
        if title and (not entry.get("title") or str(entry.get("title", "")).startswith("TMDB-")):
            entry["title"] = title
        if year and not entry.get("year"):
            entry["year"] = year
        current_seen = str(entry.get("last_seen_at") or "")
        candidate_seen = str(last_seen_at or "")
        if candidate_seen and candidate_seen >= current_seen:
            entry["last_seen_at"] = candidate_seen
            if title:
                entry["title"] = title
            if year:
                entry["year"] = year
        if latest_season is not None:
            entry["latest_season"] = max(
                coerce_int(entry.get("latest_season"), 0) or 0,
                coerce_int(latest_season, 0) or 0,
            ) or None
        if source_label not in entry["sources"]:
            entry["sources"].append(source_label)
        entry["search_text"] = " ".join(
            part.lower()
            for part in [
                str(entry.get("title") or ""),
                str(entry.get("year") or ""),
                str(entry.get("tmdb_id") or ""),
            ]
            if part
        )
        lookup[int(tmdb_id)] = entry

    def _candidate_selection_table(
        self,
        *,
        media_label: str,
        model_key: str,
        search_model: str,
        candidates: List[Dict[str, Any]],
    ) -> dict:
        headers = ["片名", "年份", "TMDB", "最近时间", "来源", "线索", "操作"]
        if not candidates:
            return {
                "component": "div",
                "props": {"class": "nrt-empty"},
                "text": f"这里暂时没有最近出现过的{media_label}；你也可以直接手动输入编号加入。",
            }

        is_tv = media_label == "剧集"
        search_label = "搜剧名或年份" if is_tv else "搜电影名或年份"
        search_row = {
            "component": "tr",
            "content": [
                {
                    "component": "th",
                    "props": {"class": "text-start py-2", "colspan": len(headers)},
                    "content": [
                        {
                            "component": "VTextField",
                            "props": {
                                "model": search_model,
                                "label": search_label,
                                "placeholder": "找到后直接点下方“加入白名单”",
                                "clearable": True,
                                "prepend-inner-icon": "mdi-magnify",
                                "hide-details": "auto",
                                "density": "comfortable",
                            },
                        }
                    ],
                }
            ],
        }

        body_rows = []
        for candidate in candidates:
            tmdb_id = coerce_int(candidate.get("tmdb_id"), 0) or 0
            latest_season = coerce_int(candidate.get("latest_season"), 0) or 0
            clue_text = (
                f"目前到 S{latest_season:02d}"
                if is_tv and latest_season
                else ("已找到系列线索" if not is_tv else "等待第一次命中")
            )
            body_rows.append(
                {
                    "component": "tr",
                    "props": {"show": self._candidate_row_show_expr(search_model, str(candidate.get("search_text") or ""))},
                    "content": [
                        self._table_cell(candidate.get("title") or f"TMDB-{tmdb_id}"),
                        self._table_cell(candidate.get("year") or "-"),
                        self._table_cell(tmdb_id),
                        self._table_cell(candidate.get("last_seen_at") or "-"),
                        self._table_cell(" / ".join(candidate.get("sources") or []) or "-"),
                        self._table_cell(clue_text),
                        self._table_cell(
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "primary",
                                    "variant": "tonal",
                                    "size": "small",
                                    "prepend-icon": "mdi-plus",
                                    "disabled": self._selection_contains_expr(model_key, tmdb_id),
                                    "onClick": self._selection_add_fixed_expr(model_key, tmdb_id),
                                },
                                "text": "加入白名单",
                            }
                        ),
                    ],
                }
            )

        return {
            "component": "VTable",
            "props": {"hover": True, "density": "compact"},
            "content": [
                {
                    "component": "thead",
                    "content": [
                        {
                            "component": "tr",
                            "content": [
                                {"component": "th", "props": {"class": "text-start"}, "text": header}
                                for header in headers
                            ],
                        },
                        search_row,
                    ],
                },
                {"component": "tbody", "content": body_rows},
            ],
        }

    @classmethod
    def _candidate_row_show_expr(cls, search_model: str, search_text: str) -> str:
        candidate_text = cls._js_value(str(search_text or "").lower())
        return (
            "{{ (() => { "
            f"const query = String(model.{search_model} || '').trim().toLowerCase(); "
            f"const haystack = {candidate_text}; "
            "return !query || haystack.includes(query); "
            "})() }}"
        )

    @staticmethod
    def _col(md: int, content: dict) -> dict:
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": md},
            "content": [content],
        }

    @staticmethod
    def _switch(model: str, label: str) -> dict:
        return {
            "component": "VSwitch",
            "props": {"model": model, "label": label},
        }

    @staticmethod
    def _textfield(model: str, label: str, placeholder: str = "") -> dict:
        return {
            "component": "VTextField",
            "props": {
                "model": model,
                "label": label,
                "placeholder": placeholder,
            },
        }

    @staticmethod
    def _textarea(model: str, label: str, placeholder: str = "") -> dict:
        return {
            "component": "VTextarea",
            "props": {
                "model": model,
                "label": label,
                "placeholder": placeholder,
                "rows": 4,
                "auto-grow": True,
            },
        }

    @staticmethod
    def _stat_card(title: str, value: Any, subtitle: str) -> dict:
        return {
            "component": "div",
            "props": {"class": "nrt-stat"},
            "content": [
                {"component": "div", "props": {"class": "nrt-stat__title"}, "text": str(title)},
                {"component": "div", "props": {"class": "nrt-stat__value"}, "text": str(value)},
                {"component": "div", "props": {"class": "nrt-stat__subtitle"}, "text": str(subtitle)},
            ],
        }

    def _page_toolbar(self) -> dict:
        plugin_api = f"plugin/{self.__class__.__name__}"
        buttons = [
            self._action_button(
                text="扫描全部",
                api=f"{plugin_api}/rescan?apikey={settings.API_TOKEN}",
                payload={"scope": "all", "notify": True},
                color="primary",
            ),
            self._action_button(
                text="仅扫剧集",
                api=f"{plugin_api}/rescan?apikey={settings.API_TOKEN}",
                payload={"scope": "tv", "notify": True},
                color="info",
            ),
            self._action_button(
                text="诊断订阅事件",
                api=f"{plugin_api}/diagnostic/event?apikey={settings.API_TOKEN}",
                payload={
                    "event": "subscribe_complete",
                    "media_type": "tv",
                    "tmdb_id": self.diagnostic_tv_tmdb_id,
                    "title": self.diagnostic_tv_title,
                    "season": 1,
                    "cleanup": True,
                },
                color="success",
            ),
            self._action_button(
                text="诊断整理事件",
                api=f"{plugin_api}/diagnostic/event?apikey={settings.API_TOKEN}",
                payload={
                    "event": "transfer_complete",
                    "media_type": "tv",
                    "tmdb_id": self.diagnostic_tv_tmdb_id,
                    "title": self.diagnostic_tv_title,
                    "season": 1,
                    "cleanup": True,
                },
                color="secondary",
            ),
        ]
        if self._enable_movie:
            buttons.append(
                self._action_button(
                    text="仅扫电影",
                    api=f"{plugin_api}/rescan?apikey={settings.API_TOKEN}",
                    payload={"scope": "movie", "notify": True},
                    color="secondary",
                )
            )
        buttons.append(
            self._action_button(
                text="回填最近 1 天",
                api=f"{plugin_api}/import/transfer_history?apikey={settings.API_TOKEN}",
                payload={"days": 1},
                color="warning",
            )
        )
        return {
            "component": "div",
            "props": {"class": "nrt-toolbar"},
            "content": [
                {
                    "component": "div",
                    "content": [
                        {"component": "div", "props": {"class": "nrt-toolbar__title"}, "text": "追剧助手"},
                        {
                            "component": "div",
                            "props": {"class": "nrt-toolbar__subtitle"},
                            "text": "这里可以查看追更名单、当前状态和重扫结果；新增或修改名单请回到配置页。内置诊断只会清理自己的测试样本，不会动你的真实订阅。",
                        },
                    ],
                },
                {"component": "div", "props": {"class": "nrt-toolbar__actions"}, "content": buttons},
            ],
        }

    @staticmethod
    def _page_style() -> dict:
        return {
            "component": "style",
            "text": """
            .nrt-page { display: flex; flex-direction: column; gap: 12px; }
            .nrt-toolbar { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; flex-wrap: wrap; }
            .nrt-toolbar__title { font-size: 1.05rem; font-weight: 700; color: rgba(var(--v-theme-on-surface), 1); }
            .nrt-toolbar__subtitle { margin-top: 4px; max-width: 780px; color: rgba(var(--v-theme-on-surface), .68); font-size: .86rem; line-height: 1.5; }
            .nrt-toolbar__actions { display: flex; gap: 8px; flex-wrap: wrap; }
            .nrt-summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
            .nrt-stat { min-width: 0; border: 1px solid rgba(var(--v-theme-on-surface), .10); border-radius: 8px; background: rgba(var(--v-theme-surface), 1); padding: 12px 14px; }
            .nrt-stat__title { color: rgba(var(--v-theme-on-surface), .64); font-size: .78rem; font-weight: 600; }
            .nrt-stat__value { margin-top: 6px; font-size: 1.4rem; font-weight: 700; color: rgba(var(--v-theme-on-surface), 1); }
            .nrt-stat__subtitle { margin-top: 4px; font-size: .78rem; color: rgba(var(--v-theme-on-surface), .60); }
            .nrt-section { border-radius: 8px; }
            .nrt-section__title { font-size: .95rem; font-weight: 700; }
            .nrt-chip-row { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
            .nrt-empty { padding: 6px 0; color: rgba(var(--v-theme-on-surface), .64); }
            @media (max-width: 960px) { .nrt-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
            @media (max-width: 600px) { .nrt-summary { grid-template-columns: repeat(1, minmax(0, 1fr)); } }
            """,
        }

    @staticmethod
    def _section_card(title: str, content: List[dict]) -> dict:
        return {
            "component": "VCard",
            "props": {"variant": "outlined", "class": "nrt-section"},
            "content": [
                {
                    "component": "VCardItem",
                    "props": {"class": "pb-0"},
                    "content": [
                        {"component": "VCardTitle", "props": {"class": "nrt-section__title"}, "text": title},
                    ],
                },
                {"component": "VCardText", "content": content},
            ],
        }

    @staticmethod
    def _status_chips(chips: List[dict]) -> dict:
        return {"component": "div", "props": {"class": "nrt-chip-row"}, "content": chips}

    @staticmethod
    def _chip(text: str, color: str, icon: str) -> dict:
        return {
            "component": "VChip",
            "props": {"color": color, "size": "small", "variant": "tonal"},
            "content": [
                {"component": "VIcon", "props": {"size": "small", "start": True}, "text": icon},
                {"component": "span", "text": text},
            ],
        }

    @staticmethod
    def _chip_cell(text: str, color: str, icon: str) -> dict:
        return {"component": "div", "content": [NextReleaseTracker._chip(text, color, icon)]}

    @staticmethod
    def _simple_table(headers: List[str], rows: List[List[Any]], empty_text: str = "暂无数据。") -> dict:
        if not rows:
            return {"component": "div", "props": {"class": "nrt-empty"}, "text": empty_text}
        header_row = {
            "component": "tr",
            "content": [{"component": "th", "props": {"class": "text-start"}, "text": header} for header in headers],
        }
        body_rows = []
        for row in rows:
            body_rows.append(
                {
                    "component": "tr",
                    "content": [NextReleaseTracker._table_cell(cell) for cell in row],
                }
            )
        return {
            "component": "VTable",
            "props": {"hover": True, "density": "compact"},
            "content": [
                {"component": "thead", "content": [header_row]},
                {"component": "tbody", "content": body_rows},
            ],
        }

    @staticmethod
    def _table_cell(value: Any) -> dict:
        if isinstance(value, dict):
            return {"component": "td", "content": [value]}
        return {"component": "td", "text": "-" if value is None or value == "" else str(value)}

    @staticmethod
    def _action_button(text: str, api: str, payload: dict, color: str) -> dict:
        return {
            "component": "VBtn",
            "props": {"color": color, "variant": "tonal", "size": "small"},
            "text": text,
            "events": {
                "click": {
                    "api": api,
                    "method": "post",
                    "params": payload,
                }
            },
        }

    @staticmethod
    def _join_values(values: Any) -> str:
        if not values:
            return "-"
        if not isinstance(values, list):
            return str(values)
        normalized = [str(value) for value in values if value is not None and value != ""]
        return ", ".join(normalized) if normalized else "-"

    @staticmethod
    def _log_message(log_entry: dict) -> str:
        context = log_entry.get("context") or {}
        context_text = ", ".join(
            f"{key}={value}"
            for key, value in context.items()
            if value is not None and value != ""
        )
        message = str(log_entry.get("message") or "-")
        return f"{message} [{context_text}]" if context_text else message

    @staticmethod
    def _level_color(level: Any) -> str:
        text = str(level or "info").lower()
        if text == "error":
            return "error"
        if text == "warning":
            return "warning"
        if text == "success":
            return "success"
        return "info"

    @staticmethod
    def _level_icon(level: Any) -> str:
        text = str(level or "info").lower()
        if text == "error":
            return "mdi-close-circle-outline"
        if text == "warning":
            return "mdi-alert-outline"
        if text == "success":
            return "mdi-check-circle-outline"
        return "mdi-information-outline"
