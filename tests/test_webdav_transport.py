"""Offline WebDAV protocol tests; no credentials or traffic to real providers."""
from __future__ import annotations

from email.message import Message
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import ipaddress
import io
from pathlib import Path
import ssl
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

from bundle_updater.webdav_transport import (
    CHUNK_BYTES, MAX_RESPONSE_BYTES, WebDAVClient, WebDAVError,
    validate_target_url,
)


NAMESPACE = "astrbot-qwenpaw/" + "a" * 32
FILENAME = "b" * 32 + ".aqbackup"
TARGET = {"id": "test", "name": "Test fixture", "enabled": True,
          "url": "https://dav.example.test/storage/", "username": "backup-user",
          "password": "fixture-app-password"}


class Response:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = str(value)
        self.body = io.BytesIO(body)
        self.reads = []

    def read(self, size):
        self.reads.append(size)
        return self.body.read(size)


class Server:
    """A streaming, in-memory WebDAV peer behind the HTTPSConnection seam."""

    def __init__(self, base="/storage/"):
        self.base = base
        self.directories = {base}
        self.files = {}
        self.requests = []
        self.connections = []
        self.hook = None
        self.error = None
        self.head_supported = True

    def connection(self, host, *, port, timeout, context):
        owner = self

        class Connection:
            def __init__(self):
                self.host, self.port, self.timeout, self.context = host, port, timeout, context
                self.closed = False
                self.sock = None
                self.headers = {}
                self.chunks = []
                self.digest = hashlib.sha256()
                self.total = 0
                self.preview = b""

            def putrequest(self, method, path, **kwargs):
                self.method, self.path = method, path

            def putheader(self, key, value):
                self.headers[key] = value

            def endheaders(self):
                if owner.error:
                    raise owner.error

            def send(self, payload):
                self.chunks.append(len(payload))
                self.digest.update(payload)
                self.total += len(payload)
                if self.total < 2048:
                    self.preview += payload

            def getresponse(self):
                owner.requests.append(self)
                if owner.hook:
                    result = owner.hook(self)
                    if result is not None:
                        return result
                return owner.respond(self)

            def close(self):
                self.closed = True

        conn = Connection()
        self.connections.append(conn)
        return conn

    def respond(self, request):
        method, path = request.method, request.path
        if method == "PROPFIND":
            if path not in self.directories and path not in self.files:
                return Response(404)
            kind = "<d:collection/>" if path in self.directories else ""
            size = self.files.get(path, {}).get("size")
            size_element = "" if size is None else f"<d:getcontentlength>{size}</d:getcontentlength>"
            body = (f'<d:multistatus xmlns:d="DAV:"><d:response><d:href>{escape(path)}</d:href>'
                    '<d:propstat><d:prop><d:resourcetype>' + kind + '</d:resourcetype>' + size_element +
                    '</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>'
                    '</d:response></d:multistatus>').encode()
            return Response(207, body, {"Content-Length": len(body)})
        if method == "MKCOL":
            if path in self.directories:
                return Response(405)
            self.directories.add(path)
            return Response(201)
        if method == "PUT":
            if path in self.files and request.headers.get("If-None-Match") == "*":
                return Response(412)
            self.files[path] = {"size": request.total, "body": request.preview,
                                "digest": request.digest.hexdigest()}
            return Response(201)
        if method == "HEAD":
            if not self.head_supported:
                return Response(405)
            if path not in self.files:
                return Response(404)
            return Response(200, headers={"Content-Length": self.files[path]["size"]})
        if method == "GET":
            if path not in self.files:
                return Response(404)
            data = self.files[path]["body"]
            return Response(200, data, {"Content-Length": len(data)})
        if method == "MOVE":
            destination = urlsplit(request.headers["Destination"]).path
            if destination in self.files and request.headers.get("Overwrite") == "F":
                return Response(412)
            self.files[destination] = self.files.pop(path)
            return Response(201)
        if method == "DELETE":
            existed = path in self.files
            self.files.pop(path, None)
            return Response(204 if existed else 404)
        return Response(405)


