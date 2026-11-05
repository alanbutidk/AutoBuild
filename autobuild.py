import threading as td
import subprocess
import os
import sys
import asyncio
import concurrent.futures
import difflib
import glob
import io
import re
import shlex
import shutil
import signal
import hashlib
import json
import time
import datetime
from enum import Enum, auto
from typing import Any


# ---------------------------------------------------------------------------
# Typed values.
#
# Every AutoBuild value is one of: string, int, float, bool, NULL, list.
# Python side: str, int, float, bool, None, list.
#
# Literals (right-hand side of `let`, call arguments, `if` operands):
#   42  -7            -> int            (no leading zeros: 007 stays a string)
#   3.14  1e5         -> float
#   true  false       -> bool
#   NULL              -> NULL
#   "text"  'text'    -> string (quotes force a string: "42" is NOT an int)
#   [1, two, (x)]     -> list (elements are typed too, lists can nest)
#   anything else     -> string (so existing bare-word scripts keep working)
#
# `(name)` inside a larger piece of text interpolates the value as text
# (true/false, NULL, lists joined by spaces). A lone `(name)` keeps the
# value's type.
# ---------------------------------------------------------------------------
class _Unknown(Exception):
    """Raised by the optimizer's lookup when a variable isn't statically known."""


_MISSING = object()
_INT_RE = re.compile(r"^[+-]?(0|[1-9]\d*)$")
_FLOAT_RE = re.compile(r"^[+-]?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?$")


def _coerce_literal(text: str):
    t = text.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        return t[1:-1]
    if t == "NULL":
        return None
    if t == "true":
        return True
    if t == "false":
        return False
    if _INT_RE.match(t):
        return int(t)
    if _FLOAT_RE.match(t):
        return float(t)
    return t


def _type_name(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, list):
        return "list"
    return "string"


def _to_str(v) -> str:
    if v is None:
        return "NULL"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, list):
        return " ".join(_to_str(i) for i in v)
    return str(v)


def _truthy(v) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, list):
        return len(v) > 0
    return str(v).strip().lower() not in ("", "false", "null")


def _as_number(v):
    if isinstance(v, bool) or v is None or isinstance(v, list):
        raise ValueError("not a number")
    return float(v)


def _compare(lv, op, rv) -> bool:
    """Shared by the executor and the optimizer so both always agree.
    op None means plain truthiness of lv."""
    if op is None:
        return _truthy(lv)
    try:
        l, r = _as_number(lv), _as_number(rv)
    except (ValueError, TypeError):
        l, r = _to_str(lv), _to_str(rv)
    return {
        "==": l == r,
        "!=": l != r,
        ">": l > r,
        "<": l < r,
        ">=": l >= r,
        "<=": l <= r,
    }[op]


def _split_list_literal(parts: list):
    """If `parts` (a list of (kind, text)) is a `[a, b, c]` literal, returns
    its elements as parts-lists, else None."""
    if not parts:
        return None
    k0, v0 = parts[0]
    k1, v1 = parts[-1]
    if k0 != "STRING" or k1 != "STRING":
        return None
    if not v0.lstrip().startswith("[") or not v1.rstrip().endswith("]"):
        return None
    p = list(parts)
    p[0] = ("STRING", p[0][1].lstrip()[1:])
    p[-1] = ("STRING", p[-1][1].rstrip()[:-1])
    elements, cur = [], []
    depth, quote = 0, None
    for kind, val in p:
        if kind != "STRING":
            cur.append((kind, val))
            continue
        buf = ""
        for ch in val:
            if quote:
                buf += ch
                if ch == quote:
                    quote = None
            elif ch in "\"'" and (buf.strip() == "" or buf.rstrip()[-1] in ",["):
                quote = ch
                buf += ch
            elif ch == "[":
                depth += 1
                buf += ch
            elif ch == "]":
                depth -= 1
                if depth < 0:
                    return None  # e.g. `[a] [b]` - not ONE list literal
                buf += ch
            elif ch == "," and depth == 0:
                if buf:
                    cur.append(("STRING", buf))
                    buf = ""
                elements.append(cur)
                cur = []
            else:
                buf += ch
        if buf:
            cur.append(("STRING", buf))
    if depth != 0 or quote:
        return None
    elements.append(cur)
    return [
        e
        for e in elements
        if e and not all(k == "STRING" and v.strip() == "" for k, v in e)
    ]


def _eval_parts(parts: list, lookup):
    """Turns parsed (kind, text) parts into a typed value. `lookup(name)`
    returns a variable's value (the executor warns on unknown names, the
    optimizer raises _Unknown)."""
    if not parts:
        return ""
    elems = _split_list_literal(parts)
    if elems is not None:
        return [_eval_parts(e, lookup) for e in elems]
    # trim the edges so `[a, (x), 3]` yields x's own typed value, not " x"
    parts = list(parts)
    if parts[0][0] == "STRING":
        parts[0] = ("STRING", parts[0][1].lstrip())
    if parts[-1][0] == "STRING":
        parts[-1] = ("STRING", parts[-1][1].rstrip())
    parts = [p for p in parts if not (p[0] == "STRING" and p[1] == "")]
    if not parts:
        return ""
    if len(parts) == 1:
        kind, val = parts[0]
        if kind == "VAR_REF":
            return lookup(val)
        return _coerce_literal(val)
    parts = list(parts)
    (k0, v0), (k1, v1) = parts[0], parts[-1]
    if k0 == "STRING" and k1 == "STRING":
        a, b = v0.lstrip(), v1.rstrip()
        if a[:1] in ("'", '"') and b[-1:] == a[:1]:
            parts[0] = ("STRING", a[1:])
            parts[-1] = ("STRING", b[:-1])
    return "".join(
        val if kind != "VAR_REF" else _to_str(lookup(val)) for kind, val in parts
    )


def _for_list_ref(text: str) -> str:
    t = text.strip()
    m = re.fullmatch(r"\((\w+)\)", t)
    return m.group(1) if m else t


# Token Types
class _TokenType(Enum):
    """This is a important internal token declare class
    NOT MEANT FOR PUBLIC USAGE"""

    COMMENT = auto()  # # ...
    GET = auto()  # get
    LET = auto()  # let
    IF = auto()  # if
    ELIF = auto()  # elif
    ELSE = auto()  # else
    THEN = auto()  # then
    DONE = auto()  # done
    FOR = auto()  # for
    FOR_TILL = auto()  # for till(n)
    FOR_IN = auto()  # for x in (LIST)
    WHILE = auto()  # while <cond> do
    TRY = auto()  # try
    CATCH = auto()  # catch / on_fail
    RETRY = auto()  # retry N [every S] do
    TIMEOUT = auto()  # timeout S do
    BREAK = auto()  # break
    CONTINUE = auto()  # continue
    DO = auto()  # do
    SHELL = auto()  # shell
    PARALLEL = auto()
    PARALLEL_DO = auto()  # parallel do
    PARALLEL_END = auto()  # pend
    RECIPE = auto()  # recipe declaration
    RECIPE_DEF = auto()  # recipe definition (name:)
    DEPENDS = auto()  # recipe dependencies
    IDENTIFIER = auto()  # variable name / value
    ASSIGN = auto()  # =
    OPERATOR = auto()  # == != > < >= <=
    VAR_REF = auto()  # (VARNAME)
    STRING = auto()  # raw string / command
    COLON = auto()  # :
    NEWLINE = auto()  # line separator
    NULL = auto()  # NULL value from missing env var
    # --- new ---
    FN = auto()  # fn
    FN_DEF = auto()  # fn name(args) {
    LBRACE = auto()  # {
    RBRACE = auto()  # }
    RETURN = auto()  # return
    CALL = auto()  # name(args) used as an expression (let x = name(a,b))
    MODULE = auto()  # module Name
    MODULE_END = auto()  # done (closing a module block, reuses DONE at parse level)
    EOF = auto()  # end of file


class Token:
    def __init__(self, type: _TokenType, value: str, line: int):
        self.type = type
        self.value = value
        self.line = line

    def __repr__(self):
        return f"Token({self.type.name}, {repr(self.value)}, line={self.line})"


