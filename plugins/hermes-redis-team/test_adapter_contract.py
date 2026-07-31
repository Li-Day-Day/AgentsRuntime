import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


def install_gateway_stubs():
    gateway = types.ModuleType("gateway")
    config = types.ModuleType("gateway.config")
    platforms = types.ModuleType("gateway.platforms")
    base = types.ModuleType("gateway.platforms.base")
    session = types.ModuleType("gateway.session")

    class Platform(str):
        pass

    class PlatformConfig:
        extra = {}

    class BasePlatformAdapter:
        def __init__(self, config=None, platform=None):
            self.config = config
            self.platform = platform
            self.is_connected = False

        def _mark_connected(self):
            self.is_connected = True

        def _mark_disconnected(self):
            self.is_connected = False

        def _set_fatal_error(self, *_args, **_kwargs):
            pass

    class MessageEvent:
        pass

    class MessageType:
        TEXT = "text"

    class ProcessingOutcome:
        SUCCESS = "success"
        CANCELLED = "cancelled"

    class SendResult:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class SessionSource:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    config.Platform = Platform
    config.PlatformConfig = PlatformConfig
    base.BasePlatformAdapter = BasePlatformAdapter
    base.MessageEvent = MessageEvent
    base.MessageType = MessageType
    base.ProcessingOutcome = ProcessingOutcome
    base.SendResult = SendResult
    session.SessionSource = SessionSource
    sys.modules.update(
        {
            "gateway": gateway,
            "gateway.config": config,
            "gateway.platforms": platforms,
            "gateway.platforms.base": base,
            "gateway.session": session,
        }
    )


install_gateway_stubs()
spec = importlib.util.spec_from_file_location("hermes_redis_team_adapter", Path(__file__).with_name("adapter.py"))
adapter = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = adapter
spec.loader.exec_module(adapter)


