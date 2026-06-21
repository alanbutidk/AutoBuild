import subprocess
import os
import sys
from enum import Enum, auto

# Token Types
class _TokenType(Enum):
	"""This is a important internal token declare class
	NOT MEANT FOR PUBLIC USAGE"""
	COMMENT	   = auto()	 # # ...
	GET		   = auto()	 # get
	LET		   = auto()	 # let
	IF		   = auto()	 # if
	ELIF	   = auto()	 # elif
	ELSE	   = auto()	 # else
	THEN	   = auto()	 # then
	DONE	   = auto()	 # done
	FOR		   = auto()	 # for
	FOR_TILL   = auto()	 # for till(n)
	FOR_IN	   = auto()	 # for x in (LIST)
	DO		   = auto()	 # do
	SHELL	   = auto()	 # shell
	RECIPE	   = auto()	 # recipe declaration
	RECIPE_DEF = auto()	 # recipe definition (name:)
	DEPENDS	   = auto()	 # recipe dependencies
	IDENTIFIER = auto()	 # variable name / value
	ASSIGN	   = auto()	 # =
	OPERATOR   = auto()	 # == != > < >= <=
	VAR_REF	   = auto()	 # (VARNAME)
	STRING	   = auto()	 # raw string / command
	COLON	   = auto()	 # :
	NEWLINE	   = auto()	 # line separator
	NULL	   = auto()	 # NULL value from missing env var
	EOF		   = auto()	 # end of file

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

	def _parse_var_refs(self, text: str) -> list:
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
			tokens.append(("VAR_REF", text[start+1:end]))
			text = text[end+1:]
		return tokens

	def _parse_condition(self, condition: str) -> tuple:
		for op in [">=", "<=", "!=", "==", ">", "<"]:
			if op in condition:
				left, right = condition.split(op, 1)
				return left.strip(), op, right.strip()
		return condition.strip(), None, None

	def _tokenize(self):
		for raw_line in self.lines:
			stripped = raw_line.strip()

			if not stripped:
				self.line += 1
				continue

			# Comment
			if stripped.startswith("#"):
				self.tokens.append(Token(_TokenType.COMMENT, stripped[1:].strip(), self.line))

			# get
			elif stripped.startswith("get "):
				var_name = stripped[4:].strip()
				val = os.environ.get(var_name)
				if val is None:
					print(f"WARNING: Environmental variable [{var_name}] is not on the runner PC")
					self.tokens.append(Token(_TokenType.GET, var_name, self.line))
					self.tokens.append(Token(_TokenType.NULL, "NULL", self.line))
				else:
					self.tokens.append(Token(_TokenType.GET, var_name, self.line))
					self.tokens.append(Token(_TokenType.IDENTIFIER, val, self.line))

			# let
			elif stripped.startswith("let "):
				rest = stripped[4:]
				name, _, value = rest.partition("=")
				self.tokens.append(Token(_TokenType.LET, name.strip(), self.line))
				self.tokens.append(Token(_TokenType.ASSIGN, "=", self.line))
				for kind, val in self._parse_var_refs(value.strip()):
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

			# done
			elif stripped == "done":
				self.tokens.append(Token(_TokenType.DONE, "done", self.line))

			# for till(n)
			elif stripped.startswith("for till("):
				num = stripped[9:stripped.index(")")].strip()
				self.tokens.append(Token(_TokenType.FOR, "for", self.line))
				self.tokens.append(Token(_TokenType.FOR_TILL, num, self.line))
				self.tokens.append(Token(_TokenType.DO, "do", self.line))

			# for x in (LIST)
			elif stripped.startswith("for ") and " in " in stripped and stripped.endswith("do"):
				rest = stripped[4:-2].strip()
				var, _, list_ref = rest.partition(" in ")
				self.tokens.append(Token(_TokenType.FOR, "for", self.line))
				self.tokens.append(Token(_TokenType.IDENTIFIER, var.strip(), self.line))
				self.tokens.append(Token(_TokenType.FOR_IN, list_ref.strip().strip("()"), self.line))
				self.tokens.append(Token(_TokenType.DO, "do", self.line))

			# shell
			elif stripped.startswith("shell "):
				cmd = stripped[6:].strip()
				self.tokens.append(Token(_TokenType.SHELL, "", self.line))
				for kind, val in self._parse_var_refs(cmd):
					self.tokens.append(Token(_TokenType[kind], val, self.line))

			# recipe declaration
			elif stripped.startswith("recipe "):
				self.tokens.append(Token(_TokenType.RECIPE, stripped[7:].strip(), self.line))

			# recipe definition (name: dep1 dep2)
			elif ":" in stripped and not stripped.startswith(" ") and not stripped.startswith("\t"):
				name, _, deps = stripped.partition(":")
				self.tokens.append(Token(_TokenType.RECIPE_DEF, name.strip(), self.line))
				self.tokens.append(Token(_TokenType.COLON, ":", self.line))
				if deps.strip():
					self.tokens.append(Token(_TokenType.DEPENDS, deps.strip(), self.line))

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
			raise SyntaxError(f"[AutoBuild] Line {t.line}: Expected {type.name}, got {t.type.name} ({repr(t.value)})")
		return self._advance()

	def _parse_string_sequence(self) -> str:
		"""Reads STRING and VAR_REF tokens into a single resolved string fragment list."""
		parts = []
		while self._current().type in (_TokenType.STRING, _TokenType.VAR_REF):
			t = self._advance()
			parts.append((t.type.name, t.value))
		return parts
	def _parse_body(self, inside_block=False) -> list:
		"""Reads indented body lines until DONE/ELIF/ELSE (inside block) or RECIPE_DEF/RECIPE/EOF (top level)."""
		body = []
		self._skip_newlines()
		while True:
			t = self._current()
			if t.type == _TokenType.EOF:
				break
			if inside_block and t.type in (_TokenType.DONE, _TokenType.ELIF, _TokenType.ELSE):
				break
			if not inside_block and t.type in (_TokenType.RECIPE_DEF, _TokenType.RECIPE):
				break
			node = self._parse_statement()
			if node:
				body.append(node)
			self._skip_newlines()
		return body

	def _parse_if(self) -> ASTNode:
		branches = []

		left = self._advance().value
		op = self._expect(_TokenType.OPERATOR).value
		right = self._expect(_TokenType.IDENTIFIER).value
		self._expect(_TokenType.THEN)
		self._skip_newlines()
		body = self._parse_body(inside_block=True)
		branches.append({"condition": (left, op, right), "body": body})

		while self._current().type == _TokenType.ELIF:
			left = self._advance().value
			op = self._expect(_TokenType.OPERATOR).value
			right = self._expect(_TokenType.IDENTIFIER).value
			self._expect(_TokenType.THEN)
			self._skip_newlines()
			body = self._parse_body(inside_block=True)
			branches.append({"condition": (left, op, right), "body": body})

		if self._current().type == _TokenType.ELSE:
			self._advance()	 # ELSE
			self._advance()	 # THEN
			self._skip_newlines()
			body = self._parse_body(inside_block=True)
			branches.append({"condition": None, "body": body})

		self._expect(_TokenType.DONE)
		return ASTNode("if", branches=branches)

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

	def _parse_statement(self) -> ASTNode:
		self._skip_newlines()
		t = self._current()

		if t.type == _TokenType.COMMENT:
			self._advance()
			return None

		elif t.type == _TokenType.GET:
			name = self._advance().value
			val_tok = self._advance()
			return ASTNode("get", name=name, value=val_tok.value, is_null=val_tok.type == _TokenType.NULL)

		elif t.type == _TokenType.LET:
			name = self._advance().value
			self._expect(_TokenType.ASSIGN)
			parts = self._parse_string_sequence()
			return ASTNode("let", name=name, parts=parts)

		elif t.type == _TokenType.IF:
			return self._parse_if()

		elif t.type == _TokenType.FOR:
			self._advance()	 # consume FOR
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

		elif t.type == _TokenType.STRING:
			parts = self._parse_string_sequence()
			return ASTNode("command", parts=parts)

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

	def GetAST(self) -> list:
		return self.ast
