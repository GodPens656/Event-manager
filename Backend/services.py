from pathlib import Path
from io import IOBase

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


def validate_recipients(recipients):
    validated = []
    for person in recipients:
        try:
            email = normalize_email(person.get("email", ""))
        except ValueError as error:
            name = person.get("name") or "без имени"
            raise ValueError(f"Проверьте email для «{name}»: {error}") from error
        recipient = person.copy()
        recipient["email"] = email
        validated.append(recipient)
    return validated


def parse_amount(value: str) -> float:
    try:
        amount = float(value.strip().replace(" ", "").replace(",", "."))
    except ValueError as error:
        raise ValueError("Введите корректную сумму") from error
    if amount <= 0:
        raise ValueError("Сумма должна быть больше нуля")
    return round(amount, 2)


def budget_totals(budget: dict):
    income_amounts = []
    for row in budget.get("income", []):
        income_amounts.append(float(row["amount"]))

    expense_amounts = []
    for row in budget.get("expenses", []):
        expense_amounts.append(float(row["amount"]))

    income = sum(income_amounts)
    expenses = sum(expense_amounts)
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

    def send_invitations(self, recipients, event, template=None):
        if template is None:
            template = DEFAULT_TEMPLATES["guests"]
        return self._send(recipients, event, template)

    def send_participant_notices(self, recipients, event, script_path, template=None):
        if template is None:
            template = DEFAULT_TEMPLATES["participants"]
        if isinstance(script_path, ScriptAttachment):
            with script_path.open() as stream:
                return self._send(
                    recipients, event, template, attachments=[stream],
                )
        script = Path(script_path)
        if not script.is_file():
            raise ValueError("Файл сценария не найден")
        return self._send(
            recipients, event, template, attachments=[str(script.resolve())],
        )

    def _send(self, recipients, event, template, attachments=None):
        # Validate every message before sending the first one.
        messages = []
        for person in validate_recipients(recipients):
            subject, body = render_mail(template, person, event)
            messages.append((person["email"], subject, body))

        count = 0
        try:
            for email, subject, message in messages:
                if attachments:
                    for attachment in attachments:
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
