from pathlib import Path
from typing import Iterable

import yagmail
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
    def __init__(self, sender: str, app_password: str) -> None:
        self.sender = normalize_email(sender)
        if not app_password:
            raise ValueError("Введите пароль приложения почты")
        self.smtp = yagmail.SMTP(self.sender, app_password)

    def send_invitations(self, recipients: Iterable[dict], event: dict) -> int:
        count = 0
        for person in validate_recipients(recipients):
            message = (
                f"Здравствуйте, {person['name']}!\n\n"
                f"Приглашаем вас на мероприятие «{event.get('name') or 'Без названия'}».\n"
                f"Дата: {event.get('date') or 'уточняется'}\n"
                f"Место: {event.get('place') or 'уточняется'}\n\n"
                f"{event.get('description', '')}"
            )
            self.smtp.send(person["email"], f"Приглашение: {event.get('name')}", message)
            count += 1
        return count

    def send_participant_notices(self, recipients: Iterable[dict], event: dict, script_path: str) -> int:
        script = Path(script_path)
        if not script.is_file():
            raise ValueError("Файл сценария не найден")
        count = 0
        for person in validate_recipients(recipients):
            message = (
                f"Здравствуйте, {person['name']}!\n\n"
                f"Вы участвуете в мероприятии «{event.get('name') or 'Без названия'}».\n"
                f"Ваша роль: {person['role']}.\nДата: {event.get('date') or 'уточняется'}.\n"
                "Сценарий находится во вложении."
            )
            self.smtp.send(person["email"], f"Роль и сценарий: {event.get('name')}", [message, str(script.resolve())])
            count += 1
        return count