class HermesRedisTeamContractTests(unittest.TestCase):
    def settings(self, root):
        return adapter.RedisTeamSettings(
            enabled=True,
            redis_url="redis://example.invalid:6379/0",
            team_id="42",
            member_id="developer",
            role="developer",
            shared_dir=str(root),
            preview_origin="http://clawmanager-egress-proxy.system.svc.cluster.local:3128",
            team_token="test-token",
        )

    def test_protocol_matches_current_worker_contract(self):
        self.assertEqual(adapter.PROTOCOL_VERSION, 4)
        self.assertIn("completion_ack_v1", adapter.PROTOCOL_CAPABILITIES)
        self.assertIn("automatic_turn_completion_v2", adapter.PROTOCOL_CAPABILITIES)
        self.assertIn("team_artifact_preview_v1", adapter.PROTOCOL_CAPABILITIES)

    def test_managed_startup_identity_loads_from_environment(self):
        with mock.patch.dict(
            os.environ,
            {
                "CLAWMANAGER_TEAM_ENABLED": "true",
                "CLAWMANAGER_TEAM_REDIS_URL": "redis://example.invalid:6379/0",
                "CLAWMANAGER_TEAM_ID": "119",
                "CLAWMANAGER_TEAM_MEMBER_ID": "developer",
                "CLAWMANAGER_INSTANCE_ID": "397",
                "CLAWMANAGER_GATEWAY_GENERATION": "12",
            },
            clear=True,
        ):
            settings = adapter.load_settings(None)
        self.assertEqual(settings.instance_id, 397)
        self.assertEqual(settings.generation, 12)

    def test_existing_cooperative_directories_are_never_chmoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "team"
            directories = [
                root,
                *(root / child for child in ("inbox", "status", "tasks", "results", "artifacts", "tmp", ".hermes-redis-team")),
            ]
            for directory in directories:
                directory.mkdir(parents=True, exist_ok=True)
            settings = self.settings(root)
            with mock.patch.object(Path, "chmod", side_effect=AssertionError("existing shared directory chmod attempted")):
                adapter.ensure_team_dirs(settings)

    def test_atomic_write_does_not_chmod_existing_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "team" / "status"
            parent.mkdir(parents=True)
            target = parent / "developer.json"
            original_chmod = Path.chmod

            def reject_parent_chmod(path, mode, *args, **kwargs):
                if path == parent:
                    raise AssertionError("existing shared parent chmod attempted")
                return original_chmod(path, mode, *args, **kwargs)

            with mock.patch.object(Path, "chmod", autospec=True, side_effect=reject_parent_chmod):
                adapter._atomic_write_json(target, {"ok": True})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"ok": True})

    def test_concurrent_members_can_create_the_same_shared_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "team" / "artifacts" / "team-42-task-7" / "members"
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(adapter._ensure_shared_directory, target) for _ in range(24)]
                for future in futures:
                    future.result()
            self.assertTrue(target.is_dir())

    def test_shared_directory_rejects_files_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_path = root / "not-a-directory"
            file_path.write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(PermissionError, "not a directory"):
                adapter._ensure_shared_directory(file_path)

            link_path = root / "linked-directory"
            target = root / "target"
            target.mkdir()
            try:
                link_path.symlink_to(target, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            with self.assertRaisesRegex(PermissionError, "symbolic links"):
                adapter._ensure_shared_directory(link_path)

    def test_existing_shared_directory_must_be_cooperatively_accessible(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "team"
            target.mkdir()
            with (
                mock.patch.object(adapter, "_effective_access", return_value=False),
                self.assertRaisesRegex(PermissionError, "lacks read/write/execute access"),
            ):
                adapter._ensure_shared_directory(target)

    def test_unusable_shared_workspace_publishes_non_retryable_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared_file = root / "team"
            shared_file.write_text("not a directory", encoding="utf-8")
            ready_file = root / "private" / "redis-team.ready.json"
            settings = adapter.RedisTeamSettings(
                enabled=True,
                redis_url="redis://example.invalid:6379/0",
                team_id="118",
                member_id="developer",
                role="developer",
                shared_dir=str(shared_file),
                ready_file=str(ready_file),
                instance_id=397,
                generation=11,
            )

            async def run_test():
                with mock.patch.object(adapter, "load_settings", return_value=settings):
                    instance = adapter.RedisTeamAdapter(types.SimpleNamespace(extra={}))
                    self.assertFalse(await instance.connect())
                    await instance.disconnect()

            asyncio.run(run_test())
            failure_file = adapter._startup_failure_path(ready_file)
            self.assertTrue(failure_file.is_file())
            failure = json.loads(failure_file.read_text(encoding="utf-8"))
            self.assertEqual(failure["state"], "failed")
            self.assertEqual(failure["instanceId"], 397)
            self.assertEqual(failure["generation"], 11)
            self.assertEqual(failure["error"]["code"], "shared_workspace_unusable")
            self.assertFalse(failure["error"]["retryable"])
            self.assertFalse(ready_file.exists())

    def test_consumer_readiness_requires_group_and_initial_presence(self):
        with tempfile.TemporaryDirectory() as tmp:
            ready_file = Path(tmp) / "private" / "redis-team.ready.json"
            settings = adapter.RedisTeamSettings(
                enabled=True,
                redis_url="redis://example.invalid:6379/0",
                team_id="117",
                member_id="developer",
                role="developer",
                shared_dir=str(Path(tmp) / "team"),
                ready_file=str(ready_file),
                instance_id=394,
                generation=9,
            )
            commands = []

            class FakeRedis:
                def __init__(self, _redis_url):
                    pass

                async def connect(self):
                    pass

                async def command(self, *args):
                    commands.append(args)
                    return "OK"

                def close(self):
                    pass

            async def run_test():
                with (
                    mock.patch.object(adapter, "load_settings", return_value=settings),
                    mock.patch.object(adapter, "AsyncRedisClient", FakeRedis),
                ):
                    failure_file = adapter._startup_failure_path(ready_file)
                    failure_file.parent.mkdir(parents=True, exist_ok=True)
                    failure_file.write_text('{"state":"failed"}\n', encoding="utf-8")
                    instance = adapter.RedisTeamAdapter(types.SimpleNamespace(extra={}))
                    self.assertTrue(await instance.connect())
                    self.assertTrue(ready_file.is_file())
                    self.assertFalse(failure_file.exists())
                    ready = json.loads(ready_file.read_text(encoding="utf-8"))
                    self.assertTrue(ready["ready"])
                    self.assertEqual(ready["state"], "ready")
                    self.assertEqual(ready["teamId"], "117")
                    self.assertEqual(ready["memberId"], "developer")
                    self.assertEqual(ready["instanceId"], 394)
                    self.assertEqual(ready["generation"], 9)
                    xgroup_index = next(index for index, command in enumerate(commands) if command[:2] == ("XGROUP", "CREATE"))
                    presence_index = next(index for index, command in enumerate(commands) if command[0] == "HSET")
                    self.assertLess(xgroup_index, presence_index)
                    await instance.disconnect()
                    self.assertFalse(ready_file.exists())

            asyncio.run(run_test())

    def test_control_plane_redis_keys_preserve_completion_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(Path(tmp))
            completion_id = "completion:42:team-42-task-7:developer:dev-1:r3"
            attempt_id = "attempt_123"
            self.assertEqual(
                adapter._completion_ack_key(settings, completion_id, attempt_id),
                "claw:team:42:completion-ack:"
                "completion:42:team-42-task-7:developer:dev-1:r3:attempt_123",
            )
            self.assertEqual(
                adapter.assignment_activity_key(
                    settings,
                    "team-42-task-7",
                    "dev-1",
                ),
                "claw:team:42:assignment-activity:team-42-task-7:dev-1",
            )

    def test_member_artifacts_require_active_assignment_and_cannot_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(Path(tmp))
            adapter.ensure_team_dirs(settings)
            with self.assertRaisesRegex(ValueError, "rootTaskId"):
                adapter._artifact_path(settings, {"path": "result.md"}, default_scope="member", write=True)

            adapter._persist_active_envelope(
                settings,
                {
                    "rootTaskId": "team-42-task-7",
                    "assignmentId": "dev-1",
                    "taskId": "team-42-task-7",
                },
            )
            target = adapter._artifact_path(
                settings,
                {"path": "result.md"},
                default_scope="member",
                write=True,
            )
            self.assertEqual(
                adapter.canonical_artifact_ref(settings, target),
                "/team/artifacts/team-42-task-7/members/developer/dev-1/result.md",
            )
            with self.assertRaisesRegex(ValueError, "traversal"):
                adapter._artifact_path(
                    settings,
                    {"path": "../other-team.txt"},
                    default_scope="member",
                    write=True,
                )

    def test_preview_uses_same_persistent_managed_url_contract_as_openclaw(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(Path(tmp))
            target = Path(tmp) / "artifacts" / "team-42-task-7" / "index.html"
            target.parent.mkdir(parents=True)
            target.write_text("<h1>ok</h1>", encoding="utf-8")
            url = adapter._preview_url(settings, target)
            self.assertTrue(url.startswith(settings.preview_origin + "/v1/42/"))
            self.assertNotIn("expires", url)
            self.assertNotIn("token", url)

    def test_worker_result_never_overwrites_team_final_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(Path(tmp))
            payload = adapter.write_task_result(
                settings,
                "team-42-task-7",
                envelope={
                    "rootTaskId": "team-42-task-7",
                    "assignmentId": "dev-1",
                },
                status="succeeded",
                summary="implementation complete",
                result_markdown="The implementation and static checks are complete.",
            )
            self.assertEqual(payload["artifactRefs"], [])
            self.assertFalse(
                (Path(tmp) / "results" / "team-42-task-7" / "result.md").exists()
            )
            completion_files = list(
                (Path(tmp) / ".hermes-redis-team" / "completions").glob("*.json")
            )
            self.assertEqual(len(completion_files), 1)

    def test_failed_worker_result_gets_assignment_scoped_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(Path(tmp))
            payload = adapter.write_task_result(
                settings,
                "team-42-task-7",
                envelope={
                    "rootTaskId": "team-42-task-7",
                    "assignmentId": "dev-1",
                },
                status="failed",
                summary="implementation failed",
                result_markdown="The implementation failed because the required input was unavailable.",
            )
            self.assertEqual(
                payload["artifactRefs"],
                [
                    "/team/artifacts/team-42-task-7/members/"
                    "developer/dev-1/failure-result.md"
                ],
            )
            self.assertFalse(
                (Path(tmp) / "results" / "team-42-task-7" / "result.md").exists()
            )

    def test_completion_status_is_bounded_before_redis_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(Path(tmp))
            with self.assertRaisesRegex(ValueError, "completion status"):
                asyncio.run(
                    adapter._propose_completion(
                        settings,
                        {
                            "taskId": "team-42-task-7",
                            "rootTaskId": "team-42-task-7",
                            "assignmentId": "dev-1",
                        },
                        status="finished",
                        summary="invalid status",
                        result_markdown="invalid status must not publish",
                        explicit=True,
                    )
                )

    def test_automatic_result_uses_the_strict_v4_completion_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(Path(tmp))
            captured = {}

            class FakeRedis:
                def __init__(self, _redis_url):
                    pass

                async def connect(self):
                    pass

                def close(self):
                    pass

            async def fake_publish(_redis, _settings, _key, event):
                captured.update(event)
                return {"published": True, "streamId": "1-0"}

            async def fake_ack(_redis, _settings, _completion_id, _attempt_id):
                return {"decision": "accepted", "reason": "accepted"}

            envelope = {
                "messageId": "msg-1",
                "taskId": "team-42-task-7",
                "rootTaskId": "team-42-task-7",
                "assignmentId": "dev-1",
            }
            adapter._persist_active_envelope(settings, envelope)
            with (
                mock.patch.object(adapter, "AsyncRedisClient", FakeRedis),
                mock.patch.object(adapter, "_publish_once", fake_publish),
                mock.patch.object(adapter, "_completion_ack", fake_ack),
            ):
                asyncio.run(
                    adapter._propose_completion(
                        settings,
                        envelope,
                        status="succeeded",
                        summary="implementation complete",
                        result_markdown="The requested implementation is complete.",
                        explicit=False,
                        automatic_turn_result=True,
                    )
                )

            event = captured
            self.assertEqual(event["completionSource"], "team_complete_task")
            self.assertTrue(event["explicitCompletion"])
            self.assertTrue(event["automaticTurnResult"])
            self.assertTrue(event["assignmentResultOnly"])
            self.assertFalse(event["rootTaskTerminal"])

    def test_normalized_envelope_preserves_assignment_contract(self):
        value = adapter.normalize_envelope(
            {
                "v": 4,
                "messageId": "msg-1",
                "taskId": "team-42-task-7",
                "rootTaskId": "team-42-task-7",
                "rootMessageId": "root-msg",
                "assignmentId": "dev-1",
                "workId": "dev-1",
                "revision": 3,
                "requiresCompletion": False,
            }
        )
        self.assertEqual(value["assignmentId"], "dev-1")
        self.assertEqual(value["rootMessageId"], "root-msg")
        self.assertEqual(value["revision"], 3)
        self.assertFalse(value["requiresCompletion"])

    def test_normalized_envelope_rejects_unstable_transport_identity(self):
        self.assertIsNone(adapter.normalize_envelope({"taskId": "team-42-task-7"}))
        self.assertIsNone(adapter.normalize_envelope({"messageId": "msg-1"}))
        self.assertIsNone(adapter.normalize_envelope({"rawPayload": "not-json", "redisId": "1-0"}))

    def test_stream_parser_treats_real_redis_empty_pending_shape_as_empty(self):
        self.assertEqual(adapter._parse_stream_response(None), [])
        self.assertEqual(adapter._parse_stream_response([]), [])
        self.assertEqual(adapter._parse_stream_response([["team-inbox", []]]), [])

    def test_consumer_switches_from_empty_pending_to_new_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(Path(tmp))
            reads = []
            handled = []

            class FakeRedis:
                async def command(self, *args):
                    reads.append(args)
                    read_id = args[-1]
                    if read_id == "0":
                        return [[adapter.inbox_key(settings), []]]
                    return [
                        [
                            adapter.inbox_key(settings),
                            [["2-0", ["payload", json.dumps({"messageId": "msg-2", "taskId": "task-2"})]]],
                        ]
                    ]

                def close(self):
                    pass

            async def run_test():
                with mock.patch.object(adapter, "load_settings", return_value=settings):
                    instance = adapter.RedisTeamAdapter(types.SimpleNamespace(extra={}))
                instance._consumer_redis = FakeRedis()
                instance.is_connected = True

                async def handle(raw):
                    handled.append(raw["messageId"])
                    instance.is_connected = False

                instance._handle_redis_message = handle
                await instance._consumer_loop()

            asyncio.run(run_test())
            self.assertEqual(handled, ["msg-2"])
            self.assertEqual(reads[0][-1], "0")
            self.assertNotIn("BLOCK", reads[0])
            self.assertEqual(reads[1][-1], ">")
            self.assertIn("BLOCK", reads[1])

    def test_consumer_recovers_pending_before_new_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(Path(tmp))
            pending_reads = 0
            handled = []

            class FakeRedis:
                async def command(self, *args):
                    nonlocal pending_reads
                    read_id = args[-1]
                    if read_id == "0":
                        pending_reads += 1
                        if pending_reads == 1:
                            return [
                                [
                                    adapter.inbox_key(settings),
                                    [["1-0", ["payload", json.dumps({"messageId": "pending", "taskId": "task-1"})]]],
                                ]
                            ]
                        return [[adapter.inbox_key(settings), []]]
                    return [
                        [
                            adapter.inbox_key(settings),
                            [["2-0", ["payload", json.dumps({"messageId": "new", "taskId": "task-2"})]]],
                        ]
                    ]

                def close(self):
                    pass

            async def run_test():
                with mock.patch.object(adapter, "load_settings", return_value=settings):
                    instance = adapter.RedisTeamAdapter(types.SimpleNamespace(extra={}))
                instance._consumer_redis = FakeRedis()
                instance.is_connected = True

                async def handle(raw):
                    handled.append(raw["messageId"])
                    if len(handled) == 2:
                        instance.is_connected = False

                instance._handle_redis_message = handle
                await instance._consumer_loop()

            asyncio.run(run_test())
            self.assertEqual(handled, ["pending", "new"])

    def test_redis_reconnect_swaps_both_clients_and_restores_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(Path(tmp))

            class FakeRedis:
                def __init__(self):
                    self.closed = False

                def close(self):
                    self.closed = True

            async def run_test():
                with mock.patch.object(adapter, "load_settings", return_value=settings):
                    instance = adapter.RedisTeamAdapter(types.SimpleNamespace(extra={}))
                old_presence = FakeRedis()
                old_consumer = FakeRedis()
                new_presence = FakeRedis()
                new_consumer = FakeRedis()
                instance._redis = old_presence
                instance._consumer_redis = old_consumer
                instance.is_connected = True
                adapter.write_local_status(
                    settings,
                    {"availability": "running", "runtimeStatus": "running"},
                )
                status = adapter.read_team_statuses(settings, settings.member_id)
                with (
                    mock.patch.object(
                        instance,
                        "_open_redis_clients",
                        new=mock.AsyncMock(return_value=(new_presence, new_consumer, status)),
                    ),
                    mock.patch.object(adapter, "_publish_ready_file") as publish_ready,
                ):
                    self.assertTrue(await instance._reconnect_redis_clients(old_consumer))
                    publish_ready.assert_called_once()
                self.assertIs(instance._redis, new_presence)
                self.assertIs(instance._consumer_redis, new_consumer)
                self.assertTrue(old_presence.closed)
                self.assertTrue(old_consumer.closed)

            asyncio.run(run_test())

    def test_completing_one_message_keeps_parallel_assignment_turn_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(Path(tmp))

            async def run_test():
                with mock.patch.object(adapter, "load_settings", return_value=settings):
                    instance = adapter.RedisTeamAdapter(types.SimpleNamespace(extra={}))
                identity = ("team-42-task-7", "dev-1")
                instance._accepted_messages = {"msg-1": identity, "msg-2": identity}
                event = types.SimpleNamespace(
                    raw_message={
                        "messageId": "msg-1",
                        "taskId": identity[0],
                        "rootTaskId": identity[0],
                        "assignmentId": identity[1],
                    },
                    message_id="msg-1",
                    source=types.SimpleNamespace(chat_id=identity[0]),
                )
                with mock.patch.object(instance, "_on_processing_complete_inner", new=mock.AsyncMock()):
                    await instance.on_processing_complete(event, adapter.ProcessingOutcome.SUCCESS)
                self.assertNotIn("msg-1", instance._accepted_messages)
                self.assertEqual(instance._accepted_messages["msg-2"], identity)

            asyncio.run(run_test())

    def test_redis_finalize_retry_never_dispatches_the_same_turn_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(Path(tmp))
            dispatches = []

            class FakeRedis:
                def __init__(self):
                    self.fail_processed_set = True

                async def command(self, *args):
                    if args[0] == "GET":
                        return None
                    if args[0] == "SET" and self.fail_processed_set:
                        self.fail_processed_set = False
                        raise ConnectionError("connection dropped before processed marker")
                    if args[0] == "XADD":
                        return "1-0"
                    return "OK"

                def close(self):
                    pass

            envelope = {
                "messageId": "msg-transport-retry",
                "taskId": "team-42-task-7",
                "rootTaskId": "team-42-task-7",
                "assignmentId": "dev-1",
                "teamId": "42",
                "from": "leader",
                "to": "developer",
                "text": "Implement the requested artifact.",
                "redisId": "7-0",
            }

            async def run_test():
                with mock.patch.object(adapter, "load_settings", return_value=settings):
                    instance = adapter.RedisTeamAdapter(types.SimpleNamespace(extra={}))
                instance._redis = FakeRedis()

                async def dispatch(value):
                    dispatches.append(value["messageId"])

                instance._dispatch_envelope = dispatch
                with self.assertRaises(ConnectionError):
                    await instance._handle_redis_message(envelope)
                self.assertIn(envelope["messageId"], instance._transport_accepted_messages)
                await instance._handle_redis_message(envelope)
                self.assertNotIn(envelope["messageId"], instance._transport_accepted_messages)

            asyncio.run(run_test())
            self.assertEqual(dispatches, ["msg-transport-retry"])

    def test_invalid_inbound_message_is_dlqed_and_acked(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(Path(tmp))
            commands = []

            class FakeRedis:
                async def command(self, *args):
                    commands.append(args)
                    return "OK"

                def close(self):
                    pass

            async def run_test():
                with mock.patch.object(adapter, "load_settings", return_value=settings):
                    instance = adapter.RedisTeamAdapter(types.SimpleNamespace(extra={}))
                instance._redis = FakeRedis()
                await instance._handle_redis_message({"redisId": "9-0", "rawPayload": "not-json"})

            asyncio.run(run_test())
            self.assertTrue(any(command[0] == "XADD" and command[1] == adapter.dlq_key(settings) for command in commands))
            self.assertTrue(any(command[0] == "XACK" and command[-1] == "9-0" for command in commands))

    def test_backlogged_monitor_observes_active_assignment_without_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self.settings(Path(tmp))
            commands = []
            dispatched = []
            formal = {
                "messageId": "assignment-1",
                "taskId": "team-42-task-7",
                "rootTaskId": "team-42-task-7",
                "assignmentId": "dev-1",
                "workId": "dev-1",
                "teamId": "42",
                "from": "leader",
                "to": "developer",
                "text": "Implement the requested artifact.",
            }
            monitor = {
                "messageId": "monitor-1",
                "taskId": "team-42-task-7",
                "rootTaskId": "team-42-task-7",
                "assignmentId": "dev-1",
                "workId": "dev-1",
                "teamId": "42",
                "from": "clawmanager-monitor",
                "to": "developer",
                "intent": "assignment_status_check",
                "requiresCompletion": False,
                "metadata": {"monitorType": "assignment_status_check", "checkId": "monitor-1"},
            }

            class FakeRedis:
                async def command(self, *args):
                    commands.append(args)
                    if args[0] == "GET":
                        return None
                    return "OK"

                def close(self):
                    pass

            async def run_test():
                with mock.patch.object(adapter, "load_settings", return_value=settings):
                    instance = adapter.RedisTeamAdapter(types.SimpleNamespace(extra={}))
                instance._redis = FakeRedis()

                async def dispatch(envelope):
                    dispatched.append(envelope["messageId"])
                    instance._accepted_messages[envelope["messageId"]] = adapter._assignment_identity(envelope)
                    adapter.write_local_status(
                        settings,
                        {
                            "availability": "running",
                            "runtimeStatus": "running",
                            "currentTaskId": envelope["taskId"],
                            "currentAssignmentId": envelope["assignmentId"],
                        },
                    )

                instance._dispatch_envelope = dispatch
                await instance._handle_redis_message({**formal, "redisId": "1-0"})
                active_before = adapter._load_active_envelope(settings)
                await instance._handle_redis_message({**monitor, "redisId": "2-0"})
                active_after = adapter._load_active_envelope(settings)

                self.assertEqual(active_before["messageId"], "assignment-1")
                self.assertEqual(active_after["messageId"], "assignment-1")

            asyncio.run(run_test())
            self.assertEqual(dispatched, ["assignment-1"])
            monitor_events = []
            for command in commands:
                if command[0] == "XADD" and command[1] == adapter.events_key(settings):
                    payload = json.loads(command[-1])
                    if payload.get("eventKind") == "assignment_check_result":
                        monitor_events.append(payload)
            self.assertEqual(len(monitor_events), 1)
            self.assertFalse(monitor_events[0]["visibleToChat"])
            self.assertEqual(monitor_events[0]["stateEffect"], "none")

    def test_generic_processing_text_is_not_a_final_result(self):
        self.assertFalse(adapter._substantive_final_text("Redis Team task processing completed"))
        self.assertFalse(adapter._substantive_final_text("需要你确认下一步吗？"))
        self.assertTrue(adapter._substantive_final_text("实现和静态验证均已完成，产物已写入当前成员目录。"))


if __name__ == "__main__":
    unittest.main()
