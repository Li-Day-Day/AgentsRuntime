import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
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
                    instance = adapter.RedisTeamAdapter(types.SimpleNamespace(extra={}))
                    self.assertTrue(await instance.connect())
                    self.assertTrue(ready_file.is_file())
                    ready = json.loads(ready_file.read_text(encoding="utf-8"))
                    self.assertTrue(ready["ready"])
                    self.assertEqual(ready["teamId"], "117")
                    self.assertEqual(ready["memberId"], "developer")
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

    def test_generic_processing_text_is_not_a_final_result(self):
        self.assertFalse(adapter._substantive_final_text("Redis Team task processing completed"))
        self.assertFalse(adapter._substantive_final_text("需要你确认下一步吗？"))
        self.assertTrue(adapter._substantive_final_text("实现和静态验证均已完成，产物已写入当前成员目录。"))


if __name__ == "__main__":
    unittest.main()
