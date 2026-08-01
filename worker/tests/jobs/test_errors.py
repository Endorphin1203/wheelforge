from wheelforge_worker.jobs.errors import sanitize_error, sanitize_text


def test_sanitizes_all_supported_secret_shapes() -> None:
    message = (
        "mysql+pymysql://worker:db-secret@db/wf "
        "custom://name:secret@example.test/path?token=query-secret&safe=yes "
        "password=plain-secret api_key: key-secret Authorization: Bearer bearer-secret"
    )

    sanitized = sanitize_text(message)

    for secret in ("db-secret", "query-secret", "plain-secret", "key-secret", "bearer-secret"):
        assert secret not in sanitized
    assert "safe=yes" in sanitized
    assert "mysql+pymysql://***@db/wf" in sanitized


def test_sanitizes_nested_exception_groups_without_losing_diagnostics() -> None:
    error = ExceptionGroup(
        "outer",
        [
            OSError("https://user:secret@example.test/simple"),
            ExceptionGroup("inner", [RuntimeError("access_token=hidden")]),
        ],
    )

    sanitized = sanitize_error(error)

    assert "ExceptionGroup: outer" in sanitized
    assert "OSError" in sanitized
    assert "RuntimeError" in sanitized
    assert "secret" not in sanitized
    assert "hidden" not in sanitized


def test_sanitizer_removes_nul_and_bounds_output() -> None:
    assert sanitize_text("a\x00" + "b" * 3000, limit=20) == "a?" + "b" * 18
