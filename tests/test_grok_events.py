"""Tests for Grok Bot event delivery and webhook signing."""
import hashlib
import hmac
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from grok_events import GrokEvents, sign_payload, encode


class MockTransport:
    """Mock webhook transport for testing."""
    
    def __init__(self):
        self.calls = []
        self.responses = []
    
    def __call__(self, url, body, headers):
        self.calls.append({
            "url": url,
            "body": json.loads(body),
            "headers": headers
        })
        if self.responses:
            return self.responses.pop(0)
        return (200, b'{"ok":true}')


class GrokEventsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        self.transport = MockTransport()
        self.events = GrokEvents(
            self.runtime,
            webhook_url="https://routine.x.ai/webhook/test",
            webhook_secret="test-secret-key",
            batch_window_seconds=2.0,
            daily_event_cap=100
        )
        # Store original method
        self._original_send = self.events._send_webhook
        # Override with mock that captures calls but uses real signing
        def mock_send(payload):
            body = encode(payload).encode()
            timestamp = int(time.time())
            signature = sign_payload(self.events.webhook_secret, body, timestamp)
            headers = {
                "Content-Type": "application/json",
                "X-Grok-Signature": signature,
                "X-Grok-Timestamp": str(timestamp),
                "User-Agent": "kakao-grok-bridge/1.0"
            }
            self.transport.calls.append({
                "url": self.events.webhook_url,
                "body": payload,
                "headers": headers
            })
            if self.transport.responses:
                return self.transport.responses.pop(0)
            return (200, b'{"ok":true}')
        self.events._send_webhook = mock_send
    
    def tearDown(self):
        self.events.close()
        self.temp.cleanup()
    
    def test_sign_payload_creates_valid_hmac(self):
        payload = b'{"test":"data"}'
        timestamp = 1234567890
        signature = sign_payload("secret", payload, timestamp)
        
        message = f"{timestamp}.{payload.decode()}"
        expected = hmac.new(
            "secret".encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()
        
        self.assertEqual(signature, expected)
    
    def test_enqueue_creates_pending_delivery(self):
        result = self.events.enqueue("100", "msg1", 1234567890)
        self.assertTrue(result)
        
        stats = self.events.stats()
        self.assertEqual(stats["pending"], 1)
        self.assertEqual(stats["daily_count"], 1)
    
    def test_batching_supersedes_previous_unsent_events(self):
        self.events.enqueue("100", "msg1", 1234567890)
        self.events.enqueue("100", "msg2", 1234567891)
        
        stats = self.events.stats()
        state_counts = stats["state_counts"]
        self.assertEqual(state_counts.get("pending", 0), 1)
        self.assertEqual(state_counts.get("superseded", 0), 1)
    
    def test_daily_cap_prevents_excessive_events(self):
        self.events.daily_cap = 3
        
        for i in range(5):
            result = self.events.enqueue("100", f"msg{i}", 1234567890 + i)
            if i < 3:
                self.assertTrue(result)
            else:
                self.assertFalse(result)
        
        stats = self.events.stats()
        self.assertEqual(stats["daily_count"], 3)
    
    def test_deliver_one_sends_minimal_payload(self):
        self.events.enqueue("100", "msg1", 1234567890)
        time.sleep(2.1)  # Wait for batch window
        
        delivered = self.events.deliver_one(lambda cid: True)
        self.assertTrue(delivered)
        
        self.assertEqual(len(self.transport.calls), 1)
        call = self.transport.calls[0]
        
        # Verify minimal payload
        payload = call["body"]
        self.assertEqual(set(payload.keys()), {"chat_id", "message_id", "sent_at"})
        self.assertEqual(payload["chat_id"], "100")
        self.assertEqual(payload["message_id"], "msg1")
        self.assertEqual(payload["sent_at"], 1234567890)
        
        # Verify headers
        headers = call["headers"]
        self.assertIn("X-Grok-Signature", headers)
        self.assertIn("X-Grok-Timestamp", headers)
        self.assertEqual(headers["Content-Type"], "application/json")
    
    def test_deliver_respects_allowlist(self):
        self.events.enqueue("100", "msg1", 1234567890)
        time.sleep(2.1)
        
        # Deny delivery
        delivered = self.events.deliver_one(lambda cid: False)
        self.assertTrue(delivered)
        
        # Should be cancelled, not delivered
        self.assertEqual(len(self.transport.calls), 0)
        
        with self.events.lock:
            row = self.events.db.execute(
                "SELECT state FROM deliveries WHERE chat_id='100'"
            ).fetchone()
            self.assertEqual(row["state"], "cancelled")
    
    def test_retry_with_exponential_backoff(self):
        self.events.enqueue("100", "msg1", 1234567890)
        time.sleep(2.1)
        
        # Simulate failures
        self.transport.responses = [(500, b"error"), (500, b"error"), (200, b"ok")]
        
        # First attempt
        self.events.deliver_one(lambda cid: True)
        with self.events.lock:
            row = self.events.db.execute("SELECT attempts, state FROM deliveries").fetchone()
            self.assertEqual(row["attempts"], 1)
            self.assertEqual(row["state"], "pending")
        
        # Second attempt (after backoff)
        time.sleep(2)
        self.events.deliver_one(lambda cid: True)
        
        # Third attempt succeeds
        time.sleep(4)
        self.events.deliver_one(lambda cid: True)
        
        with self.events.lock:
            row = self.events.db.execute("SELECT attempts, state FROM deliveries").fetchone()
            self.assertEqual(row["attempts"], 3)
            self.assertEqual(row["state"], "delivered")
    
    def test_permanent_failure_stops_retry(self):
        self.events.enqueue("100", "msg1", 1234567890)
        time.sleep(2.1)
        
        # 410 Gone is permanent failure
        self.transport.responses = [(410, b"gone")]
        
        self.events.deliver_one(lambda cid: True)
        
        with self.events.lock:
            row = self.events.db.execute("SELECT state FROM deliveries").fetchone()
            self.assertEqual(row["state"], "failed")
    
    def test_no_webhook_config_skips_delivery(self):
        events = GrokEvents(self.runtime, webhook_url="", webhook_secret="")
        result = events.enqueue("100", "msg1", 1234567890)
        self.assertFalse(result)
        events.close()
    
    def test_stats_returns_complete_summary(self):
        self.events.enqueue("100", "msg1", 1234567890)
        self.events.enqueue("101", "msg2", 1234567891)
        
        stats = self.events.stats()
        
        self.assertTrue(stats["webhook_configured"])
        self.assertEqual(stats["daily_count"], 2)
        self.assertEqual(stats["daily_cap"], 100)
        self.assertEqual(stats["pending"], 2)
        self.assertEqual(stats["batch_window_seconds"], 2.0)


if __name__ == '__main__':
    unittest.main()
