from __future__ import annotations

import ssl

import aiohttp

from school_discord_bot.services.school_news_client import SchoolNewsClient


def build_client(*, allow_insecure_ssl_fallback: bool = True) -> SchoolNewsClient:
    return SchoolNewsClient(
        session=None,  # type: ignore[arg-type]
        widget_url="https://www.dali.tc.edu.tw/ischool/widget/site_news/main2.php?allbtn=0&maximize=1&uid=test",
        timeout_seconds=20,
        user_agent="test-agent",
        allow_insecure_ssl_fallback=allow_insecure_ssl_fallback,
    )


def test_should_use_insecure_ssl_fallback_for_trusted_host() -> None:
    client = build_client()
    ssl_error = ssl.SSLCertVerificationError("certificate verify failed")
    exc = aiohttp.ClientSSLError(None, ssl_error)

    assert client._should_use_insecure_ssl_fallback("https://www.dali.tc.edu.tw/home", exc)


def test_should_not_use_insecure_ssl_fallback_for_other_host() -> None:
    client = build_client()
    ssl_error = ssl.SSLCertVerificationError("certificate verify failed")
    exc = aiohttp.ClientSSLError(None, ssl_error)

    assert not client._should_use_insecure_ssl_fallback("https://example.com/home", exc)


def test_should_not_use_insecure_ssl_fallback_when_disabled() -> None:
    client = build_client(allow_insecure_ssl_fallback=False)
    ssl_error = ssl.SSLCertVerificationError("certificate verify failed")
    exc = aiohttp.ClientSSLError(None, ssl_error)

    assert not client._should_use_insecure_ssl_fallback("https://www.dali.tc.edu.tw/home", exc)