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


class HealthCheckTests(unittest.TestCase):
    def test_health_reports_required_tool_availability(self):
        available = {"gcc": "/usr/bin/gcc", "lldb": "/usr/bin/lldb",
                     "clang-format": None}
        with patch.object(server.shutil, "which", side_effect=available.get):
            result = server.api_health()
        self.assertFalse(result["ok"])
        self.assertEqual(result["version"], server.APP_VERSION)
        self.assertEqual(result["tools"], {
            "gcc": True, "lldb": True, "clang-format": False,
        })


class ExecutionIsolationTests(unittest.TestCase):
    def test_program_environment_excludes_server_secrets(self):
        with patch.dict(server.os.environ, {
            "PATH": "/usr/bin", "LANG": "C.UTF-8",
            "VERCEL_OIDC_TOKEN": "secret",
        }, clear=True):
            result = server.program_env("/tmp/work")
        self.assertEqual(result, {
            "PATH": "/usr/bin", "LANG": "C.UTF-8", "HOME": "/tmp/work",
        })

    def test_server_environment_excludes_platform_credentials(self):
        with patch.dict(server.os.environ, {
            "PATH": "/usr/bin", "PORT": "80", "VERCEL": "1",
            "VERCEL_OIDC_TOKEN": "secret", "DATABASE_URL": "secret-db",
        }, clear=True):
            result = server.sanitized_server_env()
        self.assertEqual(result, {
            "PATH": "/usr/bin", "PORT": "80", "VERCEL": "1",
        })


class CompilerDiagnosticTests(unittest.TestCase):
    def test_source_diagnostic_is_parsed(self):
        result = server.compiler_diagnostics(
            "/tmp/prog.c:3:7: error: expected expression\n", 1)
        self.assertEqual(result[0]["file"], "prog.c")
        self.assertEqual(result[0]["line"], 3)
        self.assertEqual(result[0]["msg"], "expected expression")

    def test_linker_failure_is_not_discarded(self):
        result = server.compiler_diagnostics(
            "collect2: fatal error: cannot find 'ld'\n", 1)
        self.assertEqual(result, [{
            "file": "prog.c", "line": 1, "col": 1, "sev": "error",
            "msg": "collect2: fatal error: cannot find 'ld'",
        }])


class RequestBodyTests(unittest.TestCase):
    def test_reads_content_length_body(self):
        stream = io.BytesIO(b'{"code":"ok"}')
        result = server.read_http_body(stream, {"Content-Length": "13"})
        self.assertEqual(result, b'{"code":"ok"}')

    def test_reads_chunked_body_from_container_proxy(self):
        stream = io.BytesIO(
            b'7\r\n{"code"\r\n6\r\n:"ok"}\r\n0\r\n\r\n')
        result = server.read_http_body(stream, {"Transfer-Encoding": "chunked"})
        self.assertEqual(result, b'{"code":"ok"}')

    def test_rejects_oversized_body(self):
        with self.assertRaisesRegex(ValueError, "too large"):
            server.read_http_body(io.BytesIO(), {
                "Content-Length": str(server.MAX_REQUEST_BODY + 1),
            })


class StatelessRunTests(unittest.TestCase):
    def test_vercel_run_returns_output_without_a_session(self):
        workdir = tempfile.mkdtemp(prefix="cedit-inline-test-")
        completed = Mock(stdout="hello\n", stderr="", returncode=0)
        with patch.dict(server.os.environ, {"VERCEL": "1"}, clear=False), \
             patch.object(server, "compile_c", return_value=(
                 workdir, os.path.join(workdir, "prog"), [])), \
             patch.object(server, "prepare_execution_dir", return_value=None), \
             patch.object(server.subprocess, "run", return_value=completed):
            result = server.api_start({"code": "int main(void) { return 0; }"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["inline"])
        self.assertEqual(result["stdout"], "hello\n")
        self.assertEqual(result["exit"], 0)
        self.assertNotIn("sid", result)


if __name__ == "__main__":
    unittest.main()
