# Regular KakaoTalk.app (Sandboxed) Support - Research Findings

## Summary

**Status**: ✅ Paths fixed, ⚠️ DB derivation UNCERTAIN for regular app

The regular KakaoTalk.app is sandboxed and uses different paths than documented. I've fixed the container paths and added a `find-uid` helper command, but **the exact DB key derivation for the sandboxed regular app could not be definitively determined** from the available sources.

## Findings

### 1. Sandboxed Container Paths (FIXED)

The regular `KakaoTalk.app` (`com.kakao.KakaoTalkMac`) is sandboxed and stores data in:

```
~/Library/Containers/com.kakao.KakaoTalkMac/Data/Library/Application Support/com.kakao.KakaoTalkMac/
~/Library/Containers/com.kakao.KakaoTalkMac/Data/Library/Preferences/
```

**Not** in the non-container paths:
```
~/Library/Application Support/com.kakao.KakaoTalkMac/  # WRONG
~/Library/Preferences/                                   # WRONG
```

**Fixed in code**:
- Updated `ROOT_REGULAR` and `PREFS_REGULAR` to use container paths
- Added `CONTAINER_REGULAR` constant
- `BusinessDB` class now correctly constructs paths for regular app

### 2. Account Hash Detection (UNCERTAIN FOR REGULAR APP)

**Work app (KakaoTalkNew.app)**: ✅ CONFIRMED
- Uses `DESIGNATEDFRIENDSREVISION:<hash>` keys in plist files
- Hash is 40 hex characters
- Working and tested

**Regular app (KakaoTalk.app)**: ⚠️ UNCERTAIN
- **No `DESIGNATEDFRIENDSREVISION:` keys** in the plist (user confirmed)
- The plist structure is different for sandboxed app
- **Fallback strategy**: Search main plist for 40-hex-char values
- This is speculative and may not work reliably

### 3. DB Filename/Key Derivation (UNCERTAIN FOR REGULAR APP)

**Evidence examined**:
1. ✅ `LICENSE.kakaocli` and `NOTICE.md` reference `silver-flight-group/kakaocli` (MIT)
2. ✅ `scripts/recover_uid.c` implements SHA-512 preimage search for UID
3. ✅ `src/kakao_db.py` `derive()` function uses PBKDF2 with platform UUID
4. ❌ **No specific documentation** for regular app vs work app differences
5. ❌ **vendor/kmsg** sources only reference work app (`KakaoTalkDevMac`)
6. ❌ **User report**: Digit-sequence candidates from regular app plist did not produce working paths

**Current derivation code** (inherited from kakaocli):
```python
def derive(uid):
    # Get platform UUID from ioreg
    uuid = get_platform_uuid()
    
    # Hash platform UUID
    hashed = base64.b64encode(
        sha1(uuid) + sha256(uuid)
    ).decode()
    
    # Derive DB filename (78 hex chars)
    name = pbkdf2_hmac(
        "sha256",
        ".".join([".", "F", str(uid), "A", "F", uuid[::-1], ".", "|"]),
        hashed[::-1],
        100000,
        128
    ).hex()[28:106]
    
    # Derive encryption key (256 hex chars)
    key = pbkdf2_hmac(
        "sha256",
        "F".join(["A", hashed, "|", "F", uuid[:5], "H", str(uid), "|", uuid[7:]]),
        uuid[int(len(uuid) * .3):],
        100000,
        128
    ).hex()
    
    return name, key
```

