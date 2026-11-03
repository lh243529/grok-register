"""Offline regression coverage for signup rendering and interaction timing."""

import unittest
from unittest.mock import Mock, patch

import browser_runtime
import registration_browser


class SignupInteractionTimingTests(unittest.TestCase):
    def test_signup_page_waits_four_seconds_before_interaction(self):
        events = []
        page = Mock()
        page.get.side_effect = lambda _url: events.append("navigate")
        page.wait.doc_loaded.side_effect = lambda: events.append("loaded")
        browser = Mock()
        browser.get_tab.return_value = page
        with patch.object(registration_browser, "browser", browser), \
             patch.object(registration_browser, "page", None), \
             patch.object(registration_browser, "browser_started_with_proxy", False), \
             patch.object(registration_browser, "raise_if_cancelled", create=True), \
             patch.object(registration_browser, "_mark_registration_stage"), \
             patch.object(registration_browser.time, "sleep", side_effect=lambda seconds: events.append(("sleep", seconds))), \
             patch.object(registration_browser, "click_email_signup_button", side_effect=lambda **_kwargs: events.append("interact")):
            registration_browser.open_signup_page()
        page.get.assert_called_once_with(registration_browser.SIGNUP_URL)
        self.assertEqual(events, ["navigate", "loaded", ("sleep", 4), "interact"])

    def test_email_entry_hovers_before_click(self):
        button = Mock()
        with patch.object(registration_browser, "_raise_if_email_signup_unavailable"), \
             patch.object(registration_browser, "_run_pre_submit_js", return_value=button) as find_button, \
             patch.object(registration_browser, "raise_if_cancelled", create=True), \
             patch.object(registration_browser, "sleep_with_cancel", create=True):
            self.assertTrue(registration_browser.click_email_signup_button())
        self.assertEqual([call[0] for call in button.mock_calls], ["hover", "click"])
        script = find_button.call_args.args[0]
        self.assertIn("return target;", script)
        self.assertNotIn("target.click()", script)

    def test_cancel_after_hover_prevents_click(self):
        button = Mock()
        with patch.object(registration_browser, "_raise_if_email_signup_unavailable"), \
             patch.object(registration_browser, "_run_pre_submit_js", return_value=button), \
             patch.object(registration_browser, "raise_if_cancelled", side_effect=[None, None, InterruptedError("cancelled")], create=True):
            with self.assertRaises(InterruptedError):
                registration_browser.click_email_signup_button()
        button.hover.assert_called_once_with()
        button.click.assert_not_called()

    def test_browser_options_keep_standard_settings(self):
        options = Mock()
        with patch.object(browser_runtime, "ChromiumOptions", return_value=options), \
             patch.object(browser_runtime, "_configure_browser_environment") as environment, \
             patch.object(browser_runtime, "apply_browser_proxy_option") as proxy, \
             patch.object(browser_runtime, "_resolve_extension_path", return_value="test-extension"):
            self.assertIs(browser_runtime.create_browser_options(browser_proxy="http://127.0.0.1:8080"), options)
        options.auto_port.assert_called_once_with()
        options.set_timeouts.assert_called_once_with(base=10, page_load=60, script=30)
        environment.assert_called_once_with(options)
        proxy.assert_called_once_with(options, "http://127.0.0.1:8080")
        options.add_extension.assert_called_once_with("test-extension")


if __name__ == "__main__":
    unittest.main()
