"""Media safety tests using local fixture bytes, never a user's API."""

import asyncio
import base64
import contextlib
import hashlib
import os
import socket
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from astrbot_plugin_qwenpaw_bridge import media_bridge as media
import test_astrbot_adapter as fixture


SID = "ab_" + "a" * 32
OTHER = "ab_" + "b" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"fixture png bytes"


class FakeContent:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.reads = 0

    async def read(self, size):
        self.reads += 1
        return self.chunks.pop(0) if self.chunks else b""

    async def iter_chunked(self, size):
        while self.chunks:
            yield self.chunks.pop(0)


class FakeResponse:
    def __init__(self, chunks=(), status=200, length=None, content_type="application/octet-stream", encoding="identity"):
        self.content = FakeContent(chunks)
        self.status = status
        self.content_length = length
        self.content_type = content_type
        self.headers = {"Content-Encoding": encoding}

    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass


class FakeSession:
    def __init__(self, response): self.response = response
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def get(self, url, **kwargs):
        if kwargs.get("allow_redirects") is not False:
            raise AssertionError("Redirects must be disabled")
        if kwargs.get("auto_decompress") is not False:
            raise AssertionError("Decompression must be disabled")
        return self.response


class MediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()
        self.root = self.base / "shared"
        self.factories = {"text": fixture.Plain, "image": fixture.Image, "file": fixture.File,
                          "video": fixture.Video, "audio": fixture.Record}
        self.bridge = media.MediaBridge(self.root, [self.source], component_types=self.factories)

    def tearDown(self): self.temp.cleanup()

    def outbound(self, name="report-2026.pdf", data=b"fixture data", sid=SID):
        path = self.root / sid / "outbound" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        block = {"type": "file", "path": path.relative_to(self.root).as_posix(),
                 "filename": path.name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        return path, block

    async def mocked_download(self, response, kind="image", filename="image.jpg"):
        resolver = media.PinnedResolver("cdn.example", [("8.8.8.8", socket.AF_INET)])
        with patch.object(media, "pinned_target", AsyncMock(return_value=("https://cdn.example/file", resolver))), \
                patch.object(media.aiohttp, "TCPConnector", return_value=object()), \
                patch.object(media.aiohttp, "ClientSession", return_value=FakeSession(response)):
            return await self.bridge._download(SID, kind, "https://cdn.example/file", filename)

    async def test_inbound_copy_retains_bytes_and_real_image_suffix(self):
        source = self.source / "adapter-guessed.jpg"
        source.write_bytes(PNG)
        asset = await self.bridge.import_component(SID, "image", fixture.Image(file=str(source)))
        self.assertEqual(asset.path.read_bytes(), PNG)
        self.assertEqual(asset.path.suffix, ".png")
        self.assertTrue(asset.path.is_relative_to(self.root / SID / "inbound"))
        self.assertEqual(asset.native()["image_url"], str(asset.path))
        self.assertEqual(asset.sha256, hashlib.sha256(PNG).hexdigest())

    async def test_file_component_reads_raw_file_field_without_sync_property(self):
        source = self.source / "report.pdf"
        source.write_bytes(b"file bytes")
        component = fixture.File(name="../../report.pdf", file=str(source))
        asset = await self.bridge.import_component(SID, "file", component)
        self.assertEqual(asset.path.read_bytes(), b"file bytes")
        self.assertEqual(asset.filename, "report.pdf")

    async def test_qq_audio_file_id_uses_bounded_provider_url_download(self):
        component = fixture.Record(file="voice-cache-id", url="http://cdn.example/voice.silk")
        asset = media.StoredMedia("audio", self.root / "voice.silk", "voice.silk", 5, "0" * 64)
        with patch.object(self.bridge, "_download", AsyncMock(return_value=asset)) as download:
            result = await self.bridge.import_component(SID, "audio", component)
        self.assertIs(result, asset)
        self.assertEqual(download.await_args.args[2], "http://cdn.example/voice.silk")

    async def test_qq_video_url_discarded_by_component_is_recovered_from_raw_event(self):
        component = fixture.Video(file="video-cache-id")
        event = fixture.Event()
        event.message_obj.raw_message = types.SimpleNamespace(message=[
            {"type": "video", "data": {"file": "different-id", "url": "https://wrong.example/video"}},
            {"type": "video", "data": {"file": "video-cache-id", "url": "https://cdn.example/video.mp4"}},
        ])
        recovered = media.provider_component(event, component, "video")
        self.assertEqual(recovered.url, "https://cdn.example/video.mp4")
        asset = media.StoredMedia("video", self.root / "video.mp4", "video.mp4", 5, "0" * 64)
        with patch.object(self.bridge, "_download", AsyncMock(return_value=asset)) as download:
            await self.bridge.import_component(SID, "video", recovered)
        self.assertEqual(download.await_args.args[2], "https://cdn.example/video.mp4")

    async def test_base64_is_validated_and_bounded_before_writing(self):
        small = media.MediaBridge(self.root, [self.source], max_bytes=16)
        with self.assertRaises(media.MediaFailure):
            await small.import_component(SID, "image", fixture.Image(file="base64://" + base64.b64encode(b"x" * 17).decode()))
        with self.assertRaises(media.MediaFailure):
            await small.import_component(SID, "image", fixture.Image(file="base64://invalid*payload"))
        self.assertEqual(list(self.root.rglob("*.*")), [])

    async def test_local_file_outside_source_roots_is_refused(self):
        source = self.base / "private.txt"
        source.write_bytes(b"never copy this")
        with self.assertRaises(media.MediaFailure) as exc:
            await self.bridge.import_component(SID, "file", fixture.File(file=str(source)))
        self.assertEqual(str(exc.exception), "media_path_outside_allowed_roots")

    async def test_path_traversal_is_not_normalized_away(self):
        value = str(self.source / ".." / "private.txt")
        with self.assertRaises(media.MediaFailure): media.local_path(value)
        _, block = self.outbound()
        block["path"] = SID + "/outbound/../outbound/report-2026.pdf"
        with self.assertRaises(media.MediaFailure): await self.bridge.snapshot(SID, block)

    async def test_hardlinked_source_is_rejected(self):
        source = self.source / "original.bin"
        source.write_bytes(b"data")
        try:
            os.link(source, self.source / "linked.bin")
        except OSError:
            self.skipTest("Hardlinks unavailable in this test filesystem")
        with self.assertRaises(media.MediaFailure):
            await self.bridge.import_component(SID, "file", fixture.File(file=str(source)))

    async def test_hardlink_added_while_copying_is_rejected(self):
        source = self.source / "changing.bin"
        source.write_bytes(b"source bytes")
        linked = self.source / "new-link.bin"
        original_reader = media.safe_reader

        @contextlib.contextmanager
        def changing_reader(*args):
            with original_reader(*args) as stream:
                class ChangingStream:
                    def fileno(self): return stream.fileno()
                    def read(self, count):
                        chunk = stream.read(count)
                        if chunk and not linked.exists():
                            os.link(source, linked)
                        return chunk
                yield ChangingStream()

        with patch.object(media, "safe_reader", changing_reader):
            with self.assertRaises(media.MediaFailure):
                await self.bridge.import_component(SID, "file", fixture.File(file=str(source)))
        self.assertEqual(list((self.root / SID / "inbound").glob("*")), [])

    async def test_symlinked_source_and_intermediate_directory_are_rejected(self):
        source = self.source / "original.bin"
        source.write_bytes(b"data")
        try:
            os.symlink(source, self.source / "link.bin")
            os.symlink(self.source, self.base / "linked-source", target_is_directory=True)
        except OSError:
            self.skipTest("Symlinks unavailable in this test filesystem")
        for path in (self.source / "link.bin", self.base / "linked-source" / "original.bin"):
            bridge = media.MediaBridge(self.root, [self.base])
            with self.assertRaises(media.MediaFailure):
                await bridge.import_component(SID, "file", fixture.File(file=str(path)))

    async def test_reparse_points_are_rejected(self):
        self.assertTrue(media._has_link(types.SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)))

    @unittest.skipUnless(os.name == "posix", "POSIX FIFO check")
    async def test_fifo_is_rejected_without_waiting_for_a_writer(self):
        path = self.source / "fifo"
        os.mkfifo(path)
        with self.assertRaises(media.MediaFailure):
            with media.safe_reader(path, [self.source], 20): pass

    async def test_outbound_descriptor_enforces_session_and_relative_paths(self):
        path, block = self.outbound(sid=OTHER)
        with self.assertRaises(media.MediaFailure): await self.bridge.snapshot(SID, block)
        path, block = self.outbound()
        block["path"] = str(path)
        with self.assertRaises(media.MediaFailure): await self.bridge.snapshot(SID, block)

    async def test_nested_outbound_path_and_filename_are_preserved(self):
        path, block = self.outbound("nested/report-2026.pdf")
        asset = await self.bridge.snapshot(SID, block)
        self.assertEqual(asset.filename, "report-2026.pdf")
        self.assertEqual(asset.path.read_bytes(), path.read_bytes())
        self.assertTrue(asset.path.is_relative_to(self.root / SID / "delivery"))

    async def test_native_file_uri_is_allowed_only_for_valid_own_outbound(self):
        path, _ = self.outbound()
        asset = await self.bridge.snapshot(SID, {"type": "file", "file_url": path.as_uri()}, allow_native=True)
        self.assertEqual(asset.filename, "report-2026.pdf")
        with self.assertRaises(media.MediaFailure):
            await self.bridge.snapshot(SID, {"type": "file", "file_url": path.as_uri()})

    async def test_hash_or_size_tamper_is_rejected_and_snapshot_removed(self):
        _, block = self.outbound()
        for wrong in ({"size": block["size"] + 1}, {"sha256": "0" * 64}):
            with self.assertRaises(media.MediaFailure): await self.bridge.snapshot(SID, {**block, **wrong})
        self.assertEqual(list((self.root / SID / "delivery").glob("*")), [])

    async def test_snapshot_bytes_do_not_change_with_original(self):
        path, block = self.outbound(data=b"original bytes")
        asset = await self.bridge.snapshot(SID, block)
        path.write_bytes(b"changed bytes")
        self.assertEqual(asset.path.read_bytes(), b"original bytes")

    async def test_wechat_audio_is_file_with_notice_and_qq_audio_is_record(self):
        _, block = self.outbound("audio.wav", b"RIFFaudio data")
        block["type"] = "audio"
        wechat = await self.bridge.output_components(SID, [block], "weixin_oc")
        self.assertIsInstance(wechat[0], fixture.Plain)
        self.assertIn("以文件发送", wechat[0].text)
        self.assertIsInstance(wechat[1], fixture.File)
        qq = await self.bridge.output_components(SID, [block], "aiocqhttp")
        self.assertIsInstance(qq[0], fixture.Record)

    async def test_attachment_count_and_limits_have_hard_caps(self):
        for kwargs in ({"max_bytes": media.DEFAULT_MAX_BYTES + 1}, {"max_files": 5}, {"max_bytes": 0}):
            with self.assertRaises(media.MediaFailure): media.MediaBridge(self.root, [self.source], **kwargs)
        with self.assertRaises(media.MediaFailure):
            await self.bridge.output_components(SID, [{"type": "image"}] * 5, "aiocqhttp")

    async def test_unsafe_second_attachment_prevents_partial_return(self):
        _, first = self.outbound("first.bin")
        _, second = self.outbound("other.bin", sid=OTHER)
        with self.assertRaises(media.MediaFailure):
            await self.bridge.output_components(SID, [first, second], "aiocqhttp")
        self.assertEqual(list((self.root / SID / "delivery").glob("*")), [])

    async def test_public_dns_is_pinned_and_http_only_upgrades_to_https(self):
        loop = asyncio.get_running_loop()
        dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=dns)) as lookup:
            url, resolver = await media.pinned_target("http://cdn.example/image?signature=fixture", {"napcat"})
        self.assertEqual(url, "https://cdn.example/image?signature=fixture")
        self.assertEqual(lookup.await_args.args[1], 443)
        values = await resolver.resolve("cdn.example", 443)
        self.assertEqual(values[0]["host"], "8.8.8.8")

    async def test_private_multicast_and_mixed_dns_answers_are_refused(self):
        loop = asyncio.get_running_loop()
        for ip in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "239.1.1.1", "0.0.0.0", "::1"):
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            dns = [(family, socket.SOCK_STREAM, 6, "", (ip, 443))]
            with patch.object(loop, "getaddrinfo", AsyncMock(return_value=dns)):
                with self.assertRaises(media.MediaFailure): await media.pinned_target("https://cdn.example/image", {"napcat"})
        mixed = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ("8.8.8.8", "10.0.0.1")]
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=mixed)):
            with self.assertRaises(media.MediaFailure): await media.pinned_target("https://cdn.example/image", {"napcat"})

    async def test_only_exact_napcat_http_hostname_may_use_private_address(self):
        loop = asyncio.get_running_loop()
        dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.18.0.3", 6099))]
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=dns)):
            url, _ = await media.pinned_target("http://napcat:6099/file", {"napcat"})
            self.assertEqual(url, "http://napcat:6099/file")
            with self.assertRaises(media.MediaFailure): await media.pinned_target("http://napcat.evil/file", {"napcat"})

    async def test_download_png_and_jpeg_magic_use_actual_suffix(self):
        png = await self.mocked_download(FakeResponse([PNG], content_type="image/jpeg"))
        self.assertEqual(png.path.suffix, ".png")
        self.assertEqual(png.path.read_bytes(), PNG)
        jpeg_bytes = b"\xff\xd8\xff" + b"fixture jpeg bytes"
        jpeg = await self.mocked_download(FakeResponse([jpeg_bytes]), filename="image.png")
        self.assertEqual(jpeg.path.suffix, ".jpg")
        self.assertEqual(jpeg.path.read_bytes(), jpeg_bytes)

    async def test_aac_adts_is_not_mistaken_for_mp3(self):
        for header in (b"\xff\xf0", b"\xff\xf1", b"\xff\xf8", b"\xff\xf9"):
            self.assertEqual(media.media_filename("audio", "audio.bin", header + b"fixture"), "audio.aac")
        self.assertEqual(media.media_filename("audio", "audio.bin", b"\xff\xfbfixture"), "audio.mp3")

    async def test_redirect_and_oversized_stream_are_refused(self):
        with self.assertRaises(media.MediaFailure): await self.mocked_download(FakeResponse(status=302))
        self.bridge.max_bytes = 10
        long_response = FakeResponse([b"12345678", b"abcdefgh"])
        with self.assertRaises(media.MediaFailure): await self.mocked_download(long_response)
        self.assertEqual(list((self.root / SID / "inbound").glob("*")), [])

    async def test_oversized_header_rejects_before_reading_body(self):
        response = FakeResponse([b"body"], length=media.DEFAULT_MAX_BYTES + 1)
        with self.assertRaises(media.MediaFailure): await self.mocked_download(response)
        self.assertEqual(response.content.reads, 0)

    async def test_windows_aliases_are_canonicalized_only_after_link_checks(self):
        if os.name != "nt":
            self.skipTest("Windows 8.3 aliases")
        root = media.local_path(self.temp.name)
        self.assertEqual(root, self.base)
        self.assertTrue(media.local_path(str(Path(self.temp.name) / "future")).is_relative_to(self.base))


if __name__ == "__main__":
    unittest.main()
