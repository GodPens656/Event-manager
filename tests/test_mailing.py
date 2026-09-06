import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

import flet as ft
import yagmail

from Backend.mailing import DEFAULT_TEMPLATES, mail_settings, render_mail
from Backend.services import MailService
from Backend.storage import DEFAULT_EVENT, decode_data, encode_data, normalize_data
from Backend.attachments import ScriptAttachment
from Frontend.app import EventPlannerApp
from Frontend.mail_help import MAIL_HELP


class MailTests(unittest.TestCase):
    def setUp(self):
        self.person = {"name": "Анна", "email": "anna@example.com", "role": "Ведущая"}
        self.event = {"name": "Встреча", "date": "10.09.2026", "place": "Зал", "description": "До встречи!"}
        self.smtp_patch = patch("Backend.services.yagmail.SMTP")
        self.smtp = self.smtp_patch.start().return_value
        self.addCleanup(self.smtp_patch.stop)
        self.email_patch = patch("Backend.services.normalize_email", side_effect=lambda value: value.strip())
        self.email_patch.start()
        self.addCleanup(self.email_patch.stop)

    def test_provider_connections_and_legacy_gmail_default(self):
        for provider, host in (("gmail", "smtp.gmail.com"), ("yandex", "smtp.yandex.ru"), ("mailru", "smtp.mail.ru")):
            with self.subTest(provider=provider):
                MailService("sender@example.com", "secret", provider)
                yagmail.SMTP.assert_called_with(
                    "sender@example.com", "secret", host=host, port=465,
                    smtp_ssl=True, smtp_starttls=False, timeout=30,
                )
        MailService("sender@example.com", "secret")
        self.assertEqual(yagmail.SMTP.call_args.kwargs["host"], "smtp.gmail.com")

    def test_invalid_credentials_configuration(self):
        for password, provider in (("", "gmail"), ("  ", "yandex"), ("secret", "unknown")):
            with self.subTest(provider=provider, password=password), self.assertRaises(ValueError):
                MailService("sender@example.com", password, provider)
        yagmail.SMTP.assert_not_called()

    def test_rendering_preserves_literal_braces_and_personalizes(self):
        template = {"subject": "Для {name}: {event_name}", "body": "{{Пример}}\n{email}\n{role}\n{date}\n{place}\n{description}"}
        subject, body = render_mail(template, self.person, self.event)
        self.assertEqual(subject, "Для Анна: Встреча")
        self.assertEqual(body, "{Пример}\nanna@example.com\nВедущая\n10.09.2026\nЗал\nДо встречи!")

    def test_invalid_templates_are_rejected(self):
        for body in ("", " ", "{unknown}", "{name", "{name.__class__}", "{name!r}", "{name:>100}", "{}"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                render_mail({"subject": "Тема", "body": body}, self.person, self.event)
        with self.assertRaises(ValueError):
            render_mail({"subject": "{name}", "body": "Текст"}, {"name": "Анна\nBcc: other@example.com"}, self.event)

    def test_custom_guest_text_sent_individually_without_attachment(self):
        service = MailService("sender@example.com", "secret", "yandex")
        second = {**self.person, "name": "Борис", "email": "boris@example.com"}
        count = service.send_invitations([self.person, second], self.event, {"subject": "Привет, {name}", "body": "<Текст>\n{event_name}"})
        self.assertEqual(count, 2)
        calls = self.smtp.send.call_args_list
        self.assertEqual(calls[0].args[:2], ("anna@example.com", "Привет, Анна"))
        self.assertEqual(calls[1].args[:2], ("boris@example.com", "Привет, Борис"))
        self.assertIsInstance(calls[0].args[2], yagmail.raw)
        self.assertEqual(calls[0].args[2], "<Текст>\nВстреча")
        self.assertIsNone(calls[0].kwargs["attachments"])
        self.smtp.close.assert_called_once()

    def test_team_custom_template_keeps_script_attachment(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "сценарий.txt"
            script.write_text("Сценарий", encoding="utf-8")
            service = MailService("sender@example.com", "secret", "mailru")
            self.assertEqual(service.send_participant_notices([self.person], self.event, str(script), {"subject": "Команда", "body": "{name}: {role}"}), 1)
            call = self.smtp.send.call_args
            self.assertEqual(call.args[2], "Анна: Ведущая")
            self.assertEqual(call.kwargs["attachments"], [str(script.resolve())])

    def test_missing_script_prevents_sending(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "Файл сценария"):
                MailService("sender@example.com", "secret").send_participant_notices([self.person], self.event, str(Path(directory) / "missing.txt"))
        self.smtp.send.assert_not_called()

    def test_memory_attachment_is_complete_for_every_recipient_and_closed(self):
        streams = []

        def consume(*args, **kwargs):
            stream = kwargs["attachments"][0]
            streams.append(stream)
            self.assertEqual(stream.name, "scenario.pdf")
            self.assertEqual(stream.read(), b"%PDF-memory")

        self.smtp.send.side_effect = consume
        count = MailService("sender@example.com", "secret").send_participant_notices(
            [self.person, self.person], self.event, ScriptAttachment("scenario.pdf", b"%PDF-memory"),
        )
        self.assertEqual(count, 2)
        self.assertEqual(len(streams), 2)
        self.assertTrue(all(stream.closed for stream in streams))

    def test_all_messages_validated_before_first_send(self):
        people = [self.person, {**self.person, "name": "Invalid\nHeader"}]
        with self.assertRaises(ValueError):
            MailService("sender@example.com", "secret").send_invitations(people, self.event, {"subject": "{name}", "body": "Текст"})
        self.smtp.send.assert_not_called()

    def test_partial_failure_reports_count_and_closes_connection(self):
        self.smtp.send.side_effect = [None, OSError("Connection lost")]
        with self.assertRaisesRegex(RuntimeError, "1 из 2"):
            MailService("sender@example.com", "secret").send_invitations([self.person, self.person], self.event)
        self.smtp.close.assert_called_once()

    def test_old_data_and_custom_templates_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            old = {"event": self.event, "mail": {"sender": "old@gmail.com"}}
            path.write_text(json.dumps(old), encoding="utf-8")
            data = normalize_data(json.loads(path.read_text(encoding="utf-8")))
            settings = data["events"][0]["mail"]
            self.assertEqual(settings["sender"], "old@gmail.com")
            self.assertEqual(settings["templates"], DEFAULT_TEMPLATES)
            settings["provider"] = "mailru"
            settings["templates"]["guests"]["body"] = "Свой текст"
            self.assertEqual(decode_data(encode_data(data)), data)
            self.assertNotEqual(DEFAULT_TEMPLATES["guests"]["body"], "Свой текст")

    def test_partial_mail_settings_keep_saved_fields(self):
        settings = mail_settings({"provider": "yandex", "templates": {"guests": {"subject": "Своя тема"}}})
        self.assertEqual(settings["templates"]["guests"]["subject"], "Своя тема")
        self.assertEqual(settings["templates"]["guests"]["body"], DEFAULT_TEMPLATES["guests"]["body"])
        self.assertEqual(settings["templates"]["participants"], DEFAULT_TEMPLATES["participants"])


class MailUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.page = Mock(services=[])
        with patch("Frontend.app.BrowserStorage", autospec=True):
            self.app = EventPlannerApp(self.page)
        self.app.data = deepcopy(DEFAULT_EVENT)
        self.app.store = {"events": [self.app.data]}
        self.controls = self.app.mail_view()
        self.tiles = [c for c in self.controls if isinstance(c, ft.ExpansionTile)]

    async def test_edit_save_reload_preview_and_provider_help(self):
        provider = self.controls[3].controls[1]
        password = self.controls[4]
        provider.value = "yandex"
        password.value = "old secret"
        provider.on_select(None)
        self.assertEqual(password.value, "")
        self.assertEqual(self.tiles[0].controls[0].value, MAIL_HELP["yandex"])
        fields = self.tiles[1].controls[0].controls
        fields[0].value = "Новая тема"
        fields[1].value = "Добрый день, {name}!"
        fields[3].controls[0].on_click(None)
        self.assertIn("Иван Иванов", self.page.show_dialog.call_args.args[0].content.controls[2].value)
        save_button = next(c for c in self.controls if isinstance(c, ft.Button) and c.content == "Сохранить настройки и тексты")
        password.value = "never-save-this"
        await save_button.on_click(None)
        self.app.storage.save.assert_awaited_once_with(self.app.store)
        self.assertEqual(self.app.data["mail"]["templates"]["guests"]["body"], "Добрый день, {name}!")
        self.assertNotIn("never-save-this", json.dumps(self.app.store))
        self.assertEqual(self.app.data["mail"]["provider"], "yandex")
        controls = self.app.mail_view()
        tiles = [c for c in controls if isinstance(c, ft.ExpansionTile)]
        self.assertEqual(tiles[1].controls[0].controls[0].value, "Новая тема")
        self.assertEqual(len(self.page.services), 2)


if __name__ == "__main__":
    unittest.main()
