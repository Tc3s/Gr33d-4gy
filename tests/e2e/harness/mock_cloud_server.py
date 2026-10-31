"""
E2E Test Harness: mock_cloud_server.py
Configurable local mock HTTP server simulating CloudCode and OAuth endpoints.
"""

import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Any, Optional, Tuple


class MockCloudHandler(BaseHTTPRequestHandler):
    """Handles requests to CloudCode and OAuth endpoints with preconfigured responses."""

    def log_message(self, format, *args):
        # Suppress logging to keep test output clean
        pass

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""
        
        server: MockCloudServer = self.server  # type: ignore

        if "/token" in self.path:
            # OAuth token refresh endpoint
            status, resp_data = server.token_response
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp_data).encode("utf-8"))
            return

        if "v1internal" in self.path:
            # CloudCode endpoint
            status, resp_data = server.cloudcode_response
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if isinstance(resp_data, (dict, list)):
                self.wfile.write(json.dumps(resp_data).encode("utf-8"))
            else:
                self.wfile.write(str(resp_data).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        self.do_POST()


class MockCloudServer(HTTPServer):
    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        super().__init__((host, port), MockCloudHandler)
        self.token_response: Tuple[int, Any] = (
            200,
            {
                "access_token": "mock_refreshed_access_token",
                "expires_in": 3600,
                "token_type": "Bearer",
            }
        )
        self.cloudcode_response: Tuple[int, Any] = (
            200,
            {
                "userQuota": {
                    "standardTier": {"remainingFraction": 1.0},
                    "premiumTier": {"remainingFraction": 1.0}
                }
            }
        )
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self.shutdown()
        self.server_close()
        if self._thread:
            self._thread.join(timeout=2.0)

    @property
    def url(self) -> str:
        host, port = self.server_address
        return f"http://{host}:{port}"
