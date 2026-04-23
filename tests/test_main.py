import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch
import signal
from urllib.error import HTTPError, URLError

import main


class FakeHeaders(dict):
    def __init__(self, charset: str = "utf-8", **kwargs):
        super().__init__(**kwargs)
        self._charset = charset

    def get_content_charset(self):
        return self._charset


class FakeResponse:
    def __init__(self, payload: bytes, headers: FakeHeaders | None = None):
        self._payload = payload
        self._offset = 0
        self.headers = headers or FakeHeaders()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            data = self._payload[self._offset :]
            self._offset = len(self._payload)
            return data

        data = self._payload[self._offset : self._offset + size]
        self._offset += len(data)
        return data


class BrokenResponse(FakeResponse):
    def __init__(self, payload: bytes, fail_after: int, headers: FakeHeaders | None = None):
        super().__init__(payload, headers=headers)
        self._fail_after = fail_after

    def read(self, size: int = -1) -> bytes:
        if self._offset >= self._fail_after:
            raise RuntimeError("simulated stream failure")
        allowed = self._fail_after - self._offset
        if size < 0:
            size = allowed
        else:
            size = min(size, allowed)
        return super().read(size)


class DummyTqdm:
    writes: list[str] = []

    def __init__(self, total=0, desc="", **kwargs):
        self.total = total
        self.desc = desc
        self.n = 0
        self.closed = False
        self.postfix = {}

    @staticmethod
    def write(message: str):
        DummyTqdm.writes.append(message)

    def set_postfix(self, **kwargs):
        self.postfix = kwargs

    def update(self, n=1):
        self.n += n

    def close(self):
        self.closed = True

    def reset(self, total=None):
        if total is not None:
            self.total = total
        self.n = 0

    def set_description_str(self, desc: str):
        self.desc = desc


def http_error(code: int) -> HTTPError:
    return HTTPError(
        url="https://example.test/resource",
        code=code,
        msg=f"HTTP {code}",
        hdrs=None,
        fp=None,
    )


class FilteringTests(unittest.TestCase):
    def test_iter_package_links_filters_and_sorts(self):
        hrefs = [
            "20210101_20210107/",
            "20201231_20210107/",
            "foo/",
            "20210111_20210113",
            "20210101_20210107",  # duplikat
        ]
        got = main.iter_package_links("https://host/interferograms/", hrefs, year=2021)
        self.assertEqual(
            [
                ("20210101_20210107", "https://host/interferograms/20210101_20210107"),
                ("20210111_20210113", "https://host/interferograms/20210111_20210113"),
            ],
            got,
        )

    def test_iter_target_files_keeps_only_supported_extensions(self):
        hrefs = [
            "20210101_20210107.geo.cc.tif",
            "20210101_20210107.geo.unw.tiff",
            "20210101_20210107.geo.cc.jpg",
            "README.txt",
            "subdir/",
        ]
        got = main.iter_target_files(hrefs)
        self.assertEqual(
            [
                ("20210101_20210107.geo.cc.tif", "20210101_20210107.geo.cc.tif"),
                ("20210101_20210107.geo.unw.tiff", "20210101_20210107.geo.unw.tiff"),
            ],
            got,
        )

    def test_candidate_download_urls_has_no_fallback(self):
        url = "https://host/public/nceo_geohazards/LiCSAR_products/x/y/file.geo.cc.tif"
        self.assertEqual([url], main.candidate_download_urls(url))


class RetryAndParsingTests(unittest.TestCase):
    def test_urlopen_with_503_retry_then_success(self):
        status = []
        sentinel = object()
        with patch.object(main, "urlopen", side_effect=[http_error(503), sentinel]) as mock_open, patch.object(
            main.time, "sleep"
        ) as mock_sleep:
            got = main.urlopen_with_503_retry(
                url="https://example.test/list",
                timeout=10,
                retries_503=3,
                retry_delay_seconds=2.0,
                status_callback=status.append,
            )
        self.assertIs(got, sentinel)
        self.assertEqual(mock_open.call_count, 2)
        mock_sleep.assert_called_once_with(2.0)
        self.assertEqual(1, len(status))
        self.assertIn("próba 1/3", status[0])

    def test_urlopen_with_503_retry_exhausted(self):
        status = []
        with patch.object(main, "urlopen", side_effect=[http_error(503), http_error(503)]), patch.object(
            main.time, "sleep"
        ) as mock_sleep:
            with self.assertRaises(HTTPError):
                main.urlopen_with_503_retry(
                    url="https://example.test/list",
                    timeout=10,
                    retries_503=1,
                    retry_delay_seconds=1.0,
                    status_callback=status.append,
                )
        mock_sleep.assert_called_once_with(1.0)

    def test_urlopen_with_503_retry_does_not_retry_non_503(self):
        with patch.object(main, "urlopen", side_effect=http_error(404)) as mock_open, patch.object(
            main.time, "sleep"
        ) as mock_sleep:
            with self.assertRaises(HTTPError):
                main.urlopen_with_503_retry(
                    url="https://example.test/list",
                    timeout=10,
                    retries_503=4,
                    retry_delay_seconds=1.0,
                )
        self.assertEqual(1, mock_open.call_count)
        mock_sleep.assert_not_called()

    def test_fetch_links_parses_plain_html_href(self):
        html = b"""
        <html><body>
            <a href="a/">A</a>
            <a href="/b">B</a>
            <a>NOHREF</a>
        </body></html>
        """
        with patch.object(main, "urlopen_with_503_retry", return_value=FakeResponse(html)):
            got = main.fetch_links(
                url="https://example.test/list",
                timeout=10,
                retries_503=2,
                retry_delay_seconds=0.5,
            )
        self.assertEqual(["a/", "/b"], got)


