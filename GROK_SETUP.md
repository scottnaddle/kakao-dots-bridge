# Grok Bot Setup Guide

This guide explains how to configure the kakao-dots-bridge fork to work with Grok Bot.

## Overview

This fork adds Grok Bot support with the following features:

1. **Webhook event delivery**: POST minimal payloads (room ID, message ID, timestamp) to a Grok Bot routine webhook
2. **Private network MCP access**: MCP server binds to localhost/Tailscale only, with token authentication
3. **Draft-only**: No send tool - read and draft only
4. **Room filtering**: Allowlist-based access control for mixed work/personal accounts
5. **High volume handling**: Configurable batching window and daily event cap
6. **Compatibility**: Works with both KakaoTalkNew.app (business) and regular KakaoTalk.app

## Prerequisites

- macOS (Apple Silicon recommended)
- Python 3.11+
- Homebrew
- SQLCipher: `brew install sqlcipher`
- KakaoTalk logged in with your work account
- Tailscale (optional but recommended for private network access)

## Installation

1. Clone this fork:
```bash
git clone https://github.com/scottnaddle/kakao-dots-bridge.git
cd kakao-dots-bridge
```

2. Install SQLCipher:
```bash
brew install sqlcipher
```

3. Configure your KakaoTalk account (you need your numeric user ID):
```bash
python3 scripts/configure.py --user-id YOUR_USER_ID --profile 'Work Account'
```

## Grok Bot Configuration

### 1. Create Grok Bot Routine Webhook

In your X account where Grok Bot is available:

1. Create a new Grok Bot routine
2. Configure it to accept webhook events
3. Copy the webhook URL (should be `https://routine.x.ai/webhook/...`)
4. Note: The webhook signing secret is stored locally and never committed

### 2. Configure Room Allowlist

The allowlist prevents Grok Bot from accessing personal chats mixed with work chats:

```bash
# Start the bridge
python3 src/bridge.py serve

# In another terminal, list available rooms (shows metadata only, no messages)
curl -H "Authorization: Bearer $(cat runtime/api-token)" \
  http://127.0.0.1:18765/api/grok/chats | python3 -m json.tool

# Update allowlist with allowed room IDs
curl -X POST -H "Authorization: Bearer $(cat runtime/api-token)" \
  -H "Content-Type: application/json" \
  -H "X-Kakao-Bridge: 1" \
  -H "Origin: http://127.0.0.1:18765" \
  -d '{"room_ids": ["ROOM_ID_1", "ROOM_ID_2"]}' \
  http://127.0.0.1:18765/api/grok/allowlist
```

**Important**: The allowlist is enforced at both the watcher level (event delivery) and MCP tool level (read access).

### 3. Enable Grok Bot

Edit your `config.json`:

```json
{
  "grok_enabled": true,
  "grok_webhook_url": "https://routine.x.ai/webhook/YOUR_WEBHOOK_ID",
  "grok_batch_window_seconds": 3.0,
  "grok_daily_event_cap": 1000,
  "mcp_bind_address": "127.0.0.1",
  "auto_draft": true
}
```

Configuration options:
- `grok_batch_window_seconds`: Time window to batch messages from the same room (default: 3.0s)
- `grok_daily_event_cap`: Maximum events per day to prevent excessive usage (default: 1000)
- `mcp_bind_address`: Network interface to bind MCP server (default: 127.0.0.1)

### 4. Set Up Tailscale (Required for Remote Grok Bot)

If Grok Bot runs on a remote Linux box (not the same Mac), you need Tailscale:

**On Mac (MCP Server)**:

1. Install Tailscale: `brew install tailscale`
2. Start daemon: `sudo tailscaled`
3. Authenticate: `tailscale up`
4. Get your IP: `tailscale ip -4`
5. Start HTTP MCP: `python3 src/bridge.py grok-mcp-http --bind $(tailscale ip -4) --port 3000`

**On Linux (Grok Bot)**:

1. Install: `curl -fsSL https://tailscale.com/install.sh | sh`
2. Authenticate: `tailscale up`
3. Test connection: `curl -H "Authorization: Bearer $(cat runtime/grok-mcp-token)" http://MAC_TAILSCALE_IP:3000/health`

If Grok Bot runs on the same Mac, use stdio transport instead (no Tailscale needed).

### 5. Configure Grok Bot MCP Connection

The MCP server supports two transports:

#### Option A: stdio (Local Mac Only)

For Grok Bot running on the same Mac:

```bash
python3 src/bridge.py grok-mcp
```

Configure this as a stdio MCP server in your Grok Bot/MCP client.

#### Option B: HTTP/SSE (Remote Grok Bot)

For Grok Bot running on a remote Linux box, use HTTP transport over Tailscale.

**Security**: The HTTP server refuses to bind to `0.0.0.0` or public IPs without an explicit `--allow-unsafe` flag (NOT RECOMMENDED).

**Allowed bind addresses**:
- `127.0.0.1` (localhost only, default)
- Tailscale IPs: `100.64.0.0/10` (IPv4) or `fd7a:115c:a1e0::/48` (IPv6)

**Setup steps**:

1. Install and configure Tailscale on your Mac:
   ```bash
   brew install tailscale
   sudo tailscaled
   tailscale up
   ```

2. Get your Mac's Tailscale IP:
   ```bash
   tailscale ip -4
   # Example output: 100.64.1.23
   ```

