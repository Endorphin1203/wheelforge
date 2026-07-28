import pytest
from pip._internal.req.req_file import break_args_options, preprocess

from wheelforge_worker.parser import (
    DuplicateRequirementConflictError,
    InputTooLargeError,
    InvalidRequirementError,
    RequirementConstraintConflictError,
    RequirementsDecodeError,
    TooManyLinesError,
    UnsupportedRequirementSyntax,
    parse_requirements,
)


def _pip_preprocessed_argument(line: str) -> tuple[str, str]:
    processed_lines = list(preprocess(line + "\n"))
    assert len(processed_lines) == 1
    _, processed = processed_lines[0]
    return break_args_options(processed)


def test_parses_bare_requirement_extras_specifier_and_marker() -> None:
    result = parse_requirements(
        b'Requests[security,socks]>=2.31,<=3; python_version < "3.13"\n'
        b"urllib3\n"
    )

    assert result.encoding == "utf-8"
    assert result.normalized_text == (
        'requests[security,socks]>=2.31,<=3; python_version < "3.13"\n'
        "urllib3\n"
    )
    assert result.items[0].line_no == 1
    assert result.items[0].name == "requests"
    assert result.items[0].extras == ("security", "socks")
    assert result.items[0].specifier == ">=2.31,<=3"
    assert result.items[0].marker == 'python_version < "3.13"'
    assert result.items[0].original_text == (
        'Requests[security,socks]>=2.31,<=3; python_version < "3.13"'
    )
    assert result.items[1].specifier == ""
    assert result.items[1].marker is None


@pytest.mark.parametrize(
    ("raw", "encoding"),
    [
        (b"\xef\xbb\xbfrequests==2.32.4\r\n", "utf-8-sig"),
        ("requests==2.32.4  # \u4e2d\u6587\u6ce8\u91ca\r\n".encode("gbk"), "gbk"),
    ],
)
def test_decodes_supported_encodings_strictly(raw: bytes, encoding: str) -> None:
    result = parse_requirements(raw)

    assert result.encoding == encoding
    assert result.normalized_text == "requests==2.32.4\n"


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_normalizes_all_supported_line_endings(newline: str) -> None:
    raw = f"requests==2.32.4{newline}urllib3>=2{newline}".encode()

    result = parse_requirements(raw)

    assert result.normalized_text == "requests==2.32.4\nurllib3>=2\n"
    assert [item.line_no for item in result.items] == [1, 2]


def test_empty_file_normalizes_to_one_lf() -> None:
    result = parse_requirements(b"")

    assert result.normalized_text == "\n"
    assert result.items == ()


def test_ignores_blank_and_comment_lines_but_preserves_source_line_numbers() -> None:
    result = parse_requirements(b"# heading\n\n  requests==2  # pinned\n")

    assert result.normalized_text == "requests==2\n"
    assert len(result.items) == 1
    assert result.items[0].line_no == 3
    assert result.items[0].original_text == "  requests==2  # pinned"


def test_hash_without_preceding_whitespace_is_not_treated_as_a_comment() -> None:
    with pytest.raises(InvalidRequirementError) as raised:
        parse_requirements(b"requests==2#not-a-comment\n")

    assert raised.value.line_no == 1


@pytest.mark.parametrize(
    "marker_value",
    [
        "ops@example.test",
        "https://example.test/simple",
        "git+mirror",
    ],
)
def test_preserves_special_characters_inside_quoted_marker_values(
    marker_value: str,
) -> None:
    requirement = f'demo; platform_machine == "{marker_value}"'

    result = parse_requirements((requirement + "\n").encode())

    assert result.items[0].marker == f'platform_machine == "{marker_value}"'
    assert result.items[0].original_text == requirement
    assert result.normalized_text == requirement + "\n"


def test_strips_real_inline_comment_after_quoted_marker_value() -> None:
    requirement = 'demo; platform_machine == "x86_64"'
    original = requirement + "  # deployment note"

    result = parse_requirements((original + "\n").encode())

    assert result.items[0].marker == 'platform_machine == "x86_64"'
    assert result.items[0].original_text == original
    assert result.normalized_text == requirement + "\n"


@pytest.mark.parametrize(
    "requirement",
    [
        'demo; platform_machine == "x86 # lab"',
        'demo; platform_machine == "x86 --index-url local"',
        'demo; platform_machine == "x86 -r hidden.txt"',
    ],
)
def test_rejects_markers_that_pip_preprocessing_changes(requirement: str) -> None:
    pip_argument, pip_options = _pip_preprocessed_argument(requirement)
    assert pip_argument != requirement or pip_options

    with pytest.raises(UnsupportedRequirementSyntax) as raised:
        parse_requirements((requirement + "\n").encode())

    assert raised.value.line_no == 1
    assert raised.value.original_text == requirement


