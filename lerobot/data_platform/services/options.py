# Copyright 2024 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Compatibility parsing for existing console payloads."""

import re


def _parse_int_list(value) -> list[int] | None:
    if value in (None, "", []):
        return None
    if isinstance(value, list):
        raw_tokens = [str(v).strip() for v in value if str(v).strip()]
    else:
        normalized = re.sub(r"\s*-\s*", "-", str(value).strip())
        raw_tokens = [token for token in re.split(r"[\s,]+", normalized) if token]
    out: list[int] = []
    for token in raw_tokens:
        if re.fullmatch(r"\d+", token):
            out.append(int(token))
            continue
        match = re.fullmatch(r"(\d+)-(\d+)", token)
        if not match:
            raise ValueError(f"invalid integer/range token: {token!r}")
        start = int(match.group(1))
        end = int(match.group(2))
        if end < start:
            raise ValueError(f"range end must be >= start: {token!r}")
        out.extend(range(start, end + 1))
    return out


def _parse_positive_int(value, default: int = 1) -> int:
    if value in (None, ""):
        return default
    return max(1, int(value))


def _parse_str_list(value) -> list[str] | None:
    if value in (None, "", []):
        return None
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    tokens = re.split(r"[\s,]+", str(value).strip())
    return [token for token in tokens if token]


def _bool_option(options: dict, key: str, default: bool = False) -> bool:
    value = options.get(key, default)
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _float_option(options: dict, key: str, default: float) -> float:
    value = options.get(key, default)
    if value in (None, ""):
        return default
    return float(value)
