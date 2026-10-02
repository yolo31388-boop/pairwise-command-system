"""
pairwise-command-system - CommandSystem

A full-featured console command system:

- register_command: parameter definitions, aliases, permission levels
- parse_command: quote handling, escape sequences, completion
- execute_command: structured return values, timeouts, history
- resolve_variable: scopes, types, persistence
- run_script: if/else/while/for control flow, user functions, error recovery
"""

from __future__ import annotations

import inspect
import json
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class CommandResult:
    """Result of executing a single command."""

    success: bool = True
    output: Any = None
    error: Optional[str] = None
    code: int = 0
    command: Optional[str] = None
    args: list = field(default_factory=list)
    timed_out: bool = False

    def __bool__(self) -> bool:
        return self.success

    @property
    def exit_code(self) -> int:
        return self.code

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "code": self.code,
            "command": self.command,
            "args": self.args,
            "timed_out": self.timed_out,
        }


@dataclass
class CommandParam:
    """Definition of a single command parameter."""

    name: str
    type: Any = str
    required: bool = True
    default: Any = None
    help: str = ""
    is_flag: bool = False

    def convert(self, raw: Any) -> Any:
        if raw is None:
            return self.default
        if self.type is bool:
            if isinstance(raw, bool):
                return raw
            return str(raw).lower() in ("1", "true", "yes", "on")
        try:
            return self.type(raw)
        except (TypeError, ValueError):
            return raw


@dataclass
class Command:
    """A registered command."""

    name: str
    handler: Callable[..., Any]
    params: dict = field(default_factory=dict)
    param_order: list = field(default_factory=list)
    aliases: list = field(default_factory=list)
    permission: int = 0
    help: str = ""


class ParsedCommand(tuple):
    """(name, args, kwargs) tuple with named attribute access."""

    def __new__(cls, name, args=None, kwargs=None, raw=None):
        obj = tuple.__new__(cls, (name, list(args or []), dict(kwargs or {})))
        obj.raw = raw or ""
        return obj

    @property
    def name(self) -> str:
        return self[0]

    @property
    def args(self) -> list:
        return self[1]

    @property
    def kwargs(self) -> dict:
        return self[2]


class Variable:
    """A typed variable value."""

    __slots__ = ("value", "type")

    def __init__(self, value: Any, vtype: Optional[type] = None):
        self.value = value
        self.type = vtype if vtype is not None else _infer_type(value)

    def to_dict(self) -> dict:
        return {"value": self.value, "type": self.type.__name__}

    @staticmethod
    def from_dict(data: dict) -> "Variable":
        vtype = _TYPE_MAP.get(data.get("type", "str"))
        return Variable(data.get("value"), vtype)


_TYPE_MAP = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
    "NoneType": type(None),
}


def _infer_type(value: Any) -> type:
    if isinstance(value, bool):
        return bool
    if isinstance(value, int):
        return int
    if isinstance(value, float):
        return float
    if isinstance(value, list):
        return list
    if isinstance(value, dict):
        return dict
    if value is None:
        return type(None)
    return str


# ---------------------------------------------------------------------------
# Tokenizer: quoting + escape handling
# ---------------------------------------------------------------------------


def tokenize(text: str) -> list:
    """Split a command line into tokens, honoring quotes and escapes."""
    tokens: list = []
    current: list = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            nxt = text[i + 1]
            mapping = {
                "n": "\n",
                "t": "\t",
                "r": "\r",
                "\\": "\\",
                '"': '"',
                "'": "'",
                " ": " ",
                "0": "\0",
            }
            current.append(mapping.get(nxt, nxt))
            i += 2
            continue
        if ch == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    mapping = {
                        "n": "\n",
                        "t": "\t",
                        "r": "\r",
                        '"': '"',
                        "\\": "\\",
                        "$": "$",
                    }
                    current.append(mapping.get(nxt, nxt))
                    i += 2
                else:
                    current.append(text[i])
                    i += 1
            i += 1  # skip closing quote (or end of input)
            continue
        if ch == "'":
            i += 1
            while i < n and text[i] != "'":
                current.append(text[i])
                i += 1
            i += 1
            continue
        if ch.isspace():
            if current:
                tokens.append("".join(current))
                current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    if current:
        tokens.append("".join(current))
    return tokens


