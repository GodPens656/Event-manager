import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import asyncio
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import flet as ft
import httpx
import msgpack
from websockets.sync.client import connect

from Backend.attachments import MAX_SCRIPT_BYTES, ScriptAttachment
from Backend.storage import (
    DEFAULT_EVENT, BrowserStorage, StorageConflict, STORAGE_KEY, MAX_DATA_BYTES,
    encode_data, decode_data,
)
from Frontend.app import EventPlannerApp


def event(event_id):
    item = deepcopy(DEFAULT_EVENT)
    item["id"] = event_id
    item["event"]["name"] = event_id
    return item


class FakePreferences:
    """One browser profile; share this instance between its simulated tabs."""
    def __init__(self, data=None):
        self.values = {} if data is None else {STORAGE_KEY: encode_data(data)}
        self.writes = 0

    async def get(self, key):
        await asyncio.sleep(0)
        return self.values.get(key)

    async def set(self, key, value):
        await asyncio.sleep(0)
        self.values[key] = value
        self.writes += 1
        return True


class StorageSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.preferences = FakePreferences({"events": [event("one"), event("two")]})
        self.first = BrowserStorage(self.preferences)
        self.second = BrowserStorage(self.preferences)
        self.left = await self.first.load()
        self.right = await self.second.load()

    async def test_different_events_merge_and_can_be_saved_again(self):
        self.left["events"][0]["event"]["name"] = "First change"
        self.right["events"][1]["event"]["name"] = "Second change"
        await asyncio.gather(self.first.save(self.left), self.second.save(self.right))
        self.left["events"][0]["event"]["place"] = "Hall"
        await self.first.save(self.left)
        saved = (await self.first.load())["events"]
        self.assertEqual(saved[0]["event"]["name"], "First change")
        self.assertEqual(saved[0]["event"]["place"], "Hall")
        self.assertEqual(saved[1]["event"]["name"], "Second change")

    async def test_same_event_conflicts_without_overwriting(self):
        self.left["events"][0]["event"]["name"] = "Saved"
        await self.first.save(self.left)
        self.right["events"][0]["event"]["name"] = "Stale"
        with self.assertRaises(StorageConflict):
            await self.second.save(self.right)
        self.assertEqual((await self.second.load())["events"][0]["event"]["name"], "Saved")

    async def test_additions_and_deletions_do_not_lose_other_events(self):
        self.left["events"].append(event("three"))
        await self.first.save(self.left)
        self.right["events"].pop(0)
        await self.second.save(self.right)
        self.right["events"][0]["event"]["place"] = "Changed again"
        await self.second.save(self.right)
        self.assertEqual([item["id"] for item in (await self.second.load())["events"]], ["two", "three"])
        self.left["events"][0]["event"]["place"] = "Do not resurrect"
        with self.assertRaises(StorageConflict):
            await self.first.save(self.left)

    async def test_different_profiles_are_isolated_and_reopening_preserves_data(self):
        other = BrowserStorage(FakePreferences())
        self.assertEqual(await other.load(), {"events": []})
        await other.save({"events": [event("private")]})
        self.assertEqual(await BrowserStorage(self.preferences).load(), self.left)
        self.assertNotIn("private", encode_data(await self.first.load()))

    async def test_corrupt_json_is_not_overwritten(self):
        self.preferences.values[STORAGE_KEY] = "{broken"
        with self.assertRaises(ValueError):
            await self.first.load()
        self.assertEqual(self.preferences.values[STORAGE_KEY], "{broken")
        self.assertEqual(self.preferences.writes, 0)

    async def test_legacy_data_migrates_and_password_is_not_saved(self):
        self.preferences.values[STORAGE_KEY] = json.dumps({
            "event": {"name": "Old"},
            "mail": {"sender": "old@gmail.com", "password": "do-not-persist"},
        })
        loaded = await self.first.load()
        self.assertEqual(loaded, await self.second.load())
        self.assertEqual(loaded["events"][0]["mail"]["sender"], "old@gmail.com")
        self.assertNotIn("do-not-persist", self.preferences.values[STORAGE_KEY])

    async def test_refused_write_keeps_snapshot_and_stored_data(self):
        self.left["events"][0]["event"]["name"] = "Failed"
        before = self.preferences.values[STORAGE_KEY]
        with patch.object(self.preferences, "set", AsyncMock(return_value=False)):
            with self.assertRaises(ValueError):
                await self.first.save(self.left)
        self.assertEqual(self.preferences.values[STORAGE_KEY], before)
        self.assertEqual(self.first.last_loaded()["events"][0]["event"]["name"], "one")

    async def test_invalid_or_oversized_import_is_rejected(self):
        for payload in ("{}", '{"events": [{} , {"guests": [1]}]}', "x" * (MAX_DATA_BYTES + 1)):
            with self.assertRaises(ValueError):
                decode_data(payload)

    async def test_partial_legacy_budget_does_not_share_lists_between_profiles(self):
        first = decode_data('{"events": [{"budget": {"income": []}}]}')
        second = decode_data('{"events": [{"budget": {"income": []}}]}')
        first["events"][0]["budget"]["expenses"].append({"amount": 1})
        self.assertEqual(second["events"][0]["budget"]["expenses"], [])
        self.assertEqual(DEFAULT_EVENT["budget"]["expenses"], [])