class URLTests(unittest.TestCase):
    def test_normalizes_directory_encoding_and_https_origin(self):
        self.assertEqual(validate_target_url("HTTPS://DAV.Example.test:443/备份/每日 文件"),
                         "https://dav.example.test/%E5%A4%87%E4%BB%BD/%E6%AF%8F%E6%97%A5%20%E6%96%87%E4%BB%B6/")
        self.assertEqual(validate_target_url("https://example.test/a%20b/"), "https://example.test/a%20b/")
        self.assertEqual(validate_target_url("https://example.test"), "https://example.test/")
        self.assertEqual(validate_target_url("https://[2001:db8::1]:8443/webdav"),
                         "https://[2001:db8::1]:8443/webdav/")

    def test_rejects_traversal_authority_and_ambiguous_urls(self):
        urls = [None, "", "http://example.test/", "https://u:p@example.test/", "https://example.test/?x=1",
                "https://example.test/#frag", "https://example.test/?", "https://example.test/#",
                "https://example.test/a/../b", "https://example.test/a/./b",
                "https://example.test/%2e%2e/", "https://example.test/%252e%252e/",
                "https://example.test/%2Fescape/", "https://example.test/%5cescape/",
                "https://example.test/\\evil", "https://example.test/a//b", "https://example.test/%00",
                "https://example.test/%GG", "https://example.test/%ff", "https://example.test/a;b",
                "https://example.test/..%20/escape", "https://example.test/folder./",
                " https://example.test/", "https://example.test/\n", "https://example.test:70000/",
                "https://ex ample.test/", "https://[fe80::1%25eth0]/", "https://example.test/%0D%0A"]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(WebDAVError) as context:
                validate_target_url(url)
            self.assertEqual(context.exception.code, "url_invalid")


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.archive = Path(self.temporary.name) / FILENAME
        self.payload = b"encrypted fixture archive\n" * 100
        self.archive.write_bytes(self.payload)
        self.server = Server()
        self.client = WebDAVClient(TARGET, connection_factory=self.server.connection)

    def assert_code(self, code, callback):
        with self.assertRaises(WebDAVError) as context:
            callback()
        self.assertEqual(context.exception.code, code)
        self.assertEqual(str(context.exception), code)

    def test_probe_verifies_read_and_cleans_its_file(self):
        self.client.probe(NAMESPACE)
        self.assertEqual(self.server.files, {})
        self.assertEqual([r.method for r in self.server.requests],
                         ["PROPFIND", "MKCOL", "MKCOL", "PUT", "GET", "DELETE"])
        self.assertTrue(all(c.closed for c in self.server.connections))
        self.assertTrue(all(c.context.check_hostname and c.context.verify_mode == ssl.CERT_REQUIRED
                            for c in self.server.connections))
        self.assertTrue(all(c.host == "dav.example.test" and c.port == 443 for c in self.server.connections))

    def test_only_creates_directories_below_existing_base(self):
        self.client.probe(NAMESPACE)
        self.client.probe(NAMESPACE)
        self.assertEqual(self.server.directories, {"/storage/", "/storage/astrbot-qwenpaw/",
                                                  "/storage/" + NAMESPACE + "/"})
        self.assertTrue(all(r.path.startswith("/storage/astrbot-qwenpaw/")
                            for r in self.server.requests if r.method == "MKCOL"))

    def test_missing_base_is_not_created(self):
        self.server.directories.clear()
        self.assert_code("remote_invalid", lambda: self.client.probe(NAMESPACE))
        self.assertEqual([r.method for r in self.server.requests], ["PROPFIND"])

    def test_upload_streams_and_publishes_after_size_checks(self):
        self.payload = b"Z" * (CHUNK_BYTES * 5 + 17)
        self.archive.write_bytes(self.payload)
        result = self.client.upload(self.archive, NAMESPACE, FILENAME)
        self.assertEqual(result, {"filename": FILENAME, "size": len(self.payload)})
        final = "/storage/" + NAMESPACE + "/" + FILENAME
        self.assertEqual(set(self.server.files), {final})
        self.assertEqual(self.server.files[final]["digest"], hashlib.sha256(self.payload).hexdigest())
        put = next(r for r in self.server.requests if r.method == "PUT")
        self.assertEqual(put.chunks, [CHUNK_BYTES] * 5 + [17])
        self.assertEqual(put.headers["Content-Length"], str(len(self.payload)))
        self.assertNotIn("Transfer-Encoding", put.headers)
        move = next(r for r in self.server.requests if r.method == "MOVE")
        self.assertEqual(move.headers["Overwrite"], "F")
        self.assertEqual(move.headers["Destination"], "https://dav.example.test" + final)
        self.assertEqual([r.method for r in self.server.requests][-4:], ["PUT", "HEAD", "MOVE", "HEAD"])
        self.assertFalse(any(r.method == "GET" for r in self.server.requests))

    def test_head_not_supported_uses_bounded_depth_zero_propfind(self):
        self.server.head_supported = False
        result = self.client.upload(self.archive, NAMESPACE, FILENAME)
        self.assertEqual(result["size"], len(self.payload))
        requests = [r for r in self.server.requests if r.method == "PROPFIND"]
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(r.headers["Depth"] == "0" for r in requests))

    def test_head_501_falls_back(self):
        self.server.hook = lambda r: Response(501) if r.method == "HEAD" else None
        self.assertEqual(self.client.upload(self.archive, NAMESPACE, FILENAME)["size"], len(self.payload))

    def test_redirect_never_followed_or_body_read(self):
        response = Response(301, b"credential echo", {"Location": "https://attacker.example/capture"})
        self.server.hook = lambda r: response
        self.assert_code("redirect", lambda: self.client.probe(NAMESPACE))
        self.assertEqual(len(self.server.connections), 1)
        self.assertEqual(self.server.connections[0].host, "dav.example.test")
        self.assertEqual(response.reads, [])

    def test_errors_are_fixed_and_do_not_read_server_body(self):
        for status, code in [(401, "authentication"), (403, "permission"), (429, "remote_unavailable"),
                             (500, "remote_unavailable"), (507, "storage_full")]:
            with self.subTest(status=status):
                response = Response(status, b"secret credential echo")
                self.server.hook = lambda r: response
                self.assert_code(code, lambda: self.client.probe(NAMESPACE))
                self.assertEqual(response.reads, [])

    def test_network_tls_timeout_errors_are_sanitized(self):
        for error, code in [(OSError("password echo"), "connection"),
                            (ssl.SSLError("password echo"), "tls"),
                            (TimeoutError("password echo"), "timeout")]:
            self.server.error = error
            self.assert_code(code, lambda: self.client.probe(NAMESPACE))
            self.assertTrue(self.server.connections[-1].closed)

    def test_partial_upload_size_mismatch_is_not_published(self):
        self.server.hook = lambda r: Response(200, headers={"Content-Length": 1}) if r.method == "HEAD" else None
        self.assert_code("remote_size", lambda: self.client.upload(self.archive, NAMESPACE, FILENAME))
        self.assertFalse(any(r.method == "MOVE" for r in self.server.requests))
        self.assertEqual(self.server.files, {})
        self.assertEqual(self.server.requests[-1].method, "DELETE")
        self.assertIn(".aq-upload-", self.server.requests[-1].path)

    def test_final_size_mismatch_is_not_reported_successful(self):
        def hook(request):
            if request.method == "HEAD" and request.path.endswith(FILENAME):
                return Response(200, headers={"Content-Length": 1})
        self.server.hook = hook
        self.assert_code("remote_size", lambda: self.client.upload(self.archive, NAMESPACE, FILENAME))
        # Cleanup only touches the unique temporary path, never a potentially
        # pre-existing final archive if a non-compliant server ignored MOVE.
        self.assertIn(".aq-upload-", self.server.requests[-1].path)

    def test_move_failure_removes_temp_but_preserves_other_files(self):
        unrelated = "/storage/" + NAMESPACE + "/other-file"
        self.server.files[unrelated] = {"size": 0}
        self.server.hook = lambda r: Response(409) if r.method == "MOVE" else None
        self.assert_code("remote_invalid", lambda: self.client.upload(self.archive, NAMESPACE, FILENAME))
        self.assertEqual(set(self.server.files), {unrelated})

    def test_existing_archive_is_not_overwritten(self):
        final = "/storage/" + NAMESPACE + "/" + FILENAME
        self.server.files[final] = {"size": 5, "body": b"older"}
        self.assert_code("conflict", lambda: self.client.upload(self.archive, NAMESPACE, FILENAME))
        self.assertEqual(self.server.files[final]["body"], b"older")
        self.assertEqual(set(self.server.files), {final})

    def test_retry_after_published_move_verifies_streamed_hash(self):
        self.payload = b"r" * (CHUNK_BYTES * 4 + 27)
        self.archive.write_bytes(self.payload)
        final = "/storage/" + NAMESPACE + "/" + FILENAME
        self.server.files[final] = {"size": len(self.payload), "body": self.payload}
        response = Response(200, self.payload, {"Content-Length": len(self.payload)})
        self.server.hook = lambda r: response if r.method == "GET" and r.path == final else None
        result = self.client.upload(self.archive, NAMESPACE, FILENAME)
        self.assertEqual(result, {"filename": FILENAME, "size": len(self.payload)})
        self.assertEqual(set(self.server.files), {final})
        self.assertTrue(all(0 < size <= CHUNK_BYTES for size in response.reads))
        self.assertGreater(len(response.reads), 4)
        self.assertEqual(self.server.requests[-1].method, "DELETE")

    def test_same_size_existing_file_requires_matching_hash(self):
        final = "/storage/" + NAMESPACE + "/" + FILENAME
        self.server.files[final] = {"size": len(self.payload), "body": b"X" * len(self.payload)}
        self.assert_code("conflict", lambda: self.client.upload(self.archive, NAMESPACE, FILENAME))
        self.assertEqual(set(self.server.files), {final})
        self.assertEqual(self.server.files[final]["body"], b"X" * len(self.payload))
        self.assertTrue(any(r.method == "GET" for r in self.server.requests))

    def test_idempotent_get_extra_bytes_cannot_report_success(self):
        final = "/storage/" + NAMESPACE + "/" + FILENAME
        self.server.files[final] = {"size": len(self.payload), "body": self.payload + b"extra"}
        response = Response(200, self.payload + b"extra")
        self.server.hook = lambda r: response if r.method == "GET" else None
        self.assert_code("conflict", lambda: self.client.upload(self.archive, NAMESPACE, FILENAME))
        self.assertEqual(set(self.server.files), {final})

    def test_put_conflict_does_not_delete_the_conflicting_path(self):
        self.server.hook = lambda r: Response(412) if r.method == "PUT" else None
        self.assert_code("conflict", lambda: self.client.upload(self.archive, NAMESPACE, FILENAME))
        self.assertFalse(any(r.method == "DELETE" for r in self.server.requests))

    def test_probe_content_mismatch_cleans_probe(self):
        self.server.hook = lambda r: Response(200, b"wrong") if r.method == "GET" else None
        self.assert_code("remote_size", lambda: self.client.probe(NAMESPACE))
        self.assertEqual(self.server.files, {})

    def test_probe_cleanup_failure_is_not_success(self):
        self.server.hook = lambda r: Response(409) if r.method == "DELETE" else None
        self.assert_code("cleanup_failed", lambda: self.client.probe(NAMESPACE))

    def test_response_limit_without_trusting_content_length(self):
        response = Response(207, b"x" * (MAX_RESPONSE_BYTES + 1))
        self.server.hook = lambda r: response
        self.assert_code("remote_invalid", lambda: self.client.probe(NAMESPACE))
        self.assertTrue(all(0 < count <= 16 * 1024 for count in response.reads))

    def test_declared_oversize_response_is_not_read(self):
        response = Response(207, headers={"Content-Length": MAX_RESPONSE_BYTES + 1})
        self.server.hook = lambda r: response
        self.assert_code("remote_invalid", lambda: self.client.probe(NAMESPACE))
        self.assertEqual(response.reads, [])

    def test_xml_entity_and_wrong_resource_are_rejected(self):
        for raw in [b'<!DOCTYPE x [<!ENTITY a "x">]><x>&a;</x>',
                    '<!DOCTYPE x [<!ENTITY a "x">]><x>&a;</x>'.encode("utf-16"),
                    b'<multistatus xmlns="DAV:"><response><href>/other/</href>'
                    b'<propstat><prop><resourcetype><collection/></resourcetype></prop>'
                    b'<status>HTTP/1.1 200 OK</status></propstat></response></multistatus>']:
            self.server.hook = lambda r: Response(207, raw)
            self.assert_code("remote_invalid", lambda: self.client.probe(NAMESPACE))

    def test_duplicate_length_header_rejected(self):
        response = Response(207, headers={"Content-Length": "0"})
        response.headers["Content-Length"] = "1"
        self.server.hook = lambda r: response
        self.assert_code("remote_invalid", lambda: self.client.probe(NAMESPACE))

    def test_delete_missing_file_is_idempotent(self):
        self.client.delete(NAMESPACE, FILENAME)
        self.assertEqual([r.method for r in self.server.requests], ["DELETE"])
        self.assertEqual(self.server.requests[0].path, "/storage/" + NAMESPACE + "/" + FILENAME)

    def test_user_paths_cannot_escape_owned_namespace(self):
        for namespace in ["../", "astrbot-qwenpaw/../x", "astrbot-qwenpaw/%2e%2e",
                          "astrbot-qwenpaw/x/y", "/astrbot-qwenpaw/x", "astrbot-qwenpaw/x?z", "anything/x"]:
            self.assert_code("path_invalid", lambda: self.client.delete(namespace, FILENAME))
        for name in ["../secret", "a.aqbackup", "b" * 32 + ".aqbackup/", "/" + FILENAME,
                     "b" * 32 + ".aqbackup?secret", "B" * 32 + ".aqbackup"]:
            self.assert_code("path_invalid", lambda: self.client.delete(NAMESPACE, name))
        self.assertEqual(self.server.connections, [])

    def test_chinese_base_path_reaches_the_expected_encoded_directory(self):
        target = dict(TARGET, url="https://dav.example.test/用户/备份")
        base = urlsplit(validate_target_url(target["url"])).path
        server = Server(base)
        client = WebDAVClient(target, connection_factory=server.connection)
        client.probe(NAMESPACE)
        self.assertTrue(all(r.path.startswith(base) and r.path.isascii() for r in server.requests))

    def test_invalid_credentials_do_not_connect(self):
        for username, password in [("", "secret"), ("user:admin", "secret"),
                                   ("user", ""), ("user\n", "secret"), ("user", "x\r\ny")]:
            self.assert_code("credentials_invalid", lambda: WebDAVClient(
                dict(TARGET, username=username, password=password), connection_factory=self.server.connection))
        self.assertEqual(self.server.connections, [])

    def test_invalid_source_does_not_connect(self):
        for source in [self.archive.parent, self.archive.parent / "missing"]:
            self.assert_code("source_invalid", lambda: self.client.upload(source, NAMESPACE, FILENAME))
        self.archive.write_bytes(b"")
        self.assert_code("source_invalid", lambda: self.client.upload(self.archive, NAMESPACE, FILENAME))
        self.assertEqual(self.server.connections, [])

    def test_request_deadline_aborts_before_body_streaming(self):
        with patch("bundle_updater.webdav_transport.time.monotonic", side_effect=[0, 4000]):
            self.assert_code("timeout", lambda: self.client.probe(NAMESPACE))
        self.assertTrue(self.server.connections[0].closed)


