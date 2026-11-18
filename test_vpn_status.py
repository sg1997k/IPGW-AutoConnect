import http.client
import queue
import unittest
from unittest.mock import Mock, patch

import app
import sso_core as core

CFG = {"username": "sample", "password": "test-only", "interval_minutes": 480}
OFFLINE = 'cb({"error":"not_online_error"});'
ONLINE = 'cb({"error":"ok","user_name":"sample"});'


class TestSessionChecks(unittest.TestCase):
    def check(self, replies, internet=False):
        with patch.object(core, "_get", side_effect=replies), \
             patch.object(core, "internet_available", return_value=internet) as probe, \
             patch.object(core, "login", return_value=(True, "ok")) as login:
            result = app.check_configured_connection(CFG)
        return result, probe.call_count, login.call_count

    def test_vpn_disconnect_and_internet_works(self):
        response = http.client.RemoteDisconnected("connection without response")
        result, _, submissions = self.check([response], internet=True)
        self.assertEqual((result.state, submissions), ("reachable", 0))

    def test_offline_from_vpn_egress_with_working_internet(self):
        result, _, submissions = self.check([OFFLINE], internet=True)
        self.assertEqual((result.state, submissions), ("reachable", 0))

    def test_public_probe_requires_204_and_rejects_redirect(self):
        from unittest.mock import MagicMock
        redirect = core._NoRedirect()
        self.assertIsNone(redirect.redirect_request(None, None, 302, "", {}, "https://example.org"))
        for code, content, expected in [(204, b"", True), (200, b"login", False), (204, b"x", False)]:
            opener = MagicMock()
            response = opener.open.return_value.__enter__.return_value
            response.status = code
            response.read.return_value = content
            with self.subTest(code=code, content=content), patch("urllib.request.build_opener", return_value=opener):
                self.assertEqual(core.internet_available(), expected)

    def test_inconclusive_responses_never_submit(self):
        for reply in [TimeoutError(), http.client.RemoteDisconnected(), "cb([])",
                      "<html>login</html>", 'cb({"error":"busy"})', 'cb({"error":"ok"})']:
            with self.subTest(reply=reply):
                result, _, submissions = self.check([reply])
                self.assertEqual((result.state, submissions), ("unknown", 0))

    def test_valid_session_skips_external_check(self):
        result, probes, submissions = self.check([ONLINE])
        self.assertEqual((result.state, probes, submissions), ("online", 0, 0))

    def test_preserves_other_users_session(self):
        result, probes, submissions = self.check([ONLINE.replace("sample", "other")])
        self.assertEqual((result.state, probes, submissions), ("other", 0, 0))

    def test_real_dropout_and_confirmation(self):
        result, _, submissions = self.check([OFFLINE, OFFLINE, ONLINE])
        self.assertEqual((result.state, result.restored, submissions), ("online", True, 1))

    def test_state_changes_while_waiting(self):
        result, _, submissions = self.check([OFFLINE, ONLINE])
        self.assertEqual((result.state, submissions), ("online", 0))

    def test_lost_confirmation_stops_after_one_submission(self):
        result, _, submissions = self.check([OFFLINE, OFFLINE, TimeoutError()])
        self.assertEqual((result.state, submissions), ("pending", 1))

    def test_tray_and_system_cannot_submit_at_once(self):
        with core._authentication_slot() as first:
            self.assertTrue(first)
            result, _, submissions = self.check([OFFLINE])
        self.assertEqual((result.state, submissions), ("unknown", 0))

    def test_gui_neutral_status_clears_previous_offline_text(self):
        view = Mock()
        view.out = queue.Queue()
        view.out.put(("reachable", "互联网可用，校园网认证状态待确认"))
        app.App._poll(view)
        self.assertIn("网络可用", view.lbl_status.configure.call_args.kwargs["text"])
        self.assertIn("待确认", view.lbl_detail.configure.call_args.kwargs["text"])
        view._notify.assert_not_called()

    def test_monitor_reports_uncertainty_without_failure_notifications(self):
        monitor = app.Monitor(queue.Queue())
        with patch.object(app.cfgmod, "load", return_value=CFG), \
             patch.object(core, "_get", side_effect=TimeoutError), \
             patch.object(core, "internet_available", return_value=False), \
             patch.object(core, "login") as login, patch.object(monitor._wake, "wait", side_effect=lambda _: monitor.stop()):
            monitor.run()
        events = list(monitor.out.queue)
        self.assertEqual(events[0][0], "unknown")
        self.assertFalse(any(k == "notify" for k, _ in events))
        login.assert_not_called()

    def test_headless_boot_recovers_when_network_becomes_ready(self):
        import tempfile
        from pathlib import Path
        class Done(Exception):
            pass
        waits = []
        def wait(seconds):
            waits.append(seconds)
            if len(waits) == 2:
                raise Done()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(app, "_base_dir", return_value=directory), \
             patch.object(app.cfgmod, "load", return_value=CFG), \
             patch.object(core, "_get", side_effect=[TimeoutError(), OFFLINE, OFFLINE, ONLINE]), \
             patch.object(core, "internet_available", return_value=False), \
             patch.object(core, "login", return_value=(True, "ok")) as login, \
             patch("time.sleep", side_effect=wait), self.assertRaises(Done):
            try:
                app.run_headless()
            finally:
                log = (Path(directory) / "logs" / "auto.log").read_text(encoding="utf-8")
                self.assertIn("未返回有效响应", log)
                self.assertIn("校园网认证已恢复", log)
        self.assertEqual(waits, [15, 8 * 3600])
        self.assertEqual(login.call_count, 1)

    def test_retry_backoff_and_reset(self):
        count = 0
        delays = []
        for _ in range(12):
            delay, count = app.next_check("unknown", count, 480)
            delays.append(delay)
        self.assertEqual(delays[:4], [15, 30, 60, 120])
        self.assertEqual(delays[-1], 900)
        self.assertEqual(app.next_check("reachable", count, 480), (8 * 3600, 0))


if __name__ == "__main__":
    unittest.main()
