"""Tests for sandboxed KakaoTalk regular app support and UID finding."""
import hashlib
import json
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from kakao_db import (
    active_hash,
    derive,
    find_uid_from_db_files,
    BUNDLE,
    BUNDLE_REGULAR
)


class SandboxedAppPathsTests(unittest.TestCase):
    """Test sandbox container paths for regular KakaoTalk.app."""
    
    def test_regular_app_uses_container_paths(self):
        from kakao_db import ROOT_REGULAR, PREFS_REGULAR, CONTAINER_REGULAR
        
        # Verify container paths
        self.assertIn("Containers", str(ROOT_REGULAR))
        self.assertIn("com.kakao.KakaoTalkMac", str(ROOT_REGULAR))
        self.assertIn("/Data/Library/Application Support/", str(ROOT_REGULAR))
        
        self.assertIn("Containers", str(PREFS_REGULAR))
        self.assertIn("/Data/Library/Preferences", str(PREFS_REGULAR))
    
    def test_work_app_uses_non_container_paths(self):
        from kakao_db import ROOT, PREFS
        
        # Verify non-container paths for work app
        self.assertNotIn("Containers", str(ROOT))
        self.assertIn("Library/Application Support", str(ROOT))
        
        self.assertNotIn("Containers", str(PREFS))
        self.assertIn("Library/Preferences", str(PREFS))


class ActiveHashTests(unittest.TestCase):
    """Test active account hash detection."""
    
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.prefs_dir = Path(self.temp.name)
    
    def tearDown(self):
        self.temp.cleanup()
    
    def test_work_app_uses_designated_friends_revision(self):
        """Work app stores account hash in DESIGNATEDFRIENDSREVISION: keys."""
        test_hash = "a" * 40
        
        plist_path = self.prefs_dir / f"{BUNDLE}.12345.plist"
        plist_data = {
            f"DESIGNATEDFRIENDSREVISION:{test_hash}": True,
            "other_key": "value"
        }
        plist_path.write_bytes(plistlib.dumps(plist_data))
        
        result = active_hash(BUNDLE, self.prefs_dir)
        self.assertEqual(result, test_hash)
    
    def test_regular_app_fallback_to_main_plist(self):
        """Regular app may store account info in main plist file."""
        test_hash = "b" * 40
        
        plist_path = self.prefs_dir / f"{BUNDLE_REGULAR}.plist"
        plist_data = {
            "accountHash": test_hash,
            "other_key": 12345
        }
        plist_path.write_bytes(plistlib.dumps(plist_data))
        
        result = active_hash(BUNDLE_REGULAR, self.prefs_dir)
        self.assertEqual(result, test_hash)
    
    def test_multiple_accounts_rejected(self):
        """Reject when multiple account hashes found."""
        plist1 = self.prefs_dir / f"{BUNDLE}.12345.plist"
        plist1.write_bytes(plistlib.dumps({
            f"DESIGNATEDFRIENDSREVISION:{'a' * 40}": True
        }))
        
        plist2 = self.prefs_dir / f"{BUNDLE}.67890.plist"
        plist2.write_bytes(plistlib.dumps({
            f"DESIGNATEDFRIENDSREVISION:{'b' * 40}": True
        }))
        
        with self.assertRaises(Exception):
            active_hash(BUNDLE, self.prefs_dir)


class FindUIDTests(unittest.TestCase):
    """Test UID discovery from DB files."""
    
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root_dir = Path(self.temp.name)
    
    def tearDown(self):
        self.temp.cleanup()
    
    def test_find_uid_with_matching_db_file(self):
        """Test finding UID when derived path matches existing DB file."""
        test_uid = 1234567890
        
        # Mock ioreg to return fixed UUID
        mock_uuid = "12345678-1234-1234-1234-123456789ABC"
        
        with patch('subprocess.check_output') as mock_ioreg:
            mock_ioreg.return_value = f'"IOPlatformUUID" = "{mock_uuid}"'
            
            # Derive the expected DB filename for this UID
            import base64
            hashed = base64.b64encode(
                hashlib.sha1(mock_uuid.encode()).digest() + 
                hashlib.sha256(mock_uuid.encode()).digest()
            ).decode()
            
            name = hashlib.pbkdf2_hmac(
                "sha256",
                ".".join([".", "F", str(test_uid), "A", "F", mock_uuid[::-1], ".", "|"]).encode(),
                hashed[::-1].encode(),
                100000,
                128
            ).hex()[28:106]
            
            # Create a DB file with this name
            db_file = self.root_dir / name
            db_file.write_bytes(b"fake db content")
            
            # Now find_uid should discover it (use small test range)
            result = find_uid_from_db_files(self.root_dir, test_range=(test_uid, test_uid + 1))
            self.assertEqual(result, test_uid)
    
    def test_find_uid_returns_none_when_no_match(self):
        """Test that find_uid returns None when no matching DB found."""
        # Create a DB file with random name
        (self.root_dir / ("f" * 78)).write_bytes(b"fake")
        
        with patch('subprocess.check_output') as mock_ioreg:
            mock_ioreg.return_value = '"IOPlatformUUID" = "test-uuid"'
            
            # Use a small test range that won't match the random name
            result = find_uid_from_db_files(self.root_dir, test_range=(1000, 1010))
            self.assertIsNone(result)
    
    def test_find_uid_returns_none_for_empty_dir(self):
        """Test that find_uid returns None for empty directory."""
        with patch('subprocess.check_output') as mock_ioreg:
            mock_ioreg.return_value = '"IOPlatformUUID" = "test-uuid"'
            
            result = find_uid_from_db_files(self.root_dir)
            self.assertIsNone(result)
    
    def test_find_uid_returns_none_for_nonexistent_dir(self):
        """Test that find_uid returns None for non-existent directory."""
        nonexistent = Path("/nonexistent/path/12345")
        result = find_uid_from_db_files(nonexistent)
        self.assertIsNone(result)


class DerivePathTests(unittest.TestCase):
    """Test DB path derivation."""
    
    @unittest.skipUnless(sys.platform == 'darwin', "macOS-only test")
    def test_derive_produces_consistent_path(self):
        """Test that derive() produces consistent paths for same UID."""
        test_uid = 1234567890
        
        path1, key1 = derive(test_uid)
        path2, key2 = derive(test_uid)
        
        self.assertEqual(path1, path2)
        self.assertEqual(key1, key2)
        
        # Path should be 78 hex chars
        self.assertEqual(len(path1.name), 78)
        self.assertTrue(all(c in '0123456789abcdef' for c in path1.name.lower()))
        
        # Key should be 256 hex chars (128 bytes)
        self.assertEqual(len(key1), 256)
        self.assertTrue(all(c in '0123456789abcdef' for c in key1.lower()))


if __name__ == '__main__':
    unittest.main()
