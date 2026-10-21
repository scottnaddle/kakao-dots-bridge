"""Tests for Grok Bot MCP server and room filtering."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from grok_mcp import RoomFilter, GrokRouter, encode


class MockClient:
    """Mock bridge client for testing."""
    
    def __init__(self):
        self.chats = [
            {
                "chat_id": "100",
                "name": "업무팀",
                "type": 0,
                "member_count": 3,
                "can_draft": True,
                "muted": False
            },
            {
                "chat_id": "101",
                "name": "개인 친구",
                "type": 0,
                "member_count": 2,
                "can_draft": True,
                "muted": False
            },
            {
                "chat_id": "102",
                "name": "나와의 채팅",
                "type": 5,
                "member_count": 1,
                "can_draft": False,
                "muted": False
            },
            {
                "chat_id": "103",
                "name": "광고 채널",
                "type": 9999,
                "member_count": 0,
                "can_draft": False,
                "muted": False
            }
        ]
        self.contexts = {}
    
    def __call__(self, method, path, body=None):
        if path == "/api/grok/status":
            return {"connected": True}
        elif path == "/api/grok/chats":
            return self.chats
        elif path == "/api/grok/context":
            chat_id = body["chat_id"]
            return self.contexts.get(chat_id, {
                "chat_id": chat_id,
                "name": "Test Chat",
                "messages": [
                    {
                        "sender": "상대",
                        "text": "안녕하세요",
                        "is_from_me": False,
                        "sent_at": 1234567890,
                        "message_type": 1
                    }
                ],
                "fingerprint": "test-fingerprint"
            })
        elif path == "/api/grok/save":
            return {"saved": True, "sent": False}
        else:
            raise ValueError(f"Unknown path: {path}")


class RoomFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        self.filter = RoomFilter(self.runtime)
    
    def tearDown(self):
        self.temp.cleanup()
    
    def test_empty_allowlist_denies_all(self):
        self.assertFalse(self.filter.is_allowed("100"))
        self.assertFalse(self.filter.is_allowed("200"))
    
    def test_save_and_load_allowlist(self):
        self.filter.save_allowlist(["100", "101"])
        
        self.assertTrue(self.filter.is_allowed("100"))
        self.assertTrue(self.filter.is_allowed("101"))
        self.assertFalse(self.filter.is_allowed("102"))
    
    def test_allowlist_persists(self):
        self.filter.save_allowlist(["100"])
        
        # Create new filter instance
        filter2 = RoomFilter(self.runtime)
        self.assertTrue(filter2.is_allowed("100"))
        self.assertFalse(filter2.is_allowed("101"))
    
    def test_record_candidate_stores_metadata_only(self):
        self.filter.record_candidate({
            "chat_id": "100",
            "name": "업무팀",
            "member_count": 3,
            "type": 0
        })
        
        candidates = self.filter.list_candidates()
        self.assertEqual(len(candidates), 1)
        
        candidate = candidates[0]
        self.assertEqual(candidate["chat_id"], "100")
        self.assertEqual(candidate["name"], "업무팀")
        self.assertEqual(candidate["member_count"], 3)
        self.assertIn("discovered_at", candidate)
        
        # Should not contain message content
        self.assertNotIn("messages", candidate)
    
    def test_candidate_limit(self):
        for i in range(120):
            self.filter.record_candidate({
                "chat_id": str(i),
                "name": f"Chat {i}",
                "member_count": 2,
                "type": 0
            })
        
        candidates = self.filter.list_candidates()
        self.assertEqual(len(candidates), 100)


class GrokRouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        self.filter = RoomFilter(self.runtime)
        self.client = MockClient()
        self.router = GrokRouter(self.client, self.filter)
    
    def tearDown(self):
        self.temp.cleanup()
    
    def test_tools_list_excludes_send(self):
        response = self.router.handle({
            "method": "tools/list",
            "params": {}
        })
        
        tools = response["tools"]
        tool_names = [t["name"] for t in tools]
        
        self.assertIn("kakao_status", tool_names)
        self.assertIn("kakao_list_rooms", tool_names)
        self.assertIn("kakao_get_context", tool_names)
        self.assertIn("kakao_save_draft", tool_names)
        
        # No send tool
        self.assertNotIn("kakao_send", tool_names)
        self.assertNotIn("kakao_confirm_send", tool_names)
    
    def test_list_rooms_filters_by_type_and_allowlist(self):
        # Allow only one room
        self.filter.save_allowlist(["100"])
        
        response = self.router.handle({
            "method": "tools/call",
            "params": {
                "name": "kakao_list_rooms",
                "arguments": {}
            }
        })
        
        rooms = response["structuredContent"]["items"]
        
        # Should only include room 100
        self.assertEqual(len(rooms), 1)
        self.assertEqual(rooms[0]["chat_id"], "100")
        
        # Self-chat and channels always excluded
        room_ids = [r["chat_id"] for r in rooms]
        self.assertNotIn("102", room_ids)  # Self-chat
        self.assertNotIn("103", room_ids)  # Channel
    
    def test_get_context_enforces_allowlist(self):
        self.filter.save_allowlist(["100"])
        
        # Allowed room
        response = self.router.handle({
            "method": "tools/call",
            "params": {
                "name": "kakao_get_context",
                "arguments": {"chat_id": "100"}
            }
        })
        
        self.assertIn("messages", response["structuredContent"])
        
        # Denied room
        with self.assertRaises(ValueError):
            self.router.handle({
                "method": "tools/call",
                "params": {
                    "name": "kakao_get_context",
                    "arguments": {"chat_id": "101"}
                }
            })
    
    def test_get_context_filters_non_text_messages(self):
        self.filter.save_allowlist(["100"])
        
        self.client.contexts["100"] = {
            "chat_id": "100",
            "name": "Test",
            "messages": [
                {
                    "sender": "상대",
                    "text": "텍스트",
                    "is_from_me": False,
                    "sent_at": 1234567890,
                    "message_type": 1
                },
                {
                    "sender": "상대",
                    "text": "",
                    "is_from_me": False,
                    "sent_at": 1234567891,
                    "message_type": 2  # Attachment
                }
            ],
            "fingerprint": "test"
        }
        
        response = self.router.handle({
            "method": "tools/call",
            "params": {
                "name": "kakao_get_context",
                "arguments": {"chat_id": "100"}
            }
        })
        
        messages = response["structuredContent"]["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["message_type"], 1)
    
    def test_save_draft_enforces_allowlist(self):
        self.filter.save_allowlist(["100"])
        
        # Allowed
        response = self.router.handle({
            "method": "tools/call",
            "params": {
                "name": "kakao_save_draft",
                "arguments": {
                    "chat_id": "100",
                    "text": "답장입니다",
                    "summary": "답장",
                    "fingerprint": "test"
                }
            }
        })
        
        self.assertTrue(response["structuredContent"]["saved"])
        
        # Denied
        with self.assertRaises(ValueError):
            self.router.handle({
                "method": "tools/call",
                "params": {
                    "name": "kakao_save_draft",
                    "arguments": {
                        "chat_id": "101",
                        "text": "답장",
                        "summary": "답장",
                        "fingerprint": "test"
                    }
                }
            })
    
    def test_initialize_includes_security_instructions(self):
        response = self.router.handle({
            "method": "initialize",
            "params": {"protocolVersion": "2026-10-21"}
        })
        
        instructions = response["instructions"]
        self.assertIn("untrusted data", instructions.lower())
        self.assertIn("no send", instructions.lower())


if __name__ == '__main__':
    unittest.main()
