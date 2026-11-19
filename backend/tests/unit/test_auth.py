"""Auth hardening (backlog #19, 2026-10-21): constant-time checks, login throttle,
refusal to run on shipped default secrets."""
from app import auth
from app.config import Settings, insecure_secrets


def test_secret_equals_and_bearer():
    assert auth.secret_equals("abc", "abc")
    assert not auth.secret_equals("abd", "abc")
    assert not auth.secret_equals("", "")                 # blank secret never means open
    assert not auth.secret_equals(None, "abc")
    assert auth.bearer_matches("Bearer tok", "tok")
    assert not auth.bearer_matches("tok", "tok")           # scheme required
    assert not auth.bearer_matches("Bearer tok", "")
    assert not auth.bearer_matches(None, "tok")


def test_login_throttle_window(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: t[0])
    auth._login_failures.clear()
    for _ in range(auth.LOGIN_MAX_FAILURES - 1):
        auth.record_login_failure()
    assert auth.login_locked_for() == 0                    # 4 failures: still allowed
    auth.record_login_failure()
    assert auth.login_locked_for() > 0                     # 5th: locked
    t[0] += auth.LOGIN_WINDOW_S + 1
    assert auth.login_locked_for() == 0                    # window passed: allowed again
    auth._login_failures.clear()


def test_insecure_defaults_are_named_not_valued():
    bad = insecure_secrets(Settings(_env_file=None))
    assert set(bad) == {"secret_key", "app_password", "webhook_secret"}
    good = Settings(_env_file=None, secret_key="x" * 40, app_password="y" * 16, webhook_secret="z" * 24)
    assert insecure_secrets(good) == []
    short = Settings(_env_file=None, secret_key="x" * 40, app_password="short", webhook_secret="z" * 24)
    assert insecure_secrets(short) == ["app_password"]
