"""Compile Ultron logical tools into Hermes-native tool policy."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CompiledToolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hermes_tools: list[str] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)
    translations: dict[str, str] = Field(default_factory=dict)


class ToolPolicyCompiler:
    """Deterministic static translation from logical tool names to Hermes tools."""

    TOOL_MAP: dict[str, str] = {
        "read": "read_file",
        "file.read": "read_file",
        "search": "search_files",
        "grep": "search_files",
        "pytest": "terminal_process",
        "python": "terminal_process",
        "bash": "terminal_process",
        "terminal": "terminal_process",
        "write": "write_file",
        "edit": "edit_file",
    }

    @classmethod
    def compile(cls, logical: list[str]) -> CompiledToolPolicy:
        translations: dict[str, str] = {}
        unknown: list[str] = []
        for tool in sorted(dict.fromkeys(logical)):
            native = cls.TOOL_MAP.get(tool)
            if native is None:
                unknown.append(tool)
            else:
                translations[tool] = native
        return CompiledToolPolicy(
            hermes_tools=sorted(dict.fromkeys(translations.values())),
            unknown=unknown,
            translations=dict(sorted(translations.items())),
        )
