from dataclasses import dataclass
from io import BytesIO


MAX_SCRIPT_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class ScriptAttachment:
    name: str
    content: bytes

    def __post_init__(self):
        if not self.name or self.name in (".", ".."):
            raise ValueError("Некорректное имя файла сценария")
        for character in self.name:
            if character in '/\\:<>"|?*' or ord(character) < 32:
                raise ValueError("Некорректное имя файла сценария")
        if not isinstance(self.content, bytes) or not self.content:
            raise ValueError("Не удалось прочитать сценарий или выбран пустой файл")
        if len(self.content) > MAX_SCRIPT_BYTES:
            raise ValueError("Размер сценария не должен превышать 5 МБ")

    def open(self):
        # Mail libraries can read a named stream directly; no server file is needed.
        stream = BytesIO(self.content)
        stream.name = self.name
        return stream
