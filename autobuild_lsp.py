#!/usr/bin/env python3
"""
autobuild-lsp - Language Server for AutoBuild (.abuild) files.
https://github.com/alanbutidk/autobuild

Single file, Python 3.8+, no dependencies. Speaks LSP over stdio.

Features: diagnostics, formatting, completion (+snippets), hover, go-to-definition,
document symbols, folding ranges.

Run:  python autobuild_lsp.py          (stdio is the default / only transport)
"""

import json
import os
import re
import sys
import warnings

warnings.simplefilter("ignore")

# Keep the protocol channel clean: keep a private handle to the real stdout, then
# point sys.stdout at stderr so a stray print() can never corrupt JSON-RPC, and
# send stderr to a log file only when AUTOBUILD_LSP_LOG is set, else to nowhere.
_OUT = sys.stdout.buffer
_LOG = os.environ.get("AUTOBUILD_LSP_LOG")
try:
    sys.stderr = open(_LOG, "a", encoding="utf-8") if _LOG else open(os.devnull, "w")
except OSError:
    sys.stderr = open(os.devnull, "w")
sys.stdout = sys.stderr

VERSION = "0.1.0"

# --------------------------------------------------------------------------
# Language data
# --------------------------------------------------------------------------
KEYWORDS = {
    "let": "`let name = value` - set a variable. Read it with `(name)`.",
    "get": "`get NAME` - pull an environment variable into a variable.",
    "recipe": "`recipe name` declares a recipe; define it with `name: deps` and an indented body.",
    "default": "`default(NAME)` - recipe run by plain `autobuild`.",
    "if": "`if a == b then ... elif ... else then ... done`",
    "elif": "`elif cond then` - alternative branch.",
    "else": "`else then` - final branch.",
    "then": "Ends an `if`/`elif` condition.",
    "while": "`while cond do ... done`",
    "for": "`for till(n) do ... done` or `for x in (LIST) do ... done`",
    "in": "`for x in (LIST) do`",
    "do": "Opens a loop / retry / timeout / parallel body.",
    "done": "Closes `if`, `while`, `for`, `try`, `retry`, `timeout` and `module` blocks.",
    "break": "Leave the innermost loop.",
    "continue": "Jump to the next loop iteration.",
    "try": "`try ... catch ... done` - run the body, run `catch` if it fails.",
    "catch": "Error branch of `try` (alias: `on_fail`).",
    "on_fail": "Alias of `catch`.",
    "retry": "`retry N [every S] do ... done` - retry the body up to N times.",
    "every": "`retry N every S do` - seconds between attempts.",
    "timeout": "`timeout S do ... done` - abort the body after S seconds.",
    "parallel": "`parallel do ... pend` - run the lines inside at the same time.",
    "pend": "Closes a `parallel do` block.",
    "fn": "`fn name(args) { ... return (x) }` - define a function.",
    "return": "Return a value from a `fn` (no return = empty string).",
    "module": "`module Name ... done` - namespace for recipes/functions; call as `Name.recipe`.",
    "include": '`include "other.abuild"` - include another build file (cycles detected).',
    "shell": "`shell cmd` - run a shell command (plain lines in a recipe also run).",
    "template": '`template("NAME", key=value)` - expand a built-in template.',
    "true": "bool true",
    "false": "bool false",
    "NULL": "the NULL value",
}

