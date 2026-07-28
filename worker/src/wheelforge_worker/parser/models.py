from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RequirementItem:
    line_no: int
    name: str
    extras: tuple[str, ...]
    specifier: str
    marker: str | None
    original_text: str


@dataclass(frozen=True, slots=True)
class ParsedRequirements:
    normalized_text: str
    items: tuple[RequirementItem, ...]
    encoding: str


class RequirementsParseError(ValueError):
    """Base class for failures caused by untrusted requirements input."""


class InputTooLargeError(RequirementsParseError):
    def __init__(self, actual: int, limit: int) -> None:
        self.actual = actual
        self.limit = limit
        super().__init__(f"requirements file is {actual} bytes; limit is {limit}")


class TooManyLinesError(RequirementsParseError):
    def __init__(self, actual: int, limit: int) -> None:
        self.actual = actual
        self.limit = limit
        super().__init__(f"requirements file has {actual} lines; limit is {limit}")


class RequirementsDecodeError(RequirementsParseError):
    def __init__(self) -> None:
        super().__init__("requirements file is neither valid UTF-8 nor valid GBK")


class RequirementLineError(RequirementsParseError):
    def __init__(self, message: str, line_no: int, original_text: str) -> None:
        self.line_no = line_no
        self.original_text = original_text
        super().__init__(f"line {line_no}: {message}")


class InvalidRequirementError(RequirementLineError):
    pass


class UnsupportedRequirementSyntax(RequirementLineError):
    pass


class DuplicateRequirementConflictError(RequirementLineError):
    def __init__(
        self, name: str, line_no: int, original_text: str, existing_line_no: int
    ) -> None:
        self.name = name
        self.existing_line_no = existing_line_no
        super().__init__(
            f"requirement {name!r} conflicts with line {existing_line_no}",
            line_no,
            original_text,
        )
