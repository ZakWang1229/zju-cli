"""Command registration, metadata, validation, and dispatch."""
from dataclasses import dataclass
import json

from zju_cli.shared.errors import InputError


@dataclass(frozen=True)
class Command:
    name: str
    handler: object
    mode: str = "request"
    durable: bool = False
    resumable: bool = False
    version: int = 1
    serial_group: str | None = None
    internal_only: bool = False
    transient_fields: tuple[str, ...] = ()


class CommandRegistry:
    def __init__(self):
        self._commands = {}
        self._registration_closed = False

    def register(self, name, handler, *, mode="request", durable=False,
                 resumable=False, version=1, serial_group=None,
                 internal_only=False, transient_fields=()):
        if self._registration_closed:
            raise RuntimeError("命令注册已结束")
        if (not isinstance(name, str) or not name or name in self._commands
                or not callable(handler) or mode not in {"request", "job"}
                or type(version) is not int or version < 1
                or any(type(value) is not bool
                       for value in (durable, resumable, internal_only))
                or (resumable and not durable)
                or (serial_group is not None
                    and (not isinstance(serial_group, str) or not serial_group))
                or not isinstance(transient_fields, (list, tuple))
                or any(not isinstance(field, str) or not field
                       for field in transient_fields)):
            raise ValueError("Invalid command registration")
        self._commands[name] = Command(
            name,
            handler,
            mode,
            durable,
            resumable,
            version,
            serial_group,
            internal_only,
            tuple(transient_fields),
        )

    def close_registration(self):
        self._registration_closed = True

    def command_list(self):
        commands = [
            command
            for command in self._commands.values()
            if not command.internal_only
        ]
        return {
            "commands": sorted(command.name for command in commands),
            "metadata": {
                command.name: {
                    "mode": command.mode,
                    "durable": command.durable,
                    "resumable": command.resumable,
                    "version": command.version,
                    "serial_group": command.serial_group,
                    "transient_fields": list(command.transient_fields),
                }
                for command in commands
            },
        }

    def command(self, name):
        return self._commands.get(name)

    def dispatch(self, name, parameters, *, internal=False):
        if (not isinstance(name, str) or not isinstance(parameters, dict)
                or any(not isinstance(key, str) for key in parameters)):
            raise InputError("命令或参数格式无效")
        command = self._commands.get(name)
        if command is None or (command.internal_only and not internal):
            raise InputError("未知命令")
        result = command.handler(**parameters)
        try:
            json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"命令 {name} 返回了非 JSON 数据") from exc
        return result
