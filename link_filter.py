"""链接转义规则。

目标是把文本中的链接变成 QQ 群助手不识别、但人仍然能看懂并手动打开的形式：

* ``https://example.com/a`` -> ``example\\.com/a``
* ``example.com``           -> ``example\\.com``

域名与 IPv4 转义点，IPv6 转义冒号；路径、查询串和端口保持原样。
"""

from __future__ import annotations

import ipaddress
import re

#: 出现在链接末尾、属于正文标点的字符（匹配后裁掉）。
_TRAILING_PUNCT = ".,;:!?。，；：！？、"

#: 常见文件后缀。裸域名（没有 scheme）如果以这些后缀结尾，按文件名处理，不转义。
_FILE_EXTENSIONS = frozenset(
    {
        "7z",
        "a",
        "apk",
        "ass",
        "avi",
        "bak",
        "bash",
        "bat",
        "bin",
        "bmp",
        "bz2",
        "c",
        "cfg",
        "class",
        "cmd",
        "conf",
        "cpp",
        "cs",
        "css",
        "csv",
        "cxx",
        "dat",
        "db",
        "deb",
        "dll",
        "dmg",
        "doc",
        "docx",
        "dylib",
        "eot",
        "exe",
        "fish",
        "flac",
        "flv",
        "gif",
        "go",
        "gz",
        "h",
        "hpp",
        "htm",
        "html",
        "ico",
        "img",
        "ini",
        "ipa",
        "ipynb",
        "iso",
        "jar",
        "java",
        "jpeg",
        "jpg",
        "js",
        "json",
        "json5",
        "jsx",
        "kt",
        "kts",
        "less",
        "lib",
        "lock",
        "log",
        "lua",
        "m4a",
        "map",
        "mjs",
        "mkv",
        "mp3",
        "mp4",
        "o",
        "obj",
        "odp",
        "ods",
        "odt",
        "ogg",
        "otf",
        "pdf",
        "php",
        "pkg",
        "png",
        "ppt",
        "pptx",
        "ps1",
        "psd",
        "pyc",
        "pyi",
        "r",
        "rar",
        "rb",
        "rpm",
        "rst",
        "sass",
        "scss",
        "sql",
        "sqlite",
        "sqlite3",
        "srt",
        "svg",
        "svelte",
        "swp",
        "tar",
        "tgz",
        "tif",
        "tiff",
        "tmp",
        "toml",
        "ts",
        "tsv",
        "tsx",
        "ttf",
        "txt",
        "vue",
        "war",
        "wasm",
        "wav",
        "webm",
        "webp",
        "whl",
        "woff",
        "woff2",
        "wmv",
        "xls",
        "xlsx",
        "xml",
        "xz",
        "yaml",
        "yml",
        "zsh",
        "zst",
    }
)

_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_TLD = r"(?:[A-Za-z]{2,63}|xn--[A-Za-z0-9-]{2,59})"
_DOMAIN_HOST = rf"(?:{_LABEL}\.)+{_TLD}"
_IPV4 = r"(?:\d{1,3}\.){3}\d{1,3}"

#: IPv6 候选形式。真正的合法性交给 ipaddress 校验，避免把时间等误判成地址。
_IPV6 = (
    r"(?:[0-9A-Fa-f]{0,4}:){2,7}"
    r"(?:[0-9A-Fa-f]{0,4}|(?:\d{1,3}\.){3}\d{1,3})"
)
_IPV6_HOST = rf"\[{_IPV6}(?:%[0-9A-Za-z_.-]+)?\]"

_URL_TAIL = r"[^\s<>\"'()\[\]{}，。；：！？、（）【】《》]"

#: 带 scheme 的链接。允许 IPv6 的方括号；中文标点和成对括号视为链接终点。
_URL_TOKEN = r"[^\s<>\"'(){}，。；：！？、（）【】《》]"
_SCHEME_URL_RE = re.compile(rf"(?i)(?<![A-Za-z0-9])https?://{_URL_TOKEN}+")

#: 邮箱：只改写 @ 后面的域名部分，保留 local part 原样。
_EMAIL_RE = re.compile(
    rf"(?<![A-Za-z0-9.!#$%&'*+/=?^_`{{|}}~-])"
    rf"(?P<local>[A-Za-z0-9.!#$%&'*+/=?^_`{{|}}~-]+)"
    rf"@(?P<domain>{_DOMAIN_HOST}|{_IPV4}|{_IPV6_HOST})"
    rf"(?![A-Za-z0-9-])"
)

#: 裸 IPv6（含 IPv4-mapped 形式）。用 ipaddress 二次校验。
_IPV6_RE = re.compile(
    rf"(?<![0-9A-Fa-f:])"
    rf"(?P<addr>{_IPV6}(?:%[0-9A-Za-z_.-]+)?)"
    rf"(?![0-9A-Fa-f:])"
)

#: 裸域名 / IPv4。前后做边界保护，避免把邮箱、文件名里的片段拆出来。
_DOMAIN_RE = re.compile(
    rf"(?<![A-Za-z0-9@._%+\-])"
    rf"(?P<host>{_DOMAIN_HOST}|{_IPV4})"
    rf"(?P<port>:\d{{1,5}})?"
    rf"(?P<rest>[/?#]{_URL_TAIL}*)?"
    rf"(?![A-Za-z0-9@-])"
)


def escape_links_in_text(text: str) -> str:
    """转义一段文本中的所有链接。

    Args:
        text: 待处理的文本。

    Returns:
        转义后的文本；没有链接时原样返回。
    """
    if not text:
        return text

    text = _SCHEME_URL_RE.sub(_strip_scheme, text)
    text = _EMAIL_RE.sub(_escape_email, text)
    # 先处理 IPv6，避免 IPv4-mapped 地址里的点被提前转义。
    text = _IPV6_RE.sub(_escape_bare_ipv6, text)
    return _DOMAIN_RE.sub(_escape_bare_domain, text)


