"""web_search regressions.

DuckDuckGo sometimes answers with an anti-bot "anomaly" interstitial carrying
no results at all. The search tool posts to its HTML form first, then falls
back to Bing's structured RSS feed when that provider is challenged or down.

The fixtures below are trimmed from live responses captured on 2026-08-13 —
both the 202 interstitial and a real 200 result page — so the parser is pinned
against DuckDuckGo's actual markup rather than a guess at it.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from cagentic.tools import ToolContext, _ddg_clean, _ddg_unwrap, t_web_search

# A result block, verbatim in structure from the live page.
_RESULT = """
  <div class="result results_links results_links_deep web-result ">
    <div class="links_main links_deep result__body">
      <h2 class="result__title">
        <a rel="nofollow" class="result__a" href="{url}">{title}</a>
      </h2>
      <div class="result__extras">
        <div class="result__extras__url">
          <a class="result__url" href="{url}">{display}</a>
        </div>
      </div>
      <a class="result__snippet" href="{url}">{snippet}</a>
    </div>
  </div>
"""

_RESULTS_PAGE = (
    "<html><body>"
    + "".join(
        _RESULT.format(url=u, title=t, display=u, snippet=s)
        for u, t, s in [
            (
                "https://developers.openai.com/api/docs/pricing",
                "Pricing - OpenAI API",
                "Latest <b>pricing</b> for the API.",
            ),
            (
                "https://realpython.com/async-io-python/",
                "Python&#x27;s asyncio: A Hands-On Walkthrough",
                "A tour of async/await.",
            ),
            ("https://example.com/third", "Third", "Third snippet."),
        ]
    )
    + "</body></html>"
)

# The 202 body: an interstitial that names itself and carries zero results.
_CHALLENGE_PAGE = """<!DOCTYPE html><html><head><title>DuckDuckGo</title></head>
<body><div class="anomaly-modal__title">Unfortunately, bots use DuckDuckGo too.</div>
<form class="challenge-form"></form></body></html>"""

_RSS_PAGE = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>Bing: openai pricing</title>
  <item>
    <title>OpenAI API Pricing</title>
    <link>https://openai.com/api/pricing/</link>
    <description>Current &lt;b&gt;API pricing&lt;/b&gt; and details.</description>
  </item>
  <item>
    <title>Pricing guide</title>
    <link>https://example.com/pricing</link>
    <description>A practical guide.</description>
  </item>
</channel></rss>"""


def _blocks(out: str) -> list[str]:
    """One entry per result. Splitting on a bare `- ` would cut inside titles
    like "Pricing - OpenAI API", so anchor on the line start."""
    import re

    return [b for b in re.split(r"^- ", out, flags=re.MULTILINE) if b.strip()]


class _FakeResponse:
    def __init__(self, status: int, text: str) -> None:
        self.status_code = status
        self.text = text
        self.content = text.encode("utf-8")
        self.encoding = "utf-8"


class _SearchCase(unittest.TestCase):
    """Swap both provider requests so tests never use the network."""

    def run_search(
        self,
        response: _FakeResponse,
        *,
        fallback: _FakeResponse | None = None,
        **args,
    ) -> str:
        import requests

        seen: dict = {}

        def fake_post(url, **kw):
            seen["url"] = url
            seen.update(kw)
            return response

        def fake_get(url, **kw):
            seen["fallback_url"] = url
            seen["fallback"] = kw
            return fallback or _FakeResponse(200, _RSS_PAGE)

        original = requests.post
        original_get = requests.get
        requests.post = fake_post
        requests.get = fake_get
        try:
            out = t_web_search({"query": "openai pricing", **args}, ToolContext(root=Path(".")))
        finally:
            requests.post = original
            requests.get = original_get
        self.seen = seen
        return out


class TestTheRequestItself(_SearchCase):
    def test_it_posts_to_the_endpoint_that_is_not_challenge_gated(self) -> None:
        """The whole bug: GET on duckduckgo.com/html/ returns the 202 page."""
        self.run_search(_FakeResponse(200, _RESULTS_PAGE))
        self.assertEqual(self.seen["url"], "https://html.duckduckgo.com/html/")
        self.assertEqual(self.seen["data"], {"q": "openai pricing"})

    def test_it_sends_a_referer(self) -> None:
        self.run_search(_FakeResponse(200, _RESULTS_PAGE))
        self.assertIn("duckduckgo.com", self.seen["headers"]["Referer"])

    def test_insecure_ssl_still_reaches_verify(self) -> None:
        import requests

        seen: dict = {}
        original = requests.post
        requests.post = lambda url, **kw: (
            seen.update(kw),
            _FakeResponse(200, _RESULTS_PAGE),
        )[1]
        try:
            ctx = ToolContext(root=Path("."))
            ctx.insecure_ssl = True
            t_web_search({"query": "x"}, ctx)
        finally:
            requests.post = original
        self.assertFalse(seen["verify"])


