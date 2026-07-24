#!/usr/bin/env python3
"""Test suite for YouTube Member Auth PoC — post-hardening."""
import asyncio
import json
import os
import sys
import stat
from jose import jwt
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
load_dotenv()

import database as db
from config import settings


async def run_tests():
    results = []

    # Test 1: DB init + permissions
    await db.init_db()
    perms = stat.S_IMODE(os.stat(settings.DATABASE_PATH).st_mode)
    results.append(("DB init + chmod 0600", perms == 0o600, "perms=" + oct(perms)))

    # Test 2: Empty member wipe
    await db.replace_active_members([])
    count = await db.count_active_members()
    results.append(("Empty sync to 0 members", count == 0, "count=" + str(count)))

    # Test 3: Add members with flat format
    await db.replace_active_members([
        {"channel_id": "UC_MEMBER_1", "membership_level": "Tier 1"},
        {"channel_id": "UC_MEMBER_2", "membership_level": "Tier 2"},
    ])
    count = await db.count_active_members()
    results.append(("Add 2 members", count == 2, "count=" + str(count)))

    # Test 4: Membership check
    is_m1 = await db.is_active_member("UC_MEMBER_1")
    is_m2 = await db.is_active_member("UC_FAKE")
    results.append(("is_member(UC_MEMBER_1)", is_m1, "result=" + str(is_m1)))
    results.append(("is_member(UC_FAKE) False", not is_m2, "result=" + str(is_m2)))

    # Test 5: Replace wipes old
    await db.replace_active_members([
        {"channel_id": "UC_MEMBER_3", "membership_level": "Tier 1"},
    ])
    count = await db.count_active_members()
    is_m1_still = await db.is_active_member("UC_MEMBER_1")
    results.append(("Replace wipes old members", count == 1 and not is_m1_still, "count=" + str(count)))

    # Test 6: Edge cases
    results.append(("is_member(None) False", await db.is_active_member(None) == False, ""))
    results.append(("is_member(empty) False", await db.is_active_member("") == False, ""))

    # Test 7: User without access_token column
    await db.upsert_user("user-001", "test@example.com", "UC_TEST_CHAN")
    user = await db.get_user_by_id("user-001")
    has_token_col = "oauth_access_token" in user
    results.append(("User has no oauth_access_token", not has_token_col, "cols=" + str(list(user.keys()))))

    # Test 8: JWT with full claims
    token = jwt.encode({
        "sub": "user-001", "email": "test@example.com", "channel_id": "UC_TEST_CHAN",
        "iss": settings.JWT_ISSUER, "aud": settings.JWT_AUDIENCE,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
        "iat": datetime.now(timezone.utc), "jti": "abc123",
    }, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    decoded = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM],
                         issuer=settings.JWT_ISSUER, audience=settings.JWT_AUDIENCE)
    results.append(("JWT encode/decode with iss+aud", decoded["sub"] == "user-001", ""))

    # Test 9: JWT without iss/aud rejected
    bad_token = jwt.encode({
        "sub": "user-001", "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
    }, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    try:
        jwt.decode(bad_token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM],
                   issuer=settings.JWT_ISSUER, audience=settings.JWT_AUDIENCE,
                   options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        results.append(("JWT without iss/aud rejected", False, "should have raised"))
    except Exception:
        results.append(("JWT without iss/aud rejected", True, "correctly raised"))

    # Test 10: Creator token lifecycle
    await db.save_creator_tokens("creator@yt.com", "UC_CREATOR", "access123", "refresh456", 3600)
    tokens = await db.get_creator_tokens()
    results.append(("Creator tokens saved", tokens["refresh_token"] == "refresh456", ""))

    await db.clear_creator_tokens()
    tokens_after = await db.get_creator_tokens()
    results.append(("Creator tokens cleared", tokens_after is None, ""))

    # Test 11: Concurrent replace (race condition fix)
    async def sync_worker(n):
        await db.replace_active_members([
            {"channel_id": "UC_WORKER_" + str(n), "membership_level": "T1"}
        ])
    await asyncio.gather(*[sync_worker(i) for i in range(5)])
    count = await db.count_active_members()
    results.append(("Concurrent replace no crash", count >= 1, "count=" + str(count)))

    # Test 12: JWT expiry short (60min max)
    expiry = timedelta(minutes=settings.JWT_EXPIRY_MINUTES)
    results.append(("JWT expiry <= 60min", expiry <= timedelta(minutes=60), str(settings.JWT_EXPIRY_MINUTES) + "min"))

    # Cleanup
    await db.close_db()

    # Print results
    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    sep = "=" * 60
    print("")
    print(sep)
    print("TEST RESULTS: " + str(passed) + " passed, " + str(failed) + " failed out of " + str(len(results)))
    print(sep)
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        line = "  [" + status + "] " + name
        if detail:
            line += " (" + detail + ")"
        print(line)

    if failed:
        print("\nFAILED TESTS DETECTED")
        sys.exit(1)
    else:
        print("\nALL TESTS PASSED")


asyncio.run(run_tests())
