"""pairwise-command-system - CommandSystem

A console command system with:

- Command registration with parameter definitions, aliases and permissions
- Command parsing with quote handling, escaping and auto-completion
- Command execution with return values, timeouts and history
- Variable resolution with scoping, typing and persistence
- Script execution with control flow, functions and error handling
"""

from __future__ import annotations

import json
import os
import shlex
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


PERMISSION_LEVELS = {"guest": 0, "user": 1, "admin": 2}


@dataclass
class Param:
    """Definition of a single command parameter."""

    name: str
    type: type = str
    required: bool = True
    default: Any = None

    def convert(self, raw: str) -> Any:
        if self.type is bool:
            return raw.lower() in ("1", "true", "yes", "on")
        return self.type(raw)


@dataclass
class Command:
    """A registered command."""

    name: str
    func: Optional[Callable] = None
    params: list = field(default_factory=list)
    aliases: list = field(default_factory=list)
    permission: str = "guest"
    help: str = ""


@dataclass
class ParsedCommand:
    """Result of parsing a command line."""

    name: Optional[str]
    args: list = field(default_factory=list)


@dataclass
class CommandResult:
    """Result of executing a command."""

    success: bool
    value: Any = None
    error: Optional[str] = None


@dataclass
class Variable:
    """A typed variable."""

    name: str
    value: Any
    type: type = str

    def __post_init__(self):
        if not isinstance(self.value, self.type):
            self.value = self.type(self.value)


@dataclass
class ScriptResult:
    """Result of running a script."""

    success: bool
    results: list = field(default_factory=list)
    errors: list = field(default_factory=list)


