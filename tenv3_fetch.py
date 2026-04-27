from __future__ import annotations

import argparse
import csv
import os
import time
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import urlopen

DEFAULT_BASE_URL = "https://geodesy.unr.edu/gps_timeseries/IGS20/tenv3/IGS20/"
DEFAULT_RETRIES_503 = 5
DEFAULT_RETRY_DELAY_SECONDS = 2.0
CHUNK_SIZE = 1024 * 1024
StatusCallback = Callable[[str], None]


def log_line(message: str) -> None:
    print(message)


def normalize_url(url: str) -> str:
    return url if url.endswith("/") else f"{url}/"


def is_non_empty_file(path: Path) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > 0


def cleanup_empty_file(path: Path) -> None:
    if path.exists() and path.is_file() and path.stat().st_size == 0:
        path.unlink()


def urlopen_with_503_retry(
    url: str,
    timeout: int,
    retries_503: int,
    retry_delay_seconds: float,
    status_callback: StatusCallback | None = None,
):
    retries_done = 0
    while True:
        try:
            return urlopen(url, timeout=timeout)
        except HTTPError as exc:
            if exc.code != 503 or retries_done >= retries_503:
                raise
            retries_done += 1
            delay = retry_delay_seconds * (2 ** (retries_done - 1))
            if status_callback is not None:
                status_callback(
                    f"[RETRY] 503 | próba {retries_done}/{retries_503} | czekam {delay:.1f}s"
                )
            time.sleep(delay)


