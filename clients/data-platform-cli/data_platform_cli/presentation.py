"""Readable terminal responses and explicit navigation shortcuts without model URL guessing."""

from __future__ import annotations

from rich.markdown import Markdown


def navigation_request(message: str) -> bool:
    """Recognize explicit link requests only; operation requests still go to the assistant."""
    value = " ".join(message.casefold().split()).rstrip("?？!！。.")
    return value in {
        "网址在哪儿",
        "网址在哪",
        "链接在哪儿",
        "链接在哪",
        "给我网址",
        "给我链接",
        "你给我的链接不行呀",
        "链接打不开",
        "网址打不开",
        "这个链接打不开",
        "where is the link",
        "show me the link",
        "show link",
        "the link doesn't work",
        "link doesn't work",
    }


def response_markdown(message: str, links: list[dict], origin: str) -> Markdown:
    """Model link labels/targets are not authoritative; use the server's approved reference."""
    approved = {}
    for link in links:
        approved[link["url"]] = link
        if link["url"].startswith(origin + "/"):
            approved[link["url"][len(origin) :]] = link
    rendered = Markdown(message, hyperlinks=False)

    def clean(tokens):
        output = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token.type == "link_open":
                link = approved.get(token.attrGet("href"))
                end = index + 1
                while end < len(tokens) and tokens[end].type != "link_close":
                    end += 1
                token.type, token.tag, token.nesting = "text", "", 0
                token.content = (
                    f"{link['title']} ({link['url']})" if link else "[Unverified link omitted; use /open]"
                )
                token.attrs = {}
                output.append(token)
                index = end + 1
                continue
            if token.type == "image":
                token.type, token.tag, token.content = "text", "", "[Image omitted]"
                token.children = None
            if token.children:
                token.children = clean(token.children)
            output.append(token)
            index += 1
        return output

    rendered.parsed = clean(rendered.parsed)
    return rendered