BUILTINS = {
    "len": ("len(x)", "Length of a string or list."),
    "type": ("type(x)", "Type name of a value."),
    "int": ("int(x)", "Convert to int."),
    "float": ("float(x)", "Convert to float."),
    "str": ("str(x)", "Convert to string."),
    "bool": ("bool(x)", "Convert to bool."),
    "add": ("add(a, b)", "a + b"),
    "sub": ("sub(a, b)", "a - b"),
    "mul": ("mul(a, b)", "a * b"),
    "div": ("div(a, b)", "a / b"),
    "mod": ("mod(a, b)", "a % b"),
    "upper": ("upper(s)", "Uppercase a string."),
    "lower": ("lower(s)", "Lowercase a string."),
    "trim": ("trim(s)", "Strip surrounding whitespace."),
    "replace": ("replace(s, old, new)", "Replace text in a string."),
    "split": ("split(s, sep)", "Split a string into a list."),
    "join": ("join(list, sep)", "Join a list into a string."),
    "append": ("append(list, value)", "Append to a list."),
    "at": ("at(list, i)", "Element at index i."),
    "contains": ("contains(x, item)", "Whether x contains item."),
    "lines": ("lines(s)", "Split text into lines."),
    "basename": ("basename(path)", "Last path component."),
    "dirname": ("dirname(path)", "Directory part of a path."),
    "ext": ("ext(path)", "File extension."),
    "stem": ("stem(path)", "File name without extension."),
    "abspath": ("abspath(path)", "Absolute path."),
    "pathjoin": ("pathjoin(a, b, ...)", "Join path components."),
    "cwd": ("cwd()", "Current working directory."),
    "exists": ("exists(path)", "Path exists?"),
    "isdir": ("isdir(path)", "Is a directory?"),
    "isfile": ("isfile(path)", "Is a file?"),
    "which": ("which(cmd)", "Locate an executable on PATH."),
    "glob": ("glob(pattern)", "List of files matching a pattern."),
    "mkdir": ("mkdir(path)", "Create a directory."),
    "rm": ("rm(path)", "Remove a file or directory."),
    "cp": ("cp(src, dst)", "Copy."),
    "mv": ("mv(src, dst)", "Move / rename."),
    "touch": ("touch(path)", "Create or update a file."),
    "readtext": ("readtext(path)", "Read a whole text file."),
    "writetext": ("writetext(path, text)", "Write a text file."),
    "appendtext": ("appendtext(path, text)", "Append text to a file."),
    "openfile": ("openfile(path, mode)", "Open a file handle."),
    "writefile": ("writefile(handle, text)", "Write to an open file."),
    "readfile": ("readfile(handle)", "Read from an open file."),
    "closefile": ("closefile(handle)", "Close an open file."),
    "capture": ("capture(cmd)", "Run a command and return its output."),
    "status": ("status()", "Exit status of the last command."),
    "getenv": ("getenv(name)", "Environment variable value."),
    "platform": ("platform()", "Current platform name."),
    "exe": ("exe(name)", "Add the platform's executable suffix."),
    "print": ("print(text)", "Print text."),
    "fail": ("fail(msg)", "Abort the build with a message."),
    "getargv": ("getargv([i])", "Script argv."),
    "getfullargv": ("getfullargv([i])", "Full interpreter argv."),
    "args": ("args()", "Arguments after `--` as a list."),
    "flag": ("flag(NAME)", "Was `--NAME` passed after `--`?"),
    "option": ("option(NAME, default)", "Value of `--NAME value` after `--`."),
    "default": ("default(NAME)", "Set the default recipe."),
    "describe": ("describe(text)", "Description shown by `--list`."),
    "jobs": ("jobs(n)", "Max parallel recipes."),
    "get_proc": ("get_proc()", "Processor count."),
    "changed": (
        "changed(key, inputs, outputs)",
        "True when inputs' content hash changed, an output is missing, or with `--force`.",
    ),
    "incremental": ("incremental(file, keep=3)", "Snapshot a file when it changes."),
    "versions": ("versions(file)", "List snapshots of a file."),
    "restore": ("restore(file, n=1)", "Restore the nth previous snapshot."),
    "depfile": ("depfile(path)", "Read a compiler depfile."),
    "stamp": ("stamp(key, inputs)", "Content-hash stamp for inputs."),
    "compdb": ("compdb()", "Write compile_commands.json."),
    "template": ('template("NAME", key=value)', "Expand a built-in template."),
}

TEMPLATES = [
    "C-Basic",
    "CXX-Basic",
    "C-Basic-Incremental",
    "CXX-Basic-Incremental",
    "Python-Basic",
    "Python-Basic-Incremental",
    "Node-Basic",
    "Go-Basic",
    "Rust-Basic",
]

SNIPPETS = {
    "if": "if ${1:cond} then\n  $0\ndone",
    "ifelse": "if ${1:cond} then\n  $2\nelse then\n  $0\ndone",
    "for": "for ${1:x} in (${2:LIST}) do\n  $0\ndone",
    "fortill": "for till(${1:n}) do\n  $0\ndone",
    "while": "while ${1:cond} do\n  $0\ndone",
    "try": "try\n  $1\ncatch\n  $0\ndone",
    "retry": "retry ${1:3} every ${2:1} do\n  $0\ndone",
    "timeout": "timeout ${1:30} do\n  $0\ndone",
    "parallel": "parallel do\n  $0\npend",
    "fn": "fn ${1:name}(${2:args}) {\n  $0\n  return (${3:x})\n}",
    "module": "module ${1:Name}\n  $0\ndone",
    "recipe": "recipe ${1:name}\n\n${1:name}:\n  $0",
}

