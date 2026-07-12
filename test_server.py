import io
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import server


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def opener_with(tags):
    payload = json.dumps({"results": [{"name": tag} for tag in tags]}).encode()

    def open_response(request, timeout):
        assert request.full_url == server.DOCKER_TAGS_URL
        assert timeout == 4
        return FakeResponse(payload)

    return open_response


class UpdateCheckTests(unittest.TestCase):
    def test_semver_accepts_optional_v_prefix(self):
        self.assertEqual(server.semver("v1.2.3"), (1, 2, 3))
        self.assertEqual(server.semver("2.0.1"), (2, 0, 1))

    def test_semver_rejects_latest_and_prereleases(self):
        self.assertIsNone(server.semver("latest"))
        self.assertIsNone(server.semver("v1.2.3-beta.1"))

    def test_newest_semantic_tag_is_an_update(self):
        result = server.fetch_update_status(
            "v1.2.0", opener_with(["latest", "v1.1.9", "v2.0.0", "edge"]))
        self.assertTrue(result["ok"])
        self.assertTrue(result["update_available"])
        self.assertEqual(result["latest"], "v2.0.0")

    def test_current_or_newer_version_has_no_update(self):
        current = server.fetch_update_status("v2.0.0", opener_with(["v1.9.9", "v2.0.0"]))
        newer = server.fetch_update_status("v2.1.0", opener_with(["v2.0.0"]))
        self.assertFalse(current["update_available"])
        self.assertFalse(newer["update_available"])

    def test_dev_build_skips_network(self):
        def fail_if_called(*args, **kwargs):
            raise AssertionError("Docker Hub should not be called")

        result = server.fetch_update_status("dev", fail_if_called)
        self.assertFalse(result["update_available"])

    def test_api_failure_is_quiet_and_cached(self):
        server.UPDATE_CACHE.update(checked_at=0.0, value=None)
        with patch.object(server, "fetch_update_status", side_effect=OSError("offline")) as fetch:
            first = server.api_update()
            second = server.api_update()
        self.assertFalse(first["ok"])
        self.assertFalse(first["update_available"])
        self.assertEqual(second, first)
        fetch.assert_called_once()


class TraceFailureTests(unittest.TestCase):
    def api_trace_with(self, trace_payload):
        workdir = tempfile.mkdtemp(prefix="cedit-test-")

        def fake_lldb(*args, **kwargs):
            with open(kwargs["env"]["TRACE_OUT"], "w") as output:
                json.dump(trace_payload, output)
            return Mock(stdout="", stderr="")

        with patch.object(server, "compile_c",
                          return_value=(workdir, os.path.join(workdir, "prog"), [])), \
             patch.object(server.subprocess, "run", side_effect=fake_lldb):
            return server.api_trace({"code": "int main(void) { return 0; }"})

    def test_lldb_launch_error_is_not_reported_as_compile_error(self):
        result = self.api_trace_with({
            "steps": [],
            "error": "personality set failed: Operation not permitted",
        })
        self.assertFalse(result["ok"])
        self.assertIn("LLDB could not start", result["error"])

    def test_empty_trace_has_an_actionable_error(self):
        result = self.api_trace_with({"steps": [], "error": ""})
        self.assertFalse(result["ok"])
        self.assertIn("No executable lines", result["error"])


if __name__ == "__main__":
    unittest.main()