class RealTLSTests(unittest.TestCase):
    """Exercise the real stdlib TLS/HTTP stack against an isolated local peer."""

    def setUp(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.cert_path = root / "localhost.crt"
        key_path = root / "localhost.key"
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
                .not_valid_after(now + timedelta(hours=1))
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
                .sign(key, hashes.SHA256()))
        self.cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
        self.peer = Server()
        peer = self.peer

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _serve(self):
                size = int(self.headers.get("Content-Length", "0"))
                remaining, digest, preview = size, hashlib.sha256(), b""
                while remaining:
                    data = self.rfile.read(min(CHUNK_BYTES, remaining))
                    if not data:
                        break
                    digest.update(data)
                    if size < 2048 or getattr(peer, "preserve_bodies", False):
                        preview += data
                    remaining -= len(data)
                request = SimpleNamespace(method=self.command, path=self.path, headers=self.headers,
                    total=size - remaining, preview=preview, digest=digest)
                peer.requests.append(request)
                response = peer.hook(request) if peer.hook else None
                if response is None:
                    response = peer.respond(request)
                body = response.body.getvalue()
                self.send_response(response.status)
                for key, value in response.headers.items():
                    self.send_header(key, value)
                if "Content-Length" not in response.headers:
                    self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            do_PROPFIND = do_MKCOL = do_PUT = do_GET = do_HEAD = do_MOVE = do_DELETE = _serve

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.cert_path), str(key_path))
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.httpd.shutdown)
        self.target = dict(TARGET, url=f"https://127.0.0.1:{self.httpd.server_port}/storage/")

    def test_default_client_rejects_untrusted_tls_certificate(self):
        with self.assertRaises(WebDAVError) as error:
            WebDAVClient(self.target, timeout=2).probe(NAMESPACE)
        self.assertEqual(error.exception.code, "tls")
        self.assertEqual(self.peer.requests, [])

    def test_real_https_probe_stream_upload_and_delete(self):
        def trusted_local_connection(host, *, port, timeout, context):
            local_context = ssl.create_default_context(cafile=str(self.cert_path))
            return http.client.HTTPSConnection(host, port=port, timeout=timeout, context=local_context)

        client = WebDAVClient(self.target, connection_factory=trusted_local_connection, timeout=2)
        client.probe(NAMESPACE)
        archive = Path(self.temporary.name) / FILENAME
        contents = b"Q" * (CHUNK_BYTES * 7 + 51)
        archive.write_bytes(contents)
        result = client.upload(archive, NAMESPACE, FILENAME)
        final = "/storage/" + NAMESPACE + "/" + FILENAME
        self.assertEqual(result["size"], len(contents))
        self.assertEqual(self.peer.files[final]["digest"], hashlib.sha256(contents).hexdigest())
        client.delete(NAMESPACE, FILENAME)
        self.assertEqual(self.peer.files, {})


if __name__ == "__main__":
    unittest.main()
