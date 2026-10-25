"""Heuristic C/C++ header parsing; no filesystem or CLI side effects."""
import re


HEADER_EXTS = {".h", ".hpp", ".hh", ".hxx"}


SKIP_NAME_TOKENS = {"class", "struct", "union", "enum", "final"}


BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


LINE_COMMENT = re.compile(r"//[^\n]*")


DECL_RE = re.compile(r"^\s*(class|struct|enum|union)\b(.*)$")


INCLUDE_RE = re.compile(r'^\s*#\s*include\s*([<"])([^>"]+)[>"]')


DEFINE_RE = re.compile(r"^\s*#\s*define\s+([A-Za-z_]\w*)")


VAR_RE = re.compile(
    r"^\s*((?:extern|static|const|constexpr|volatile|inline)\b[^;(){}]*?)"
    r"\b([A-Za-z_]\w*)\s*(\[[^\]]*\])?\s*(=[^;]*)?;\s*$")


NAME_RE = re.compile(r"[A-Za-z_]\w*")


def strip_comments(src):
    def repl(m):
        return "\n" * m.group(0).count("\n")
    src = BLOCK_COMMENT.sub(repl, src)
    src = LINE_COMMENT.sub("", src)
    return src


def looks_like_macro(token):
    return (token.isupper() or token.endswith("_API") or
            token.startswith("__"))


def symbol_name(rest):
    """Declared identifier from the text after class/struct/enum/union.
    Skips 'enum class', macro decorations (CAMERA_API) and 'final'."""
    head = rest.split("{", 1)[0].split(";", 1)[0].split(":", 1)[0]
    for tok in NAME_RE.findall(head):
        if tok in SKIP_NAME_TOKENS or looks_like_macro(tok):
            continue
        return tok
    return ""


def parse_header(src):
    """-> (symbols, includes)

    symbols:  (name, kind, decl_line) - class/struct/enum/union (incl. forward
              declarations; lines cut at the opening brace, enums kept
              whole), plus #define macros and exposed global variables.
    includes: (target, system_flag) pairs, in source order.
    """
    symbols = []
    includes = []
    for line in strip_comments(src).splitlines():
        mi = INCLUDE_RE.match(line)
        if mi:
            includes.append((mi.group(2).strip(), 1 if mi.group(1) == "<" else 0))
            continue
        md = DEFINE_RE.match(line)
        if md:
            symbols.append((md.group(1), "define", line.strip()))
            continue
        m = DECL_RE.match(line)
        if m:
            kind, rest = m.group(1), m.group(2)
            text = (kind + rest).rstrip()
            if kind in ("class", "struct", "union"):
                text = text.split("{", 1)[0].rstrip()
            if not text or text == kind:
                continue
            name = symbol_name(rest)
            if name:
                symbols.append((name, kind, text))
            continue
        mv = VAR_RE.match(line)
        if mv:
            symbols.append((mv.group(2), "var", mv.group(0).strip()))
    return symbols, includes
