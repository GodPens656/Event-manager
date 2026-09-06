from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO


MAX_SCRIPT_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class ScriptAttachment:
    name: str
    content: bytes

    def __post_init__(self):
        if (not self.name or self.name in {".", ".."}
                or any(char in self.name for char in '/\\:<>"|?*')
                or any(ord(char) < 32 for char in self.name)):
            raise ValueError("Некорректное имя файла сценария")
        if not isinstance(self.content, bytes) or not self.content:
            raise ValueError("Не удалось прочитать сценарий или выбран пустой файл")
        if len(self.content) > MAX_SCRIPT_BYTES:
            raise ValueError("Размер сценария не должен превышать 5 МБ")

    @contextmanager
    def open(self):
        # Mail libraries can read a named stream directly; no server file is needed.
        with BytesIO(self.content) as stream:
            stream.name = self.name
            yield stream
