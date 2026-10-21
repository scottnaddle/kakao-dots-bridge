"""HTTP/SSE MCP transport for Grok Bot with private network binding.

Binds only to localhost (127.0.0.1) or Tailscale (100.64.0.0/10) addresses.
Refuses 0.0.0.0 or public IPs unless explicit unsafe flag is set.
Requires bearer token authentication.
"""
from __future__ import annotations
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import secrets
import socket
from typing import Callable

from grok_mcp import GrokRouter, encode, MAX_BODY


def is_safe_bind_address(address: str, allow_unsafe: bool = False) -> tuple[bool, str]:
    """
    Check if bind address is safe (localhost or Tailscale only).
    
    Returns (is_safe, reason) tuple.
    """
    if allow_unsafe:
        return True, "unsafe mode enabled"
    
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False, "invalid IP address"
    
    # Allow localhost
    if ip.is_loopback:
        return True, "localhost"
    
    # Allow Tailscale range (100.64.0.0/10)
    # https://tailscale.com/kb/1015/100.x-addresses
    if isinstance(ip, ipaddress.IPv4Address):
        tailscale_network = ipaddress.ip_network("100.64.0.0/10")
        if ip in tailscale_network:
            return True, "tailscale"
    
    # Allow IPv6 Tailscale (fd7a:115c:a1e0::/48)
    if isinstance(ip, ipaddress.IPv6Address):
        tailscale_v6_network = ipaddress.ip_network("fd7a:115c:a1e0::/48")
        if ip in tailscale_v6_network:
            return True, "tailscale-v6"
    
    # Reject all other addresses
    return False, f"unsafe address: {ip} (not localhost or tailscale)"


def validate_bearer_token(headers: dict, expected_token: str) -> bool:
    """Validate bearer token from Authorization header."""
    auth = headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    token = auth[7:]  # Remove "Bearer " prefix
    return secrets.compare_digest(token, expected_token)


class HTTPMCPHandler(BaseHTTPRequestHandler):
    """HTTP handler for MCP protocol with SSE support."""
    
    server: 'HTTPMCPServer'
    
    def log_message(self, *args):
        """Suppress default logging."""
        pass
    
    def send_json_response(self, body: dict, status: int = 200):
        """Send JSON response."""
        data = encode(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
    
    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()
    
    def do_GET(self):
        """Handle GET requests (health check, SSE)."""
        # Validate token
        if not validate_bearer_token(dict(self.headers), self.server.token):
            self.send_json_response({"error": "unauthorized"}, 401)
            return
        
        if self.path == "/health":
            self.send_json_response({"status": "healthy", "mcp": "grok-bot"})
        elif self.path == "/sse":
            # SSE endpoint for future event streaming
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            
            # Send initial connected event
            self.wfile.write(b"event: connected\n")
            self.wfile.write(b"data: {\"status\":\"ok\"}\n\n")
            self.wfile.flush()
        else:
            self.send_json_response({"error": "not found"}, 404)
    
    def do_POST(self):
        """Handle POST requests (MCP protocol)."""
        # Validate token
        if not validate_bearer_token(dict(self.headers), self.server.token):
            self.send_json_response({"error": "unauthorized"}, 401)
            return
        
        if self.path != "/mcp":
            self.send_json_response({"error": "not found"}, 404)
            return
        
        # Read and validate request
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length > MAX_BODY:
                self.send_json_response({"error": "request too large"}, 413)
                return
            
            body = self.rfile.read(content_length)
            request = json.loads(body)
            
            # Route through MCP router
            result = self.server.router.handle(request)
            
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": result
            }
            self.send_json_response(response)
        
        except json.JSONDecodeError:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32700,
                    "message": "Parse error"
                }
            }
            self.send_json_response(response, 400)
        
        except Exception:
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id") if "request" in locals() else None,
                "error": {
                    "code": -32603,
                    "message": "Internal error"
                }
            }
            self.send_json_response(response, 500)


class HTTPMCPServer:
    """HTTP MCP server with private network binding."""
    
    def __init__(
        self,
        router: GrokRouter,
        bind_address: str,
        port: int,
        token: str,
        allow_unsafe: bool = False
    ):
        # Validate bind address
        is_safe, reason = is_safe_bind_address(bind_address, allow_unsafe)
        if not is_safe:
            raise ValueError(
                f"Bind address {bind_address} is not allowed: {reason}. "
                f"Only localhost (127.0.0.1) or Tailscale (100.64.0.0/10) "
                f"addresses are permitted unless allow_unsafe=True is set."
            )
        
        self.router = router
        self.bind_address = bind_address
        self.port = port
        self.token = token
        self.allow_unsafe = allow_unsafe
        
        # Create HTTP server
        self.server = ThreadingHTTPServer(
            (bind_address, port),
            HTTPMCPHandler
        )
        self.server.token = token
        self.server.router = router
    
    def serve_forever(self):
        """Start serving requests."""
        print(
            encode({
                "transport": "http-mcp",
                "address": f"http://{self.bind_address}:{self.port}",
                "endpoints": {
                    "health": "/health",
                    "mcp": "/mcp",
                    "sse": "/sse"
                },
                "auth": "bearer-token-required",
                "binding": "safe" if not self.allow_unsafe else "unsafe"
            }),
            flush=True
        )
        self.server.serve_forever()
    
    def shutdown(self):
        """Shutdown server."""
        self.server.shutdown()


def serve_http(
    client: Callable,
    runtime: Path,
    bind_address: str = "127.0.0.1",
    port: int = 3000,
    allow_unsafe: bool = False
):
    """Start HTTP MCP server."""
    from grok_mcp import RoomFilter
    
    # Load or generate token
    token_path = runtime / "grok-mcp-token"
    if not token_path.exists():
        token_path.write_text(secrets.token_urlsafe(40))
        token_path.chmod(0o600)
    token = token_path.read_text().strip()
    
    room_filter = RoomFilter(runtime)
    router = GrokRouter(client, room_filter)
    
    server = HTTPMCPServer(router, bind_address, port, token, allow_unsafe)
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down MCP server...", flush=True)
        server.shutdown()
