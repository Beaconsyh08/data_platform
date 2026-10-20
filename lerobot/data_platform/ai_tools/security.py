"""Assistant disclosure controls. Authorization and the fixed tool allowlist remain the boundary."""

from __future__ import annotations

import base64
import os
import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

REFUSAL = (
    "我可以解读你有权限访问的数据摘要、质量报告和任务状态，或准备处理流程草稿。"
    "不能提供平台源码、内部指令、数据库导出、服务器文件或任何密码、密钥和令牌。"
)
_SECRET_NAME = re.compile(r"(?i)password|passwd|secret|token|(?:api|access|private)[_-]?key|database_url")
_CREDENTIALS = re.compile(
    r"(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----"
    r"|(?:mysql(?:\+pymysql)?|postgres(?:ql)?|redis|sqlite)(?:\+\w+)?://[^\s\"'<>]+"
    r"|\b(?:sk-[a-zA-Z0-9_-]{8,}|AKIA[A-Z0-9]{16}|LTAI[a-zA-Z0-9]{12,})\b"
    r"|\bBearer\s+[a-zA-Z0-9._~+/=-]+"
    r"|\beyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\b"
    r"|(?:password|passwd|api[_ -]?key|access[_ -]?token|secret|密码|密钥|令牌)"
    r"[\"']?\s*[:=：]\s*[\"']?[^\s,;\"'<>]+"
)
_SERVER_PATH = re.compile(
    r"(?i)(?<!\w)/(?:etc|home|root|opt|srv|var|tmp|proc|sys|run|mnt|data\d*)/[^\s\"'<>]*"
    r"|[A-Z]:\\[^\s\"'<>]+"
)
_ACTION = r"(?:show|print|dump|reveal|read|extract|export|output|display|give|provide|repeat|what\s+is|what's|tell|输出|打印|给我|告诉我|读出|读取|列出|查看|展示|导出|显示|获取|提供|复述)"
_PRIVATE = r"(?:source\s*code|system\s*prompt|developer\s*(?:prompt|message)|hidden\s*instructions?|database\s*(?:contents?|dump|rows|tables)|all\s+users|api[_ -]?key|password|secrets?|private[_ -]?key|access[_ -]?token|environment\s*variables?|server\.env|/etc/|/proc/|源码|源代码|系统提示词|内部指令|开发者提示|数据库(?:内容|表|记录|数据)|所有用户|密码|密钥|令牌|环境变量)"
_DISCLOSURE = re.compile(f"(?is){_ACTION}.{{0,100}}{_PRIVATE}|{_PRIVATE}.{{0,60}}{_ACTION}")
_CODE = re.compile(r"(?i)```\s*(?:python|javascript|typescript|bash|shell|sql|sh|py)\b")
_UNSAFE_PARAMETER = re.compile(
    r"(?i)^(?:(?:.+_)?(?:path|root|dir|url|uri)|api_key|password|token|secret|sql|source_code|shell_command)$"
)


def normalize(text: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", text) if unicodedata.category(char) != "Cf")


def restricted_request(text: str) -> bool:
    """Reject explicit disclosure requests; obfuscation detection is supplemental, never authorization."""
    candidates = [normalize(text), unquote(normalize(text))]
    for encoded in re.findall(r"(?<!\w)[A-Za-z0-9_+/=-]{24,4096}(?!\w)", text)[:8]:
        try:
            candidates.append(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode())
        except (ValueError, UnicodeError):
            continue
    return any(_DISCLOSURE.search(candidate) for candidate in candidates)


@dataclass(frozen=True)
class DisclosureGuard:
    # Values are held only in this process for matching; never put them into prompts or audit records.
    secrets: tuple[str, ...] = field(default=(), repr=False)

    @classmethod
    def from_environment(cls):
        values = set()
        for name, value in os.environ.items():
            if _SECRET_NAME.search(name) and len(value) >= 4:
                values.add(value)
                if "://" in value:
                    try:
                        password = urlsplit(value).password
                        if password and len(unquote(password)) >= 4:
                            values.add(unquote(password))
                    except ValueError:
                        pass
        variants = set()
        for value in values:
            variants.update(
                (
                    value,
                    quote(value, safe=""),
                    value.encode().hex(),
                    base64.b64encode(value.encode()).decode(),
                )
            )
            variants.add(base64.urlsafe_b64encode(value.encode()).decode().rstrip("="))
        return cls(tuple(sorted(variants, key=len, reverse=True)))

    def redact(self, text: str) -> str:
        normalized = normalize(text)
        value = normalized
        for secret in self.secrets:
            value = value.replace(secret, "[redacted]")
        value = _SERVER_PATH.sub("[redacted]", _CREDENTIALS.sub("[redacted]", value))
        return text if value == normalized else value

    def tree(self, value):
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, list):
            return [self.tree(item) for item in value]
        if isinstance(value, dict):
            return {self.redact(str(key)): self.tree(item) for key, item in value.items()}
        return value

    def safe_draft(self, value) -> bool:
        """Model drafts cannot introduce file/network/credential parameters through public schemas."""
        if isinstance(value, str):
            return self.redact(value) == value
        if isinstance(value, list):
            return all(self.safe_draft(item) for item in value)
        if isinstance(value, dict):
            return all(
                not _UNSAFE_PARAMETER.fullmatch(str(key)) and self.safe_draft(item)
                for key, item in value.items()
            )
        return True

    def response(self, result, *, instructions: str = ""):
        """Filter model prose and legacy receipts before persistence or presentation."""
        value = dict(result)
        if isinstance(value.get("message"), str):
            text = value["message"]
            if (
                self.redact(text) != text
                or _CODE.search(text)
                or any(
                    len(line.strip()) >= 60 and line.strip() in normalize(text)
                    for line in instructions.splitlines()
                )
            ):
                value.update(message=REFUSAL, proposal=None, security={"code": "sensitive_output"})
        if value.get("proposal") and not self.safe_draft(value["proposal"]):
            value.update(message=REFUSAL, proposal=None, security={"code": "unsafe_draft"})
        return self.tree(value)