def test_rejects_marker_environment_expansion_used_by_pip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requirement = 'demo; platform_machine == "${UPPER_ENV}"'
    monkeypatch.setenv("UPPER_ENV", "arm64")
    pip_argument, pip_options = _pip_preprocessed_argument(requirement)
    assert pip_argument != requirement
    assert not pip_options

    with pytest.raises(UnsupportedRequirementSyntax) as raised:
        parse_requirements((requirement + "\n").encode())

    assert raised.value.line_no == 1


def test_every_normalized_line_is_safe_for_pip_requirements_preprocessing() -> None:
    result = parse_requirements(
        b'requests[security]>=2,<=3; python_version < "3.13"  # supported\n'
        b'demo; platform_machine == "https://example.test/simple"\n'
    )

    normalized_lines = result.normalized_text.splitlines()
    processed_lines = [line for _, line in preprocess(result.normalized_text)]
    assert processed_lines == normalized_lines
    assert [break_args_options(line) for line in processed_lines] == [
        (line, "") for line in normalized_lines
    ]


def test_accepts_exact_lower_upper_and_compatible_release_specifiers() -> None:
    result = parse_requirements(
        b"one==1.0\ntwo>=2.0\nthree<=3.0\nfour~=4.0\nfive>=1,<=2\n"
    )

    assert [item.specifier for item in result.items] == [
        "==1.0",
        ">=2.0",
        "<=3.0",
        "~=4.0",
        ">=1,<=2",
    ]


@pytest.mark.parametrize("specifier", ["!=1", ">1", "<2", "===1", "==1.*"])
def test_rejects_specifiers_outside_the_approved_subset(specifier: str) -> None:
    with pytest.raises(UnsupportedRequirementSyntax) as raised:
        parse_requirements(f"demo{specifier}\n".encode())

    assert raised.value.line_no == 1


@pytest.mark.parametrize(
    "line",
    [
        "git+https://example.test/x.git",
        " GIT+HTTPS://example.test/x.git ",
        "hg+https://example.test/x",
        "SVN+SSH://example.test/x",
        "bzr+http://example.test/x",
        "demo @ https://example.test/x.whl",
        "DEMO @ FTP://example.test/x.whl",
        "https://example.test/x.whl",
        "file:///tmp/demo.whl",
        "-e .",
        " --EDITABLE ../demo ",
        "-r other.txt",
        " --Requirement other.txt ",
        "-c constraints.txt",
        " --CONSTRAINT constraints.txt ",
        "--index-url https://example.test/simple",
        " --TRUSTED-HOST example.test ",
        "requests==2 --hash=sha256:deadbeef",
        "requests --CONFIG-SETTINGS=build=value",
        "/opt/packages/demo",
        "./demo",
        "../demo",
        "~/demo",
        "vendor/demo",
        "vendor\\demo",
        "C:\\packages\\demo.whl",
        "c:/packages/demo.whl",
        "\\\\server\\share\\demo.whl",
        "demo.whl",
        "dist/demo.tar.gz",
        "demo.zip",
        'demo.zip ; python_version >= "3.9"',
        'demo.WHL\t; python_version >= "3.9"',
        'demo.TAR.GZ  ; python_version >= "3.9"',
    ],
)
def test_rejects_unsafe_syntax_before_pep508_parsing(line: str) -> None:
    with pytest.raises(UnsupportedRequirementSyntax) as raised:
        parse_requirements((line + "\n").encode())

    assert raised.value.line_no == 1
    assert raised.value.original_text == line


def test_unsafe_text_cannot_be_hidden_after_an_inline_comment() -> None:
    result = parse_requirements(b"requests==2  # -r hidden.txt\n")

    assert result.normalized_text == "requests==2\n"


def test_rejects_nul_with_line_number() -> None:
    with pytest.raises(UnsupportedRequirementSyntax) as raised:
        parse_requirements(b"requests\nurl\x00lib3\n")

    assert raised.value.line_no == 2


def test_rejects_backslash_line_continuation_with_line_number() -> None:
    with pytest.raises(UnsupportedRequirementSyntax) as raised:
        parse_requirements(b"requests>=2, \\" + b"\n<3\n")

    assert raised.value.line_no == 1


def test_rejects_bytes_that_are_neither_utf8_nor_gbk() -> None:
    with pytest.raises(RequirementsDecodeError):
        parse_requirements(b"requests\n\x81")


def test_accepts_input_at_exact_byte_limit() -> None:
    raw = b"#" + (b"x" * (512 * 1024 - 2)) + b"\n"

    result = parse_requirements(raw)

    assert result.items == ()
    assert result.normalized_text == "\n"


def test_rejects_input_over_byte_limit_before_decoding() -> None:
    with pytest.raises(InputTooLargeError) as raised:
        parse_requirements(b"x" * (512 * 1024 + 1))

    assert raised.value.limit == 512 * 1024


def test_accepts_exactly_two_thousand_logical_lines() -> None:
    raw = "".join(f"pkg{line}\n" for line in range(2000)).encode()

    result = parse_requirements(raw)

    assert len(result.items) == 2000
    assert result.items[-1].line_no == 2000