class StreamingLinkEscaper:
    """流式文本的增量链接转义器。

    流式 chunk 可能在任意位置切开链接（例如 ``https://exa`` + ``mple.com``），
    所以这里只把「安全边界」之前的内容送出去，最后一个可能仍在延伸的
    ASCII token 留在缓冲区，等下一个 chunk 或结束时再处理。

    安全边界：空白、非 ASCII 字符、引号/括号。链接 token 由 ASCII 字符组成，
    在这些位置不可能继续延伸。
    """

    #: 可以明确结束一个 ASCII 链接 token 的字符。
    _SAFE_CHARS = frozenset("\"'<>()[]{}")

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, text: str) -> list[str]:
        """喂入一段流式文本，返回当前可以安全发送的转义片段。"""
        if text:
            self._pending += text

        cut = self._safe_cut(self._pending)
        if cut <= 0:
            return []

        prefix, self._pending = self._pending[:cut], self._pending[cut:]
        escaped = escape_links_in_text(prefix)
        return [escaped] if escaped else []

    def flush(self) -> list[str]:
        """流结束时清空缓冲区。"""
        if not self._pending:
            return []

        pending, self._pending = self._pending, ""
        escaped = escape_links_in_text(pending)
        return [escaped] if escaped else []

    @classmethod
    def _safe_cut(cls, text: str) -> int:
        """返回最后一个安全边界之后的位置；没有边界时返回 0。"""
        for index in range(len(text) - 1, -1, -1):
            char = text[index]
            if char.isspace() or ord(char) > 127 or char in cls._SAFE_CHARS:
                return index + 1
        return 0


def _strip_scheme(match: re.Match[str]) -> str:
    """去掉 ``http(s)://``，并转义 authority 中的域名。"""
    token = _trim_trailing_punct(match.group(0))
    body = token.split("://", 1)[1]
    cut = _authority_end(body)
    authority, rest = body[:cut], body[cut:]
    return _escape_authority(authority) + _escape_query_marker(rest)


def _escape_email(match: re.Match[str]) -> str:
    """转义邮箱域名，local part 保持原样。"""
    return match.group("local") + "@" + _escape_host_text(match.group("domain"))


def _escape_bare_ipv6(match: re.Match[str]) -> str:
    """转义裸 IPv6 地址中的冒号。"""
    addr = match.group("addr")
    core, scope_sep, scope = addr.partition("%")
    try:
        ipaddress.IPv6Address(core)
    except ValueError:
        return addr
    escaped = core.replace(":", "\\:")
    return f"{escaped}%{scope}" if scope_sep else escaped


def _escape_bare_domain(match: re.Match[str]) -> str:
    """转义裸域名 / IPv4 的 host 部分。"""
    host = match.group("host")
    if _looks_like_file_name(host):
        return match.group(0)

    port = match.group("port") or ""
    rest = _trim_trailing_punct(match.group("rest") or "")
    return _escape_host_text(host) + port + _escape_query_marker(rest)


def _escape_host_text(host: str) -> str:
    """转义 host：域名/IPv4 加点，IPv6 加冒号。"""
    if host.startswith("[") and host.endswith("]"):
        inner = host[1:-1]
        if ":" in inner:
            return "[" + inner.replace(":", "\\:") + "]"
        return "[" + inner.replace(".", "\\.") + "]"

    core, scope_sep, scope = host.partition("%")
    try:
        ipaddress.IPv6Address(core)
    except ValueError:
        return host.replace(".", "\\.")
    escaped = core.replace(":", "\\:")
    return f"{escaped}%{scope}" if scope_sep else escaped


def _authority_end(body: str) -> int:
    """返回 URL authority 结束位置（路径 / 查询 / 锚点之前）。"""
    positions = [body.find(char) for char in "/?#"]
    positions = [pos for pos in positions if pos >= 0]
    return min(positions) if positions else len(body)


def _escape_authority(authority: str) -> str:
    """只转义 authority 中 host 的点/冒号，保留 userinfo 与端口。"""
    userinfo, at, hostport = authority.rpartition("@")
    prefix = f"{userinfo}@" if at else ""

    host, port = _split_host_port(hostport)
    return prefix + _escape_host_text(host) + port


def _split_host_port(hostport: str) -> tuple[str, str]:
    """拆分 host 与端口，IPv6 方括号形式原样保留。"""
    if hostport.startswith("["):
        end = hostport.find("]")
        if end >= 0:
            return hostport[: end + 1], hostport[end + 1 :]

    # 未加方括号的 IPv6（URL 中不规范，但正文里会出现）整体保留。
    try:
        ipaddress.IPv6Address(hostport)
        return hostport, ""
    except ValueError:
        pass

    host, colon, port = hostport.rpartition(":")
    if colon and port.isdigit():
        return host, f":{port}"
    return hostport, ""


def _escape_query_marker(text: str) -> str:
    """给 query 串前的 ``?`` 加反斜杠（只处理第一个未转义的问号）。"""
    index = text.find("?")
    while index > 0 and text[index - 1] == "\\":
        index = text.find("?", index + 1)
    if index < 0:
        return text
    return text[:index] + "\\?" + text[index + 1 :]


def _trim_trailing_punct(token: str) -> str:
    """裁掉链接末尾的正文标点。"""
    while token and token[-1] in _TRAILING_PUNCT:
        token = token[:-1]
    return token


def _looks_like_file_name(host: str) -> bool:
    """判断裸字符串更像文件名而不是域名。"""
    suffix = host.rsplit(".", 1)[-1].lower()
    return suffix in _FILE_EXTENSIONS
