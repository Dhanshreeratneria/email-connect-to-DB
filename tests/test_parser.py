from app.services.email_parser import parse_message
def test_parse_plain_email():
 raw={"id":"m1","threadId":"t1","internalDate":"0","labelIds":["INBOX"],"payload":{"headers":[{"name":"From","value":"Alice <alice@example.com>"},{"name":"To","value":"bob@example.com"},{"name":"Subject","value":"Hello"}],"mimeType":"text/plain","body":{"data":"SGVsbG8="}}}
 value=parse_message(raw);assert value["sender_email"]=="alice@example.com";assert value["body_text"]=="Hello";assert value["recipients"]==["bob@example.com"]
