from copy import deepcopy
from string import Formatter


MAIL_PROVIDERS = {
    "gmail": {"name": "Gmail", "host": "smtp.gmail.com"},
    "yandex": {"name": "Яндекс Почта", "host": "smtp.yandex.ru"},
    "mailru": {"name": "Mail.ru", "host": "smtp.mail.ru"},
}

DEFAULT_TEMPLATES = {
    "guests": {
        "subject": "Приглашение: {event_name}",
        "body": (
            "Здравствуйте, {name}!\n\n"
            "Приглашаем вас на мероприятие «{event_name}».\n"
            "Дата: {date}\nМесто: {place}\n\n{description}"
        ),
    },
    "participants": {
        "subject": "Роль и сценарий: {event_name}",
        "body": (
            "Здравствуйте, {name}!\n\n"
            "Вы участвуете в мероприятии «{event_name}».\n"
            "Ваша роль: {role}.\nДата: {date}.\n"
            "Сценарий находится во вложении."
        ),
    },
}


def mail_settings(value: dict) -> dict:
    """Fill missing mail settings, including settings from older data files."""
    result = {"sender": "", "provider": "gmail", "templates": deepcopy(DEFAULT_TEMPLATES)}
    if isinstance(value.get("sender"), str):
        result["sender"] = value["sender"]
    if isinstance(value.get("provider"), str) and value["provider"] in MAIL_PROVIDERS:
        result["provider"] = value["provider"]
    templates = value.get("templates", {})
    if isinstance(templates, dict):
        for target, template in result["templates"].items():
            saved = templates.get(target, {})
            if isinstance(saved, dict):
                for field in template:
                    if isinstance(saved.get(field), str):
                        template[field] = saved[field]
    return result


def render_mail(template: dict, person: dict, event: dict) -> tuple[str, str]:
    values = {
        "name": person.get("name") or "получатель",
        "email": person.get("email", ""),
        "role": person.get("role") or "уточняется",
        "event_name": event.get("name") or "Без названия",
        "date": event.get("date") or "уточняется",
        "place": event.get("place") or "уточняется",
        "description": event.get("description", ""),
    }
    rendered = []
    for field, label in (("subject", "Тема"), ("body", "Текст")):
        text = template.get(field, "")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{label} письма не может быть пустым")
        try:
            for _, name, spec, conversion in Formatter().parse(text):
                if name is not None:
                    if name not in values:
                        raise ValueError(f"Неизвестная подстановка: {{{name}}}")
                    if spec or conversion:
                        raise ValueError("Используйте подстановки без форматов и преобразований")
            result = text.format_map(values)
        except ValueError as error:
            raise ValueError(f"{label} письма: {error}. Для обычных скобок используйте {{{{ и }}}}") from error
        if not result.strip():
            raise ValueError(f"{label} письма пуст после подстановки данных")
        if field == "subject" and ("\n" in result or "\r" in result):
            raise ValueError("Тема письма должна быть в одну строку")
        rendered.append(result)
    return rendered[0], rendered[1]