class DownloadTests(unittest.TestCase):
    def test_download_file_skips_existing_non_empty(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "x.bin"
            target.write_bytes(b"abc")
            status = []
            got = main.download_file(
                file_url="https://example.test/file",
                destination=target,
                timeout=10,
                retries_503=1,
                retry_delay_seconds=0.0,
                status_callback=status.append,
            )
        self.assertFalse(got)
        self.assertEqual(["[SKIP] x.bin"], status)

    def test_download_file_from_url_writes_bytes_and_status(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "out.bin"
            headers = FakeHeaders()
            headers["Content-Length"] = "6"
            status = []
            with patch.object(main, "urlopen_with_503_retry", return_value=FakeResponse(b"abcdef", headers=headers)), patch.object(
                main, "PROGRESS_LOG_EVERY_SECONDS", 0.0
            ):
                main.download_file_from_url(
                    file_url="https://example.test/file",
                    destination=target,
                    timeout=10,
                    retries_503=1,
                    retry_delay_seconds=0.0,
                    status_callback=status.append,
                )
            self.assertEqual(b"abcdef", target.read_bytes())
        self.assertTrue(any(s.startswith("[START]") for s in status))
        self.assertTrue(any(s.startswith("[PROGRESS]") for s in status))
        self.assertTrue(any(s.startswith("[OK]") for s in status))

    def test_download_file_from_url_removes_partial_file_on_error(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "partial.bin"
            broken = BrokenResponse(b"abcdef", fail_after=3, headers=FakeHeaders(Content_Length="6"))
            with patch.object(main, "urlopen_with_503_retry", return_value=broken):
                with self.assertRaises(RuntimeError):
                    main.download_file_from_url(
                        file_url="https://example.test/file",
                        destination=target,
                        timeout=10,
                        retries_503=1,
                        retry_delay_seconds=0.0,
                        status_callback=lambda _: None,
                    )
            self.assertFalse(target.exists())

    def test_collect_missing_jobs_only_returns_missing_or_empty(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            ok = td_path / "ok.bin"
            ok.write_bytes(b"x")
            empty = td_path / "empty.bin"
            empty.write_bytes(b"")
            missing = td_path / "missing.bin"
            jobs = [
                ("u1", "ok.bin", ok),
                ("u2", "empty.bin", empty),
                ("u3", "missing.bin", missing),
            ]
            got = main.collect_missing_jobs(jobs)
        self.assertEqual(
            [
                ("u2", "empty.bin", empty),
                ("u3", "missing.bin", missing),
            ],
            got,
        )


class MainFlowTests(unittest.TestCase):
    def setUp(self):
        DummyTqdm.writes = []

    def make_args(self, out_dir: Path, **kwargs) -> Namespace:
        data = {
            "base_url": "https://gws-access.jasmin.ac.uk/public/nceo_geohazards/LiCSAR_products/",
            "mission": ["/137/137A_05266_171717/interferograms"],
            "results_dir": str(out_dir),
            "year": 2021,
            "timeout": 10,
            "retry_503": 2,
            "retry_delay": 0.0,
            "verify_rounds": 1,
        }
        data.update(kwargs)
        return Namespace(**data)

    def test_main_successful_download(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "results"
            args = self.make_args(out_dir)
            logs = []

            def fake_fetch_links(url, **kwargs):
                if url.endswith("/interferograms/"):
                    return ["20210101_20210107/"]
                if url.endswith("/20210101_20210107/"):
                    return ["20210101_20210107.geo.cc.tif"]
                return []

            def fake_download_file(file_url, destination, **kwargs):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"data")
                return True

            with patch.object(main, "parse_args", return_value=args), patch.object(
                main, "fetch_links", side_effect=fake_fetch_links
            ), patch.object(main, "download_file", side_effect=fake_download_file), patch.object(
                main, "log_line", side_effect=logs.append
            ), patch.object(
                main, "tqdm", DummyTqdm
            ):
                rc = main.main()

            expected = (
                out_dir
                / "137_137A_05266_171717"
                / "20210101_20210107"
                / "20210101_20210107.geo.cc.tif"
            )
            self.assertEqual(0, rc)
            self.assertTrue(expected.exists())
            self.assertTrue(any("kompletna" in line for line in logs))

    def test_main_returns_error_when_file_still_missing(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "results"
            args = self.make_args(out_dir, verify_rounds=1)
            logs = []

            def fake_fetch_links(url, **kwargs):
                if url.endswith("/interferograms/"):
                    return ["20210101_20210107/"]
                if url.endswith("/20210101_20210107/"):
                    return ["20210101_20210107.geo.cc.tif"]
                return []

            def fake_download_file(file_url, destination, **kwargs):
                return False

            with patch.object(main, "parse_args", return_value=args), patch.object(
                main, "fetch_links", side_effect=fake_fetch_links
            ), patch.object(main, "download_file", side_effect=fake_download_file), patch.object(
                main, "log_line", side_effect=logs.append
            ), patch.object(
                main, "tqdm", DummyTqdm
            ):
                rc = main.main()

            self.assertEqual(2, rc)
            self.assertTrue(any("nadal brakuje 1 plików" in line for line in logs))

    def test_main_verify_round_can_recover_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "results"
            args = self.make_args(out_dir, verify_rounds=2)
            logs = []
            calls = {"count": 0}

            def fake_fetch_links(url, **kwargs):
                if url.endswith("/interferograms/"):
                    return ["20210101_20210107/"]
                if url.endswith("/20210101_20210107/"):
                    return ["20210101_20210107.geo.cc.tif"]
                return []

            def fake_download_file(file_url, destination, **kwargs):
                calls["count"] += 1
                if calls["count"] == 1:
                    return False
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"fixed")
                return True

            with patch.object(main, "parse_args", return_value=args), patch.object(
                main, "fetch_links", side_effect=fake_fetch_links
            ), patch.object(main, "download_file", side_effect=fake_download_file), patch.object(
                main, "log_line", side_effect=logs.append
            ), patch.object(
                main, "tqdm", DummyTqdm
            ):
                rc = main.main()

            self.assertEqual(0, rc)
            self.assertGreaterEqual(calls["count"], 2)
            self.assertTrue(any("kompletna" in line for line in logs))

    def test_main_returns_error_on_listing_failure(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "results"
            args = self.make_args(out_dir)
            logs = []

            with patch.object(main, "parse_args", return_value=args), patch.object(
                main, "fetch_links", side_effect=URLError("boom")
            ), patch.object(main, "log_line", side_effect=logs.append), patch.object(
                main, "tqdm", DummyTqdm
            ):
                rc = main.main()

            self.assertEqual(2, rc)
            self.assertTrue(any("Nie udało się pobrać listy pakietów" in line for line in logs))

    def test_main_graceful_stop_finishes_current_file_and_stops_next(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "results"
            args = self.make_args(out_dir, verify_rounds=1)
            logs = []
            registered_handlers = {}
            call_count = {"n": 0}

            def fake_fetch_links(url, **kwargs):
                if url.endswith("/interferograms/"):
                    return ["20210101_20210107/"]
                if url.endswith("/20210101_20210107/"):
                    return [
                        "20210101_20210107.geo.cc.tif",
                        "20210101_20210107.geo.unw.tif",
                    ]
                return []

            def fake_signal(sig, handler):
                registered_handlers[sig] = handler
                return None

            def fake_download_file(file_url, destination, **kwargs):
                call_count["n"] += 1
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"ok")
                if call_count["n"] == 1:
                    handler = registered_handlers.get(signal.SIGINT)
                    self.assertIsNotNone(handler)
                    handler(signal.SIGINT, None)
                return True

            with patch.object(main, "parse_args", return_value=args), patch.object(
                main, "fetch_links", side_effect=fake_fetch_links
            ), patch.object(main, "download_file", side_effect=fake_download_file), patch.object(
                main, "log_line", side_effect=logs.append
            ), patch.object(
                main, "tqdm", DummyTqdm
            ), patch.object(
                main.signal, "signal", side_effect=fake_signal
            ), patch.object(
                main.signal, "getsignal", return_value=signal.default_int_handler
            ):
                rc = main.main()

            self.assertEqual(130, rc)
            self.assertEqual(1, call_count["n"])
            self.assertTrue(any("Zatrzymanie użytkownika" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
