import asyncio
import json
import os
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

from test_adapter_contract import adapter


REDIS_URL = os.getenv("HERMES_REDIS_TEST_URL", "").strip()


@unittest.skipUnless(REDIS_URL, "set HERMES_REDIS_TEST_URL to run real Redis integration tests")
class HermesRedisTeamRedisIntegrationTests(unittest.TestCase):
    def settings(self, root: Path, team_id: str) -> adapter.RedisTeamSettings:
        return adapter.RedisTeamSettings(
            enabled=True,
            redis_url=REDIS_URL,
            team_id=team_id,
            member_id="developer",
            role="developer",
            shared_dir=str(root),
        )

    async def cleanup(self, redis, settings):
        keys = await redis.command("KEYS", f"claw:team:{settings.team_id}:*")
        if keys:
            await redis.command("DEL", *keys)

    async def wait_pending_zero(self, redis, settings):
        deadline = asyncio.get_running_loop().time() + 5
        while True:
            pending = await redis.command(
                "XPENDING",
                adapter.inbox_key(settings),
                settings.consumer_group,
            )
            if pending[0] == 0:
                return
            if asyncio.get_running_loop().time() >= deadline:
                self.fail(f"consumer did not acknowledge the message: {pending}")
            await asyncio.sleep(0.05)

    def test_new_message_is_delivered_after_empty_pending_probe(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmp:
                settings = self.settings(Path(tmp), f"integration-{uuid.uuid4().hex}")
                control = adapter.AsyncRedisClient(REDIS_URL)
                await control.connect()
                await self.cleanup(control, settings)
                received = asyncio.Event()
                instance = None
                try:
                    with mock.patch.object(adapter, "load_settings", return_value=settings):
                        instance = adapter.RedisTeamAdapter(types.SimpleNamespace(extra={}))

                    async def dispatch(envelope, **_kwargs):
                        instance._accepted_messages[envelope["messageId"]] = adapter._assignment_identity(envelope)
                        received.set()

                    instance._dispatch_envelope = dispatch
                    self.assertTrue(await instance.connect())
                    await adapter.xadd_json(
                        control,
                        adapter.inbox_key(settings),
                        {
                            "messageId": "msg-new",
                            "taskId": "team-task-1",
                            "rootTaskId": "team-task-1",
                            "assignmentId": "dev-1",
                            "teamId": settings.team_id,
                            "from": "leader",
                            "to": "developer",
                            "text": "Implement the requested artifact.",
                        },
                    )
                    await asyncio.wait_for(received.wait(), timeout=5)
                    await self.wait_pending_zero(control, settings)
                finally:
                    if instance is not None:
                        await instance.disconnect()
                    await self.cleanup(control, settings)
                    control.close()

        asyncio.run(run_test())

    def test_owned_pending_message_is_recovered_after_restart(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmp:
                settings = self.settings(Path(tmp), f"integration-{uuid.uuid4().hex}")
                control = adapter.AsyncRedisClient(REDIS_URL)
                await control.connect()
                await self.cleanup(control, settings)
                received = asyncio.Event()
                instance = None
                try:
                    try:
                        await control.command(
                            "XGROUP",
                            "CREATE",
                            adapter.inbox_key(settings),
                            settings.consumer_group,
                            "0",
                            "MKSTREAM",
                        )
                    except adapter.RespError as exc:
                        if "BUSYGROUP" not in str(exc):
                            raise
                    await adapter.xadd_json(
                        control,
                        adapter.inbox_key(settings),
                        {
                            "messageId": "msg-pending",
                            "taskId": "team-task-2",
                            "rootTaskId": "team-task-2",
                            "assignmentId": "dev-2",
                            "teamId": settings.team_id,
                            "from": "leader",
                            "to": "developer",
                            "text": "Recover this assignment.",
                        },
                    )
                    claimed = await control.command(
                        "XREADGROUP",
                        "GROUP",
                        settings.consumer_group,
                        settings.member_id,
                        "COUNT",
                        1,
                        "STREAMS",
                        adapter.inbox_key(settings),
                        ">",
                    )
                    self.assertEqual(len(adapter._parse_stream_response(claimed)), 1)

                    with mock.patch.object(adapter, "load_settings", return_value=settings):
                        instance = adapter.RedisTeamAdapter(types.SimpleNamespace(extra={}))

                    async def dispatch(envelope, **_kwargs):
                        instance._accepted_messages[envelope["messageId"]] = adapter._assignment_identity(envelope)
                        received.set()

                    instance._dispatch_envelope = dispatch
                    self.assertTrue(await instance.connect())
                    await asyncio.wait_for(received.wait(), timeout=5)
                    await self.wait_pending_zero(control, settings)
                finally:
                    if instance is not None:
                        await instance.disconnect()
                    await self.cleanup(control, settings)
                    control.close()

        asyncio.run(run_test())

    def test_formal_assignment_publishes_lifecycle_and_busy_presence(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as tmp:
                settings = self.settings(Path(tmp), f"integration-{uuid.uuid4().hex}")
                control = adapter.AsyncRedisClient(REDIS_URL)
                await control.connect()
                await self.cleanup(control, settings)
                started = asyncio.Event()
                instance = None
                try:
                    with mock.patch.object(adapter, "load_settings", return_value=settings):
                        instance = adapter.RedisTeamAdapter(types.SimpleNamespace(extra={}))

                    async def handle_message(_event):
                        started.set()

                    instance.handle_message = handle_message
                    self.assertTrue(await instance.connect())
                    await adapter.xadd_json(
                        control,
                        adapter.inbox_key(settings),
                        {
                            "messageId": "msg-lifecycle",
                            "taskId": "team-task-lifecycle",
                            "rootTaskId": "team-task-lifecycle",
                            "rootMessageId": "root-lifecycle",
                            "assignmentId": "dev-lifecycle",
                            "workId": "dev-lifecycle",
                            "teamId": settings.team_id,
                            "from": "leader",
                            "to": "developer",
                            "text": "Run the lifecycle integration test.",
                            "requiresCompletion": True,
                        },
                    )
                    await asyncio.wait_for(started.wait(), timeout=5)
                    await self.wait_pending_zero(control, settings)
                    rows = await control.command("XRANGE", adapter.events_key(settings), "-", "+")
                    events = []
                    for row in rows:
                        fields = row[1]
                        for index in range(0, len(fields), 2):
                            if fields[index] == "payload":
                                events.append(json.loads(fields[index + 1]))
                    lifecycle = [
                        event for event in events if event.get("event") in {"task_received", "task_started"}
                    ]
                    self.assertEqual(
                        [event["event"] for event in lifecycle],
                        ["task_received", "task_started"],
                    )
                    self.assertEqual(len({event["eventId"] for event in lifecycle}), 2)
                    status = adapter.read_team_statuses(settings, settings.member_id)
                    self.assertEqual(status["availability"], "busy")
                    self.assertEqual(status["runtimeStatus"], "running")
                    self.assertEqual(status["currentAssignmentId"], "dev-lifecycle")
                finally:
                    if instance is not None:
                        await instance.disconnect()
                    await self.cleanup(control, settings)
                    control.close()

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