LOOP_KINDS = {"while", "for"}
CLOSED_BY_DONE = {"if", "while", "for", "try", "retry", "timeout", "module"}
OPENERS = {"if", "while", "for", "try", "retry", "timeout", "parallel", "module", "fn"}

# Order matters: first match wins.
_PATTERNS = [
    ("get", r"get\s+([A-Za-z_]\w*)\s*"),
    ("let", r"let\s+([A-Za-z_][\w\-]*)\s*=(.*)"),
    ("include", r"include\s+.+"),
    ("fn", r"fn\s+([\w.\-]+)\s*\((.*)\)\s*\{"),
    ("rbrace", r"\}"),
    ("return", r"return\b.*"),
    ("module", r"module\s+([\w.\-]+)\s*"),
    ("if", r"if\s+.*then"),
    ("elif", r"elif\s+.*then"),
    ("else", r"else(\s+then)?"),
    ("while", r"while\s+.*\bdo"),
    ("for", r"for\s+.*\bdo"),
    ("try", r"try"),
    ("catch", r"(catch|on_fail)"),
    ("retry", r"retry\s+\S+(\s+every\s+\S+)?\s+do"),
    ("timeout", r"timeout\s+\S+\s+do"),
    ("parallel", r"parallel\s+do"),
    ("pend", r"pend"),
    ("done", r"done"),
    ("break", r"break"),
    ("continue", r"continue"),
    ("shell", r"shell\b.*"),
    ("recipe_decl", r"recipe\s+(.+)"),
    ("recipe_def", r"([A-Za-z_][\w.\-]*)\s*:(?!=)\s*(.*)"),
    ("call", r"([A-Za-z_][\w.]*)\((.*)\)\s*"),
]
PATTERNS = [(k, re.compile(p, re.S)) for k, p in _PATTERNS]


def classify(s):
    if s.startswith("#"):
        return "comment", None
    for kind, rx in PATTERNS:
        m = rx.fullmatch(s)
        if m:
            return kind, m
    return "other", None


