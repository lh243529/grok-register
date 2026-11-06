"""Mailbox HTTP must bypass application leases and environment proxies."""
import os
from pathlib import Path
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from curl_cffi import CurlOpt
import browser_runtime
import cf_mail_debug
import grok_register_ttk as app
import mail_service
import outlook_mail
import preflight
import proxy_pool
from proxy_pool import ProxyLease
from registration_parallel import load_isolated_module


class MailDirectTransportTests(unittest.TestCase):
    def test_curl_session_forces_direct_and_preserves_request_options(self):
        with patch.object(mail_service.requests, "Session") as factory:
            session = factory.return_value.__enter__.return_value
            response = mail_service.http_post(
                "https://mail.example.test/api/mails", json={"address": "user"},
                headers={"Authorization": "Bearer test"}, params={"limit": 20},
                timeout=23, replay_safe=True, proxy="http://bad:1",
                proxies={"all": "http://bad:2"}, proxy_auth=("user", "pass"),
                trust_env=True, curl_options={CurlOpt.PROXY: "http://bad:3"},
            )
        factory.assert_called_once_with(trust_env=False, curl_options={CurlOpt.PROXY: ""})
        session.request.assert_called_once_with(
            "POST", "https://mail.example.test/api/mails", proxy=None,
            json={"address": "user"}, headers={"Authorization": "Bearer test"},
            params={"limit": 20}, timeout=23,
        )
        self.assertIs(response, session.request.return_value)
        factory.return_value.__exit__.assert_called_once_with(None, None, None)

    def test_default_timeout_and_each_request_has_its_own_session(self):
        with patch.object(mail_service.requests, "Session") as factory:
            mail_service.http_get("https://mail.example.test/domains")
            mail_service.http_get("https://mail.example.test/messages")
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(factory.return_value.__exit__.call_count, 2)
        for call in factory.return_value.__enter__.return_value.request.call_args_list:
            self.assertEqual(call.kwargs, {"proxy": None, "timeout": 15})

    def test_transport_failure_is_not_retried_or_converted_to_proxy_error(self):
        error = RuntimeError("CONNECT tunnel failed 502")
        with patch.object(mail_service.requests, "Session") as factory:
            session = factory.return_value.__enter__.return_value
            session.request.side_effect = error
            with self.assertRaises(RuntimeError) as caught:
                mail_service.http_post("https://mail.example.test/api/new_address")
        self.assertIs(caught.exception, error)
        session.request.assert_called_once()
        factory.return_value.__exit__.assert_called_once()
        self.assertIs(factory.return_value.__exit__.call_args.args[1], error)

    def test_all_temp_mail_endpoints_use_the_direct_transport(self):
        base = "https://mail.example.test"
        address = "user@example.test"
        config = {
            "cloudflare_api_base": base, "cloudflare_auth_mode": "none",
            "cloudflare_path_accounts": "/api/new_address",
            "cloudflare_path_messages": "/api/mails",
            "cloudmail_api_base": base, "cloudmail_public_token": "public-test",
        }
        client = mail_service.CloudflareMailClient(base)
        cases = [
            (mail_service.cloudflare_create_temp_address, (base,), {}, "POST", "/api/new_address", {"address": address, "jwt": "jwt"}),
            (mail_service.cloudflare_create_account, (base, address, "pass"), {}, "POST", "/api/new_address", {}),
            (mail_service.cloudflare_get_domains, (base,), {}, "GET", "/domains", []),
            (mail_service.cloudflare_get_token, (base, address, "pass"), {}, "POST", "/token", {"token": "test"}),
            (mail_service.cloudflare_get_messages, (base, "jwt"), {}, "GET", "/api/mails", []),
            (mail_service.cloudflare_get_message_detail, (base, "jwt", "1"), {}, "GET", "/api/mail/1", {}),
            (mail_service.cloudmail_get_messages, (address,), {}, "POST", "/api/public/emailList", {"code": 200, "data": []}),
            (mail_service.create_account, (address, "pass"), {}, "POST", "/accounts", {}),
            (mail_service.get_domains, (), {}, "GET", "/domains", {"hydra:member": []}),
            (mail_service.get_token, (address, "pass"), {}, "POST", "/token", {"token": "test"}),
            (mail_service.get_messages, ("jwt",), {}, "GET", "/messages", {"hydra:member": []}),
            (mail_service.get_message_detail, ("jwt", "1"), {}, "GET", "/messages/1", {}),
            (mail_service.yyds_create_account, (), {"address": address}, "POST", "/accounts", {"success": True, "data": {}}),
            (mail_service.yyds_get_domains, (), {}, "GET", "/domains", {"success": True, "data": []}),
            (mail_service.yyds_get_token, (address,), {}, "POST", "/token", {"success": True, "data": {"token": "test"}}),
            (mail_service.yyds_get_messages, (address,), {}, "GET", "/messages", {"success": True, "data": {"messages": []}}),
            (mail_service.yyds_get_message_detail, ("1",), {}, "GET", "/messages/1", {"success": True, "data": {}}),
            (client.create_address, (), {}, "POST", "/api/new_address", {"address": address, "jwt": "jwt"}),
            (client.fetch_box, ("jwt", "/api/mails", {"limit": 20}), {}, "GET", "/api/mails", []),
            (client.get_detail, ("jwt", "1"), {}, "GET", "/api/mail/1", {}),
        ]
        with patch.object(mail_service, "config", config):
            for fn, args, kwargs, method, suffix, payload in cases:
                with self.subTest(endpoint=fn.__name__), patch.object(mail_service.requests, "Session") as factory:
                    response = factory.return_value.__enter__.return_value.request.return_value
                    response.status_code = 200
                    response.json.return_value = payload
                    fn(*args, **kwargs)
                    factory.assert_called_once_with(trust_env=False, curl_options={CurlOpt.PROXY: ""})
                    request = factory.return_value.__enter__.return_value.request
                    request.assert_called_once()
                    self.assertEqual(request.call_args.args[0], method)
                    self.assertTrue(request.call_args.args[1].endswith(suffix))
                    self.assertIsNone(request.call_args.kwargs["proxy"])
                    self.assertNotIn("replay_safe", request.call_args.kwargs)

    def test_detail_fallback_and_debug_boxes_stay_on_direct_transport(self):
        client = mail_service.CloudflareMailClient("https://mail.example.test")
        with patch.object(mail_service.requests, "Session") as factory:
            request = factory.return_value.__enter__.return_value.request
            first = Mock(status_code=404)
            second = Mock(status_code=200)
            second.json.return_value = {"id": "1"}
            request.side_effect = [first, second]
            self.assertEqual(client.get_detail("jwt", "1"), {"id": "1"})
            self.assertEqual(factory.call_count, 2)
            self.assertTrue(request.call_args.args[1].endswith("/api/mails/1"))
            request.side_effect = None
            request.return_value = second
            client.probe_all_boxes("jwt")
            self.assertEqual(factory.call_count, 8)

    def test_runtime_binding_cannot_replace_worker_transports(self):
        root = Path(mail_service.__file__).resolve()
        for worker_id in (1, 2):
            worker = load_isolated_module(root, "_test_mail_direct_worker_%s" % worker_id)
            get, post = worker.http_get, worker.http_post
            configured = {"proxy": "http://bad:1"}
            cancel = Mock()
            injected = Mock(side_effect=AssertionError("shared HTTP used"))
            worker.bind_runtime({"config": configured, "http_get": injected,
                                 "http_post": injected, "raise_if_cancelled": cancel})
            self.assertIs(worker.http_get, get)
            self.assertIs(worker.http_post, post)
            self.assertIs(worker.config, configured)
            self.assertIs(worker.raise_if_cancelled, cancel)
            with patch.object(worker.requests, "Session") as factory:
                worker.http_get("https://mail.example.test/domains")
                factory.assert_called_once()
            injected.assert_not_called()

    def test_single_thread_entry_and_preflight_use_direct_transport(self):
        config = {"email_provider": "cloudflare", "cloudflare_api_base": "https://mail.example.test"}
        get, post = mail_service.http_get, mail_service.http_post
        with patch.object(app, "config", config), patch.object(mail_service, "config", config), \
             patch.object(app, "http_get", side_effect=AssertionError("shared HTTP used")), \
             patch.object(app, "http_post", side_effect=AssertionError("shared HTTP used")), \
             patch.object(mail_service.requests, "Session") as factory:
            response = factory.return_value.__enter__.return_value.request.return_value
            response.status_code = 200
            response.json.return_value = [{"domain": "example.test"}]
            app.cloudflare_get_domains(config["cloudflare_api_base"])
            self.assertIn("服务连接成功", preflight._check_mailbox_connection(config))
            self.assertEqual(factory.call_count, 2)
        self.assertIs(mail_service.http_get, get)
        self.assertIs(mail_service.http_post, post)

    def test_browser_and_generic_http_keep_their_proxy_configuration(self):
        old_config = browser_runtime._config
        old_lease = proxy_pool.current_proxy_lease()
        try:
            for configured, lease in (
                ({"proxy_mode": "direct"}, None),
                ({"proxy_mode": "auto", "proxy": "http://legacy:7890"}, None),
                ({"proxy_mode": "pool"}, ProxyLease("node", "http://lease:7890", "w", 1, 1, "a", "s")),
            ):
                with self.subTest(config=configured):
                    browser_runtime.configure_runtime(configured)
                    proxy_pool._TLS.lease = lease
                    expected = lease.proxy_url if lease else configured.get("proxy", "")
                    with patch.object(mail_service.requests, "Session"), \
                         patch.object(browser_runtime.requests, "get") as generic, \
                         patch.object(browser_runtime, "prepare_chromium_proxy", return_value=(expected, None)) as prepare:
                        mail_service.http_get("https://mail.example.test/domains")
                        self.assertEqual(mail_service._cloudmail_network_label(), "direct")
                        browser_runtime.http_get("https://example.test")
                        self.assertEqual(browser_runtime.prepare_browser_proxy()[0], expected)
                        if expected:
                            prepare.assert_called_once()
                            self.assertEqual(generic.call_args.kwargs["proxies"], {"http": expected, "https": expected})
                        else:
                            prepare.assert_not_called()
                            self.assertNotIn("proxies", generic.call_args.kwargs)
        finally:
            browser_runtime.configure_runtime(old_config)
            proxy_pool._TLS.lease = old_lease

    def test_outlook_session_is_direct_and_closed_after_retry(self):
        with patch.object(outlook_mail.requests, "Session") as factory, \
             patch.object(outlook_mail, "_sleep_interruptibly") as sleep:
            session = factory.return_value.__enter__.return_value
            response = Mock(status_code=200)
            session.request.side_effect = [outlook_mail.requests.ConnectionError("temporary"), response]
            result = outlook_mail._request_with_backoff(
                "POST", outlook_mail.MICROSOFT_COMMON_TOKEN_URL,
                data={"refresh_token": "test"}, timeout=12,
                proxies={"https": "http://bad:1"},
            )
        self.assertIs(result, response)
        self.assertFalse(session.trust_env)
        factory.assert_called_once_with()
        self.assertEqual(session.request.call_count, 2)
        self.assertEqual(session.request.call_args.kwargs, {"data": {"refresh_token": "test"}, "timeout": 12})
        sleep.assert_called_once()
        factory.return_value.__exit__.assert_called_once_with(None, None, None)

    def test_outlook_closes_session_on_failure_and_cancellation(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled), patch.object(outlook_mail.requests, "Session") as factory:
                session = factory.return_value.__enter__.return_value
                session.request.side_effect = outlook_mail.requests.RequestException("failed")
                with self.assertRaises((outlook_mail.requests.RequestException, RuntimeError)):
                    outlook_mail._request_with_backoff("GET", "https://mail.example.test", cancel_callback=lambda: cancelled)
                factory.return_value.__exit__.assert_called_once()
                self.assertEqual(session.request.call_count, 0 if cancelled else 1)