class Executor:
	"""Walks the AST produced by Parser and executes each node."""
	def __init__(self, ast: list):
		self.ast = ast
		self.vars = {}
		self.recipes_declared = []
		self.recipes_defined = {}
		self._collect_recipes()
		self._execute(self.ast)

	def _collect_recipes(self):
		"""Pre-pass to register all declared recipes before execution."""
		for node in self.ast:
			if node.type == "recipe_decl":
				if node.data["name"] not in self.recipes_declared:
					self.recipes_declared.append(node.data["name"])

	def _resolve(self, parts: list) -> str:
		"""Resolves a list of (STRING, VAR_REF) parts into a final string."""
		result = ""
		for kind, val in parts:
			if kind == "VAR_REF":
				resolved = self.vars.get(val, None)
				if resolved is None:
					print(f"WARNING: Variable [{val}] is not defined")
					result += "NULL"
				else:
					result += resolved
			else:
				result += val
		return result

	def _eval_condition(self, left: str, op: str, right: str) -> bool:
		"""Evaluates a condition, attempting numeric comparison first."""
		lv = self.vars.get(left, left)
		rv = self.vars.get(right, right)
		try:
			lv_n, rv_n = float(lv), float(rv)
			return {
				"==": lv_n == rv_n,
				"!=": lv_n != rv_n,
				">":  lv_n >  rv_n,
				"<":  lv_n <  rv_n,
				">=": lv_n >= rv_n,
				"<=": lv_n <= rv_n,
			}[op]
		except (ValueError, TypeError):
			return {
				"==": lv == rv,
				"!=": lv != rv,
				">":  lv >	rv,
				"<":  lv <	rv,
				">=": lv >= rv,
				"<=": lv <= rv,
			}[op]

	def _run_command(self, cmd: str):
		"""Runs a shell command, streaming output live."""
		print(f"\n>> {cmd}\n")
		result = subprocess.run(cmd, shell=True)
		if result.returncode != 0:
			raise SystemExit(f"[AutoBuild] Command failed with exit code {result.returncode}:\n	 {cmd}")

	def _execute_node(self, node) -> None:
		if node.type == "get":
			self.vars[node.data["name"]] = node.data["value"]

		elif node.type == "let":
			self.vars[node.data["name"]] = self._resolve(node.data["parts"])

		elif node.type == "shell":
			cmd = self._resolve(node.data["parts"])
			self._run_command(cmd)

		elif node.type == "command":
			cmd = self._resolve(node.data["parts"])
			self._run_command(cmd)

		elif node.type == "if":
			for branch in node.data["branches"]:
				condition = branch["condition"]
				if condition is None:
					# else branch
					self._execute(branch["body"])
					break
				left, op, right = condition
				if self._eval_condition(left, op, right):
					self._execute(branch["body"])
					break

		elif node.type == "for_till":
			count = node.data["count"]
			for _ in range(count):
				self._execute(node.data["body"])

		elif node.type == "for_in":
			var = node.data["var"]
			list_ref = node.data["list_ref"]
			items = self.vars.get(list_ref, "").split()
			for item in items:
				self.vars[var] = item
				self._execute(node.data["body"])
			# clean up loop var
			self.vars.pop(var, None)

		elif node.type == "recipe_decl":
			pass  # already handled in _collect_recipes

		elif node.type == "recipe_def":
			name = node.data["name"]
			if name not in self.recipes_declared:
				raise SyntaxError(f"[AutoBuild] Recipe '{name}' was not declared before definition")
			self.recipes_defined[name] = node

	def _execute(self, nodes: list):
		for node in nodes:
			if node:
				self._execute_node(node)

	def RunRecipe(self, name: str):
		"""Runs a recipe by name, resolving dependencies first."""
		if name not in self.recipes_declared:
			raise SystemExit(f"[AutoBuild] Unknown recipe '{name}'")
		if name not in self.recipes_defined:
			raise SystemExit(f"[AutoBuild] Recipe '{name}' was declared but never defined")
		node = self.recipes_defined[name]
		for dep in node.data["deps"]:
			self.RunRecipe(dep)
		print(f"[AutoBuild] Running recipe: {name}")
		self._execute(node.data["body"])
