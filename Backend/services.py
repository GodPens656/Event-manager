from pathlib import Path
from io import IOBase
from typing import Iterable

import yagmail
from Backend.attachments import ScriptAttachment
from Backend.mailing import DEFAULT_TEMPLATES, MAIL_PROVIDERS, render_mail
from email_validator import (
    EmailSyntaxError,
    EmailUndeliverableError,
    validate_email,
)


def normalize_email(value: str) -> str:
    if not value.strip():
        raise ValueError("Введите email")
    try:
        result = validate_email(
            value.strip(), check_deliverability=True, timeout=5
        )
        return result.normalized
    except EmailSyntaxError as error:
        raise ValueError("Email имеет неверный формат") from error
    except EmailUndeliverableError as error:
        raise ValueError(
            "Почтовый домен не существует или не принимает письма"
        ) from error


def validate_recipients(recipients: Iterable[dict]) -> list[dict]:
    validated = []
    for person in recipients:
        try:
            email = normalize_email(person.get("email", ""))
        except ValueError as error:
            name = person.get("name") or "без имени"
            raise ValueError(f"Проверьте email для «{name}»: {error}") from error
        validated.append({**person, "email": email})
    return validated


def parse_amount(value: str) -> float:
    try:
        amount = float(value.strip().replace(" ", "").replace(",", "."))
    except ValueError as error:
        raise ValueError("Введите корректную сумму") from error
    if amount <= 0:
        raise ValueError("Сумма должна быть больше нуля")
    return round(amount, 2)


def budget_totals(budget: dict) -> tuple[float, float, float]:
    income = sum(float(row["amount"]) for row in budget.get("income", []))
    expenses = sum(float(row["amount"]) for row in budget.get("expenses", []))
    return income, expenses, income - expenses


class MailService:
    def __init__(self, sender: str, app_password: str, provider: str = "gmail") -> None:
        if provider not in MAIL_PROVIDERS:
            raise ValueError("Выберите поддерживаемый почтовый сервис")
        self.sender = normalize_email(sender)
        if not app_password or not app_password.strip():
            raise ValueError("Введите пароль приложения почты")
        self.smtp = yagmail.SMTP(
            self.sender, app_password.strip(), host=MAIL_PROVIDERS[provider]["host"],
            port=465, smtp_ssl=True, smtp_starttls=False, timeout=30,
        )

    def send_invitations(self, recipients: Iterable[dict], event: dict, template: dict | None = None) -> int:
        return self._send(recipients, event, template if template is not None else DEFAULT_TEMPLATES["guests"])

    def send_participant_notices(self, recipients: Iterable[dict], event: dict, script_path: str | ScriptAttachment, template: dict | None = None) -> int:
        if isinstance(script_path, ScriptAttachment):
            with script_path.open() as stream:
                return self._send(
                    recipients, event, template if template is not None else DEFAULT_TEMPLATES["participants"],
                    attachments=[stream],
                )
        script = Path(script_path)
        if not script.is_file():
            raise ValueError("Файл сценария не найден")
        return self._send(
            recipients, event, template if template is not None else DEFAULT_TEMPLATES["participants"],
            attachments=[str(script.resolve())],
        )

    def _send(self, recipients: Iterable[dict], event: dict, template: dict, attachments: list[str | IOBase] | None = None) -> int:
        # Validate every message before sending the first one.
        messages = [
            (person["email"], *render_mail(template, person, event))
            for person in validate_recipients(recipients)
        ]
        count = 0
        try:
            for email, subject, message in messages:
                for attachment in attachments or []:
                    if isinstance(attachment, IOBase):
                        attachment.seek(0)
                # A user-entered path or HTML must remain literal message text.
                self.smtp.send(email, subject, yagmail.raw(message), attachments=attachments)
                count += 1
        except Exception as error:
            raise RuntimeError(f"Отправлено писем: {count} из {len(messages)}. {error}") from error
        finally:
            self.smtp.close()
        return count