class Blk:
    __slots__ = ("kind", "line", "orig", "name")

    def __init__(self, kind, line, orig, name=""):
        self.kind, self.line, self.orig, self.name = kind, line, orig, name


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------
class Analysis:
    def __init__(self, text):
        self.lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        self.depths = []  # per line: indent depth or None (blank)
        self.kinds = []  # per line: statement kind ('blank' for empty lines)
        self.diags = []  # (line, c0, c1, severity, message)
        self.symbols = []  # dicts: name, kind, line, detail
        self.folds = []  # (start, end)
        self.vars = {}  # name -> first line
        self.recipes_declared = {}  # qualified -> line
        self.recipes_defined = {}  # qualified -> line
        self.fns = {}  # name -> line
        self.modules = {}  # name -> line
        self.has_include = False
        self._run()

    def err(self, i, msg, sev=1, c0=None, c1=None):
        raw = self.lines[i]
        c0 = len(raw) - len(raw.lstrip()) if c0 is None else c0
        c1 = len(raw.rstrip()) if c1 is None else c1
        self.diags.append((i, c0, max(c1, c0 + 1), sev, msg))

    def _run(self):
        stack = []
        deferred_calls = []  # (line, name, c0)
        dep_refs = []  # (line, name, modname, c0)
        default_refs = []

        def modname():
            return ".".join(b.name for b in stack if b.kind == "module")

        def qual(n):
            m = modname()
            return f"{m}.{n}" if m else n

        for i, raw in enumerate(self.lines):
            s = raw.strip()
            if not s:
                self.depths.append(None)
                self.kinds.append("blank")
                continue
            orig = len(raw.expandtabs(2)) - len(raw.expandtabs(2).lstrip())
            kind, m = classify(s)
            col = len(raw) - len(raw.lstrip())

            # end an open recipe body when we dedent to/below its definition
            if (
                stack
                and stack[-1].kind == "recipe"
                and kind != "comment"
                and orig <= stack[-1].orig
            ):
                stack.pop()

            depth = len(stack)

            if kind == "comment":
                if stack and stack[-1].kind == "recipe" and orig <= stack[-1].orig:
                    depth -= 1

            elif kind == "get":
                self.vars.setdefault(m.group(1), i)

            elif kind == "let":
                self.vars.setdefault(m.group(1), i)
                if not stack:
                    self.symbols.append(
                        dict(name=m.group(1), kind=13, line=i, detail="let")
                    )
                rhs = m.group(2).strip()
                cm = re.fullmatch(r"([A-Za-z_][\w.]*)\((.*)\)", rhs, re.S)
                if cm:
                    deferred_calls.append(
                        (i, cm.group(1), raw.find(cm.group(1), raw.find("=")))
                    )

            elif kind == "include":
                self.has_include = True

            elif kind == "call":
                name = m.group(1)
                if name == "template":
                    self.has_include = True
                if name == "default":
                    default_refs.append(
                        (i, m.group(2).strip().strip("\"'"), raw.find("(") + 1)
                    )
                if not stack:
                    deferred_calls.append((i, name, col))

            elif kind == "recipe_decl":
                for n in m.group(1).split():
                    self.recipes_declared.setdefault(qual(n), i)

            elif kind == "recipe_def":
                if stack and stack[-1].kind not in ("module", "recipe"):
                    kind = "other"  # a command line inside a block that looks like `x:`
                else:
                    name = m.group(1)
                    q = qual(name)
                    if q in self.recipes_defined:
                        self.err(
                            i,
                            f"recipe '{name}' is already defined (line {self.recipes_defined[q] + 1})",
                            1,
                            col,
                            col + len(name),
                        )
                    self.recipes_defined.setdefault(q, i)
                    self.symbols.append(dict(name=q, kind=6, line=i, detail="recipe"))
                    depth = len(stack)
                    stack.append(Blk("recipe", i, orig, name))
                    pos = raw.find(":") + 1
                    for dm in re.finditer(r"[^\s]+", m.group(2)):
                        dep_refs.append(
                            (i, dm.group(0), modname(), pos + 1 + dm.start())
                        )

            elif kind in OPENERS:
                name = ""
                if kind == "module":
                    name = m.group(1)
                    self.modules.setdefault(name, i)
                    self.symbols.append(
                        dict(name=name, kind=3, line=i, detail="module")
                    )
                elif kind == "fn":
                    name = m.group(1)
                    self.fns.setdefault(name, i)
                    self.symbols.append(
                        dict(
                            name=name,
                            kind=12,
                            line=i,
                            detail="fn(" + m.group(2).strip() + ")",
                        )
                    )
                    for p in re.split(r"\s*,\s*", m.group(2).strip()):
                        if re.fullmatch(r"[A-Za-z_]\w*", p or ""):
                            self.vars.setdefault(p, i)
                elif kind == "for":
                    fm = re.match(r"for\s+([A-Za-z_]\w*)\s+in\b", s)
                    if fm:
                        self.vars.setdefault(fm.group(1), i)
                stack.append(Blk(kind, i, orig, name))

            elif kind in ("elif", "else"):
                if not stack or stack[-1].kind != "if":
                    self.err(i, f"'{kind}' without a matching 'if'")
                depth = max(len(stack) - 1, 0)

            elif kind == "catch":
                if not stack or stack[-1].kind != "try":
                    self.err(i, "'catch' without a matching 'try'")
                depth = max(len(stack) - 1, 0)

            elif kind in ("done", "pend", "rbrace"):
                while stack and stack[-1].kind == "recipe":
                    stack.pop()
                want = {"done": "'done'", "pend": "'pend'", "rbrace": "'}'"}[kind]
                if not stack:
                    self.err(i, f"unexpected {want}: nothing is open")
                else:
                    top = stack[-1]
                    ok = (
                        (kind == "done" and top.kind in CLOSED_BY_DONE)
                        or (kind == "pend" and top.kind == "parallel")
                        or (kind == "rbrace" and top.kind == "fn")
                    )
                    if ok:
                        stack.pop()
                        if i > top.line + 1:
                            self.folds.append((top.line, i - 1))
                    else:
                        exp = {"parallel": "'pend'", "fn": "'}'"}.get(
                            top.kind, "'done'"
                        )
                        self.err(
                            i,
                            f"{want} does not match '{top.kind}' opened on line {top.line + 1}; expected {exp}",
                        )
                depth = len(stack)

            elif kind in ("break", "continue"):
                if not any(b.kind in LOOP_KINDS for b in stack):
                    self.err(i, f"'{kind}' outside of a loop", 2)

            elif kind == "return":
                if not any(b.kind == "fn" for b in stack):
                    self.err(i, "'return' outside of a function", 2)

            self.depths.append(depth)
            self.kinds.append(kind)

        for b in stack:
            if b.kind != "recipe":
                exp = {"parallel": "pend", "fn": "}"}.get(b.kind, "done")
                self.err(b.line, f"'{b.kind}' block is never closed (expected '{exp}')")

        # ---- cross-reference checks (skipped when other files may provide names)
        if not self.has_include:
            for q, i in self.recipes_declared.items():
                if q not in self.recipes_defined:
                    self.err(i, f"recipe '{q}' is declared but never defined", 2)
            for i, name, c0 in deferred_calls:
                if name not in BUILTINS and name not in self.fns and "." not in name:
                    self.err(i, f"unknown function '{name}'", 2, c0, c0 + len(name))
            for i, dep, mod, c0 in dep_refs:
                if not self._known_recipe(dep, mod):
                    self.err(i, f"unknown recipe '{dep}'", 2, c0, c0 + len(dep))
            for i, name, c0 in default_refs:
                if name and not self._known_recipe(name, ""):
                    self.err(
                        i,
                        f"default recipe '{name}' is not defined",
                        2,
                        c0,
                        c0 + len(name),
                    )

    def _known_recipe(self, name, mod):
        d = self.recipes_defined
        if name in d or (mod and f"{mod}.{name}" in d):
            return True
        return name in self.recipes_declared

    # ---- formatting
    def format(self, tab=2):
        out, blank = [], False
        for raw, d in zip(self.lines, self.depths):
            if d is None:
                if out and not blank:
                    out.append("")
                blank = True
                continue
            blank = False
            out.append(" " * (tab * d) + raw.strip())
        while out and out[-1] == "":
            out.pop()
        return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# LSP plumbing