if __name__ == "__main__":
	from arghandle import ArgHandle
	cli = ArgHandle()
	cli.ProgramName("AutoBuild")

	cli.PrintOnNoArgs("No arguments provided. Use --help or -h for usage.", Exit=True)

	# Handle version before parsing .abuild
	if cli.IsArgInActualArgs("--version") or cli.IsArgInActualArgs("-v"):
		raise SystemExit("AutoBuild v1.0.0\n")

	# Handle help before parsing .abuild
	if cli.IsArgInActualArgs("--help") or cli.IsArgInActualArgs("-h"):
		# Register help and version only, no recipes yet
		cli.RegisterToHelp("version", ["--version", "-v"], HelpMsg="Prints the version and exit")
		cli.HandleHelp()

	# Only now load and parse .abuild
	if not __import__("pathlib").Path(".abuild").exists():
		raise SystemExit("[AutoBuild] No .abuild file found in current directory.\n")
	try:
		source = open(".abuild").read()
		tokens = Tokenize(source).GetTokens()
		ast = Parser(tokens).GetAST()
		executor = Executor(ast)
		#print(f"[DEBUG]: Recipes declared; {executor.recipes_declared}")
	except Exception as e:
		raise SystemExit(f"Error while running file: {e}\n")
	# Match and run recipe
	arg = sys.argv[1] if len(sys.argv) > 1 else None
	try:
		if arg and arg in executor.recipes_declared:
			executor.RunRecipe(arg)
		else:
			raise SystemExit(f"[AutoBuild] Unknown argument. Use --help or -h for usage.\n")
	except Exception as e:
		raise SystemExit(f"Error while running file: {e}\n")
