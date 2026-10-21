"""Event delivery to Grok Bot webhooks with HMAC signing and batching.

Minimal payload (room_id, message_id, timestamp only), configurable batching
window, daily event cap, and local logging without exposing room names.
"""
from __future__ import annotations
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from typing import Callable
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


def now_unix():
    return int(time.time())


def encode(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def sign_payload(secret: str, payload: bytes, timestamp: int) -> str:
    """Sign webhook payload with HMAC-SHA256."""
    if not secret:
        raise ValueError("Webhook secret is required")
    message = f"{timestamp}.{payload.decode()}"
    signature = hmac.new(
        secret.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()
    return signature


class GrokEvents:
    """Deliver batched, minimal new-message events to Grok Bot webhook."""
    
    def __init__(
        self,
        runtime: Path,
        webhook_url: str = "",
        webhook_secret: str = "",
        batch_window_seconds: float = 3.0,
        daily_event_cap: int = 1000
    ):
        self.runtime = Path(runtime)
        self.runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        
        self.webhook_url = webhook_url
        self.webhook_secret = webhook_secret
        self.batch_window = max(0.5, min(float(batch_window_seconds), 30.0))
        self.daily_cap = max(10, min(int(daily_event_cap), 100000))
        
        db_path = self.runtime / "grok-events.sqlite"
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS deliveries(
                id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                sent_at INTEGER NOT NULL,
                batch_key TEXT NOT NULL,
                batch_started REAL NOT NULL,
                state TEXT NOT NULL,
                attempts INTEGER DEFAULT 0,
                next_at REAL NOT NULL,
                last_status INTEGER,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pending ON deliveries(state, next_at);
            CREATE INDEX IF NOT EXISTS idx_batch ON deliveries(batch_key, state);
            CREATE INDEX IF NOT EXISTS idx_created ON deliveries(created_at);
            CREATE TABLE IF NOT EXISTS daily_counts(
                day TEXT PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 0
            );
        ''')
        self.db.commit()
        db_path.chmod(0o600)
    
    def close(self):
        self.db.close()
    
    def _get_daily_count(self) -> int:
        """Get event count for today."""
        today = time.strftime("%Y-%m-%d", time.localtime())
        with self.lock:
            row = self.db.execute(
                "SELECT count FROM daily_counts WHERE day=?",
                (today,)
            ).fetchone()
            return row["count"] if row else 0
    
    def _increment_daily_count(self) -> bool:
        """Increment daily counter. Returns False if cap exceeded."""
        today = time.strftime("%Y-%m-%d", time.localtime())
        with self.lock:
            current = self._get_daily_count()
            if current >= self.daily_cap:
                return False
            self.db.execute(
                "INSERT OR REPLACE INTO daily_counts(day, count) VALUES(?, ?)",
                (today, current + 1)
            )
            self.db.commit()
            return True
    
    def enqueue(self, chat_id: str, message_id: str, sent_at: int) -> bool:
        """
        Enqueue a new message event for batched delivery.
        Returns False if daily cap exceeded.
        """
        if not self.webhook_url or not self.webhook_secret:
            return False
        
        # Check daily cap
        if not self._increment_daily_count():
            return False
        
        event_id = hashlib.sha256(
            encode([chat_id, message_id]).encode()
        ).hexdigest()[:16]
        
        batch_key = f"{chat_id}:{int(time.time() / self.batch_window)}"
        current_time = time.time()
        
        with self.lock:
            # Check if already queued/delivered
            existing = self.db.execute(
                "SELECT 1 FROM deliveries WHERE id=?",
                (event_id,)
            ).fetchone()
            if existing:
                return True
            
            # Find or create batch window
            batch_info = self.db.execute(
                """SELECT MIN(batch_started) as started 
                   FROM deliveries 
                   WHERE batch_key=? AND state='pending' AND attempts=0""",
                (batch_key,)
            ).fetchone()
            
            batch_started = batch_info["started"] if batch_info["started"] else current_time
            
            # Supersede previous unsent events in this batch
            self.db.execute(
                """UPDATE deliveries 
                   SET state='superseded' 
                   WHERE batch_key=? AND state='pending' AND attempts=0""",
                (batch_key,)
            )
            
            # Insert new event
            next_delivery = min(
                current_time + self.batch_window,
                batch_started + 5.0  # Cap max delay at 5s
            )
            
            self.db.execute(
                """INSERT INTO deliveries(
                    id, chat_id, message_id, sent_at, batch_key,
                    batch_started, state, next_at, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (event_id, chat_id, message_id, sent_at, batch_key,
                 batch_started, next_delivery, now_unix())
            )
            self.db.commit()
        
        return True
    
    def _send_webhook(self, payload: dict) -> tuple[int, bytes]:
        """Send POST request to webhook URL."""
        body = encode(payload).encode()
        timestamp = now_unix()
        signature = sign_payload(self.webhook_secret, body, timestamp)
        
        headers = {
            "Content-Type": "application/json",
            "X-Grok-Signature": signature,
            "X-Grok-Timestamp": str(timestamp),
            "User-Agent": "kakao-grok-bridge/1.0"
        }
        
        request = Request(
            self.webhook_url,
            data=body,
            headers=headers,
            method="POST"
        )
        
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, response.read(65536)
        except HTTPError as e:
            return e.code, b""
        except URLError:
            return 0, b""
    
    def deliver_one(self, allowed: Callable[[str], bool]) -> bool:
        """
        Attempt to deliver one pending event.
        Returns True if an event was processed, False if queue is empty.
        """
        with self.lock:
            row = self.db.execute(
                """SELECT * FROM deliveries 
                   WHERE state='pending' AND next_at <= ? 
                   ORDER BY next_at LIMIT 1""",
                (time.time(),)
            ).fetchone()
        
        if not row:
            return False
        
        # Check if delivery is still permitted
        try:
            permitted = allowed(row["chat_id"])
        except Exception:
            permitted = False
        
        status = None
        if permitted and self.webhook_url and self.webhook_secret:
            try:
                # Minimal payload: only IDs and timestamp
                payload = {
                    "chat_id": row["chat_id"],
                    "message_id": row["message_id"],
                    "sent_at": row["sent_at"]
                }
                status, _ = self._send_webhook(payload)
            except Exception:
                pass
        
        # Update delivery state
        attempts = row["attempts"] + 1
        
        if not permitted:
            new_state = "cancelled"
        elif status and 200 <= status < 300:
            new_state = "delivered"
        elif status in (410, 413) or attempts >= 6:
            new_state = "failed"
        elif status and 400 <= status < 500 and status not in (408, 429):
            new_state = "failed"
        else:
            new_state = "pending"
        
        next_attempt = time.time() + min(300, 2 ** attempts)
        
        with self.lock:
            self.db.execute(
                """UPDATE deliveries 
                   SET attempts=?, state=?, next_at=?, last_status=? 
                   WHERE id=?""",
                (attempts, new_state, next_attempt, status, row["id"])
            )
            self.db.commit()
        
        return True
    
    def stats(self) -> dict:
        """Get delivery statistics."""
        with self.lock:
            state_counts = dict(
                self.db.execute(
                    "SELECT state, COUNT(*) as count FROM deliveries GROUP BY state"
                ).fetchall()
            )
            
            today_count = self._get_daily_count()
            
            pending = self.db.execute(
                "SELECT COUNT(*) as count FROM deliveries WHERE state='pending'"
            ).fetchone()["count"]
        
        return {
            "webhook_configured": bool(self.webhook_url and self.webhook_secret),
            "daily_count": today_count,
            "daily_cap": self.daily_cap,
            "pending": pending,
            "state_counts": state_counts,
            "batch_window_seconds": self.batch_window
        }
