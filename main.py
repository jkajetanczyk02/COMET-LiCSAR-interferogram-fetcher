from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
from collections import deque
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable
from tqdm import tqdm
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

DEFAULT_BASE_URL = (
    "https://gws-access.jasmin.ac.uk/public/nceo_geohazards/"
    "LiCSAR_products/"
)
DEFAULT_MISSIONS = ("/137/137A_05266_171717/interferograms",)
PACKAGE_PATTERN = re.compile(r"^(\d{8})_(\d{8})$")
# W listingach LiCSAR spotyka się zarówno .tif, jak i .tiff.
ALLOWED_SUFFIXES = (
    ".geo.cc.tiff",
    ".geo.unw.tiff",
    ".geo.cc.tif",
    ".geo.unw.tif",
)
PROGRESS_LOG_EVERY_SECONDS = 1.0
CHUNK_SIZE = 1024 * 1024
DEFAULT_RETRIES_503 = 5
DEFAULT_RETRY_DELAY_SECONDS = 2.0
DEFAULT_VERIFY_ROUNDS = 2
StatusCallback = Callable[[str], None]
FileProgressCallback = Callable[[str, str, int, int | None, float, float | None], None]
STATUS_ORDER = (
    "INFO",
    "START",
    "PROGRESS",
    "OK",
    "SKIP",
    "RETRY",
    "VERIFY",
    "WARN",
    "ERROR",
    "DONE",
)
STATUS_LOG_LINES = 5


