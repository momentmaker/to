from bot.bot_app import _gate_tweet_v2_on_oauth
from tests.helpers.fakes import fake_settings


def test_oauth_gate_disables_when_oauth_missing():
    settings = fake_settings(TWEET_DAILY_V2_ENABLED=True)
    # No X_* creds set in fake_settings defaults.
    _gate_tweet_v2_on_oauth(settings)
    assert settings.TWEET_DAILY_V2_ENABLED is False


def test_oauth_gate_keeps_enabled_when_oauth_complete():
    settings = fake_settings(
        TWEET_DAILY_V2_ENABLED=True,
        X_CONSUMER_KEY="a", X_CONSUMER_SECRET="b",
        X_ACCESS_TOKEN="c", X_ACCESS_TOKEN_SECRET="d",
    )
    _gate_tweet_v2_on_oauth(settings)
    assert settings.TWEET_DAILY_V2_ENABLED is True


def test_oauth_gate_noop_when_already_disabled():
    settings = fake_settings(TWEET_DAILY_V2_ENABLED=False)
    _gate_tweet_v2_on_oauth(settings)
    assert settings.TWEET_DAILY_V2_ENABLED is False


def test_oauth_gate_partial_creds_still_disables():
    """Three of four OAuth fields set — still incomplete → disable."""
    settings = fake_settings(
        TWEET_DAILY_V2_ENABLED=True,
        X_CONSUMER_KEY="a", X_CONSUMER_SECRET="b",
        X_ACCESS_TOKEN="c", X_ACCESS_TOKEN_SECRET="",
    )
    _gate_tweet_v2_on_oauth(settings)
    assert settings.TWEET_DAILY_V2_ENABLED is False
