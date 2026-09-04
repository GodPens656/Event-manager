import json
from copy import deepcopy
from pathlib import Path
from threading import RLock


DEFAULT_DATA = {
    "event": {"name": "", "date": "", "place": "", "description": ""},
    "guests": [],
    "participants": [],
    "budget": {"income": [], "expenses": []},
    "mail": {"sender": ""},
}


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
            result = deepcopy(DEFAULT_DATA)
            for key, default in result.items():
                if key in raw and isinstance(raw[key], type(default)):
                    result[key] = raw[key]
            return result

    def save(self, data: dict) -> None:
        with self.lock:
            temporary = self.path.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
            temporary.replace(self.path)

