"""Phase 2 regressions — the web UI and browser-extension surfaces.

Covers the parts that are testable in Python: the upload boundary, the @-mention
image path that attachments ride on, chat search, page context, and the
per-site browser permissions. The rendering half (markdown, highlighting,
theming) is exercised in the browser; what matters here is that the routes
behave and that nothing can write outside the workspace.
"""

from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from cagentic.browser import URL_ACTIONS, _clean_site_rules, host_allowed
from cagentic.engine import _IMAGE_SUFFIXES, process_user_input


class TestSiteRules(unittest.TestCase):
    def test_no_rules_allows_everything(self) -> None:
        ok, _ = host_allowed("https://example.com/x", {"allow": [], "deny": []})
        self.assertTrue(ok)

    def test_deny_blocks_the_host_and_its_subdomains(self) -> None:
        rules = {"allow": [], "deny": ["bank.example"]}
        for url in ("https://bank.example/", "https://login.bank.example/x"):
            ok, why = host_allowed(url, rules)
            self.assertFalse(ok, url)
            self.assertIn("blocked by site rule", why)

    def test_allow_list_blocks_everything_else(self) -> None:
        rules = {"allow": ["github.com"], "deny": []}
        self.assertTrue(host_allowed("https://gist.github.com/x", rules)[0])
        ok, why = host_allowed("https://evil.test/x", rules)
        self.assertFalse(ok)
        self.assertIn("not in the allowed-sites list", why)

    def test_deny_beats_allow(self) -> None:
        rules = {"allow": ["example.com"], "deny": ["secret.example.com"]}
        self.assertTrue(host_allowed("https://www.example.com/", rules)[0])
        self.assertFalse(host_allowed("https://secret.example.com/", rules)[0])

    def test_glob_patterns(self) -> None:
        rules = {"allow": ["*.internal.test"], "deny": []}
        self.assertTrue(host_allowed("https://wiki.internal.test/", rules)[0])
        self.assertFalse(host_allowed("https://elsewhere.test/", rules)[0])

    def test_unparseable_or_hostless_url_is_refused_under_an_allow_list(self) -> None:
        rules = {"allow": ["example.com"], "deny": []}
        self.assertFalse(host_allowed("not a url", rules)[0])
        self.assertFalse(host_allowed("about:blank", rules)[0])

    def test_malformed_config_degrades_to_no_rules(self) -> None:
        self.assertEqual(_clean_site_rules("nonsense"), {"allow": [], "deny": []})
        self.assertEqual(_clean_site_rules({"allow": [1, "", "  ok  "]})["allow"], ["ok"])

    def test_url_actions_are_the_ones_the_bridge_can_pre_check(self) -> None:
        """The rest are checked extension-side, where the tab URL is known."""
        self.assertEqual(URL_ACTIONS, {"open", "navigate", "download"})


