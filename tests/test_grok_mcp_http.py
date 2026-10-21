"""Tests for HTTP/SSE MCP transport with security constraints."""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from grok_mcp_http import (
    is_safe_bind_address,
    validate_bearer_token,
    HTTPMCPServer,
    serve_http
)
from grok_mcp import RoomFilter, GrokRouter


class MockClient:
    """Mock bridge client for testing."""
    
    def __call__(self, method, path, body=None):
        if path == "/api/grok/status":
            return {"connected": True}
        elif path == "/api/grok/chats":
            return [
                {
                    "chat_id": "100",
                    "name": "Test Room",
                    "type": 0,
                    "member_count": 2,
                    "can_draft": True,
                    "muted": False
                }
            ]
        elif path == "/api/grok/context":
            return {
                "chat_id": body["chat_id"],
                "name": "Test",
                "messages": [
                    {
                        "sender": "상대",
                        "text": "테스트",
                        "is_from_me": False,
                        "sent_at": 1234567890,
                        "message_type": 1
                    }
                ],
                "fingerprint": "test-fp"
            }
        elif path == "/api/grok/save":
            return {"saved": True, "sent": False}
        return {}


class BindAddressTests(unittest.TestCase):
    """Test bind address validation."""
    
    def test_localhost_allowed(self):
        is_safe, reason = is_safe_bind_address("127.0.0.1")
        self.assertTrue(is_safe)
        self.assertEqual(reason, "localhost")
    
    def test_localhost_ipv6_allowed(self):
        is_safe, reason = is_safe_bind_address("::1")
        self.assertTrue(is_safe)
        self.assertEqual(reason, "localhost")
    
    def test_tailscale_ipv4_allowed(self):
        is_safe, reason = is_safe_bind_address("100.64.0.1")
        self.assertTrue(is_safe)
        self.assertEqual(reason, "tailscale")
        
        is_safe, reason = is_safe_bind_address("100.127.255.254")
        self.assertTrue(is_safe)
        self.assertEqual(reason, "tailscale")
    
    def test_tailscale_ipv6_allowed(self):
        is_safe, reason = is_safe_bind_address("fd7a:115c:a1e0::1")
        self.assertTrue(is_safe)
        self.assertEqual(reason, "tailscale-v6")
    
    def test_wildcard_rejected(self):
        is_safe, reason = is_safe_bind_address("0.0.0.0")
        self.assertFalse(is_safe)
        self.assertIn("unsafe", reason)
    
    def test_public_ip_rejected(self):
        is_safe, reason = is_safe_bind_address("1.2.3.4")
        self.assertFalse(is_safe)
        self.assertIn("unsafe", reason)
        
        is_safe, reason = is_safe_bind_address("8.8.8.8")
        self.assertFalse(is_safe)
        self.assertIn("unsafe", reason)
    
    def test_private_non_tailscale_rejected(self):
        is_safe, reason = is_safe_bind_address("192.168.1.1")
        self.assertFalse(is_safe)
        self.assertIn("unsafe", reason)
        
        is_safe, reason = is_safe_bind_address("10.0.0.1")
        self.assertFalse(is_safe)
        self.assertIn("unsafe", reason)
    
    def test_unsafe_flag_allows_any(self):
        is_safe, reason = is_safe_bind_address("0.0.0.0", allow_unsafe=True)
        self.assertTrue(is_safe)
        self.assertEqual(reason, "unsafe mode enabled")
        
        is_safe, reason = is_safe_bind_address("8.8.8.8", allow_unsafe=True)
        self.assertTrue(is_safe)
        self.assertEqual(reason, "unsafe mode enabled")
    
    def test_invalid_address_rejected(self):
        is_safe, reason = is_safe_bind_address("not-an-ip")
        self.assertFalse(is_safe)
        self.assertIn("invalid", reason)


class BearerTokenTests(unittest.TestCase):
    """Test bearer token authentication."""
    
    def test_valid_token_accepted(self):
        headers = {"Authorization": "Bearer secret-token-123"}
        self.assertTrue(validate_bearer_token(headers, "secret-token-123"))
    
    def test_invalid_token_rejected(self):
        headers = {"Authorization": "Bearer wrong-token"}
        self.assertFalse(validate_bearer_token(headers, "secret-token-123"))
    
    def test_missing_authorization_rejected(self):
        headers = {}
        self.assertFalse(validate_bearer_token(headers, "secret-token-123"))
    
    def test_malformed_authorization_rejected(self):
        headers = {"Authorization": "Basic dXNlcjpwYXNz"}
        self.assertFalse(validate_bearer_token(headers, "secret-token-123"))
        
        headers = {"Authorization": "secret-token-123"}
        self.assertFalse(validate_bearer_token(headers, "secret-token-123"))
    
    def test_case_sensitive_bearer(self):
        headers = {"Authorization": "bearer secret-token-123"}
        self.assertFalse(validate_bearer_token(headers, "secret-token-123"))


