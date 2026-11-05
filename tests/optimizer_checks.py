#!/usr/bin/env python3
"""
optimizer_checks.py

AutoBuild's `.abuild` language has no way to inspect its own AST or the
Optimizer's fold/eliminate counters from inside a recipe - those are
properties of the interpreter's internals, not runtime values a script
can assert on with `shell test`. This script covers exactly that gap,
as a companion to `test.all` in .abuild.

Run directly:  python3 optimizer_checks.py
Exits 0 if every check passes, non-zero (with a message) otherwise.
"""
import sys

from autobuild import Tokenize, Parser, Optimizer


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" - {detail}" if detail and not condition else ""))
    return condition


def main():
    results = []

    # 1. A provably-true `if` branch is folded: the `if` node disappears
    #    from the AST entirely and its body is inlined in place - not just
    #    "produces the same output", but the AST node itself is gone.
    src_true = """
let OS = linux
if OS==windows then
  echo dead branch
elif OS==linux then
  echo alive branch
else then
  echo also dead
done
"""
    ast = Parser(Tokenize(src_true).GetTokens()).GetAST()
    opt = Optimizer(ast)
    out_types = [n.type for n in opt.GetAST()]
    results.append(
        check(
            "constant-true if-chain is folded away (no 'if' node remains)",
            "if" not in out_types,
            f"got node types: {out_types}",
        )
    )
    results.append(
        check(
            "the winning branch's command was inlined",
            "command" in out_types,
            f"got node types: {out_types}",
        )
    )
    results.append(
        check(
            "optimizer's own stats report at least 2 eliminated dead branches",
            opt.stats["eliminated"] >= 2,
            f"stats: {opt.stats}",
        )
    )

    # 2. Every branch provably false, no else: the whole `if` disappears,
    #    leaving NO trace (not even an inlined command) - dead code, gone.
    src_false = """
let OS = linux
if OS==windows then
  echo never runs
elif OS==darwin then
  echo also never runs
done
"""
    ast2 = Parser(Tokenize(src_false).GetTokens()).GetAST()
    opt2 = Optimizer(ast2)
    out_types2 = [n.type for n in opt2.GetAST()]
    results.append(
        check(
            "if-chain with every branch false and no else vanishes completely",
            "if" not in out_types2 and "command" not in out_types2,
            f"got node types: {out_types2}",
        )
    )

    # 3. A condition on a variable whose value isn't known until runtime
    #    (here: it's never assigned via let/get at all before the if) is
    #    left alone - the optimizer must not guess.
    src_unresolvable = """
if RUNTIME_ONLY_VAR==x then
  echo maybe
else then
  echo maybe not
done
"""
    ast3 = Parser(Tokenize(src_unresolvable).GetTokens()).GetAST()
    opt3 = Optimizer(ast3)
    out_types3 = [n.type for n in opt3.GetAST()]
    results.append(
        check(
            "if on a var with no known value at optimize-time is left untouched",
            "if" in out_types3,
            f"got node types: {out_types3}",
        )
    )

    print()
    if all(results):
        print(f"optimizer_checks.py: all {len(results)} checks passed.")
        return 0
    else:
        failed = len(results) - sum(results)
        print(f"optimizer_checks.py: {failed}/{len(results)} check(s) FAILED.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
