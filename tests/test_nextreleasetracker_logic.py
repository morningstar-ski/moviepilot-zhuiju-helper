import sys
import types
import unittest
import importlib.util
import json
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins.v2" / "nextreleasetracker"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import logic  # type: ignore  # noqa: E402
import state  # type: ignore  # noqa: E402


def load_plugin_module():
    for name in list(sys.modules):
        if name == "nextreleasetracker" or name.startswith("nextreleasetracker."):
            sys.modules.pop(name, None)
        if name == "app" or name.startswith("app."):
            sys.modules.pop(name, None)

    app_mod = types.ModuleType("app")
    sys.modules["app"] = app_mod

    pytz_mod = types.ModuleType("pytz")
    pytz_mod.timezone = lambda name: name
    sys.modules["pytz"] = pytz_mod

    apscheduler_mod = types.ModuleType("apscheduler")
    apscheduler_triggers_mod = types.ModuleType("apscheduler.triggers")
    apscheduler_cron_mod = types.ModuleType("apscheduler.triggers.cron")

    class DummyCronTrigger:
        @staticmethod
        def from_crontab(expr):
            if len(str(expr or "").split()) != 5:
                raise ValueError("Wrong number of fields; got invalid cron expression")
            return expr

    apscheduler_cron_mod.CronTrigger = DummyCronTrigger
    sys.modules["apscheduler"] = apscheduler_mod
    sys.modules["apscheduler.triggers"] = apscheduler_triggers_mod
    sys.modules["apscheduler.triggers.cron"] = apscheduler_cron_mod

    fastapi_mod = types.ModuleType("fastapi")
    fastapi_mod.Body = lambda default=None, **kwargs: default
    sys.modules["fastapi"] = fastapi_mod

    chain_mod = types.ModuleType("app.chain")
    mediaserver_mod = types.ModuleType("app.chain.mediaserver")
    subscribe_mod = types.ModuleType("app.chain.subscribe")
    tmdb_mod = types.ModuleType("app.chain.tmdb")

    class DummyMediaServerChain:
        def media_exists(self, media):
            return None

    class DummySubscribeChain:
        calls = []

        def add(self, **kwargs):
            self.__class__.calls.append(kwargs)
            return 1, "ok"

    class DummyTmdbChain:
        def tmdb_seasons(self, tmdb_id):
            return []

        def tmdb_collection(self, collection_id):
            return []

    mediaserver_mod.MediaServerChain = DummyMediaServerChain
    chain_mod.MediaServerChain = DummyMediaServerChain
    subscribe_mod.SubscribeChain = DummySubscribeChain
    tmdb_mod.TmdbChain = DummyTmdbChain

    sys.modules["app.chain"] = chain_mod
    sys.modules["app.chain.mediaserver"] = mediaserver_mod
    sys.modules["app.chain.subscribe"] = subscribe_mod
    sys.modules["app.chain.tmdb"] = tmdb_mod

    config_mod = types.ModuleType("app.core.config")
    config_mod.settings = types.SimpleNamespace(TZ="Asia/Shanghai", API_TOKEN="test-token")
    context_mod = types.ModuleType("app.core.context")
    context_mod.MediaInfo = object
    event_mod = types.ModuleType("app.core.event")

    class DummyEvent:
        def __init__(self, event_data=None):
            self.event_data = event_data or {}

    class DummyEventManager:
        @staticmethod
        def register(_event_type):
            def decorator(func):
                return func

            return decorator

    event_mod.Event = DummyEvent
    event_mod.eventmanager = DummyEventManager()
    sys.modules["app.core.config"] = config_mod
    sys.modules["app.core.context"] = context_mod
    sys.modules["app.core.event"] = event_mod

    subscribe_oper_mod = types.ModuleType("app.db.subscribe_oper")
    transferhistory_oper_mod = types.ModuleType("app.db.transferhistory_oper")

    class DummySubscribeOper:
        def exists(self, **kwargs):
            return False

        def list(self, state=None):
            return []

    class DummyTransferHistoryOper:
        def list_by_date(self, _cutoff):
            return []

    subscribe_oper_mod.SubscribeOper = DummySubscribeOper
    transferhistory_oper_mod.TransferHistoryOper = DummyTransferHistoryOper
    sys.modules["app.db.subscribe_oper"] = subscribe_oper_mod
    sys.modules["app.db.transferhistory_oper"] = transferhistory_oper_mod

    log_mod = types.ModuleType("app.log")
    log_mod.logger = types.SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
        exception=lambda *args, **kwargs: None,
    )
    sys.modules["app.log"] = log_mod

    plugins_mod = types.ModuleType("app.plugins")

    class DummyPluginBase:
        def __init__(self):
            self._data = {}
            self._messages = []
            self.chain = types.SimpleNamespace(recognize_media=lambda **kwargs: None)

        def get_data(self, key):
            return self._data.get(key)

        def save_data(self, key, value):
            self._data[key] = value

        def del_data(self, key):
            self._data.pop(key, None)

        def update_config(self, config):
            self._config = config
            return True

        def post_message(self, **kwargs):
            self._messages.append(kwargs)
            self._last_message = kwargs

    plugins_mod._PluginBase = DummyPluginBase
    sys.modules["app.plugins"] = plugins_mod

    schemas_types_mod = types.ModuleType("app.schemas.types")

    class _Value:
        def __init__(self, value):
            self.value = value

    schemas_types_mod.EventType = types.SimpleNamespace(
        SubscribeComplete=_Value("SubscribeComplete"),
        TransferComplete=_Value("TransferComplete"),
    )
    schemas_types_mod.MediaType = types.SimpleNamespace(
        TV=_Value("\u7535\u89c6\u5267"),
        MOVIE=_Value("\u7535\u5f71"),
    )
    schemas_types_mod.NotificationType = types.SimpleNamespace(Plugin="Plugin")
    sys.modules["app.schemas.types"] = schemas_types_mod

    spec = importlib.util.spec_from_file_location(
        "nextreleasetracker",
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["nextreleasetracker"] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def iter_component_nodes(tree):
    if isinstance(tree, dict):
        yield tree
        content = tree.get("content")
        if isinstance(content, list):
            for item in content:
                yield from iter_component_nodes(item)
        elif isinstance(content, dict):
            yield from iter_component_nodes(content)
    elif isinstance(tree, list):
        for item in tree:
            yield from iter_component_nodes(item)


class LogicTests(unittest.TestCase):
    def test_select_ready_tv_seasons_filters_future_pending_and_season_zero(self):
        seasons = [
            {"season_number": 0, "episode_count": 2, "air_date": "2026-06-01"},
            {"season_number": 2, "episode_count": 8, "air_date": "2026-06-05"},
            {"season_number": 3, "episode_count": 10, "air_date": "2026-06-20"},
            {"season_number": 4, "episode_count": 0, "air_date": "2026-06-01"},
        ]
        ready = logic.select_ready_tv_seasons(
            latest_season=1,
            pending_seasons=[2],
            seasons=seasons,
            today=logic.parse_date("2026-06-03"),
            grace_days=3,
        )
        self.assertEqual([], ready)

    def test_select_ready_tv_seasons_accepts_released_season(self):
        seasons = [
            {"season_number": 2, "episode_count": 8, "air_date": "2026-06-05"},
        ]
        ready = logic.select_ready_tv_seasons(
            latest_season=1,
            pending_seasons=[],
            seasons=seasons,
            today=logic.parse_date("2026-06-03"),
            grace_days=3,
        )
        self.assertEqual(1, len(ready))
        self.assertEqual(2, ready[0].season_number)

    def test_select_ready_collection_movies_filters_known_pending_and_future_titles(self):
        items = [
            {"tmdb_id": 101, "title": "Part 2", "year": "2026", "release_date": "2026-06-01", "collection_id": 9},
            {"tmdb_id": 102, "title": "Part 3", "year": "2027", "release_date": "2026-06-10", "collection_id": 9},
            {"tmdb_id": 103, "title": "Part 4", "year": "2026", "release_date": "2026-06-02", "collection_id": 9},
        ]
        ready = logic.select_ready_collection_movies(
            known_tmdb_ids=[101],
            pending_tmdb_ids=[103],
            items=items,
            today=logic.parse_date("2026-06-03"),
            grace_days=3,
        )
        self.assertEqual([], ready)

    def test_parse_season_token_supports_history_style_values(self):
        self.assertEqual(1, logic.parse_season_token("S01"))
        self.assertEqual(12, logic.parse_season_token("Season 12"))
        self.assertEqual(3, logic.parse_season_token(3))

    def test_season_fully_exists_requires_complete_episode_count(self):
        existing = {"seasons": {2: [1, 2, 3, 4]}}
        self.assertTrue(logic.season_fully_exists(existing, 2, 4))
        self.assertFalse(logic.season_fully_exists(existing, 2, 5))


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self._db = {}
        self._store = state.TrackerStateStore(
            load_fn=lambda key: self._db.get(key),
            save_fn=lambda key, value: self._db.__setitem__(key, value),
            delete_fn=lambda key: self._db.pop(key, None),
            now_fn=lambda: "2026-06-03 21:00:00",
            log_limit=3,
        )

    def test_movie_track_migrates_to_collection_key(self):
        created = self._store.upsert_movie_track(
            anchor_tmdb_id=10,
            title="Movie 1",
            year="2025",
            source="manual",
            collection_id=None,
            known_tmdb_ids=[10],
        )
        self.assertEqual("movie:10", created["track_key"])

        migrated = self._store.acknowledge_movie_completion(
            track_key="movie:10",
            tmdb_id=10,
            title="Movie 1",
            year="2025",
            collection_id=88,
            source="transfer_complete",
        )
        self.assertEqual("collection:88", migrated["track_key"])
        self.assertIn("collection:88", self._store.get_movie_tracks())
        self.assertNotIn("movie:10", self._store.get_movie_tracks())

    def test_tv_pending_is_cleared_when_season_acknowledged(self):
        self._store.upsert_tv_track(
            tmdb_id=100,
            title="Show",
            year="2026",
            latest_season=1,
            source="manual",
        )
        self._store.mark_tv_pending(100, 2)
        track = self._store.acknowledge_tv_completion(
            tmdb_id=100,
            title="Show",
            year="2026",
            season=2,
            source="transfer_complete",
        )
        self.assertEqual([], track["pending_seasons"])
        self.assertEqual(2, track["latest_season"])

    def test_action_log_honors_retention_limit(self):
        self._store.append_action(level="info", action="a1", message="1")
        self._store.append_action(level="info", action="a2", message="2")
        self._store.append_action(level="info", action="a3", message="3")
        self._store.append_action(level="info", action="a4", message="4")
        logs = self._store.get_action_log()
        self.assertEqual(3, len(logs))
        self.assertEqual("a2", logs[0]["action"])
        self.assertEqual("a4", logs[-1]["action"])

    def test_clear_action_log_empties_existing_entries(self):
        self._store.append_action(level="info", action="a1", message="1")
        self._store.append_action(level="info", action="a2", message="2")
        self._store.clear_action_log()
        self.assertEqual([], self._store.get_action_log())


class PluginPageTests(unittest.TestCase):
    def test_plugin_defaults_to_tv_only(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin({})
        self.assertFalse(plugin._enable_movie)
        self.assertTrue(plugin._enable_tv)
        self.assertEqual([], plugin._selected_tv_ids)
        self.assertEqual([], plugin._selected_movie_ids)

    def test_init_plugin_recovers_selected_ids_from_state_when_config_missing(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        movie_track_key = logic.build_movie_track_key(None, 550)
        plugin._data[state.TrackerStateStore.KEY_TRACKED_TV] = {
            logic.build_tv_track_key(60625): {
                "tmdb_id": 60625,
                "title": "Rick and Morty",
                "year": "2013",
                "latest_season": 1,
                "pending_seasons": [],
                "source": "manual",
                "added_at": "2026-06-04 12:00:00",
            }
        }
        plugin._data[state.TrackerStateStore.KEY_TRACKED_MOVIE] = {
            movie_track_key: {
                "track_key": movie_track_key,
                "anchor_tmdb_id": 550,
                "title": "Fight Club",
                "year": "1999",
                "collection_id": None,
                "known_tmdb_ids": [550],
                "pending_tmdb_ids": [],
                "source": "manual",
                "added_at": "2026-06-04 12:00:00",
            }
        }

        plugin.init_plugin({})

        store = plugin._ensure_state_store()
        self.assertEqual([60625], plugin._selected_tv_ids)
        self.assertEqual([550], plugin._selected_movie_ids)
        self.assertTrue(plugin._enable_movie)
        self.assertIn("60625", plugin._config["tracked_tv_ids"])
        self.assertIn("550", plugin._config["tracked_movie_ids"])
        self.assertEqual([60625], store.get_selected_tv_ids())
        self.assertEqual([550], store.get_selected_movie_ids())
        self.assertIn("60625", store.get_tv_tracks())
        self.assertIn(movie_track_key, store.get_movie_tracks())

    def test_init_plugin_migrates_config_selection_into_state_store(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()

        plugin.init_plugin(
            {
                "enabled": True,
                "enable_movie": True,
                "tracked_tv_ids": "60625",
                "tracked_movie_ids": "550",
            }
        )

        store = plugin._ensure_state_store()
        self.assertEqual([60625], store.get_selected_tv_ids())
        self.assertEqual([550], store.get_selected_movie_ids())
        self.assertEqual("60625", plugin._config["tracked_tv_ids"])
        self.assertEqual("550", plugin._config["tracked_movie_ids"])

    def test_init_plugin_config_selection_overrides_existing_state_selection(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin._data[state.TrackerStateStore.KEY_SELECTED_TV_IDS] = [60625]
        plugin._data[state.TrackerStateStore.KEY_SELECTED_MOVIE_IDS] = [550]

        plugin.init_plugin(
            {
                "enabled": True,
                "enable_movie": True,
                "tracked_tv_ids": "77777",
                "tracked_movie_ids": "603",
            }
        )

        store = plugin._ensure_state_store()
        self.assertEqual([77777], plugin._selected_tv_ids)
        self.assertEqual([603], plugin._selected_movie_ids)
        self.assertEqual([77777], store.get_selected_tv_ids())
        self.assertEqual([603], store.get_selected_movie_ids())
        self.assertEqual("77777", plugin._config["tracked_tv_ids"])
        self.assertEqual("603", plugin._config["tracked_movie_ids"])

    def test_init_plugin_config_selection_overrides_existing_empty_state_keys(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin._data[state.TrackerStateStore.KEY_SELECTED_TV_IDS] = []
        plugin._data[state.TrackerStateStore.KEY_SELECTED_MOVIE_IDS] = []

        plugin.init_plugin(
            {
                "enabled": True,
                "enable_movie": True,
                "tracked_tv_ids": "60625",
                "tracked_movie_ids": "603",
            }
        )

        store = plugin._ensure_state_store()
        self.assertEqual([60625], plugin._selected_tv_ids)
        self.assertEqual([603], plugin._selected_movie_ids)
        self.assertEqual([60625], store.get_selected_tv_ids())
        self.assertEqual([603], store.get_selected_movie_ids())
        self.assertEqual("60625", plugin._config["tracked_tv_ids"])
        self.assertEqual("603", plugin._config["tracked_movie_ids"])

    def test_init_plugin_recovers_tracks_when_empty_state_keys_exist(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        movie_track_key = logic.build_movie_track_key(None, 550)
        plugin._data[state.TrackerStateStore.KEY_SELECTED_TV_IDS] = []
        plugin._data[state.TrackerStateStore.KEY_SELECTED_MOVIE_IDS] = []
        plugin._data[state.TrackerStateStore.KEY_TRACKED_TV] = {
            logic.build_tv_track_key(60625): {
                "tmdb_id": 60625,
                "title": "Rick and Morty",
                "year": "2013",
                "latest_season": 1,
                "pending_seasons": [],
                "source": "manual",
                "added_at": "2026-06-04 12:00:00",
            }
        }
        plugin._data[state.TrackerStateStore.KEY_TRACKED_MOVIE] = {
            movie_track_key: {
                "track_key": movie_track_key,
                "anchor_tmdb_id": 550,
                "title": "Fight Club",
                "year": "1999",
                "collection_id": None,
                "known_tmdb_ids": [550],
                "pending_tmdb_ids": [],
                "source": "manual",
                "added_at": "2026-06-04 12:00:00",
            }
        }

        plugin.init_plugin({})

        store = plugin._ensure_state_store()
        self.assertEqual([60625], plugin._selected_tv_ids)
        self.assertEqual([550], plugin._selected_movie_ids)
        self.assertEqual([60625], store.get_selected_tv_ids())
        self.assertEqual([550], store.get_selected_movie_ids())
        self.assertIn("60625", store.get_tv_tracks())
        self.assertIn(movie_track_key, store.get_movie_tracks())

    def test_init_plugin_explicit_empty_selection_clears_existing_state_selection(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin._data[state.TrackerStateStore.KEY_SELECTED_TV_IDS] = [60625]
        plugin._data[state.TrackerStateStore.KEY_SELECTED_MOVIE_IDS] = [550]

        plugin.init_plugin(
            {
                "enabled": True,
                "enable_movie": True,
                "tracked_tv_ids": "",
                "tracked_movie_ids": "",
            }
        )

        store = plugin._ensure_state_store()
        self.assertEqual([], plugin._selected_tv_ids)
        self.assertEqual([], plugin._selected_movie_ids)
        self.assertEqual([], store.get_selected_tv_ids())
        self.assertEqual([], store.get_selected_movie_ids())
        self.assertEqual("", plugin._config["tracked_tv_ids"])
        self.assertEqual("", plugin._config["tracked_movie_ids"])

    def test_init_plugin_explicit_empty_tv_selection_does_not_override_movie_state(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin._data[state.TrackerStateStore.KEY_SELECTED_TV_IDS] = [60625]
        plugin._data[state.TrackerStateStore.KEY_SELECTED_MOVIE_IDS] = [550]

        plugin.init_plugin(
            {
                "enabled": True,
                "enable_movie": True,
                "tracked_tv_ids": "",
            }
        )

        store = plugin._ensure_state_store()
        self.assertEqual([], plugin._selected_tv_ids)
        self.assertEqual([550], plugin._selected_movie_ids)
        self.assertEqual([], store.get_selected_tv_ids())
        self.assertEqual([550], store.get_selected_movie_ids())

    def test_init_plugin_with_explicit_empty_selection_still_cleans_stale_tracks(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin._data[state.TrackerStateStore.KEY_TRACKED_TV] = {
            logic.build_tv_track_key(60625): {
                "tmdb_id": 60625,
                "title": "Rick and Morty",
                "year": "2013",
                "latest_season": 1,
                "pending_seasons": [],
                "source": "manual",
                "added_at": "2026-06-04 12:00:00",
            }
        }

        plugin.init_plugin({"tracked_tv_ids": "", "tracked_movie_ids": "", "manual_movie_mappings": ""})

        self.assertEqual([], plugin._selected_tv_ids)
        self.assertEqual([], plugin._ensure_state_store().get_selected_tv_ids())
        self.assertEqual({}, plugin._ensure_state_store().get_tv_tracks())

    def test_init_plugin_sanitizes_form_only_fields(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "tracked_tv_ids": "60625\n60625",
                "tv_candidate_id": "12345",
                "movie_remove_id": "9",
            }
        )

        self.assertEqual([60625], plugin._selected_tv_ids)
        self.assertEqual(set(plugin_module.NextReleaseTracker.CONFIG_FIELDS), set(plugin._config.keys()))
        self.assertEqual("60625", plugin._config["tracked_tv_ids"])
        self.assertNotIn("tv_candidate_id", plugin._config)
        self.assertNotIn("movie_remove_id", plugin._config)

    def test_unselected_tv_event_is_ignored(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin({"enabled": True, "enable_tv": True})

        plugin.on_subscribe_complete(
            plugin_module.Event(
                {
                    "subscribe_info": {"season": 1},
                    "mediainfo": {
                        "tmdb_id": 60625,
                        "type": plugin_module.MediaType.TV.value,
                        "title": "Rick and Morty",
                        "year": "2013",
                    },
                }
            )
        )

        self.assertEqual({}, plugin._ensure_state_store().get_tv_tracks())

    def test_api_track_add_updates_selected_list(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin({"enabled": True, "enable_tv": True})

        response = plugin.api_track_add(
            {
                "media_type": "tv",
                "tmdb_id": 60625,
                "season": 1,
                "title": "Rick and Morty",
                "year": "2013",
            }
        )

        self.assertTrue(response["success"])
        self.assertEqual([60625], plugin._selected_tv_ids)
        self.assertEqual("60625", plugin._config["tracked_tv_ids"])
        self.assertEqual([60625], plugin._ensure_state_store().get_selected_tv_ids())

    def test_selected_tv_track_notifies_and_clears_after_detection(self):
        plugin_module = load_plugin_module()
        plugin_module.SubscribeChain.calls = []
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "notify": True,
                "enable_tv": True,
                "enable_movie": False,
                "tracked_tv_ids": "60625",
                "cron": "0 3 * * 1",
            }
        )
        store = plugin._ensure_state_store()
        store.acknowledge_tv_completion(
            tmdb_id=60625,
            title="Rick and Morty",
            year="2013",
            season=1,
            source="manual",
        )
        plugin._tmdb_chain = types.SimpleNamespace(
            tmdb_seasons=lambda tmdb_id: [
                {"season_number": 2, "episode_count": 10, "air_date": "2026-06-01"},
            ]
        )
        plugin._media_server_chain = types.SimpleNamespace(media_exists=lambda media: None)
        plugin._subscribe_exists = lambda **kwargs: False

        summary = plugin._run_rescan(scope="tv", reason="test", notify=True)

        self.assertTrue(summary["success"])
        self.assertEqual(1, summary["tv_candidates"])
        self.assertEqual(1, summary["notifications_sent"])
        self.assertEqual(1, summary["tracks_completed"])
        self.assertEqual({}, store.get_tv_tracks())
        self.assertEqual([], plugin._selected_tv_ids)
        self.assertEqual([], store.get_selected_tv_ids())
        self.assertEqual("", plugin._config["tracked_tv_ids"])
        self.assertEqual(1, len(plugin_module.SubscribeChain.calls))
        self.assertEqual(60625, plugin_module.SubscribeChain.calls[0]["tmdbid"])
        self.assertEqual(2, plugin_module.SubscribeChain.calls[0]["season"])
        self.assertIn("\u65b0\u5b63 S02", plugin._last_message["text"])
        self.assertIn("\u5df2\u81ea\u52a8\u6dfb\u52a0\u8ba2\u9605", plugin._last_message["text"])
        self.assertIn("\u5df2\u7ed3\u675f\u672c\u6761\u8ffd\u8e2a", plugin._last_message["text"])

    def test_selected_tv_track_in_library_completes_without_notification(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "notify": True,
                "enable_tv": True,
                "enable_movie": False,
                "tracked_tv_ids": "60625",
            }
        )
        store = plugin._ensure_state_store()
        store.acknowledge_tv_completion(
            tmdb_id=60625,
            title="Rick and Morty",
            year="2013",
            season=1,
            source="manual",
        )
        plugin._tmdb_chain = types.SimpleNamespace(
            tmdb_seasons=lambda tmdb_id: [
                {"season_number": 2, "episode_count": 10, "air_date": "2026-06-01"},
            ]
        )
        plugin.chain = types.SimpleNamespace(recognize_media=lambda **kwargs: types.SimpleNamespace())
        plugin._media_server_chain = types.SimpleNamespace(
            media_exists=lambda media: types.SimpleNamespace(seasons={2: list(range(1, 11))})
        )
        plugin._subscribe_exists = lambda **kwargs: False

        summary = plugin._run_rescan(scope="tv", reason="test", notify=True)

        self.assertTrue(summary["success"])
        self.assertEqual(1, summary["tv_candidates"])
        self.assertEqual(1, summary["existing_in_library"])
        self.assertEqual(0, summary["notifications_sent"])
        self.assertEqual(1, summary["tracks_completed"])
        self.assertEqual({}, store.get_tv_tracks())
        self.assertEqual([], plugin._selected_tv_ids)
        self.assertFalse(hasattr(plugin, "_last_message"))

    def test_history_import_only_applies_to_selected_items(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "enable_tv": True,
                "enable_movie": False,
                "tracked_tv_ids": "60625",
            }
        )

        selected = types.SimpleNamespace(
            status=True,
            tmdbid=60625,
            type=plugin_module.MediaType.TV.value,
            seasons="S01",
            title="Rick and Morty",
            year="2013",
            id=1,
        )
        skipped = types.SimpleNamespace(
            status=True,
            tmdbid=99999,
            type=plugin_module.MediaType.TV.value,
            seasons="S02",
            title="Other Show",
            year="2020",
            id=2,
        )
        plugin_module.TransferHistoryOper = lambda: types.SimpleNamespace(list_by_date=lambda _cutoff: [selected, skipped])

        response = plugin.api_import_transfer_history({"days": 1})
        tracks = plugin._ensure_state_store().get_tv_tracks()

        self.assertTrue(response["success"])
        self.assertEqual(1, response["data"]["tv_imported"])
        self.assertIn("60625", tracks)
        self.assertNotIn("99999", tracks)

    def test_package_version_matches_plugin_version(self):
        plugin_module = load_plugin_module()
        payload = json.loads((Path(__file__).resolve().parents[1] / "package.v2.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["NextReleaseTracker"]["version"], plugin_module.NextReleaseTracker.plugin_version)

    def test_form_contains_explicit_whitelist_sections(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin({"enabled": True, "enable_tv": True, "enable_movie": False})

        form, model = plugin.get_form()
        form_text = repr(form)

        self.assertIn("\u5267\u96c6\u8ffd\u66f4\u540d\u5355", form_text)
        self.assertIn("\u624b\u52a8\u52a0\u5165\u5267\u96c6\u540d\u5355", form_text)
        self.assertIn("\u6e05\u7a7a\u767d\u540d\u5355", form_text)
        self.assertIn("\u5f53\u524d\u5df2\u52a0\u5165\u7684\u5267\u96c6", form_text)
        self.assertIn("\u5f53\u524d\u5df2\u52a0\u5165\u7684\u7535\u5f71", form_text)
        self.assertIn("\u5267\u96c6\u5019\u9009\u540d\u5355", form_text)
        self.assertIn("\u7535\u5f71\u5019\u9009\u540d\u5355", form_text)
        self.assertNotIn("\u5019\u9009\u68c0\u7d22", form_text)
        self.assertIn("\u641c\u7d22\u5267\u96c6\u5019\u9009\u5217\u8868", form_text)
        self.assertIn("\u641c\u7d22\u7535\u5f71\u5019\u9009\u5217\u8868", form_text)
        self.assertNotIn("\u4e0b\u8868\u6309\u6700\u8fd1\u65f6\u95f4\u6392\u5e8f", form_text)
        self.assertNotIn("\u7535\u5f71\u9ed8\u8ba4\u53ea\u4f1a\u6309 TMDB collection \u81ea\u52a8\u627e\u4e0b\u4e00\u90e8", form_text)
        self.assertIn("\u6bcf\u5206\u949f\u6700\u591a 5 \u6b21", form_text)
        self.assertIn("\u7535\u5f71\u624b\u52a8\u5173\u8054\uff08\u9ad8\u7ea7\uff09", form_text)
        self.assertIn("603=604,605", form_text)
        self.assertIn("TMDB \u7f16\u53f7", form_text)
        self.assertIn("\u9ed1\u5ba2\u5e1d\u56fd", form_text)
        self.assertIn("TMDB collection", form_text)
        self.assertEqual("", model["tv_candidate_id"])
        self.assertEqual("", model["movie_remove_id"])
        self.assertEqual("", model["tv_candidate_search_text"])
        self.assertEqual("", model["movie_candidate_search_text"])
        self.assertEqual(1, model["tv_candidate_page"])
        self.assertEqual(1, model["movie_candidate_page"])
        self.assertEqual(1, model["tv_selected_page"])
        self.assertEqual(1, model["movie_selected_page"])
        self.assertNotIn("tv_search_text", model)
        self.assertNotIn("movie_search_text", model)
        self.assertEqual(5, model["max_tmdb_calls_per_minute"])
        self.assertIn("操作", form_text)
        self.assertFalse(
            any(
                node.get("component") == "VTextarea"
                and (node.get("props") or {}).get("model") in {"tracked_tv_ids", "tracked_movie_ids"}
                for node in iter_component_nodes(form)
            )
        )

    def test_form_selection_sections_are_not_hidden_by_mode_switches(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin({"enabled": True, "enable_tv": True, "enable_movie": False})

        form, _ = plugin.get_form()
        show_values = [
            str((node.get("props") or {}).get("show", ""))
            for node in iter_component_nodes(form)
            if node.get("component") == "VCard" and "show" in (node.get("props") or {})
        ]

        self.assertNotIn("{{ model.enable_tv !== false }}", show_values)
        self.assertNotIn("{{ model.enable_movie !== false }}", show_values)
        self.assertNotIn("{{ enable_tv }}", show_values)
        self.assertNotIn("{{ enable_movie }}", show_values)
        form_text = repr(form)
        self.assertIn("\u641c\u7d22\u5267\u96c6\u5019\u9009\u5217\u8868", form_text)
        self.assertIn("\u641c\u7d22\u7535\u5f71\u5019\u9009\u5217\u8868", form_text)
        self.assertFalse(any(node.get("component") == "VSheet" for node in iter_component_nodes(form)))

    def test_candidate_lookup_dedupes_and_sorts_by_recent_time(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin({"enabled": True, "enable_tv": True, "enable_movie": True, "history_days": 365})

        histories = [
            types.SimpleNamespace(
                status=True,
                tmdbid=60625,
                type=plugin_module.MediaType.TV.value,
                seasons="S01",
                title="Rick and Morty",
                year="2013",
                date="2026-06-02 09:00:00",
            ),
            types.SimpleNamespace(
                status=True,
                tmdbid=60625,
                type=plugin_module.MediaType.TV.value,
                seasons="S02",
                title="Rick and Morty",
                year="2013",
                date="2026-06-03 09:00:00",
            ),
            types.SimpleNamespace(
                status=True,
                tmdbid=550,
                type=plugin_module.MediaType.MOVIE.value,
                seasons=None,
                title="Fight Club",
                year="1999",
                date="2026-06-04 09:00:00",
            ),
        ]
        subscriptions = [
            types.SimpleNamespace(
                tmdbid=1396,
                type=plugin_module.MediaType.TV.value,
                season=5,
                name="Breaking Bad",
                year="2008",
                last_update="2026-06-05 08:00:00",
                date="2026-06-01 08:00:00",
            )
        ]
        plugin_module.TransferHistoryOper = lambda: types.SimpleNamespace(list_by_date=lambda _cutoff: histories)
        plugin_module.SubscribeOper = lambda: types.SimpleNamespace(list=lambda state=None: subscriptions)

        tv_candidates, _ = plugin._sorted_form_candidates(plugin_module.MediaType.TV.value)
        movie_candidates, _ = plugin._sorted_form_candidates(plugin_module.MediaType.MOVIE.value)

        self.assertEqual([1396, 60625], [item["tmdb_id"] for item in tv_candidates])
        self.assertEqual(5, tv_candidates[0]["latest_season"])
        self.assertEqual(2, tv_candidates[1]["latest_season"])
        self.assertEqual(5, tv_candidates[0]["baseline_season"])
        self.assertEqual(2, tv_candidates[1]["baseline_season"])
        self.assertEqual(["\u6700\u8fd1\u8ba2\u9605"], tv_candidates[0]["sources"])
        self.assertEqual(["\u6700\u8fd1\u5165\u5e93"], movie_candidates[0]["sources"])

    def test_form_renders_candidate_table_when_local_history_exists(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        histories = [
            types.SimpleNamespace(
                status=True,
                tmdbid=60625,
                type=plugin_module.MediaType.TV.value,
                seasons="S09",
                title="Rick and Morty",
                year="2013",
                date="2026-06-03 09:00:00",
            )
        ]
        plugin_module.TransferHistoryOper = lambda: types.SimpleNamespace(list_by_date=lambda _cutoff: histories)
        plugin_module.SubscribeOper = lambda: types.SimpleNamespace(list=lambda state=None: [])

        plugin.init_plugin({"enabled": True, "enable_tv": True, "enable_movie": False})
        form, _ = plugin.get_form()
        form_text = repr(form)
        self.assertIn("\u641c\u7d22\u5267\u96c6\u5019\u9009\u5217\u8868", form_text)
        self.assertIn("\u5267\u96c6\u5019\u9009\u540d\u5355", form_text)
        self.assertIn("Rick and Morty", form_text)
        self.assertIn("\u52a0\u5165\u767d\u540d\u5355", form_text)
        self.assertIn("VPagination", form_text)
        self.assertIn("tv_candidate_page", form_text)
        self.assertNotIn("\u4e0b\u8868\u6309\u6700\u8fd1\u65f6\u95f4\u6392\u5e8f", form_text)
        self.assertNotIn("鎼滃墽鍚嶆垨骞翠唤", form_text)

        nodes = list(iter_component_nodes(form))
        self.assertTrue(
            any(
                node.get("component") == "tr"
                and "tv_candidate_page" in str((node.get("props") or {}).get("show", ""))
                for node in nodes
            )
        )
        self.assertTrue(any(node.get("component") == "VTable" for node in nodes))
        self.assertFalse(
            any(
                node.get("component") == "VCard"
                and "nrt-candidate-row" in str((node.get("props") or {}).get("class", ""))
                for node in nodes
            )
        )
        self.assertFalse(
            any(
                "{{ (() => {" in str(node.get("text", ""))
                and node.get("component") != "span"
                for node in nodes
            )
        )
        self.assertGreater(
            form_text.index("\u5f53\u524d\u5df2\u52a0\u5165\u7684\u5267\u96c6"),
            form_text.index("\u5267\u96c6\u5019\u9009\u540d\u5355"),
        )

    def test_form_keeps_selected_list_visible_when_no_local_candidates(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "enable_tv": True,
                "enable_movie": False,
                "tracked_tv_ids": "60625",
            }
        )

        form, _ = plugin.get_form()
        form_text = repr(form)

        self.assertIn("\u5f53\u524d\u5df2\u52a0\u5165\u7684\u5267\u96c6", form_text)
        self.assertIn("\u5267\u96c6\u5019\u9009\u540d\u5355", form_text)
        self.assertIn("60625", form_text)
        self.assertIn("\u6682\u65e0\u6700\u8fd1\u51fa\u73b0\u8fc7\u7684\u5267\u96c6\u5019\u9009\uff1b\u53ef\u4ee5\u76f4\u63a5\u624b\u52a8\u8f93\u5165 TMDB \u7f16\u53f7\u52a0\u5165\u3002", form_text)
        self.assertNotIn("\u4e0b\u8868\u6309\u6700\u8fd1\u65f6\u95f4\u6392\u5e8f", form_text)
        self.assertNotIn("\u52a0\u5165\u540e\uff0c\u63d2\u4ef6\u4f1a\u7ee7\u7eed\u5173\u6ce8\u8fd9\u90e8\u5267\u540e\u9762\u7684\u65b0\u4e00\u5b63", form_text)
        self.assertIn("tv_candidate_search_text", form_text)
        self.assertNotIn('\"model\": \"candidate_search_text\"', form_text)
        self.assertIn("待保存移除：TMDB-60625", form_text)

    def test_form_selected_tv_table_inline_remove_keeps_save_semantics(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "enable_tv": True,
                "enable_movie": False,
                "tracked_tv_ids": "60625",
                "manual_tv_seasons": "60625=9",
            }
        )

        form, _ = plugin.get_form()
        form_text = repr(form)

        self.assertIn("tv_selected_page", form_text)
        self.assertIn("model.tracked_tv_ids = ids.join('\\\\n')", form_text)
        self.assertIn("model.manual_tv_seasons = lines.join('\\\\n')", form_text)
        self.assertIn("待保存移除：TMDB-60625", form_text)

    def test_form_exposes_manual_tv_season_controls(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()

        plugin.init_plugin({"enabled": True, "enable_tv": True, "enable_movie": False})

        form, defaults = plugin.get_form()
        form_text = repr(form)

        self.assertIn("\u5df2\u8ffd\u5230\u7b2c\u51e0\u5b63", form_text)
        self.assertIn("tv_manual_season", form_text)
        self.assertIn("tv_season_edit_tmdb_id", form_text)
        self.assertIn("tv_season_edit_value", form_text)
        self.assertIn("tv_pending_notice", form_text)
        self.assertIn("\u5f85\u4fdd\u5b58\u64cd\u4f5c", form_text)
        self.assertIn("\u66f4\u65b0\u5b63\u6570", form_text)
        self.assertIn("\u6e05\u9664\u5b63\u6570", form_text)
        self.assertIn("manual_tv_seasons", form_text)
        self.assertIn("\u5df2\u8ffd\u5b63\u6570", form_text)
        self.assertNotIn("\u5267\u96c6\u5df2\u8ffd\u5b63\u6570\uff08\u6bcf\u884c\u4e00\u6761\uff09", form_text)
        self.assertEqual("", defaults["manual_tv_seasons"])
        self.assertEqual("", defaults["tv_manual_season"])
        self.assertEqual("", defaults["tv_pending_notice"])
        self.assertEqual("", defaults["tv_season_edit_tmdb_id"])
        self.assertEqual("", defaults["tv_season_edit_value"])

    def test_form_tv_table_does_not_render_raw_status_expression(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "enable_tv": True,
                "tracked_tv_ids": "247718",
            }
        )

        form, _ = plugin.get_form()
        nodes = list(iter_component_nodes(form))
        self.assertFalse(any("String(season).padStart(2, '0')" in str(node.get("text", "")) for node in nodes))
        self.assertFalse(any("const mappings =" in str(node.get("text", "")) for node in nodes))
        self.assertFalse(any("{{ (() => {" in str(node.get("text", "")) for node in nodes))

    def test_form_tv_table_shows_manual_season_column_without_textarea(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "enable_tv": True,
                "tracked_tv_ids": "247718",
                "manual_tv_seasons": "247718=2",
            }
        )

        form, _ = plugin.get_form()
        form_text = repr(form)

        self.assertIn("S02", form_text)
        self.assertIn("已追到 S02", form_text)
        self.assertIn("剧集编号（已加入）", form_text)
        self.assertNotIn("剧集已追季数（每行一条）", form_text)
        self.assertNotIn("当前已加入 1 个剧集：TMDB-247718（当前到 S02）", form_text)
    def test_form_tv_table_pending_status_is_bound_to_manual_tv_seasons_model(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "enable_tv": True,
                "tracked_tv_ids": "247718",
            }
        )

        form, _ = plugin.get_form()
        nodes = list(iter_component_nodes(form))
        self.assertTrue(
            any(
                node.get("component") == "td"
                and node.get("text") == "待保存，保存后生效"
                and "manual_tv_seasons" in str((node.get("props") or {}).get("show", ""))
                for node in nodes
            )
        )

    def test_form_new_unsaved_tv_entry_shows_pending_instead_of_missing_season_prompt(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "enable_tv": True,
                "tracked_tv_ids": "",
            }
        )

        form = plugin._selection_current_table_live(
            media_label="剧集",
            model_key="tracked_tv_ids",
            page_model="tv_selected_page",
            selected_ids=[],
            track_lookup={},
            is_movie=False,
            candidates=[{"tmdb_id": 247718, "title": "TMDB-247718", "baseline_season": 0}],
            manual_tv_season_model="manual_tv_seasons",
        )
        nodes = list(iter_component_nodes(form))

        self.assertTrue(
            any(
                node.get("component") == "td"
                and node.get("text") == "待保存，保存后生效"
                and "savedIds = []" in str((node.get("props") or {}).get("show", ""))
                for node in nodes
            )
        )
        self.assertTrue(
            any(
                node.get("component") == "td"
                and node.get("text") == "请设置已追季数"
                and "!((() => { const ids =" in str((node.get("props") or {}).get("show", ""))
                and "savedIds = []" in str((node.get("props") or {}).get("show", ""))
                for node in nodes
            )
        )

    def test_form_tv_table_pending_season_cell_ignores_stale_instance_manual_season(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "enable_tv": True,
                "tracked_tv_ids": "247718",
            }
        )
        plugin._manual_tv_seasons = {247718: 2}

        form, _ = plugin.get_form()
        nodes = list(iter_component_nodes(form))
        pending_cells = [
            node
            for node in nodes
            if node.get("component") == "td"
            and node.get("text") in {"待保存", "待保存，保存后生效", "S02"}
        ]
        self.assertFalse(any(node.get("text") == "S02" for node in pending_cells))
        self.assertTrue(any(node.get("text") == "待保存" for node in pending_cells))

    def test_form_tv_add_actions_prepend_and_emit_pending_notice(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin({"enabled": True, "enable_tv": True, "enable_movie": False})

        form, _ = plugin.get_form()
        form_text = repr(form)

        self.assertIn("ids.unshift(candidate)", form_text)
        self.assertIn("tv_pending_notice", form_text)
        self.assertIn("待保存新增", form_text)
        self.assertIn("tv_selected_page", form_text)
        self.assertIn("VPagination", form_text)

    def test_form_candidates_ignore_tmdb_discover_noise(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin_module.TransferHistoryOper = lambda: types.SimpleNamespace(list_by_date=lambda _cutoff: [])
        plugin_module.SubscribeOper = lambda: types.SimpleNamespace(list=lambda state=None: [])
        plugin._tmdb_discover_candidates = lambda media_type: [
            types.SimpleNamespace(
                tmdb_id=60625 if media_type == plugin_module.MediaType.TV.value else 603,
                title="Discover Candidate",
                year="2026",
                release_date="2026-06-03",
                number_of_seasons=9,
            )
        ]

        plugin.init_plugin({"enabled": True, "enable_tv": True, "enable_movie": True})

        tv_candidates, hidden_tv_candidates = plugin._sorted_form_candidates(plugin_module.MediaType.TV.value)
        movie_candidates, hidden_movie_candidates = plugin._sorted_form_candidates(plugin_module.MediaType.MOVIE.value)

        self.assertEqual([], tv_candidates)
        self.assertEqual([], movie_candidates)
        self.assertEqual(0, hidden_tv_candidates)
        self.assertEqual(0, hidden_movie_candidates)

    def test_form_candidates_filter_deleted_library_items(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        histories = [
            types.SimpleNamespace(
                status=True,
                tmdbid=60625,
                type=plugin_module.MediaType.TV.value,
                seasons="S09",
                title="Rick and Morty",
                year="2013",
                date="2026-06-03 09:00:00",
            ),
            types.SimpleNamespace(
                status=True,
                tmdbid=1396,
                type=plugin_module.MediaType.TV.value,
                seasons="S05",
                title="Breaking Bad",
                year="2008",
                date="2026-06-05 08:00:00",
            ),
        ]
        plugin_module.TransferHistoryOper = lambda: types.SimpleNamespace(list_by_date=lambda _cutoff: histories)
        plugin_module.SubscribeOper = lambda: types.SimpleNamespace(list=lambda state=None: [])
        plugin.chain = types.SimpleNamespace(
            recognize_media=lambda **kwargs: types.SimpleNamespace(tmdb_id=kwargs["tmdbid"])
        )
        plugin._media_server_chain = types.SimpleNamespace(
            media_exists=lambda media: media if getattr(media, "tmdb_id", None) == 1396 else None
        )

        tv_candidates, hidden_tv_candidates = plugin._sorted_form_candidates(plugin_module.MediaType.TV.value)

        self.assertEqual([1396], [item["tmdb_id"] for item in tv_candidates])
        self.assertEqual(0, hidden_tv_candidates)
        self.assertNotIn(60625, [item["tmdb_id"] for item in tv_candidates])

    def test_selected_entries_bootstrap_tracks_from_local_catalog(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()

        histories = [
            types.SimpleNamespace(
                status=True,
                tmdbid=60625,
                type=plugin_module.MediaType.TV.value,
                seasons="S09",
                title="Rick and Morty",
                year="2013",
                date="2026-06-03 09:00:00",
            ),
            types.SimpleNamespace(
                status=True,
                tmdbid=550,
                type=plugin_module.MediaType.MOVIE.value,
                seasons=None,
                title="Fight Club",
                year="1999",
                date="2026-06-03 10:00:00",
            ),
        ]
        plugin_module.TransferHistoryOper = lambda: types.SimpleNamespace(list_by_date=lambda _cutoff: histories)
        plugin_module.SubscribeOper = lambda: types.SimpleNamespace(list=lambda state=None: [])

        plugin.chain = types.SimpleNamespace(
            recognize_media=lambda **kwargs: types.SimpleNamespace(
                title="Fight Club",
                year="1999",
                collection_id=999,
            )
        )
        plugin.init_plugin(
            {
                "enabled": True,
                "enable_tv": True,
                "enable_movie": True,
                "tracked_tv_ids": "60625",
                "tracked_movie_ids": "550",
            }
        )

        store = plugin._ensure_state_store()
        tv_track = store.get_tv_tracks()["60625"]
        movie_track = next(iter(store.get_movie_tracks().values()))

        self.assertEqual(9, tv_track["latest_season"])
        self.assertEqual(550, movie_track["anchor_tmdb_id"])
        self.assertEqual(999, movie_track["collection_id"])
        self.assertEqual("collection:999", movie_track["track_key"])

    def test_selected_tv_without_local_baseline_does_not_guess_from_tmdb_discover(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()

        plugin_module.TransferHistoryOper = lambda: types.SimpleNamespace(list_by_date=lambda _cutoff: [])
        plugin_module.SubscribeOper = lambda: types.SimpleNamespace(list=lambda state=None: [])
        plugin._tmdb_discover_candidates = lambda media_type: (
            [
                types.SimpleNamespace(
                    tmdb_id=60625,
                    title="Rick and Morty",
                    year="2013",
                    release_date="2026-06-03",
                    number_of_seasons=9,
                )
            ]
            if media_type == plugin_module.MediaType.TV.value
            else []
        )

        plugin.init_plugin(
            {
                "enabled": True,
                "enable_tv": True,
                "tracked_tv_ids": "60625",
            }
        )

        store = plugin._ensure_state_store()
        self.assertEqual([60625], store.get_selected_tv_ids())
        self.assertEqual({}, store.get_tv_tracks())

    def test_manual_tv_baseline_bootstraps_selected_track_without_local_history(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()

        plugin_module.TransferHistoryOper = lambda: types.SimpleNamespace(list_by_date=lambda _cutoff: [])
        plugin_module.SubscribeOper = lambda: types.SimpleNamespace(list=lambda state=None: [])
        plugin._tmdb_discover_candidates = lambda media_type: []

        plugin.init_plugin(
            {
                "enabled": True,
                "enable_tv": True,
                "tracked_tv_ids": "247718",
                "manual_tv_seasons": "247718=2",
            }
        )

        store = plugin._ensure_state_store()
        tv_track = store.get_tv_tracks()["247718"]

        self.assertEqual(2, tv_track["latest_season"])
        self.assertEqual("TMDB-247718", tv_track["title"])
        self.assertEqual("247718=2", plugin._current_config_snapshot()["manual_tv_seasons"])

    def test_transient_tv_season_edit_fields_are_applied_on_save(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()

        plugin_module.TransferHistoryOper = lambda: types.SimpleNamespace(list_by_date=lambda _cutoff: [])
        plugin_module.SubscribeOper = lambda: types.SimpleNamespace(list=lambda state=None: [])
        plugin._tmdb_discover_candidates = lambda media_type: []

        plugin.init_plugin(
            {
                "enabled": True,
                "enable_tv": True,
                "tracked_tv_ids": "247718",
                "tv_season_edit_tmdb_id": "247718",
                "tv_season_edit_value": "S02",
            }
        )

        store = plugin._ensure_state_store()
        tv_track = store.get_tv_tracks()["247718"]

        self.assertEqual([247718], plugin._selected_tv_ids)
        self.assertEqual(2, tv_track["latest_season"])
        self.assertEqual("247718=2", plugin._current_config_snapshot()["manual_tv_seasons"])

    def test_transient_tv_add_fields_accept_sxx_and_bootstrap_track_on_save(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()

        plugin_module.TransferHistoryOper = lambda: types.SimpleNamespace(list_by_date=lambda _cutoff: [])
        plugin_module.SubscribeOper = lambda: types.SimpleNamespace(list=lambda state=None: [])
        plugin._tmdb_discover_candidates = lambda media_type: []

        plugin.init_plugin(
            {
                "enabled": True,
                "enable_tv": True,
                "tracked_tv_ids": "",
                "tv_candidate_id": "247718",
                "tv_manual_season": "S02",
            }
        )

        store = plugin._ensure_state_store()
        tv_track = store.get_tv_tracks()["247718"]

        self.assertEqual([247718], plugin._selected_tv_ids)
        self.assertEqual(2, tv_track["latest_season"])
        self.assertEqual("247718=2", plugin._current_config_snapshot()["manual_tv_seasons"])

    def test_removed_tv_id_is_pruned_from_manual_tv_seasons_on_save(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()

        plugin.init_plugin(
            {
                "enabled": True,
                "enable_tv": True,
                "tracked_tv_ids": "60625",
                "manual_tv_seasons": "60625=3\n247718=2",
            }
        )

        self.assertEqual("60625=3", plugin._current_config_snapshot()["manual_tv_seasons"])

    def test_cleared_tv_ids_prune_manual_tv_seasons_on_save(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()

        plugin.init_plugin(
            {
                "enabled": True,
                "enable_tv": True,
                "tracked_tv_ids": "",
                "manual_tv_seasons": "247718=2",
            }
        )

        self.assertEqual("", plugin._current_config_snapshot()["manual_tv_seasons"])

    def test_manual_movie_selection_bootstraps_track_without_local_history(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()

        plugin_module.TransferHistoryOper = lambda: types.SimpleNamespace(list_by_date=lambda _cutoff: [])
        plugin_module.SubscribeOper = lambda: types.SimpleNamespace(list=lambda state=None: [])
        plugin._tmdb_discover_candidates = lambda media_type: []
        plugin.chain = types.SimpleNamespace(
            recognize_media=lambda **kwargs: types.SimpleNamespace(
                title="Interstellar",
                year="2014",
                collection_id=None,
            )
        )

        plugin.init_plugin(
            {
                "enabled": True,
                "enable_tv": False,
                "enable_movie": True,
                "tracked_movie_ids": "157336",
            }
        )

        store = plugin._ensure_state_store()
        movie_track = store.get_movie_tracks()["movie:157336"]

        self.assertEqual(157336, movie_track["anchor_tmdb_id"])
        self.assertEqual([157336], movie_track["known_tmdb_ids"])
        self.assertEqual("Interstellar", movie_track["title"])
        self.assertEqual("2014", movie_track["year"])

    def test_manual_movie_mapping_can_be_edited_from_config_and_syncs_back(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "enable_movie": True,
                "manual_movie_mappings": "603=604,605 # Matrix sequel chain",
            }
        )

        store = plugin._ensure_state_store()
        mapping = store.get_manual_mapping(603)

        self.assertEqual([604, 605], mapping["target_tmdb_ids"])
        self.assertEqual("Matrix sequel chain", mapping["note"])
        self.assertEqual(
            "603=604,605 # Matrix sequel chain",
            plugin._current_config_snapshot()["manual_movie_mappings"],
        )

        response = plugin.api_manual_map_add(
            {
                "source_tmdb_id": 603,
                "target_tmdb_ids": [604, 605, 606],
                "note": "Matrix sequel chain",
            }
        )

        self.assertTrue(response["success"])
        self.assertEqual(
            "603=604,605,606 # Matrix sequel chain",
            plugin._config["manual_movie_mappings"],
        )

    def test_invalid_cron_falls_back_to_default(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()

        plugin.init_plugin({"enabled": True, "cron": "bad cron"})

        self.assertEqual("0 3 * * 1", plugin._cron)
        self.assertEqual("0 3 * * 1", plugin._config["cron"])

    def test_tmdb_minute_limit_is_capped_at_five(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()

        plugin.init_plugin({"enabled": True, "max_tmdb_calls_per_minute": 99})

        self.assertEqual(5, plugin._max_tmdb_calls_per_minute)
        self.assertEqual(5, plugin._config["max_tmdb_calls_per_minute"])

    def test_get_service_uses_minute_tick_for_paced_scans(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()

        plugin.init_plugin({"enabled": True, "enable_tv": True, "cron": "0 3 * * 1"})
        services = plugin.get_service()

        self.assertEqual("scan_tick", services[0]["id"])
        self.assertEqual("* * * * *", services[0]["trigger"])
        self.assertEqual("cron_tick", services[0]["func_kwargs"]["reason"])

    def test_cron_tick_spreads_weekly_scan_across_cycle(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "notify": False,
                "enable_tv": True,
                "enable_movie": False,
                "tracked_tv_ids": "100\n101\n102\n103\n104\n105\n106",
                "cron": "0 3 * * 1",
            }
        )
        store = plugin._ensure_state_store()
        for tmdb_id in range(100, 107):
            store.acknowledge_tv_completion(
                tmdb_id=tmdb_id,
                title=f"Show {tmdb_id}",
                year="2020",
                season=1,
                source="manual",
            )

        calls = []
        plugin._tmdb_chain = types.SimpleNamespace(
            tmdb_seasons=lambda tmdb_id: calls.append(tmdb_id) or []
        )
        plugin._now = lambda: "2026-06-01 03:00:00"

        first = plugin._run_rescan(scope="tv", reason="cron_tick", notify=False)

        self.assertTrue(first["success"])
        self.assertEqual(1, first["planned_tracks"])
        self.assertEqual(1, first["scanned_tv"])
        self.assertEqual(6, first["remaining_tracks"])
        self.assertEqual([100], calls)

        plugin._now = lambda: "2026-06-02 03:00:00"
        second = plugin._run_rescan(scope="tv", reason="cron_tick", notify=False)

        self.assertTrue(second["success"])
        self.assertEqual(1, second["planned_tracks"])
        self.assertEqual(1, second["scanned_tv"])
        self.assertEqual(5, second["remaining_tracks"])
        self.assertEqual([100, 101], calls)

    def test_cron_tick_without_due_tasks_updates_runtime_and_records_idle_log(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "notify": False,
                "enable_tv": True,
                "enable_movie": False,
                "tracked_tv_ids": "100",
                "cron": "0 3 * * 1",
            }
        )
        store = plugin._ensure_state_store()
        store.acknowledge_tv_completion(
            tmdb_id=100,
            title="Show 100",
            year="2020",
            season=1,
            source="manual",
        )
        runtime = store.get_runtime_state()
        runtime["scan_plan"] = {
            "cron": "0 3 * * 1",
            "scope": "tv",
            "period_minutes": 10080,
            "cycle_started_at": "2026-06-01 03:00:00",
            "task_ids": ["tv:100"],
            "pending_task_ids": [],
        }
        store.update_runtime(runtime)
        plugin._now = lambda: "2026-06-01 03:01:00"

        summary = plugin._run_rescan(scope="tv", reason="cron_tick", notify=False)
        runtime_after = store.get_runtime_state()
        logs = store.get_action_log()

        self.assertTrue(summary["success"])
        self.assertEqual(0, summary["planned_tracks"])
        self.assertEqual("2026-06-01 03:01:00", runtime_after["last_scan_finished_at"])
        self.assertEqual(0, runtime_after["last_scan_summary"]["planned_tracks"])
        self.assertEqual("cron_idle", logs[-1]["action"])
        self.assertEqual(1, runtime_after["current_cycle_stats"]["tick_count"])
        self.assertEqual(1, runtime_after["current_cycle_stats"]["idle_tick_count"])

    def test_new_cycle_clears_previous_action_log(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "notify": False,
                "enable_tv": True,
                "enable_movie": False,
                "tracked_tv_ids": "100",
                "cron": "0 3 * * 1",
            }
        )
        store = plugin._ensure_state_store()
        store.append_action(level="info", action="old_action", message="old")
        runtime = store.get_runtime_state()
        runtime["scan_plan"] = {
            "cron": "0 3 * * 1",
            "scope": "tv",
            "period_minutes": 10080,
            "cycle_started_at": "2026-05-01 03:00:00",
            "task_ids": ["tv:100"],
            "pending_task_ids": ["tv:100"],
        }
        store.update_runtime(runtime)

        plugin._resolve_scan_task_plan(scope="tv", reason="cron_tick")

        self.assertEqual([], store.get_action_log())
        self.assertEqual(0, store.get_runtime_state()["current_cycle_stats"]["tick_count"])

    def test_scheduled_plan_prunes_legacy_logs_before_cycle_start(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "notify": False,
                "enable_tv": True,
                "enable_movie": False,
                "tracked_tv_ids": "100",
                "cron": "0 3 * * 1",
            }
        )
        store = plugin._ensure_state_store()
        store.append_action(level="info", action="legacy", message="old")
        runtime = store.get_runtime_state()
        runtime["scan_plan"] = {
            "cron": "0 3 * * 1",
            "scope": "tv",
            "period_minutes": 10080,
            "cycle_started_at": "2099-01-01 00:00:00",
            "task_ids": ["tv:100"],
            "pending_task_ids": ["tv:100"],
        }
        store.update_runtime(runtime)

        plugin._resolve_scan_task_plan(scope="tv", reason="cron_tick")

        self.assertEqual([], store.get_action_log())

    def test_locked_scan_increments_cycle_locked_skip_count(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "notify": False,
                "enable_tv": True,
                "enable_movie": False,
                "tracked_tv_ids": "100",
                "cron": "0 3 * * 1",
            }
        )
        store = plugin._ensure_state_store()
        store.update_runtime(
            {
                "current_cycle_stats": {
                    "cycle_started_at": "2026-06-01 03:00:00",
                    "tick_count": 0,
                    "idle_tick_count": 0,
                    "active_tick_count": 0,
                    "processed_task_count": 0,
                    "locked_skip_count": 0,
                    "last_active_tick_at": None,
                    "last_locked_skip_at": None,
                }
            }
        )
        plugin._scan_lock.acquire()
        try:
            summary = plugin._run_rescan(scope="tv", reason="cron_tick", notify=False)
        finally:
            plugin._scan_lock.release()

        runtime = store.get_runtime_state()
        self.assertFalse(summary["success"])
        self.assertEqual(1, runtime["current_cycle_stats"]["locked_skip_count"])

    def test_auto_subscribe_decision_log_is_recorded_for_tv(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "notify": True,
                "enable_tv": True,
                "enable_movie": False,
                "tracked_tv_ids": "60625",
            }
        )
        store = plugin._ensure_state_store()
        store.acknowledge_tv_completion(
            tmdb_id=60625,
            title="Rick and Morty",
            year="2013",
            season=1,
            source="manual",
        )
        plugin._tmdb_chain = types.SimpleNamespace(
            tmdb_seasons=lambda tmdb_id: [
                {"season_number": 2, "episode_count": 10, "air_date": "2026-06-01"},
                {"season_number": 3, "episode_count": 10, "air_date": "2026-06-01"},
            ]
        )
        plugin._media_server_chain = types.SimpleNamespace(media_exists=lambda media: None)
        plugin._subscribe_exists = lambda **kwargs: False

        plugin._run_rescan(scope="tv", reason="test", notify=True)

        logs = store.get_action_log()
        decision = [entry for entry in logs if entry["action"] == "auto_subscribe_decision"][-1]
        self.assertEqual("earliest_available_season", decision["context"]["reason"])
        self.assertEqual(2, decision["context"]["selected"])

    def test_page_shows_current_cycle_stats(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "notify": True,
                "enable_tv": True,
                "enable_movie": False,
                "cron": "0 3 * * 1",
            }
        )
        store = plugin._ensure_state_store()
        store.update_runtime(
            {
                "current_cycle_stats": {
                    "cycle_started_at": "2026-06-01 03:00:00",
                    "tick_count": 7,
                    "idle_tick_count": 5,
                    "active_tick_count": 2,
                    "processed_task_count": 2,
                    "locked_skip_count": 1,
                    "last_active_tick_at": "2026-06-01 05:00:00",
                    "last_locked_skip_at": "2026-06-01 05:01:00",
                }
            }
        )

        page = plugin.get_page()
        page_text = repr(page)
        self.assertIn("本周期 tick 次数", page_text)
        self.assertIn("本周期空跑次数", page_text)
        self.assertIn("本周期锁冲突跳过", page_text)

    def test_manual_rescan_stops_after_five_tmdb_calls_per_minute(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "notify": False,
                "enable_tv": True,
                "enable_movie": False,
                "tracked_tv_ids": "200\n201\n202\n203\n204\n205",
            }
        )
        store = plugin._ensure_state_store()
        for tmdb_id in range(200, 206):
            store.acknowledge_tv_completion(
                tmdb_id=tmdb_id,
                title=f"Show {tmdb_id}",
                year="2020",
                season=1,
                source="manual",
            )

        calls = []
        plugin._tmdb_chain = types.SimpleNamespace(
            tmdb_seasons=lambda tmdb_id: calls.append(tmdb_id) or []
        )
        plugin._now = lambda: "2026-06-03 10:00:00"

        summary = plugin._run_rescan(scope="tv", reason="api", notify=False)

        self.assertTrue(summary["success"])
        self.assertTrue(summary["budget_exhausted"])
        self.assertEqual(5, summary["tmdb_calls_used"])
        self.assertEqual(5, summary["scanned_tv"])
        self.assertEqual(1, summary["remaining_tracks"])
        self.assertEqual([200, 201, 202, 203, 204], calls)

    def test_tv_rescan_without_notify_keeps_track_and_marks_pending(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "notify": False,
                "enable_tv": True,
                "enable_movie": False,
                "tracked_tv_ids": "60625",
            }
        )
        store = plugin._ensure_state_store()
        store.acknowledge_tv_completion(
            tmdb_id=60625,
            title="Rick and Morty",
            year="2013",
            season=1,
            source="manual",
        )
        plugin._tmdb_chain = types.SimpleNamespace(
            tmdb_seasons=lambda tmdb_id: [
                {"season_number": 2, "episode_count": 10, "air_date": "2026-06-01"},
            ]
        )
        plugin._media_server_chain = types.SimpleNamespace(media_exists=lambda media: None)
        plugin._subscribe_exists = lambda **kwargs: False

        summary = plugin._run_rescan(scope="tv", reason="test", notify=False)
        track = store.get_tv_tracks()["60625"]

        self.assertTrue(summary["success"])
        self.assertEqual(1, summary["tv_candidates"])
        self.assertEqual(0, summary["notifications_sent"])
        self.assertEqual(0, summary["tracks_completed"])
        self.assertEqual(1, summary["tracks_updated"])
        self.assertEqual([2], track["pending_seasons"])
        self.assertEqual([60625], plugin._selected_tv_ids)

    def test_movie_rescan_without_notify_keeps_track_and_marks_pending(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "notify": False,
                "enable_tv": False,
                "enable_movie": True,
                "tracked_movie_ids": "603",
            }
        )
        store = plugin._ensure_state_store()
        track = store.upsert_movie_track(
            anchor_tmdb_id=603,
            title="The Matrix",
            year="1999",
            source="manual",
            collection_id=2344,
            known_tmdb_ids=[603],
        )
        store.acknowledge_movie_completion(
            track_key=track["track_key"],
            tmdb_id=603,
            title="The Matrix",
            year="1999",
            source="manual",
            collection_id=2344,
        )
        plugin._tmdb_chain = types.SimpleNamespace(
            tmdb_collection=lambda collection_id: [
                {
                    "tmdb_id": 604,
                    "title": "The Matrix Reloaded",
                    "year": "2003",
                    "release_date": "2026-06-01",
                    "collection_id": 2344,
                }
            ]
        )
        plugin._media_server_chain = types.SimpleNamespace(media_exists=lambda media: None)
        plugin._subscribe_exists = lambda **kwargs: False

        summary = plugin._run_rescan(scope="movie", reason="test", notify=False)
        movie_tracks = store.get_movie_tracks()
        updated_track = movie_tracks["collection:2344"]

        self.assertTrue(summary["success"])
        self.assertEqual(1, summary["movie_candidates"])
        self.assertEqual(0, summary["notifications_sent"])
        self.assertEqual(0, summary["tracks_completed"])
        self.assertEqual(1, summary["tracks_updated"])
        self.assertEqual([604], updated_track["pending_tmdb_ids"])
        self.assertEqual([603], plugin._selected_movie_ids)

    def test_movie_rescan_with_notify_uses_movie_friendly_copy(self):
        plugin_module = load_plugin_module()
        plugin_module.SubscribeChain.calls = []
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "notify": True,
                "enable_tv": False,
                "enable_movie": True,
                "tracked_movie_ids": "603",
            }
        )
        store = plugin._ensure_state_store()
        store.upsert_movie_track(
            anchor_tmdb_id=603,
            title="The Matrix",
            year="1999",
            source="manual",
            collection_id=2344,
        )
        store.acknowledge_movie_completion(
            track_key="collection:2344",
            tmdb_id=603,
            title="The Matrix",
            year="1999",
            collection_id=2344,
            source="manual",
        )
        plugin._tmdb_chain = types.SimpleNamespace(
            tmdb_collection=lambda collection_id: [
                {
                    "tmdb_id": 604,
                    "title": "The Matrix Reloaded",
                    "year": "2003",
                    "release_date": "2026-06-01",
                    "collection_id": 2344,
                }
            ]
        )
        plugin._media_server_chain = types.SimpleNamespace(media_exists=lambda media: None)
        plugin._subscribe_exists = lambda **kwargs: False

        summary = plugin._run_rescan(scope="movie", reason="test", notify=True)

        self.assertTrue(summary["success"])
        self.assertEqual(1, summary["movie_candidates"])
        self.assertEqual(1, summary["notifications_sent"])
        self.assertEqual(1, summary["tracks_completed"])
        self.assertEqual(1, len(plugin_module.SubscribeChain.calls))
        self.assertEqual(604, plugin_module.SubscribeChain.calls[0]["tmdbid"])
        self.assertIn("\u7eed\u4f5c The Matrix Reloaded(2003)", plugin._last_message["text"])
        self.assertIn("\u5df2\u81ea\u52a8\u6dfb\u52a0\u8ba2\u9605", plugin._last_message["text"])
        self.assertIn("\u5df2\u7ed3\u675f\u672c\u6761\u8ffd\u8e2a", plugin._last_message["text"])

    def test_tv_rescan_with_notify_keeps_track_when_auto_subscribe_fails(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "notify": True,
                "enable_tv": True,
                "enable_movie": False,
                "tracked_tv_ids": "60625",
            }
        )
        store = plugin._ensure_state_store()
        store.acknowledge_tv_completion(
            tmdb_id=60625,
            title="Rick and Morty",
            year="2013",
            season=1,
            source="manual",
        )
        plugin._tmdb_chain = types.SimpleNamespace(
            tmdb_seasons=lambda tmdb_id: [
                {"season_number": 2, "episode_count": 10, "air_date": "2026-06-01"},
            ]
        )
        plugin._media_server_chain = types.SimpleNamespace(media_exists=lambda media: None)
        plugin._subscribe_exists = lambda **kwargs: False
        plugin._subscribe_chain = types.SimpleNamespace(add=lambda **kwargs: (None, "failed"))

        summary = plugin._run_rescan(scope="tv", reason="test", notify=True)

        self.assertTrue(summary["success"])
        self.assertEqual(0, summary["notifications_sent"])
        self.assertEqual(0, summary["tracks_completed"])
        self.assertEqual(1, summary["tracks_updated"])
        self.assertEqual([2], store.get_tv_tracks()["60625"]["pending_seasons"])
        self.assertEqual([60625], plugin._selected_tv_ids)
        self.assertFalse(hasattr(plugin, "_last_message"))

    def test_movie_rescan_with_notify_keeps_track_when_auto_subscribe_fails(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "notify": True,
                "enable_tv": False,
                "enable_movie": True,
                "tracked_movie_ids": "603",
            }
        )
        store = plugin._ensure_state_store()
        track = store.upsert_movie_track(
            anchor_tmdb_id=603,
            title="The Matrix",
            year="1999",
            source="manual",
            collection_id=2344,
            known_tmdb_ids=[603],
        )
        store.acknowledge_movie_completion(
            track_key=track["track_key"],
            tmdb_id=603,
            title="The Matrix",
            year="1999",
            source="manual",
            collection_id=2344,
        )
        plugin._tmdb_chain = types.SimpleNamespace(
            tmdb_collection=lambda collection_id: [
                {
                    "tmdb_id": 604,
                    "title": "The Matrix Reloaded",
                    "year": "2003",
                    "release_date": "2026-06-01",
                    "collection_id": 2344,
                }
            ]
        )
        plugin._media_server_chain = types.SimpleNamespace(media_exists=lambda media: None)
        plugin._subscribe_exists = lambda **kwargs: False
        plugin._subscribe_chain = types.SimpleNamespace(add=lambda **kwargs: (None, "failed"))

        summary = plugin._run_rescan(scope="movie", reason="test", notify=True)

        self.assertTrue(summary["success"])
        self.assertEqual(0, summary["notifications_sent"])
        self.assertEqual(0, summary["tracks_completed"])
        self.assertEqual(1, summary["tracks_updated"])
        self.assertEqual([604], store.get_movie_tracks()["collection:2344"]["pending_tmdb_ids"])
        self.assertEqual([603], plugin._selected_movie_ids)
        self.assertFalse(hasattr(plugin, "_last_message"))

    def test_movie_rescan_with_existing_subscription_completes_without_notification(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "notify": True,
                "enable_tv": False,
                "enable_movie": True,
                "tracked_movie_ids": "603",
            }
        )
        store = plugin._ensure_state_store()
        store.upsert_movie_track(
            anchor_tmdb_id=603,
            title="The Matrix",
            year="1999",
            source="manual",
            collection_id=2344,
        )
        store.acknowledge_movie_completion(
            track_key="collection:2344",
            tmdb_id=603,
            title="The Matrix",
            year="1999",
            collection_id=2344,
            source="manual",
        )
        plugin._tmdb_chain = types.SimpleNamespace(
            tmdb_collection=lambda collection_id: [
                {
                    "tmdb_id": 604,
                    "title": "The Matrix Reloaded",
                    "year": "2003",
                    "release_date": "2026-06-01",
                    "collection_id": 2344,
                }
            ]
        )
        plugin._media_server_chain = types.SimpleNamespace(media_exists=lambda media: None)
        plugin._subscribe_exists = lambda **kwargs: True

        summary = plugin._run_rescan(scope="movie", reason="test", notify=True)

        self.assertTrue(summary["success"])
        self.assertEqual(1, summary["movie_candidates"])
        self.assertEqual(1, summary["existing_subscriptions"])
        self.assertEqual(0, summary["notifications_sent"])
        self.assertEqual(1, summary["tracks_completed"])
        self.assertEqual({}, store.get_movie_tracks())
        self.assertEqual([], plugin._selected_movie_ids)
        self.assertFalse(hasattr(plugin, "_last_message"))

    def test_movie_track_remove_by_collection_cleans_manual_mapping(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "enable_movie": True,
                "tracked_movie_ids": "603",
                "manual_movie_mappings": "603=604,605",
            }
        )
        store = plugin._ensure_state_store()
        track = store.upsert_movie_track(
            anchor_tmdb_id=603,
            title="The Matrix",
            year="1999",
            source="manual",
            collection_id=2344,
            known_tmdb_ids=[603],
        )
        store.acknowledge_movie_completion(
            track_key=track["track_key"],
            tmdb_id=603,
            title="The Matrix",
            year="1999",
            source="manual",
            collection_id=2344,
        )

        response = plugin.api_track_remove(
            {
                "media_type": "movie",
                "collection_id": 2344,
            }
        )

        self.assertTrue(response["success"])
        self.assertIsNone(store.get_manual_mapping(603))
        self.assertEqual([], store.get_selected_movie_ids())
        self.assertEqual("", plugin._config["manual_movie_mappings"])

    def test_manual_movie_mapping_remove_api_removes_config_snapshot(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "enable_movie": True,
                "manual_movie_mappings": "603=604,605",
            }
        )

        response = plugin.api_manual_map_remove({"source_tmdb_id": 603})

        self.assertTrue(response["success"])
        self.assertEqual("", plugin._config["manual_movie_mappings"])

    def test_page_contains_toolbar_sections_and_tv_only_actions(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "notify": True,
                "enable_tv": True,
                "enable_movie": False,
                "cron": "0 3 * * 1",
                "grace_days": 3,
                "history_days": 365,
                "log_retention": 200,
            }
        )
        store = plugin._ensure_state_store()
        store.acknowledge_tv_completion(
            tmdb_id=60625,
            title="Rick and Morty",
            year="2013",
            season=9,
            source="history_import",
        )
        store.append_action(
            level="success",
            action="history_import",
            message="Transfer history import completed",
            context={"days": 1},
        )
        store.update_runtime(
            {
                "last_scan_finished_at": "2026-06-03 22:15:44",
                "last_scan_scope": "tv",
                "last_scan_reason": "api",
                "last_scan_summary": {"errors": 0, "subscriptions_added": 0, "tracks_updated": 1},
                "last_history_import": {"records": 1, "tv_imported": 1, "movie_imported": 0, "errors": 0},
            }
        )

        page = plugin.get_page()
        page_text = repr(page)

        self.assertIn("\u8ffd\u5267\u52a9\u624b", page_text)
        self.assertIn("\u626b\u63cf\u5168\u90e8", page_text)
        self.assertIn("\u4ec5\u626b\u5267\u96c6", page_text)
        self.assertIn("\u8bca\u65ad\u8ba2\u9605\u4e8b\u4ef6", page_text)
        self.assertIn("\u8bca\u65ad\u6574\u7406\u4e8b\u4ef6", page_text)
        self.assertIn("\u56de\u586b\u6700\u8fd1 1 \u5929", page_text)
        self.assertNotIn("\u4ec5\u626b\u7535\u5f71", page_text)
        self.assertIn("\u8fd0\u884c\u6001\u8bca\u65ad", page_text)
        self.assertIn("\u5df2\u52a0\u5165\u7684\u5267\u96c6", page_text)
        self.assertNotIn("\u5f53\u524d\u8ffd\u8e2a\u4e2d\u7684\u5267\u96c6", page_text)
        self.assertIn("\u6700\u8fd1\u52a8\u4f5c\u65e5\u5fd7", page_text)
        self.assertIn("plugin/NextReleaseTracker/rescan?apikey=test-token", page_text)
        self.assertIn("plugin/NextReleaseTracker/diagnostic/event?apikey=test-token", page_text)

    def test_page_merges_track_status_into_selected_tables(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "notify": True,
                "enable_tv": True,
                "enable_movie": True,
                "tracked_tv_ids": "60625",
                "tracked_movie_ids": "603",
            }
        )
        store = plugin._ensure_state_store()
        store.upsert_tv_track(
            tmdb_id=60625,
            title="Rick and Morty",
            year="2013",
            latest_season=9,
            source="manual",
        )
        store.mark_tv_pending(60625, 10)
        movie_track = store.upsert_movie_track(
            anchor_tmdb_id=603,
            title="The Matrix",
            year="1999",
            source="manual",
            collection_id=2344,
            known_tmdb_ids=[603],
        )
        store.mark_movie_pending(movie_track["track_key"], 604)

        page_text = repr(plugin.get_page())

        self.assertNotIn("\u5f53\u524d\u8ffd\u8e2a\u4e2d\u7684\u7535\u5f71", page_text)
        self.assertIn("\u5df2\u53d1\u73b0\u5f85\u5904\u7406\u65b0\u5b63", page_text)
        self.assertIn("\u5df2\u53d1\u73b0\u5f85\u5904\u7406\u7eed\u4f5c", page_text)

    def test_runtime_diagnostic_event_verifies_and_cleans_up(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "notify": True,
                "enable_tv": True,
                "enable_movie": False,
                "cron": "0 3 * * 1",
                "grace_days": 3,
                "history_days": 365,
                "log_retention": 200,
            }
        )

        def dispatch(event_name, event_data):
            event = plugin_module.Event(event_data)
            if event_name == "subscribe_complete":
                return plugin.on_subscribe_complete(event)
            return plugin.on_transfer_complete(event)

        plugin._dispatch_diagnostic_event = dispatch

        response = plugin.api_diagnostic_event(
            {
                "event": "transfer_complete",
                "media_type": "tv",
                "tmdb_id": 9901234,
                "title": "Diagnostic Series",
                "season": 2,
                "cleanup": True,
            }
        )

        self.assertTrue(response["success"])
        result = response["data"]
        self.assertEqual("transfer_complete", result["event"])
        self.assertTrue(result["track_detected"])
        self.assertTrue(result["cleanup_performed"])
        self.assertEqual(2, result["latest_season"])

        store = plugin._ensure_state_store()
        self.assertEqual({}, store.get_tv_tracks())
        runtime = store.get_runtime_state()
        self.assertTrue(runtime["last_diagnostic"]["success"])
        self.assertEqual("transfer_complete", runtime["last_diagnostic"]["event"])
        self.assertEqual("diagnostic_transfer_complete", store.get_action_log()[-1]["action"])

    def test_init_plugin_backfill_on_enable_runs_only_once_and_records_runtime_state(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()

        calls = []

        def fake_import_transfer_history(*, days, reason):
            calls.append((days, reason))
            return {"days": days, "reason": reason, "records": 1}

        plugin._import_transfer_history = fake_import_transfer_history

        plugin.init_plugin(
            {
                "enabled": True,
                "backfill_on_enable": True,
                "history_days": 30,
            }
        )

        runtime = plugin._ensure_state_store().get_runtime_state()
        self.assertEqual([(30, "bootstrap")], calls)
        self.assertTrue(runtime["bootstrap_history_imported"])
        self.assertEqual({"days": 30, "reason": "bootstrap", "records": 1}, runtime["bootstrap_history_summary"])

        plugin.init_plugin(
            {
                "enabled": True,
                "backfill_on_enable": True,
                "history_days": 30,
            }
        )

        self.assertEqual([(30, "bootstrap")], calls)

    def test_on_subscribe_complete_acknowledges_selected_tv_track(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "enable_tv": True,
                "tracked_tv_ids": "60625",
            }
        )

        event = plugin_module.Event(
            {
                "subscribe_info": {"season": 3},
                "mediainfo": {
                    "tmdb_id": 60625,
                    "type": plugin_module.MediaType.TV.value,
                    "title": "Rick and Morty",
                    "year": "2013",
                },
            }
        )

        plugin.on_subscribe_complete(event)

        track = plugin._ensure_state_store().get_tv_tracks()["60625"]
        self.assertEqual(3, track["latest_season"])
        self.assertEqual("subscribe_complete", track["source"])
        self.assertEqual("subscribe_complete", plugin._ensure_state_store().get_action_log()[-1]["action"])

    def test_on_transfer_complete_ignores_unselected_tv_track(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "enable_tv": True,
                "tracked_tv_ids": "60625",
            }
        )

        event = plugin_module.Event(
            {
                "meta": "S04",
                "transfer_history_id": 9001,
                "mediainfo": {
                    "tmdb_id": 77777,
                    "type": plugin_module.MediaType.TV.value,
                    "title": "Ignored Series",
                    "year": "2024",
                },
            }
        )

        plugin.on_transfer_complete(event)

        self.assertEqual({}, plugin._ensure_state_store().get_tv_tracks())
        self.assertEqual([], plugin._ensure_state_store().get_action_log())

    def test_api_rescan_returns_scan_summary_from_single_scan_cycle(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "notify": False,
                "enable_tv": True,
                "enable_movie": False,
                "tracked_tv_ids": "60625",
            }
        )
        store = plugin._ensure_state_store()
        store.acknowledge_tv_completion(
            tmdb_id=60625,
            title="Rick and Morty",
            year="2013",
            season=1,
            source="manual",
        )
        plugin._tmdb_chain = types.SimpleNamespace(
            tmdb_seasons=lambda tmdb_id: [
                {"season_number": 2, "episode_count": 10, "air_date": "2026-06-01"},
            ]
        )
        plugin._media_server_chain = types.SimpleNamespace(media_exists=lambda media: None)
        plugin._subscribe_exists = lambda **kwargs: False

        response = plugin.api_rescan({"scope": "tv", "notify": False})

        self.assertTrue(response["success"])
        summary = response["data"]
        self.assertTrue(summary["success"])
        self.assertEqual("tv", summary["scope"])
        self.assertEqual("api", summary["reason"])
        self.assertEqual(1, summary["scanned_tv"])
        self.assertEqual(1, summary["tracks_updated"])
        self.assertEqual([2], store.get_tv_tracks()["60625"]["pending_seasons"])

    def test_api_catalog_exposes_automation_health_endpoint(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin({"enabled": True, "enable_tv": True})

        api_paths = [item["path"] for item in plugin.get_api()]
        self.assertIn("/automation/health", api_paths)

    def test_api_automation_health_returns_scheduler_and_runtime_summary(self):
        plugin_module = load_plugin_module()
        plugin = plugin_module.NextReleaseTracker()
        plugin.init_plugin(
            {
                "enabled": True,
                "notify": True,
                "enable_tv": True,
                "enable_movie": False,
                "cron": "0 3 * * 1",
            }
        )
        store = plugin._ensure_state_store()
        store.update_runtime(
            {
                "last_scan_started_at": "2026-06-01 03:00:00",
                "last_scan_finished_at": "2026-06-01 03:01:00",
                "last_scan_scope": "tv",
                "last_scan_reason": "cron_tick",
                "last_scan_summary": {
                    "success": True,
                    "errors": 0,
                    "planned_tracks": 1,
                    "remaining_tracks": 6,
                    "budget_exhausted": False,
                },
                "current_cycle_stats": {
                    "cycle_started_at": "2026-06-01 03:00:00",
                    "tick_count": 7,
                    "idle_tick_count": 5,
                    "active_tick_count": 2,
                    "processed_task_count": 2,
                    "locked_skip_count": 1,
                    "last_active_tick_at": "2026-06-01 05:00:00",
                    "last_locked_skip_at": "2026-06-01 05:01:00",
                },
                "scan_plan": {
                    "scope": "tv",
                    "cycle_started_at": "2026-06-01 03:00:00",
                    "period_minutes": 10080,
                    "task_ids": ["tv:100", "tv:101"],
                    "pending_task_ids": ["tv:101"],
                },
                "tmdb_rate_limit": {
                    "limit": 5,
                    "used": 2,
                    "window_started_at": "2026-06-01 05:00:00",
                },
            }
        )

        response = plugin.api_automation_health()

        self.assertTrue(response["success"])
        data = response["data"]
        self.assertEqual("* * * * *", data["scheduler"]["minute_tick_cron"])
        self.assertEqual("0 3 * * 1", data["scheduler"]["configured_cycle_cron"])
        self.assertTrue(data["scheduler"]["plugin_enabled"])
        self.assertEqual("2026-06-01 03:00:00", data["runtime"]["last_scan_started_at"])
        self.assertEqual("2026-06-01 03:01:00", data["runtime"]["last_scan_finished_at"])
        self.assertEqual(7, data["cycle"]["tick_count"])
        self.assertEqual(5, data["cycle"]["idle_tick_count"])
        self.assertEqual(1, data["plan"]["pending_task_count"])
        self.assertEqual("tv:101", data["plan"]["next_due_task_id"])
        self.assertEqual(5, data["tmdb_rate_limit"]["limit"])
        self.assertEqual(2, data["tmdb_rate_limit"]["used"])


if __name__ == "__main__":
    unittest.main()