def read_stations_file(
    stations_file: Path,
    status_callback: StatusCallback | None = None,
) -> list[str]:
    stations: list[str] = []
    seen: set[str] = set()

    with stations_file.open("r", encoding="utf-8") as source:
        for line_number, raw_line in enumerate(source, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            station = line.upper()
            if station in seen:
                if status_callback is not None:
                    status_callback(
                        f"[SKIP] Powtórzona stacja '{station}' "
                        f"(linia {line_number} w pliku listy)."
                    )
                continue

            seen.add(station)
            stations.append(station)

    return stations


def station_url(base_url: str, station: str) -> str:
    return urljoin(normalize_url(base_url), f"{station}.tenv3")


def station_paths(results_dir: Path, station: str) -> tuple[Path, Path]:
    station_dir = results_dir / station
    return (station_dir / f"{station}.tenv3", station_dir / f"{station}.csv")


def write_tenv3_file(
    source_url: str,
    destination: Path,
    timeout: int,
    retries_503: int,
    retry_delay_seconds: float,
    status_callback: StatusCallback | None = None,
) -> None:
    cleanup_empty_file(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if status_callback is not None:
        status_callback(f"[START] Pobieranie {destination.name}")

    try:
        with urlopen_with_503_retry(
            url=source_url,
            timeout=timeout,
            retries_503=retries_503,
            retry_delay_seconds=retry_delay_seconds,
            status_callback=status_callback,
        ) as response, destination.open("wb") as output:
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                output.write(chunk)
    except Exception:
        if destination.exists():
            destination.unlink()
        raise

    if status_callback is not None:
        status_callback(f"[OK] Pobrano {destination.name}")


def export_tenv3_to_csv(
    source_path: Path,
    csv_path: Path,
    station: str,
    status_callback: StatusCallback | None = None,
) -> tuple[int, int]:
    cleanup_empty_file(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    malformed_rows = 0
    rows_written = 0

    try:
        with source_path.open("r", encoding="utf-8", errors="replace") as source:
            lines = source.readlines()

        header: list[str] | None = None
        data_lines: list[tuple[int, str]] = []
        for index, raw_line in enumerate(lines, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            if header is None:
                header = stripped.split()
                continue
            data_lines.append((index, stripped))

        if header is None:
            raise ValueError(f"Plik {source_path.name} nie zawiera nagłówka.")

        with csv_path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(header)

            expected_columns = len(header)
            for line_number, stripped in data_lines:
                columns = stripped.split()
                if len(columns) != expected_columns:
                    malformed_rows += 1
                    if status_callback is not None:
                        status_callback(
                            f"[WARN] {station}: pomijam wiersz {line_number}, "
                            f"liczba kolumn {len(columns)} != {expected_columns}"
                        )
                    continue

                writer.writerow(columns)
                rows_written += 1
    except Exception:
        if csv_path.exists():
            csv_path.unlink()
        raise

    if status_callback is not None:
        status_callback(
            f"[OK] Zapisano {csv_path.name} | wiersze: {rows_written} | "
            f"pominięte: {malformed_rows}"
        )
    return malformed_rows, rows_written


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pobiera pliki .tenv3 dla listy stacji i zapisuje także CSV."
    )
    parser.add_argument(
        "--stations-file",
        required=True,
        help="Ścieżka do pliku txt z nazwami stacji (jedna stacja na linię).",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help="Bazowy URL do endpointów .tenv3",
    )
    parser.add_argument(
        "--results-dir",
        "-o",
        default=os.environ.get("TENV3_RESULTS_DIR", "results"),
        help="Katalog docelowy na pliki .tenv3 i .csv",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Timeout żądań HTTP w sekundach (domyślnie: 30)",
    )
    parser.add_argument(
        "--retry-503",
        type=int,
        default=DEFAULT_RETRIES_503,
        help=f"Liczba ponowień dla HTTP 503 (domyślnie: {DEFAULT_RETRIES_503})",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=DEFAULT_RETRY_DELAY_SECONDS,
        help=(
            "Bazowy delay (sekundy) dla retry 503; używany jest exponential backoff "
            f"(domyślnie: {DEFAULT_RETRY_DELAY_SECONDS})"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    stations_file = Path(args.stations_file).resolve()
    results_dir = Path(args.results_dir).resolve()
    results_dir.mkdir(parents=True, exist_ok=True)

    retries_503 = max(0, args.retry_503)
    retry_delay_seconds = max(0.0, args.retry_delay)
    timeout = max(1, args.timeout)
    base_url = normalize_url(args.base_url)

    if not stations_file.exists():
        log_line(f"[ERROR] Nie znaleziono pliku listy stacji: {stations_file}")
        return 2

    try:
        stations = read_stations_file(stations_file, status_callback=log_line)
    except Exception as exc:
        log_line(f"[ERROR] Nie udało się odczytać listy stacji: {exc}")
        return 2

    processed = len(stations)
    downloaded = 0
    skipped = 0
    failed = 0
    malformed_rows_total = 0
    incomplete = 0

    log_line(f"[INFO] Plik listy: {stations_file}")
    log_line(f"[INFO] Liczba unikalnych stacji do przetworzenia: {processed}")

    for station in stations:
        tenv3_path, csv_path = station_paths(results_dir, station)
        cleanup_empty_file(tenv3_path)
        cleanup_empty_file(csv_path)

        if is_non_empty_file(tenv3_path) and is_non_empty_file(csv_path):
            skipped += 1
            log_line(f"[SKIP] {station}: istnieją {tenv3_path.name} i {csv_path.name}")
            continue

        tenv3_was_downloaded = False
        if not is_non_empty_file(tenv3_path):
            url = station_url(base_url, station)
            try:
                write_tenv3_file(
                    source_url=url,
                    destination=tenv3_path,
                    timeout=timeout,
                    retries_503=retries_503,
                    retry_delay_seconds=retry_delay_seconds,
                    status_callback=log_line,
                )
                downloaded += 1
                tenv3_was_downloaded = True
            except Exception as exc:
                failed += 1
                incomplete += 1
                log_line(f"[WARN] {station}: nie udało się pobrać {url}: {exc}")
                continue

        try:
            malformed_rows, _ = export_tenv3_to_csv(
                source_path=tenv3_path,
                csv_path=csv_path,
                station=station,
                status_callback=log_line,
            )
            malformed_rows_total += malformed_rows
        except Exception as exc:
            failed += 1
            incomplete += 1
            log_line(f"[WARN] {station}: nie udało się wygenerować CSV: {exc}")
            continue

        if not is_non_empty_file(tenv3_path) or not is_non_empty_file(csv_path):
            failed += 1
            incomplete += 1
            log_line(f"[ERROR] {station}: wynik niekompletny po przetworzeniu.")
            continue

        if not tenv3_was_downloaded:
            log_line(f"[INFO] {station}: użyto lokalnego pliku {tenv3_path.name}, CSV odświeżone.")

    log_line(
        f"[DONE] Stacje: {processed}, pobrane: {downloaded}, "
        f"pominięte: {skipped}, nieudane: {failed}, "
        f"malformed rows: {malformed_rows_total}, niekompletne: {incomplete}"
    )
    log_line(f"[DONE] Folder wynikowy: {results_dir}")

    if failed > 0 or incomplete > 0:
        log_line("[ERROR] Pobieranie zakończone niekompletnie.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
