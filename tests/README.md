# Testy regresyjne

Zestaw testów znajduje się w pliku:
- `tests/test_main.py`

Uruchomienie pełnego zestawu:

```bash
uv run python -m unittest discover -s tests -v
```

Zakres testów:
- filtrowanie pakietów i plików (`iter_package_links`, `iter_target_files`)
- brak fallbacku URL (`candidate_download_urls`)
- retry dla HTTP 503 (`urlopen_with_503_retry`)
- parsowanie plain HTML linków (`fetch_links`)
- pobieranie plików, status i czyszczenie częściowych danych po błędzie
- sprawdzanie kompletności (`collect_missing_jobs`, `is_download_complete`)
- scenariusze end-to-end `main()`:
  - pełny sukces
  - brakujące pliki po wszystkich próbach (błąd końcowy)
  - odzyskanie braków w rundzie weryfikacji
  - błąd listingu misji (integrity error)