class _MailboxHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
            return
        body = b'{"address":"user@example.test","jwt":"test-jwt","data":[]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.do_GET()

    def log_message(self, *args):
        pass


class MailDirectLoopbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _MailboxHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%s" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_real_mail_sessions_ignore_bad_proxy_environment_and_redirects(self):
        env = {key: "http://127.0.0.1:1" for key in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
        )}
        env.update({"NO_PROXY": "", "no_proxy": ""})
        with patch.dict(os.environ, env):
            before = dict(os.environ)
            for path in ("/ok", "/redirect"):
                with self.subTest(path=path):
                    self.assertEqual(mail_service.http_get(self.base + path, timeout=3).status_code, 200)
                    self.assertEqual(outlook_mail._request_with_backoff("GET", self.base + path, timeout=3).status_code, 200)
            self.assertEqual(mail_service.http_post(self.base + "/ok", json={}, replay_safe=True).status_code, 200)
            self.assertEqual(outlook_mail._request_with_backoff("POST", self.base + "/ok", data={"token": "test"}, timeout=3).status_code, 200)
            self.assertEqual(cf_mail_debug.create_address(self.base), ("user@example.test", "test-jwt"))
            client = mail_service.CloudflareMailClient(self.base, timeout=3)
            self.assertEqual(client.fetch_box("jwt", "/api/mails", {}), [])
            self.assertIn("address", client.get_detail("jwt", "1"))
            self.assertEqual(dict(os.environ), before)


if __name__ == "__main__":
    unittest.main()
