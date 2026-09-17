from app.services.email_parser import classify_attachment_type, parse_message


def test_parse_plain_email():
    raw = {
        "id": "m1",
        "threadId": "t1",
        "internalDate": "0",
        "labelIds": ["INBOX"],
        "payload": {
            "headers": [
                {"name": "From", "value": "Alice <alice@example.com>"},
                {"name": "To", "value": "bob@example.com"},
                {"name": "Subject", "value": "Hello"},
            ],
            "mimeType": "text/plain",
            "body": {"data": "SGVsbG8="},
        },
    }
    value = parse_message(raw)

    assert value["sender_email"] == "alice@example.com"
    assert value["body_text"] == "Hello"
    assert value["recipients"] == [
        {"name": None, "email": "bob@example.com", "type": "to"}
    ]
    assert value["to_addresses"] == ["bob@example.com"]
    assert value["cc_addresses"] == []


def test_parse_email_tags_to_cc_bcc_separately():
    raw = {
        "id": "m2",
        "threadId": "t2",
        "internalDate": "0",
        "labelIds": ["INBOX"],
        "payload": {
            "headers": [
                {"name": "From", "value": "Alice <alice@example.com>"},
                {"name": "To", "value": "Bob Smith <bob@example.com>"},
                {"name": "Cc", "value": '"Doe, Jane" <jane@example.com>'},
                {"name": "Bcc", "value": "hidden@example.com"},
                {"name": "Subject", "value": "Hello"},
            ],
            "mimeType": "text/plain",
            "body": {"data": ""},
        },
    }
    value = parse_message(raw)

    assert value["recipients"] == [
        {"name": "Bob Smith", "email": "bob@example.com", "type": "to"},
        # getaddresses correctly keeps "Doe, Jane" as one display name
        # instead of splitting on the comma inside it.
        {"name": "Doe, Jane", "email": "jane@example.com", "type": "cc"},
        {"name": None, "email": "hidden@example.com", "type": "bcc"},
    ]
    assert value["to_addresses"] == ["bob@example.com"]
    assert value["cc_addresses"] == ["jane@example.com"]


def test_parse_email_with_attachment_includes_friendly_type():
    raw = {
        "id": "m3",
        "threadId": "t3",
        "internalDate": "0",
        "labelIds": ["INBOX"],
        "payload": {
            "headers": [
                {"name": "From", "value": "alice@example.com"},
                {"name": "To", "value": "bob@example.com"},
                {"name": "Subject", "value": "Photo"},
            ],
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "image/jpeg",
                    "filename": "1000210177.jpg",
                    "body": {"attachmentId": "att1", "size": 42613},
                }
            ],
        },
    }
    value = parse_message(raw)

    assert value["has_attachments"] is True
    assert value["attachments"] == [
        {
            "filename": "1000210177.jpg",
            "mime_type": "image/jpeg",
            "type": "image",
            "attachment_id": "att1",
            "size": 42613,
        }
    ]


def test_classify_attachment_type():
    assert classify_attachment_type("image/jpeg") == "image"
    assert classify_attachment_type("image/png") == "image"
    assert classify_attachment_type("application/pdf") == "pdf"
    assert classify_attachment_type("application/zip") == "archive"
    assert (
        classify_attachment_type(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        == "document"
    )
    assert (
        classify_attachment_type(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        == "spreadsheet"
    )
    assert classify_attachment_type("video/mp4") == "video"
    assert classify_attachment_type("audio/mpeg") == "audio"
    assert classify_attachment_type("text/plain") == "text"
    assert classify_attachment_type("application/octet-stream") == "other"
    assert classify_attachment_type(None) == "other"