class AttachmentTests(unittest.TestCase):
    def test_named_stream_is_closed_after_failure(self):
        script = ScriptAttachment("сценарий.pdf", b"%PDF-test")
        with self.assertRaisesRegex(OSError, "SMTP failed"):
            with script.open() as stream:
                self.assertEqual(stream.name, "сценарий.pdf")
                self.assertEqual(stream.read(), b"%PDF-test")
                raise OSError("SMTP failed")
        self.assertTrue(stream.closed)

    def test_independent_streams_for_same_filename(self):
        with ScriptAttachment("script.txt", b"first").open() as first:
            with ScriptAttachment("script.txt", b"second").open() as second:
                self.assertIsNot(first, second)
                self.assertEqual(first.read(), b"first")
                self.assertEqual(second.read(), b"second")

    def test_invalid_names_empty_and_oversized_files(self):
        for name in ("../data.json", "..\\data.json", "C:\\file.txt", "..", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                ScriptAttachment(name, b"content")
        for content in (None, b"", b"x" * (MAX_SCRIPT_BYTES + 1)):
            with self.assertRaises(ValueError):
                ScriptAttachment("script.txt", content)


class BrowserUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.page = Mock(services=[], width=1200, route="/event/one/mail")
        with patch("Frontend.app.BrowserStorage", autospec=True):
            self.app = EventPlannerApp(self.page)
        self.app.data = event("one")
        self.app.store = {"events": [self.app.data]}
        self.app.storage.load.return_value = self.app.store
        self.controls = self.app.mail_view()

    def button(self, name):
        return next(control for control in self.controls
                    if isinstance(control, ft.Button) and control.content == name)

    async def pick(self, files):
        self.app.mail_picker = Mock(pick_files=AsyncMock(return_value=files))
        await self.button("Выбрать файл").on_click(None)

    async def test_browser_file_has_no_local_path_and_cancel_keeps_selection(self):
        await self.pick([ft.FilePickerFile(id=1, name="script.pdf", size=4, bytes=b"test")])
        self.assertEqual(self.app.script_attachment.content, b"test")
        self.app.mail_picker.pick_files.assert_awaited_once_with(
            dialog_title="Выберите сценарий", allow_multiple=False, with_data=True,
        )
        await self.pick([])
        self.assertEqual(self.app.script_attachment.name, "script.pdf")
        await self.pick([ft.FilePickerFile(id=2, name="empty.pdf", size=0, bytes=b"")])
        self.assertEqual(self.app.script_attachment.name, "script.pdf")

    async def test_selection_after_switching_event_is_ignored(self):
        async def selection(**_):
            self.app.data = event("two")
            return [ft.FilePickerFile(id=1, name="old.txt", size=4, bytes=b"test")]
        self.app.mail_picker = Mock(pick_files=AsyncMock(side_effect=selection))
        await self.button("Выбрать файл").on_click(None)
        self.assertIsNone(self.app.script_attachment)

    async def test_send_passes_uploaded_bytes_without_server_file(self):
        self.app.data["participants"] = [{"name": "Test User", "email": "test@example.com", "role": "Host"}]
        self.app.script_attachment = ScriptAttachment("scenario.txt", b"scenario")
        self.controls[3].controls[0].value = "sender@example.com"
        self.controls[4].value = "secret"
        captured = []

        def deliver(people, event_data, attachment, template):
            captured.append(attachment)
            self.assertEqual(attachment.content, b"scenario")
            self.assertTrue(self.app.sending)
            return 1

        with patch("Frontend.app.MailService") as mail:
            mail.return_value.send_participant_notices.side_effect = deliver
            await self.app.send_buttons[1].on_click(None)
            mail.assert_called_once_with("sender@example.com", "secret", "gmail")
        self.assertEqual(len(captured), 1)
        self.assertIsInstance(captured[0], ScriptAttachment)
        self.assertFalse(self.app.sending)
        self.assertFalse(self.app.send_buttons[1].disabled)

    async def test_missing_attachment_does_not_open_smtp(self):
        self.app.data["participants"] = [{"name": "Test User"}]
        with patch("Frontend.app.MailService") as mail:
            await self.app.send_buttons[1].on_click(None)
            mail.assert_not_called()

    async def test_second_send_is_ignored_while_first_is_running(self):
        self.app.sending = True
        with patch("Frontend.app.MailService") as mail:
            await self.app.send_buttons[0].on_click(None)
            mail.assert_not_called()
        self.assertTrue(self.app.sending)

    async def test_resize_preserves_form_values_and_event_switch_clears_secrets(self):
        self.app.show_workspace(0)
        form = self.app.content.controls[-1]
        name = form.controls[0].controls[0]
        name.value = "Unsaved edit"
        self.page.width = 390
        self.app.resize_workspace()
        self.assertFalse(self.app.sidebar.visible)
        self.assertTrue(self.page.navigation_bar.visible)
        self.assertIs(self.app.content.controls[-1], form)
        self.assertEqual(name.value, "Unsaved edit")
        self.app.password = "secret"
        self.app.script_attachment = ScriptAttachment("script.txt", b"text")
        self.app.show_event_selector()
        self.assertIsNone(self.app.script_attachment)
        self.assertEqual(self.app.password, "")
        self.assertIsNone(self.page.navigation_bar)

    async def test_storage_conflict_reloads_and_reports_failure(self):
        self.app.storage.save.side_effect = StorageConflict("Changed in another tab")
        with patch.object(self.app, "route_change") as reload_page:
            self.assertFalse(await self.app.save_store())
        reload_page.assert_called_once_with(None)

    async def test_import_adds_copies_and_export_contains_local_data(self):
        preferences = FakePreferences({"events": [event("one")]})
        self.app.storage = BrowserStorage(preferences)
        self.app.store = await self.app.storage.load()
        archive = encode_data({"events": [event("one")]}).encode("utf-8")
        picker = Mock(
            pick_files=AsyncMock(return_value=[ft.FilePickerFile(id=1, name="data.json", size=len(archive), bytes=archive)]),
            save_file=AsyncMock(),
        )
        self.app.backup_picker = picker
        await self.app.import_data(None)
        saved = await BrowserStorage(preferences).load()
        self.assertEqual(len(saved["events"]), 2)
        self.assertEqual(saved["events"][0]["id"], "one")
        self.assertNotEqual(saved["events"][1]["id"], "one")
        await self.app.export_data(None)
        self.assertEqual(json.loads(picker.save_file.call_args.kwargs["src_bytes"]), saved)

    async def test_failed_save_rolls_back_session_and_failed_load_blocks_forms(self):
        preferences = FakePreferences({"events": [event("one")]})
        self.app.storage = BrowserStorage(preferences)
        self.app.store = await self.app.storage.load()
        self.app.data = self.app.store["events"][0]
        self.app.data["event"]["name"] = "Failed edit"
        with patch.object(preferences, "set", AsyncMock(return_value=False)):
            self.assertFalse(await self.app.save_store())
        self.assertEqual(self.app.data["event"]["name"], "one")
        with patch.object(preferences, "get", AsyncMock(side_effect=RuntimeError("Storage unavailable"))):
            await self.app.route_change(None)
        self.assertIsNone(self.app.data)
        self.assertIn("Не удалось загрузить", self.page.add.call_args.args[0].controls[0].value)


class WebServerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = str(Path(directory.name) / "events.json")
        self.legacy_path = Path(path)
        self.legacy_path.write_text('{"events": [{"id": "server-private"}]}', encoding="utf-8")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.http_url = f"http://127.0.0.1:{port}"
        self.ws_url = f"ws://127.0.0.1:{port}/ws"
        entrypoint = Path(__file__).resolve().parent.parent / "main.py"
        self.server = subprocess.Popen(
            [sys.executable, str(entrypoint)],
            cwd=directory.name,
            env=dict(
                os.environ,
                EVENT_DATA_FILE=path,
                FLET_SERVER_IP="127.0.0.1",
                FLET_SERVER_PORT=str(port),
                # Exercise the real Flet entry point without opening a GUI in tests.
                FLET_FORCE_WEB_SERVER="true",
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self.addCleanup(self.stop_server)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                self.fail(self.server.stdout.read().decode("utf-8", errors="replace"))
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            self.fail("Flet startup timed out")

    def stop_server(self):
        self.server.terminate()
        try:
            self.server.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.server.kill()
            self.server.communicate()

    def test_http_deep_links_and_real_flet_sessions(self):
        with httpx.Client(base_url=self.http_url, timeout=5, trust_env=False) as client:
            routes = {
                "/": "Выберите мероприятие для редактирования",
                "/events/add": "Добавить мероприятие",
                "/event/one": "Основная информация для писем",
                "/event/one/mail": "Тексты сохраняются отдельно",
                "/event/one/guests/add": "Укажите имя и email",
            }
            for route, expected in routes.items():
                with self.subTest(route=route):
                    response = client.get(route)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("text/html", response.headers["content-type"])
                    with connect(self.ws_url, open_timeout=5, proxy=None) as websocket:
                        websocket.send(msgpack.packb([1, {
                            "session_id": "", "page_name": "",
                            "page": {"route": route, "web": True, "width": 390, "height": 844},
                        }]))
                        registration = msgpack.unpackb(websocket.recv(timeout=5))
                        self.assertEqual(registration[0], 1)
                        self.assertEqual(registration[1]["error"], "")
                        # Startup builds controls through the actual Flet session.
                        for _ in range(20):
                            update = msgpack.unpackb(websocket.recv(timeout=5), strict_map_key=False)
                            if update[0] == 5:
                                request = update[1]
                                self.assertEqual(request["args"]["key"], STORAGE_KEY)
                                result = encode_data({"events": [event("one")]}) if request["name"] == "get" else True
                                websocket.send(msgpack.packb([5, {
                                    "control_id": request["control_id"], "call_id": request["call_id"],
                                    "result": result, "error": "",
                                }]))
                                continue
                            self.assertEqual(update[0], 2, update)
                            self.assertNotIn("server-private", str(update))
                            if expected in str(update):
                                break
                        else:
                            self.fail(f"View did not render: {route}")
            for route in ("/data/event_data.json", "/Backend/storage.py"):
                response = client.get(route)
                self.assertNotIn('"events":', response.text)
                self.assertNotIn("class BrowserStorage", response.text)
            self.assertEqual(self.legacy_path.read_text(encoding="utf-8"), '{"events": [{"id": "server-private"}]}')


if __name__ == "__main__":
    unittest.main()
