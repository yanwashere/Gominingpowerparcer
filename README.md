# GoMining Power Parser

Парсер реальной мощности NFT-майнеров [GoMining](https://nft.gomining.com) на блокчейне TON.

## Зачем

На маркетплейсе (getgems.io) NFT показывает **базовую мощность** (`BASELINE_POWER`).
Но владелец может докупить апгрейды в приложении GoMining — тогда **реальная мощность** будет выше.
Такие майнеры продаются по "базовой" цене, хотя их фактический хешрейт больше → **выгодная покупка**.

Этот парсер показывает:
- Базовую и реальную мощность (TH/s)
- Прибавку от апгрейдов (+TH, %)
- Цену в TON и метрику **TH/TON** (чем выше — тем лучше)
- Эффективность (W/TH)

## Поддерживаемые коллекции

| Название | Адрес |
|----------|-------|
| GoMining Digital Miners (Release 1) | `EQBY-QwusK_kNxUy2F7LpPa7-e8XKEskLedb6fXzJ6n9JjWD` |
| GoMining Digital Miners: Release 2 | `EQBykWvdmoyFD2kB4BJbdrxRqyWKzSLDn-SgwNvznRfbsaKv` |

## Установка

```bash
pip install -r requirements.txt
```

## Быстрый старт

```bash
# Посмотреть конкретный майнер по ID
python main.py miner 349973

# С реальной мощностью (нужен токен GoMining — см. get_token.md)
python main.py miner 349973 --token "eyJhbGciOiJ..."

# Проверить несколько майнеров сразу
python main.py miner 349973 100001 200050

# Загрузить список IDs из файла
python main.py miner --file ids.txt

# Сканировать ВСЕ листинги Release 2, показать топ-50 по TH/TON
python main.py scan "GoMining Digital Miners: Release 2" --top 50

# Только апгрейднутые майнеры (нужен токен)
python main.py scan --all --upgraded-only --token "eyJhbGciOiJ..."

# Фильтр: мощность > 2 TH, цена < 500 TON
python main.py scan --min-power 2 --max-price 500

# Экспорт в CSV
python main.py scan --csv miners.csv

# Список коллекций
python main.py collections
```

## Как работает

### Источники данных

| Источник | Что даёт | Авторизация |
|----------|----------|-------------|
| `api.getgems.io/graphql` | Цены, листинги, атрибуты из NFT метаданных | Нет |
| `tonapi.io/v2` | On-chain данные NFT (адреса, метаданные) | Опционально (бесплатный ключ) |
| `nft.gomining.com/api` | **Реальная мощность** с апгрейдами | **JWT токен** |

### Как получить токен GoMining

Смотри файл [get_token.md](get_token.md).

### Переменные окружения

```bash
export GOMINING_TOKEN="eyJ..."   # токен GoMining
export TONAPI_KEY="..."          # ключ tonapi.io (опционально)
```

## Параметры команды `scan`

| Опция | Описание |
|-------|----------|
| `--sort value` | Сортировка по TH/TON (лучшие сделки первые) |
| `--sort upgrade` | Сортировка по проценту апгрейда |
| `--sort price` | По цене (дешёвые первые) |
| `--sort power` | По мощности (высокие первые) |
| `--top N` | Показать только топ N майнеров |
| `--upgraded-only` | Только майнеры с real > baseline |
| `--min-power TH` | Минимальная мощность |
| `--max-price TON` | Максимальная цена |
| `--max MAX` | Макс. листингов для загрузки |
| `--csv FILE` | Экспорт в CSV |
| `--all` | Сканировать обе коллекции |

## Пример вывода

```
                     🔥 GoMining NFT Miner Analysis                      
╭──────────┬──────────────────────────┬──────────┬──────────┬───────────────╮
│        # │ Name                     │ Baseline │ Real     │ Price (TON)   │
│          │                          │ Power    │ Power    │ Value (TH/TON)│
├──────────┼──────────────────────────┼──────────┼──────────┼───────────────┤
│   349973 │ The Mine Box #349973     │ 2.000 TH │ 2.050 TH │ 450.00 TON    │
│          │                          │          │ +2.5%    │ 0.00456 TH/T  │
╰──────────┴──────────────────────────┴──────────┴──────────┴───────────────╯
```