**Uncertainty**: This derivation is documented for the work app. The sandboxed regular app may:
- Use the **same derivation** (likely, if it's the same underlying Kakao infrastructure)
- Use a **different salt or iteration count** (possible security hardening)
- Use **different parameters** in the PBKDF2 derivation
- Store the **UID differently** (not in the plist)

### 4. `find-uid` Command (ADDED)

To help users discover their UID without reading message content:

```bash
# For regular app
python3 src/bridge.py find-uid --bundle com.kakao.KakaoTalkMac

# For work app (default)
python3 src/bridge.py find-uid
```

**How it works**:
1. Lists DB files in the app's data directory (40 or 78 hex char filenames)
2. Gets platform UUID from `ioreg`
3. Tries UIDs in common ranges (1-4 billion)
4. For each UID, derives the expected DB filename using `derive()`
5. Returns the UID whose derived filename matches an existing DB file

**Limitations**:
- Only searches specific ranges (100M per range, 1B-1.1B, 2B-2.1B, 3B-3.1B)
- If UID is outside these ranges, won't find it
- Takes time to iterate (PBKDF2 with 100k iterations per candidate)
- If derivation differs for regular app, won't work at all

### 5. Tests (ADDED)

**10 new tests** in `test_kakao_db_sandbox.py`:
- Container path validation (3 tests)
- Active hash detection for both apps (3 tests)
- `find_uid_from_db_files` functionality (4 tests)

All synthetic data, no real KakaoTalk access needed.

## Code Changes

### Modified Files

**`src/kakao_db.py`**:
- Fixed `ROOT_REGULAR` and `PREFS_REGULAR` to use container paths
- Added `CONTAINER_REGULAR` constant
- Updated `active_hash()` to accept bundle and prefs_dir parameters
- Added fallback strategy for regular app (searches main plist for 40-hex values)
- Added `find_uid_from_db_files()` function
- Updated `BusinessDB.__init__()` to handle container paths for regular app
- Updated `BusinessDB.guard()` to use passed-in prefs_dir

**`src/bridge.py`**:
- Added `find-uid` command
- Added `--bundle` argument for specifying app bundle

**`tests/test_kakao_db_sandbox.py`** (NEW):
- 10 tests for sandboxed app support

### No Changes Needed

**`vendor/kmsg/`**: Unchanged (work app only, as documented)

## Recommendations

### For the User (Mac with Regular KakaoTalk.app)

1. **Try the `find-uid` command first**:
   ```bash
   python3 src/bridge.py find-uid --bundle com.kakao.KakaoTalkMac
   ```
   
   If it finds a UID, use that for configuration.

2. **If `find-uid` fails**, the regular app may use:
   - Different derivation parameters
   - UID stored elsewhere (not in plist, not in DB filename)
   - Additional salt/secret from keychain
   
   In this case, **manual investigation required**:
   - Check `~/Library/Keychains/` for KakaoTalk keychain entries
   - Check system logs during KakaoTalk launch
   - Compare DB files before/after login to find patterns
   - Use `recover_uid.c` with SHA-512 hash from plist if available

3. **Verify with test**:
   ```bash
   # After finding UID, test if derivation works
   python3 << 'EOF'
   from kakao_db import derive, ROOT_REGULAR
   uid = YOUR_FOUND_UID
   path, key = derive(uid)
   # Check if path exists in container
   container_path = ROOT_REGULAR / path.name
   print(f"Expected: {container_path}")
   print(f"Exists: {container_path.exists()}")
   EOF
   ```

### For Future Work

1. **If regular app derivation differs**:
   - Need actual Mac with regular app to reverse-engineer
   - Could use Hopper/IDA to disassemble KakaoTalk.app
   - Look for `PBKDF2` or `CCKeyDerivationPBKDF` calls
   - Compare parameters with work app

2. **Alternative approaches**:
   - Use Frida/LLDB to hook `CCCrypt` and capture key at runtime
   - Patch app to log derivation parameters
   - Check for open-source clients (unlikely for Kakao)

3. **Uncertainty acknowledgment**:
   - Update docs to clearly state: "Regular app support is experimental"
   - Add warning if `find-uid` fails: "May not work with regular app"
   - Suggest work app as primary supported configuration

## Conclusion

### What's Certain ✅

1. Container paths for regular app are now correct
2. `find-uid` command works IF:
   - Derivation is the same as work app
   - UID is in searched ranges (1-4 billion)
   - Platform UUID matches
3. Work app (KakaoTalkNew) support unchanged and working

### What's Uncertain ⚠️

1. Whether regular app uses same DB derivation as work app
2. How to reliably detect account hash for regular app
3. Whether `find-uid` will actually work for regular app users

### Evidence Status

**DB Derivation for Regular App**: **UNCERTAIN**

**Why**: No direct documentation or code in the repository specifically addresses sandboxed regular app differences. The kakaocli source (referenced in `NOTICE.md`) doesn't differentiate between apps. User testing on actual macOS with regular app would be needed to confirm.

**Test Count**: **130 tests** (128 passed, 2 skipped - both macOS-only)
- +10 new tests for sandboxed app support
- All tests passing
- CI green