# --------------------------------------------------------------------------
docs = {}
_shutdown = False


def send(obj):
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    _OUT.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    _OUT.flush()


def reply(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if error:
        msg["error"] = error
    else:
        msg["result"] = result
    send(msg)


def notify(method, params):
    send({"jsonrpc": "2.0", "method": method, "params": params})


def read_message():
    inp = sys.stdin.buffer
    length = None
    while True:
        line = inp.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            if length is not None:
                break
            continue
        k, _, v = line.decode("ascii", "replace").partition(":")
        if k.lower() == "content-length":
            length = int(v.strip())
    return json.loads(inp.read(length).decode("utf-8"))


def rng(l0, c0, l1, c1):
    return {
        "start": {"line": l0, "character": c0},
        "end": {"line": l1, "character": c1},
    }


def publish(uri):
    a = Analysis(docs[uri])
    notify(
        "textDocument/publishDiagnostics",
        {
            "uri": uri,
            "diagnostics": [
                {
                    "range": rng(l, c0, l, c1),
                    "severity": sev,
                    "source": "autobuild",
                    "message": msg,
                }
                for (l, c0, c1, sev, msg) in a.diags
            ],
        },
    )


def word_at(line, ch):
    for m in re.finditer(r"[\w.\-]+", line):
        if m.start() <= ch <= m.end():
            return m.group(0)
    return ""


def completions(text, line, ch):
    a = Analysis(text)
    cur = a.lines[line] if line < len(a.lines) else ""
    prefix = cur[:ch]
    word = re.search(r"[\w.\-]*$", prefix).group(0)
    before = prefix[: len(prefix) - len(word)]
    items = []

    def kw(label, kind, detail="", doc="", insert=None, snippet=False, sort="5"):
        it = {"label": label, "kind": kind, "sortText": sort + label}
        if detail:
            it["detail"] = detail
        if doc:
            it["documentation"] = {"kind": "markdown", "value": doc}
        if insert:
            it["insertText"] = insert
            it["insertTextFormat"] = 2 if snippet else 1
        items.append(it)

    def add_recipes():
        for q in sorted(set(a.recipes_defined) | set(a.recipes_declared)):
            kw(q, 3, "recipe", sort="1")

    def add_vars():
        for v in sorted(a.vars):
            kw(v, 6, "variable", sort="1")

    def add_builtins():
        for n, (sig, doc) in BUILTINS.items():
            kw(n, 3, sig, doc, sort="3")
        for n in a.fns:
            kw(n, 3, "fn", sort="2")

    if re.search(r"default\(\s*$", before):
        add_recipes()
    elif re.search(r"template\(\s*[\"']?$", before):
        for t in TEMPLATES:
            kw(t, 12, "template")
    elif re.search(r"\([^()]*$", before):
        add_vars()
        add_builtins()
    elif re.match(r"^\s*[A-Za-z_][\w.\-]*\s*:\s*([\w.\-]+\s+)*$", before):
        add_recipes()
    elif re.match(r"^\s*recipe\s", before):
        pass
    elif re.match(r"^\s*let\s+\S+\s*=\s*$", before):
        add_builtins()
        add_vars()
    else:
        if before.strip() == "":
            for k, doc in KEYWORDS.items():
                kw(k, 14, "keyword", doc, sort="2")
            for k, body in SNIPPETS.items():
                kw(k, 15, "snippet", insert=body, snippet=True, sort="0")
        add_builtins()
        add_vars()
    return {"isIncomplete": False, "items": items}


def hover(text, line, ch):
    a = Analysis(text)
    if line >= len(a.lines):
        return None
    w = word_at(a.lines[line], ch)
    if not w:
        return None
    short = w.split(".")[-1]
    md = None
    if short in BUILTINS and w not in a.recipes_defined:
        sig, doc = BUILTINS[short]
        md = f"```autobuild\n{sig}\n```\n{doc}"
    elif w in KEYWORDS:
        md = f"**{w}**\n\n{KEYWORDS[w]}"
    elif w in a.recipes_defined or short in a.recipes_defined:
        l = a.recipes_defined.get(w, a.recipes_defined.get(short))
        md = f"**recipe** `{w}` (line {l + 1})\n\n```autobuild\n{a.lines[l].strip()}\n```"
    elif w in a.fns:
        l = a.fns[w]
        md = f"**fn** `{w}`\n\n```autobuild\n{a.lines[l].strip()}\n```"
    elif w in a.vars:
        l = a.vars[w]
        md = f"**variable** `{w}`\n\n```autobuild\n{a.lines[l].strip()}\n```"
    return {"contents": {"kind": "markdown", "value": md}} if md else None


def definition(uri, text, line, ch):
    a = Analysis(text)
    if line >= len(a.lines):
        return None
    w = word_at(a.lines[line], ch)
    short = w.split(".")[-1]
    for table in (a.recipes_defined, a.fns, a.modules, a.vars):
        for key in (w, short):
            if key in table:
                l = table[key]
                return {"uri": uri, "range": rng(l, 0, l, len(a.lines[l]))}
    for q, l in a.recipes_defined.items():
        if q.split(".")[-1] == short:
            return {"uri": uri, "range": rng(l, 0, l, len(a.lines[l]))}
    return None


# --------------------------------------------------------------------------
# Semantic tokens (syntax colouring)
# --------------------------------------------------------------------------
TOKEN_TYPES = [
    "keyword",
    "function",
    "variable",
    "string",
    "number",
    "comment",
    "operator",
    "namespace",
    "parameter",
]
TOKEN_MODS = ["declaration", "defaultLibrary"]
T_KW, T_FN, T_VAR, T_STR, T_NUM, T_COM, T_OP, T_NS, T_PARAM = range(9)
M_DECL, M_LIB = 1, 2

_COND_WORDS = {"and", "or", "not"}
_STRUCT = {
    "if",
    "elif",
    "else",
    "while",
    "for",
    "try",
    "catch",
    "retry",
    "timeout",
    "parallel",
    "pend",
    "done",
    "break",
    "continue",
}
_VALUE_KINDS = {
    "let",
    "call",
    "if",
    "elif",
    "while",
    "for",
    "retry",
    "timeout",
    "return",
}
_RX_STR = re.compile(r"\"[^\"\n]*\"|'[^'\n]*'")
_RX_VAR = re.compile(r"\([A-Za-z_][\w.\-]*\)")
_RX_WORD = re.compile(r"[A-Za-z_][\w]*")
_RX_CALL = re.compile(r"([A-Za-z_][\w.]*)\(")
_RX_NUM = re.compile(r"(?<![\w.])[+-]?\d+(?:\.\d+)?(?![\w.])")
_RX_OP = re.compile(r"==|!=|<=|>=|<|>")


def semantic_tokens(a):
    data, prev_line, prev_col = [], 0, 0
    for i, raw in enumerate(a.lines):
        kind = a.kinds[i]
        if kind == "blank":
            continue
        n = len(raw)
        used = [False] * n
        toks = []

        def put(st, en, t, mod=0):
            if st < 0 or en > n or st >= en or any(used[st:en]):
                return
            for k in range(st, en):
                used[k] = True
            toks.append((st, en - st, t, mod))

        col0 = n - len(raw.lstrip())
        if kind == "comment":
            put(col0, len(raw.rstrip()), T_COM)
        else:
            for m in _RX_STR.finditer(raw):
                put(m.start(), m.end(), T_STR)
            for m in _RX_VAR.finditer(raw):
                if m.start() > 0 and (
                    raw[m.start() - 1].isalnum() or raw[m.start() - 1] == "_"
                ):
                    continue  # it's a call like default(build), not a (var) reference
                put(m.start(), m.end(), T_VAR)

            first = _RX_WORD.search(raw, col0)
            if kind in ("get", "let", "include", "return", "shell"):
                if first:
                    put(first.start(), first.end(), T_KW)
                nm = _RX_WORD.search(raw, first.end()) if first else None
                if kind in ("get", "let") and nm:
                    put(nm.start(), nm.end(), T_VAR, M_DECL)
                    if kind == "let":
                        eq = raw.find("=", nm.end())
                        if eq >= 0:
                            put(eq, eq + 1, T_OP)
            elif kind == "fn":
                put(first.start(), first.end(), T_KW)
                nm = _RX_WORD.search(raw, first.end())
                if nm:
                    put(nm.start(), nm.end(), T_FN, M_DECL)
                    po = raw.find("(", nm.end())
                    pc = raw.rfind(")")
                    if po >= 0 and pc > po:
                        for pm in _RX_WORD.finditer(raw, po, pc):
                            put(pm.start(), pm.end(), T_PARAM, M_DECL)
            elif kind == "module":
                put(first.start(), first.end(), T_KW)
                nm = _RX_WORD.search(raw, first.end())
                if nm:
                    put(nm.start(), nm.end(), T_NS, M_DECL)
            elif kind in _STRUCT:
                cond = kind in ("if", "elif", "while")
                for wm in _RX_WORD.finditer(raw, col0):
                    w = wm.group(0)
                    if (
                        w in KEYWORDS
                        or (cond and w in _COND_WORDS)
                        or (w == "till" and kind == "for")
                    ):
                        put(wm.start(), wm.end(), T_KW)
                if kind == "for":
                    fm = re.match(r"\s*for\s+([A-Za-z_]\w*)\s+in\b", raw)
                    if fm:
                        put(fm.start(1), fm.end(1), T_VAR, M_DECL)
                if cond:
                    for om in _RX_OP.finditer(raw):
                        put(om.start(), om.end(), T_OP)
            elif kind == "recipe_decl":
                put(first.start(), first.end(), T_KW)
                for wm in re.finditer(r"[^\s]+", raw[first.end() :]):
                    put(first.end() + wm.start(), first.end() + wm.end(), T_FN, M_DECL)
            elif kind == "recipe_def":
                colon = raw.find(":")
                put(col0, colon, T_FN, M_DECL)
                put(colon, colon + 1, T_OP)
                for wm in re.finditer(r"[^\s]+", raw[colon + 1 :]):
                    put(colon + 1 + wm.start(), colon + 1 + wm.end(), T_FN)

            if kind in _VALUE_KINDS:
                for cm in _RX_CALL.finditer(raw):
                    nm = cm.group(1)
                    put(cm.start(1), cm.end(1), T_FN, M_LIB if nm in BUILTINS else 0)
                for nm_ in _RX_NUM.finditer(raw):
                    put(nm_.start(), nm_.end(), T_NUM)
                for wm in _RX_WORD.finditer(raw):
                    if wm.group(0) in ("true", "false", "NULL"):
                        put(wm.start(), wm.end(), T_KW)

        for st, ln, t, mod in sorted(toks):
            dl = i - prev_line
            dc = st - prev_col if dl == 0 else st
            data.extend([dl, dc, ln, t, mod])
            prev_line, prev_col = i, st
    return data


def handle(msg):
    global _shutdown
    method, id_, p = msg.get("method"), msg.get("id"), msg.get("params") or {}

    if method == "initialize":
        reply(
            id_,
            {
                "capabilities": {
                    "textDocumentSync": {"openClose": True, "change": 1},
                    "completionProvider": {
                        "triggerCharacters": ["(", ".", ":", '"', " "]
                    },
                    "hoverProvider": True,
                    "definitionProvider": True,
                    "documentSymbolProvider": True,
                    "documentFormattingProvider": True,
                    "foldingRangeProvider": True,
                    "semanticTokensProvider": {
                        "legend": {
                            "tokenTypes": TOKEN_TYPES,
                            "tokenModifiers": TOKEN_MODS,
                        },
                        "full": True,
                    },
                },
                "serverInfo": {"name": "autobuild-lsp", "version": VERSION},
            },
        )
    elif method == "initialized":
        pass
    elif method == "shutdown":
        _shutdown = True
        reply(id_, None)
    elif method == "exit":
        sys.exit(0 if _shutdown else 1)
    elif method == "textDocument/didOpen":
        d = p["textDocument"]
        docs[d["uri"]] = d["text"]
        publish(d["uri"])
    elif method == "textDocument/didChange":
        uri = p["textDocument"]["uri"]
        docs[uri] = p["contentChanges"][-1]["text"]
        publish(uri)
    elif method == "textDocument/didClose":
        uri = p["textDocument"]["uri"]
        docs.pop(uri, None)
        notify("textDocument/publishDiagnostics", {"uri": uri, "diagnostics": []})
    elif method == "textDocument/completion":
        pos = p["position"]
        reply(
            id_,
            completions(
                docs.get(p["textDocument"]["uri"], ""), pos["line"], pos["character"]
            ),
        )
    elif method == "textDocument/hover":
        pos = p["position"]
        reply(
            id_,
            hover(
                docs.get(p["textDocument"]["uri"], ""), pos["line"], pos["character"]
            ),
        )
    elif method == "textDocument/definition":
        uri, pos = p["textDocument"]["uri"], p["position"]
        reply(id_, definition(uri, docs.get(uri, ""), pos["line"], pos["character"]))
    elif method == "textDocument/documentSymbol":
        a = Analysis(docs.get(p["textDocument"]["uri"], ""))
        out = []
        for s in a.symbols:
            l = s["line"]
            r = rng(l, 0, l, len(a.lines[l]))
            out.append(
                {
                    "name": s["name"],
                    "detail": s["detail"],
                    "kind": s["kind"],
                    "range": r,
                    "selectionRange": r,
                }
            )
        reply(id_, out)
    elif method == "textDocument/formatting":
        text = docs.get(p["textDocument"]["uri"], "")
        a = Analysis(text)
        tab = (p.get("options") or {}).get("tabSize", 2)
        new = a.format(tab)
        if new == text:
            reply(id_, [])
        else:
            last = len(a.lines) - 1
            reply(id_, [{"range": rng(0, 0, last, len(a.lines[last])), "newText": new}])
    elif method == "textDocument/semanticTokens/full":
        a = Analysis(docs.get(p["textDocument"]["uri"], ""))
        reply(id_, {"data": semantic_tokens(a)})
    elif method == "textDocument/foldingRange":
        a = Analysis(docs.get(p["textDocument"]["uri"], ""))
        reply(id_, [{"startLine": s, "endLine": e} for s, e in a.folds])
    elif id_ is not None:
        reply(id_, error={"code": -32601, "message": f"method not found: {method}"})


def main():
    if "--version" in sys.argv or "-v" in sys.argv:
        _OUT.write(f"autobuild-lsp {VERSION}\n".encode())
        _OUT.flush()
        return
    if "--help" in sys.argv or "-h" in sys.argv:
        _OUT.write((__doc__ or "").encode())
        _OUT.flush()
        return
    while True:
        msg = read_message()
        if msg is None:
            break
        try:
            handle(msg)
        except SystemExit:
            raise
        except Exception as e:  # never die on a single bad request
            sys.stderr.write(f"autobuild-lsp error: {e!r}\n")
            if msg.get("id") is not None and msg.get("method"):
                reply(msg["id"], error={"code": -32603, "message": str(e)})


if __name__ == "__main__":
    main()

