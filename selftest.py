"""astrbot_plugin_linkpass 的离线自测。

覆盖：scheme 去除、域名 / IPv4 / IPv6 / 邮箱转义、正文标点、幂等性和
非链接文本保护。

用法（在 AstrBot 仓库根目录）::

    uv run python data/plugins/astrbot_plugin_linkpass/selftest.py

全部检查通过时退出码为 0。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from link_filter import (  # noqa: E402
    ZERO_WIDTH_SPACE,
    StreamingLinkEscaper,
    escape_links_in_text,
)

FAILURES: list[str] = []
CHECKS = 0


def check(name: str, actual: str, expected: str) -> None:
    """记录一条检查结果。"""
    global CHECKS
    CHECKS += 1
    ok = actual == expected
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       expected: {expected!r}")
        print(f"       actual  : {actual!r}")
        FAILURES.append(name)


def with_zw(text: str) -> str:
    """给期望文本中的转义分隔符补上零宽空格。"""
    return (
        text.replace("\\.", "\\." + ZERO_WIDTH_SPACE)
        .replace("\\:", "\\:" + ZERO_WIDTH_SPACE)
        .replace("\\?", "\\?" + ZERO_WIDTH_SPACE)
    )


def main() -> int:
    """运行全部检查并返回退出码。"""
    cases = [
        # scheme 与域名
        (
            "scheme 去除与 query",
            "https://example.com/a?x=1.2",
            "example\\.com/a\\?x=1.2",
        ),
        (
            "多级域名与端口",
            "http://www.example.co.uk:8080/a.b",
            "www\\.example\\.co\\.uk:8080/a.b",
        ),
        ("大写 scheme", "HTTPS://Example.COM", "Example\\.COM"),
        ("裸域名", "example.com", "example\\.com"),
        ("裸域名带路径", "www.example.com/a", "www\\.example\\.com/a"),
        ("Markdown 链接", "[官网](https://example.com)", "[官网](example\\.com)"),
        ("中文紧邻", "看https://example.com吧", "看example\\.com吧"),
        ("中文句号", "访问https://example.com。", "访问example\\.com。"),
        ("多个链接", "a.com b.net", "a\\.com b\\.net"),
        ("localhost 仅去 scheme", "https://localhost:3000/x", "localhost:3000/x"),
        # IPv4
        ("IPv4", "http://127.0.0.1:8080", "127\\.0\\.0\\.1:8080"),
        ("裸 IPv4", "192.168.1.1", "192\\.168\\.1\\.1"),
        # IPv6
        ("IPv6 压缩", "2001:db8::1", "2001\\:db8\\:\\:1"),
        ("IPv6 回环", "::1", "\\:\\:1"),
        ("IPv6 zone", "fe80::1%eth0", "fe80\\:\\:1%eth0"),
        (
            "IPv6 URL",
            "http://[2001:db8::1]:8080/path",
            "[2001\\:db8\\:\\:1]:8080/path",
        ),
        ("IPv4-mapped", "::ffff:192.168.1.1", "\\:\\:ffff\\:192\\.168\\.1\\.1"),
        # 邮箱
        ("邮箱域名", "user@example.com", "user@example\\.com"),
        (
            "邮箱多级域名",
            "first.last@sub.example.com",
            "first.last@sub\\.example\\.com",
        ),
        ("邮箱 IPv4 字面量", "user@192.168.1.1", "user@192\\.168\\.1\\.1"),
        ("邮箱 IPv6 字面量", "user@[2001:db8::1]", "user@[2001\\:db8\\:\\:1]"),
        # 非链接
        ("文件名不转义", "notes.txt", "notes.txt"),
        ("时间不误判", "time 12:34:56", "time 12:34:56"),
        ("版本号不误判", "version 1.2.3", "version 1.2.3"),
    ]

    for name, source, expected in cases:
        check(name, escape_links_in_text(source), with_zw(expected))

    # 幂等性：转义后的文本再次处理不应继续变化。
    for name, source, expected in cases:
        escaped_expected = with_zw(expected)
        check(
            f"{name}（幂等）",
            escape_links_in_text(escaped_expected),
            escaped_expected,
        )

    def stream_escape(chunks: list[str]) -> str:
        escaper = StreamingLinkEscaper()
        output: list[str] = []
        for chunk in chunks:
            output.extend(escaper.feed(chunk))
        output.extend(escaper.flush())
        return "".join(output)

    check(
        "流式跨 chunk 域名",
        stream_escape(["看 https://exa", "mple.com/a?x=1 好"]),
        with_zw("看 example\\.com/a\\?x=1 好"),
    )
    check(
        "流式跨 chunk 邮箱",
        stream_escape(["user@exam", "ple.com 好"]),
        with_zw("user@example\\.com 好"),
    )
    check(
        "流式跨 chunk IPv6",
        stream_escape(["2001:db", "8::1 好"]),
        with_zw("2001\\:db8\\:\\:1 好"),
    )
    check(
        "流式末尾无边界",
        stream_escape(["example", ".com"]),
        with_zw("example\\.com"),
    )
    check(
        "流式普通文本",
        stream_escape(["hello", " world"]),
        "hello world",
    )

    if FAILURES:
        print(f"\n{len(FAILURES)} 项检查失败。")
        return 1
    print(f"\n全部 {CHECKS} 项检查通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
