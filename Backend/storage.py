import json
from copy import deepcopy
from pathlib import Path
from threading import RLock
from uuid import uuid4


DEFAULT_EVENT = {
    "id": "",
    "event": {"name": "", "date": "", "place": "", "description": ""},
    "guests": [],
    "participants": [],
    "budget": {"income": [], "expenses": []},
    "mail": {"sender": ""},
}

DEFAULT_DATA = {"events": []}


class JsonStorage:
    def __init__(self, path: str = "data/event_data.json") -> None:
        self.path = Path(path)
        self.lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.save(deepcopy(DEFAULT_DATA))

    def load(self) -> dict:
        with self.lock:
            try:
                with self.path.open("r", encoding="utf-8") as file:
                    raw = json.load(file)
            except (OSError, json.JSONDecodeError):
                raw = {}
            if not isinstance(raw, dict):
                raw = {}

            # Старый формат содержал одно мероприятие в корне файла. При
            # первом запуске новой версии переносим его в список без потерь.
            migrated = "events" not in raw and "event" in raw
            raw_events = [raw] if migrated else raw.get("events", [])
            result = deepcopy(DEFAULT_DATA)
            if isinstance(raw_events, list):
                for raw_event in raw_events:
                    if not isinstance(raw_event, dict):
                        continue
                    event_data = deepcopy(DEFAULT_EVENT)
                    for key, default in event_data.items():
                        value = raw_event.get(key)
                        if isinstance(value, type(default)):
                            event_data[key] = value
                    event_data["id"] = event_data["id"] or uuid4().hex
                    result["events"].append(event_data)
            if migrated:
                self.save(result)
            return result

    def save(self, data: dict) -> None:
        with self.lock:
            temporary = self.path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
            temporary.replace(self.path)