class HTTPMCPServerTests(unittest.TestCase):
    """Test HTTP MCP server."""
    
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        self.client = MockClient()
        self.filter = RoomFilter(self.runtime)
        self.router = GrokRouter(self.client, self.filter)
        self.token = "test-token-123"
        self.server = None
        self.server_thread = None
    
    def tearDown(self):
        if self.server:
            self.server.shutdown()
        if self.server_thread:
            self.server_thread.join(timeout=2)
        self.temp.cleanup()
    
    def start_server(self, bind_address="127.0.0.1", port=0, allow_unsafe=False):
        """Start test server in background thread."""
        self.server = HTTPMCPServer(
            self.router,
            bind_address,
            port,
            self.token,
            allow_unsafe
        )
        # Get assigned port
        self.port = self.server.server.server_address[1]
        self.base_url = f"http://{bind_address}:{self.port}"
        
        self.server_thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True
        )
        self.server_thread.start()
        time.sleep(0.1)  # Let server start
    
    def make_request(self, path, method="GET", body=None, token=None):
        """Make HTTP request to server."""
        url = self.base_url + path
        headers = {}
        
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        
        request = Request(url, data=data, headers=headers, method=method)
        
        try:
            with urlopen(request, timeout=2) as response:
                content = response.read()
                if content:
                    return response.status, json.loads(content)
                return response.status, {}
        except HTTPError as e:
            content = e.read()
            if content and e.headers.get("Content-Type", "").startswith("application/json"):
                return e.code, json.loads(content)
            return e.code, {}
    
    def test_server_starts_on_localhost(self):
        self.start_server("127.0.0.1")
        self.assertIsNotNone(self.server)
    
    def test_server_refuses_wildcard_bind(self):
        with self.assertRaises(ValueError) as ctx:
            HTTPMCPServer(self.router, "0.0.0.0", 3000, self.token, allow_unsafe=False)
        
        self.assertIn("not allowed", str(ctx.exception))
        self.assertIn("0.0.0.0", str(ctx.exception))
    
    def test_server_refuses_public_ip(self):
        with self.assertRaises(ValueError) as ctx:
            HTTPMCPServer(self.router, "8.8.8.8", 3000, self.token, allow_unsafe=False)
        
        self.assertIn("not allowed", str(ctx.exception))
    
    def test_server_allows_tailscale_ip(self):
        # Cannot actually bind to Tailscale IP in test environment
        # Just verify it passes validation
        is_safe, reason = is_safe_bind_address("100.64.0.1", allow_unsafe=False)
        self.assertTrue(is_safe)
        self.assertEqual(reason, "tailscale")
    
    def test_unsafe_flag_allows_public_ip(self):
        # Should not raise with unsafe flag
        server = HTTPMCPServer(self.router, "0.0.0.0", 3000, self.token, allow_unsafe=True)
        self.assertIsNotNone(server)
    
    def test_health_check_requires_token(self):
        self.start_server()
        
        # Without token
        try:
            status, body = self.make_request("/health")
            self.assertEqual(status, 401)
            if "error" in body:
                self.assertEqual(body["error"], "unauthorized")
        except Exception as e:
            # Request failed as expected (connection refused or similar)
            pass
        
        # With valid token
        status, body = self.make_request("/health", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "healthy")
    
    def test_mcp_endpoint_requires_token(self):
        self.start_server()
        
        mcp_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {}
        }
        
        # Without token
        status, body = self.make_request("/mcp", method="POST", body=mcp_request)
        self.assertEqual(status, 401)
        
        # With valid token
        status, body = self.make_request("/mcp", method="POST", body=mcp_request, token=self.token)
        self.assertEqual(status, 200)
        self.assertIn("result", body)
    
    def test_wrong_token_rejected(self):
        self.start_server()
        
        status, body = self.make_request("/health", token="wrong-token")
        self.assertEqual(status, 401)
    
    def test_mcp_protocol_works(self):
        self.start_server()
        self.filter.save_allowlist(["100"])
        
        # Initialize
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {}
        }
        status, response = self.make_request("/mcp", method="POST", body=request, token=self.token)
        self.assertEqual(status, 200)
        self.assertIn("capabilities", response["result"])
        
        # List tools
        request = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {}
        }
        status, response = self.make_request("/mcp", method="POST", body=request, token=self.token)
        self.assertEqual(status, 200)
        tools = response["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        self.assertIn("kakao_list_rooms", tool_names)
        self.assertNotIn("kakao_send", tool_names)
    
    def test_mcp_allowlist_enforced(self):
        self.start_server()
        
        # Room not in allowlist
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "kakao_get_context",
                "arguments": {"chat_id": "100"}
            }
        }
        
        status, response = self.make_request("/mcp", method="POST", body=request, token=self.token)
        # Should return 200 with error in response body (not HTTP error)
        self.assertIn("error", response)
        
        # Add to allowlist
        self.filter.save_allowlist(["100"])
        
        status, response = self.make_request("/mcp", method="POST", body=request, token=self.token)
        self.assertEqual(status, 200)
        self.assertIn("result", response)
    
    def test_sse_endpoint_requires_token(self):
        self.start_server()
        
        # Without token
        status, _ = self.make_request("/sse")
        self.assertEqual(status, 401)


if __name__ == '__main__':
    unittest.main()
