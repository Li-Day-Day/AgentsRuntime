import asyncio
import inspect
import unittest

from selkies import input_handler


class RecordingLogger:
    def __init__(self):
        self.messages = []

    def _record(self, message, *args, **kwargs):
        if args:
            message = message % args
        self.messages.append(str(message))

    debug = _record
    info = _record
    warning = _record
    error = _record


class ClipboardHarness:
    KEY_INSERT = 0xFF63

    def __init__(self, data, mime_type):
        self.data = data
        self.mime_type = mime_type
        self.pending_visible = None
        self.pending_reads = 0
        self.delayed_values = {}
        self.never_visible = set()
        self.fail_values = set()
        self.pasted = []
        self.key_events = []
        self.writes = []
        self.clear_count = 0
        self.write_started = asyncio.Event()

    async def read(self, use_binary=False):
        if self.pending_visible is not None:
            pending_data, pending_mime = self.pending_visible
            if pending_data not in self.never_visible:
                self.pending_reads -= 1
                if self.pending_reads <= 0:
                    self.data = pending_data
                    self.mime_type = pending_mime
                    self.pending_visible = None
        return self.data, self.mime_type

    async def write(self, data, mime_type="text/plain"):
        self.write_started.set()
        self.writes.append((data, mime_type))
        if data in self.fail_values:
            return False
        delay = self.delayed_values.get(data, 0)
        if delay or data in self.never_visible:
            self.pending_visible = (data, mime_type)
            self.pending_reads = delay
        else:
            self.data = data
            self.mime_type = mime_type
            self.pending_visible = None
        return True

    async def clear(self):
        self.clear_count += 1
        self.data = None
        self.mime_type = None
        self.pending_visible = None
        return True

    async def send_key(self, keysym, down=True):
        self.key_events.append((keysym, down))
        if keysym == self.KEY_INSERT and down:
            self.pasted.append(self.data)


class UnicodeClipboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_logger = input_handler.logger_webrtc_input
        self.logger = RecordingLogger()
        input_handler.logger_webrtc_input = self.logger

    async def asyncTearDown(self):
        input_handler.logger_webrtc_input = self.original_logger

    def make_input(self, data="original", mime_type="text/plain", modifiers=None):
        target = input_handler.WebRTCInput.__new__(input_handler.WebRTCInput)
        target.loop = asyncio.get_running_loop()
        target.is_wayland = True
        target.active_modifiers = set(modifiers or ())
        target.clipboard_paused = False
        target.clipboard_injection_lock = asyncio.Lock()
        target.unicode_clipboard_restore_task = None
        target.unicode_clipboard_original = None
        target.unicode_clipboard_original_captured = False
        target.unicode_clipboard_last_injected = None
        target.pending_inbound_clipboard = None
        target.UNICODE_CLIPBOARD_CONFIRM_TIMEOUT = 0.08
        target.UNICODE_CLIPBOARD_CONFIRM_INTERVAL = 0.002
        target.UNICODE_CLIPBOARD_RESTORE_DELAY = 0.03

        harness = ClipboardHarness(data, mime_type)
        target.read_clipboard = harness.read
        target.write_clipboard = harness.write
        target._clear_system_clipboard = harness.clear
        target.send_x11_keypress = harness.send_key
        return target, harness

    async def wait_until_unpaused(self, target):
        async def wait_loop():
            while target.clipboard_paused:
                await asyncio.sleep(0.002)
        await asyncio.wait_for(wait_loop(), timeout=0.3)

    async def test_paste_waits_until_unicode_is_observable(self):
        target, harness = self.make_input("fixed-old")
        harness.delayed_values["你"] = 3

        await target._inject_unicode_via_clipboard("你")

        self.assertEqual(harness.pasted, ["你"])
        self.assertTrue(target.clipboard_paused)
        await self.wait_until_unpaused(target)
        self.assertEqual(harness.data, "fixed-old")

    async def test_consecutive_unicode_input_debounces_one_original_restore(self):
        target, harness = self.make_input("fixed-old")

        await target._inject_unicode_via_clipboard("你")
        first_restore = target.unicode_clipboard_restore_task
        await target._inject_unicode_via_clipboard("好")
        second_restore = target.unicode_clipboard_restore_task
        await asyncio.sleep(0)

        self.assertIsNot(first_restore, second_restore)
        self.assertTrue(first_restore.cancelled())
        self.assertEqual(target.unicode_clipboard_original, ("fixed-old", "text/plain"))
        self.assertEqual(harness.pasted, ["你", "好"])
        await self.wait_until_unpaused(target)
        self.assertEqual(harness.data, "fixed-old")
        self.assertEqual(
            sum(data == "fixed-old" for data, _ in harness.writes),
            1,
        )

    async def test_inbound_clipboard_is_queued_and_latest_value_wins(self):
        target, harness = self.make_input("fixed-old")

        await target._inject_unicode_via_clipboard("你")
        await target._write_inbound_clipboard("browser-one")
        await target._write_inbound_clipboard(b"browser-two", "image/png")

        self.assertEqual(harness.data, "你")
        self.assertEqual(
            target.pending_inbound_clipboard,
            (b"browser-two", "image/png"),
        )
        await self.wait_until_unpaused(target)
        self.assertEqual(harness.data, b"browser-two")
        self.assertEqual(harness.mime_type, "image/png")

    async def test_compare_and_swap_preserves_user_copy(self):
        target, harness = self.make_input("fixed-old")

        await target._inject_unicode_via_clipboard("你")
        harness.data = "user-new-copy"
        harness.mime_type = "text/plain"
        harness.pending_visible = None

        await self.wait_until_unpaused(target)
        self.assertEqual(harness.data, "user-new-copy")
        self.assertFalse(any(data == "fixed-old" for data, _ in harness.writes))

    async def test_empty_text_and_binary_original_clipboards_restore_safely(self):
        cases = [
            (None, None),
            ("fixed-old", "text/plain"),
            (b"\x89PNG\r\n", "image/png"),
        ]
        for original_data, original_mime in cases:
            with self.subTest(data_type=type(original_data).__name__):
                target, harness = self.make_input(original_data, original_mime)
                await target._inject_unicode_via_clipboard("你")
                self.assertEqual(harness.pasted, ["你"])
                await self.wait_until_unpaused(target)
                self.assertEqual(harness.data, original_data)
                self.assertEqual(harness.mime_type, original_mime)
                if original_data is None:
                    self.assertEqual(harness.clear_count, 1)

    async def test_error_and_cancellation_always_unpause_and_restore_modifiers(self):
        control_l = 0xFFE3
        target, harness = self.make_input("fixed-old", modifiers={control_l})
        harness.fail_values.add("你")

        await target._inject_unicode_via_clipboard("你")

        self.assertFalse(target.clipboard_paused)
        self.assertIsNone(target.unicode_clipboard_restore_task)
        self.assertIn((control_l, True), harness.key_events)

        target, harness = self.make_input("fixed-old", modifiers={control_l})
        harness.never_visible.add("你")
        injection = asyncio.create_task(
            target._inject_unicode_via_clipboard("你")
        )
        await harness.write_started.wait()
        await asyncio.sleep(0)
        injection.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await injection

        self.assertFalse(target.clipboard_paused)
        self.assertIsNone(target.unicode_clipboard_restore_task)
        self.assertIn((control_l, True), harness.key_events)

    async def test_restore_task_cancellation_cleans_state(self):
        target, harness = self.make_input("fixed-old")
        await target._inject_unicode_via_clipboard("你")
        restore_task = target.unicode_clipboard_restore_task
        restore_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await restore_task

        self.assertFalse(target.clipboard_paused)
        self.assertIsNone(target.unicode_clipboard_restore_task)
        self.assertEqual(harness.data, "fixed-old")

    async def test_inbound_routes_use_shared_gate_and_logs_hide_content(self):
        source = inspect.getsource(input_handler.WebRTCInput.on_message)
        self.assertGreaterEqual(source.count("self._write_inbound_clipboard"), 3)
        self.assertNotIn("async def _write_cw", source)
        self.assertNotIn("async def _write_cb", source)
        self.assertNotIn("async def _write_multipart", source)

        target, _ = self.make_input()
        secret = "clipboard-body-must-not-appear"
        await target._write_inbound_clipboard(secret)
        self.assertNotIn(secret, "\n".join(self.logger.messages))


if __name__ == "__main__":
    unittest.main(verbosity=2)

