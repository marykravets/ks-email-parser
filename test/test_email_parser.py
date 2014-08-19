import email_parser

import os
import tempfile
from unittest import TestCase


SRC_PATH = os.path.join('test', 'src')
TEMPLATES_DIR = os.path.join('test', 'templates_html')


class TestParser(TestCase):

    def test_list_available_locales(self):
        locales = email_parser.list_locales(SRC_PATH)

        self.assertListEqual(['en'], locales)

    def test_list_available_emails(self):
        emails = email_parser.list_emails(SRC_PATH, 'en')

        self.assertEqual(len(emails), 2)
        email = next(filter(lambda e: e.name == 'dummy_email', emails))
        self.assertEqual('Dummy subject', email.subject)
        self.assertEqual('#head\n\n**strong** content', email.content['content'])

    def test_parse_emails(self):
        with tempfile.TemporaryDirectory() as dest_dir:
            email_parser.parse_emails(SRC_PATH, dest_dir, TEMPLATES_DIR)
            email_files = os.listdir(os.path.join(dest_dir, 'en'))

        expected = [
            'dummy_email.html',
            'dummy_email.subject',
            'dummy_email.text',
            'order_email.html',
            'order_email.subject',
            'order_email.text']
        self.assertListEqual(expected, email_files)


class TestEmail(TestCase):

    def setUp(self):
        email_dir = os.path.join(SRC_PATH, 'en')
        self.email = email_parser.Email.from_xml(email_dir, 'dummy_email.xml')
        self.template_path = os.path.join(SRC_PATH, 'en', 'basic_template.html')

    def test_render_text(self):
        email_text = self.email.content_to_text()

        self.assertTrue('content' in email_text)
        self.assertEqual('head\nstrong content', email_text['content'])

    def test_render_html(self):
        email_html = self.email.content_to_html('')

        self.assertTrue('content' in email_html)
        self.assertEqual('<h1>head</h1>\n<p><strong>strong</strong> content</p>', email_html['content'])

    def test_render_text_anchor_as_href(self):
        d = 'dummy'
        email = email_parser.Email(d, d, d, d, d, {'content': '<a href="http://test.me">test value</a>'})
        content = email.content_to_text()

        self.assertEqual('http://test.me', content['content'])

    def test_render_text_anchor_as_value_if_href_missing(self):
        d = 'dummy'
        email = email_parser.Email(d, d, d, d, d, {'content': '<a>test value</a>'})
        content = email.content_to_text()

        self.assertEqual('test value', content['content'])

    def test_render_html_with_css(self):
        email_html = self.email.content_to_html('h1 {font-size:12px;}')

        self.assertTrue('content' in email_html)
        expected = '<h1 style="font-size: 12px">head</h1>\n<p><strong>strong</strong> content</p>'
        self.assertEqual(expected, email_html['content'])

    def test_correct_content_order(self):
        email = email_parser.Email.from_xml(os.path.join(SRC_PATH, 'en'), 'order_email.xml')

        self.assertEqual(email.order[0][0], 'content1')
        self.assertEqual(email.order[1][0], 'content2')
