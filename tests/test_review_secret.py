from __future__ import annotations

import pytest

from retailprintguard.api.auth import PasswordService
from retailprintguard.api.review_secret import ReviewSecretVerifier


def test_review_secret_is_argon2_only_and_fail_closed_when_unconfigured() -> None:
    assert ReviewSecretVerifier(None).configured is False
    assert ReviewSecretVerifier(None).verify("synthetic-confirmation") is False
    with pytest.raises(ValueError, match="Argon2id"):
        ReviewSecretVerifier("not-a-password-hash")


def test_review_secret_verifies_without_retaining_clear_text() -> None:
    encoded = PasswordService().hash("synthetic-confirmation")
    verifier = ReviewSecretVerifier(encoded)

    assert verifier.configured is True
    assert verifier.verify("synthetic-confirmation") is True
    assert verifier.verify("wrong-confirmation") is False
