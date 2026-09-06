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
    if not isinstance(raw, dict) or not ({"events", "event"} & raw.keys()):
        raise ValueError("Файл должен содержать данные мероприятий")
    events = raw.get("events") if "events" in raw else [raw]
    if not isinstance(events, list):
        raise ValueError("Некорректный список мероприятий")
    result = deepcopy(DEFAULT_DATA)
    ids = set()
    for item in events:
        if not isinstance(item, dict):
            raise ValueError("Некорректные данные мероприятия")
        event = deepcopy(DEFAULT_EVENT)
        for key, default in event.items():
            value = item.get(key, default)
            if not isinstance(value, type(default)):
                raise ValueError(f"Некорректное поле мероприятия: {key}")
            event[key] = deepcopy(value)
        event_id = event["id"] or uuid4().hex
        if any(c in event_id for c in "/\\?#") or event_id in ids:
            raise ValueError("Некорректный или повторяющийся идентификатор мероприятия")
        event["id"] = event_id
        ids.add(event_id)
        event["event"] = {**DEFAULT_EVENT["event"], **event["event"]}
        if any(not isinstance(value, str) for value in event["event"].values()):
            raise ValueError("Поля мероприятия должны содержать текст")
        for target in ("guests", "participants"):
            fields = ("name", "email", "role") if target == "participants" else ("name", "email")
            for person in event[target]:
                if not isinstance(person, dict) or any(not isinstance(person.get(k), str) for k in fields):
                    raise ValueError("Некорректные данные получателя")
                person["id"] = str(person.get("id") or uuid4().hex)
        event["budget"] = {**deepcopy(DEFAULT_EVENT["budget"]), **event["budget"]}
        for target in ("income", "expenses"):
            rows = event["budget"][target]
            if not isinstance(rows, list):
                raise ValueError("Некорректные статьи бюджета")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("title"), str):
                    raise ValueError("Некорректная статья бюджета")
                amount = row.get("amount")
                if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not 0 < amount < float("inf"):
                    raise ValueError("Некорректная сумма в бюджете")
                row["id"] = str(row.get("id") or uuid4().hex)
        # Only known settings are persisted; passwords and attachments never are.
        event["mail"] = mail_settings(event["mail"])
        result["events"].append(event)
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
        return deepcopy(DEFAULT_DATA) if encoded is None else decode_data(encoded)

    async def _write(self, data: dict) -> None:
        success = await asyncio.wait_for(self.preferences.set(STORAGE_KEY, encode_data(data)), timeout=10)
        if success is not True:
            raise ValueError("Браузер не подтвердил сохранение. Проверьте доступ к хранилищу сайта и свободное место.")

    async def load(self) -> dict:
        async with self._lock():
            encoded = await asyncio.wait_for(self.preferences.get(STORAGE_KEY), timeout=10)
            data = deepcopy(DEFAULT_DATA) if encoded is None else decode_data(encoded)
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
            before = {event["id"]: event for event in self._snapshot["events"]}
            after = {event["id"]: event for event in submitted["events"]}
            latest = {event["id"]: event for event in current["events"]}
            changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
            for key in changed:
                if latest.get(key) != before.get(key):
                    raise StorageConflict("Мероприятие изменено в другой вкладке этого браузера. Данные обновлены; повторите изменение.")
            merged = [
                after.get(event["id"], event) if event["id"] in changed else event
                for event in current["events"]
                if event["id"] not in changed or event["id"] in after
            ]
            merged.extend(event for event in submitted["events"] if event["id"] not in before and event["id"] not in latest)
            await self._write({"events": merged})
            self._snapshot = deepcopy(submitted)

    def last_loaded(self) -> dict:
        return deepcopy(self._snapshot if self._snapshot is not None else DEFAULT_DATA)