class CommandSystem:
    def __init__(self, storage_path: Optional[str] = None):
        self.state = {}
        self.commands: dict[str, Command] = {}
        self.aliases: dict[str, str] = {}
        self.history: list[str] = []
        self._history_index = 0
        # scopes[0] is the global scope; later entries are local scopes.
        self.scopes: list[dict[str, Variable]] = [{}]
        self.user_roles: dict[str, str] = {}
        self.storage_path = storage_path or os.path.join(
            os.path.expanduser("~"), ".command_system_vars.json"
        )
        self._load_variables()

    # ------------------------------------------------------------------
    # Registration: parameter definitions, aliases, permissions
    # ------------------------------------------------------------------
    def register_command(
        self,
        name: Optional[str] = None,
        func: Optional[Callable] = None,
        params: Optional[list] = None,
        aliases: Optional[list] = None,
        permission: str = "guest",
        help: str = "",
    ):
        """Register a command with parameter definitions, aliases and a
        required permission level. Called with no name, returns the registry."""
        if name is None:
            return self.commands
        if permission not in PERMISSION_LEVELS:
            raise ValueError(f"unknown permission level: {permission}")
        param_defs = [
            p if isinstance(p, Param) else Param(**p) if isinstance(p, dict) else Param(str(p))
            for p in (params or [])
        ]
        command = Command(
            name=name,
            func=func,
            params=param_defs,
            aliases=list(aliases or []),
            permission=permission,
            help=help,
        )
        self.commands[name] = command
        for alias in command.aliases:
            self.aliases[alias] = name
        return command

    # ------------------------------------------------------------------
    # Parsing: quotes, escaping, auto-completion
    # ------------------------------------------------------------------
    def parse_command(self, line: str = "") -> ParsedCommand:
        """Parse a command line, honoring quotes and escape sequences so
        arguments containing spaces stay together."""
        lexer = shlex.shlex(line, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        try:
            tokens = list(lexer)
        except ValueError:
            # Unbalanced quote: fall back to a best-effort split.
            tokens = line.split()
        if not tokens:
            return ParsedCommand(name=None, args=[])
        return ParsedCommand(name=tokens[0], args=tokens[1:])

    def complete(self, prefix: str = "") -> list:
        """Auto-complete a partially typed command name or alias."""
        names = set(self.commands) | set(self.aliases)
        return sorted(n for n in names if n.startswith(prefix))

    # ------------------------------------------------------------------
    # Execution: return values, timeouts, history
    # ------------------------------------------------------------------
    def execute_command(
        self,
        line: str = "",
        user: str = "admin",
        timeout: Optional[float] = 5.0,
    ) -> CommandResult:
        """Execute a command line and return a CommandResult describing
        success or failure. Commands run with a timeout and every executed
        line is recorded in the history."""
        parsed = self.parse_command(line)
        if parsed.name is None:
            return CommandResult(success=False, error="empty command")

        self.history.append(line)
        self._history_index = len(self.history)

        name = self.aliases.get(parsed.name, parsed.name)
        command = self.commands.get(name)
        if command is None:
            suggestions = self.complete(parsed.name[:1])
            return CommandResult(
                success=False,
                error=f"unknown command: {parsed.name}",
                value={"suggestions": suggestions},
            )

        user_level = PERMISSION_LEVELS.get(self.user_roles.get(user, "admin"), 0)
        required_level = PERMISSION_LEVELS[command.permission]
        if user_level < required_level:
            return CommandResult(
                success=False,
                error=f"permission denied: '{name}' requires {command.permission}",
            )

        try:
            args = self._bind_args(command, parsed.args)
        except (ValueError, TypeError) as exc:
            return CommandResult(success=False, error=f"bad arguments: {exc}")

        if command.func is None:
            return CommandResult(success=True, value=None)

        outcome: dict[str, Any] = {}

        def _run():
            try:
                outcome["value"] = command.func(*args)
            except Exception as exc:  # noqa: BLE001 - reported to caller
                outcome["error"] = str(exc)

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            return CommandResult(success=False, error=f"timeout after {timeout}s")
        if "error" in outcome:
            return CommandResult(success=False, error=outcome["error"])
        return CommandResult(success=True, value=outcome.get("value"))

    def _bind_args(self, command: Command, raw_args: list) -> list:
        bound = []
        for index, param in enumerate(command.params):
            if index < len(raw_args):
                bound.append(param.convert(raw_args[index]))
            elif param.required:
                raise ValueError(f"missing required argument: {param.name}")
            else:
                bound.append(param.default)
        bound.extend(raw_args[len(command.params):])
        return bound

    def history_back(self) -> Optional[str]:
        """Move up through the history (up-arrow behavior)."""
        if not self.history or self._history_index <= 0:
            return None
        self._history_index -= 1
        return self.history[self._history_index]

    def history_forward(self) -> Optional[str]:
        """Move down through the history (down-arrow behavior)."""
        if self._history_index >= len(self.history):
            return None
        self._history_index += 1
        if self._history_index == len(self.history):
            return ""
        return self.history[self._history_index]

    # ------------------------------------------------------------------
    # Variables: scoping, typing, persistence
    # ------------------------------------------------------------------
    def push_scope(self):
        """Enter a new local variable scope."""
        self.scopes.append({})

    def pop_scope(self):
        """Leave the current local variable scope."""
        if len(self.scopes) > 1:
            self.scopes.pop()

    def set_variable(self, name: str, value: Any, scope: str = "local"):
        """Set a typed variable in the local or global scope. Global
        variables are persisted to disk."""
        var = Variable(name=name, value=value, type=type(value))
        target = self.scopes[0] if scope == "global" else self.scopes[-1]
        target[name] = var
        if scope == "global":
            self._save_variables()
        return var

    def resolve_variable(self, name: Optional[str] = None, default: Any = None):
        """Resolve a variable by walking the scope chain from the innermost
        local scope out to the global scope. Called with no name, returns all
        currently visible variables."""
        if name is None:
            visible = {}
            for scope in self.scopes:
                visible.update({k: v.value for k, v in scope.items()})
            return visible
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name].value
        return default

    def _save_variables(self):
        data = {
            name: {"value": var.value, "type": var.type.__name__}
            for name, var in self.scopes[0].items()
        }
        try:
            with open(self.storage_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        except OSError:
            pass

    def _load_variables(self):
        types = {"str": str, "int": int, "float": float, "bool": bool, "list": list, "dict": dict}
        try:
            with open(self.storage_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        for name, spec in data.items():
            var_type = types.get(spec.get("type", "str"), str)
            self.scopes[0][name] = Variable(name=name, value=spec.get("value"), type=var_type)

    # ------------------------------------------------------------------
    # Scripts: control flow, functions, error handling
    # ------------------------------------------------------------------
    def run_script(self, script: str = "", user: str = "admin") -> ScriptResult:
        """Run a multi-line script. Supports ``if/else/endif``,
        ``for ... in a..b`` / ``endfor``, ``while`` / ``endwhile`` and
        ``def name(args)`` / ``enddef`` function definitions. A failing line
        is recorded in ``errors`` and does not stop the script."""
        result = ScriptResult(success=True)
        lines = [ln.strip() for ln in script.splitlines()]
        self._run_block(lines, 0, len(lines), result, user)
        result.success = not result.errors
        return result

    def _run_block(self, lines, start, end, result, user):
        index = start
        while index < end:
            line = lines[index]
            index += 1
            if not line or line.startswith("#"):
                continue
            keyword = line.split(None, 1)[0].lower()

            if keyword == "if":
                block_end, else_at = self._find_block(lines, index - 1, end, "if", "endif")
                condition = line[2:].strip()
                if self._eval_condition(condition):
                    self._run_block(lines, index, else_at or block_end, result, user)
                elif else_at is not None:
                    self._run_block(lines, else_at + 1, block_end, result, user)
                index = block_end + 1
            elif keyword == "for":
                block_end, _ = self._find_block(lines, index - 1, end, "for", "endfor")
                var_name, iterable = self._parse_for(line)
                for value in iterable:
                    self.set_variable(var_name, value)
                    self._run_block(lines, index, block_end, result, user)
                index = block_end + 1
            elif keyword == "while":
                block_end, _ = self._find_block(lines, index - 1, end, "while", "endwhile")
                condition = line[5:].strip()
                guard = 0
                while self._eval_condition(condition):
                    self._run_block(lines, index, block_end, result, user)
                    guard += 1
                    if guard > 10000:
                        result.errors.append("while loop iteration limit exceeded")
                        break
                index = block_end + 1
            elif keyword == "def":
                block_end, _ = self._find_block(lines, index - 1, end, "def", "enddef")
                self._define_function(line, lines[index:block_end])
                index = block_end + 1
            elif keyword == "set":
                self._assign(line, result)
            elif keyword in ("else", "endif", "endfor", "endwhile", "enddef"):
                continue
            else:
                executed = self.execute_command(line, user=user)
                result.results.append(executed)
                if not executed.success:
                    result.errors.append(f"line {index}: {executed.error}")
        return index

    @staticmethod
    def _find_block(lines, start, end, opener, closer):
        depth = 0
        else_at = None
        for i in range(start, end):
            keyword = lines[i].split(None, 1)[0].lower() if lines[i] else ""
            if keyword == opener:
                depth += 1
            elif keyword == closer:
                depth -= 1
                if depth == 0:
                    return i, else_at
            elif keyword == "else" and depth == 1 and else_at is None:
                else_at = i
        return end, else_at

    def _parse_for(self, line):
        # for i in 1..5   |   for x in a,b,c
        body = line[3:].strip()
        var_name, _, spec = body.partition(" in ")
        spec = spec.strip()
        if ".." in spec:
            lo, _, hi = spec.partition("..")
            iterable = range(int(lo), int(hi) + 1)
        else:
            iterable = [self._eval_value(part.strip()) for part in spec.split(",")]
        return var_name.strip(), iterable

    def _define_function(self, header, body_lines):
        # def greet(name) -> defines a new command running the body lines
        signature = header[3:].strip()
        name, _, arg_spec = signature.partition("(")
        arg_names = [a.strip() for a in arg_spec.rstrip(")").split(",") if a.strip()]
        name = name.strip()

        def _func(*args, _name=name, _args=arg_names, _body=body_lines):
            self.push_scope()
            try:
                for arg_name, value in zip(_args, args):
                    self.set_variable(arg_name, value)
                sub = ScriptResult(success=True)
                self._run_block(_body, 0, len(_body), sub, "admin")
                return sub
            finally:
                self.pop_scope()

        self.register_command(
            name,
            func=_func,
            params=[Param(a) for a in arg_names],
            help=f"script-defined command {name}",
        )

    def _assign(self, line, result):
        # set x = 1+2   |   set global y = "hi"
        rest = line[3:].strip()
        scope = "local"
        if rest.startswith("global "):
            scope = "global"
            rest = rest[len("global "):].strip()
        name, _, expr = rest.partition("=")
        try:
            self.set_variable(name.strip(), self._eval_value(expr.strip()), scope=scope)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"set failed: {exc}")

    def _eval_condition(self, expr) -> bool:
        return bool(self._eval_value(expr))

    def _eval_value(self, expr):
        variables = self.resolve_variable()
        try:
            return eval(expr, {"__builtins__": {}}, dict(variables))  # noqa: S307
        except Exception:  # noqa: BLE001
            if expr in variables:
                return variables[expr]
            try:
                return int(expr)
            except ValueError:
                try:
                    return float(expr)
                except ValueError:
                    return expr.strip("'\"") if expr else expr