# ---------------------------------------------------------------------------
# CommandSystem
# ---------------------------------------------------------------------------


class CommandSystem:
    """Central command registry, parser, executor and script engine."""

    DEFAULT_ROLES = {"guest": 0, "user": 1, "moderator": 5, "admin": 10}

    def __init__(self, variable_file: Optional[str] = None,
                 default_timeout: float = 5.0,
                 default_permission: int = 0):
        self.state: dict = {}
        self.commands: dict[str, Command] = {}
        self.aliases: dict[str, str] = {}
        self.roles: dict[str, int] = dict(self.DEFAULT_ROLES)
        self.variables: dict[str, Variable] = {}
        self._scopes: list[dict[str, Variable]] = []
        self.history: list[str] = []
        self._hist_index: int = -1
        self.default_timeout = default_timeout
        self.default_permission = default_permission
        self.variable_file = variable_file
        self.functions: dict[str, dict] = {}
        if variable_file and os.path.exists(variable_file):
            self.load_variables()
        self._register_builtins()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_command(
        self,
        name: Optional[str] = None,
        handler: Optional[Callable[..., Any]] = None,
        params: Optional[list] = None,
        aliases: Optional[list] = None,
        permission: int = 0,
        help: str = "",
        *,
        func: Optional[Callable[..., Any]] = None,
    ):
        """Register a command with parameter definitions, aliases and rights.

        Usable directly (``register_command("x", handler, ...)``) or as a
        decorator, with or without arguments
        (``@register_command("x")`` / ``@register_command``).
        Calling it with no arguments returns the full command registry.
        """
        if name is None:
            return self.commands

        # register_command used as a plain decorator: @register_command
        if callable(name) and handler is None:
            decorated = name
            cname = getattr(decorated, "__name__", "command").lstrip("_")
            self.register_command(cname, decorated, params=params,
                                  aliases=aliases, permission=permission,
                                  help=help)
            return decorated

        handler = handler or func
        if handler is None:
            # Used as a decorator factory: @register_command("name", ...)
            def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
                self.register_command(name, fn, params=params,
                                      aliases=aliases, permission=permission,
                                      help=help)
                return fn

            return decorator

        param_dict: dict[str, CommandParam] = {}
        param_order: list[str] = []
        for raw_param in params or []:
            param = self._make_param(raw_param)
            param_dict[param.name] = param
            param_order.append(param.name)

        cmd = Command(
            name=name,
            handler=handler,
            params=param_dict,
            param_order=param_order,
            aliases=list(dict.fromkeys(aliases or [])),
            permission=permission,
            help=help or (inspect.getdoc(handler) or ""),
        )
        self.commands[name] = cmd
        for alias in cmd.aliases:
            self.aliases[alias] = name
        return cmd

    @staticmethod
    def _make_param(raw: Any) -> CommandParam:
        if isinstance(raw, CommandParam):
            return raw
        if isinstance(raw, str):
            return CommandParam(name=raw)
        if isinstance(raw, (tuple, list)):
            pname = raw[0]
            ptype = raw[1] if len(raw) > 1 else str
            required = raw[2] if len(raw) > 2 else True
            default = raw[3] if len(raw) > 3 else None
            return CommandParam(name=pname, type=ptype, required=required,
                                default=default)
        if isinstance(raw, dict):
            data = dict(raw)
            pname = data.pop("name")
            return CommandParam(pname, **data)
        raise ValueError(f"unsupported parameter definition: {raw!r}")

    def unregister_command(self, name: str) -> bool:
        resolved = self.resolve_name(name)
        if resolved is None:
            return False
        cmd = self.commands.pop(resolved)
        for alias in cmd.aliases:
            self.aliases.pop(alias, None)
        return True

    def resolve_name(self, name: str) -> Optional[str]:
        return name if name in self.commands else self.aliases.get(name)

    def get_command(self, name: str) -> Optional[Command]:
        resolved = self.resolve_name(name)
        return self.commands.get(resolved) if resolved else None

    def list_commands(self) -> list[str]:
        return sorted(self.commands)

    def add_role(self, role: str, level: int) -> None:
        self.roles[role] = level

    def _role_level(self, role: Any) -> int:
        if role is None:
            return 0
        if isinstance(role, (int, float)):
            return int(role)
        return self.roles.get(str(role), 0)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def parse_command(self, text: Optional[str] = None,
                      complete: bool = False,
                      context: Optional[dict] = None) -> Any:
        """Parse a command line.

        Returns a :class:`ParsedCommand` ``(name, args, kwargs)``.  With
        ``complete=True`` returns auto-completion candidates for the last
        token.  Called with no input it returns the empty parse result.
        """
        if complete:
            prefix = text or ""
            return self.complete(prefix, context=context)
        if text is None:
            return ParsedCommand("", [], {}, "")

        tokens = tokenize(text)
        if not tokens:
            return ParsedCommand("", [], {}, text)

        name = tokens[0]
        args: list = []
        kwargs: dict = {}
        cmd = self.get_command(name)

        idx = 0
        for token in tokens[1:]:
            if token.startswith("--") and "=" in token:
                key, _, value = token[2:].partition("=")
                kwargs[key.replace("-", "_")] = value
                idx += 1
            elif token.startswith("--"):
                key = token[2:].replace("-", "_")
                param = cmd.params.get(key) if cmd else None
                kwargs[key] = False if (param and param.is_flag) else True
                idx += 1
            else:
                param = self._param_at(cmd, idx)
                if param is not None:
                    args.append(param.convert(token))
                else:
                    args.append(token)
                idx += 1

        return ParsedCommand(name, args, kwargs, text)

    @staticmethod
    def _param_at(cmd: Optional[Command], index: int) -> Optional[CommandParam]:
        if cmd is None or index >= len(cmd.param_order):
            return None
        return cmd.params[cmd.param_order[index]]

    def complete(self, prefix: str, context: Optional[dict] = None) -> list:
        """Return command-name completions for a prefix (tab completion)."""
        prefix = prefix.lstrip()
        names = list(self.commands) + list(self.aliases)
        return sorted(name for name in names
                      if name.startswith(prefix) and self._can_access(
                          self.get_command(name), context))

    def _can_access(self, cmd: Optional[Command],
                    context: Optional[dict]) -> bool:
        if cmd is None:
            return False
        if context is None or "permission" not in context and "role" not in context:
            return True
        level = context.get("permission")
        if level is None:
            level = self._role_level(context.get("role"))
        return int(level) >= cmd.permission

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute_command(self, command: Any = None, *extra: Any,
                        context: Optional[dict] = None,
                        timeout: Optional[float] = None,
                        record_history: bool = True) -> CommandResult:
        """Execute a command line (or pre-parsed command).

        Always returns a :class:`CommandResult` with success/output/error,
        enforces permissions, applies a timeout, and records history.
        """
        if command is None:
            return CommandResult(False, None, "no command given", code=2)

        if isinstance(command, ParsedCommand):
            name, args, kwargs = command.name, list(command.args), dict(command.kwargs)
            raw = command.raw or command.name
        elif isinstance(command, (list, tuple)):
            name, args, kwargs = command[0], list(command[1:]), {}
            raw = " ".join(str(p) for p in command)
        elif extra:
            name, args, kwargs = str(command), list(extra), {}
            raw = " ".join([str(command)] + [str(a) for a in extra])
        else:
            parsed = self.parse_command(str(command))
            name, args, kwargs, raw = parsed.name, parsed.args, parsed.kwargs, parsed.raw

        if record_history:
            self.add_history(raw)

        cmd = self.get_command(name)
        if cmd is None:
            return CommandResult(False, None, f"unknown command: {name}",
                                 code=127, command=name, args=args)

        if not self._can_access(cmd, context):
            return CommandResult(False, None,
                                 f"permission denied: {cmd.name} requires "
                                 f"permission level {cmd.permission}",
                                 code=126, command=cmd.name, args=args)

        try:
            call_args, call_kwargs = self._bind_args(cmd, args, kwargs)
        except (TypeError, ValueError) as exc:
            return CommandResult(False, None, str(exc), code=2,
                                 command=cmd.name, args=args)

        timeout = self.default_timeout if timeout is None else timeout
        result = self._run_with_timeout(cmd, call_args, call_kwargs, timeout)

        if isinstance(result, CommandResult):
            result.command = result.command or cmd.name
            result.args = result.args or args
            return result
        return CommandResult(True, result, None, code=0,
                             command=cmd.name, args=args)

    def _bind_args(self, cmd: Command, args: list, kwargs: dict):
        call_args: list = []
        call_kwargs: dict = {}
        consumed = 0
        for pname in cmd.param_order:
            param = cmd.params[pname]
            if pname in kwargs:
                call_kwargs[pname] = param.convert(kwargs.pop(pname))
            elif param.is_flag:
                call_kwargs[pname] = param.default if param.default is not None else False
            elif consumed < len(args):
                call_kwargs[pname] = param.convert(args[consumed])
                consumed += 1
            elif param.required:
                raise ValueError(f"missing required parameter: {pname}")
            else:
                call_kwargs[pname] = param.default
        call_args.extend(args[consumed:])
        call_kwargs.update(kwargs)
        return call_args, call_kwargs

    def _run_with_timeout(self, cmd: Command, args: list, kwargs: dict,
                          timeout: Optional[float]):
        box: dict = {}

        def runner():
            try:
                box["value"] = cmd.handler(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - surfaced via result
                box["error"] = exc

        if timeout is None or timeout <= 0:
            runner()
        else:
            thread = threading.Thread(target=runner, daemon=True)
            thread.start()
            thread.join(timeout)
            if thread.is_alive():
                return CommandResult(
                    False, None,
                    f"command '{cmd.name}' timed out after {timeout}s",
                    code=124, command=cmd.name, args=list(args), timed_out=True,
                )
        if "error" in box:
            exc = box["error"]
            return CommandResult(False, None, f"{type(exc).__name__}: {exc}",
                                 code=1, command=cmd.name, args=list(args))
        return box.get("value")

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def add_history(self, entry: str) -> None:
        if entry and entry.strip():
            self.history.append(entry)
        self._hist_index = len(self.history)

    def get_history(self, limit: Optional[int] = None) -> list:
        return self.history[-limit:] if limit else list(self.history)

    def clear_history(self) -> None:
        self.history.clear()
        self._hist_index = -1

    def history_prev(self) -> Optional[str]:
        """Move up in history (like pressing the up arrow)."""
        if not self.history:
            return None
        if self._hist_index < 0 or self._hist_index > len(self.history):
            self._hist_index = len(self.history)
        if self._hist_index > 0:
            self._hist_index -= 1
        return self.history[self._hist_index]

    def history_next(self) -> Optional[str]:
        """Move down in history (like pressing the down arrow)."""
        if not self.history or self._hist_index < 0:
            return None
        if self._hist_index < len(self.history) - 1:
            self._hist_index += 1
            return self.history[self._hist_index]
        self._hist_index = len(self.history)
        return None

    # ------------------------------------------------------------------
    # Variables: scopes, types, persistence
    # ------------------------------------------------------------------

    def set_variable(self, name: str, value: Any,
                     vtype: Optional[type] = None,
                     scope: str = "global") -> Variable:
        """Set a typed variable in the global or current local scope."""
        if isinstance(value, Variable):
            var = value
        else:
            var = Variable(value, vtype)
        if not isinstance(var.value, var.type):
            try:
                var.value = var.type(var.value)
            except (TypeError, ValueError):
                var.type = _infer_type(var.value)
        if scope == "local" and self._scopes:
            self._scopes[-1][name] = var
        else:
            self.variables[name] = var
        return var

    def resolve_variable(self, name: Optional[str] = None,
                         scope: Optional[str] = None) -> Any:
        """Resolve a variable, honoring local-then-global scope order.

        Called with no name it returns a merged view of all visible
        variables (locals shadowing globals).
        """
        if name is None:
            merged = {k: v.value for k, v in self.variables.items()}
            for frame in self._scopes:
                merged.update({k: v.value for k, v in frame.items()})
            return merged

        if name.startswith("$"):
            name = name[1:]

        if scope in (None, "local"):
            for frame in reversed(self._scopes):
                if name in frame:
                    return frame[name].value
        if scope in (None, "global") and name in self.variables:
            return self.variables[name].value
        if scope is not None and scope not in ("local", "global"):
            raise KeyError(f"unknown scope: {scope}")
        raise KeyError(f"undefined variable: {name}")

    def get_variable(self, name: str, default: Any = None) -> Any:
        try:
            return self.resolve_variable(name)
        except KeyError:
            return default

    def get_variable_type(self, name: str) -> Optional[type]:
        for frame in reversed(self._scopes):
            if name in frame:
                return frame[name].type
        var = self.variables.get(name)
        return var.type if var else None

    def delete_variable(self, name: str, scope: str = "global") -> bool:
        target = self._scopes[-1] if scope == "local" and self._scopes else self.variables
        return target.pop(name, None) is not None

    def push_scope(self) -> None:
        self._scopes.append({})

    def pop_scope(self) -> dict:
        return self._scopes.pop() if self._scopes else {}

    def save_variables(self, path: Optional[str] = None) -> str:
        """Persist global variables (with types) to a JSON file."""
        path = path or self.variable_file
        if not path:
            raise ValueError("no variable file configured")
        data = {name: var.to_dict() for name, var in self.variables.items()}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        return path

    def load_variables(self, path: Optional[str] = None) -> dict:
        """Load previously persisted variables, restoring their types."""
        path = path or self.variable_file
        if not path or not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for name, payload in data.items():
            self.variables[name] = Variable.from_dict(payload)
        return dict(self.variables)

    _VAR_PATTERN = re.compile(r"\$(\w+)|\$\{([^}]+)\}")

    def _expand(self, text: str) -> str:
        """Expand $var and ${var} references inside a string."""

        def repl(match: re.Match) -> str:
            key = match.group(1) or match.group(2)
            value = self.get_variable(key)
            return "" if value is None else str(value)

        return self._VAR_PATTERN.sub(repl, text)

    # ------------------------------------------------------------------
    # Script engine: control flow, functions, error handling
    # ------------------------------------------------------------------

    def run_script(self, script: Any = None,
                   context: Optional[dict] = None,
                   stop_on_error: bool = False,
                   timeout: Optional[float] = None) -> CommandResult:
        """Run a multi-line script.

        Supports ``if/elif/else/endif``, ``while/endwhile``,
        ``for x in .../endfor``, ``def name(args)/enddef`` functions,
        ``set name value`` variable assignment, and per-line error
        recovery (a failing line does not abort the script unless
        ``stop_on_error`` is set).
        """
        if script is None:
            return CommandResult(True, [], None, code=0, command="script")

        if isinstance(script, (list, tuple)):
            lines = [str(line) for line in script]
        else:
            lines = str(script).splitlines()

        results: list = []
        errors: list = []
        output: list = []
        context = dict(context or {})
        context.setdefault("stop", False)
        deadline = None
        if timeout:
            import time
            deadline = time.monotonic() + timeout

        self.push_scope()
        try:
            self._run_block(lines, 0, len(lines), results, errors, output,
                            context, stop_on_error, deadline)
        finally:
            self.pop_scope()

        success = not errors and not context.get("aborted")
        error_text = "; ".join(errors) if errors else None
        code = 0 if success else 1
        return CommandResult(success, output, error_text, code=code,
                             command="script", args=results)

    def _run_block(self, lines: list, start: int, end: int, results: list,
                   errors: list, output: list, context: dict,
                   stop_on_error: bool, deadline) -> int:
        idx = start
        while idx < end:
            if context.get("stop"):
                return idx
            if deadline is not None:
                import time
                if time.monotonic() > deadline:
                    errors.append("script timed out")
                    context["aborted"] = True
                    return idx

            raw = lines[idx]
            line = raw.strip()
            idx += 1
            if not line or line.startswith("#"):
                continue

            keyword = line.split(None, 1)[0].lower()

            if keyword == "if":
                idx = self._run_if(lines, idx, end, results, errors, output,
                                   context, stop_on_error, deadline, line)
                continue
            if keyword == "while":
                idx = self._run_while(lines, idx, end, results, errors, output,
                                      context, stop_on_error, deadline, line)
                continue
            if keyword == "for":
                idx = self._run_for(lines, idx, end, results, errors, output,
                                    context, stop_on_error, deadline, line)
                continue
            if keyword == "def":
                idx = self._define_function(lines, idx, end, line)
                continue
            if keyword in ("endif", "endwhile", "endfor", "enddef",
                           "else", "elif"):
                return idx - 1  # let the enclosing construct consume it
            if keyword == "set":
                self._script_set(line, errors)
                continue
            if keyword == "return":
                context["return"] = self._expand(line[6:].strip())
                context["stop"] = True
                return idx
            if keyword == "break":
                context["break"] = True
                return idx
            if keyword == "continue":
                context["continue"] = True
                return idx

            result = self._run_script_line(line, context, results, output)
            results.append(result)
            if result.output is not None:
                output.append(result.output)
            if not result.success:
                errors.append(f"line {idx}: {result.error}")
                if stop_on_error:
                    context["aborted"] = True
                    return idx
        return idx

    def _run_script_line(self, line: str, context: dict,
                         results: list, output: list) -> CommandResult:
        expanded = self._expand(line)
        tokens = tokenize(expanded)
        if tokens and tokens[0] in self.functions:
            return self._call_function(tokens[0], tokens[1:], context,
                                       results, output)
        return self.execute_command(expanded, context=context,
                                    record_history=False)

    # -- control flow ----------------------------------------------------

    def _find_block_end(self, lines: list, idx: int, end: int,
                        openers: tuple, closers: tuple,
                        middles: tuple = ()) -> tuple:
        """Locate matching middle/closer keywords, honoring nesting."""
        depth = 0
        marks: dict[str, int] = {}
        while idx < end:
            word = lines[idx].strip().split(None, 1)[0].lower() \
                if lines[idx].strip() else ""
            if word in openers:
                depth += 1
            elif word in closers:
                if depth == 0:
                    marks.setdefault(word, idx)
                    return idx, marks
                depth -= 1
            elif word in middles and depth == 0:
                marks.setdefault(word, idx)
            idx += 1
        return idx, marks

    def _run_if(self, lines, idx, end, results, errors, output, context,
                stop_on_error, deadline, line):
        condition = line[2:].strip()
        block_end, marks = self._find_block_end(
            lines, idx, end, ("if",), ("endif",), ("elif", "else"))

        branches = []  # (condition or None, start, stop)
        cond: Optional[str] = condition
        seg_start = idx
        cursor = idx
        while cursor < block_end:
            word = lines[cursor].strip().split(None, 1)[0].lower()
            if word in ("elif", "else"):
                branches.append((cond, seg_start, cursor))
                seg_start = cursor + 1
                cond = None if word == "else" else \
                    lines[cursor].strip()[4:].strip()
            cursor += 1
        branches.append((cond, seg_start, block_end))

        for branch_cond, bstart, bend in branches:
            if branch_cond is None or self._eval_condition(branch_cond):
                self._run_block(lines, bstart, bend, results, errors, output,
                                context, stop_on_error, deadline)
                break
        return block_end + 1

    def _run_while(self, lines, idx, end, results, errors, output, context,
                   stop_on_error, deadline, line):
        condition = line[5:].strip()
        block_end, _ = self._find_block_end(lines, idx, end,
                                            ("while",), ("endwhile",))
        guard = 0
        while self._eval_condition(condition):
            guard += 1
            if guard > 100000:
                errors.append("while loop iteration limit exceeded")
                break
            self._run_block(lines, idx, block_end, results, errors, output,
                            context, stop_on_error, deadline)
            if context.pop("break", False) or context.get("stop") \
                    or context.get("aborted"):
                break
            context.pop("continue", None)
        return block_end + 1

    def _run_for(self, lines, idx, end, results, errors, output, context,
                 stop_on_error, deadline, line):
        match = re.match(r"for\s+(\w+)\s+in\s+(.+)", line, re.IGNORECASE)
        block_end, _ = self._find_block_end(lines, idx, end,
                                            ("for",), ("endfor",))
        if not match:
            errors.append(f"malformed for loop: {line}")
            return block_end + 1
        var_name, iterable_expr = match.group(1), match.group(2)
        iterable = self._eval_iterable(iterable_expr)
        for item in iterable:
            self.set_variable(var_name, item, scope="local")
            self._run_block(lines, idx, block_end, results, errors, output,
                            context, stop_on_error, deadline)
            if context.pop("break", False) or context.get("stop") \
                    or context.get("aborted"):
                break
            context.pop("continue", None)
        return block_end + 1

    def _eval_iterable(self, expr: str):
        expr = self._expand(expr.strip())
        range_match = re.fullmatch(r"range\(([^)]*)\)", expr)
        if range_match:
            parts = [int(p.strip()) for p in range_match.group(1).split(",")
                     if p.strip()]
            return range(*parts)
        if expr.startswith("[") and expr.endswith("]"):
            inner = expr[1:-1].strip()
            return [p.strip() for p in inner.split(",")] if inner else []
        return expr.split() if expr else []

    # -- functions ---------------------------------------------------------

    def _define_function(self, lines, idx, end, line):
        match = re.match(r"def\s+(\w+)\s*(?:\(([^)]*)\))?", line)
        block_end, _ = self._find_block_end(lines, idx, end,
                                            ("def",), ("enddef",))
        if not match:
            return block_end + 1
        fname = match.group(1)
        params = [p.strip() for p in (match.group(2) or "").split(",")
                  if p.strip()]
        self.functions[fname] = {
            "params": params,
            "body": lines[idx:block_end],
        }
        return block_end + 1

    def _call_function(self, fname: str, args: list, context: dict,
                       results: list, output: list) -> CommandResult:
        spec = self.functions[fname]
        self.push_scope()
        try:
            for pname, pval in zip(spec["params"], args):
                self.set_variable(pname, pval, scope="local")
            errors: list = []
            local_ctx = dict(context)
            self._run_block(spec["body"], 0, len(spec["body"]), results,
                            errors, output, local_ctx, False, None)
            value = local_ctx.get("return", results[-1].output
                                  if results else None)
            success = not errors
            return CommandResult(success, value,
                                 "; ".join(errors) if errors else None,
                                 code=0 if success else 1, command=fname)
        finally:
            self.pop_scope()

    # -- script helpers ----------------------------------------------------

    def _script_set(self, line: str, errors: list) -> None:
        rest = line[3:].strip()
        if "=" in rest:
            name, _, value = rest.partition("=")
        else:
            parts = rest.split(None, 1)
            name, value = parts[0], (parts[1] if len(parts) > 1 else "")
        name = name.strip()
        if not name:
            errors.append(f"malformed set: {line}")
            return
        self.set_variable(name, self._expand(value.strip()), scope="local")

    def _eval_condition(self, condition: str) -> bool:
        condition = self._expand(condition.strip())
        if not condition:
            return False
        negate = False
        if condition.startswith("not "):
            negate = True
            condition = condition[4:].strip()
        for op in ("==", "!=", ">=", "<=", ">", "<"):
            if op in condition:
                left, _, right = condition.partition(op)
                result = self._compare(left.strip(), right.strip(), op)
                return (not result) if negate else result
        value = condition.lower() not in ("", "0", "false", "none", "no")
        return (not value) if negate else value

    @staticmethod
    def _compare(left: str, right: str, op: str) -> bool:
        def num(text: str):
            try:
                return float(text)
            except ValueError:
                return None

        lnum, rnum = num(left), num(right)
        lhs, rhs = (lnum, rnum) if (lnum is not None and rnum is not None) \
            else (left, right)
        if op == "==":
            return lhs == rhs
        if op == "!=":
            return lhs != rhs
        if op == ">":
            return lhs > rhs
        if op == "<":
            return lhs < rhs
        if op == ">=":
            return lhs >= rhs
        return lhs <= rhs

    # ------------------------------------------------------------------
    # Built-in commands
    # ------------------------------------------------------------------

    def _register_builtins(self) -> None:
        self.register_command(
            "help", self._cmd_help, aliases=["?"],
            help="List available commands or show help for one command.")
        self.register_command(
            "echo", self._cmd_echo, aliases=["print"],
            help="Echo the given arguments.")
        self.register_command(
            "history", self._cmd_history,
            help="Show the command history.")

    def _cmd_help(self, name: Optional[str] = None) -> str:
        if name:
            cmd = self.get_command(name)
            if cmd is None:
                return f"unknown command: {name}"
            params = " ".join(
                f"<{p}>" if cmd.params[p].required else f"[{p}]"
                for p in cmd.param_order)
            alias = f" (aliases: {', '.join(cmd.aliases)})" if cmd.aliases else ""
            return f"{cmd.name}{alias} {params}\n{cmd.help}".strip()
        lines = []
        for cname in sorted(self.commands):
            cmd = self.commands[cname]
            lines.append(f"{cname}: {cmd.help}" if cmd.help else cname)
        return "\n".join(lines)

    def _cmd_echo(self, *args: Any) -> str:
        return " ".join(str(a) for a in args)

    def _cmd_history(self) -> list:
        return list(self.history)
