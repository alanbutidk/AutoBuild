# AutoBuild

**AutoBuild** is a build system with a small scripting language built in, written in [Python](https://python.org) (single file, `autobuild.py`, v3.1.0).

No `.PHONY` games like in _Make_. You declare recipes, write their logic, and run them. Build files are called `.abuild` and live in the project root.

---

## Features

- Recipes with dependencies, every recipe runs at most once per invocation
- Typed values: string, int, float, bool, NULL and lists
- `if` / `elif` / `else`, `for till(n)`, `for x in (LIST)`, `while`, `break`, `continue`
- `try` / `catch` (or `on_fail`), `retry N every S do`, `timeout S do`
- Functions (`fn`) and modules (`module Name ... done`, call recipes as `Name.recipe`)
- `parallel do ... pend` blocks and `--jobs` for running independent recipes at once
- Content-hash incremental builds with `changed()` (touching a file without editing it does not rebuild)
- Binary snapshots with `incremental()`, `versions()` and `restore()`
- Optimizer that folds constants and drops dead branches before anything runs (`--explain` shows what it did)
- Dependency graph output as tree, DOT, Mermaid or run order
- Built-in templates for C, C++, Python, Node, Go and Rust
- Watch mode, dry runs, run history and `compile_commands.json` generation
- `include "other.abuild"` with cycle detection

---

## Install

You need Python 3 and the [`arghandle`](https://github.com/alanbutidk/arghandle) package (used for the CLI).

```
git clone https://github.com/alanbutidk/autobuild
cd autobuild
pip install arghandle
python autobuild.py --help
```

To get a standalone binary, AutoBuild builds itself (via Nuitka):

```
python autobuild.py build
```

or with the included makefile: `make build`.

---

## Usage

```
autobuild [RECIPE] [options] [-- recipe args]
```

Plain `autobuild` runs the recipe set with `default(NAME)`, or lists the recipes if there is none.

| Option | What it does |
| --- | --- |
| `--help`, `-h` | Print help |
| `--version`, `-v` | Print version |
| `--list`, `-l` | List recipes with descriptions, default one marked |
| `--dry-run`, `-n` | Show what would run, run nothing, leave no history |
| `--jobs N\|auto`, `-j N` | Run up to N independent recipes at once (also `jobs(n)` in `.abuild`) |
| `--var NAME=VALUE`, `-D NAME=VALUE`, `NAME=VALUE` | Set a variable like make does, beats `let` |
| `--force` | Ignore the incremental cache, `changed()` always returns true |
| `--explain` | Print what the optimizer folded or eliminated |
| `--watch`, `-w` | Re-run when files change (`--watch-path P`, `--watch-interval S`) |
| `--graph [tree\|dot\|mermaid\|order] [RECIPE]` | Print the dependency graph, never runs anything |
| `--history [N]` | Show the last N runs (default 10) |
| `--log-file PATH` | Use PATH instead of `.abuild.log` |
| `--templates [NAME]` | List templates, or show one |
| `--init NAME [key=value ...]` | Create a `.abuild` from a template |
| `--expand` | Print the `.abuild` after includes and templates are expanded |
| `--compdb` | Write `compile_commands.json` from the C/C++ templates |
| `--profile NAME` | Build profile for C/C++/Rust templates (`debug`, `release`, `asan`, `tsan`), also the variable `(profile)` |
| `--repl`, `-r` | Start the REPL (experimental) |
| `-- ARGS...` | Everything after `--` goes to your recipes via `args()`, `flag(NAME)` and `option(NAME, default)` |

Examples:

```
autobuild test
autobuild pkg.build
autobuild all -j 4
autobuild build --dry-run
autobuild build --profile asan
autobuild test --var FILTER=parser -- -k fast
autobuild build --watch
autobuild --graph mermaid
autobuild --init CXX-Basic-Incremental
```

---

## A simple example

`.abuild`:

```
get OS
get CC

let CFLAGS = -Wall -O2 -g
let PROGRAM = Test

if OS==Windows_NT then
  let executable = .exe
  let RM = del /f
else then
  let executable =
  let RM = rm -rf
done

recipe clean
recipe compile
recipe link
recipe build
recipe run
recipe repeat

default(build)

clean:
  shell (RM) (PROGRAM)(executable) *.o

compile:
  for file in (FILES) do
    (CC) (CFLAGS) -c (file).c -o (file).o
  done

link: compile
  (CC) -o (PROGRAM)(executable) main.o utils.o parser.o lexer.o

build: clean link
  shell echo Build complete: (PROGRAM)(executable)

run: build
  shell (PROGRAM)(executable)

repeat:
  for till(3) do
    shell echo AutoBuild is working!
  done
```

Run a recipe with `autobuild compile`. The OS value is `Windows_NT` on Windows.

---

## Language notes

**Variables.** `let name = value` sets one, `(name)` reads it. Inside a larger piece of text it is interpolated as text, on its own it keeps its type. Bare words are strings, `42` is an int, `3.14` a float, `true`/`false` bools, `NULL` is NULL, quotes force a string and `[1, two, (x)]` is a list. `get NAME` pulls an environment variable.

**Functions and modules.**

```
module pkg
  fn tag(name) {
    let t = v-(name)
    return (t)
  }

  recipe pack
  pack:
    let v = tag(1.0.0)
    shell test "(v)" = "v-1.0.0"
done
```

Run it with `autobuild pkg.pack`. A function with no `return` gives an empty string.

**Parallel blocks.**

```
build:
  parallel do
    gcc -c a.c
    gcc -c b.c
  pend
```

**Incremental builds.**

```
let c = changed(app, src/*.c include/*.h, build/app)
if c == true then
  gcc src/*.c -o build/app
done
```

`changed(key, inputs, outputs)` hashes the content of every input and compares it to the digest stored in `.abuild.cache`. It returns true on a first build, when an input was added, removed, renamed or edited, when a listed output is missing, or with `--force`. The new digest is only committed after the recipe succeeds, so a half-finished build gets rebuilt next time.

---

## Builtins

| Area | Functions |
| --- | --- |
| Values | `len` `type` `int` `float` `str` `bool` `add` `sub` `mul` `div` `mod` |
| Text and lists | `upper` `lower` `trim` `replace` `split` `join` `append` `at` `contains` `lines` |
| Paths | `basename` `dirname` `ext` `stem` `abspath` `pathjoin` `cwd` `exists` `isdir` `isfile` `which` `glob` |
| Files | `mkdir` `rm` `cp` `mv` `touch` `readtext` `writetext` `appendtext` `openfile` `writefile` `readfile` `closefile` |
| Running things | `capture` `status` `getenv` `platform` `exe` `print` `fail` |
| Arguments | `getargv` `getfullargv` `args` `flag` `option` |
| Build control | `default` `describe` `jobs` `get_proc` `changed` `incremental` `versions` `restore` `depfile` `stamp` `compdb` |

---

## Templates

Start a project with `autobuild --init NAME`, or use one inside a `.abuild` with `template("NAME", key=value)`.

| Template | What you get |
| --- | --- |
| `C-Basic`, `CXX-Basic` | Compile everything under `src/` into one binary |
| `C-Basic-Incremental`, `CXX-Basic-Incremental` | One recipe per source file, only changed sources, headers (tracked via depfiles) or flags are recompiled, then relinked |
| `Python-Basic` | venv, pip install, pytest, run |
| `Python-Basic-Incremental` | Same, but pip and pytest only run when requirements or sources change |
| `Node-Basic` | npm/yarn/pnpm install only when `package.json` or the lockfile changes, then build and test |
| `Go-Basic` | build, vet, test, run |
| `Rust-Basic` | cargo build, test, run, clean |

See `autobuild --templates NAME` for the parameters of each one.

---

## Files AutoBuild creates

- `.abuild.cache`: digests for `changed()`
- `.abuild.log`: run history, shown with `--history`
- `.abuild.history`: snapshots from `incremental()`

---

## License

GPL3, see `LICENSE`.