class TestParsing(_SearchCase):
    def test_results_are_listed_with_urls(self) -> None:
        out = self.run_search(_FakeResponse(200, _RESULTS_PAGE))
        self.assertIn("Pricing - OpenAI API", out)
        self.assertIn("https://developers.openai.com/api/docs/pricing", out)

    def test_entities_are_decoded(self) -> None:
        """`&#x27;` used to reach the model, which quoted it back verbatim."""
        out = self.run_search(_FakeResponse(200, _RESULTS_PAGE))
        self.assertIn("Python's asyncio", out)
        self.assertNotIn("&#x27;", out)

    def test_each_result_gets_its_own_snippet(self) -> None:
        """Pairing is positional, so an off-by-one would attach result 2's
        snippet to result 1."""
        blocks = _blocks(self.run_search(_FakeResponse(200, _RESULTS_PAGE)))
        self.assertIn("Latest pricing for the API.", blocks[0])
        self.assertNotIn("A tour of async/await.", blocks[0])
        self.assertIn("A tour of async/await.", blocks[1])

    def test_limit_is_honoured(self) -> None:
        out = self.run_search(_FakeResponse(200, _RESULTS_PAGE), limit=2)
        self.assertEqual(len(_blocks(out)), 2)

    def test_a_snippet_shipped_as_a_div_still_parses(self) -> None:
        """DDG has shipped it as both <a> and <div>; losing every snippet on
        their next markup change would be a silent regression."""
        page = _RESULTS_PAGE.replace('<a class="result__snippet"', '<div class="result__snippet"')
        page = page.replace("</a>\n    </div>", "</div>\n    </div>")
        self.assertIn("Latest pricing", self.run_search(_FakeResponse(200, page)))

    def test_a_result_with_no_snippet_still_appears(self) -> None:
        page = _RESULTS_PAGE.replace('class="result__snippet"', 'class="other"')
        out = self.run_search(_FakeResponse(200, page))
        self.assertIn("Pricing - OpenAI API", out)
        self.assertFalse(out.startswith("ERROR:"))


class TestRedirectUnwrapping(unittest.TestCase):
    def test_a_wrapped_link_becomes_its_destination(self) -> None:
        """The model is handed these to fetch; the redirector is useless to it."""
        self.assertEqual(
            _ddg_unwrap("//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&rut=abc"),
            "https://example.com/a",
        )

    def test_a_direct_link_is_untouched(self) -> None:
        self.assertEqual(_ddg_unwrap("https://example.com/a"), "https://example.com/a")

    def test_a_wrapper_with_no_target_falls_back_to_the_original(self) -> None:
        href = "//duckduckgo.com/l/?rut=abc"
        self.assertEqual(_ddg_unwrap(href), href)


class TestFailureModesAreDistinguished(_SearchCase):
    """Provider failures fall through without hiding total search failure."""

    def test_the_202_interstitial_falls_back_to_structured_results(self) -> None:
        out = self.run_search(_FakeResponse(202, _CHALLENGE_PAGE))
        self.assertFalse(out.startswith("ERROR:"))
        self.assertIn("OpenAI API Pricing", out)
        self.assertEqual(self.seen["fallback_url"], "https://www.bing.com/search")
        self.assertEqual(self.seen["fallback"]["params"]["format"], "rss")

    def test_fallback_rss_is_cleaned_and_includes_snippets(self) -> None:
        out = self.run_search(_FakeResponse(202, _CHALLENGE_PAGE))
        self.assertIn("https://openai.com/api/pricing/", out)
        self.assertIn("Current API pricing and details.", out)
        self.assertNotIn("<b>", out)

    def test_a_challenge_served_with_200_still_uses_the_fallback(self) -> None:
        self.assertIn("OpenAI API Pricing", self.run_search(_FakeResponse(200, _CHALLENGE_PAGE)))

    def test_a_primary_server_error_also_uses_the_fallback(self) -> None:
        out = self.run_search(_FakeResponse(503, "<html>Service Unavailable</html>"))
        self.assertIn("OpenAI API Pricing", out)

    def test_both_provider_failures_are_reported_together(self) -> None:
        out = self.run_search(
            _FakeResponse(202, _CHALLENGE_PAGE),
            fallback=_FakeResponse(503, "Service unavailable"),
        )
        self.assertTrue(out.startswith("ERROR: all search providers failed"))
        self.assertIn("bot check", out)
        self.assertIn("Bing returned HTTP 503", out)
        self.assertIn("Do not immediately retry", out)

    def test_a_genuinely_empty_search_is_not_an_error(self) -> None:
        out = self.run_search(_FakeResponse(200, "<html><body>No results.</body></html>"))
        self.assertEqual(out, "(no results)")
        self.assertFalse(out.startswith("ERROR:"))

    def test_results_are_returned_even_if_the_status_is_odd(self) -> None:
        """Parsing succeeded, so the status code is not worth failing over."""
        out = self.run_search(_FakeResponse(202, _RESULTS_PAGE))
        self.assertIn("Pricing - OpenAI API", out)
        self.assertFalse(out.startswith("ERROR:"))

    def test_a_transport_failure_is_described_not_dumped(self) -> None:
        """The urllib3/SSL traceback leaking into the transcript is the bug the
        error-surface work fixed; keep this path routed through it."""
        import requests

        original = requests.post
        original_get = requests.get

        def boom(*a, **k):
            raise requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED")

        requests.post = boom
        requests.get = boom
        try:
            out = t_web_search({"query": "x"}, ToolContext(root=Path(".")))
        finally:
            requests.post = original
            requests.get = original_get
        self.assertTrue(out.startswith("ERROR:"))
        self.assertNotIn("Traceback", out)
        self.assertIn("certificate", out.lower())


class TestClean(unittest.TestCase):
    def test_tags_are_stripped_and_whitespace_collapsed(self) -> None:
        """The markup's own newlines would otherwise break the one-line-per-
        field layout the model reads."""
        self.assertEqual(_ddg_clean("<b>Latest</b>\n   pricing\n  here"), "Latest pricing here")

    def test_entities_are_decoded(self) -> None:
        self.assertEqual(_ddg_clean("A &amp; B &#x27;C&#x27;"), "A & B 'C'")


if __name__ == "__main__":
    unittest.main()