3. Start HTTP MCP server bound to Tailscale interface:
   ```bash
   python3 src/bridge.py grok-mcp-http --bind 100.64.1.23 --port 3000
   ```

   This will output:
   ```json
   {
     "transport": "http-mcp",
     "address": "http://100.64.1.23:3000",
     "endpoints": {
       "health": "/health",
       "mcp": "/mcp",
       "sse": "/sse"
     },
     "auth": "bearer-token-required",
     "binding": "safe"
   }
   ```

4. Copy the MCP token from your Mac:
   ```bash
   cat runtime/grok-mcp-token
   ```

5. On your remote Linux box where Grok Bot runs, configure the MCP connection:
   - **Transport**: HTTP
   - **URL**: `http://100.64.1.23:3000/mcp` (use your Mac's Tailscale IP)
   - **Authentication**: Bearer token
   - **Token**: Paste token from step 4

6. Test the connection from Linux box:
   ```bash
   # Install Tailscale on Linux box
   curl -fsSL https://tailscale.com/install.sh | sh
   tailscale up
   
   # Test health endpoint
   curl -H "Authorization: Bearer YOUR_TOKEN_HERE" \
     http://100.64.1.23:3000/health
   
   # Should return: {"status":"healthy","mcp":"grok-bot"}
   ```

**Token Security**: The token is auto-generated and stored in `runtime/grok-mcp-token` with 0600 permissions. Never commit it to git. Rotate by deleting the file and restarting the server.

**Firewall**: No firewall configuration needed - Tailscale creates an encrypted WireGuard tunnel. Only devices on your Tailnet can reach the MCP server.

**Network Binding Safety**:
- ✅ `127.0.0.1` - Localhost only (default)
- ✅ `100.64.x.x` - Tailscale (safe, encrypted)
- ❌ `0.0.0.0` - All interfaces (REFUSED by default)
- ❌ `192.168.x.x` - Private LAN (REFUSED by default)
- ❌ Public IPs - (REFUSED by default)

To override safety checks (NOT RECOMMENDED):
```bash
python3 src/bridge.py grok-mcp-http --bind 0.0.0.0 --port 3000 --allow-unsafe
# WARNING: Exposes MCP to entire network. Use only for testing.
```

**SSE Support**: The `/sse` endpoint is available for future event streaming but not currently used by the MCP protocol.

## Testing

Run the test suite:

```bash
python3 -m unittest discover -s tests -v
```

This runs synthetic tests only - no real KakaoTalk data is used.

## Event Delivery

When enabled, the bridge will:

1. Watch for new text messages in allowed rooms
2. Batch events within the configured window (3s by default)
3. POST minimal payload to your Grok Bot webhook:
   ```json
   {
     "chat_id": "12345",
     "message_id": "67890",
     "sent_at": 1234567890
   }
   ```
4. Sign the payload with HMAC-SHA256
5. Include headers:
   - `X-Grok-Signature`: HMAC signature
   - `X-Grok-Timestamp`: Unix timestamp
   - `X-Grok-Request-Id`: Unique request ID

The payload contains **NO room names, member names, or message content**. Grok Bot must use the MCP tools to fetch context.

## MCP Tools

Available MCP tools for Grok Bot (read and draft only):

1. `kakao_status` - Check connection status
2. `kakao_list_rooms` - List allowed rooms (no message content)
3. `kakao_get_context` - Read up to 20 text messages from an allowed room
4. `kakao_save_draft` - Save a draft reply (does not send)

**No send tool is provided**. This is intentional for safety.

## Filtering Rules

The following are **always excluded**, even if allowlisted:

- Self-chat (나와의 채팅)
- Channels and advertisements
- Muted rooms
- Attachments (only text messages are processed)

## Logs and Privacy

- Event logs never include room names or message content
- Only room IDs are logged for audit purposes
- Webhook secret is stored in `runtime/grok-webhook-secret` with 0600 permissions
- Daily event counter resets at midnight local time

## Troubleshooting

### Events not being delivered

1. Check Grok Bot is enabled: `curl -H "Authorization: Bearer $(cat runtime/api-token)" http://127.0.0.1:18765/api/grok/status`
2. Verify room is in allowlist
3. Check daily cap hasn't been reached
4. Review `runtime/grok-events.sqlite` for delivery state

### MCP connection fails

1. Ensure binding to correct interface (127.0.0.1 or Tailscale IP)
2. Check token authentication
3. Verify room allowlist is configured

### Room not accessible

1. Ensure room ID is in allowlist
2. Check room is not self-chat or channel
3. Verify room has `can_draft: true` in `/api/grok/chats`

## Security Considerations

1. **Webhook secret**: Never commit to git, store in `runtime/` only
2. **Network binding**: Default localhost only; use Tailscale for remote access
3. **Allowlist**: Review regularly, remove personal chats
4. **Daily cap**: Prevents runaway event delivery
5. **No send capability**: Draft only, manual send required

## Upstream Compatibility

This fork maintains compatibility with the original dongwook0101/kakao-dots-bridge:

- Additive changes only
- Original Dots functionality preserved
- Can be upstreamed if desired
- Configuration is backward compatible (new fields optional)

## Support

For issues specific to Grok Bot support, please file issues on the fork repository.
For original functionality, refer to the upstream repository.
