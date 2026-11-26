"""OneBot 11 协议解析：消息段、事件、CQ 码、拆条。"""

from __future__ import annotations

from dsdaughter.channels.onebot import (
    MessageEvent,
    at_segment,
    parse_segments,
    split_for_chat,
    split_text,
    text_segment,
)


def _payload(**overrides):
    base = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 1001,
        "self_id": 999,
        "user_id": 123,
        "message": [{"type": "text", "data": {"text": "你好呀"}}],
        "sender": {"nickname": "老爸"},
    }
    base.update(overrides)
    return base


def test_parse_array_segments():
    segments = parse_segments(
        [
            {"type": "text", "data": {"text": "看这个"}},
            {"type": "image", "data": {"file": "a.jpg"}},
            {"type": "at", "data": {"qq": "999"}},
        ]
    )
    assert [s.type for s in segments] == ["text", "image", "at"]
    assert segments[1].data["file"] == "a.jpg"


def test_parse_cq_string():
    segments = parse_segments("你好[CQ:at,qq=999]看[CQ:image,file=x.jpg]")
    assert [s.type for s in segments] == ["text", "at", "text", "image"]
    assert segments[0].data["text"] == "你好"
    assert segments[1].data["qq"] == "999"


def test_parse_cq_escapes():
    segments = parse_segments("a&#91;b&#93;&#44;c")
    assert segments[0].data["text"] == "a[b],c"


def test_message_event_private():
    event = MessageEvent.from_payload(_payload())
    assert event is not None
    assert event.is_group is False
    assert event.user_id == 123
    assert event.nickname == "老爸"
    assert event.plain_text() == "你好呀"


def test_message_event_group_and_mention():
    payload = _payload(
        message_type="group",
        group_id=456,
        message=[
            {"type": "at", "data": {"qq": "999"}},
            {"type": "text", "data": {"text": " 在吗"}},
        ],
    )
    event = MessageEvent.from_payload(payload)
    assert event is not None
    assert event.is_group is True
    assert event.group_id == 456
    assert event.mentioned(999) is True
    assert event.mentioned(111) is False
    assert event.plain_text() == "在吗"


def test_at_all_counts_as_mention():
    event = MessageEvent.from_payload(
        _payload(message=[{"type": "at", "data": {"qq": "all"}}], message_type="group", group_id=1)
    )
    assert event is not None
    assert event.mentioned(12345) is True


def test_non_message_payload_returns_none():
    assert MessageEvent.from_payload({"post_type": "meta_event"}) is None
    assert MessageEvent.from_payload({"post_type": "message", "message_id": None}) is None


def test_plain_text_placeholders():
    event = MessageEvent.from_payload(
        _payload(
            message=[
                {"type": "text", "data": {"text": "听"}},
                {"type": "record", "data": {"file": "a.amr"}},
                {"type": "text", "data": {"text": "看"}},
                {"type": "image", "data": {"file": "b.jpg"}},
            ]
        )
    )
    assert event is not None
    assert event.plain_text() == "听[语音]看[图片]"


def test_segment_helpers():
    assert text_segment("hi").to_dict() == {"type": "text", "data": {"text": "hi"}}
    assert at_segment(123).to_dict() == {"type": "at", "data": {"qq": "123"}}


def test_split_text_short():
    assert split_text("你好", 100) == ["你好"]
    assert split_text("   ", 100) == []


def test_split_text_respects_limit_and_punctuation():
    text = "第一句话。" * 40
    chunks = split_text(text, 60)
    assert len(chunks) > 1
    assert all(len(chunk) <= 60 for chunk in chunks)
    assert "".join(chunks) == text
    assert all(chunk.endswith("。") for chunk in chunks)


def test_split_text_hard_cut_when_no_punctuation():
    chunks = split_text("啊" * 250, 100)
    assert len(chunks) == 3
    assert all(len(chunk) <= 100 for chunk in chunks)


# ---------------------------------------------------------------- 连发拆分
def test_split_for_chat_short_stays_single():
    assert split_for_chat("哦。", min_chars=60) == ["哦。"]
    assert split_for_chat("", min_chars=60) == []


def test_split_for_chat_splits_long_reply_by_sentence():
    text = (
        "今天风好大啊。我出门的时候差点被吹跑了。"
        "然后我就干脆回家躺着，听了会儿歌。你说我是不是很聪明？"
    )
    chunks = split_for_chat(text, min_chars=10, max_parts=3)
    assert 2 <= len(chunks) <= 3
    assert "".join(chunks) == text


def test_split_for_chat_without_punctuation():
    chunks = split_for_chat("啊" * 100, min_chars=10, max_parts=3)
    assert chunks == ["啊" * 100]
