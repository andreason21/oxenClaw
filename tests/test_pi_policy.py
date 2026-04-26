"""Phase 13: session policy (chat type, send policy, overrides, provenance)."""

from __future__ import annotations

from oxenclaw.pi import (
    AgentSession,
    CreateAgentSessionOptions,
    InMemorySessionManager,
    ThinkingLevel,
)
from oxenclaw.pi.policy import (
    InputProvenance,
    SendMode,
    SendPolicy,
    SessionChatType,
    SessionOverrides,
    SessionPolicy,
    derive_session_key,
    deserialize_policy,
    get_policy,
    normalize_session_key,
    serialize_policy,
    set_policy,
)

# ─── normalize / derive key ─────────────────────────────────────────


def test_normalize_replaces_unsafe_chars() -> None:
    assert normalize_session_key("Hello World!") == "hello_world_"
    assert normalize_session_key("a/b@c:d-e.f") == "a_b_c:d-e.f"
    assert normalize_session_key("") == "anon"


def test_normalize_truncates_with_hash_suffix() -> None:
    raw = "x" * 500
    out = normalize_session_key(raw, max_len=64)
    assert len(out) == 64
    assert "." in out  # has the digest separator


def test_derive_session_key_includes_thread() -> None:
    a = derive_session_key(channel="telegram", account_id="main", chat_id="42")
    b = derive_session_key(channel="telegram", account_id="main", chat_id="42", thread_id="9")
    assert a != b
    assert "t:9" in b


# ─── SendPolicy ─────────────────────────────────────────────────────


def test_dm_only_blocks_groups() -> None:
    p = SendPolicy(mode=SendMode.DM_ONLY)
    assert p.should_reply(chat_type=SessionChatType.DM, sender_id="u", text="hi") is True
    assert p.should_reply(chat_type=SessionChatType.GROUP, sender_id="u", text="hi") is False


def test_allowlist_only_replies_to_listed_senders() -> None:
    p = SendPolicy(mode=SendMode.ALLOWLIST, allow=("alice",))
    assert p.should_reply(chat_type=SessionChatType.GROUP, sender_id="alice", text="hi") is True
    assert p.should_reply(chat_type=SessionChatType.GROUP, sender_id="bob", text="hi") is False


def test_pairing_blocks_until_completed() -> None:
    p = SendPolicy(mode=SendMode.PAIRING, pairing_completed=False)
    assert p.should_reply(chat_type=SessionChatType.DM, sender_id="u", text="hi") is False
    p2 = SendPolicy(mode=SendMode.PAIRING, pairing_completed=True)
    assert p2.should_reply(chat_type=SessionChatType.DM, sender_id="u", text="hi") is True


def test_addressed_only_requires_mention_or_reply() -> None:
    p = SendPolicy(mode=SendMode.ADDRESSED_ONLY, addressed_handles=("@oxenclaw",))
    assert (
        p.should_reply(chat_type=SessionChatType.GROUP, sender_id="u", text="@oxenclaw hi") is True
    )
    assert (
        p.should_reply(chat_type=SessionChatType.GROUP, sender_id="u", text="random chatter")
        is False
    )
    # Reply-to-bot also triggers.
    assert (
        p.should_reply(
            chat_type=SessionChatType.GROUP,
            sender_id="u",
            text="ok",
            is_reply_to_bot=True,
        )
        is True
    )


def test_open_mode_blocked_only_by_deny() -> None:
    p = SendPolicy(mode=SendMode.OPEN, deny=("spammer",))
    assert p.should_reply(chat_type=SessionChatType.GROUP, sender_id="anyone", text="x") is True
    assert p.should_reply(chat_type=SessionChatType.GROUP, sender_id="spammer", text="x") is False


# ─── overrides + provenance round-trip ──────────────────────────────


def test_policy_round_trip_through_metadata() -> None:
    pol = SessionPolicy(
        chat_type=SessionChatType.THREAD,
        send=SendPolicy(
            mode=SendMode.ALLOWLIST,
            allow=("alice", "bob"),
            deny=("carol",),
            pairing_completed=False,
            addressed_handles=("@bot",),
        ),
        overrides=SessionOverrides(
            model_id="claude-haiku-4-5",
            thinking=ThinkingLevel.HIGH,
            temperature=0.2,
            max_tokens=2048,
            extra_params={"top_p": 0.9},
        ),
        provenance=InputProvenance(
            channel="telegram",
            account_id="main",
            chat_id="42",
            sender_id="u",
            thread_id="9",
            message_id="m1",
            received_at=12345.6,
        ),
    )
    blob = serialize_policy(pol)
    back = deserialize_policy(blob)
    assert back.chat_type is SessionChatType.THREAD
    assert back.send.mode is SendMode.ALLOWLIST
    assert back.send.allow == ("alice", "bob")
    assert back.overrides.model_id == "claude-haiku-4-5"
    assert back.overrides.extra_params == {"top_p": 0.9}
    assert back.provenance is not None
    assert back.provenance.thread_id == "9"


def test_get_set_policy_on_session_metadata() -> None:
    s = AgentSession(agent_id="x")
    assert get_policy(s).chat_type is SessionChatType.DM  # default
    set_policy(
        s,
        SessionPolicy(
            chat_type=SessionChatType.GROUP,
            send=SendPolicy(mode=SendMode.OPEN),
        ),
    )
    assert get_policy(s).chat_type is SessionChatType.GROUP
    assert get_policy(s).send.mode is SendMode.OPEN
    # Original metadata keys untouched.
    s.metadata["other"] = "kept"
    set_policy(s, SessionPolicy())
    assert s.metadata.get("other") == "kept"


def test_deserialize_policy_handles_missing_or_garbled_input() -> None:
    assert deserialize_policy(None).chat_type is SessionChatType.DM
    assert deserialize_policy({}).send.mode is SendMode.DM_ONLY
    # Bad enum value falls back to default.
    bad = deserialize_policy({"chat_type": "unknown_type"})
    assert bad.chat_type is SessionChatType.DM


async def test_policy_persists_through_session_manager() -> None:
    sm = InMemorySessionManager()
    s = await sm.create(CreateAgentSessionOptions(agent_id="x"))
    set_policy(
        s,
        SessionPolicy(
            chat_type=SessionChatType.GROUP,
            send=SendPolicy(mode=SendMode.ALLOWLIST, allow=("u1",)),
        ),
    )
    await sm.save(s)
    fetched = await sm.get(s.id)
    assert fetched is not None
    pol = get_policy(fetched)
    assert pol.chat_type is SessionChatType.GROUP
    assert pol.send.allow == ("u1",)
