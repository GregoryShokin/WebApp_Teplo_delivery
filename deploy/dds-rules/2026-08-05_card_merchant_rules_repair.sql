-- Ремонт правил после инцидента 03.08.2026 «карт-оплата ihc.ru забрала себе все карт-списания».
--
-- Что случилось. «Запомнить» на карт-оплате хостинга (Оплата в YM*ihc.ru MOSKVA RUS, 5 000 ₽)
-- пошло по ветке «есть ИНН → матчим по ИНН». Но у карт-операции T-Банка ИНН получателя — это
-- сам банк-эквайер (7710140679, счёт 30232…), одинаковый у ВСЕХ оплат картой. Правило
-- «T-Bank: РКО — плата за обслуживание/пакет/услуги» (priority 30, тот же ИНН) при этом было
-- перезаписано: у него обнулили purpose_pattern и provider, а статью и контрагента заменили на
-- «Оплаты систем автоматизации» / IHC.ru. Получилось «любой расход с ИНН эквайера → IHC.ru».
--
-- Кого задело: Ozon 5 972,00 · Магнит 299,97 · Магистр 224,00 (= 6 495,97 ₽) — статья, контрагент
-- и, как следствие, фантомная открытая дебиторка на IHC.ru. Уцелели только те карт-оплаты, кого
-- перехватили правила меньшего приоритета (mango — 28, avito — 28) или уже размеченные вручную.
--
-- Код починен отдельно (merchant_text + _remember_binding_rule): «запомнить» на карт-операции
-- теперь пишет ИМЯ МЕРЧАНТА из назначения, а чужие правила не расширяет. Этот файл возвращает
-- прод-данные в согласованное состояние. Идемпотентен, порядок шагов важен.

BEGIN;

-- 1) Правило РКО — обратно в сеяное состояние: узкий текст + ИНН-якорь + статья «РКО».
--    ИНН-якорь тут законный: без него «плата за» ловит «ВыПЛАТА ЗАработной платы».
UPDATE classification_rules
SET purpose_pattern = 'плата за',
    provider        = 'tbank',
    direction       = 'out',
    counterparty_id = NULL,
    article_id      = (SELECT id FROM dds_articles WHERE code = 'bank_service_fee'),
    comment         = 'auto пункт1: по назначению платежа',
    updated_at      = now()
WHERE name = 'T-Bank: РКО — плата за обслуживание/пакет/услуги';

-- 2) Карт-оплаты ihc.ru ведём туда, куда владелец разметил их 03.08: статья «Оплаты систем
--    автоматизации» и контрагент IHC.ru. Правило по имени мерчанта уже есть с 18.06 (паттерн
--    'ihc.ru', priority 28) — правим его, а не заводим второе с тем же текстом.
UPDATE classification_rules
SET name            = 'T-Bank: Оплаты систем автоматизации — ihc.ru',
    article_id      = (SELECT id FROM dds_articles WHERE code = 'oplaty_sistem_avtomatizacii'),
    counterparty_id = (SELECT id FROM counterparty WHERE name = 'IHC.ru (поставщик серверов)'),
    provider        = NULL,  -- смена банка не должна выключать привязку
    updated_at      = now()
WHERE purpose_pattern = 'ihc.ru';

-- 3) Имя мерчанта — псевдонимом карточки: реестр узнаёт продавца и без правила (новый шаг
--    классификатора match_counterparty_by_merchant). Псевдоним уникален по всему реестру.
INSERT INTO counterparty_alias (id, counterparty_id, alias, source)
SELECT gen_random_uuid(), c.id, 'ihc.ru', 'card_merchant'
FROM counterparty c
WHERE c.name = 'IHC.ru (поставщик серверов)'
  AND NOT EXISTS (SELECT 1 FROM counterparty_alias a WHERE lower(a.alias) = 'ihc.ru');

COMMIT;

-- Проверка после применения (ожидается: РКО-правило со своим текстом и статьёй, правило ihc.ru
-- с контрагентом, ни одного активного правила по ИНН 7710140679 без purpose_pattern):
--
--   SELECT priority, name, counterparty_inn_match, purpose_pattern, article_id, counterparty_id
--   FROM classification_rules WHERE is_active ORDER BY priority, name;
--
-- Три ошибочно размеченные операции возвращает в разбор отдельный скрипт
-- deploy/dds-rules/2026-08-05_card_merchant_reopen_ops.py — SQL для них не годится: у каждой
-- висит автопредоплата, и снимать её должен код (_drop_untouched_bank_prepayments).
