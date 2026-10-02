"""Offline checks for page parsing and failure handling, without personal fixtures."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch = load("fetch_xhs_video")
prepare = load("prepare_video")


class PageReaderTests(unittest.TestCase):
    note_id = "67f4e33e000000001b025337"

    def page(self, note):
        state = {"LAUNCHER_SSR_STORE_PAGE_DATA": {"noteData": note}}
        return "<script>window.__SETUP_SERVER_STATE__=" + json.dumps(state) + ";</script>"

    def test_matches_actual_note_not_unrelated_page_data(self):
        note = {"noteId": self.note_id, "title": "测试笔记", "video": {}}
        self.assertEqual(fetch.extract_note(self.page(note), self.note_id)["title"], "测试笔记")
        with self.assertRaises(fetch.ReadError):
            fetch.extract_note(self.page(note), "0" * 24)

    def test_login_or_summary_is_not_a_readable_video(self):
        with self.assertRaises(fetch.ReadError):
            fetch.extract_note("<title>登录</title><p>视频简介</p>", self.note_id)

    def test_login_redirect_keeps_share_parameters(self):
        target = fetch.normalize_page_url(
            "https://www.xiaohongshu.com/login?redirectPath="
            "http%3A%2F%2Fwww.xiaohongshu.com%2Fdiscovery%2Fitem%2F" + self.note_id + "%3Fa%3D1"
        )
        self.assertEqual(target, "https://www.xiaohongshu.com/discovery/item/" + self.note_id + "?a=1")

    def test_js_undefined_is_not_replaced_inside_text(self):
        self.assertEqual(fetch.json_literals('{"text":"undefined","value":undefined}'),
                         '{"text":"undefined","value":null}')

    def test_media_selection_prefers_h264_and_rejects_unrelated_hosts(self):
        note = {"video": {"media": {"stream": {
            "h265": [{"masterUrl": "https://sns-video-v6.xhscdn.com/second.mp4"}],
            "h264": [{"masterUrl": "http://sns-video-v6.xhscdn.com/first.mp4",
                      "backupUrls": ["https://unrelated.invalid/file.mp4"]}],
        }}}}
        self.assertEqual(fetch.media_urls(note), ["https://sns-video-v6.xhscdn.com/first.mp4",
                                                 "https://sns-video-v6.xhscdn.com/second.mp4"])

    def test_missing_media_tools_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "缺少可用工具"):
                prepare.prepare(Path(directory) / "video.mp4", directory,
                                ffmpeg="not-a-real-ffmpeg-binary", ffprobe="not-a-real-ffprobe-binary")

    def test_missing_or_changed_media_data_is_a_read_error(self):
        for note in ({"video": {"media": None}}, {"video": {"media": {"stream": {"h264": None}}}}):
            with self.subTest(note=note), self.assertRaises(fetch.ReadError):
                fetch.media_urls(note)
        note = {"video": {"media": {"stream": {"h264": [
            {"masterUrl": "https://sns-video-v6.xhscdn.com/first.mp4", "backupUrls": None}
        ]}}}}
        self.assertEqual(fetch.media_urls(note), ["https://sns-video-v6.xhscdn.com/first.mp4"])


if __name__ == "__main__":
    unittest.main()
