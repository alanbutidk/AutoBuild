#!/usr/bin/env python3
"""
module_and_parallel_checks.py

Two more things `.abuild` can't assert about itself with `shell test`,
because they're about the interpreter's internal registration/refusal
behavior rather than a runtime string value:

  1. A recipe declared inside `module X ... done` is only ever reachable
     as `X.recipe` - the bare name must NOT leak into the top-level
     namespace. There's no AutoBuild-language way to ask "is this name
     absent from recipes_declared" from inside a recipe.

  2. `parallel do` must REFUSE a `let` (or any non-shell/command) child
     rather than silently running it in a subprocess where the result
     would vanish. Proving this needs catching the specific SystemExit
     and checking its message - not something `shell test` can do.

Run directly:  python3 module_and_parallel_checks.py
Exits 0 if every check passes, non-zero (with a message) otherwise.
"""

import sys

from old_autobuild import Tokenize, Parser, Executor, Optimizer


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" - {detail}" if detail and not condition else ""))
    return condition


def main():
    results = []

    # 1. Module namespacing: bare name must be absent, qualified name present.
    src = """
module pkg
  recipe build
  build:
    echo building
done
"""
    ast = Parser(Tokenize(src).GetTokens()).GetAST()
    ex = Executor(Optimizer(ast).GetAST())
    results.append(
        check(
            "qualified name 'pkg.build' is declared",
            "pkg.build" in ex.recipes_declared,
            f"declared: {ex.recipes_declared}",
        )
    )
    results.append(
        check(
            "bare name 'build' is NOT declared at top level (module isolation)",
            "build" not in ex.recipes_declared,
            f"declared: {ex.recipes_declared}",
        )
    )
    try:
        ex.RunRecipe("build")
        results.append(check("RunRecipe('build') should have raised", False))
    except SystemExit as e:
        results.append(
            check(
                "RunRecipe('build') correctly raises 'Unknown recipe'",
                "Unknown recipe" in str(e),
                str(e),
            )
        )

    # 2. Two independent modules don't collide even if they declare the
    #    same recipe name.
    src2 = """
module a
  recipe build
  build:
    echo building A
done
module b
  recipe build
  build:
    echo building B
done
"""
    ast2 = Parser(Tokenize(src2).GetTokens()).GetAST()
    ex2 = Executor(Optimizer(ast2).GetAST())
    results.append(
        check(
            "two modules can each declare their own 'build' without colliding",
            "a.build" in ex2.recipes_declared and "b.build" in ex2.recipes_declared,
            f"declared: {ex2.recipes_declared}",
        )
    )

    # 3. `parallel do` must refuse a `let` child - not silently drop it,
    #    not silently run it in a way that loses the result.
    src3 = """
recipe bad
bad:
  parallel do
    let x = 5
  pend
"""
    ast3 = Parser(Tokenize(src3).GetTokens()).GetAST()
    ex3 = Executor(Optimizer(ast3).GetAST())
    try:
        ex3.RunRecipe("bad")
        results.append(
            check("parallel do with a 'let' child should have raised", False)
        )
    except SystemExit as e:
        results.append(
            check(
                "parallel do correctly refuses a 'let' child",
                "only supports shell/command" in str(e),
                str(e),
            )
        )

    # 4. `parallel do` still works normally when every child IS shell/command
    #    (make sure the guard isn't over-eager).
    src4 = """
recipe ok
ok:
  parallel do
    shell echo one
    shell echo two
  pend
"""
    ast4 = Parser(Tokenize(src4).GetTokens()).GetAST()
    ex4 = Executor(Optimizer(ast4).GetAST())
    try:
        ex4.RunRecipe("ok")
        results.append(
            check("parallel do with only shell children runs without error", True)
        )
    except SystemExit as e:
        results.append(
            check(
                "parallel do with only shell children runs without error",
                False,
                str(e),
            )
        )

    print()
    if all(results):
        print(f"module_and_parallel_checks.py: all {len(results)} checks passed.")
        return 0
    else:
        failed = len(results) - sum(results)
        print(
            f"module_and_parallel_checks.py: {failed}/{len(results)} check(s) FAILED."
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