class Tokenize:
    def __init__(self, source: str):
        self.source = source
        self.tokens = []
        self.lines = source.splitlines()
        self.line = 1
        self._tokenize()

    @staticmethod
    def _parse_var_refs(text: str) -> list:
        tokens = []
        while text:
            start = text.find("(")
            if start == -1:
                tokens.append(("STRING", text))
                break
            if start > 0:
                tokens.append(("STRING", text[:start]))
            end = text.find(")", start)
            if end == -1:
                tokens.append(("STRING", text[start:]))
                break
            var_contents = text[start + 1 : end]
            # Only treat this as a var-ref if it looks like a plain
            # identifier (letters/digits/underscore, not starting with
            # a digit). Anything else - spaces, quotes, commas, or
            # nested calls like GetTokens(,) / Tokenize("...") - is
            # literal text, not a variable reference. Emit just the
            # "(" as a literal char and keep scanning from right after
            # it, so inner parens get handled on their own instead of
            # the whole span being swallowed as one bogus var name.
            if (
                var_contents
                and var_contents.replace("_", "").isalnum()
                and not var_contents[0].isdigit()
            ):
                tokens.append(("VAR_REF", var_contents))
                text = text[end + 1 :]
            else:
                tokens.append(("STRING", "("))
                text = text[start + 1 :]
        return tokens

    @staticmethod
    def _scan_top(text: str):
        """Yields (index, char) for characters that sit at the top level of
        a condition: outside "quotes", (parens) and [brackets]."""
        depth, quote = 0, None
        for i, ch in enumerate(text):
            if quote:
                if ch == quote:
                    quote = None
                continue
            before = text[:i].rstrip()
            if ch in "\"'" and (before == "" or before[-1] in ",[(=<>!"):
                quote = ch
            elif ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif depth == 0:
                yield i, ch

    @staticmethod
    def _split_logic(text: str, which: str) -> list:
        """Splits a condition on top-level `or`/`||` (which="or") or
        `and`/`&&` (which="and"). Returns [text] when there is no such operator."""
        pat = (
            re.compile(r"\s+or\s+|\|\|")
            if which == "or"
            else re.compile(r"\s+and\s+|&&")
        )
        top = {i for i, _ in Tokenize._scan_top(text)}
        parts, start, i = [], 0, 0
        while i < len(text):
            m = pat.match(text, i) if i in top else None
            if m and m.end() > i:
                parts.append(text[start:i])
                start = i = m.end()
            else:
                i += 1
        parts.append(text[start:])
        return [p.strip() for p in parts]

    @staticmethod
    def _parse_condition(condition: str) -> tuple:
        c = condition.strip()
        # `a and b`, `a or b`, `not x == 3`, and bare calls are expressions
        # evaluated whole at runtime (op None => Executor._eval_expr)
        if (
            len(Tokenize._split_logic(c, "or")) > 1
            or len(Tokenize._split_logic(c, "and")) > 1
            or c.startswith("not ")
            or Tokenize._is_call_like(c)
        ):
            return c, None, None
        top = {i for i, _ in Tokenize._scan_top(c)}
        for i in sorted(top):
            for op in (">=", "<=", "!=", "==", ">", "<"):
                if c.startswith(op, i):
                    return c[:i].strip(), op, c[i + len(op) :].strip()
        return c, None, None

    @staticmethod
    def _is_call_like(s: str) -> bool:
        """name(args) shape - the name must touch the "(" and the call's own
        closing paren must be the LAST character, so `hello (x)` and
        `f(a) (b)` are text, not calls."""
        s = s.strip()
        if "(" not in s or not s.endswith(")"):
            return False
        name = s.partition("(")[0]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", name):
            return False
        depth = 0
        for i in range(len(name), len(s)):
            ch = s[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(s) - 1:
                    return False
        return depth == 0

    @staticmethod
    def _split_call(value: str) -> "tuple[str, str]":
        """Splits `name(args...)` into (name, args_text), matching the
        call's own closing paren by tracking depth - so a var-ref like
        (h) inside the arg list doesn't get mistaken for the call's own
        closing paren."""
        name, _, rest = value.partition("(")
        depth = 1
        i = 0
        for i, ch in enumerate(rest):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
        args_text = rest[:i]
        return name.strip(), args_text

    @staticmethod
    def _split_args(args_text: str) -> list:
        """Splits a call's argument text on top-level commas only - commas
        inside (VAR_REF), [lists] or "quoted strings" don't separate args."""
        args = []
        depth = 0
        current = ""
        quote = None
        for ch in args_text:
            if quote:
                current += ch
                if ch == quote:
                    quote = None
            elif ch in "\"'" and (
                current.strip() == "" or current.rstrip()[-1] in ",[("
            ):
                quote = ch
                current += ch
            elif ch in "([":
                depth += 1
                current += ch
            elif ch in ")]":
                depth -= 1
                current += ch
            elif ch == "," and depth == 0:
                args.append(current)
                current = ""
            else:
                current += ch
        if current or args:
            args.append(current)
        return args

    def _tokenize(self):
        for raw_line in self.lines:
            stripped = raw_line.strip()

            if not stripped:
                self.line += 1
                continue

            # Comment
            if stripped.startswith("#"):
                self.tokens.append(
                    Token(_TokenType.COMMENT, stripped[1:].strip(), self.line)
                )

            # get
            elif stripped.startswith("get "):
                var_name = stripped[4:].strip()
                val = os.environ.get(var_name)
                missing = val is None
                if val is None:
                    syst = __import__("platform").system()
                    if var_name == "OS":
                        if syst == "Linux":
                            val = "linux"
                        elif syst == "Darwin":
                            val = "darwin"
                        else:
                            print(
                                f"WARNING: Environmental variable [{var_name}] is not on the runner PC"
                            )
                            val = "NULL"
                    else:
                        print(
                            f"WARNING: Environmental variable [{var_name}] is not on the runner PC"
                        )
                        val = "NULL"
                self.tokens.append(Token(_TokenType.GET, var_name, self.line))
                self.tokens.append(
                    Token(
                        _TokenType.NULL
                        if missing and val == "NULL"
                        else _TokenType.IDENTIFIER,
                        val,
                        self.line,
                    )
                )

            # fn name(args) {
            elif stripped.startswith("fn ") and stripped.endswith("{"):
                header = stripped[3:-1].strip()
                name, _, argstr = header.partition("(")
                argstr = argstr.rstrip(")").strip()
                args = [a.strip() for a in argstr.split(",") if a.strip()]
                self.tokens.append(Token(_TokenType.FN_DEF, name.strip(), self.line))
                for a in args:
                    self.tokens.append(Token(_TokenType.IDENTIFIER, a, self.line))
                self.tokens.append(Token(_TokenType.LBRACE, "{", self.line))

            # closing brace for fn body
            elif stripped == "}":
                self.tokens.append(Token(_TokenType.RBRACE, "}", self.line))

            # return <expr>
            elif stripped.startswith("return "):
                expr = stripped[7:].strip()
                self.tokens.append(Token(_TokenType.RETURN, "", self.line))
                for kind, val in self._parse_var_refs(expr):
                    self.tokens.append(Token(_TokenType[kind], val, self.line))

            # module Name
            elif stripped.startswith("module "):
                self.tokens.append(
                    Token(_TokenType.MODULE, stripped[7:].strip(), self.line)
                )

            # let  (supports  let x = fn_call(a, b)  as well as plain strings)
            elif stripped.startswith("let "):
                rest = stripped[4:]
                name, _, value = rest.partition("=")
                value = value.strip()
                name = name.strip()
                soft = name.endswith("?")  # `let NAME ?= value`: only if not set yet
                if soft:
                    name = name[:-1].strip()
                self.tokens.append(Token(_TokenType.LET, name, self.line))
                self.tokens.append(
                    Token(_TokenType.ASSIGN, "?=" if soft else "=", self.line)
                )
                if self._is_call_like(value):
                    cname, cargs = self._split_call(value)
                    self.tokens.append(Token(_TokenType.CALL, cname, self.line))
                    if cargs.strip():
                        for a in self._split_args(cargs):
                            for kind, val in self._parse_var_refs(a.strip()):
                                self.tokens.append(
                                    Token(_TokenType[kind], val, self.line)
                                )
                            self.tokens.append(
                                Token(_TokenType.COMMENT, "\x00ARGSEP\x00", self.line)
                            )
                else:
                    for kind, val in self._parse_var_refs(value):
                        self.tokens.append(Token(_TokenType[kind], val, self.line))
            # if
            elif stripped.startswith("if ") and stripped.endswith("then"):
                condition = stripped[3:-4].strip()
                left, op, right = self._parse_condition(condition)
                self.tokens.append(Token(_TokenType.IF, left, self.line))
                if op:
                    self.tokens.append(Token(_TokenType.OPERATOR, op, self.line))
                    self.tokens.append(Token(_TokenType.IDENTIFIER, right, self.line))
                self.tokens.append(Token(_TokenType.THEN, "then", self.line))

            # elif
            elif stripped.startswith("elif ") and stripped.endswith("then"):
                condition = stripped[5:-4].strip()
                left, op, right = self._parse_condition(condition)
                self.tokens.append(Token(_TokenType.ELIF, left, self.line))
                if op:
                    self.tokens.append(Token(_TokenType.OPERATOR, op, self.line))
                    self.tokens.append(Token(_TokenType.IDENTIFIER, right, self.line))
                self.tokens.append(Token(_TokenType.THEN, "then", self.line))

            # else
            elif stripped.startswith("else"):
                self.tokens.append(Token(_TokenType.ELSE, "else", self.line))
                self.tokens.append(Token(_TokenType.THEN, "then", self.line))

            # try ... catch|on_fail ... done
            elif stripped == "try":
                self.tokens.append(Token(_TokenType.TRY, "try", self.line))

            elif stripped in ("catch", "on_fail"):
                self.tokens.append(Token(_TokenType.CATCH, "catch", self.line))

            # retry N [every SECONDS] do ... done
            elif re.fullmatch(r"retry\s+\S+(\s+every\s+\S+)?\s+do", stripped):
                m = re.fullmatch(r"retry\s+(\S+)(?:\s+every\s+(\S+))?\s+do", stripped)
                self.tokens.append(Token(_TokenType.RETRY, m.group(1), self.line))
                self.tokens.append(
                    Token(_TokenType.IDENTIFIER, m.group(2) or "", self.line)
                )
                self.tokens.append(Token(_TokenType.DO, "do", self.line))

            # timeout SECONDS do ... done
            elif re.fullmatch(r"timeout\s+\S+\s+do", stripped):
                m = re.fullmatch(r"timeout\s+(\S+)\s+do", stripped)
                self.tokens.append(Token(_TokenType.TIMEOUT, m.group(1), self.line))
                self.tokens.append(Token(_TokenType.DO, "do", self.line))

            # while <condition> do
            elif stripped.startswith("while ") and stripped.endswith("do"):
                left, op, right = self._parse_condition(stripped[6:-2].strip())
                self.tokens.append(Token(_TokenType.WHILE, left, self.line))
                if op:
                    self.tokens.append(Token(_TokenType.OPERATOR, op, self.line))
                    self.tokens.append(Token(_TokenType.IDENTIFIER, right, self.line))
                self.tokens.append(Token(_TokenType.DO, "do", self.line))

            elif stripped == "break":
                self.tokens.append(Token(_TokenType.BREAK, "break", self.line))

            elif stripped == "continue":
                self.tokens.append(Token(_TokenType.CONTINUE, "continue", self.line))

            # done  (closes if / for / parallel / recipe-less blocks / module)
            elif stripped == "done":
                self.tokens.append(Token(_TokenType.DONE, "done", self.line))

            # parallel do
            elif stripped == "parallel do":
                self.tokens.append(Token(_TokenType.PARALLEL, "parallel", self.line))
                self.tokens.append(Token(_TokenType.PARALLEL_DO, "do", self.line))
            # pend

            elif stripped == "pend":
                self.tokens.append(Token(_TokenType.PARALLEL_END, "pend", self.line))

            # for till(n)
            elif stripped.startswith("for till("):
                num = stripped[9 : stripped.index(")")].strip()
                self.tokens.append(Token(_TokenType.FOR, "for", self.line))
                self.tokens.append(Token(_TokenType.FOR_TILL, num, self.line))
                self.tokens.append(Token(_TokenType.DO, "do", self.line))

            # for x in (LIST)
            elif (
                stripped.startswith("for ")
                and " in " in stripped
                and stripped.endswith("do")
            ):
                rest = stripped[4:-2].strip()
                var, _, list_ref = rest.partition(" in ")
                self.tokens.append(Token(_TokenType.FOR, "for", self.line))
                self.tokens.append(Token(_TokenType.IDENTIFIER, var.strip(), self.line))
                self.tokens.append(
                    Token(_TokenType.FOR_IN, _for_list_ref(list_ref), self.line)
                )
                self.tokens.append(Token(_TokenType.DO, "do", self.line))

            # shell
            elif stripped.startswith("shell "):
                cmd = stripped[6:].strip()
                self.tokens.append(Token(_TokenType.SHELL, "", self.line))
                for kind, val in self._parse_var_refs(cmd):
                    self.tokens.append(Token(_TokenType[kind], val, self.line))

            # recipe declaration
            elif stripped.startswith("recipe "):
                self.tokens.append(
                    Token(_TokenType.RECIPE, stripped[7:].strip(), self.line)
                )

            # bare call statement:  print(x)   fail(msg)   writefile((h), text)
            elif self._is_call_like(stripped):
                cname, cargs = self._split_call(stripped)
                self.tokens.append(Token(_TokenType.CALL, cname, self.line))
                if cargs.strip():
                    for a in self._split_args(cargs):
                        for kind, val in self._parse_var_refs(a.strip()):
                            self.tokens.append(Token(_TokenType[kind], val, self.line))
                        self.tokens.append(
                            Token(_TokenType.COMMENT, "\x00ARGSEP\x00", self.line)
                        )

            # recipe definition (name: dep1 dep2), also used (indented)
            # for a recipe-def inside `module X ... done`.
            #
            # This language doesn't use indentation to distinguish a
            # recipe-def from a plain command line (a module's own
            # recipe-defs ARE indented, same as any command), so the only
            # reliable signal is shape: the text before the colon must
            # look like a bare recipe name (letters/digits/underscore/dot
            # for dotted module-qualified names), not arbitrary text with
            # spaces - which is what guards against a false match on
            # something like `echo done: all passed`.
            elif (
                ":" in stripped
                and stripped.split(":", 1)[0].strip()
                and stripped.split(":", 1)[0]
                .strip()
                .replace(".", "")
                .replace("_", "")
                .isalnum()
            ):
                name, _, deps = stripped.partition(":")
                self.tokens.append(
                    Token(_TokenType.RECIPE_DEF, name.strip(), self.line)
                )
                self.tokens.append(Token(_TokenType.COLON, ":", self.line))
                if deps.strip():
                    self.tokens.append(
                        Token(_TokenType.DEPENDS, deps.strip(), self.line)
                    )

            # recipe body / shell command line (indented)
            else:
                self.tokens.append(Token(_TokenType.STRING, "", self.line))
                for kind, val in self._parse_var_refs(stripped):
                    self.tokens.append(Token(_TokenType[kind], val, self.line))

            self.tokens.append(Token(_TokenType.NEWLINE, "\n", self.line))
            self.line += 1

        self.tokens.append(Token(_TokenType.EOF, "", self.line))

    def GetTokens(self) -> list:
        return self.tokens


class ASTNode:
    def __init__(self, type: str, **kwargs):
        self.type = type
        self.data = kwargs

    def __repr__(self):
        return f"ASTNode({self.type}, {self.data})"


class Parser:
    """Parses a flat token list from Tokenize into an AST ready for execution."""

    def __init__(self, tokens: list):
        self.tokens = tokens
        self.pos = 0
        self.ast = []
        self._parse()

    def _current(self) -> Token:
        return self.tokens[self.pos]

    def _advance(self) -> Token:
        t = self.tokens[self.pos]
        self.pos += 1
        return t

    def _skip_newlines(self):
        while self._current().type == _TokenType.NEWLINE:
            self._advance()

    def _expect(self, type: _TokenType) -> Token:
        t = self._current()
        if t.type != type:
            raise SyntaxError(
                f"[AutoBuild] Line {t.line}: Expected {type.name}, got {t.type.name} ({repr(t.value)})"
            )
        return self._advance()

    def _parse_string_sequence(self) -> str:
        """Reads STRING and VAR_REF tokens into a single resolved string fragment list."""
        parts = []
        while self._current().type in (_TokenType.STRING, _TokenType.VAR_REF):
            t = self._advance()
            parts.append((t.type.name, t.value))
        return parts

    def _parse_call_args(self) -> list:
        """Reads a CALL's argument list: groups of STRING/VAR_REF parts
        separated by an ARGSEP marker (a COMMENT token carrying \x00ARGSEP\x00),
        as emitted by the tokenizer for `let x = fn(a, b)`."""
        args = []
        current = []
        while self._current().type in (
            _TokenType.STRING,
            _TokenType.VAR_REF,
            _TokenType.COMMENT,
        ):
            t = self._advance()
            if t.type == _TokenType.COMMENT and t.value == "\x00ARGSEP\x00":
                args.append(current)
                current = []
                continue
            current.append((t.type.name, t.value))
        if current:
            args.append(current)
        return args

    def _parse_body(self, inside_block=False) -> list:
        """Reads indented body lines until DONE/ELIF/ELSE/RBRACE (inside block)
        or RECIPE_DEF/RECIPE/MODULE/EOF (top level).

        Note: FN_DEF is intentionally NOT a stop condition even when
        inside_block is False. A recipe body (which is itself parsed with
        inside_block=False, since recipes are top-level declarations, not
        `if`/`for`/`{...}` blocks) can legitimately contain a local `fn`
        definition - e.g. a helper only that recipe uses. Only the start
        of the NEXT top-level declaration (another recipe, or a module)
        should end the current recipe's body.
        """
        body = []
        self._skip_newlines()
        while True:
            t = self._current()
            if t.type == _TokenType.EOF:
                break
            if t.type in (
                _TokenType.DONE,
                _TokenType.ELIF,
                _TokenType.ELSE,
                _TokenType.PARALLEL_END,
                _TokenType.RBRACE,
                _TokenType.CATCH,
            ):
                break
            if not inside_block and t.type in (
                _TokenType.RECIPE_DEF,
                _TokenType.RECIPE,
                _TokenType.MODULE,
            ):
                break
            node = self._parse_statement()
            if node:
                body.append(node)
            self._skip_newlines()
        return body

    def _parse_if(self) -> ASTNode:
        branches = []

        left = self._advance().value
        op = right = None
        if self._current().type == _TokenType.OPERATOR:
            op = self._advance().value
            right = self._expect(_TokenType.IDENTIFIER).value
        self._expect(_TokenType.THEN)
        self._skip_newlines()
        body = self._parse_body(inside_block=True)
        branches.append({"condition": (left, op, right), "body": body})

        while self._current().type == _TokenType.ELIF:
            left = self._advance().value
            op = right = None
            if self._current().type == _TokenType.OPERATOR:
                op = self._advance().value
                right = self._expect(_TokenType.IDENTIFIER).value
            self._expect(_TokenType.THEN)
            self._skip_newlines()
            body = self._parse_body(inside_block=True)
            branches.append({"condition": (left, op, right), "body": body})

        if self._current().type == _TokenType.ELSE:
            self._advance()  # ELSE
            self._advance()  # THEN
            self._skip_newlines()
            body = self._parse_body(inside_block=True)
            branches.append({"condition": None, "body": body})

        self._expect(_TokenType.DONE)
        return ASTNode("if", branches=branches)

    def _parse_try(self) -> ASTNode:
        self._skip_newlines()
        body = self._parse_body(inside_block=True)
        catch = None
        if self._current().type == _TokenType.CATCH:
            self._advance()
            self._skip_newlines()
            catch = self._parse_body(inside_block=True)
        self._expect(_TokenType.DONE)
        return ASTNode("try", body=body, catch=catch)

    def _parse_retry(self) -> ASTNode:
        count = self._advance().value
        every = self._expect(_TokenType.IDENTIFIER).value
        self._expect(_TokenType.DO)
        self._skip_newlines()
        body = self._parse_body(inside_block=True)
        self._expect(_TokenType.DONE)
        return ASTNode("retry", count=count, every=every, body=body)

    def _parse_timeout(self) -> ASTNode:
        seconds = self._advance().value
        self._expect(_TokenType.DO)
        self._skip_newlines()
        body = self._parse_body(inside_block=True)
        self._expect(_TokenType.DONE)
        return ASTNode("timeout", seconds=seconds, body=body)

    def _parse_while(self) -> ASTNode:
        left = self._advance().value
        op = right = None
        if self._current().type == _TokenType.OPERATOR:
            op = self._advance().value
            right = self._expect(_TokenType.IDENTIFIER).value
        self._expect(_TokenType.DO)
        self._skip_newlines()
        body = self._parse_body(inside_block=True)
        self._expect(_TokenType.DONE)
        return ASTNode("while", condition=(left, op, right), body=body)

    def _parse_for_till(self) -> ASTNode:
        count = self._expect(_TokenType.FOR_TILL).value
        self._expect(_TokenType.DO)
        self._skip_newlines()
        body = self._parse_body(inside_block=True)
        self._expect(_TokenType.DONE)
        return ASTNode("for_till", count=int(count), body=body)

    def _parse_for_in(self) -> ASTNode:
        var = self._expect(_TokenType.IDENTIFIER).value
        list_ref = self._expect(_TokenType.FOR_IN).value
        self._expect(_TokenType.DO)
        self._skip_newlines()
        body = self._parse_body(inside_block=True)
        self._expect(_TokenType.DONE)
        return ASTNode("for_in", var=var, list_ref=list_ref, body=body)

    def _parse_recipe_def(self) -> ASTNode:
        name = self._advance().value
        self._expect(_TokenType.COLON)
        deps = []
        if self._current().type == _TokenType.DEPENDS:
            deps = self._advance().value.split()
        self._skip_newlines()
        body = self._parse_body(inside_block=False)
        return ASTNode("recipe_def", name=name, deps=deps, body=body)

    def _parse_parallel(self) -> ASTNode:
        self._expect(_TokenType.PARALLEL_DO)
        self._skip_newlines()
        body = self._parse_body(inside_block=True)
        self._expect(_TokenType.PARALLEL_END)
        return ASTNode("parallel", body=body)

    def _parse_fn_def(self) -> ASTNode:
        name = self._advance().value
        params = []
        while self._current().type == _TokenType.IDENTIFIER:
            params.append(self._advance().value)
        self._expect(_TokenType.LBRACE)
        self._skip_newlines()
        body = []
        return_expr = None
        self._skip_newlines()
        while True:
            t = self._current()
            if t.type == _TokenType.RBRACE:
                break
            if t.type == _TokenType.RETURN:
                self._advance()
                return_expr = self._parse_string_sequence()
                self._skip_newlines()
                continue
            node = self._parse_statement()
            if node:
                body.append(node)
            self._skip_newlines()
        self._expect(_TokenType.RBRACE)
        return ASTNode("fn_def", name=name, params=params, body=body, ret=return_expr)

    def _parse_module(self) -> ASTNode:
        name = self._advance().value
        self._skip_newlines()
        body = self._parse_body(inside_block=True)
        self._expect(_TokenType.DONE)
        return ASTNode("module", name=name, body=body)

    def _parse_statement(self) -> ASTNode:
        self._skip_newlines()
        t = self._current()

        if t.type == _TokenType.COMMENT:
            self._advance()
            return None

        elif t.type == _TokenType.GET:
            name = self._advance().value
            val_tok = self._advance()
            return ASTNode(
                "get",
                name=name,
                value=val_tok.value,
                is_null=val_tok.type == _TokenType.NULL,
            )

        elif t.type == _TokenType.LET:
            name = self._advance().value
            soft = self._expect(_TokenType.ASSIGN).value == "?="
            if self._current().type == _TokenType.CALL:
                call_name = self._advance().value
                args = self._parse_call_args()
                return ASTNode(
                    "let_call", name=name, call=call_name, args=args, soft=soft
                )
            parts = self._parse_string_sequence()
            return ASTNode("let", name=name, parts=parts, soft=soft)

        elif t.type == _TokenType.IF:
            return self._parse_if()

        elif t.type == _TokenType.FOR:
            self._advance()  # consume FOR
            if self._current().type == _TokenType.FOR_TILL:
                return self._parse_for_till()
            else:
                return self._parse_for_in()

        elif t.type == _TokenType.SHELL:
            self._advance()
            parts = self._parse_string_sequence()
            return ASTNode("shell", parts=parts)

        elif t.type == _TokenType.RECIPE:
            name = self._advance().value
            return ASTNode("recipe_decl", name=name)

        elif t.type == _TokenType.RECIPE_DEF:
            return self._parse_recipe_def()

        elif t.type == _TokenType.FN_DEF:
            return self._parse_fn_def()

        elif t.type == _TokenType.MODULE:
            return self._parse_module()

        elif t.type == _TokenType.TRY:
            self._advance()
            return self._parse_try()

        elif t.type == _TokenType.RETRY:
            return self._parse_retry()

        elif t.type == _TokenType.TIMEOUT:
            return self._parse_timeout()

        elif t.type == _TokenType.WHILE:
            return self._parse_while()

        elif t.type == _TokenType.BREAK:
            self._advance()
            return ASTNode("break")

        elif t.type == _TokenType.CONTINUE:
            self._advance()
            return ASTNode("continue")

        elif t.type == _TokenType.CALL:
            call_name = self._advance().value
            args = self._parse_call_args()
            return ASTNode("call", call=call_name, args=args)

        elif t.type == _TokenType.STRING:
            parts = self._parse_string_sequence()
            return ASTNode("command", parts=parts)
        elif t.type == _TokenType.PARALLEL:
            self._advance()
            return self._parse_parallel()
        else:
            self._advance()
            return None

    def _parse(self):
        while self._current().type != _TokenType.EOF:
            self._skip_newlines()
            if self._current().type == _TokenType.EOF:
                break
            node = self._parse_statement()
            if node:
                self.ast.append(node)
                if node.type == "recipe_def":
                    # default()/describe() only make sense at top level; a recipe
                    # body runs to the next recipe or EOF, so lift them out
                    # (otherwise a default() at the end of the file would
                    # silently become part of the last recipe).
                    keep, lifted = [], []
                    for child in node.data["body"]:
                        is_meta = (
                            child is not None
                            and child.type == "call"
                            and child.data["call"] in ("default", "describe")
                        )
                        (lifted if is_meta else keep).append(child)
                    node.data["body"] = keep
                    self.ast.extend(lifted)

    def GetAST(self) -> list:
        return self.ast


# ---------------------------------------------------------------------------
# Optimizer: a small, provable pass that runs on the AST before execution.
#
#   1. Constant folding: an `if`/`elif` branch whose condition operands are
#      BOTH literal values (no VAR_REF anywhere in either side, and neither
#      side is itself a variable name that could change at runtime... in
#      this language conditions are already plain left/op/right strings, so
#      "constant" means neither side depends on a variable that is unknown
#      at optimize-time) gets evaluated once, at optimize-time, instead of
#      on every execution.
#   2. Dead-code elimination: once a branch's condition is folded, whichever
#      sibling branches can provably never run (because an earlier branch
#      in the same if-chain always fires) are dropped from the AST.
#
# The pass only folds what it can prove from variables already known at
# optimize-time (populated from `let`/`get` statements seen so far, in
# textual order, at the top level and inside recipe bodies as they are
# visited). It never guesses.
# ---------------------------------------------------------------------------
class Optimizer:
    def __init__(
        self, ast: list, explain: bool = False, cli_vars: "dict | None" = None
    ):
        self.original_ast = ast
        self.explain = explain
        self.cli_vars = dict(cli_vars or {})  # --var NAME=VALUE: fixed for the run
        self.known_vars = dict(self.cli_vars)
        self.stats = {"folded": 0, "eliminated": 0}
        self.log_lines = []
        self.optimized_ast = self._optimize_block(ast)

    def _known_lookup(self, name):
        if name in self.known_vars:
            return self.known_vars[name]
        raise _Unknown(name)

    def _static_operand(self, text: str):
        t = text.strip()
        if t in self.known_vars:
            return self.known_vars[t]
        return _eval_parts(Tokenize._parse_var_refs(t), self._known_lookup)

    def _condition_is_static(self, left: str, op, right) -> bool:
        # `left` must be a variable whose value we already know (the same
        # rule the executor uses: it looks `left` up as a variable name).
        if left not in self.known_vars:
            return False
        if op is None:
            return True
        if Tokenize._is_call_like(right):
            return False
        try:
            self._static_operand(right)
        except _Unknown:
            return False
        return True

    def _eval_condition_static(self, left: str, op, right):
        rv = None if op is None else self._static_operand(right)
        return _compare(self.known_vars[left], op, rv)

    def _forget_loop_effects(self, loop_node):
        names, opaque = _loop_effects([loop_node])
        if opaque:  # a user fn is called: it may assign any global
            self.known_vars.clear()
        else:
            for n in names:
                if n not in self.cli_vars:
                    self.known_vars.pop(n, None)

    def _optimize_block(self, nodes: list) -> list:
        out = []
        for node in nodes:
            new_node = self._optimize_node(node)
            if new_node is not None:
                if isinstance(new_node, list):
                    out.extend(new_node)
                else:
                    out.append(new_node)
        return out

    def _optimize_node(self, node):
        if node is None:
            return None

        if node.type == "get":
            if node.data["name"] not in self.cli_vars:
                self.known_vars[node.data["name"]] = (
                    None if node.data["is_null"] else node.data["value"]
                )
            return node

        if node.type == "let":
            name = node.data["name"]
            if name in self.cli_vars:
                return node  # the command line wins; this let is skipped at runtime
            if node.data.get("soft"):
                return node  # `?=`: skipped when known, otherwise unknown either way
            try:
                self.known_vars[node.data["name"]] = _eval_parts(
                    node.data["parts"], self._known_lookup
                )
            except _Unknown:
                self.known_vars.pop(node.data["name"], None)
            return node

        if node.type in ("let_call", "call"):
            if node.type == "let_call":
                # Result of a function call is not known until runtime.
                nm = node.data["name"]
                locked = nm in self.cli_vars or (
                    node.data.get("soft") and nm in self.known_vars
                )
                if not locked:
                    self.known_vars.pop(nm, None)
            if _has_user_call(node):
                # A user fn's body may assign globals; forget what we knew.
                self.known_vars.clear()
            return node

        if node.type == "if":
            branches = node.data["branches"]
            resolved_index = None
            for i, branch in enumerate(branches):
                cond = branch["condition"]
                if cond is None:
                    resolved_index = i
                    break
                left, op, right = cond
                if self._condition_is_static(left, op, right):
                    result = self._eval_condition_static(left, op, right)
                    if result:
                        resolved_index = i
                        break
                    # this branch is provably dead; continue checking next
                    continue
                else:
                    # Can't prove statically past this point - keep the
                    # remaining if-chain as-is (optimize each body though).
                    new_branches = []
                    for j, b in enumerate(branches):
                        if j < i:
                            continue  # earlier branches already proven dead
                        b_body = self._optimize_block(b["body"])
                        new_branches.append(
                            {"condition": b["condition"], "body": b_body}
                        )
                    if len(new_branches) < len(branches):
                        self.stats["eliminated"] += len(branches) - len(new_branches)
                        self.log_lines.append(
                            f"  - eliminated {len(branches) - len(new_branches)} "
                            f"provably-false branch(es) before an unresolvable condition"
                        )
                    return ASTNode("if", branches=new_branches)

            if resolved_index is not None:
                # Entire if/elif/else chain collapses to one branch's body.
                self.stats["folded"] += 1
                dropped = len(branches) - 1
                if dropped:
                    self.stats["eliminated"] += dropped
                self.log_lines.append(
                    f"  - folded `if` chain ({len(branches)} branch(es)) to a "
                    f"single constant branch, eliminating {dropped} dead branch(es)"
                )
                winning_body = branches[resolved_index]["body"]
                return self._optimize_block(winning_body)

            # All branches provably false and no else - whole if is dead code.
            self.stats["folded"] += 1
            self.stats["eliminated"] += len(branches)
            self.log_lines.append(
                f"  - eliminated entire `if` chain ({len(branches)} branch(es)); "
                f"every condition was provably false at optimize-time"
            )
            return None

        if node.type in ("try", "retry", "timeout"):
            self._forget_loop_effects(node)  # body may assign; value unknown after
            return node

        if node.type in ("for_till", "for_in", "while"):
            # A loop body can run 0..n times and reassign variables, so
            # anything it may assign is NOT a known constant - before the
            # body (2nd iteration sees the new value) or after it.
            self._forget_loop_effects(node)
            node.data["body"] = self._optimize_block(node.data["body"])
            self._forget_loop_effects(node)
            return node

        if node.type == "parallel":
            node.data["body"] = self._optimize_block(node.data["body"])
            return node

        if node.type == "recipe_def":
            saved = dict(self.known_vars)
            node.data["body"] = self._optimize_block(node.data["body"])
            self.known_vars = saved  # recipe-local lets don't leak globally
            return node

        if node.type == "fn_def":
            saved = dict(self.known_vars)
            for p in node.data["params"]:
                self.known_vars.pop(p, None)
            node.data["body"] = self._optimize_block(node.data["body"])
            self.known_vars = saved
            return node

        if node.type == "module":
            node.data["body"] = self._optimize_block(node.data["body"])
            return node

        return node

    def GetAST(self) -> list:
        return self.optimized_ast

    def PrintReport(self):
        if not self.explain:
            return
        print("\033[35m[AutoBuild Optimizer]\033[0m")
        if not self.log_lines:
            print("  - nothing provably foldable; AST unchanged")
        else:
            for line in self.log_lines:
                print(line)
        print(
            f"  = {self.stats['folded']} chain(s) folded, "
            f"{self.stats['eliminated']} dead branch(es) removed\n"
        )


# ---------------------------------------------------------------------------
# Built-in functions, callable from AutoBuild scripts exactly like a
# user-defined `fn`: `let h = openfile(out.txt, w, utf-8)`.
#
# File handles are kept in Executor.open_files, keyed by a small string id
# handed back to the script as the fn's return value - so a script never
# manipulates a raw Python file object, only an opaque handle string it
# passes to writefile/readfile/closefile. This keeps the language's value
# model consistent (everything is a string) while avoiding "complex nested
# solutions" - one line to open, one line to write, one line to close.
#
# Allowed modes are restricted to a safe, explicit allowlist (no arbitrary
# mode strings reaching Python's open()), and encoding defaults to utf-8
# when omitted.
# ---------------------------------------------------------------------------
_ALLOWED_FILE_MODES = {"r", "w", "a", "r+", "w+", "a+"}


def _builtin_openfile(ex: "Executor", args: list) -> str:
    args = [_to_str(a) for a in args]
    if len(args) not in (2, 3):
        raise SystemExit(
            "[AutoBuild] openfile(filename, mode, encoding=utf-8) expects "
            "2 or 3 arguments"
        )
    filename = args[0]
    mode = args[1]
    encoding = args[2] if len(args) == 3 else "utf-8"
    if mode not in _ALLOWED_FILE_MODES:
        raise SystemExit(
            f"[AutoBuild] openfile: mode '{mode}' is not allowed "
            f"(allowed: {', '.join(sorted(_ALLOWED_FILE_MODES))})"
        )
    try:
        fh = (
            io.StringIO()  # dry-run: never touch the disk
            if (ex.dry_run and mode != "r")
            else open(filename, mode, encoding=encoding, newline="")
        )
    except OSError as e:
        raise SystemExit(f"[AutoBuild] openfile: could not open '{filename}': {e}")
    with ex.vars_lock:
        ex._next_handle += 1
        handle = f"fh{ex._next_handle}"
        ex.open_files[handle] = fh
    return handle


def _builtin_writefile(ex: "Executor", args: list) -> str:
    args = [_to_str(a) for a in args]
    if len(args) != 2:
        raise SystemExit("[AutoBuild] writefile(handle, text) expects 2 arguments")
    handle, text = args
    fh = ex.open_files.get(handle)
    if fh is None:
        raise SystemExit(f"[AutoBuild] writefile: unknown or closed handle '{handle}'")
    fh.write(text)
    return ""


def _builtin_readfile(ex: "Executor", args: list) -> str:
    args = [_to_str(a) for a in args]
    if len(args) != 1:
        raise SystemExit("[AutoBuild] readfile(handle) expects 1 argument")
    (handle,) = args
    fh = ex.open_files.get(handle)
    if fh is None:
        raise SystemExit(f"[AutoBuild] readfile: unknown or closed handle '{handle}'")
    return fh.read()


def _builtin_closefile(ex: "Executor", args: list) -> str:
    args = [_to_str(a) for a in args]
    if len(args) != 1:
        raise SystemExit("[AutoBuild] closefile(handle) expects 1 argument")
    (handle,) = args
    fh = ex.open_files.pop(handle, None)
    if fh is None:
        raise SystemExit(
            f"[AutoBuild] closefile: unknown or already-closed handle '{handle}'"
        )
    fh.close()
    return ""


# ---------------------------------------------------------------------------
# Incremental builds: `changed(key, inputs, outputs)`
#
#   let c = changed(app, src/*.c include/*.h, build/app)
#   if c == true then
#       gcc src/*.c -o build/app
#   done
#
# Returns true or false. It hashes the CONTENT of every
# input file (globs, ** and directories are supported; inputs are separated
# by spaces) and compares that to the digest stored under `key` in
# `.abuild.cache`. It answers "true" when:
#   - nothing was recorded for `key` yet (first build),
#   - any input file was added, removed, renamed or its content changed,
#   - any file listed in the optional `outputs` argument is missing,
#   - or the run was started with --force.
# Touching a file without editing it does NOT trigger a rebuild (unlike
# mtime-based tools).
#
# The new digest is only COMMITTED to the cache after the whole recipe
# finishes successfully (Executor.commit_cache). If the build fails halfway,
# the cache keeps the old digest, so the next run rebuilds instead of
# wrongly believing the half-finished build is up to date.
# Limitation: file names containing spaces can't be listed (space-separated).
# ---------------------------------------------------------------------------
CACHE_PATH = ".abuild.cache"


# Directories that never count as build inputs when a directory or a ** glob
# is expanded (compiled caches, VCS data, dependency folders).
_IGNORED_DIRS = {
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    ".abuild.history",
}


def _expand_inputs(spec, quiet=()) -> list:
    """spec: a whitespace-separated string of paths/globs/dirs, OR a list of
    them (list items are literal paths first, so names with spaces work).
    `quiet`: paths that may legitimately not exist yet (dry-run)."""
    files = set()
    patterns = [_to_str(x) for x in spec] if isinstance(spec, list) else spec.split()
    for pattern in patterns:
        matches = (
            [pattern] if os.path.exists(pattern) else glob.glob(pattern, recursive=True)
        )
        if not matches and os.path.normpath(pattern) not in quiet:
            print(f"WARNING: changed(): input '{pattern}' matched nothing")
        in_pattern = set(re.split(r"[\\/]", pattern))
        for m in matches:
            # skip matches inside an ignored dir the pattern didn't name itself
            if (set(re.split(r"[\\/]", m)) & _IGNORED_DIRS) - in_pattern:
                continue
            if os.path.isdir(m):
                for root, dirs, names in os.walk(m):
                    dirs[:] = [d for d in dirs if d not in _IGNORED_DIRS]
                    for n in names:
                        if not n.endswith(".pyc"):
                            files.add(os.path.normpath(os.path.join(root, n)))
            elif os.path.isfile(m):
                files.add(os.path.normpath(m))
    return sorted(files)


def _inputs_digest(spec, salt: str = "", quiet=()):
    files = _expand_inputs(spec, quiet)
    digest = _hash_files(files)
    if salt:
        # salt: extra text that must also match, e.g. compiler flags
        digest = hashlib.sha256((digest + "\0" + salt).encode()).hexdigest()
    return files, digest


def _hash_files(files: list) -> str:
    h = hashlib.sha256()
    for path in files:
        fh = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    fh.update(chunk)
        except OSError as e:
            raise SystemExit(f"[AutoBuild] changed: could not read '{path}': {e}")
        h.update(path.encode("utf-8") + b"\0" + fh.hexdigest().encode() + b"\0")
    return h.hexdigest()


def _builtin_changed(ex: "Executor", args: list) -> str:
    raw_inputs = args[1] if len(args) > 1 else ""  # a list stays a list
    args = [_to_str(a) for a in args]
    if len(args) not in (2, 3, 4):
        raise SystemExit(
            "[AutoBuild] changed(key, inputs, outputs=, salt=) expects 2 to 4 arguments"
        )
    key = args[0].strip()
    if not key:
        raise SystemExit("[AutoBuild] changed: key must not be empty")
    outputs = args[2].split() if len(args) >= 3 else []
    spec = raw_inputs if isinstance(raw_inputs, list) else args[1]
    salt = args[3] if len(args) == 4 else ""
    _, digest = _inputs_digest(spec, salt, ex.dry_dirty if ex.dry_run else ())
    ex._pending()[key] = digest
    cache = ex.load_cache()

    reason = None
    if ex.force:
        reason = "forced by --force"
    elif key not in cache:
        reason = "no previous build recorded"
    elif cache[key] != digest:
        reason = "inputs changed"
    else:
        for out in outputs:
            if not os.path.exists(out):
                reason = f"output '{out}' is missing"
                break
    if reason is None and ex.dry_run:
        # dry-run: an input that an earlier (skipped) step would rebuild
        for t in spec if isinstance(spec, list) else spec.split():
            if os.path.normpath(_to_str(t)) in ex.dry_dirty:
                reason = f"input '{t}' would be rebuilt"
                break
    if reason and ex.dry_run:
        ex.dry_dirty.update(os.path.normpath(o) for o in outputs)

    if ex.run_log:
        ex.run_log.record.setdefault("incremental", {})[key] = (
            "rebuild" if reason else "up-to-date"
        )
    if reason:
        print(f"\033[33m[AutoBuild] changed({key}): yes ({reason})\033[0m")
        return True
    print(f"\033[32m[AutoBuild] changed({key}): no (up to date)\033[0m")
    return False


_BUILTIN_FNS = {
    "openfile": _builtin_openfile,
    "writefile": _builtin_writefile,
    "readfile": _builtin_readfile,
    "closefile": _builtin_closefile,
    "changed": _builtin_changed,
}


# ---------------------------------------------------------------------------
# Standard library. Called like any fn:   let n = len((xs))      print(hello)
# A call on its own line (print/fail/writefile/...) is a statement.
# Arguments are typed values; use (name) to pass a variable.
#
#   getargv([i])  getfullargv([i])   script argv / Python's sys.orig_argv
#   incremental(file, keep=3)        snapshot a file when it changes
#   restore(file, n=1)  versions(file)
#   len type int float str bool upper lower trim replace split join append
#   at contains add sub mul div mod glob exists which basename dirname ext
#   capture status lines getenv platform exe  mkdir rm cp mv touch readtext writetext
#   appendtext isdir isfile abspath pathjoin stem  default describe
#   flag option args compdb
#   cwd print fail  get_proc() jobs(n)  depfile(path) stamp(key, inputs)
# ---------------------------------------------------------------------------
def _reg(name):
    def deco(fn):
        _BUILTIN_FNS[name] = fn
        return fn

    return deco


def _need(name: str, args: list, lo: int, hi: "int | None" = None):
    hi = lo if hi is None else hi
    if not (lo <= len(args) <= hi):
        exp = str(lo) if lo == hi else f"{lo}-{hi}"
        raise SystemExit(
            f"[AutoBuild] {name}() expects {exp} argument(s), got {len(args)}"
        )


def _num(v, fname: str):
    if isinstance(v, bool) or v is None or isinstance(v, list):
        raise SystemExit(
            f"[AutoBuild] {fname}(): expected a number, got {_type_name(v)}"
        )
    if isinstance(v, (int, float)):
        return v
    t = str(v).strip()
    try:
        return int(t) if _INT_RE.match(t) else float(t)
    except ValueError:
        raise SystemExit(f"[AutoBuild] {fname}(): '{v}' is not a number")


def _flat(args: list) -> list:
    out = []
    for a in args:
        out.extend(_flat(a) if isinstance(a, list) else [_to_str(a)])
    return out


def _argv_pick(name: str, seq: list, args: list):
    _need(name, args, 0, 1)
    if not args:
        return list(seq)
    idx = int(_num(args[0], name))
    return seq[idx] if -len(seq) <= idx < len(seq) else None


@_reg("getargv")
def _bi_getargv(ex, args):
    return _argv_pick("getargv", sys.argv, args)


@_reg("getfullargv")
def _bi_getfullargv(ex, args):
    return _argv_pick("getfullargv", getattr(sys, "orig_argv", sys.argv), args)


@_reg("print")
def _bi_print(ex, args):
    print(" ".join(_to_str(a) for a in args))
    return None


@_reg("fail")
def _bi_fail(ex, args):
    raise SystemExit("[AutoBuild] " + " ".join(_to_str(a) for a in args))


@_reg("len")
def _bi_len(ex, args):
    _need("len", args, 1)
    v = args[0]
    return 0 if v is None else len(v) if isinstance(v, (list, str)) else len(_to_str(v))


@_reg("type")
def _bi_type(ex, args):
    _need("type", args, 1)
    return _type_name(args[0])


@_reg("int")
def _bi_int(ex, args):
    _need("int", args, 1)
    v = args[0]
    if isinstance(v, (bool, int, float)):
        return int(v)
    try:
        return int(str(v).strip())
    except ValueError:
        try:
            return int(float(str(v).strip()))
        except ValueError:
            raise SystemExit(
                f"[AutoBuild] int(): cannot convert {_type_name(v)} '{_to_str(v)}'"
            )


@_reg("float")
def _bi_float(ex, args):
    _need("float", args, 1)
    v = args[0]
    if isinstance(v, (bool, int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except ValueError:
        raise SystemExit(
            f"[AutoBuild] float(): cannot convert {_type_name(v)} '{_to_str(v)}'"
        )


@_reg("str")
def _bi_str(ex, args):
    _need("str", args, 1)
    return _to_str(args[0])


@_reg("bool")
def _bi_bool(ex, args):
    _need("bool", args, 1)
    return _truthy(args[0])


def _arith(name, op):
    def fn(ex, args):
        _need(name, args, 2)
        return op(_num(args[0], name), _num(args[1], name))

    _BUILTIN_FNS[name] = fn


def _div(a, b):
    if b == 0:
        raise SystemExit("[AutoBuild] div(): division by zero")
    if isinstance(a, int) and isinstance(b, int) and a % b == 0:
        return a // b
    return a / b


def _mod(a, b):
    if b == 0:
        raise SystemExit("[AutoBuild] mod(): modulo by zero")
    return a % b


_arith("add", lambda a, b: a + b)
_arith("sub", lambda a, b: a - b)
_arith("mul", lambda a, b: a * b)
_arith("div", _div)
_arith("mod", _mod)


@_reg("upper")
def _bi_upper(ex, args):
    _need("upper", args, 1)
    return _to_str(args[0]).upper()


@_reg("lower")
def _bi_lower(ex, args):
    _need("lower", args, 1)
    return _to_str(args[0]).lower()


@_reg("trim")
def _bi_trim(ex, args):
    _need("trim", args, 1)
    return _to_str(args[0]).strip()


@_reg("replace")
def _bi_replace(ex, args):
    _need("replace", args, 3)
    return _to_str(args[0]).replace(_to_str(args[1]), _to_str(args[2]))


@_reg("split")
def _bi_split(ex, args):
    _need("split", args, 1, 2)
    text = _to_str(args[0])
    return text.split() if len(args) == 1 else text.split(_to_str(args[1]))


@_reg("join")
def _bi_join(ex, args):
    _need("join", args, 1, 2)
    items = args[0] if isinstance(args[0], list) else [args[0]]
    return (" " if len(args) == 1 else _to_str(args[1])).join(_to_str(i) for i in items)


@_reg("append")
def _bi_append(ex, args):
    _need("append", args, 2, 64)
    base = (
        list(args[0])
        if isinstance(args[0], list)
        else ([] if args[0] is None else [args[0]])
    )
    return base + list(args[1:])


@_reg("at")
def _bi_at(ex, args):
    _need("at", args, 2)
    seq = args[0] if isinstance(args[0], list) else _to_str(args[0]).split()
    i = int(_num(args[1], "at"))
    return seq[i] if -len(seq) <= i < len(seq) else None


@_reg("contains")
def _bi_contains(ex, args):
    _need("contains", args, 2)
    hay, needle = args
    if isinstance(hay, list):
        return any(_compare(i, "==", needle) for i in hay)
    return _to_str(needle) in _to_str(hay)


@_reg("glob")
def _bi_glob(ex, args):
    out = set()
    for pat in _flat(args):
        out.update(os.path.normpath(m) for m in glob.glob(pat, recursive=True))
    return sorted(out)


@_reg("exists")
def _bi_exists(ex, args):
    _need("exists", args, 1)
    return os.path.exists(_to_str(args[0]))


@_reg("which")
def _bi_which(ex, args):
    _need("which", args, 1)
    return shutil.which(_to_str(args[0]))


@_reg("basename")
def _bi_basename(ex, args):
    _need("basename", args, 1)
    return os.path.basename(_to_str(args[0]))


@_reg("dirname")
def _bi_dirname(ex, args):
    _need("dirname", args, 1)
    return os.path.dirname(_to_str(args[0]))


@_reg("ext")
def _bi_ext(ex, args):
    _need("ext", args, 1)
    return os.path.splitext(_to_str(args[0]))[1]


@_reg("cwd")
def _bi_cwd(ex, args):
    _need("cwd", args, 0)
    return os.getcwd()


def _dry(ex, text: str):
    with ex._print_lock:
        ex.dry_cmds += 1
    print(f"\033[33m[dry-run] {text}\033[0m")


def _timeout_arg(args: list, i: int, fname: str):
    if len(args) > i and args[i] is not None:
        return float(_num(args[i], fname))
    return None


@_reg("capture")
def _bi_capture(ex, args):
    """capture(cmd, timeout=): run cmd, return its stdout (trailing newlines
    trimmed). A non-zero exit fails (wrap in try to handle it). Not executed
    in --dry-run (returns an empty string)."""
    _need("capture", args, 1, 2)
    cmd = _to_str(args[0])
    if ex.dry_run:
        _dry(ex, f"(capture) {cmd}")
        return ""
    code, out = ex._exec(cmd, capture=True, timeout=_timeout_arg(args, 1, "capture"))
    if code != 0:
        raise SystemExit(f"[AutoBuild] Command failed with exit code {code}:\n\t {cmd}")
    return (out or "").rstrip("\r\n")


@_reg("status")
def _bi_status(ex, args):
    """status(cmd, timeout=): run cmd (output streams as usual) and return
    its exit code as an int instead of failing the build."""
    _need("status", args, 1, 2)
    cmd = _to_str(args[0])
    if ex.dry_run:
        _dry(ex, f"(status) {cmd}")
        return 0
    print(f"\n\033[36m{cmd}\n\033[0m")
    code, _ = ex._exec(cmd, timeout=_timeout_arg(args, 1, "status"))
    return code


@_reg("lines")
def _bi_lines(ex, args):
    _need("lines", args, 1)
    return _to_str(args[0]).splitlines()


@_reg("getenv")
def _bi_getenv(ex, args):
    _need("getenv", args, 1, 2)
    return os.environ.get(_to_str(args[0]), args[1] if len(args) == 2 else None)


@_reg("platform")
def _bi_platform(ex, args):
    _need("platform", args, 0)
    return {"win32": "windows", "cygwin": "windows"}.get(
        sys.platform,
        "darwin"
        if sys.platform == "darwin"
        else "linux"
        if sys.platform.startswith("linux")
        else sys.platform,
    )


@_reg("exe")
def _bi_exe(ex, args):
    """exe(name): name + '.exe' on Windows, unchanged elsewhere."""
    _need("exe", args, 1)
    n = _to_str(args[0])
    return n + ".exe" if os.name == "nt" and not n.lower().endswith(".exe") else n


# ---- portable file operations (same behaviour on Linux/macOS/Windows) ----
def _expand_paths(args: list) -> list:
    out = []
    for a in _flat(args):
        if re.search(r"[*?\[]", a) and not os.path.exists(a):
            out.extend(sorted(glob.glob(a, recursive=True)))
        else:
            out.append(a)
    return out


def _rm_guard(path: str):
    real = os.path.abspath(path) if os.path.islink(path) else os.path.realpath(path)
    cwd = os.path.realpath(os.getcwd())
    home = os.path.realpath(os.path.expanduser("~"))
    if (
        os.path.dirname(real) == real  # a filesystem root
        or real in (cwd, home)
        or cwd.startswith(real.rstrip(os.sep) + os.sep)  # an ancestor of cwd
    ):
        raise SystemExit(f"[AutoBuild] rm: refusing to remove '{path}'")


@_reg("mkdir")
def _bi_mkdir(ex, args):
    """mkdir(path, ...): create directories (and parents); fine if they exist."""
    for pth in _flat(args):
        if ex.dry_run:
            _dry(ex, f"mkdir {pth}")
        else:
            os.makedirs(pth, exist_ok=True)
    return None


@_reg("rm")
def _bi_rm(ex, args):
    """rm(path, ...): remove files or directories (recursively); globs work;
    missing paths are ignored. Refuses to remove / , ~ , the project root or
    any parent of it."""
    for pth in _expand_paths(args):
        _rm_guard(pth)
        if ex.dry_run:
            _dry(ex, f"rm {pth}")
        elif os.path.isdir(pth) and not os.path.islink(pth):
            shutil.rmtree(pth)
        elif os.path.lexists(pth):
            os.remove(pth)
    return None


def _copy_move(ex, args, fname, op):
    _need(fname, args, 2, 256)
    *srcs_raw, dst = _flat(args)
    srcs = _expand_paths(srcs_raw)
    if not srcs:
        raise SystemExit(f"[AutoBuild] {fname}: nothing matched {srcs_raw}")
    into_dir = os.path.isdir(dst) or len(srcs) > 1
    for src in srcs:
        target = (
            os.path.join(dst, os.path.basename(os.path.normpath(src)))
            if into_dir
            else dst
        )
        if ex.dry_run:
            _dry(ex, f"{fname} {src} {target}")
            continue
        if into_dir:
            os.makedirs(dst, exist_ok=True)
        if not os.path.exists(src):
            raise SystemExit(f"[AutoBuild] {fname}: '{src}' does not exist")
        if os.path.isdir(src):
            if fname == "cp":
                shutil.copytree(src, target, dirs_exist_ok=True)
            else:
                shutil.move(src, target)
        else:
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            (shutil.copy2 if fname == "cp" else shutil.move)(src, target)
    return None


@_reg("cp")
def _bi_cp(ex, args):
    """cp(src, ..., dst): copy files/dirs; into dst when dst is a directory
    or several sources are given."""
    return _copy_move(ex, args, "cp", shutil.copy2)


@_reg("mv")
def _bi_mv(ex, args):
    """mv(src, ..., dst): move/rename; same rules as cp."""
    return _copy_move(ex, args, "mv", shutil.move)


@_reg("touch")
def _bi_touch(ex, args):
    for pth in _expand_paths(args):
        if ex.dry_run:
            _dry(ex, f"touch {pth}")
            continue
        parent = os.path.dirname(pth)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(pth, "a"):
            os.utime(pth, None)
    return None


@_reg("readtext")
def _bi_readtext(ex, args):
    _need("readtext", args, 1)
    try:
        with open(_to_str(args[0]), encoding="utf-8", newline="") as f:
            return f.read()
    except OSError as e:
        raise SystemExit(f"[AutoBuild] readtext: {e}")


def _text_of(v) -> str:
    return (
        "\n".join(_to_str(i) for i in v) + "\n" if isinstance(v, list) else _to_str(v)
    )


def _write_text(ex, args, fname, mode):
    _need(fname, args, 2)
    path = _to_str(args[0])
    if ex.dry_run:
        _dry(ex, f"{fname} {path}")
        return None
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, mode, encoding="utf-8", newline="") as f:
        f.write(_text_of(args[1]))
    return None


@_reg("writetext")
def _bi_writetext(ex, args):
    """writetext(path, text): replace a file's contents (a list is written
    one item per line)."""
    return _write_text(ex, args, "writetext", "w")


@_reg("appendtext")
def _bi_appendtext(ex, args):
    return _write_text(ex, args, "appendtext", "a")


@_reg("isdir")
def _bi_isdir(ex, args):
    _need("isdir", args, 1)
    return os.path.isdir(_to_str(args[0]))


@_reg("isfile")
def _bi_isfile(ex, args):
    _need("isfile", args, 1)
    return os.path.isfile(_to_str(args[0]))


@_reg("abspath")
def _bi_abspath(ex, args):
    _need("abspath", args, 1)
    return os.path.abspath(_to_str(args[0]))


@_reg("pathjoin")
def _bi_pathjoin(ex, args):
    _need("pathjoin", args, 1, 64)
    return os.path.join(*[_to_str(a) for a in args])


@_reg("stem")
def _bi_stem(ex, args):
    _need("stem", args, 1)
    return os.path.splitext(os.path.basename(_to_str(args[0])))[0]


# ---- default recipe + descriptions ----
@_reg("default")
def _bi_default(ex, args):
    """default(NAME): the recipe that runs when you type just `autobuild`.
    The last default() wins. (default(NAME, soft) only applies when nothing
    set one yet - templates use that so your own default() always wins.)"""
    _need("default", args, 1, 2)
    name = _to_str(args[0]).strip()
    if not name:
        raise SystemExit("[AutoBuild] default() needs a recipe name")
    soft = len(args) == 2 and _to_str(args[1]) == "soft"
    if not (soft and ex.default_recipe):
        ex.default_recipe = name
    return None


@_reg("describe")
def _bi_describe(ex, args):
    """describe(RECIPE, text): one-line description shown by --list."""
    _need("describe", args, 2)
    ex.descriptions[_to_str(args[0]).strip()] = _to_str(args[1]).strip()
    return None


@_reg("flag")
def _bi_flag(ex, args):
    """flag(NAME): true when --NAME was given on the command line (before any
    `--`). e.g.  autobuild test --verbose   ->  if flag(verbose) then ..."""
    _need("flag", args, 1)
    return f"--{_to_str(args[0]).lstrip('-')}" in _own_args(sys.argv[1:])


@_reg("option")
def _bi_option(ex, args):
    """option(NAME, default=NULL): the value of --NAME VALUE or --NAME=VALUE
    (typed like a literal), else the default."""
    _need("option", args, 1, 2)
    name = _to_str(args[0]).lstrip("-")
    own = _own_args(sys.argv[1:])
    for i, a in enumerate(own):
        if a == f"--{name}" and i + 1 < len(own):
            nxt = own[i + 1]
            if not nxt.startswith("-") or _INT_RE.match(nxt) or _FLOAT_RE.match(nxt):
                return _coerce_literal(nxt)
        if a.startswith(f"--{name}="):
            return _coerce_literal(a.split("=", 1)[1])
    return args[1] if len(args) == 2 else None


@_reg("args")
def _bi_args(ex, args):
    """args(): everything after a literal `--` as a list, e.g.
    autobuild test -- -k parser --verbose   ->  [-k, parser, --verbose]"""
    _need("args", args, 0)
    return sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []


@_reg("compdb")
def _bi_compdb(ex, args):
    """compdb(path=compile_commands.json): write the compilation database
    that the C/C++ templates collected (for clangd and other IDE tooling)."""
    _need("compdb", args, 0, 1)
    path = _to_str(args[0]) if args else "compile_commands.json"
    if ex.dry_run:
        _dry(ex, f"compdb {path}")
        return None
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_COMPDB, f, indent=2)
    print(f"[AutoBuild] wrote {path} ({len(_COMPDB)} entries)")
    return None


@_reg("depfile")
def _bi_depfile(ex, args):
    """depfile(path): the files listed as prerequisites in a compiler-written
    Makefile-style dependency file (gcc/clang -MMD -MF path). Only files that
    exist are returned; a missing/unreadable depfile gives an empty list."""
    _need("depfile", args, 1)
    try:
        with open(_to_str(args[0]), encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    text = text.replace("\\\r\n", " ").replace("\\\n", " ")
    rule = text.split("\n", 1)[0]  # first rule only (-MP adds phony ones after)
    m = re.search(r":(\s|$)", rule)
    if not m:
        return []
    deps = []
    for tok in re.findall(r"(?:\\ |[^\s])+", rule[m.end() :]):
        path = os.path.normpath(tok.replace("\\ ", " ").replace("$$", "$"))
        if os.path.exists(path) and path not in deps:
            deps.append(path)
    return deps


@_reg("stamp")
def _bi_stamp(ex, args):
    """stamp(key, inputs, salt=): silently re-record the current state of
    `inputs` as 'built' for changed(key, ...). Use it after a build step whose
    real inputs only became known during the step (a fresh depfile)."""
    _need("stamp", args, 2, 3)
    key = _to_str(args[0]).strip()
    spec = args[1] if isinstance(args[1], list) else _to_str(args[1])
    _, digest = _inputs_digest(spec, _to_str(args[2]) if len(args) == 3 else "")
    ex._pending()[key] = digest
    return None


def get_proc_count() -> int:
    """CPUs this process may use (respects affinity/cgroup pinning where the
    OS reports it), at least 1."""
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


@_reg("get_proc")
def _bi_get_proc(ex, args):
    _need("get_proc", args, 0)
    return get_proc_count()


@_reg("jobs")
def _bi_jobs(ex, args):
    """jobs(n): run up to n independent recipes at once (0 = one per CPU).
    jobs() returns the current setting. `--jobs`/`-j` on the command line
    wins over a script's jobs(n). Call it at top level, before recipes run."""
    _need("jobs", args, 0, 1)
    if not args:
        return ex.jobs
    n = int(_num(args[0], "jobs"))
    n = get_proc_count() if n <= 0 else min(n, 512)
    if ex.jobs_cli is not None:
        return ex.jobs  # the command line wins
    if ex._sched_started:
        print(
            "WARNING: jobs() has no effect once recipes are running; call it at top level"
        )
        return ex.jobs
    ex.jobs = n
    return n


# ---- incremental(file, keep=3): versioned snapshots of a file ----
# Each time the file's CONTENT differs from its newest snapshot, a copy is
# stored in .abuild.history/<file>/. `keep` is how many OLD versions are
# retained besides the current one (default 3). Returns true when a new
# snapshot was taken (first sight or content changed), else false.
HISTORY_DIR = ".abuild.history"


def _snap_dir(path: str) -> str:
    safe = os.path.normpath(path).replace(os.sep, "__").replace(":", "_")
    return os.path.join(HISTORY_DIR, safe)


def _snaps(d: str) -> list:
    try:
        return sorted(n for n in os.listdir(d) if n.endswith(".snap"))
    except FileNotFoundError:
        return []


def _content_digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _take_snapshot(path: str) -> bool:
    if not os.path.isfile(path):
        raise SystemExit(f"[AutoBuild] incremental: '{path}' is not a file")
    digest = _content_digest(path)[:16]
    d = _snap_dir(path)
    os.makedirs(d, exist_ok=True)
    snaps = _snaps(d)
    if snaps and snaps[-1].rsplit("-", 1)[1] == digest + ".snap":
        return False
    shutil.copyfile(path, os.path.join(d, f"{time.time_ns():020d}-{digest}.snap"))
    return True


@_reg("incremental")
def _bi_incremental(ex, args):
    _need("incremental", args, 1, 2)
    path = _to_str(args[0])
    keep = 3 if len(args) == 1 else int(_num(args[1], "incremental"))
    if keep < 0:
        raise SystemExit("[AutoBuild] incremental: keep must be >= 0")
    if not os.path.exists(path):
        # e.g. the first build, before the binary has ever been produced:
        # nothing to snapshot yet - not an error.
        print(
            f"[AutoBuild] incremental: '{path}' does not exist yet - nothing to snapshot"
        )
        return False
    if ex.dry_run:
        if not os.path.isfile(path):
            raise SystemExit(f"[AutoBuild] incremental: '{path}' is not a file")
        digest = _content_digest(path)[:16]
        snaps = _snaps(_snap_dir(path))
        return not (snaps and snaps[-1].rsplit("-", 1)[1] == digest + ".snap")
    changed = _take_snapshot(path)
    d = _snap_dir(path)
    snaps = _snaps(d)
    for old in snaps[: max(0, len(snaps) - (keep + 1))]:
        os.remove(os.path.join(d, old))
    if ex.run_log:
        ex.run_log.record.setdefault("incremental", {})[f"file:{path}"] = (
            "snapshot" if changed else "unchanged"
        )
    return changed


@_reg("versions")
def _bi_versions(ex, args):
    _need("versions", args, 1)
    out = []
    for n in _snaps(_snap_dir(_to_str(args[0]))):
        ts = datetime.datetime.fromtimestamp(int(n.split("-")[0]) / 1e9)
        out.append(ts.isoformat(timespec="seconds"))
    return out


@_reg("restore")
def _bi_restore(ex, args):
    """restore(file, n=1): put back the version n snapshots before the
    current one. The current content is snapshotted first, so a restore
    can itself be undone."""
    _need("restore", args, 1, 2)
    path = _to_str(args[0])
    n = 1 if len(args) == 1 else int(_num(args[1], "restore"))
    if n < 1:
        raise SystemExit("[AutoBuild] restore: n must be >= 1")
    if ex.dry_run:
        print(f"[dry-run] would restore {path} to the version {n} snapshot(s) back")
        return True
    exists = os.path.isfile(path)
    if exists:
        _take_snapshot(path)
    d = _snap_dir(path)
    snaps = _snaps(d)
    idx = -1 - n if exists else -n
    if -len(snaps) > idx:
        raise SystemExit(
            f"[AutoBuild] restore: only {max(0, len(snaps) - (1 if exists else 0))} "
            f"older version(s) stored for '{path}'"
        )
    shutil.copyfile(os.path.join(d, snaps[idx]), path)
    return True


# ---------------------------------------------------------------------------
# Run log: a persistent history of every AutoBuild run.
#
# Each run appends ONE JSON line to `.abuild.log` (override with
# --log-file PATH) recording the recipe, outcome, duration, every command
# that ran (with exit code and timing) and the error if it failed.
# `autobuild --history [N]` prints the last N runs.
# ---------------------------------------------------------------------------
DEFAULT_LOG_PATH = ".abuild.log"


class RunLog:
    """Collects what happened during one run and appends it as a single
    JSON line to a log file when the run ends."""

    def __init__(self, path: str = DEFAULT_LOG_PATH):
        self.path = path
        self._t0 = time.perf_counter()
        self._finished = False
        self.record = {
            "started": datetime.datetime.now().isoformat(timespec="seconds"),
            "recipe": None,
            "status": "unknown",  # success | failed | error | interrupted
            "duration_s": 0.0,
            "commands": [],
            "error": None,
        }

    def command(
        self, cmd: str, returncode: int, elapsed: float, parallel: bool = False
    ):
        self.record["commands"].append(
            {
                "cmd": cmd,
                "exit": returncode,
                "seconds": round(elapsed, 3),
                "parallel": parallel,
            }
        )

    def finish(self, status: str, error: "str | None" = None):
        if self._finished:  # only ever write one record per run
            return
        self._finished = True
        if getattr(self, "disabled", False):  # --dry-run leaves no history
            return
        self.record["status"] = status
        self.record["error"] = error
        self.record["duration_s"] = round(time.perf_counter() - self._t0, 3)
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(self.record) + "\n")
        except OSError as e:
            print(f"WARNING: could not write run log '{self.path}': {e}")


def print_history(path: str, limit: int = 10):
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
    except FileNotFoundError:
        print(f"No run history yet ({path} does not exist).")
        return
    colors = {"success": "\033[32m", "failed": "\033[31m", "error": "\033[31m"}
    for line in lines:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        c = colors.get(r.get("status"), "\033[33m")
        print(
            f"{r.get('started', '?')}  {c}{r.get('status', '?'):<11}\033[0m "
            f"{r.get('recipe') or '-':<20} {r.get('duration_s', 0)}s  "
            f"({len(r.get('commands', []))} cmd)"
        )
        if r.get("error"):
            print(f"    \033[31m{r['error'].strip().splitlines()[0]}\033[0m")


def _run_command_in_process(cmd: str) -> "tuple[str, int]":
    """Runs one already-resolved shell command string in its own OS process.

    This function is the ProcessPoolExecutor worker target, so it MUST stay
    a plain top-level function that only takes/returns pickle-safe plain
    data (str, int) - never an Executor, a lock, an AST node, or any live
    object. That boundary is what keeps this "secure": a parallel child can
    only ever receive a command string that the parent has already fully
    resolved (all (VAR_REF)s substituted) - it never sees interpreter
    internals, and it can't reach back into the parent's `vars` dict even
    by accident, because separate processes don't share memory at all.
    subprocess.run with shell=True is used the same way the sequential
    executor already uses it - this function doesn't introduce a new way
    to escape the shell sandboxing question, only a new process to run the
    existing kind of command in.
    """
    result = subprocess.run(cmd, shell=True)
    return cmd, result.returncode


def _has_user_call(n) -> bool:
    """True if a call node (or any call nested in its arguments) targets a
    user-defined fn, whose body may assign globals."""
    if n.type not in ("let_call", "call"):
        return False
    if n.data["call"] not in _BUILTIN_FNS:
        return True
    for a in n.data["args"]:
        text = "".join(v if k == "STRING" else f"({v})" for k, v in a)
        for name in re.findall(r"([A-Za-z_][\w.]*)\(", text):
            if name not in _BUILTIN_FNS:
                return True
    return False


def _loop_effects(nodes: list):
    """(names assigned anywhere inside, whether a user fn is called)."""
    names, opaque = set(), False
    for n in nodes:
        if n is None:
            continue
        t = n.type
        if t in ("let", "let_call", "get"):
            names.add(n.data["name"])
        if _has_user_call(n):
            opaque = True
        if t == "for_in":
            names.add(n.data["var"])
        if t == "try":
            names.add("error")  # set inside the catch body
            subs = [n.data["body"], n.data["catch"] or []]
        elif t == "if":
            subs = [b["body"] for b in n.data["branches"]]
        elif "body" in n.data:
            subs = [n.data["body"]]
        else:
            subs = []
        for sub in subs:
            sn, so = _loop_effects(sub)
            names |= sn
            opaque = opaque or so
    return names, opaque


# ---------------------------------------------------------------------------
# Templates:   template("CXX-Basic-Incremental", std=c++20, out=build/game)
#
# A `template(...)` line anywhere in .abuild (or an included file) is
# replaced, before parsing, by ready-made recipes - so you don't have to
# write build syntax. It's a plain statement: put your own recipes before or
# after it, or use prefix=NAME to keep the template's recipes in a module
# (NAME.build) so names never collide.
#
# Names are forgiving: case, dashes and aliases don't matter, and small
# typos resolve to the closest template (with a notice on stderr).
# Templates that depend on your files (C/C++) look at the file system when
# the template is expanded, which happens on EVERY run, so adding a source
# file just works. See `autobuild --templates`, `--init`, `--expand`.
# ---------------------------------------------------------------------------
_TEMPLATES = {}


def _template(name: str, desc: str, params: dict, aliases=(), default: str = "build"):
    def deco(fn):
        _TEMPLATES[name] = {
            "desc": desc,
            "params": params,
            "aliases": list(aliases),
            "gen": fn,
            "default": default,
        }
        return fn

    return deco


def _norm_tname(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower().replace("+", "p"))


def _resolve_template(given: str) -> str:
    table = {}
    for canon, t in _TEMPLATES.items():
        table[_norm_tname(canon)] = canon
        for a in t["aliases"]:
            table[_norm_tname(a)] = canon
    key = _norm_tname(given)
    if key in table:
        return table[key]
    close = difflib.get_close_matches(key, list(table), n=1, cutoff=0.8)
    if close:
        canon = table[close[0]]
        print(f"[AutoBuild] template '{given}' -> using '{canon}'", file=sys.stderr)
        return canon
    raise SystemExit(
        f"[AutoBuild] Unknown template '{given}'. Available: "
        + ", ".join(sorted(_TEMPLATES))
    )


def _unix(path: str) -> str:
    return os.path.normpath(path).replace(os.sep, "/")


def _q(text: str) -> str:
    return '"' + text.replace('"', "'") + '"'


def _guard_dir(path: str, what: str):
    n = os.path.normpath(path)
    if n in (".", "..") or os.path.isabs(n) or n.startswith(".."):
        raise SystemExit(
            f"[AutoBuild] template: {what}='{path}' must be a relative directory "
            f"below the project (clean deletes it)"
        )


def _find_files(dirs: list, exts: set) -> list:
    found = set()
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for root, subdirs, files in os.walk(d):
            subdirs[:] = [
                x for x in subdirs if x not in _IGNORED_DIRS and not x.startswith(".")
            ]
            for f in files:
                if os.path.splitext(f)[1].lower() in exts:
                    found.add(_unix(os.path.join(root, f)))
    safe = sorted(p for p in found if not re.search(r"[\s(),\"']", p))
    for p in sorted(found - set(safe)):
        print(
            f"WARNING: template: skipping '{p}' (spaces/quotes/commas in the name)",
            file=sys.stderr,
        )
    return safe


def _exe(path: str) -> str:
    if os.name == "nt":
        return path.replace("/", "\\")
    return path if path.startswith(("/", "./")) else "./" + path


def _outpath(p: dict) -> str:
    out = _unix(p["out"].replace("{profile}", p.get("profile", "")))
    if os.name == "nt" and not out.lower().endswith(".exe"):
        out += ".exe"
    return out


def _kp(p: dict) -> str:
    return p["prefix"] + "-" if p.get("prefix") else ""


# ---- C / C++ ---------------------------------------------------------------
def _c_params(lang: str, incremental: bool) -> dict:
    cxx = lang == "cxx"
    params = {
        "cc": ("g++" if cxx else "gcc", "compiler command"),
        "std": ("c++17" if cxx else "c11", "language standard (-std=)"),
        "src": ("src", "source directory (searched recursively)"),
        "include": ("include", "header directory (added with -I when it exists)"),
        "out": ("build/{profile}/app", "output binary ({profile} = the build profile)"),
        "cflags": (
            "-Wall -Wextra",
            "extra compiler flags (optimization comes from the profile)",
        ),
        "ldflags": ("", 'linker flags / libraries, e.g. "-lm -lpthread"'),
        "run_args": ("", "arguments passed by the run recipe"),
        "profile": (
            "release",
            "build profile: debug, release, asan or tsan (override: --profile NAME)",
        ),
        "prefix": ("", "keep recipes in a module, e.g. prefix=cxx gives cxx.build"),
    }
    if incremental:
        params["obj"] = (
            "build/{profile}/obj",
            "object-file directory (removed by clean)",
        )
        params["jobs"] = (
            "auto",
            "compile this many files at once: auto, a number, or 1",
        )
    return params


_C_PROFILES = {  # profile -> (compile flags, extra link flags)
    "debug": ("-O0 -g", ""),
    "release": ("-O2 -DNDEBUG", ""),
    "asan": (
        "-O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer",
        "-fsanitize=address,undefined",
    ),
    "tsan": ("-O1 -g -fsanitize=thread", "-fsanitize=thread"),
}
_COMPDB = []  # compile_commands.json entries registered while templates expand


def _c_ld(p: dict) -> str:
    return f"{p['ldflags']} {_C_PROFILES[p['profile']][1]}".strip()


def _compdb_add(cc: str, flags: str, src: str, obj: str):
    try:
        argv = shlex.split(flags)
    except ValueError:
        argv = flags.split()
    _COMPDB.append(
        {
            "directory": os.getcwd(),
            "file": src,
            "arguments": [cc, *argv, "-c", src, "-o", obj],
        }
    )


def _c_common(p: dict, lang: str):
    if p["profile"] not in _C_PROFILES:
        raise SystemExit(
            f"[AutoBuild] template: unknown profile '{p['profile']}' - choose one of: "
            + ", ".join(_C_PROFILES)
        )
    cxx = lang == "cxx"
    exts = {".cpp", ".cc", ".cxx", ".c++"} if cxx else {".c"}
    hexts = {".h", ".hpp", ".hh", ".hxx", ".inl"} if cxx else {".h"}
    srcs = _find_files([p["src"]], exts)
    hdrs = _find_files([p["src"], p["include"]], hexts)
    flags = f"-std={p['std']} {p['cflags']} {_C_PROFILES[p['profile']][0]}".strip()
    if p["include"] and os.path.isdir(p["include"]):
        flags += f" -I{_unix(p['include'])}"
    return srcs, hdrs, flags


def _c_basic(p: dict, lang: str):
    srcs, _, flags = _c_common(p, lang)
    out = _outpath(p)
    outdir = os.path.dirname(out) or "."
    if srcs:
        build = (
            f"build:\n    mkdir({outdir})\n"
            f"    shell {p['cc']} {flags} {' '.join(srcs)} -o {out} {_c_ld(p)}\n"
        )
    else:
        build = f"build:\n    fail(no sources found under {p['src']} - add files or pass src=DIR)\n"
    for sfile in srcs:
        _compdb_add(
            p["cc"],
            flags,
            sfile,
            f"{outdir}/{os.path.splitext(os.path.basename(sfile))[0]}.o",
        )
    defs = (
        build
        + "compdb:\n    compdb(compile_commands.json)\n"
        + f"run: build\n    shell {_exe(out)} {p['run_args']}\n"
        + f"clean:\n    rm({out})\n"
        + "all: build\n"
    )
    return ["build", "run", "clean", "all", "compdb"], defs


def _c_incremental(p: dict, lang: str):
    """One recipe per source file. The compiler writes a depfile (-MMD), so
    the exact headers each object includes are tracked: editing a header
    rebuilds only the files that really include it."""
    kp = _kp(p)
    srcs, _hdrs, flags = _c_common(p, lang)
    cc, ld = p["cc"], _c_ld(p)
    out = _outpath(p)
    outdir = os.path.dirname(out) or "."
    objdir = _unix(p["obj"].replace("{profile}", p["profile"]))
    _guard_dir(objdir, "obj")
    src_root = _unix(p["src"])
    salt_c = _q(cc + " " + flags)
    names, defs, objpaths, used = [], [], [], set()
    for sfile in srcs:
        rel = sfile[len(src_root) + 1 :] if sfile.startswith(src_root + "/") else sfile
        base = re.sub(r"[^A-Za-z0-9_]", "_", os.path.splitext(rel)[0])
        name, i = f"cc_{base}", 2
        while name in used:
            name, i = f"cc_{base}_{i}", i + 1
        used.add(name)
        names.append(name)
        objpath, dpath = f"{objdir}/{name[3:]}.o", f"{objdir}/{name[3:]}.d"
        objpaths.append(objpath)
        _compdb_add(cc, flags, sfile, objpath)
        defs.append(
            f"{name}:\n"
            f"    mkdir({objdir})\n"
            f"    let _cc_deps = depfile({dpath})\n"
            f"    let _cc_in = append((_cc_deps), {sfile})\n"
            f"    if changed({kp}{name}, (_cc_in), {objpath}, {salt_c}) then\n"
            f"        shell {cc} {flags} -MMD -MP -MF {dpath} -c {sfile} -o {objpath}\n"
            f"        let _cc_deps = depfile({dpath})\n"
            f"        let _cc_in = append((_cc_deps), {sfile})\n"
            f"        stamp({kp}{name}, (_cc_in), {salt_c})\n"
            f"    else\n"
            f"        print(up to date: {sfile})\n"
            f"    done\n"
        )
    if srcs:
        defs.append(
            f"link: {' '.join(names)}\n"
            f"    mkdir({outdir})\n"
            f"    if changed({kp}link, {' '.join(objpaths)}, {out}, {_q(cc + ' ' + flags + ' ' + ld)}) then\n"
            f"        shell {cc} {' '.join(objpaths)} -o {out} {ld}\n"
            f"    else\n"
            f"        print(up to date: {out})\n"
            f"    done\n"
        )
    else:
        defs.append(
            f"link:\n    fail(no sources found under {p['src']} - add files or pass src=DIR)\n"
        )
    defs.append("build: link\n")
    defs.append(f"run: build\n    shell {_exe(out)} {p['run_args']}\n")
    defs.append(f"clean:\n    rm({objdir}, {out})\n")
    defs.append("compdb:\n    compdb(compile_commands.json)\n")
    defs.append("all: build\n")
    return names + ["link", "build", "run", "clean", "all", "compdb"], "\n".join(defs)


_CXX_DESC = "C++"
_template(
    "CXX-Basic",
    "Compile every C++ file under src/ into one binary in a single step. Recipes: build run clean all.",
    _c_params("cxx", False),
    ("cpp-basic", "c++-basic"),
)(lambda p: _c_basic(p, "cxx"))
_template(
    "CXX-Basic-Incremental",
    "C++ with one recipe per source file: only files whose source, included headers (tracked exactly via the compiler's depfiles) or flags changed are recompiled, then relinked. Parallel with jobs=. Recipes: build run clean all + cc_<file> link.",
    _c_params("cxx", True),
    (
        "cpp-basic-incremental",
        "c++-basic-incremental",
        "cpp-incremental",
        "c++-incremental",
        "cxx-incremental",
    ),
)(lambda p: _c_incremental(p, "cxx"))
_template(
    "C-Basic",
    "Compile every C file under src/ into one binary in a single step. Recipes: build run clean all.",
    _c_params("c", False),
)(lambda p: _c_basic(p, "c"))
_template(
    "C-Basic-Incremental",
    "C with one recipe per source file: only changed files are recompiled, then relinked. Parallel with jobs=.",
    _c_params("c", True),
    ("c-incremental",),
)(lambda p: _c_incremental(p, "c"))


# ---- Python ----------------------------------------------------------------
def _py_params() -> dict:
    return {
        "python": ("python3", "interpreter used to create the virtualenv"),
        "venv": (".venv", "virtualenv directory"),
        "requirements": ("requirements.txt", "requirements file (skipped when absent)"),
        "tests": ("tests", "test directory"),
        "entry": ("main.py", "script run by the run recipe"),
        "prefix": ("", "keep recipes in a module, e.g. prefix=py gives py.test"),
    }


def _py(p: dict, incremental: bool):
    kp = _kp(p)
    _guard_dir(p["venv"], "venv")
    venv = _unix(p["venv"])
    bindir = f"{venv}/Scripts" if os.name == "nt" else f"{venv}/bin"
    req, tests, entry = p["requirements"], p["tests"], p["entry"]
    defs = [
        f"venv:\n    if not exists({venv}) then\n        shell {p['python']} -m venv {venv}\n    done\n"
    ]
    if incremental:
        install = (
            f"install: venv\n    if exists({req}) then\n"
            f"        if changed({kp}py-install, {req}, {venv}) then\n"
            f"            shell {bindir}/pip install -r {req}\n"
            f"        else\n            print(dependencies up to date)\n        done\n    done\n"
        )
    else:
        install = f"install: venv\n    if exists({req}) then\n        shell {bindir}/pip install -r {req}\n    done\n"
    defs.append(install)
    pytest = f"{bindir}/python -m pytest" + (
        f" {tests}" if os.path.isdir(tests) else ""
    )
    if incremental:
        watch = [
            x
            for x in (
                ["src", tests, entry, "pyproject.toml", "setup.cfg", req]
                + sorted(glob.glob("*.py"))
            )
            if os.path.exists(x)
        ]
        watch = sorted(set(w for w in watch if not re.search(r"[\s(),\"']", w)))
        defs.append(
            f"test: install\n    if changed({kp}py-test, {' '.join(watch)}) then\n"
            f"        shell {pytest}\n    else\n        print(sources unchanged since the last passing test run - skipped)\n    done\n"
        )
    else:
        defs.append(f"test: install\n    shell {pytest}\n")
    defs.append(f"run: install\n    shell {bindir}/python {entry}\n")
    defs.append(f"clean:\n    rm({venv}, .pytest_cache)\n")
    defs.append("all: test\n")
    return ["venv", "install", "test", "run", "clean", "all"], "\n".join(defs)


_template(
    "Python-Basic",
    "Virtualenv + pip install + pytest + run. Recipes: venv install test run clean all.",
    _py_params(),
    ("py-basic",),
    default="test",
)(lambda p: _py(p, False))
_template(
    "Python-Basic-Incremental",
    "Like Python-Basic, but pip install runs only when requirements change and pytest only when sources change.",
    _py_params(),
    ("py-incremental", "python-incremental"),
    default="test",
)(lambda p: _py(p, True))


# ---- Node / Go / Rust ------------------------------------------------------
def _node(p: dict):
    kp = _kp(p)
    pm = p["pm"]
    lock = [
        f
        for f in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml")
        if os.path.exists(f)
    ]
    watch = " ".join(["package.json"] + lock)
    defs = [
        f"install:\n    if changed({kp}node-install, {watch}, node_modules) then\n"
        f"        shell {pm} install\n    else\n        print(node_modules up to date)\n    done\n",
        f"build: install\n    shell {pm} run {p['build_script']}\n",
        f"test: install\n    shell {pm} run {p['test_script']}\n",
        "clean:\n    rm(node_modules, dist)\n",
        "all: build\n",
    ]
    return ["install", "build", "test", "clean", "all"], "\n".join(defs)


_template(
    "Node-Basic",
    "npm/yarn/pnpm: install only when package.json or the lockfile changes, then build and test. Recipes: install build test clean all.",
    {
        "pm": ("npm", "package manager"),
        "build_script": ("build", "package.json script for build"),
        "test_script": ("test", "package.json script for test"),
        "prefix": ("", "keep recipes in a module"),
    },
    ("js-basic", "javascript-basic"),
)(_node)


def _go(p: dict):
    out = _outpath(p)
    defs = [
        f"build:\n    shell go build -o {out} {p['pkg']}\n",
        "vet:\n    shell go vet ./...\n",
        "test: vet\n    shell go test ./...\n",
        f"run: build\n    shell {_exe(out)}\n",
        f"clean:\n    rm({out})\n",
        "all: test build\n",
    ]
    return ["build", "vet", "test", "run", "clean", "all"], "\n".join(defs)


_template(
    "Go-Basic",
    "go build / vet / test / run (Go caches builds itself). Recipes: build vet test run clean all.",
    {
        "out": ("bin/app", "output binary"),
        "pkg": (".", "package to build"),
        "prefix": ("", "keep recipes in a module"),
    },
)(_go)


def _rust(p: dict):
    if p["profile"] not in ("debug", "release"):
        raise SystemExit(
            f"[AutoBuild] template: unknown profile '{p['profile']}' - choose debug or release"
        )
    flag = " --release" if p["profile"] == "release" else ""
    defs = [
        f"build:\n    shell cargo build{flag}\n",
        "test:\n    shell cargo test\n",
        f"run: build\n    shell cargo run{flag}\n",
        "clean:\n    shell cargo clean\n",
        "all: test build\n",
    ]
    return ["build", "test", "run", "clean", "all"], "\n".join(defs)


_template(
    "Rust-Basic",
    "cargo build / test / run / clean (cargo does its own incremental builds). Recipes: build test run clean all.",
    {
        "profile": ("debug", "debug or release"),
        "prefix": ("", "keep recipes in a module"),
    },
)(_rust)


_RECIPE_DOCS = {
    "build": "Build the project",
    "run": "Build and run the program",
    "clean": "Remove build outputs",
    "all": "Build everything and run the checks",
    "test": "Run the tests",
    "install": "Install dependencies",
    "venv": "Create the virtualenv",
    "vet": "Run go vet",
    "link": "Link the final binary",
    "compdb": "Write compile_commands.json for editors and clangd",
}


def _expand_template(given: str, given_params: dict) -> str:
    canon = _resolve_template(given)
    t = _TEMPLATES[canon]
    given_params = dict(given_params)
    user_default = given_params.pop("default", None)  # accepted by every template
    bad = sorted(set(given_params) - set(t["params"]))
    if bad:
        raise SystemExit(
            f"[AutoBuild] template {canon}: unknown parameter(s) {', '.join(bad)}. "
            f"Valid: {', '.join(t['params'])}, default"
        )
    p = {k: v[0] for k, v in t["params"].items()}
    p.update(given_params)
    if "profile" in t["params"] and _CLI_PROFILE:
        p["profile"] = _CLI_PROFILE  # --profile beats the template() line
    prefix = p.get("prefix", "")
    if prefix and not re.fullmatch(r"[A-Za-z_]\w*", prefix):
        raise SystemExit(f"[AutoBuild] template {canon}: prefix must be a plain name")
    names, defs = t["gen"](p)
    q = (lambda n: f"{prefix}.{n}") if prefix else (lambda n: n)
    out = [
        f"# --- template {canon}: generated by AutoBuild - edit the template line, not this ---"
    ]
    jobs = str(p.get("jobs", "")).strip()
    if jobs == "auto":
        out.append("jobs(get_proc())")
    elif jobs.isdigit() and int(jobs) > 1:
        out.append(f"jobs({jobs})")
    if "profile" in t["params"]:
        out.append(
            f"let profile ?= {p['profile']}"
        )  # visible to your recipes as (profile)
    for n in names:
        doc = _RECIPE_DOCS.get(n) or (
            f"Compile source file: {n[3:]}" if n.startswith("cc_") else ""
        )
        if doc:
            out.append(f"describe({q(n)}, {doc})")
    dflt = t["default"] if user_default is None else user_default
    if dflt and dflt.lower() != "none":
        if dflt not in names:
            raise SystemExit(
                f"[AutoBuild] template {canon}: default={dflt} is not one of: {', '.join(names)}"
            )
        # explicit default=X always wins; the built-in one yields to your own default()
        out.append(f"default({q(dflt)}{'' if user_default is not None else ', soft'})")
    if prefix:
        out += [f"recipe {q(n)}" for n in names]
        out += [f"module {prefix}", defs.rstrip("\n"), "done"]
    else:
        out += [f"recipe {n}" for n in names]
        out.append(defs.rstrip("\n"))
    out.append(f"# --- end template {canon} ---")
    return "\n".join(out)


def _parse_template_line(line: str):
    cname, cargs = Tokenize._split_call(line)
    args = [a.strip() for a in Tokenize._split_args(cargs)] if cargs.strip() else []
    if not args:
        raise SystemExit(
            '[AutoBuild] template() needs a name, e.g. template("CXX-Basic")'
        )
    name = args[0].strip("\"'")
    params = {}
    for a in args[1:]:
        m = re.fullmatch(r"(\w+)\s*=\s*(.*)", a, re.S)
        if not m:
            raise SystemExit(f"[AutoBuild] template(): expected key=value, got '{a}'")
        params[m.group(1)] = m.group(2).strip().strip("\"'")
    return name, params


def _templates_cli(argv: list) -> int:
    i = argv.index("--templates")
    nxt = argv[i + 1] if i + 1 < len(argv) else None
    name = nxt if nxt and not nxt.startswith("-") else None
    if name is None:
        print('Templates - use in .abuild as   template("NAME", key=value, ...)\n')
        w = max(len(n) for n in _TEMPLATES)
        for n in sorted(_TEMPLATES):
            print(f"  {n:<{w}}  {_TEMPLATES[n]['desc']}")
        print(
            "\nDetails: autobuild --templates NAME     New project: autobuild --init NAME"
        )
        return 0
    canon = _resolve_template(name)
    t = _TEMPLATES[canon]
    print(f"{canon}\n  {t['desc']}")
    if t["aliases"]:
        print("  also known as: " + ", ".join(t["aliases"]))
    print("\nParameters (key=value):")
    w = max(len(k) for k in t["params"])
    for k, (default, desc) in t["params"].items():
        print(f"  {k:<{w}}  default {default!r:<14} {desc}")
    print(
        f"  {'default':<{w}}  default {t['default']!r:<14} recipe run by plain `autobuild` (none = don't set one)"
    )
    print(f'\nExample .abuild:\n  template("{canon}")\n')
    print("Expands (with defaults, for the files in this directory) to:\n")
    print(_expand_template(canon, {}))
    return 0


def _init_cli(argv: list) -> int:
    i = argv.index("--init")
    rest = []
    for a in argv[i + 1 :]:
        if a.startswith("-"):
            break
        rest.append(a)
    if not rest:
        return _templates_cli(["--templates"])
    canon = _resolve_template(rest[0])
    params = {}
    for a in rest[1:]:
        if "=" not in a:
            print(f"AutoBuild: expected key=value, got '{a}'", file=sys.stderr)
            return 1
        k, v = a.split("=", 1)
        params[k] = v
    _expand_template(canon, params)  # validate before writing anything
    if os.path.exists(".abuild"):
        print(
            "AutoBuild: .abuild already exists - not overwriting it.", file=sys.stderr
        )
        return 1
    args = "".join(
        f", {k}={_q(v) if re.search(r'\s', v) else v}" for k, v in params.items()
    )
    with open(".abuild", "w", encoding="utf-8") as f:
        f.write(
            f"# .abuild - created by `autobuild --init {canon}`\n"
            f'template("{canon}"{args})\n\n'
            "# Add your own recipes below, for example:\n"
            "#   recipe deploy\n#   deploy: build\n#       shell ./deploy.sh\n"
        )
    print(f"Created .abuild using template {canon}.")
    print(
        "Next: autobuild --graph    (see the recipes)\n      autobuild build      (run it)"
    )
    return 0


def _expand_cli() -> int:
    if not os.path.exists(".abuild"):
        print("AutoBuild: No .abuild file found in current directory.", file=sys.stderr)
        return 1
    try:
        with open(".abuild", encoding="utf-8") as f:
            print(
                _expand_includes(f.read(), os.getcwd(), [os.path.realpath(".abuild")])
            )
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 1
    return 0


def _clean_err(e: BaseException) -> str:
    msg = str(e)
    return msg[len("[AutoBuild] ") :] if msg.startswith("[AutoBuild] ") else msg


class _LoopControl(Exception):
    pass


class _BreakLoop(_LoopControl):
    pass


class _ContinueLoop(_LoopControl):
    pass


# ---------------------------------------------------------------------------
# include "other.abuild"  - textual include, resolved relative to the file
# that contains the include line, with cycle detection. Line numbers in
# later error messages refer to the expanded text.
# ---------------------------------------------------------------------------
def _expand_includes(source: str, base_dir: str, stack: list) -> str:
    out = []
    for raw in source.splitlines():
        st = raw.strip()
        if st.startswith("include "):
            target = st[8:].strip().strip("\"'")
            path = os.path.normpath(os.path.join(base_dir, target))
            real = os.path.realpath(path)
            if real in stack:
                chain = " -> ".join(os.path.basename(x) for x in stack + [real])
                raise SystemExit(f"[AutoBuild] include cycle: {chain}")
            if not os.path.isfile(path):
                raise SystemExit(f"[AutoBuild] include: file not found: {path}")
            with open(path, encoding="utf-8") as f:
                out.append(
                    _expand_includes(f.read(), os.path.dirname(path), stack + [real])
                )
        elif st.startswith("template(") and Tokenize._is_call_like(st):
            tname, tparams = _parse_template_line(st)
            out.append(_expand_template(tname, tparams))
        else:
            out.append(raw)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Recipe dependency graph.
#
# graph: {recipe: [(dependency_as_written, resolved_name_or_None), ...]}
# A dependency written without its module prefix resolves against the
# depending recipe's own module first (same rule the executor uses).
# Used by `--graph` (static, never runs anything) and by the executor
# (validates before running: cycles / unknown dependencies abort up front,
# and every recipe runs at most once per invocation).
# ---------------------------------------------------------------------------
def collect_recipes(ast: list):
    declared, deps = [], {}

    def walk(nodes, prefix):
        for n in nodes:
            if n is None:
                continue
            if n.type == "recipe_decl":
                q = prefix + n.data["name"]
                if q not in declared:
                    declared.append(q)
            elif n.type == "recipe_def":
                deps[prefix + n.data["name"]] = list(n.data["deps"])
            elif n.type == "module":
                walk(n.data["body"], f"{n.data['name']}.")

    walk(ast, "")
    return declared, deps


def build_graph(declared: list, deps_raw: dict) -> dict:
    dset = set(declared)
    graph = {n: [] for n in declared}
    for name in declared:
        prefix = name.rsplit(".", 1)[0] + "." if "." in name else ""
        for raw in deps_raw.get(name, []):
            if raw in dset:
                res = raw
            elif prefix and prefix + raw in dset:
                res = prefix + raw
            else:
                res = None
            graph[name].append((raw, res))
    return graph


def graph_reachable(graph: dict, roots: list) -> set:
    seen, todo = set(), list(roots)
    while todo:
        n = todo.pop()
        if n in seen or n not in graph:
            continue
        seen.add(n)
        todo.extend(res for _, res in graph[n] if res)
    return seen


def find_cycles(graph: dict, roots: list) -> list:
    cycles, state = [], {}

    def dfs(n, stack):
        state[n] = 1
        stack.append(n)
        for _, res in graph.get(n, []):
            if res is None:
                continue
            if state.get(res) == 1:
                cyc = stack[stack.index(res) :] + [res]
                key = frozenset(cyc)
                if all(frozenset(c) != key for c in cycles):
                    cycles.append(cyc)
            elif res not in state:
                dfs(res, stack)
        stack.pop()
        state[n] = 2

    for r in roots:
        if r in graph and r not in state:
            dfs(r, [])
    return cycles


def graph_problems(graph: dict, defined: set, roots: "list | None" = None) -> dict:
    nodes = graph_reachable(graph, roots) if roots else set(graph)
    return {
        "missing": [
            (n, raw) for n in sorted(nodes) for raw, res in graph[n] if res is None
        ],
        "undefined": sorted(n for n in nodes if n not in defined),
        "cycles": find_cycles(graph, roots or sorted(graph)),
    }


def _graph_roots(graph: dict) -> list:
    depended = {res for deps in graph.values() for _, res in deps if res}
    roots = sorted(n for n in graph if n not in depended)
    return roots or sorted(graph)


def _unicode_ok() -> bool:
    try:
        "├└│✗↻".encode(sys.stdout.encoding or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def render_tree(graph: dict, roots: list) -> str:
    uni = _unicode_ok()
    T, L, V = ("├── ", "└── ", "│   ") if uni else ("|-- ", "`-- ", "|   ")
    bad, cyc = ("✗ missing", "↻ cycle") if uni else ("(missing)", "(cycle)")
    lines, expanded = [], set()

    def rec(name, prefix, stack):
        deps = graph[name]
        for i, (raw, res) in enumerate(deps):
            last = i == len(deps) - 1
            conn = L if last else T
            if res is None:
                lines.append(f"{prefix}{conn}{raw}  {bad}")
            elif res in stack:
                lines.append(f"{prefix}{conn}{res}  {cyc}")
            elif res in expanded and graph[res]:
                lines.append(f"{prefix}{conn}{res} (*)")
            else:
                lines.append(f"{prefix}{conn}{res}")
                expanded.add(res)
                rec(res, prefix + ("    " if last else V), stack + [res])

    for r in roots:
        lines.append(r)
        expanded.add(r)
        rec(r, "", [r])
    if any("(*)" in ln for ln in lines):
        lines.append("")
        lines.append("(*) already shown above")
    return "\n".join(lines)


def _cycle_nodes(graph: dict, roots: list) -> set:
    return {n for c in find_cycles(graph, roots) for n in c}


def render_dot(graph: dict, nodes: set) -> str:
    cyc = _cycle_nodes(graph, sorted(nodes))
    lines = [
        "digraph autobuild {",
        "  rankdir=LR;",
        "  node [shape=box, style=rounded];",
    ]
    for n in sorted(nodes):
        lines.append(f'  "{n}"' + (" [color=red];" if n in cyc else ";"))
    for n in sorted(nodes):
        for raw, res in graph[n]:
            if res is None:
                lines.append(
                    f'  "{raw}" [color=red, style="rounded,dashed", label="{raw}\\n(missing)"];'
                )
                lines.append(f'  "{n}" -> "{raw}" [color=red, style=dashed];')
            elif n in cyc and res in cyc:
                lines.append(f'  "{n}" -> "{res}" [color=red];')
            else:
                lines.append(f'  "{n}" -> "{res}";')
    lines.append("}")
    return "\n".join(lines)


def render_mermaid(graph: dict, nodes: set) -> str:
    cyc = _cycle_nodes(graph, sorted(nodes))
    ids = {n: f"n{i}" for i, n in enumerate(sorted(nodes))}
    lines = ["graph LR"]
    for n in sorted(nodes):
        lines.append(f'  {ids[n]}["{n}"]')
    missing = 0
    for n in sorted(nodes):
        for raw, res in graph[n]:
            if res is None:
                lines.append(f'  m{missing}["{raw} (missing)"]:::missing')
                lines.append(f"  {ids[n]} -.-> m{missing}")
                missing += 1
            else:
                lines.append(f"  {ids[n]} --> {ids[res]}")
    lines.append("  classDef missing stroke:#d33,stroke-dasharray:4;")
    lines.append("  classDef cycle stroke:#d33,stroke-width:2px;")
    for n in sorted(cyc):
        lines.append(f"  class {ids[n]} cycle;")
    return "\n".join(lines)


def render_order(graph: dict, roots: list) -> str:
    order, seen = [], set()

    def visit(n):
        if n in seen:
            return
        seen.add(n)
        for _, res in graph[n]:
            if res:
                visit(res)
        order.append(n)

    for r in roots:
        visit(r)
    level = {}
    for n in order:  # dependencies always come first in `order`
        level[n] = 1 + max((level[res] for _, res in graph[n] if res), default=-1)
    lines = ["Build order (dependencies first):"]
    lines += [f"  {i}. {n}" for i, n in enumerate(order, 1)]
    lines += ["", "Stages (recipes in the same stage don't depend on each other):"]
    for lv in range(max(level.values(), default=-1) + 1):
        lines.append(
            f"  stage {lv + 1}: " + " ".join(n for n in order if level[n] == lv)
        )
    return "\n".join(lines)


def collect_meta(ast: list):
    """default() / describe() found at top level, read statically (nothing
    is executed). Only literal arguments count."""
    default, desc = None, {}

    def lit(parts):
        if any(k == "VAR_REF" for k, _ in parts):
            return None
        return "".join(v for _, v in parts).strip().strip("\"'")

    for n in ast:
        if n is None or n.type != "call":
            continue
        args = [lit(a) for a in n.data["args"]]
        if n.data["call"] == "default" and args and args[0]:
            if not (len(args) == 2 and args[1] == "soft" and default):
                default = args[0]
        elif n.data["call"] == "describe" and len(args) == 2 and args[0]:
            desc[args[0]] = args[1] or ""
    return default, desc


def _format_recipe_list(declared: list, deps_raw: dict, desc: dict, default) -> str:
    if not declared:
        return "  (no recipes declared)"
    graph = build_graph(declared, deps_raw)
    names = sorted(declared)
    w = max(len(n) for n in names)
    lines = []
    for n in names:
        mark = "*" if n == default else " "
        needs = [raw for raw, _ in graph[n]]
        tail = f"  [needs: {', '.join(needs)}]" if needs else ""
        lines.append(f"  {n:<{w}} {mark} {desc.get(n, '')}{tail}".rstrip())
    if default:
        lines.append("\n  * = default (runs when you type just `autobuild`)")
    return "\n".join(lines)


_WATCH_SKIP_DIRS = _IGNORED_DIRS | {
    "build",
    "dist",
    "target",
    ".idea",
    ".vscode",
    ".cache",
}


def _watch_snapshot(roots: list) -> dict:
    snap = {}
    for root in roots:
        walk = (
            [(root, [], [os.path.basename(root)])]
            if os.path.isfile(root)
            else os.walk(root)
        )
        for dirpath, dirs, files in walk:
            dirs[:] = [
                d
                for d in dirs
                if d not in _WATCH_SKIP_DIRS and not d.startswith(".abuild")
            ]
            for f in files:
                if f.startswith(".abuild.") or f.endswith((".pyc", ".swp", "~")):
                    continue
                path = os.path.join(
                    os.path.dirname(root) if os.path.isfile(root) else dirpath, f
                )
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                snap[path] = (st.st_mtime_ns, st.st_size)
    return snap


def _watch_cli(argv: list) -> int:
    """--watch / -w: run the recipe, then re-run it whenever a file in the
    project changes (--watch-path P to restrict, --watch-interval S to poll
    faster/slower). Every run is a fresh process, so .abuild edits and new
    source files are picked up. Changes made while a run is in progress are
    detected by the NEXT poll only if they happen after it ends."""
    own = _own_args(argv)
    rest = list(argv[len(own) :])
    args, roots, interval, i = [], [], 0.5, 0
    while i < len(own):
        a = own[i]
        if a in ("--watch", "-w"):
            pass
        elif a == "--watch-path" or a.startswith("--watch-path="):
            if "=" in a:
                roots.append(a.split("=", 1)[1])
            elif i + 1 < len(own):
                roots.append(own[i + 1])
                i += 1
            else:
                print("AutoBuild: --watch-path expects a path", file=sys.stderr)
                return 1
        elif a == "--watch-interval" or a.startswith("--watch-interval="):
            if "=" in a:
                v = a.split("=", 1)[1]
            elif i + 1 < len(own):
                v = own[i + 1]
                i += 1
            else:
                print("AutoBuild: --watch-interval expects seconds", file=sys.stderr)
                return 1
            try:
                interval = max(0.05, float(v))
            except ValueError:
                print("AutoBuild: --watch-interval expects seconds", file=sys.stderr)
                return 1
        else:
            args.append(a)
        i += 1
    roots = roots or ["."]
    cmd = [sys.executable, os.path.abspath(sys.argv[0])] + args + rest
    print(f"[watch] watching {', '.join(roots)} - Ctrl-C to stop")
    try:
        while True:
            code = subprocess.call(cmd)
            print(
                f"[watch] run finished (exit code {code}); waiting for changes...",
                flush=True,
            )
            baseline = _watch_snapshot(roots)
            while True:
                time.sleep(interval)
                cur = _watch_snapshot(roots)
                if cur == baseline:
                    continue
                while True:  # debounce: let editors finish writing
                    time.sleep(0.2)
                    nxt = _watch_snapshot(roots)
                    if nxt == cur:
                        break
                    cur = nxt
                changed = sorted(
                    k for k in set(cur) | set(baseline) if cur.get(k) != baseline.get(k)
                )
                more = f" (+{len(changed) - 3} more)" if len(changed) > 3 else ""
                print(
                    f"[watch] change detected: {', '.join(changed[:3])}{more}",
                    flush=True,
                )
                break
    except KeyboardInterrupt:
        print("\n[watch] stopped")
        return 0


def _compdb_cli() -> int:
    if not os.path.exists(".abuild"):
        print("AutoBuild: No .abuild file found in current directory.", file=sys.stderr)
        return 1
    try:
        with open(".abuild", encoding="utf-8") as f:
            _expand_includes(f.read(), os.getcwd(), [os.path.realpath(".abuild")])
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 1
    if not _COMPDB:
        print(
            "AutoBuild: no C/C++ template in .abuild, so there is nothing to put in compile_commands.json.",
            file=sys.stderr,
        )
        return 1
    with open("compile_commands.json", "w", encoding="utf-8") as f:
        json.dump(_COMPDB, f, indent=2)
    print(f"Wrote compile_commands.json ({len(_COMPDB)} entries)")
    return 0


def _list_cli() -> int:
    if not os.path.exists(".abuild"):
        print("AutoBuild: No .abuild file found in current directory.", file=sys.stderr)
        return 1
    try:
        with open(".abuild", encoding="utf-8") as f:
            source = _expand_includes(
                f.read(), os.getcwd(), [os.path.realpath(".abuild")]
            )
        ast = Parser(Tokenize(source).GetTokens()).GetAST()
    except BaseException as e:
        print(f"Error while reading .abuild: {e}", file=sys.stderr)
        return 1
    declared, deps_raw = collect_recipes(ast)
    default, desc = collect_meta(ast)
    print("Recipes:\n" + _format_recipe_list(declared, deps_raw, desc, default))
    return 0


_VAR_RE = re.compile(r"^([A-Za-z_]\w*)=(.*)$", re.S)
_VALUE_FLAGS = {
    "--log-file",
    "--jobs",
    "-j",
    "--var",
    "-D",
    "--profile",
    "--watch-path",
    "--watch-interval",
}
_CLI_PROFILE = None  # --profile NAME (or --var profile=NAME); read by templates


def _own_args(argv: list) -> list:
    """AutoBuild's own arguments: everything before a literal `--`. What
    follows `--` belongs to your recipes (see args())."""
    return list(argv[: argv.index("--")]) if "--" in argv else list(argv)


_KNOWN_LONG = {
    "--help",
    "--version",
    "--explain",
    "--force",
    "--var",
    "--profile",
    "--watch",
    "--watch-path",
    "--watch-interval",
    "--compdb",
    "--list",
    "--dry-run",
    "--templates",
    "--init",
    "--expand",
    "--jobs",
    "--graph",
    "--history",
    "--log-file",
    "--repl",
}


def _opt_value(own: list, names: list, default=None, required: bool = True):
    for i, a in enumerate(own):
        for n in names:
            if a == n:
                if i + 1 < len(own) and not own[i + 1].startswith("-"):
                    return own[i + 1]
                if required:
                    raise SystemExit(f"[AutoBuild] {n} expects a value")
                return default
            if n.startswith("--") and a.startswith(n + "="):
                return a[len(n) + 1 :]
    return default


def _warn_unknown_flags(own: list):
    for a in own:
        if not a.startswith("--") or len(a) < 4:
            continue
        name = a.split("=", 1)[0]
        if name in _KNOWN_LONG:
            continue
        close = difflib.get_close_matches(name, sorted(_KNOWN_LONG), n=1, cutoff=0.85)
        if close:
            print(
                f"WARNING: unknown option '{name}' (did you mean '{close[0]}'?) "
                f"- treating it as a script flag",
                file=sys.stderr,
            )


def _parse_cli_vars(argv: list) -> dict:
    """--var NAME=VALUE | --var=NAME=VALUE | -D NAME=VALUE | -DNAME=VALUE |
    make-style NAME=VALUE | --profile NAME. A bare `--var NAME` means true.
    Values are typed like literals in .abuild (5 -> int, true -> bool...)."""
    argv = _own_args(argv)
    out, i = {}, 0
    while i < len(argv):
        a, spec = argv[i], None
        if a in ("--var", "-D"):
            if i + 1 >= len(argv):
                raise SystemExit(f"[AutoBuild] {a} expects NAME=VALUE")
            spec = argv[i + 1]
            i += 1
        elif a.startswith("--var="):
            spec = a[6:]
        elif a.startswith("-D") and len(a) > 2:
            spec = a[2:]
        elif a == "--profile":
            if i + 1 >= len(argv):
                raise SystemExit("[AutoBuild] --profile expects a name")
            out["profile"] = argv[i + 1]
            i += 1
        elif a.startswith("--profile="):
            out["profile"] = a.split("=", 1)[1]
        elif a in ("--log-file", "--jobs", "-j", "--watch-path", "--watch-interval"):
            i += 1  # skip this flag's value
        elif not a.startswith("-") and _VAR_RE.match(a):
            spec = a
        if spec is not None:
            m = _VAR_RE.match(spec)
            if m:
                out[m.group(1)] = _coerce_literal(m.group(2))
            elif re.fullmatch(r"[A-Za-z_]\w*", spec):
                out[spec] = True
            else:
                raise SystemExit(f"[AutoBuild] expected NAME=VALUE, got '{spec}'")
        i += 1
    return out


def _positionals(argv: list) -> list:
    """Recipe names: non-flag arguments before `--`, skipping flag values
    and make-style NAME=VALUE variables."""
    pos, skip = [], False
    for a in _own_args(argv):
        if skip:
            skip = False
        elif a in _VALUE_FLAGS:
            skip = True
        elif not a.startswith("-") and not _VAR_RE.match(a):
            pos.append(a)
    return pos


def _parse_jobs(argv: list):
    """--jobs N | --jobs=N | -j N | -jN  (N = number or 'auto'; 0 = auto)."""
    argv = _own_args(argv)
    for i, a in enumerate(argv):
        if a in ("--jobs", "-j") and i + 1 < len(argv):
            v = argv[i + 1]
        elif a.startswith("--jobs="):
            v = a.split("=", 1)[1]
        elif a.startswith("-j") and a[2:].isdigit():
            v = a[2:]
        elif a in ("--jobs", "-j"):
            raise SystemExit("[AutoBuild] --jobs expects a number or 'auto'")
        else:
            continue
        if v == "auto":
            return get_proc_count()
        if v.isdigit():
            return get_proc_count() if int(v) == 0 else min(int(v), 512)
        raise SystemExit(f"[AutoBuild] --jobs expects a number or 'auto', got '{v}'")
    return None


class _ThreadStdout:
    """Routes print() output of a recipe-worker thread into that thread's
    own buffer so parallel recipes' output never interleaves."""

    def __init__(self, real, ex):
        self._real = real
        self._ex = ex

    def write(self, text):
        buf = getattr(self._ex._tls, "buf", None)
        return (self._real if buf is None else buf).write(text)

    def flush(self):
        self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)


_GRAPH_FORMATS = ("tree", "dot", "mermaid", "order")


def _graph_cli(argv: list) -> int:
    args = list(argv)
    gi = args.index("--graph")
    fmt = "tree"
    if gi + 1 < len(args) and args[gi + 1] in _GRAPH_FORMATS:
        fmt = args.pop(gi + 1)
    args.pop(gi)
    pos = _positionals(args)
    target = pos[0] if pos else None

    if not os.path.exists(".abuild"):
        print("AutoBuild: No .abuild file found in current directory.", file=sys.stderr)
        return 1
    try:
        with open(".abuild", encoding="utf-8") as f:
            source = _expand_includes(
                f.read(), os.getcwd(), [os.path.realpath(".abuild")]
            )
        ast = Parser(Tokenize(source).GetTokens()).GetAST()
    except BaseException as e:
        print(f"Error while reading .abuild: {e}", file=sys.stderr)
        return 1
    declared, deps_raw = collect_recipes(ast)
    graph = build_graph(declared, deps_raw)
    if not graph:
        print("No recipes declared.")
        return 0
    if target and target not in graph:
        near = sorted(
            r for r in graph if r.endswith("." + target) or r.startswith(target + ".")
        )
        print(f"AutoBuild: Unknown recipe '{target}'.", file=sys.stderr)
        print(
            "Did you mean one of:\n  " + "\n  ".join(near)
            if near
            else "Available recipes:\n  " + "\n  ".join(sorted(graph)),
            file=sys.stderr,
        )
        return 1
    roots = [target] if target else _graph_roots(graph)
    nodes = graph_reachable(graph, roots)
    if fmt == "tree":
        out = render_tree(graph, roots)
    elif fmt == "dot":
        out = render_dot(graph, nodes)
    elif fmt == "mermaid":
        out = render_mermaid(graph, nodes)
    else:
        probs = find_cycles(graph, roots)
        out = (
            "Cannot order: dependency cycle(s) found."
            if probs
            else render_order(graph, roots)
        )
    print(out)
    problems = graph_problems(graph, set(deps_raw), roots if target else None)
    for cyc in problems["cycles"]:
        print("problem: dependency cycle: " + " -> ".join(cyc), file=sys.stderr)
    for n, raw in problems["missing"]:
        print(f"problem: '{n}' depends on unknown recipe '{raw}'", file=sys.stderr)
    for n in problems["undefined"]:
        print(f"warning: recipe '{n}' is declared but never defined", file=sys.stderr)
    return 1 if (problems["cycles"] or problems["missing"]) else 0


class Executor:
    """Walks the AST produced by Parser (optionally via Optimizer) and executes each node."""

    def __init__(
        self,
        ast: list,
        modules: "dict | None" = None,
        run_log: "RunLog | None" = None,
        force: bool = False,
        jobs_cli: "int | None" = None,
        dry_run: bool = False,
        cli_vars: "dict | None" = None,
    ):
        self.ast = ast
        self._tls = td.local()  # per-thread vars / output buffer / loop depth
        self.force = force  # --force: changed() always answers "true"
        self.pending_cache = {}  # key -> digest, committed on success only
        self._cache = None
        self._done = set()  # recipes already run in THIS invocation
        self._loop_depth = 0  # for `break`/`continue` outside-loop errors
        self._validated = False
        self.jobs_cli = jobs_cli  # --jobs on the command line (wins over jobs(n))
        self.jobs = jobs_cli or 1
        self._sched_started = False
        self._print_lock = td.Lock()
        self._real_stdout = None
        self._graph = {}
        self.dry_run = dry_run  # --dry-run: print commands, run nothing
        self.dry_cmds = 0
        self.default_recipe = None  # set by default(NAME)
        self.descriptions = {}  # set by describe(NAME, text)
        self.dry_dirty = set()  # outputs a dry run says WOULD be rebuilt
        self._cache_lock = td.RLock()
        self.run_log = run_log  # must be set before _execute() runs below
        self.cli_vars = dict(cli_vars or {})  # --var / VAR=value: win over `let`
        self.vars = dict(self.cli_vars)
        self.vars_lock = td.Lock()
        self.recipes_declared = []
        self.recipes_defined = {}
        self.fns = {}
        self.modules = modules if modules is not None else {}
        self.open_files = {}  # handle id (str) -> file object, for openfile()/friends
        self._next_handle = 0
        self._collect(self.ast, prefix="")
        self._execute(self.ast)

    # `vars` and the loop depth are PER THREAD while recipes run in
    # parallel (each worker gets a private copy of the globals); on the main
    # thread they're simply the global ones.
    @property
    def vars(self):
        v = getattr(self._tls, "vars", None)
        return self._global_vars if v is None else v

    @vars.setter
    def vars(self, value):
        self._global_vars = value

    @property
    def _loop_depth(self):
        return getattr(self._tls, "loop_depth", 0)

    @_loop_depth.setter
    def _loop_depth(self, value):
        self._tls.loop_depth = value

    def load_cache(self) -> dict:
        with self._cache_lock:
            if self._cache is None:
                try:
                    with open(CACHE_PATH, encoding="utf-8") as f:
                        data = json.load(f)
                    self._cache = data if isinstance(data, dict) else {}
                except (OSError, json.JSONDecodeError):
                    self._cache = {}
            return self._cache

    def _pending(self) -> dict:
        """Where changed() records digests: the running recipe's own dict
        (committed when THAT recipe succeeds), else the top-level one."""
        d = getattr(self._tls, "pending", None)
        return self.pending_cache if d is None else d

    def commit_cache(self, source: "dict | None" = None):
        """Persists digests recorded by changed(). Written atomically (temp
        file + replace). Does nothing in --dry-run."""
        src = self.pending_cache if source is None else source
        if self.dry_run or not src:
            src.clear()
            return
        with self._cache_lock:
            cache = self.load_cache()
            cache.update(src)
            tmp = f"{CACHE_PATH}.{os.getpid()}.tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(cache, f, indent=2, sort_keys=True)
                os.replace(tmp, CACHE_PATH)
            except OSError as e:
                print(f"WARNING: could not write cache '{CACHE_PATH}': {e}")
        src.clear()

    def _collect(self, nodes, prefix: str):
        """Pre-pass to register all declared recipes and fns before execution,
        including ones nested inside `module Name ... done` blocks (registered
        under `Name.recipe`)."""
        for node in nodes:
            if node is None:
                continue
            if node.type == "recipe_decl":
                qualified = f"{prefix}{node.data['name']}"
                if qualified not in self.recipes_declared:
                    self.recipes_declared.append(qualified)
            elif node.type == "recipe_def":
                pass  # collected via recipe_decl; definition matched at exec time
            elif node.type == "fn_def":
                qualified = f"{prefix}{node.data['name']}"
                self.fns[qualified] = node
            elif node.type == "module":
                self._collect(node.data["body"], prefix=f"{node.data['name']}.")

    def _lookup(self, name: str):
        with self.vars_lock:
            if name in self.vars:
                return self.vars[name]
        print(f"WARNING: Variable [{name}] is not defined")
        return None

    def _resolve(self, parts: list) -> str:
        """Resolves (STRING, VAR_REF) parts into plain text (for shell
        commands): every value is stringified."""
        return "".join(
            val if kind != "VAR_REF" else _to_str(self._lookup(val))
            for kind, val in parts
        )

    def _value(self, parts: list):
        """Resolves parts into a TYPED value (see 'Typed values' above)."""
        return _eval_parts(parts, self._lookup)

    def _arg_value(self, parts: list, module_prefix: str = ""):
        """Like _value, but an argument that is itself a call to a known
        builtin/fn - e.g. jobs(get_proc()) - is evaluated (any depth)."""
        text = "".join(v if k == "STRING" else f"({v})" for k, v in parts).strip()
        if text and Tokenize._is_call_like(text):
            name = text.partition("(")[0]
            if (
                name in _BUILTIN_FNS
                or name in self.fns
                or (module_prefix and module_prefix + name in self.fns)
            ):
                return self._eval_operand(text, module_prefix)
        return self._value(parts)

    def _eval_operand(self, text: str, module_prefix: str = ""):
        """Evaluates one side of a condition / a for-in source: `not X`,
        a call like changed(a, b), a variable name, or a literal."""
        t = text.strip()
        if t.startswith("not "):
            return not _truthy(self._eval_operand(t[4:], module_prefix))
        if Tokenize._is_call_like(t):
            name, argtext = Tokenize._split_call(t)
            args = []
            if argtext.strip():
                args = [
                    self._arg_value(Tokenize._parse_var_refs(a.strip()), module_prefix)
                    for a in Tokenize._split_args(argtext)
                ]
            return self.CallFn(name, args, module_prefix=module_prefix)
        with self.vars_lock:
            if t in self.vars:
                return self.vars[t]
        return self._value(Tokenize._parse_var_refs(t))

    def _eval_expr(self, text: str, module_prefix: str = "") -> bool:
        """A full condition: `a and b`, `a or b` (or && / ||), `not x`,
        a comparison, a call, or a bare variable (truthiness). `and` binds
        tighter than `or`; both short-circuit."""
        t = text.strip()
        parts = Tokenize._split_logic(t, "or")
        if len(parts) > 1:
            return any(self._eval_expr(p, module_prefix) for p in parts)
        parts = Tokenize._split_logic(t, "and")
        if len(parts) > 1:
            return all(self._eval_expr(p, module_prefix) for p in parts)
        if t.startswith("not "):
            return not self._eval_expr(t[4:], module_prefix)
        left, op, right = Tokenize._parse_condition(t)
        lv = self._eval_operand(left, module_prefix)
        rv = None if op is None else self._eval_operand(right, module_prefix)
        return _compare(lv, op, rv)

    def _eval_cond(self, condition, module_prefix: str = "") -> bool:
        left, op, right = condition
        if op is None:
            return self._eval_expr(left, module_prefix)
        lv = self._eval_operand(left, module_prefix)
        rv = self._eval_operand(right, module_prefix)
        return _compare(lv, op, rv)

    def _loop_body(self, body: list, module_prefix: str) -> bool:
        """Runs one loop iteration; returns True when the loop must stop."""
        try:
            self._execute(body, module_prefix=module_prefix)
        except _ContinueLoop:
            return False
        except _BreakLoop:
            return True
        return False

    def _may_assign(self, name: str, soft=False) -> bool:
        """A command-line variable (--var) can't be reassigned by the script
        (like make); `let NAME ?= v` only assigns when NAME isn't set yet."""
        if name in self.cli_vars:
            return False
        if soft:
            with self.vars_lock:
                return name not in self.vars
        return True

    def _kill_tree(self, proc):
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except OSError:
            pass

    def _exec(self, cmd: str, capture: bool = False, timeout: "float | None" = None):
        """Runs `cmd` in a shell. Returns (exit_code, captured_stdout or None).
        Output streams live unless this thread's output is buffered (parallel
        recipes) or capture=True. A timeout kills the whole process group."""
        if timeout is None:
            timeout = getattr(self._tls, "cmd_timeout", None)
        buffered = getattr(self._tls, "buf", None) is not None
        kw = {"shell": True}
        if timeout is not None and os.name == "posix":
            kw["start_new_session"] = True  # so the timeout can kill children too
        if capture:
            kw.update(stdout=subprocess.PIPE, text=True, errors="replace")
            if buffered:
                kw["stderr"] = subprocess.PIPE
        elif buffered:
            kw.update(
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            )
        start = time.perf_counter()
        proc = subprocess.Popen(cmd, **kw)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill_tree(proc)
            proc.communicate()
            if self.run_log:
                self.run_log.command(cmd, -1, time.perf_counter() - start)
            raise SystemExit(
                f"[AutoBuild] Command timed out after {timeout:g}s:\n\t {cmd}"
            )
        except BaseException:  # e.g. Ctrl-C: don't leave the child running
            self._kill_tree(proc)
            raise
        if buffered:
            sys.stdout.write((err if capture else out) or "")
        if self.run_log:
            self.run_log.command(cmd, proc.returncode, time.perf_counter() - start)
        return proc.returncode, (out if capture else None)

    def _run_command(self, cmd: str):
        """Runs a shell command, streaming output live."""
        if self.dry_run:
            with self._print_lock:
                self.dry_cmds += 1
            print(f"\033[33m[dry-run] {cmd}\033[0m")
            return
        print(f"\n\033[36m{cmd}\n\033[0m")
        code, _ = self._exec(cmd)
        if code != 0:
            raise SystemExit(
                f"[AutoBuild] Command failed with exit code {code}:\n\t {cmd}"
            )

    def CallFn(self, name: str, arg_values: list, module_prefix: str = ""):
        """Calls a built-in or a declared `fn`, binding params to the
        (typed) arg_values, executing its body and returning the value of
        its `return` expression (NULL if none). If `name` isn't found as-is
        and we're inside `module X ... done`, also tries `X.name`."""
        if name in _BUILTIN_FNS:
            return _BUILTIN_FNS[name](self, arg_values)

        resolved_name = name
        if resolved_name not in self.fns and module_prefix:
            candidate = f"{module_prefix}{name}"
            if candidate in self.fns:
                resolved_name = candidate
        if resolved_name not in self.fns:
            raise SystemExit(f"[AutoBuild] Unknown function '{name}'")
        node = self.fns[resolved_name]
        params = node.data["params"]
        if len(arg_values) != len(params):
            raise SystemExit(
                f"[AutoBuild] Function '{name}' expects {len(params)} "
                f"argument(s), got {len(arg_values)}"
            )
        inner_prefix = (
            resolved_name.rsplit(".", 1)[0] + "." if "." in resolved_name else ""
        )
        # Isolated local scope: save + restore caller's vars for the params.
        saved = {}
        with self.vars_lock:
            for p, v in zip(params, arg_values):
                saved[p] = self.vars.get(p, _MISSING)
                self.vars[p] = v
        saved_depth, self._loop_depth = self._loop_depth, 0  # break can't escape a fn
        try:
            self._execute(node.data["body"], module_prefix=inner_prefix)
            ret = node.data.get("ret")
            return self._value(ret) if ret else None
        finally:
            self._loop_depth = saved_depth
            with self.vars_lock:
                for p in params:
                    if saved[p] is _MISSING:
                        self.vars.pop(p, None)
                    else:
                        self.vars[p] = saved[p]

    def _run_parallel(self, body: list, module_prefix: str = ""):
        """Runs a `parallel do ... pend` block's children as real OS
        processes (via ProcessPoolExecutor), orchestrated by asyncio so the
        parent isn't blocked serially awaiting each one.

        Security/correctness boundary: every child here must be a `shell`
        or `command` node. Those are resolved to a plain string IN THIS
        PROCESS first (using the live self.vars, under self.vars_lock,
        exactly like sequential execution does) - only the fully-resolved
        string crosses into the worker process. A node that would mutate
        interpreter state (`let`, `let_call`, `if`, `for`, nested `fn`
        calls, etc.) is refused here rather than silently running in a
        child process where any writes to `self.vars` would vanish the
        moment that process exits - that's the kind of bug that looks fine
        in testing and then quietly loses state in production. If you need
        to compute a value and then run parallel shell commands using it,
        compute it with `let`/`fn` BEFORE the `parallel do` block.
        """
        resolved_cmds = []
        for child_node in body:
            if not child_node:
                continue
            if child_node.type not in ("shell", "command"):
                raise SystemExit(
                    f"[AutoBuild] `parallel do` only supports shell/command "
                    f"lines (each runs in its own process); got '{child_node.type}'. "
                    f"Compute any values with let/fn before the parallel block."
                )
            resolved_cmds.append(self._resolve(child_node.data["parts"]))

        if not resolved_cmds:
            return

        if self.dry_run:
            for cmd in resolved_cmds:
                with self._print_lock:
                    self.dry_cmds += 1
                print(f"\033[33m[dry-run] (parallel) {cmd}\033[0m")
            return

        async def _run_all():
            loop = asyncio.get_event_loop()
            # Shell children are typically I/O/subprocess-bound (compilers,
            # network calls, etc.), not CPU-bound Python work, so cap by
            # command count rather than core count - capping at cpu_count()
            # would serialize a `parallel do` of, say, 5 network calls on a
            # 2-core box for no reason. Still bounded (never unbounded) so
            # a huge parallel block can't fork-bomb the machine.
            max_workers = max(1, min(len(resolved_cmds), 32))
            with concurrent.futures.ProcessPoolExecutor(
                max_workers=max_workers
            ) as pool:
                tasks = [
                    loop.run_in_executor(pool, _run_command_in_process, cmd)
                    for cmd in resolved_cmds
                ]
                for cmd in resolved_cmds:
                    print(f"\n\033[36m{cmd}\n\033[0m")
                results = await asyncio.gather(*tasks, return_exceptions=True)
            return results

        results = asyncio.run(_run_all())

        failures = []
        for cmd, result in zip(resolved_cmds, results):
            if isinstance(result, BaseException):
                if self.run_log:
                    self.run_log.command(cmd, -1, 0.0, parallel=True)
                failures.append((cmd, f"process error: {result}"))
                continue
            _, returncode = result
            if self.run_log:
                # per-command timing isn't available from the process pool
                self.run_log.command(cmd, returncode, 0.0, parallel=True)
            if returncode != 0:
                failures.append((cmd, f"exit code {returncode}"))

        if failures:
            details = "\n  ".join(f"{cmd} -> {why}" for cmd, why in failures)
            raise SystemExit(
                f"[AutoBuild] {len(failures)} parallel command(s) failed:\n  {details}"
            )

    def _execute_node(self, node, module_prefix: str = "") -> None:
        if node.type == "get":
            if node.data["name"] not in self.cli_vars:
                self.vars[node.data["name"]] = (
                    None if node.data["is_null"] else node.data["value"]
                )

        elif node.type == "let":
            if self._may_assign(node.data["name"], node.data.get("soft")):
                self.vars[node.data["name"]] = self._value(node.data["parts"])

        elif node.type == "let_call":
            if self._may_assign(node.data["name"], node.data.get("soft")):
                arg_values = [
                    self._arg_value(a, module_prefix) for a in node.data["args"]
                ]
                self.vars[node.data["name"]] = self.CallFn(
                    node.data["call"], arg_values, module_prefix=module_prefix
                )

        elif node.type == "call":
            arg_values = [self._arg_value(a, module_prefix) for a in node.data["args"]]
            self.CallFn(node.data["call"], arg_values, module_prefix=module_prefix)

        elif node.type == "shell":
            cmd = self._resolve(node.data["parts"])
            self._run_command(cmd)

        elif node.type == "command":
            cmd = self._resolve(node.data["parts"])
            self._run_command(cmd)

        elif node.type == "if":
            for branch in node.data["branches"]:
                condition = branch["condition"]
                if condition is None or self._eval_cond(condition, module_prefix):
                    self._execute(branch["body"], module_prefix=module_prefix)
                    break

        elif node.type == "for_till":
            self._loop_depth += 1
            try:
                for _ in range(node.data["count"]):
                    if self._loop_body(node.data["body"], module_prefix):
                        break
            finally:
                self._loop_depth -= 1

        elif node.type == "for_in":
            var = node.data["var"]
            ref = node.data["list_ref"].strip()
            with self.vars_lock:
                known = ref in self.vars
            if not known and re.fullmatch(r"[A-Za-z_]\w*", ref):
                items = []  # undefined variable name: iterate nothing
            else:
                src = self._eval_operand(ref, module_prefix)
                if isinstance(src, list):
                    items = list(src)
                elif src is None:
                    items = []
                else:
                    items = _to_str(src).split()
            self._loop_depth += 1
            try:
                for item in items:
                    self.vars[var] = item
                    if self._loop_body(node.data["body"], module_prefix):
                        break
            finally:
                self._loop_depth -= 1
                # clean up loop var
                self.vars.pop(var, None)

        elif node.type == "while":
            self._loop_depth += 1
            try:
                while self._eval_cond(node.data["condition"], module_prefix):
                    if self._loop_body(node.data["body"], module_prefix):
                        break
            finally:
                self._loop_depth -= 1

        elif node.type in ("break", "continue"):
            if self._loop_depth == 0:
                raise SystemExit(f"[AutoBuild] `{node.type}` used outside of a loop")
            raise _BreakLoop() if node.type == "break" else _ContinueLoop()

        elif node.type == "try":
            try:
                self._execute(node.data["body"], module_prefix=module_prefix)
            except SystemExit as e:
                if node.data["catch"] is not None:
                    self.vars["error"] = _clean_err(e)
                    self._execute(node.data["catch"], module_prefix=module_prefix)

        elif node.type == "retry":
            n = int(
                _num(self._eval_operand(node.data["count"], module_prefix), "retry")
            )
            ev = node.data["every"]
            delay = (
                float(_num(self._eval_operand(ev, module_prefix), "retry"))
                if ev
                else 0.0
            )
            if n < 1:
                raise SystemExit("[AutoBuild] retry: count must be >= 1")
            for attempt in range(1, n + 1):
                try:
                    self._execute(node.data["body"], module_prefix=module_prefix)
                    break
                except SystemExit as e:
                    if attempt == n:
                        raise SystemExit(
                            f"[AutoBuild] {_clean_err(e)} (after {n} attempt(s))"
                        )
                    print(
                        f"\033[33m[AutoBuild] attempt {attempt}/{n} failed: "
                        f"{' '.join(_clean_err(e).split())} - retrying"
                        + (f" in {delay:g}s" if delay else "")
                        + "\033[0m"
                    )
                    if delay and not self.dry_run:
                        time.sleep(delay)

        elif node.type == "timeout":
            secs = float(
                _num(self._eval_operand(node.data["seconds"], module_prefix), "timeout")
            )
            saved = getattr(self._tls, "cmd_timeout", None)
            self._tls.cmd_timeout = secs
            try:
                self._execute(node.data["body"], module_prefix=module_prefix)
            finally:
                self._tls.cmd_timeout = saved

        elif node.type == "parallel":
            self._run_parallel(node.data["body"], module_prefix=module_prefix)

        elif node.type == "recipe_decl":
            pass  # already handled in _collect

        elif node.type == "recipe_def":
            name = node.data["name"]
            if name not in self.recipes_declared:
                raise SyntaxError(
                    f"[AutoBuild] Recipe '{name}' was not declared before definition"
                )
            self.recipes_defined[name] = node

        elif node.type == "fn_def":
            # A `fn` defined directly inside a recipe body (rather than at
            # module/top level, where _collect already registered it) is
            # registered here, the first time execution actually reaches
            # it - so it becomes callable for the rest of that recipe run
            # (and any later run, since self.fns persists on the Executor).
            qualified = f"{module_prefix}{node.data['name']}"
            if qualified not in self.fns:
                self.fns[qualified] = node

        elif node.type == "module":
            # A module block's top-level statements (lets, recipe defs, fn
            # defs...) execute in place, same as at top level; recipe/fn
            # names inside were already namespace-qualified in _collect.
            prefix = f"{node.data['name']}."
            for child in node.data["body"]:
                if child is None:
                    continue
                if child.type == "recipe_def":
                    qualified = f"{prefix}{child.data['name']}"
                    if qualified not in self.recipes_declared:
                        raise SyntaxError(
                            f"[AutoBuild] Recipe '{qualified}' was not declared "
                            f"before definition"
                        )
                    self.recipes_defined[qualified] = child
                elif child.type == "fn_def":
                    pass  # already collected under qualified name
                else:
                    self._execute_node(child, module_prefix=prefix)

    def _execute(self, nodes: list, module_prefix: str = ""):
        for node in nodes:
            if node:
                self._execute_node(node, module_prefix=module_prefix)

    def RunRecipe(self, name: str):
        """Runs a recipe by name (supports `module.recipe` names) after its
        dependencies. Before anything executes, the dependency graph below
        `name` is validated (cycles and unknown dependencies abort up
        front), and every recipe runs at most once per invocation, so a
        shared dependency in a diamond isn't run twice. A dependency named
        without its module prefix resolves against the depending recipe's
        own module first."""
        if name not in self.recipes_declared:
            raise SystemExit(f"[AutoBuild] Unknown recipe '{name}'")
        graph = build_graph(
            self.recipes_declared,
            {n: d.data["deps"] for n, d in self.recipes_defined.items()},
        )
        probs = graph_problems(graph, set(self.recipes_defined), [name])
        if probs["cycles"]:
            raise SystemExit(
                "[AutoBuild] Dependency cycle: " + " -> ".join(probs["cycles"][0])
            )
        if probs["missing"]:
            n, raw = probs["missing"][0]
            raise SystemExit(
                f"[AutoBuild] Recipe '{n}' depends on unknown recipe '{raw}'"
            )
        if probs["undefined"]:
            raise SystemExit(
                f"[AutoBuild] Recipe '{probs['undefined'][0]}' was declared but never defined"
            )
        self._graph = graph
        self._sched_started = True
        if self.jobs > 1 and len(graph_reachable(graph, [name])) > 1:
            self._run_graph_parallel(name)
        else:
            self._run_recipe(name)

    def _run_graph_parallel(self, root: str):
        """Runs `root` and everything it depends on with up to self.jobs
        recipes at once. A recipe starts only when ALL its dependencies have
        finished. Each worker gets a private copy of the variables; when a
        recipe succeeds, the variables it set/changed are merged back, so
        dependents (which start later) see them, while recipes running
        side by side never see each other's. After the first failure no new
        recipe starts; running ones finish, then the run fails."""
        graph = self._graph
        needed = graph_reachable(graph, [root])
        pending = {n: {res for _, res in graph[n] if res} for n in needed}
        dependents = {n: [] for n in needed}
        for n, ds in pending.items():
            for d in ds:
                dependents[d].append(n)
        ready = sorted(n for n, ds in pending.items() if not ds)
        running, failures = {}, []
        self._real_stdout = sys.stdout
        sys.stdout = _ThreadStdout(self._real_stdout, self)
        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.jobs, thread_name_prefix="abuild"
        )
        try:
            while ready or running:
                while ready and len(running) < self.jobs and not failures:
                    n = ready.pop(0)
                    running[pool.submit(self._run_recipe_job, n)] = n
                if not running:
                    break
                done, _ = concurrent.futures.wait(
                    list(running), return_when=concurrent.futures.FIRST_COMPLETED
                )
                for fut in done:
                    n = running.pop(fut)
                    exc = fut.exception()
                    if exc is not None:
                        failures.append((n, exc))
                        continue
                    self._done.add(n)
                    for child in dependents[n]:
                        pending[child].discard(n)
                        if not pending[child] and child not in ready:
                            ready.append(child)
        except KeyboardInterrupt:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            pool.shutdown(wait=True)
            sys.stdout = self._real_stdout
        if failures:
            if len(failures) == 1:
                n, e = failures[0]
                raise SystemExit(f"[AutoBuild] Recipe '{n}' failed: {e}")
            raise SystemExit(
                f"[AutoBuild] {len(failures)} recipes failed: "
                + "; ".join(f"{n}: {e}" for n, e in failures)
            )

    def _run_recipe_job(self, name: str):
        node = self.recipes_defined[name]
        module_prefix = name.rsplit(".", 1)[0] + "." if "." in name else ""
        real = self._real_stdout
        with self._print_lock:
            real.write(f"[AutoBuild] Starting recipe: {name}\n")
            real.flush()
        with self.vars_lock:
            local = dict(self._global_vars)
        base = dict(local)
        self._tls.vars = local
        self._tls.buf = io.StringIO()
        self._tls.loop_depth = 0
        if self.run_log:
            self.run_log.record.setdefault("recipes_run", []).append(name)
        t0, err = time.perf_counter(), None
        self._tls.pending = {}
        try:
            self._execute(node.data["body"], module_prefix=module_prefix)
        except BaseException as e:
            err = e
        finally:
            out = self._tls.buf.getvalue()
            pend = self._tls.pending
            self._tls.buf = None
            self._tls.vars = None
            self._tls.pending = None
        if err is None:
            self.commit_cache(pend)
        verb = "FAILED after" if err is not None else "finished in"
        block = f"[AutoBuild] Recipe '{name}' {verb} {time.perf_counter() - t0:.2f}s\n"
        block += out if (not out or out.endswith("\n")) else out + "\n"
        with self._print_lock:
            real.write(block)
            real.flush()
        if err is not None:
            raise err
        with self.vars_lock:  # merge what this recipe set/changed
            for k, v in local.items():
                if k not in base or base[k] != v:
                    self._global_vars[k] = v

    def _run_recipe(self, name: str):
        if name in self._done:
            return
        node = self.recipes_defined[name]
        module_prefix = name.rsplit(".", 1)[0] + "." if "." in name else ""
        for _, res in self._graph[name]:
            self._run_recipe(res)
        print(f"[AutoBuild] Running recipe: {name}")
        if self.run_log:
            self.run_log.record.setdefault("recipes_run", []).append(name)
        saved_depth, self._loop_depth = self._loop_depth, 0
        self._tls.pending = {}
        try:
            self._execute(node.data["body"], module_prefix=module_prefix)
            self.commit_cache(self._tls.pending)  # this recipe succeeded
        finally:
            self._loop_depth = saved_depth
            self._tls.pending = None
        self._done.add(name)


