import csv
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import tenv3_fetch


class FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload
        self._offset = 0

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


def http_error(code: int) -> HTTPError:
    return HTTPError(
        url="https://example.test/resource",
        code=code,
        msg=f"HTTP {code}",
        hdrs=None,
        fp=None,
    )


class StationsFileTests(unittest.TestCase):
    def test_read_stations_file_skips_comments_blanks_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as td:
            stations_path = Path(td) / "stations.txt"
            stations_path.write_text(
                "\n"
                "# comment\n"
                " 0bug \n"
                "1abc\n"
                "0BUG\n"
                "  \n",
                encoding="utf-8",
            )
            logs = []
            stations = tenv3_fetch.read_stations_file(stations_path, status_callback=logs.append)

        self.assertEqual(["0BUG", "1ABC"], stations)
        self.assertTrue(any("[SKIP] Powtórzona stacja '0BUG'" in line for line in logs))

    def test_station_url_and_paths(self):
        results = Path("/tmp/results")
        self.assertEqual(
            "https://host/base/0BUG.tenv3",
            tenv3_fetch.station_url("https://host/base", "0BUG"),
        )
        self.assertEqual(
            (results / "0BUG" / "0BUG.tenv3", results / "0BUG" / "0BUG.csv"),
            tenv3_fetch.station_paths(results, "0BUG"),
        )


class RetryAndDownloadTests(unittest.TestCase):
    def test_urlopen_with_503_retry_then_success(self):
        logs = []
        sentinel = object()
        with patch.object(tenv3_fetch, "urlopen", side_effect=[http_error(503), sentinel]) as mock_open, patch.object(
            tenv3_fetch.time, "sleep"
        ) as mock_sleep:
            got = tenv3_fetch.urlopen_with_503_retry(
                url="https://example.test/x",
                timeout=10,
                retries_503=2,
                retry_delay_seconds=2.0,
                status_callback=logs.append,
            )

        self.assertIs(got, sentinel)
        self.assertEqual(2, mock_open.call_count)
        mock_sleep.assert_called_once_with(2.0)
        self.assertTrue(any("próba 1/2" in line for line in logs))

    def test_urlopen_with_503_retry_does_not_retry_other_errors(self):
        with patch.object(tenv3_fetch, "urlopen", side_effect=http_error(404)) as mock_open, patch.object(
            tenv3_fetch.time, "sleep"
        ) as mock_sleep:
            with self.assertRaises(HTTPError):
                tenv3_fetch.urlopen_with_503_retry(
                    url="https://example.test/x",
                    timeout=10,
                    retries_503=2,
                    retry_delay_seconds=1.0,
                )

        self.assertEqual(1, mock_open.call_count)
        mock_sleep.assert_not_called()

    def test_write_tenv3_file_rewrites_empty_partial(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "0BUG.tenv3"
            target.write_bytes(b"")

            with patch.object(
                tenv3_fetch,
                "urlopen_with_503_retry",
                return_value=FakeResponse(b"site a b\n0BUG x y\n"),
            ):
                tenv3_fetch.write_tenv3_file(
                    source_url="https://example.test/0BUG.tenv3",
                    destination=target,
                    timeout=10,
                    retries_503=1,
                    retry_delay_seconds=0.0,
                )

            self.assertTrue(target.exists())
            self.assertEqual(b"site a b\n0BUG x y\n", target.read_bytes())


class CsvExportTests(unittest.TestCase):
    def test_export_tenv3_to_csv_parses_and_skips_malformed_rows(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "0BUG.tenv3"
            out = Path(td) / "0BUG.csv"
            src.write_text(
                "site YYMMMDD yyyy.yyyy\n"
                "0BUG 18FEB28 2018.1602\n"
                "0BUG only_two\n"
                "0BUG 18MAR01 2018.1629\n",
                encoding="utf-8",
            )
            logs = []
            malformed, rows_written = tenv3_fetch.export_tenv3_to_csv(
                source_path=src,
                csv_path=out,
                station="0BUG",
                status_callback=logs.append,
            )

            with out.open("r", encoding="utf-8", newline="") as csv_file:
                rows = list(csv.reader(csv_file))

        self.assertEqual(1, malformed)
        self.assertEqual(2, rows_written)
        self.assertEqual(["site", "YYMMMDD", "yyyy.yyyy"], rows[0])
        self.assertEqual(["0BUG", "18FEB28", "2018.1602"], rows[1])
        self.assertEqual(["0BUG", "18MAR01", "2018.1629"], rows[2])
        self.assertTrue(any("pomijam wiersz 3" in line for line in logs))


class MainFlowTests(unittest.TestCase):
    def make_args(self, stations_file: Path, results_dir: Path) -> Namespace:
        return Namespace(
            stations_file=str(stations_file),
            base_url="https://example.test/base/",
            results_dir=str(results_dir),
            timeout=10,
            retry_503=1,
            retry_delay=0.0,
        )

    def test_main_success_when_one_skipped_and_one_downloaded(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            stations_file = td_path / "stations.txt"
            stations_file.write_text("0BUG\n1ABC\n", encoding="utf-8")
            results_dir = td_path / "results"
            results_dir.mkdir(parents=True, exist_ok=True)
            # 0BUG already complete -> should skip.
            station0_dir = results_dir / "0BUG"
            station0_dir.mkdir(parents=True, exist_ok=True)
            (station0_dir / "0BUG.tenv3").write_text("site a b\n0BUG x y\n", encoding="utf-8")
            (station0_dir / "0BUG.csv").write_text("site,a,b\n0BUG,x,y\n", encoding="utf-8")

            def fake_urlopen_with_retry(url, **kwargs):
                if url.endswith("/1ABC.tenv3"):
                    return FakeResponse(b"site YYMMMDD yyyy.yyyy\n1ABC 18MAR01 2018.1629\n")
                raise AssertionError(f"Unexpected URL: {url}")

            with patch.object(tenv3_fetch, "parse_args", return_value=self.make_args(stations_file, results_dir)), patch.object(
                tenv3_fetch, "urlopen_with_503_retry", side_effect=fake_urlopen_with_retry
            ):
                rc = tenv3_fetch.main()

            self.assertEqual(0, rc)
            self.assertTrue((results_dir / "1ABC" / "1ABC.tenv3").exists())
            self.assertTrue((results_dir / "1ABC" / "1ABC.csv").exists())

    def test_main_returns_2_when_some_station_fails(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            stations_file = td_path / "stations.txt"
            stations_file.write_text("0BUG\n1ABC\n", encoding="utf-8")
            results_dir = td_path / "results"

            def fake_urlopen_with_retry(url, **kwargs):
                if url.endswith("/0BUG.tenv3"):
                    return FakeResponse(b"site YYMMMDD yyyy.yyyy\n0BUG 18MAR01 2018.1629\n")
                raise HTTPError(url=url, code=404, msg="Not Found", hdrs=None, fp=None)

            with patch.object(tenv3_fetch, "parse_args", return_value=self.make_args(stations_file, results_dir)), patch.object(
                tenv3_fetch, "urlopen_with_503_retry", side_effect=fake_urlopen_with_retry
            ):
                rc = tenv3_fetch.main()

            self.assertEqual(2, rc)
            self.assertTrue((results_dir / "0BUG" / "0BUG.tenv3").exists())
            self.assertTrue((results_dir / "0BUG" / "0BUG.csv").exists())
            self.assertFalse((results_dir / "1ABC" / "1ABC.tenv3").exists())


if __name__ == "__main__":
    unittest.main()
