# Comet LiCSAR Downloader

Prosty skrypt do pobierania danych interferogramów LiCSAR z listingu HTML.

Skrypt:
- wyszukuje pakiety z datami (np. `20210101_20210107`),
- filtruje tylko te, gdzie **obie daty są z wybranego roku** (domyślnie `2021`),
- pobiera tylko pliki:
  - `.geo.cc.tif/.tiff`
  - `.geo.unw.tif/.tiff`
- zapisuje je w folderach:
  - `results/<misja>/<pakiet>/...`

## Wymagania

Nie musisz znać programowania. Potrzebujesz:
- komputera z internetem,
- terminala,
- narzędzia `uv`.

### Instalacja `uv` (Mac)

Najprościej:

```bash
brew install uv
```

### Instalacja `uv` (Windows)

Opcja 1 (PowerShell, zalecana):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Opcja 2 (`winget`):

```powershell
winget install --id=astral-sh.uv -e
```

## Jak uruchomić (krok po kroku)

1. Otwórz terminal.
2. Wejdź do folderu projektu:

```bash
cd /Users/lsok/Desktop/julie-script
```

3. Uruchom pobieranie (przykład dla 2 misji):

```bash
uv run python main.py \
  --mission /64/064A_04019_131313/interferograms \
  --mission /137/137A_05266_171717/interferograms \
  --year 2021 \
  -o /Users/lsok/Desktop/comet-results
```

Po zakończeniu pliki będą w:
- `/Users/lsok/Desktop/comet-results`

## Jak uruchomić na Windows (PowerShell)

1. Otwórz PowerShell.
2. Wejdź do folderu projektu:

```powershell
cd C:\sciezka\do\julie-script
```

3. Uruchom pobieranie (przykład dla 2 misji):

```powershell
uv run python main.py `
  --mission /64/064A_04019_131313/interferograms `
  --mission /137/137A_05266_171717/interferograms `
  --year 2021 `
  -o C:\sciezka\do\comet-results
```

Po zakończeniu pliki będą w folderze:
- `C:\sciezka\do\comet-results`

## Najważniejsze opcje

- `--year 2021`  
  rok, który musi wystąpić w obu datach nazwy pakietu.

- `-o / --output-dir / --results-dir`  
  gdzie zapisać wyniki.

- `--retry-503 5`  
  ile razy ponawiać przy błędzie serwera 503.

- `--retry-delay 2`  
  opóźnienie bazowe retry (sekundy).

- `--verify-rounds 2`  
  ile rund dogrywania brakujących plików wykonać po głównym pobieraniu.

## Przerywanie działania

- `Ctrl+C` (pierwszy raz):  
  skrypt dokończy **aktualnie pobierany plik** i zatrzyma się bez uszkadzania tego pliku.

- `Ctrl+C` (drugi raz):  
  wymusza natychmiastowe przerwanie.

## Przydatne (Mac): żeby system nie usypiał się w trakcie

```bash
caffeinate -i uv run python main.py ...
```

## Testy regresyjne (opcjonalnie)

Jeśli chcesz sprawdzić, czy zmiany niczego nie zepsuły:

```bash
uv run python -m unittest discover -s tests -v
```