def test_rejects_more_than_two_thousand_logical_lines() -> None:
    raw = "".join(f"pkg{line}\n" for line in range(2001)).encode()

    with pytest.raises(TooManyLinesError) as raised:
        parse_requirements(raw)

    assert raised.value.limit == 2000


def test_merges_canonical_duplicates_constraints_and_extras() -> None:
    result = parse_requirements(
        b"My_Pkg[beta]>=1\nmy-pkg[alpha]<=3\nmy.pkg>=1\n"
    )

    assert len(result.items) == 1
    item = result.items[0]
    assert item.name == "my-pkg"
    assert item.extras == ("alpha", "beta")
    assert item.specifier == ">=1,<=3"
    assert item.line_no == 1
    assert item.original_text == "My_Pkg[beta]>=1"
    assert result.normalized_text == "my-pkg[alpha,beta]>=1,<=3\n"


@pytest.mark.parametrize(
    "line",
    [
        "demo>=3,<=2",
        "demo==1,>=2",
        "demo~=2.4,>=3",
    ],
)
def test_rejects_conflicting_constraints_on_first_occurrence(line: str) -> None:
    with pytest.raises(RequirementConstraintConflictError) as raised:
        parse_requirements((line + "\n").encode())

    assert raised.value.line_no == 1
    assert raised.value.name == "demo"
    assert raised.value.original_text == line


def test_keeps_same_name_with_different_markers_as_independent_items() -> None:
    result = parse_requirements(
        b'demo==1; python_version < "3.11"\n'
        b'demo==2; python_version >= "3.11"\n'
    )

    assert len(result.items) == 2
    assert [item.specifier for item in result.items] == ["==1", "==2"]
    assert [item.line_no for item in result.items] == [1, 2]


@pytest.mark.parametrize(
    "lines",
    [
        "demo==1\ndemo==2\n",
        "demo==1\ndemo>=2\n",
        "demo>=3\ndemo<=2\n",
        "demo~=2.4\ndemo==3.0\n",
        "demo~=2.4.1\ndemo==2.5\n",
    ],
)
def test_rejects_obvious_duplicate_conflicts(lines: str) -> None:
    with pytest.raises(DuplicateRequirementConflictError) as raised:
        parse_requirements(lines.encode())

    assert raised.value.line_no == 2
    assert raised.value.name == "demo"


@pytest.mark.parametrize(
    ("compatible", "lower"),
    [
        ("1.4.5", "1.5.0.dev0"),
        ("1.4.5", "1.5.0a1"),
        ("1.4.5", "1.5.0b1"),
        ("1.4.5", "1.5.0rc1"),
        ("1.4.5", "1.5.0.post1"),
        ("1!2.4.5", "1!2.5.0a1"),
    ],
)
def test_rejects_compatible_release_next_prefix_boundaries(
    compatible: str, lower: str
) -> None:
    lines = f"demo~={compatible}\ndemo>={lower}\n"

    with pytest.raises(DuplicateRequirementConflictError):
        parse_requirements(lines.encode())


def test_rejects_multiple_compatible_constraints_with_excluded_lower_bound() -> None:
    with pytest.raises(RequirementConstraintConflictError):
        parse_requirements(b"demo~=1.4.5,~=1.4.6,>=1.5.0.dev0\n")


@pytest.mark.parametrize(
    "line",
    [
        "demo~=1.4.5,>=1.4.6.dev0",
        "demo~=1.4.5,>=1.4.5.post1",
        "demo~=1!2.4.5,>=1!2.4.6rc1",
        "demo~=1.4.5,~=1.4.6,<=1.4.9",
    ],
)
def test_accepts_compatible_release_bounds_within_the_allowed_prefix(line: str) -> None:
    result = parse_requirements((line + "\n").encode())

    assert result.items[0].name == "demo"


def test_accepts_public_and_local_exact_pin_intersection() -> None:
    result = parse_requirements(b"demo==1.0\ndemo==1.0+local\n")

    assert result.items[0].specifier == "==1.0,==1.0+local"


def test_rejects_distinct_local_exact_pins() -> None:
    with pytest.raises(DuplicateRequirementConflictError):
        parse_requirements(b"demo==1.0+foo\ndemo==1.0+bar\n")


@pytest.mark.parametrize(
    "lines",
    [
        "demo==1.0.post1\ndemo>=1.0.post1\n",
        "demo==1.0.dev1\ndemo<=1.0.dev1\n",
    ],
)
def test_accepts_post_and_dev_exact_candidates(lines: str) -> None:
    result = parse_requirements(lines.encode())

    assert len(result.items) == 1


def test_merges_overlapping_compatible_release_constraints() -> None:
    result = parse_requirements(b"demo~=2.4\ndemo>=2.5\ndemo<=2.9\n")

    assert len(result.items) == 1
    assert result.items[0].specifier == "~=2.4,>=2.5,<=2.9"


def test_invalid_requirement_reports_the_source_line() -> None:
    with pytest.raises(InvalidRequirementError) as raised:
        parse_requirements(b"requests\nnot a requirement !\n")

    assert raised.value.line_no == 2
    assert raised.value.original_text == "not a requirement !"
