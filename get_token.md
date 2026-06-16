# Как получить GoMining JWT токен

Токен нужен для получения **реальной мощности** майнеров (с учётом апгрейдов).
Без токена парсер работает, но показывает только `BASELINE_POWER` из метаданных NFT.

## Способ 1: Браузер + DevTools (рекомендуется)

1. Открой [https://nft.gomining.com](https://nft.gomining.com) в Chrome/Firefox
2. Войди в аккаунт (через Tonkeeper или другой TON кошелёк)
3. Открой DevTools (`F12` или `Cmd+Opt+I`)
4. Перейди на вкладку **Network**
5. Обнови страницу или открой любого своего майнера
6. Найди любой запрос к `nft.gomining.com/api/...`
7. Нажми на него → вкладка **Headers**
8. В разделе **Request Headers** найди строку:
   ```
   Authorization: Bearer eyJhbGciOiJ...
   ```
9. Скопируй всё что после `Bearer ` — это и есть твой токен

## Способ 2: Через переменную окружения

```bash
export GOMINING_TOKEN="eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9..."
python main.py miner 349973
```

## Способ 3: Через аргумент --token

```bash
python main.py miner 349973 --token "eyJhbGciOiJ..."
```

## Важно

- Токен истекает через некоторое время — получай новый при ошибке `401 Unauthorized`
- Токен даёт доступ только к просмотру данных, не к транзакциям
- Храни токен в секрете (не публикуй в git)