if __name__ == "__main__":
    from arghandle import ArgHandle

    # behave like a normal CLI when piped (`autobuild --graph | head`)
    if hasattr(__import__("signal"), "SIGPIPE"):
        __import__("signal").signal(
            __import__("signal").SIGPIPE, __import__("signal").SIG_DFL
        )

    cli = ArgHandle("AutoBuild", "v3.1.0")
    # (no PrintOnNoArgs: plain `autobuild` runs the default() recipe, or lists the recipes)
    cli.CustomHelpMsg(
        """
AutoBuild v3.1.0 HELP called:
\033[36m--help OR -h: Print this help message and exit.\033[0m
\033[36m--version OR -v: Print version and exit.\033[0m
\033[36m--explain: Print what the optimizer folded/eliminated before running.\033[0m
\033[33mBuiltins: changed incremental restore versions getargv getfullargv len type int float str bool
  upper lower trim replace split join append at contains add sub mul div mod glob exists which
  basename dirname ext cwd print fail get_proc jobs depfile stamp
  capture status lines getenv platform exe mkdir rm cp mv touch readtext writetext appendtext isdir isfile
  abspath pathjoin stem default describe flag option args compdb  (blocks: try/catch(on_fail)/done  retry N [every S] do  timeout S do)  (types: string int float bool NULL list)\033[0m
\033[36m--force: Ignore the incremental cache; changed() always returns true.\033[0m
\033[36m--var NAME=VALUE OR -D NAME=VALUE OR just NAME=VALUE: Set a variable like make does (beats `let`; 5 is an int, true a bool). Repeatable.\033[0m
\033[36m--profile NAME: Build profile for C/C++/Rust templates (debug release asan tsan); also the variable (profile).\033[0m
\033[36m--watch OR -w [--watch-path P] [--watch-interval S]: Re-run when files change.\033[0m
\033[36m--compdb: Write compile_commands.json from the C/C++ template.\033[0m
\033[36m-- ARGS...: Everything after -- goes to your recipes: args(); also flag(NAME) / option(NAME, default).\033[0m
\033[36m--list OR -l: List the recipes with their descriptions and mark the default one.\033[0m
\033[36m--dry-run OR -n: Show the recipes and commands that would run, run nothing, change nothing.\033[0m
\033[36m--templates [NAME]: List templates (or show one). In .abuild: template("CXX-Basic-Incremental", std=c++20)\033[0m
\033[36m--init NAME [key=value ...]: Create a .abuild that uses a template.\033[0m
\033[36m--expand: Print .abuild after includes and templates are expanded.\033[0m
\033[36m--jobs N|auto OR -j N: Run up to N independent recipes at once (also: jobs(N) / jobs(get_proc()) in .abuild).\033[0m
\033[36m--graph [tree|dot|mermaid|order] [RECIPE]: Print the recipe dependency graph (never runs anything).\033[0m
\033[36m--history [N]: Show the last N runs (default 10) from the run log.\033[0m
\033[36m--log-file PATH: Use PATH instead of .abuild.log for the run log.\033[0m
\033[36m--repl or -r: Start autobuild REPL (experimental).\033[0m
\033[33m[RECIPE]: The recipe name (supports module.recipe)...\033[0m

\033[36m

\033[4mExamples:\033[0m\033[36m
autobuild test # test is the recipe name you defined in .abuild
autobuild pkg.build # a recipe named 'build' inside 'module pkg ... done'
autobuild -h
autobuild -v
autobuild test --explain
autobuild --history 5
autobuild all -j 4 # run independent recipes 4 at a time
autobuild --init CXX-Basic-Incremental # start a C++ project
autobuild build --dry-run # preview, run nothing
autobuild # runs the recipe named by default(NAME)
autobuild build --profile asan # sanitizer build in build/asan/
autobuild test --var FILTER=parser -- -k fast # variables and recipe args
autobuild build --watch # rebuild on every save
autobuild --list # recipes + descriptions
autobuild --graph # dependency tree of every recipe
autobuild all --graph mermaid # just what 'all' depends on
autobuild --graph dot > deps.dot
\033[0m
"""
    )
    own = _own_args(sys.argv[1:])
    _warn_unknown_flags(own)
    cli.HandleBasic()
    explain = "--explain" in own
    force = "--force" in own
    log_path = _opt_value(own, ["--log-file"], DEFAULT_LOG_PATH)

    # --history needs neither an .abuild file nor a recipe
    if any(a == "--history" or a.startswith("--history=") for a in own):
        n = _opt_value(own, ["--history"], "10", required=False)
        print_history(log_path, int(n) if str(n).isdigit() else 10)
        raise SystemExit(0)

    if "--watch" in own or "-w" in own:
        raise SystemExit(_watch_cli(sys.argv[1:]))
    _CLI_PROFILE = _parse_cli_vars(sys.argv[1:]).get("profile")  # for templates
    if "--compdb" in own:
        raise SystemExit(_compdb_cli())
    if "--list" in own or "-l" in own:
        raise SystemExit(_list_cli())
    if "--templates" in own:
        raise SystemExit(_templates_cli(own))
    if "--init" in own:
        raise SystemExit(_init_cli(own))
    if "--expand" in own:
        raise SystemExit(_expand_cli())

    # --graph only reads and parses .abuild; it never runs anything
    if "--graph" in own:
        raise SystemExit(_graph_cli(own))

    run_log = RunLog(log_path)
    pos = _positionals(sys.argv[1:])
    arg = pos[0] if pos else ""
    dry_run = "--dry-run" in own or "-n" in own
    run_log.disabled = dry_run  # a dry run leaves no history
    try:
        jobs_cli = _parse_jobs(sys.argv[1:])
        cli_vars = _parse_cli_vars(sys.argv[1:])
    except SystemExit as e:
        run_log.finish("error", str(e))
        raise
    run_log.record["recipe"] = arg or None

    # Load autobuild CLI and parse .abuild
    if not __import__("pathlib").Path(".abuild").exists():
        run_log.finish("error", "No .abuild file found in current directory")
        raise SystemExit("AutoBuild: No .abuild file found in current directory.\n")
    try:
        source = _expand_includes(
            open(".abuild").read(), os.getcwd(), [os.path.realpath(".abuild")]
        )
        tokens = Tokenize(source).GetTokens()
        ast = Parser(tokens).GetAST()
        optimizer = Optimizer(ast, explain=explain, cli_vars=cli_vars)
        optimizer.PrintReport()
        executor = Executor(
            optimizer.GetAST(),
            run_log=run_log,
            force=force,
            jobs_cli=jobs_cli,
            dry_run=dry_run,
            cli_vars=cli_vars,
        )
        # print(f"[DEBUG]: Recipes declared; {executor.recipes_declared}")
    except KeyboardInterrupt:
        run_log.finish("interrupted", "KeyboardInterrupt")
        raise
    except BaseException as e:
        # BaseException: a failing top-level command raises SystemExit,
        # which `except Exception` would let through unlogged.
        run_log.finish("error", str(e))
        raise SystemExit(f"Error while running file: {e}\n")
    # Match and run recipe
    if not arg and executor.default_recipe:
        arg = executor.default_recipe
        run_log.record["recipe"] = arg
        print(f"[AutoBuild] No recipe given - running default: {arg}")
    if not arg:
        run_log.finish("error", "No recipe name given and no default() set")
        cli.ErrorArgPrint(
            "AutoBuild: No recipe name given and no default(RECIPE) set.\n"
            "Recipes:\n"
            + _format_recipe_list(
                executor.recipes_declared,
                {n: d.data["deps"] for n, d in executor.recipes_defined.items()},
                executor.descriptions,
                None,
            )
            + "\nUse --list or --help for more.\n"
        )
    elif arg not in executor.recipes_declared:
        # Always say SOMETHING here - silently exiting on a typo'd or
        # missing recipe name (e.g. a recipe that only exists as
        # 'test.smoke' after a module rename, when the user typed just
        # 'test') is worse than a wrong-looking error: it looks like the
        # whole CLI silently did nothing, with no way to tell why.
        suggestions = [
            r
            for r in executor.recipes_declared
            if r == arg or r.endswith(f".{arg}") or r.startswith(f"{arg}.")
        ]
        msg = f"AutoBuild: Unknown recipe '{arg}'.\n"
        if suggestions:
            msg += "Did you mean one of:\n  " + "\n  ".join(sorted(suggestions)) + "\n"
        else:
            msg += (
                "Available recipes:\n  "
                + "\n  ".join(sorted(executor.recipes_declared))
                + "\n"
            )
        run_log.finish("error", f"Unknown recipe '{arg}'")
        cli.ErrorArgPrint(msg)
    else:
        try:
            executor.RunRecipe(arg)  # pyright: ignore
            executor.commit_cache()  # top-level changed() digests
            if dry_run:
                print(
                    f"\033[33m[dry-run] {executor.dry_cmds} command(s) would run in "
                    f"{len(executor._done)} recipe(s); nothing was executed\033[0m"
                )
            run_log.finish("success")
        except KeyboardInterrupt:
            run_log.finish("interrupted", "KeyboardInterrupt")
            raise
        except BaseException as e:
            run_log.finish("failed", str(e))
            cli.ErrorArgPrint(f"Error while running file: {e}\n")