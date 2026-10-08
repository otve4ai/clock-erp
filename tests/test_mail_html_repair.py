import unittest
from email.message import EmailMessage
from unittest.mock import Mock

from app.services.mail import parse_message, sanitize_html
from app.services.mail_html_repair import repair_messages
from tests import test_mail


class MailHTMLRepairTests(unittest.TestCase):
    setUp = test_mail.MailServiceTest.setUp
    tearDown = test_mail.MailServiceTest.tearDown

    def fixture(self):
        message = EmailMessage()
        message['Message-ID'] = '<repair@example.test>'
        message['From'] = 'sender@example.test'
        message['To'] = 'erp@example.test'
        message['Subject'] = 'Order'
        message['Date'] = 'Thu, 08 Oct 2026 09:00:00 +0000'
        message.set_content('<html><head><style>html { width:100%; } @media screen {p{color:red}}</style>'
                            '<title>Hidden title</title></head><body><p>Order received</p>'
                            '<script>alert(1)</script></body></html>', subtype='html')
        message.add_attachment(b'attachment bytes', maintype='application', subtype='octet-stream', filename='order.txt')
        raw = message.as_bytes()
        parsed = parse_message(raw)
        parsed.update(html_body='html { width:100%; }<p>Order received</p>', snippet='html { width:100%; }')
        thread, unused = self.store.ingest(self.account['id'], 'inbox', 'INBOX', '1', 7, parsed)
        client = Mock()
        client.select.return_value = ('OK', [])
        client.response.return_value = ('UIDVALIDITY', [b'1'])
        client.uid.return_value = ('OK', [(b'BODY', raw)])
        return thread, client

    def test_sanitizer_discards_service_content_but_preserves_visible_body(self):
        value, unused = sanitize_html('<head><style>html{width:100%}</style><title>Hidden</title></head>'
                                      '<template><div>hidden<style>x</style></div></template>'
                                      '<script>danger()</script><p>Visible &amp; safe</p>')
        self.assertEqual(value, '<p>Visible &amp; safe</p>')

    def test_unclosed_style_does_not_leak_css(self):
        self.assertEqual(sanitize_html('<p>Visible</p><style>html{color:red}')[0], '<p>Visible</p>')

    def test_repair_preserves_metadata_and_attachments_and_is_idempotent(self):
        thread_id, client = self.fixture()
        before = self.store.get_thread(thread_id)
        dry = repair_messages(self.store, client)
        self.assertEqual(dry['changed'], 1)
        self.assertEqual(self.store.get_thread(thread_id), before)
        result = repair_messages(self.store, client, apply=True)
        self.assertEqual(result['changed'], 1)
        after = self.store.get_thread(thread_id)
        self.assertEqual(after['last_snippet'], 'Order received')
        self.assertEqual(after['messages'][0]['html_body'], '<p>Order received</p>\n')
        for key in ('id', 'status', 'unread_count', 'message_count', 'assignee_id', 'links'):
            self.assertEqual(before[key], after[key])
        for key in ('id', 'thread_id', 'is_read', 'attachments', 'recipients', 'message_id'):
            self.assertEqual(before['messages'][0][key], after['messages'][0][key])
        client.select.assert_called_with('INBOX', readonly=True)
        client.uid.assert_called_with('fetch', '7', '(BODY.PEEK[])')
        self.assertEqual(repair_messages(self.store, client, apply=True)['changed'], 0)

    def test_uidvalidity_change_or_missing_original_never_rewrites_saved_message(self):
        thread_id, client = self.fixture()
        before = self.store.get_thread(thread_id)
        client.response.return_value = ('UIDVALIDITY', [b'2'])
        self.assertEqual(repair_messages(self.store, client, apply=True)['skipped'], 1)
        client.uid.assert_not_called()
        client.response.return_value = ('UIDVALIDITY', [b'1'])
        client.uid.return_value = ('OK', [])
        self.assertEqual(repair_messages(self.store, client, apply=True)['skipped'], 1)
        self.assertEqual(self.store.get_thread(thread_id), before)

    def test_different_message_id_never_rewrites_saved_message(self):
        thread_id, client = self.fixture()
        before = self.store.get_thread(thread_id)
        raw = client.uid.return_value[1][0][1].replace(b'<repair@example.test>', b'<other@example.test>')
        client.uid.return_value = ('OK', [(b'BODY', raw)])
        self.assertEqual(repair_messages(self.store, client, apply=True)['skipped'], 1)
        self.assertEqual(self.store.get_thread(thread_id), before)


if __name__ == '__main__':
    unittest.main()
