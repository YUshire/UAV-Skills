"""JSON fence validation keeps prose labels outside parseable payloads."""
import unittest

from check_reply_json import check_reply


class ReplyJsonTests(unittest.TestCase):
    def test_two_aircraft_with_labels_outside_blocks(self):
        reply = 'UAV-A:\n```json\n[{"params":{"waypoints":{"count":5,"first":{},"last":{}}}}]\n```\nUAV-B:\n```json\n[]\n```'
        self.assertEqual(check_reply(reply), [])

    def test_label_inside_each_block_is_reported(self):
        errors = check_reply('```json\nUAV-A:\n[]\n```\n```json\nUAV-B:\n[]\n```')
        self.assertEqual([error["block"] for error in errors], [1, 2])

    def test_unclosed_fence_is_reported(self):
        self.assertEqual(check_reply('```json\n[]')[0]["error"], "unclosed JSON fence")

    def test_no_fence_does_not_invent_missing_actions(self):
        self.assertEqual(check_reply("原请求不可行，尚未下发。"), [])


if __name__ == "__main__":
    unittest.main()