class TestImageMentions(unittest.TestCase):
    """Browser attachments reach the model through the @-mention pipeline, so
    the image branch is what makes a pasted screenshot work at all."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # A real 1x1 PNG, so the encoder isn't fed something degenerate.
        self.png_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
            "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )

    def tearDown(self) -> None:
        try:
            self._tmp.cleanup()
        except (OSError, PermissionError):
            pass

    def test_image_is_attached_as_an_image_not_decoded_as_text(self) -> None:
        (self.root / "shot.png").write_bytes(self.png_bytes)
        msg = process_user_input("look at @shot.png", workspace=self.root, home=self.root)
        self.assertEqual(len(msg.get("images") or []), 1)
        self.assertEqual(base64.b64decode(msg["images"][0]), self.png_bytes)
        # And the transcript says so rather than carrying binary mojibake.
        self.assertIn("(image attached)", msg["content"])
        self.assertNotIn("PNG", msg["content"])

    def test_text_files_still_inline_their_contents(self) -> None:
        (self.root / "a.txt").write_text("hello\nworld\n")
        msg = process_user_input("read @a.txt", workspace=self.root, home=self.root)
        self.assertNotIn("images", msg)
        self.assertIn("hello", msg["content"])

    def test_oversized_image_is_reported_not_silently_dropped(self) -> None:
        from cagentic import engine

        big = self.root / "big.png"
        big.write_bytes(b"\x89PNG" + b"0" * (engine._MAX_IMAGE_BYTES + 1))
        msg = process_user_input("see @big.png", workspace=self.root, home=self.root)
        self.assertFalse(msg.get("images"))
        self.assertIn("too large", msg["content"])

    def test_every_listed_suffix_takes_the_image_path(self) -> None:
        for suffix in sorted(_IMAGE_SUFFIXES):
            p = self.root / f"pic{suffix}"
            p.write_bytes(self.png_bytes)
            msg = process_user_input(f"@pic{suffix}", workspace=self.root, home=self.root)
            self.assertEqual(len(msg.get("images") or []), 1, suffix)


class _GatewayCase(unittest.TestCase):
    """Builds a real Gateway against a temp workspace and config dir."""

    def setUp(self) -> None:
        import os

        self._cfgdir = tempfile.TemporaryDirectory()
        os.environ["XDG_CONFIG_HOME"] = self._cfgdir.name
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

        from cagentic import config
        from cagentic.agent import Agent
        from cagentic.gateway import Gateway
        from cagentic.ollama_client import OllamaClient

        cfg = config.load()
        agent = Agent(
            client=OllamaClient(host="http://localhost:11434"),
            model="test",
            root=self.root,
            config=cfg,
        )
        self.gw = Gateway(agent, cfg, port=0)

    def tearDown(self) -> None:
        for d in (self._tmp, self._cfgdir):
            try:
                d.cleanup()
            except (OSError, PermissionError):
                pass


class TestUploads(_GatewayCase):
    def _upload(self, name: str, data: bytes):
        return self.gw.save_upload(name, base64.b64encode(data).decode("ascii"))

    def test_saves_inside_the_workspace_and_returns_a_relative_path(self) -> None:
        payload, status = self._upload("notes.txt", b"hello")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(Path(payload["path"]).is_absolute(), payload["path"])
        landed = self.root / payload["path"]
        self.assertTrue(landed.is_file())
        self.assertEqual(landed.read_bytes(), b"hello")

    def test_a_traversing_name_cannot_escape_the_workspace(self) -> None:
        payload, status = self._upload("../../../etc/passwd", b"x")
        self.assertEqual(status, 200, payload)
        # The name is flattened, so it lands inside the uploads dir.
        landed = self.root / payload["path"]
        self.assertTrue(landed.is_file())
        self.assertIn(".cagentic/uploads", payload["path"].replace("\\", "/"))
        self.assertNotIn("..", payload["path"])

    def test_absolute_name_is_flattened(self) -> None:
        payload, _ = self._upload("/etc/shadow", b"x")
        self.assertTrue((self.root / payload["path"]).is_file())
        self.assertTrue(payload["path"].replace("\\", "/").startswith(".cagentic/uploads"))

    def test_same_name_twice_does_not_clobber(self) -> None:
        first, _ = self._upload("a.txt", b"one")
        second, _ = self._upload("a.txt", b"two")
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual((self.root / first["path"]).read_bytes(), b"one")
        self.assertEqual((self.root / second["path"]).read_bytes(), b"two")

    def test_oversized_upload_is_rejected(self) -> None:
        from cagentic.gateway import MAX_UPLOAD_BYTES

        _payload, status = self._upload("big.bin", b"0" * (MAX_UPLOAD_BYTES + 1))
        self.assertEqual(status, 413)

    def test_bad_base64_is_rejected(self) -> None:
        payload, status = self.gw.save_upload("a.txt", "not base64!!")
        self.assertEqual(status, 400)
        self.assertIn("base64", payload["error"])

    def test_empty_name_is_rejected(self) -> None:
        _payload, status = self.gw.save_upload("...", base64.b64encode(b"x").decode())
        self.assertEqual(status, 400)

    def test_kind_is_classified_for_the_chip(self) -> None:
        self.assertEqual(self._upload("a.png", b"x")[0]["kind"], "image")
        self.assertEqual(self._upload("a.pdf", b"x")[0]["kind"], "document")
        self.assertEqual(self._upload("a.py", b"x")[0]["kind"], "text")

    def test_uploaded_file_is_reachable_through_an_at_mention(self) -> None:
        """The whole point of writing into the workspace: the existing mention
        pipeline can read it back without a second code path."""
        payload, _ = self._upload("notes.txt", b"attachment body\n")
        msg = process_user_input(
            f"summarise @{payload['path']}", workspace=self.root, home=self.root
        )
        self.assertIn("attachment body", msg["content"])

    def test_gateway_redisplays_only_the_authored_attachment_prompt(self) -> None:
        payload, _ = self._upload("private-notes.txt", b"provider-only attachment body\n")
        prompt = f"summarise @{payload['path']}"
        msg = process_user_input(prompt, workspace=self.root, home=self.root)
        msg.pop("_attachment_count", None)

        rendered = self.gw.render_messages([msg])

        self.assertEqual(rendered, [{"role": "user", "content": prompt}])
        self.assertNotIn("provider-only attachment body", rendered[0]["content"])

    def test_legacy_attachment_expansion_is_not_exposed_after_reload(self) -> None:
        prompt = "summarise @.cagentic/uploads/notes.txt"
        legacy = {
            "role": "user",
            "content": (
                prompt
                + "\n\n--- @/workspace/.cagentic/uploads/notes.txt  (1 lines total) ---\n"
                + "    1  private historical content"
            ),
        }

        rendered = self.gw.render_messages([legacy])

        self.assertEqual(rendered, [{"role": "user", "content": prompt}])


class TestPageContext(_GatewayCase):
    def test_context_rides_along_with_the_next_message(self) -> None:
        self.gw.add_page_context("Context from the page:\n- URL: https://x.test")
        merged = self.gw._consume_pending_context("what does this say?")
        self.assertIn("https://x.test", merged)
        self.assertTrue(merged.endswith("what does this say?"))

    def test_it_is_consumed_once(self) -> None:
        self.gw.add_page_context("page one")
        self.gw._consume_pending_context("first")
        self.assertEqual(self.gw._consume_pending_context("second"), "second")

    def test_empty_context_is_refused(self) -> None:
        self.assertFalse(self.gw.add_page_context("   ")["ok"])

    def test_oversized_context_is_truncated_not_dropped(self) -> None:
        from cagentic.gateway import MAX_PAGE_CONTEXT

        self.gw.add_page_context("x" * (MAX_PAGE_CONTEXT * 2))
        merged = self.gw._consume_pending_context("go")
        self.assertIn("truncated", merged)
        self.assertLess(len(merged), MAX_PAGE_CONTEXT * 2)

    def test_backlog_is_bounded(self) -> None:
        for i in range(12):
            self.gw.add_page_context(f"page {i}")
        merged = self.gw._consume_pending_context("go")
        self.assertNotIn("page 0", merged)
        self.assertIn("page 11", merged)


class TestChatSearch(_GatewayCase):
    def test_empty_query_returns_nothing(self) -> None:
        self.assertEqual(self.gw.search_chats("   ")["results"], [])

    def test_results_carry_a_snippet(self) -> None:
        from cagentic import sessions

        data = sessions.make("test")
        data["title"] = "Deployment notes"
        data["messages"] = [{"role": "user", "content": "how do I roll back the canary deploy?"}]
        sessions.save(data)

        out = self.gw.search_chats("canary")
        self.assertTrue(out["results"], out)
        hit = next(r for r in out["results"] if r["id"] == data["id"])
        self.assertIn("canary", hit["snippet"])


if __name__ == "__main__":
    unittest.main()