def format_bytes(num_bytes: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(num_bytes)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{num_bytes} B"


def format_eta(seconds: float) -> str:
    if seconds < 0:
        return "?:??"
    total_seconds = int(seconds)
    hours, rem = divmod(total_seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


ACTIVE_DASHBOARD = None


def log_line(message: str) -> None:
    if ACTIVE_DASHBOARD is not None:
        ACTIVE_DASHBOARD.add_log(message)
        return
    tqdm.write(message)


def safe_status_text(message: str, max_len: int | None = None) -> str:
    if max_len is None:
        cols = shutil.get_terminal_size((120, 20)).columns
        max_len = max(30, cols - 4)
    text = " ".join(message.splitlines()).strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


class TerminalDashboard:
    def __init__(
        self,
        status_order: tuple[str, ...] = STATUS_ORDER,
        lines_per_status: int = STATUS_LOG_LINES,
    ) -> None:
        self.status_order = status_order
        self.lines_per_status = lines_per_status
        self.status_buffers = {status: deque(maxlen=lines_per_status) for status in status_order}
        self.widgets: list = []
        self._build_layout()

    def _build_layout(self) -> None:
        position = 0
        self.overall_bar = tqdm(
            total=1,
            desc="Cały proces",
            unit="plik",
            dynamic_ncols=True,
            leave=True,
            position=position,
        )
        self.widgets.append(self.overall_bar)
        position += 1

        self.file_bar = tqdm(
            total=1,
            desc="Plik",
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            dynamic_ncols=True,
            leave=True,
            position=position,
        )
        self.widgets.append(self.file_bar)
        position += 1

        self.status_headers: dict[str, object] = {}
        self.status_lines: dict[str, list[object]] = {}
        self.status_separators: dict[str, object] = {}

        for status in self.status_order:
            header = tqdm(
                total=1,
                desc="",
                bar_format="{desc}",
                dynamic_ncols=True,
                leave=True,
                position=position,
            )
            self.widgets.append(header)
            self.status_headers[status] = header
            position += 1

            lines = []
            for _ in range(self.lines_per_status):
                line = tqdm(
                    total=1,
                    desc="",
                    bar_format="{desc}",
                    dynamic_ncols=True,
                    leave=True,
                    position=position,
                )
                self.widgets.append(line)
                lines.append(line)
                position += 1
            self.status_lines[status] = lines

            separator = tqdm(
                total=1,
                desc="",
                bar_format="{desc}",
                dynamic_ncols=True,
                leave=True,
                position=position,
            )
            self.widgets.append(separator)
            self.status_separators[status] = separator
            position += 1

        self.refresh_all_statuses()

    def _set_widget_text(self, widget, text: str) -> None:
        widget.set_description_str(safe_status_text(text))

    def _status_from_message(self, message: str) -> str:
        stripped = message.strip()
        if stripped.startswith("[") and "]" in stripped:
            marker = stripped[1 : stripped.find("]")]
            marker = marker.strip().upper()
            if marker in self.status_buffers:
                return marker
        return "INFO"

    def refresh_status(self, status: str) -> None:
        buffer = self.status_buffers[status]
        self._set_widget_text(
            self.status_headers[status],
            f"[{status}] ostatnie {len(buffer)}/{self.lines_per_status}",
        )
        newest_first = list(reversed(buffer))
        for idx, widget in enumerate(self.status_lines[status]):
            text = newest_first[idx] if idx < len(newest_first) else ""
            self._set_widget_text(widget, text)
        self._set_widget_text(self.status_separators[status], "-" * 24)

    def refresh_all_statuses(self) -> None:
        for status in self.status_order:
            self.refresh_status(status)

    def add_log(self, message: str) -> None:
        status = self._status_from_message(message)
        self.status_buffers[status].append(message)
        self.refresh_status(status)

    def update_overall(
        self,
        *,
        total: int,
        completed: int,
        ok: int,
        skip: int,
        fail: int,
        missing: int,
        listing_errors: int,
    ) -> None:
        target_total = max(total, 1)
        if self.overall_bar.total != target_total:
            self.overall_bar.reset(total=target_total)
            self.overall_bar.n = 0
        if completed < self.overall_bar.n:
            self.overall_bar.reset(total=target_total)
            self.overall_bar.n = 0
        delta = completed - self.overall_bar.n
        if delta > 0:
            self.overall_bar.update(delta)

        self.overall_bar.set_description_str("Cały proces")
        self.overall_bar.set_postfix(
            ok=ok,
            skip=skip,
            fail=fail,
            missing=missing,
            listing=listing_errors,
        )

    def on_file_event(
        self,
        stage: str,
        file_name: str,
        downloaded: int,
        total_bytes: int | None,
        speed_bps: float,
        eta_seconds: float | None,
    ) -> None:
        if stage == "skip":
            self.file_bar.reset(total=1)
            self.file_bar.n = 1
            self.file_bar.set_description_str(safe_status_text(f"Plik: {file_name}"))
            self.file_bar.set_postfix(done="SKIP", speed="-", eta="-")
            return

        if stage == "start":
            target_total = max(total_bytes or 1, 1)
            self.file_bar.reset(total=target_total)
            self.file_bar.n = 0
            self.file_bar.set_description_str(safe_status_text(f"Plik: {file_name}"))
            if total_bytes:
                self.file_bar.set_postfix(
                    done=f"0/{format_bytes(total_bytes)}",
                    speed="0 B/s",
                    eta="--:--",
                )
            else:
                self.file_bar.set_postfix(done="0 B", speed="0 B/s", eta="--:--")
            return

        target_total = max(total_bytes or downloaded or 1, 1)
        if self.file_bar.total != target_total:
            self.file_bar.total = target_total
        if downloaded < self.file_bar.n:
            self.file_bar.reset(total=target_total)
            self.file_bar.n = 0
        delta = downloaded - self.file_bar.n
        if delta > 0:
            self.file_bar.update(delta)

        if total_bytes:
            done_text = f"{format_bytes(downloaded)}/{format_bytes(total_bytes)}"
        else:
            done_text = format_bytes(downloaded)

        self.file_bar.set_description_str(safe_status_text(f"Plik: {file_name}"))
        self.file_bar.set_postfix(
            done=done_text,
            speed=f"{format_bytes(int(max(speed_bps, 0.0)))}/s",
            eta=format_eta(eta_seconds if eta_seconds is not None else -1),
        )

    def close(self) -> None:
        for widget in reversed(self.widgets):
            widget.close()


def sanitize_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return name or "mission"


def normalize_url(url: str) -> str:
    return url if url.endswith("/") else f"{url}/"


def resolve_mission_url(base_url: str, mission_endpoint: str) -> str:
    endpoint = mission_endpoint.strip()
    if not endpoint:
        raise ValueError("Pusty endpoint misji")

    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return normalize_url(endpoint)

    return normalize_url(urljoin(base_url, endpoint.lstrip("/")))


def mission_folder_name(mission_url: str, mission_endpoint: str) -> str:
    parts = [part for part in urlparse(mission_url).path.split("/") if part]
    if len(parts) >= 3 and parts[-1] == "interferograms":
        return sanitize_name(f"{parts[-3]}_{parts[-2]}")
    if len(parts) >= 2:
        return sanitize_name(parts[-2])
    return sanitize_name(mission_endpoint)


def is_download_complete(path: Path) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > 0


def candidate_download_urls(file_url: str) -> list[str]:
    return [file_url]


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
            message = (
                f"[RETRY] 503 | próba {retries_done}/{retries_503} | "
                f"czekam {delay:.1f}s"
            )
            if status_callback is not None:
                status_callback(message)
            time.sleep(delay)


class HrefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.links.append(value)
                break


def fetch_links(
    url: str,
    timeout: int,
    retries_503: int,
    retry_delay_seconds: float,
    status_callback: StatusCallback | None = None,
) -> list[str]:
    with urlopen_with_503_retry(
        url=url,
        timeout=timeout,
        retries_503=retries_503,
        retry_delay_seconds=retry_delay_seconds,
        status_callback=status_callback,
    ) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        html = response.read().decode(charset, errors="replace")

    parser = HrefParser()
    parser.feed(html)
    return parser.links


def link_name(href: str) -> str:
    path = urlparse(href).path
    name = Path(path).name if path else ""
    return name.strip()


def is_package_for_year(name: str, year: int) -> bool:
    match = PACKAGE_PATTERN.fullmatch(name)
    if not match:
        return False
    prefix = str(year)
    first_date, second_date = match.groups()
    return first_date.startswith(prefix) and second_date.startswith(prefix)


def iter_package_links(base_url: str, hrefs: Iterable[str], year: int) -> list[tuple[str, str]]:
    packages: dict[str, str] = {}
    for href in hrefs:
        name = link_name(href.rstrip("/"))
        if not is_package_for_year(name, year):
            continue
        packages[name] = urljoin(base_url, href)

    return sorted(packages.items(), key=lambda item: item[0])


def iter_target_files(hrefs: Iterable[str]) -> list[tuple[str, str]]:
    files: dict[str, str] = {}
    for href in hrefs:
        name = link_name(href)
        if not name:
            continue
        if not name.endswith(ALLOWED_SUFFIXES):
            continue
        files[name] = href

    return sorted(files.items(), key=lambda item: item[0])


def download_file_from_url(
    file_url: str,
    destination: Path,
    timeout: int,
    retries_503: int,
    retry_delay_seconds: float,
    status_callback: StatusCallback | None = None,
    file_progress_callback: FileProgressCallback | None = None,
) -> None:
    if destination.exists() and destination.stat().st_size == 0:
        destination.unlink()

    destination.parent.mkdir(parents=True, exist_ok=True)
    if status_callback is not None:
        status_callback(f"[START] {destination.name}")

    start_ts = time.monotonic()
    last_log_ts = start_ts
    downloaded_bytes = 0

    try:
        with urlopen_with_503_retry(
            url=file_url,
            timeout=timeout,
            retries_503=retries_503,
            retry_delay_seconds=retry_delay_seconds,
            status_callback=status_callback,
        ) as response, destination.open("wb") as output:
            content_length = response.headers.get("Content-Length")
            total_bytes = int(content_length) if content_length and content_length.isdigit() else None
            if file_progress_callback is not None:
                file_progress_callback("start", destination.name, 0, total_bytes, 0.0, None)

            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break

                output.write(chunk)
                downloaded_bytes += len(chunk)

                now = time.monotonic()
                if (now - last_log_ts) < PROGRESS_LOG_EVERY_SECONDS:
                    continue

                elapsed = max(now - start_ts, 1e-9)
                speed_bps = downloaded_bytes / elapsed
                if total_bytes:
                    percent = (downloaded_bytes / total_bytes) * 100
                    remaining_bytes = max(total_bytes - downloaded_bytes, 0)
                    eta_seconds = remaining_bytes / speed_bps if speed_bps > 0 else -1
                    if file_progress_callback is not None:
                        file_progress_callback(
                            "progress",
                            destination.name,
                            downloaded_bytes,
                            total_bytes,
                            speed_bps,
                            eta_seconds,
                        )
                    if status_callback is not None:
                        status_callback(
                            f"[PROGRESS] {destination.name} | "
                            f"{percent:6.2f}% | "
                            f"{format_bytes(downloaded_bytes)}/{format_bytes(total_bytes)} | "
                            f"{format_bytes(int(speed_bps))}/s | ETA {format_eta(eta_seconds)}"
                        )
                else:
                    if file_progress_callback is not None:
                        file_progress_callback(
                            "progress",
                            destination.name,
                            downloaded_bytes,
                            total_bytes,
                            speed_bps,
                            None,
                        )
                    if status_callback is not None:
                        status_callback(
                            f"[PROGRESS] {destination.name} | "
                            f"{format_bytes(downloaded_bytes)} | "
                            f"{format_bytes(int(speed_bps))}/s"
                        )
                last_log_ts = now
    except Exception:
        if destination.exists():
            destination.unlink()
        raise

    total_elapsed = max(time.monotonic() - start_ts, 1e-9)
    average_speed_bps = downloaded_bytes / total_elapsed
    if file_progress_callback is not None:
        file_progress_callback(
            "done",
            destination.name,
            downloaded_bytes,
            total_bytes,
            average_speed_bps,
            0.0,
        )
    if status_callback is not None:
        status_callback(
            f"[OK] {destination.name} | "
            f"{format_bytes(downloaded_bytes)} | "
            f"{format_bytes(int(average_speed_bps))}/s | "
            f"{format_eta(total_elapsed)}"
        )


def download_file(
    file_url: str,
    destination: Path,
    timeout: int,
    retries_503: int,
    retry_delay_seconds: float,
    status_callback: StatusCallback | None = None,
    file_progress_callback: FileProgressCallback | None = None,
) -> bool:
    if is_download_complete(destination):
        if file_progress_callback is not None:
            file_progress_callback("skip", destination.name, 0, None, 0.0, None)
        if status_callback is not None:
            status_callback(f"[SKIP] {destination.name}")
        return False

    last_exc: Exception | None = None
    sources = candidate_download_urls(file_url)

    for source_url in sources:
        try:
            download_file_from_url(
                file_url=source_url,
                destination=destination,
                timeout=timeout,
                retries_503=retries_503,
                retry_delay_seconds=retry_delay_seconds,
                status_callback=status_callback,
                file_progress_callback=file_progress_callback,
            )
            return True
        except Exception as exc:
            last_exc = exc
            log_line(f"[WARN] Nieudane źródło {source_url}: {exc}")

    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"Nie udało się pobrać pliku: {file_url}")


def collect_missing_jobs(jobs: list[tuple[str, str, Path]]) -> list[tuple[str, str, Path]]:
    missing: list[tuple[str, str, Path]] = []
    for file_url, file_name, target in jobs:
        if not is_download_complete(target):
            missing.append((file_url, file_name, target))
    return missing


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Pobiera interferogramy z listingu HTML i zapisuje je w results/<misja>/<pakiet>/"
        )
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help="Bazowy URL do katalogu LiCSAR_products",
    )
    parser.add_argument(
        "--mission",
        action="append",
        default=None,
        help=(
            "Endpoint misji (powtarzalny): np. /64/064A_04019_131313/interferograms "
            "lub pełny URL"
        ),
    )
    parser.add_argument(
        "--results-dir",
        "--output-dir",
        "-o",
        dest="results_dir",
        default=os.environ.get("LICSAR_RESULTS_DIR", "results"),
        help=(
            "Katalog docelowy na pobrane pliki "
            "(domyślnie: LICSAR_RESULTS_DIR lub results)"
        ),
    )
    parser.add_argument(
        "--year",
        type=int,
        default=2021,
        help="Rok, który musi występować w obu datach nazwy pakietu (domyślnie: 2021)",
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
        help=(
            "Liczba ponowień dla błędu HTTP 503 "
            f"(domyślnie: {DEFAULT_RETRIES_503})"
        ),
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
    parser.add_argument(
        "--verify-rounds",
        type=int,
        default=DEFAULT_VERIFY_ROUNDS,
        help=(
            "Ile rund weryfikacji i dogrywania brakujących plików wykonać po głównym przebiegu "
            f"(domyślnie: {DEFAULT_VERIFY_ROUNDS})"
        ),
    )
    return parser.parse_args()


def main() -> int:
    global ACTIVE_DASHBOARD
    args = parse_args()
    base_url = normalize_url(args.base_url)
    mission_endpoints = args.mission if args.mission else list(DEFAULT_MISSIONS)
    results_dir = Path(args.results_dir).resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    retries_503 = max(0, args.retry_503)
    retry_delay_seconds = max(0.0, args.retry_delay)
    verify_rounds = max(0, args.verify_rounds)

    downloaded = 0
    skipped = 0
    failed_attempts = 0
    missing_files_total = 0
    integrity_issues = 0
    known_total_files = 0
    completed_targets: set[Path] = set()

    dashboard = TerminalDashboard(status_order=STATUS_ORDER, lines_per_status=STATUS_LOG_LINES)
    ACTIVE_DASHBOARD = dashboard

    def mark_completed(target: Path) -> bool:
        if not is_download_complete(target):
            return False
        resolved = target.resolve()
        if resolved in completed_targets:
            return False
        completed_targets.add(resolved)
        return True

    def refresh_overall() -> None:
        missing = max(known_total_files - len(completed_targets), 0)
        dashboard.update_overall(
            total=known_total_files,
            completed=len(completed_targets),
            ok=downloaded,
            skip=skipped,
            fail=failed_attempts,
            missing=missing,
            listing_errors=integrity_issues,
        )

    refresh_overall()
    try:
        for mission_endpoint in mission_endpoints:
            try:
                mission_url = resolve_mission_url(base_url, mission_endpoint)
            except ValueError as exc:
                log_line(f"[WARN] Pomijam misję '{mission_endpoint}': {exc}")
                refresh_overall()
                continue

            mission_name = mission_folder_name(mission_url, mission_endpoint)
            mission_dir = results_dir / mission_name
            mission_dir.mkdir(parents=True, exist_ok=True)
            log_line(f"[INFO] Misja: {mission_name} ({mission_url})")

            try:
                top_links = fetch_links(
                    mission_url,
                    timeout=args.timeout,
                    retries_503=retries_503,
                    retry_delay_seconds=retry_delay_seconds,
                    status_callback=log_line,
                )
            except (HTTPError, URLError, TimeoutError) as exc:
                log_line(f"[ERROR] Nie udało się pobrać listy pakietów z {mission_url}: {exc}")
                integrity_issues += 1
                refresh_overall()
                continue

            packages = iter_package_links(mission_url, top_links, year=args.year)
            log_line(f"[INFO] Znaleziono {len(packages)} pakietów z rokiem {args.year} w obu datach.")

            mission_jobs: list[tuple[str, str, Path]] = []
            mission_listing_errors = 0

            for package_name, package_url in packages:
                package_dir = mission_dir / package_name
                package_dir.mkdir(parents=True, exist_ok=True)
                log_line(f"[INFO] Skanuję pakiet: {package_name}")

                try:
                    package_links = fetch_links(
                        package_url,
                        timeout=args.timeout,
                        retries_503=retries_503,
                        retry_delay_seconds=retry_delay_seconds,
                        status_callback=log_line,
                    )
                except (HTTPError, URLError, TimeoutError) as exc:
                    log_line(f"[ERROR] Pomijam {package_url} (błąd pobrania listingu): {exc}")
                    mission_listing_errors += 1
                    continue

                files = iter_target_files(package_links)
                if not files:
                    log_line(f"[WARN] Brak plików .geo.cc/.geo.unw (.tif/.tiff) w pakiecie {package_name}")
                    continue

                for file_name, file_href in files:
                    file_url = urljoin(package_url, file_href)
                    target = package_dir / file_name
                    mission_jobs.append((file_url, file_name, target))

            mission_total = len(mission_jobs)
            mission_downloaded = 0
            mission_skipped = 0
            mission_failed = 0

            known_total_files += mission_total
            for _, _, target in mission_jobs:
                mark_completed(target)
            refresh_overall()

            if mission_total == 0:
                log_line(
                    f"[INFO] Misja {mission_name}: brak plików do pobrania. "
                    f"Błędy listingu: {mission_listing_errors}"
                )
                if mission_listing_errors > 0:
                    integrity_issues += mission_listing_errors
                refresh_overall()
                continue

            log_line(f"[INFO] Misja {mission_name}: plików do przetworzenia: {mission_total}")

            for file_url, file_name, target in mission_jobs:
                log_line(f"[INFO] Pobieranie pliku: {file_name}")
                try:
                    if download_file(
                        file_url=file_url,
                        destination=target,
                        timeout=args.timeout,
                        retries_503=retries_503,
                        retry_delay_seconds=retry_delay_seconds,
                        status_callback=log_line,
                        file_progress_callback=dashboard.on_file_event,
                    ):
                        downloaded += 1
                        mission_downloaded += 1
                    else:
                        skipped += 1
                        mission_skipped += 1
                except (HTTPError, URLError, TimeoutError) as exc:
                    mission_failed += 1
                    failed_attempts += 1
                    log_line(f"[WARN] Nie udało się pobrać {file_url}: {exc}")
                except Exception as exc:
                    mission_failed += 1
                    failed_attempts += 1
                    log_line(f"[WARN] Nie udało się pobrać {file_url}: {exc}")

                mark_completed(target)
                refresh_overall()

            missing_jobs = collect_missing_jobs(mission_jobs)

            for verify_round in range(1, verify_rounds + 1):
                if not missing_jobs:
                    break

                log_line(
                    f"[VERIFY] Misja {mission_name}: runda {verify_round}/{verify_rounds}, "
                    f"brakujące pliki: {len(missing_jobs)}"
                )

                still_missing: list[tuple[str, str, Path]] = []
                for file_url, file_name, target in missing_jobs:
                    log_line(
                        f"[VERIFY] Runda {verify_round}/{verify_rounds} "
                        f"Dogrywanie: {file_name}"
                    )
                    try:
                        if download_file(
                            file_url=file_url,
                            destination=target,
                            timeout=args.timeout,
                            retries_503=retries_503,
                            retry_delay_seconds=retry_delay_seconds,
                            status_callback=log_line,
                            file_progress_callback=dashboard.on_file_event,
                        ):
                            downloaded += 1
                            mission_downloaded += 1
                        else:
                            skipped += 1
                            mission_skipped += 1
                    except (HTTPError, URLError, TimeoutError) as exc:
                        mission_failed += 1
                        failed_attempts += 1
                        log_line(f"[WARN] Nie udało się pobrać {file_url}: {exc}")
                    except Exception as exc:
                        mission_failed += 1
                        failed_attempts += 1
                        log_line(f"[WARN] Nie udało się pobrać {file_url}: {exc}")

                    if not is_download_complete(target):
                        still_missing.append((file_url, file_name, target))
                    else:
                        mark_completed(target)
                    refresh_overall()

                missing_jobs = still_missing

            mission_missing = len(missing_jobs)
            if mission_missing > 0:
                missing_files_total += mission_missing
                log_line(f"[ERROR] Misja {mission_name}: nadal brakuje {mission_missing} plików po weryfikacji.")
                for _, missing_name, missing_target in missing_jobs[:10]:
                    log_line(f"[ERROR] Brak: {missing_name} -> {missing_target}")
                if mission_missing > 10:
                    log_line(f"[ERROR] ... i jeszcze {mission_missing - 10} plików.")
            else:
                log_line(f"[VERIFY] Misja {mission_name}: kompletna (wszystkie pliki obecne).")

            if mission_listing_errors > 0:
                integrity_issues += mission_listing_errors
                log_line(
                    f"[ERROR] Misja {mission_name}: {mission_listing_errors} błędów listingu; "
                    "kompletność nie jest w 100% potwierdzona."
                )

            log_line(
                f"[INFO] Podsumowanie misji {mission_name}: "
                f"OK:{mission_downloaded} SKIP:{mission_skipped} FAIL:{mission_failed} "
                f"MISSING:{mission_missing} LISTING_ERRORS:{mission_listing_errors}"
            )
            log_line(
                f"[DONE] Misja {mission_name}: OK={mission_downloaded}, SKIP={mission_skipped}, "
                f"FAIL={mission_failed}, MISSING={mission_missing}"
            )
            refresh_overall()

        log_line(
            f"[DONE] Pobrano: {downloaded}, pominięto (już istniały): {skipped}, "
            f"nieudane próby: {failed_attempts}, brakujące pliki: {missing_files_total}, "
            f"błędy listingu: {integrity_issues}"
        )
        log_line(f"[DONE] Folder wynikowy: {results_dir}")
        refresh_overall()
        if missing_files_total > 0 or integrity_issues > 0:
            log_line("[ERROR] Pobieranie zakończone niekompletnie.")
            refresh_overall()
            return 2
        return 0
    finally:
        ACTIVE_DASHBOARD = None
        dashboard.close()


if __name__ == "__main__":
    raise SystemExit(main())
