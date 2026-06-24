"""Sensitivity/secret detection — the single gate that forces a memory candidate
down the approval path, refuses silent persistence, and masks the SSE chip
(``services.memory.sensitivity.has_secret``). A miss here means a credential is
auto-saved in cleartext AND embedded into the (unencrypted) search index, so the
detector's coverage is load-bearing.
"""
from services.memory.models import Sensitivity
from services.memory.sensitivity import classify_sensitivity, has_secret


class TestUriUserinfoCredentials:
    """Credentials embedded in a connection-string URI userinfo — the keyword
    rule (``password=``) never sees these, but they are the common way a user
    pastes a service credential."""

    def test_redis_uri_with_password_is_secret(self):
        assert has_secret("the staging redis is redis://default:Sup3rS3cret@10.0.0.5:6379")

    def test_postgres_uri_with_password_is_secret(self):
        assert has_secret("postgres://admin:hunter2pass@db.internal:5432/app")

    def test_amqp_uri_with_password_is_secret(self):
        assert has_secret("amqp://guest:guestpw99@rabbit:5672")

    def test_uri_without_password_is_not_secret(self):
        # No userinfo password → not a credential (just a URL).
        assert not has_secret("see https://example.com/docs/setup")


class TestBareHighEntropyToken:
    """A rotated deploy/access token pasted with no ``sk-``/``key:`` prefix."""

    def test_bare_hex_deploy_token_is_secret(self):
        assert has_secret(
            "the deploy token is 4f9a1c8e7b2d3a6f5e0c9b8a7d6e5f4c3b2a1d0e")

    def test_long_word_without_digits_is_not_a_token(self):
        # Mixed-charset lookahead requires a digit — ordinary prose never trips.
        assert not has_secret("a" * 40)

    def test_short_alphanumeric_is_not_a_token(self):
        assert not has_secret("ref abc123 def456")

    def test_hyphenated_uuid_is_not_flagged(self):
        # UUIDs are not secrets — the '-' splits them below the 32-run threshold.
        assert not has_secret("session 550e8400-e29b-41d4-a716-446655440000 expired")

    def test_underscored_record_id_is_not_flagged(self):
        assert not has_secret("memory mem_01HXYZ7Qpqr8stuvwxyz0123456789ab saved")


class TestNoFalsePositivesOnOrdinaryMemories:
    def test_plain_personal_facts_are_normal(self):
        for text in (
            "I work downtown and like tea",
            "my son is 3 years old",
            "the meeting room is B12 on floor 3",
        ):
            assert classify_sensitivity(text) == Sensitivity.NORMAL.value, text


class TestExistingPatternsStillFire:
    def test_openai_style_key(self):
        assert has_secret("sk-ABCDEFGHIJKLMNOP1234")

    def test_email_is_pii_sensitive_not_secret(self):
        assert classify_sensitivity("mail me at a@b.com") == Sensitivity.SENSITIVE.value
