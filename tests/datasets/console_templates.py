"""Read composed template sources for existing browser-script behavior assertions."""

import re
from pathlib import Path

TEMPLATES = Path(__file__).parents[2] / "lerobot/data_platform/templates"


def template_source(path: str | Path) -> str:
    path = Path(path)
    if not path.is_absolute():
        path = TEMPLATES / path
    return re.sub(
        r"{%\s*include\s+[\"\x27]([^\"\x27]+)[\"\x27]\s*%}",
        lambda match: template_source(TEMPLATES / match[1]),
        path.read_text(),
    )
