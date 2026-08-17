# 🧚 Фея 1.0 — Crowding Gate Only

Это GitHub Pages-версия **предварительного фильтра включения/выключения Алмаза/Veles**.

## Рабочая архитектура

1. Фея считает текущий `OI value / rolling 24h quote turnover`.
2. Сравнивает его с `adaptive Q80`, рассчитанным по последним 180 дням.
3. Если `ratio <= Q80` → **Фея ON** → Veles/Алмаз можно включать и он самостоятельно ищет вход.
4. Если `ratio > Q80` → **Фея OFF** → Veles/Алмаз не должен искать новые сделки.

**NO-OVERLAP намеренно НЕ находится в Фее.** Это условие относится к внутренней логике входа Алмаза/Veles и тестируется/реализуется там отдельно.

## Активы

- HYPEUSDT
- PENDLEUSDT
- ONDOUSDT
- WIFUSDT

## Adaptive Q80

Порог ежедневно пересчитывается по последним 180 дням истории ratio.
Стартовый validated snapshot (15.08.2026 UTC):

- HYPE: `0.7830287563`
- PENDLE: `1.4511754425294032`
- ONDO: `0.7815167713847584`
- WIF: `0.8428355913575445`

## Данные

Исторический updater использует официальные Binance Futures daily archives:

- USD-M Futures metrics → `sum_open_interest_value`
- 1m klines → quote volume
- знаменатель = trailing 24h sum of 1m quote volume

Живая страница сначала пытается получить `sumOpenInterestValue` из Binance Futures Open Interest Statistics; если endpoint недоступен, использует fallback `openInterest × markPrice`. 24h turnover берётся из `quoteVolume` USD-M Futures 24h ticker.

## Автообновление

`.github/workflows/update-feya.yml` запускается ежедневно в `03:17 UTC` и также вручную через **Actions → Update Feya Q80 → Run workflow**.

Fail-safe: `thresholds.json` переписывается только если расчёт успешно завершился для всех четырёх монет. Если свежий daily archive Binance ещё не опубликован, предыдущий рабочий threshold сохраняется.

## GitHub Pages

Загрузить содержимое ZIP в корень репозитория с сохранением папок, включая `.github/workflows/`.
Для Pages выбрать **Deploy from a branch → main → /(root)**.
