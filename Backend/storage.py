import asyncio
import json
from copy import deepcopy
from uuid import uuid4
from weakref import WeakKeyDictionary

from Backend.mailing import mail_settings


DEFAULT_EVENT = {
    "id": "",
    "event": {"name": "", "date": "", "place": "", "description": ""},
    "guests": [],
    "participants": [],
    "budget": {"income": [], "expenses": []},
    "mail": mail_settings({}),
}
DEFAULT_DATA = {"events": []}
STORAGE_KEY = "event_manager.events.v1"
MAX_DATA_BYTES = 2 * 1024 * 1024
_LOOP_LOCKS = WeakKeyDictionary()


class StorageConflict(ValueError):
    """The same browser's event was changed in another tab."""


def normalize_data(raw: dict) -> dict:
    """Validate browser data and migrate the old single event format."""
    if not isinstance(raw, dict):
        raise ValueError("Файл должен содержать данные мероприятий")
    if "events" in raw:
        events = raw["events"]
    elif "event" in raw:
        events = [raw]
    else:
        raise ValueError("Файл должен содержать данные мероприятий")
    if not isinstance(events, list):
        raise ValueError("Некорректный список мероприятий")

    result = deepcopy(DEFAULT_DATA)
    ids = set()
    for item in events:
        event = normalize_event(item)
        if event["id"] in ids:
            raise ValueError("Некорректный или повторяющийся идентификатор мероприятия")
        ids.add(event["id"])
        result["events"].append(event)
    return result


def normalize_event(item: dict) -> dict:
    if not isinstance(item, dict):
        raise ValueError("Некорректные данные мероприятия")

    event = deepcopy(DEFAULT_EVENT)
    for key, default in DEFAULT_EVENT.items():
        value = item.get(key, default)
        if not isinstance(value, type(default)):
            raise ValueError(f"Некорректное поле мероприятия: {key}")
        event[key] = deepcopy(value)

    if not event["id"]:
        event["id"] = uuid4().hex
    for character in event["id"]:
        if character in "/\\?#":
            raise ValueError("Некорректный или повторяющийся идентификатор мероприятия")

    details = DEFAULT_EVENT["event"].copy()
    details.update(event["event"])
    for value in details.values():
        if not isinstance(value, str):
            raise ValueError("Поля мероприятия должны содержать текст")
    event["event"] = details

    normalize_people(event["guests"], ("name", "email"))
    normalize_people(event["participants"], ("name", "email", "role"))
    event["budget"] = normalize_budget(event["budget"])
    # Пароли и вложения не входят в сохраняемые настройки.
    event["mail"] = mail_settings(event["mail"])
    return event


def normalize_people(people: list, fields: tuple) -> None:
    for person in people:
        if not isinstance(person, dict):
            raise ValueError("Некорректные данные получателя")
        for field in fields:
            if not isinstance(person.get(field), str):
                raise ValueError("Некорректные данные получателя")
        person["id"] = str(person.get("id") or uuid4().hex)


def normalize_budget(value: dict) -> dict:
    budget = deepcopy(DEFAULT_EVENT["budget"])
    budget.update(value)
    for target in ("income", "expenses"):
        rows = budget[target]
        if not isinstance(rows, list):
            raise ValueError("Некорректные статьи бюджета")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Некорректная статья бюджета")
            if not isinstance(row.get("title"), str):
                raise ValueError("Некорректная статья бюджета")
            amount = row.get("amount")
            if isinstance(amount, bool):
                raise ValueError("Некорректная сумма в бюджете")
            if not isinstance(amount, (int, float)):
                raise ValueError("Некорректная сумма в бюджете")
            if not 0 < amount < float("inf"):
                raise ValueError("Некорректная сумма в бюджете")
            row["id"] = str(row.get("id") or uuid4().hex)
    return budget


def events_by_id(events: list) -> dict:
    result = {}
    for event in events:
        result[event["id"]] = event
    return result


def encode_data(data: dict) -> str:
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_DATA_BYTES:
        raise ValueError("Объём данных превышает 2 МБ. Экспортируйте архив и удалите ненужные мероприятия.")
    return encoded


def decode_data(encoded: str) -> dict:
    if not isinstance(encoded, str) or len(encoded.encode("utf-8")) > MAX_DATA_BYTES:
        raise ValueError("Некорректный формат или слишком большой объём данных")
    try:
        return normalize_data(json.loads(encoded))
    except json.JSONDecodeError as error:
        raise ValueError("Данные повреждены: неверный формат JSON") from error


class BrowserStorage:
    """Persist only through the connected device's SharedPreferences service."""

    def __init__(self, preferences):
        self.preferences = preferences
        self._snapshot = None

    @staticmethod
    def _lock():
        # Serialize read/merge/write across tabs on this server process.
        # The registry contains locks only, never users' data.
        loop = asyncio.get_running_loop()
        if loop not in _LOOP_LOCKS:
            _LOOP_LOCKS[loop] = asyncio.Lock()
        return _LOOP_LOCKS[loop]

    async def _read(self) -> dict:
        encoded = await asyncio.wait_for(self.preferences.get(STORAGE_KEY), timeout=10)
        if encoded is None:
            return deepcopy(DEFAULT_DATA)
        return decode_data(encoded)

    async def _write(self, data: dict) -> None:
        success = await asyncio.wait_for(self.preferences.set(STORAGE_KEY, encode_data(data)), timeout=10)
        if success is not True:
            raise ValueError("Браузер не подтвердил сохранение. Проверьте доступ к хранилищу сайта и свободное место.")

    async def load(self) -> dict:
        async with self._lock():
            encoded = await asyncio.wait_for(self.preferences.get(STORAGE_KEY), timeout=10)
            if encoded is None:
                data = deepcopy(DEFAULT_DATA)
            else:
                data = decode_data(encoded)
            if encoded is not None and json.loads(encoded) != data:
                await self._write(data)
            self._snapshot = deepcopy(data)
            return data

    async def save(self, data: dict) -> None:
        submitted = normalize_data(data)
        async with self._lock():
            if self._snapshot is None:
                raise ValueError("Сначала дождитесь загрузки локальных данных")
            current = await self._read()
            before = events_by_id(self._snapshot["events"])
            after = events_by_id(submitted["events"])
            latest = events_by_id(current["events"])

            changed_ids = set()
            for event_id in before:
                if before[event_id] != after.get(event_id):
                    changed_ids.add(event_id)
            for event_id in after:
                if event_id not in before:
                    changed_ids.add(event_id)

            for event_id in changed_ids:
                if latest.get(event_id) != before.get(event_id):
                    raise StorageConflict(
                        "Мероприятие изменено в другой вкладке этого браузера. "
                        "Данные обновлены; повторите изменение."
                    )

            merged = []
            for event in current["events"]:
                event_id = event["id"]
                if event_id not in changed_ids:
                    merged.append(event)
                elif event_id in after:
                    merged.append(after[event_id])

            for event in submitted["events"]:
                event_id = event["id"]
                if event_id not in before and event_id not in latest:
                    merged.append(event)

            await self._write({"events": merged})
            self._snapshot = deepcopy(submitted)

    def last_loaded(self) -> dict:
        if self._snapshot is None:
            return deepcopy(DEFAULT_DATA)
        return deepcopy(self._snapshot)
