-- ДДС · Пункт 1 · Правила классификации операций Тинькофф (T-Bank)
-- Дата: 2026-06-18
--
-- Операционные ДАННЫЕ (не миграция): применять и на dev, и на prod.
--   docker exec -i teplo-postgres psql -U teplo -d teplo < 2026-06-18_tbank_classification_rules.sql
--
-- Идемпотентно: блок снимает прежнюю версию своих правил (namespace 'T-Bank: ')
-- и пересоздаёт. article_id РЕЗОЛВИТСЯ ПО dds_articles.code — id различаются
-- между dev/prod (uuid генерится при заливке каталога), хардкодить uuid нельзя.
--
-- Матчинг: якорь = ИНН (counterparty_inn_match, точное совпадение по цифрам),
-- надёжнее имени. Где ИНН технический (АО ТБанк: карт-операции/комиссии) —
-- матч по подстроке назначения (purpose_pattern, casefold contains).
--
-- НЕ покрыто правилами (осознанно остаётся в needs_review):
--   * карт-закуп бизнес-картой (MAGNIT/Пятёрочка/магазины/аптеки) — решение владельца: owner-review (нужен чек);
--   * ЕНП Казначейства — дробить Налоги/Налоги с з/п, источник разбивки внешний (ведомость);
--   * новые контрагенты не из методологии: ИП Лизякин, ИП Скачкова, ИП Шевченко — уточнить роль у владельца;
--   * сторно карт-покупки (Отмена операции оплаты ...), поступление без контрагента.

BEGIN;

-- 0) Снести прежнюю версию правил этого namespace
DELETE FROM classification_rules WHERE name LIKE 'T-Bank: %';

-- 1) Правила по ИНН (контрагент → статья), provider=tbank, direction=out
INSERT INTO classification_rules
  (id, name, priority, is_active, provider, direction, counterparty_inn_match, action, article_id, comment)
SELECT gen_random_uuid(), v.name, 20, true, 'tbank', 'out', v.inn, 'set_article', a.id,
       'auto пункт1: контрагент по ИНН'
FROM (VALUES
  ('T-Bank: Оплата поставщикам — ИП Буряк (Продмаркет)',      '231006560100', 'payment_to_supplier'),
  ('T-Bank: Оплата поставщикам — ООО ТОРА (Амай)',            '6165233720',   'payment_to_supplier'),
  ('T-Bank: Оплата поставщикам — ООО АЛЬЯНС ЮГ',              '6168026120',   'payment_to_supplier'),
  ('T-Bank: Оплата поставщикам — ООО МЯСНОФФ-ДОН (Мяснов)',   '6162049667',   'payment_to_supplier'),
  ('T-Bank: Оплата поставщикам — ООО МИСТЕРИЯ',               '7707133576',   'payment_to_supplier'),
  ('T-Bank: Оплата поставщикам — ИП Карпов (Чародейка)',      '614300291463', 'payment_to_supplier'),
  ('T-Bank: Оплата поставщикам — ИП Манякина (Упак)',         '614311179343', 'payment_to_supplier'),
  ('T-Bank: Оплата поставщикам — ИП Егиазарян',               '614307902094', 'payment_to_supplier'),
  ('T-Bank: Оплата поставщикам — ООО КВАРЦ (Кола)',           '6143098186',   'payment_to_supplier'),
  ('T-Bank: Оплата поставщикам — ИП Алиев',                   '612306009409', 'payment_to_supplier'),
  ('T-Bank: Системы автоматизации — АО АЙКО',                 '1655166016',   'oplaty_sistem_avtomatizacii'),
  ('T-Bank: Системы автоматизации — ООО ЛЕММА',               '6168118525',   'oplaty_sistem_avtomatizacii'),
  ('T-Bank: Системы автоматизации — ООО ДОКСИНБОКС',          '7802193688',   'oplaty_sistem_avtomatizacii'),
  ('T-Bank: Сайт и приложение — ООО Назад в будущее',         '7839494297',   'sait_i_prilozhenie'),
  ('T-Bank: Телекоммуникации — ООО МИКРОЭЛ',                  '6143049372',   'telekommunikacii'),
  ('T-Bank: Аренда торг. точек — ООО ЭкоЦентр (вывоз мусора)','3444177534',   'arenda_torgovyh_tochek'),
  ('T-Bank: Аренда торг. точек — ЧОО Охрана Юг',             '6167107489',   'arenda_torgovyh_tochek'),
  ('T-Bank: Аренда торг. точек — ООО СПЕЦАВТО ЮГ',           '6167146456',   'arenda_torgovyh_tochek'),
  ('T-Bank: Контекстная реклама — ООО О.О (Синапс)',          '3525346702',   'kontekstnaya_reklama'),
  ('T-Bank: SEO-оптимизация — ООО СИНАПСИС (Трубина)',        '3525357535',   'seo_optimizaciya'),
  ('T-Bank: Таргетированная реклама — ИП Билинский',          '312334807779', 'targetirovannaya_reklama'),
  ('T-Bank: Налоги с з/п — ОСФР по Ростовской обл.',          '6163013494',   'nalogi_s_z_p')
) AS v(name, inn, code)
JOIN dds_articles a ON a.code = v.code;

-- 2) Правила по назначению для технического контрагента АО ТБанк, provider=tbank, direction=out
--    (эквайринг-комиссия раньше РКО по priority, чтобы «...по терминалам эквайринга» не уходило в РКО)
INSERT INTO classification_rules
  (id, name, priority, is_active, provider, direction, purpose_pattern, action, article_id, comment)
SELECT gen_random_uuid(), v.name, v.pr, true, 'tbank', 'out', v.pat, 'set_article', a.id,
       'auto пункт1: по назначению платежа'
FROM (VALUES
  ('T-Bank: Эквайринг (комиссия по терминалам)',          'терминалам эквайринга', 'ekvairing',              25),
  ('T-Bank: Поиск и найм — Avito',                        'avito',                 'poisk_i_naim_personala', 28),
  ('T-Bank: Телекоммуникации — Mango Office',             'mango',                 'telekommunikacii',       28),
  ('T-Bank: Телекоммуникации — ihc.ru',                   'ihc.ru',                'telekommunikacii',       28),
  ('T-Bank: РКО — плата за обслуживание/пакет/услуги',    'плата за',              'bank_service_fee',       30)
) AS v(name, pat, code, pr)
JOIN dds_articles a ON a.code = v.code;

-- 2a) РКО матчим ТОЛЬКО для технического контрагента АО ТБанк (ИНН 7710140679).
--     Без ИНН-якоря широкий паттерн «плата за» ловит «ВыПЛАТА ЗАработной платы»
--     (перевод ЗП на ИП Шокину) и ошибочно метит зарплатный транзит как РКО.
UPDATE classification_rules SET counterparty_inn_match = '7710140679'
WHERE name = 'T-Bank: РКО — плата за обслуживание/пакет/услуги';

-- 3) Выравнивание эквайринга T-Bank на каноническую статью (как у Сбера)
--    Аналитика-только: меняем статью, сумму/направление не трогаем → баланс кошелька не меняется.
UPDATE classification_rules
SET article_id = (SELECT id FROM dds_articles WHERE code = 'postuplenie_deneg_s_torg_tochek'),
    updated_at = now()
WHERE name = 'Зачисление эквайринга T-Bank';

UPDATE cashflow_transactions
SET article_id = (SELECT id FROM dds_articles WHERE code = 'postuplenie_deneg_s_torg_tochek')
WHERE article_id = (SELECT id FROM dds_articles WHERE code = 'revenue_acquiring_tbank');

COMMIT;
