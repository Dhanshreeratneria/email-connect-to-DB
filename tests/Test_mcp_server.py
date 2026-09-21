from types import SimpleNamespace
import base64

from mcp.types import ImageContent

from app.mcp.server import attachment_to_content_blocks, group_recipients, serialize


def test_group_recipients_splits_by_type():
    recipients = [
        {"name": "Bob", "email": "bob@example.com", "type": "to"},
        {"name": None, "email": "jane@example.com", "type": "cc"},
        {"name": None, "email": "hidden@example.com", "type": "bcc"},
    ]

    grouped = group_recipients(recipients)

    assert [r["email"] for r in grouped["to"]] == ["bob@example.com"]
    assert [r["email"] for r in grouped["cc"]] == ["jane@example.com"]
    assert [r["email"] for r in grouped["bcc"]] == ["hidden@example.com"]
    assert grouped["unknown"] == []


def test_group_recipients_tolerates_legacy_flat_string_rows():
    # Emails synced before this fix stored `recipients` as a flat list
    # of raw strings with no to/cc/bcc distinction. These must not be
    # dropped or crash serialization — they land in "unknown".
    legacy_recipients = ["bob@example.com", "jane@example.com"]

    grouped = group_recipients(legacy_recipients)

    assert grouped["to"] == []
    assert [r["email"] for r in grouped["unknown"]] == legacy_recipients


def test_serialize_reports_recipient_count_and_passes_through_attachment_type():
    email = SimpleNamespace(
        id=754,
        rfc_message_id="<abc@mail.gmail.com>",
        thread_id="t1",
        sender_name="Shree Hari",
        sender_email="harishre76@gmail.com",
        recipients=[
            {"name": None, "email": "you@example.com", "type": "to"},
            {"name": "Jane", "email": "jane@example.com", "type": "cc"},
        ],
        subject="Test",
        body_text="",
        received_at=SimpleNamespace(isoformat=lambda: "2026-09-17T12:09:00+00:00"),
        labels=["INBOX"],
        category="primary",
        has_attachments=True,
        attachments=[
            {
                "filename": "1000210177.jpg",
                "mime_type": "image/jpeg",
                "type": "image",
                "attachment_id": "att1",
                "size": 42613,
            }
        ],
    )

    result = serialize(email)

    assert result["recipient_count"] == 2
    assert [r["email"] for r in result["recipients"]["to"]] == ["you@example.com"]
    assert [r["email"] for r in result["recipients"]["cc"]] == ["jane@example.com"]
    assert result["attachments"][0]["type"] == "image"
    assert result["category"] == "primary"


def test_get_attachment_content_returns_native_image_content():
    image_bytes = b"\x89PNG\r\n\x1a\nimage-data"
    attachment = SimpleNamespace(
        id=1,
        filename="photo.png",
        mime_type="image/png",
        size=len(image_bytes),
        content=image_bytes,
    )

    blocks = attachment_to_content_blocks(None, attachment)

    assert len(blocks) == 1
    assert isinstance(blocks[0], ImageContent)
    assert blocks[0].type == "image"
    assert getattr(blocks[0], "mime_type", getattr(blocks[0], "mimeType", None)) == "image/png"
    assert base64.b64decode(blocks[0].data) == image_bytes