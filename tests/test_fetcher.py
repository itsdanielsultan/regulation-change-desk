from __future__ import annotations

import io
import sys
import unittest
import urllib.error
import urllib.request
import urllib.response
from dataclasses import replace
from email.message import Message
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from reg_change_desk.fetcher import SourceDefinition, fetch_snapshot, validate_source_url  # noqa: E402


DEFINITION = SourceDefinition(
    id="demo",
    title="Demo",
    publisher="Official demo",
    jurisdiction="Fictional",
    url="https://official.example.test/laws/demo.xml",
    allowed_host="official.example.test",
    allowed_path_prefix="/laws/demo.xml",
    content_format="xml",
    attribution="Demo",
)


class FakeResponse(io.BytesIO):
    status = 200

    def __init__(self, body: bytes, url: str = DEFINITION.url) -> None:
        super().__init__(body)
        self._url = url
        self.headers = Message()
        self.headers["Content-Type"] = "application/xml"

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FetcherTests(unittest.TestCase):
    def test_rejects_non_https_and_other_hosts(self) -> None:
        for scheme in ("http", "ftp", "file", "data"):
            with self.subTest(scheme=scheme), self.assertRaisesRegex(ValueError, "HTTPS"):
                validate_source_url(DEFINITION, f"{scheme}://official.example.test/laws/demo.xml")
        with self.assertRaisesRegex(ValueError, "host"):
            validate_source_url(DEFINITION, "https://attacker.example/laws/demo.xml")

    def test_path_prefix_lookalikes_are_rejected(self) -> None:
        for suffix in (".evil", "/other", ";other", "%2Fother", "?other=1", "#other"):
            with self.subTest(suffix=suffix), self.assertRaisesRegex(ValueError, "allowlist"):
                validate_source_url(DEFINITION, DEFINITION.url + suffix)
        validate_source_url(DEFINITION, DEFINITION.url)

    def test_invalid_initial_url_is_rejected_before_transport(self) -> None:
        with patch("reg_change_desk.fetcher.urllib.request.build_opener") as transport:
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                fetch_snapshot(replace(DEFINITION, url=DEFINITION.url.replace("https:", "http:")))
        transport.assert_not_called()

    def test_unapproved_redirect_is_not_contacted(self) -> None:
        targets = (
            "https://attacker.example/laws/demo.xml",
            DEFINITION.url + ".evil",
            "http://official.example.test/laws/demo.xml",
            "ftp://official.example.test/laws/demo.xml",
            "file:///laws/demo.xml",
            "data:text/plain,unapproved",
        )
        for target in targets:
            contacted: list[str] = []

            def initial_response(request):
                contacted.append(request.full_url)
                if len(contacted) != 1:
                    self.fail(f"Redirect target was contacted: {request.full_url}")
                headers = Message()
                headers["Location"] = target
                response = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
                response.msg = "Found"
                self.addCleanup(response.close)
                return response

            with self.subTest(target=target), patch(
                "reg_change_desk.fetcher.urllib.request.HTTPSHandler.https_open",
                side_effect=initial_response,
            ), patch(
                "reg_change_desk.fetcher.urllib.request.HTTPHandler.http_open",
                side_effect=AssertionError("Unapproved HTTP target contacted"),
            ), patch(
                "reg_change_desk.fetcher.urllib.request.FTPHandler.ftp_open",
                side_effect=AssertionError("Unapproved FTP target contacted"),
            ), patch(
                "reg_change_desk.fetcher.urllib.request.FileHandler.file_open",
                side_effect=AssertionError("Unapproved file target opened"),
            ), patch(
                "reg_change_desk.fetcher.urllib.request.DataHandler.data_open",
                side_effect=AssertionError("Unapproved data target opened"),
            ):
                with self.assertRaises((ValueError, urllib.error.HTTPError)) as caught:
                    fetch_snapshot(DEFINITION)
                if isinstance(caught.exception, urllib.error.HTTPError):
                    caught.exception.close()
                self.assertEqual(contacted, [DEFINITION.url])

    def test_approved_redirect_still_works(self) -> None:
        contacted: list[str] = []

        def response_for_request(request):
            contacted.append(request.full_url)
            headers = Message()
            if len(contacted) == 1:
                headers["Location"] = "https://official.example.test:443/laws/demo.xml"
                response = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
                response.msg = "Found"
            else:
                headers["Content-Type"] = "application/xml"
                response = urllib.response.addinfourl(
                    io.BytesIO(b"<law>Approved text</law>"), headers, request.full_url, 200
                )
                response.msg = "OK"
            return response

        with patch(
            "reg_change_desk.fetcher.urllib.request.HTTPSHandler.https_open",
            side_effect=response_for_request,
        ):
            result = fetch_snapshot(DEFINITION, opener=urllib.request.urlopen)
        self.assertEqual(result.canonical_text, "Approved text")
        self.assertEqual(contacted, [DEFINITION.url, "https://official.example.test:443/laws/demo.xml"])

    def test_redirect_target_is_revalidated(self) -> None:
        def opener(request, timeout):
            return FakeResponse(b"<law><s>safe</s></law>", "https://attacker.example/laws/demo.xml")

        with self.assertRaisesRegex(ValueError, "host"):
            fetch_snapshot(DEFINITION, opener=opener)

    def test_size_cap_is_enforced(self) -> None:
        def opener(request, timeout):
            return FakeResponse(b"<law>too large</law>")

        with self.assertRaisesRegex(ValueError, "byte cap"):
            fetch_snapshot(DEFINITION, opener=opener, max_bytes=5)

    def test_xml_is_canonicalized(self) -> None:
        def opener(request, timeout):
            return FakeResponse(b"<law><section>  Demo  text </section></law>")

        result = fetch_snapshot(DEFINITION, opener=opener)
        self.assertEqual(result.canonical_text, "Demo text")


if __name__ == "__main__":
    unittest.main()
