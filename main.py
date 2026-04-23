from __future__ import annotations

import argparse
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
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


def fetch_links(url: str, timeout: int) -> list[str]:
    with urlopen(url, timeout=timeout) as response:
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


def download_file(file_url: str, destination: Path, timeout: int) -> bool:
    if destination.exists():
        print(f"[SKIP] {destination}")
        return False

    destination.parent.mkdir(parents=True, exist_ok=True)
    print(f"[START] {destination.name}")

    start_ts = time.monotonic()
    last_log_ts = start_ts
    downloaded_bytes = 0

    try:
        with urlopen(file_url, timeout=timeout) as response, destination.open("wb") as output:
            content_length = response.headers.get("Content-Length")
            total_bytes = int(content_length) if content_length and content_length.isdigit() else None

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
                    print(
                        f"[PROGRESS] {destination.name} | "
                        f"{percent:6.2f}% | "
                        f"{format_bytes(downloaded_bytes)}/{format_bytes(total_bytes)} | "
                        f"{format_bytes(int(speed_bps))}/s | ETA {format_eta(eta_seconds)}"
                    )
                else:
                    print(
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
    print(
        f"[OK] {destination} | "
        f"{format_bytes(downloaded_bytes)} | "
        f"{format_bytes(int(average_speed_bps))}/s | "
        f"{format_eta(total_elapsed)}"
    )
    return True


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
        default="results",
        help="Katalog docelowy na pobrane pliki (domyślnie: results)",
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_url = normalize_url(args.base_url)
    mission_endpoints = args.mission if args.mission else list(DEFAULT_MISSIONS)
    results_dir = Path(args.results_dir).resolve()
    results_dir.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    skipped = 0

    for mission_endpoint in mission_endpoints:
        try:
            mission_url = resolve_mission_url(base_url, mission_endpoint)
        except ValueError as exc:
            print(f"[WARN] Pomijam misję '{mission_endpoint}': {exc}")
            continue

        mission_name = mission_folder_name(mission_url, mission_endpoint)
        mission_dir = results_dir / mission_name
        mission_dir.mkdir(parents=True, exist_ok=True)
        print(f"[INFO] Misja: {mission_name} ({mission_url})")

        try:
            top_links = fetch_links(mission_url, timeout=args.timeout)
        except (HTTPError, URLError, TimeoutError) as exc:
            print(f"[ERROR] Nie udało się pobrać listy pakietów z {mission_url}: {exc}")
            continue

        packages = iter_package_links(mission_url, top_links, year=args.year)
        print(f"[INFO] Znaleziono {len(packages)} pakietów z rokiem {args.year} w obu datach.")

        for package_name, package_url in packages:
            package_dir = mission_dir / package_name
            package_dir.mkdir(parents=True, exist_ok=True)
            print(f"[INFO] Pakiet: {package_name}")

            try:
                package_links = fetch_links(package_url, timeout=args.timeout)
            except (HTTPError, URLError, TimeoutError) as exc:
                print(f"[WARN] Pomijam {package_url} (błąd pobrania listingu): {exc}")
                continue

            files = iter_target_files(package_links)
            if not files:
                print("[WARN] Brak plików .geo.cc/.geo.unw (.tif/.tiff) w tym pakiecie")
                continue

            for file_name, file_href in files:
                file_url = urljoin(package_url, file_href)
                target = package_dir / file_name
                try:
                    if download_file(file_url, target, timeout=args.timeout):
                        downloaded += 1
                    else:
                        skipped += 1
                except (HTTPError, URLError, TimeoutError) as exc:
                    print(f"[WARN] Nie udało się pobrać {file_url}: {exc}")

    print(f"[DONE] Pobrano: {downloaded}, pominięto (już istniały): {skipped}")
    print(f"[DONE] Folder wynikowy: {results_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
