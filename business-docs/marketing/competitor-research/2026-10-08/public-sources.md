# Публичные источники конкурентов — Волгодонск, 08.10.2026

Первичный реестр шести компаний: 20 блюд и 25 источников. Это карта источников и небольшая выборка меню; полнота аудита не заявляется. Цены собственных сайтов отделены от поискового индекса. Себестоимость не рассчитана: нет граммовок ингредиентов и привязки к датированным закупкам «Тепла».

| Компания | Что подтверждено | Существенный пробел |
|---|---|---|
| [Суши Шеф](https://sushi-shef.ru/) | 4 блюда с ценами, весами и опубликованными составами; акции и доставка | Неизвестны количества ингредиентов; конфликт адреса Курчатова 48А/48Д |
| [Ёбидоёби](https://volgodonsk.yobidoyobi.ru/) | Адрес; 4 блюда из индекса официального сайта | Прямой сайт показал CAPTCHA; цены и веса не проверены live |
| [Суши Ролл / Chicken & Roll](https://xn--61-vlcmaqngs4c.xn--p1ai/) | 4 блюда, акции, доставка, списание бонусов; редирект старого бренда | Нет количества ингредиентов; условия Ланча содержат противоречие |
| [Суши Микс](https://taplink.cc/_sushi_mix_) | Два адреса, iOS/Android, каталог, 4 цены, доставка и скидка на ДР | Карточки списка не дали вес и состав; нужен просмотр деталей |
| [Пицца да мамма](https://www.pizzadamamma.ru/) | Публичный каталог 24 пицц; выборка 4 с весом, размером и составом | Тесто отсутствует в кратком списке топпингов; нужен модельный рецепт |
| [СушиВёсла](https://xn--80adjkr6adm9b.xn--p1ai/) | Адрес Весенняя 56Г, приложение, источник отзывов | Локальное меню из JS-страницы не получено |

## Результаты, которые нельзя потерять

- Chicken & Roll сейчас перенаправляет на «Суши Ролл». Один конкурент, историческое название сохранено. Юридическую преемственность отдельно не проверяли.
- У Суши Шефа Филадельфия стоит 429 ₽ за 226 г / 8 шт., Люкс — 699 ₽ за 260 г / 8 шт. Краткие списки ингредиентов совпадают. Это не доказывает одинаковое количество рыбы.
- У «Суши Ролл» опубликованы бесплатная доставка от 1300 ₽, скидки по времени и предел списания бонусов 20%. Текст акции «Ланч» противоречив: «не действует на неакционные позиции». Не исправлять молча на предполагаемое правило.
- У «Суши Микс» найдены собственный Taplink, каталог и ссылки на приложения. Доставка заявлена 100 ₽, скидка на день рождения 10%; точные ограничения последней не опубликованы в прочитанном блоке.
- У «Пицца да мамма» выборка: Пепперони 700 ₽, Ассорти / Мясная / 4 сыра по 800 ₽; диаметр 35 см, опубликованные веса 650±50 г либо 720–750 г. Не пропускать тесто при расчёте модельной себестоимости.
- Ёбидоёби: разные индексированные страницы расходятся в весах Каппа маки (130/140 г) и Филадельфии с копчёным лососем лайт (230/225 г). Записи не отмечены как актуальные проверенные предложения.
- СушиВёсла: найдены исторические весенние акции 2026 года. Они намеренно не перенесены в текущие предложения октября.

## Как получены данные

Публичные HTML и каталог были прочитаны во временном каталоге. Сырые страницы в этот переносимый реестр не включены; SHA256 прямых загрузок сохранены для происхождения. В JSON указаны URL, тип источника и дата; для прямых загрузок — точное время и SHA256. Из публичного HTML извлечены только данные меню, адреса, ссылки и условия для покупателей.

У Суши Шефа составы доступны в `schema.org/Product`, у Суши Микс — ссылки и цены в публичном `window.data`. У Пицца да мамма каталог загружается с Tilda: адрес read-only endpoint и параметры установлены по опубликованному HTML и его скрипту. Доступ к закрытым API, сессиям, клиентским данным не использовался. Заказы и сообщения не отправлялись.

CAPTCHA Ёби не обходилась. Taplink frontend JS вернул 403, дальнейшее чтение этого ресурса остановлено. Составы Суши Микс не восстановлены по названиям или фотографиям.

## Отзывы

Карты допустимы с пометкой возможной недостоверности. Мнение владельца о накрутке Суши Шефа и Ёбидоёби сохранено как `owner_assessment`, а не как независимо доказанный факт. Яндекс Еда — предпочтительный источник для Шефа по указанию владельца, но без безусловной гарантии достоверности всех текстов.

У СушиВёсла ранее прочитаны 8 полных текстов из поискового индекса карточки Карт, но отдельные строки не сохранены: `reported_read_count=8`, `captured_rows=0`. Темы вкуса, сервиса, дорогих ингредиентов, замен, опозданий и комплектации оставлены как предварительные направления проверки, а не воспроизводимый корпус из 8 отзывов. Количественные выводы требуют нового сбора строк. У остальных в этой подзадаче самостоятельная выборка отзывов не собрана.

Визуальный дизайн всех шести компаний требует просмотра скриншотов. Цвета или шрифты из HTML не заменяют визуальный аудит.

## Источники

- `shef_home`: [https://sushi-shef.ru/](https://sushi-shef.ru/) — official_html; проверено 2026-10-08T00:49:30+03:00.
- `shef_promos`: [https://sushi-shef.ru/volgodonsk/landing/akcii](https://sushi-shef.ru/volgodonsk/landing/akcii) — official_html; проверено 2026-10-08T00:50:53+03:00.
- `shef_delivery`: [https://sushi-shef.ru/volgodonsk/landing/dostavka](https://sushi-shef.ru/volgodonsk/landing/dostavka) — official_page_web; проверено 2026-10-08.
- `shef_pay`: [https://sushi-shef.ru/volgodonsk/landing/pay](https://sushi-shef.ru/volgodonsk/landing/pay) — official_search_index; проверено 2026-10-08.
- `shef_2gis`: [https://2gis.ru/volgodonsk/firm/70000001049148270](https://2gis.ru/volgodonsk/firm/70000001049148270) — maps_search_index; проверено 2026-10-08.
- `shef_eda_lead`: [https://xn--80aafbg2bzayj.xn--p1ai/volgodonsk/restaurant/sushi_shef_j2gns/3014806878_-rosto-set/](https://xn--80aafbg2bzayj.xn--p1ai/volgodonsk/restaurant/sushi_shef_j2gns/3014806878_-rosto-set/) — third_party_source_discovery; проверено 2026-10-08.
- `chicken_home`: [https://xn--61-vlcmaqngs4c.xn--p1ai/](https://xn--61-vlcmaqngs4c.xn--p1ai/) — official_html; проверено 2026-10-08T00:49:27+03:00.
- `chicken_delivery`: [https://xn--61-vlcmaqngs4c.xn--p1ai/delivery-payment](https://xn--61-vlcmaqngs4c.xn--p1ai/delivery-payment) — official_html; проверено 2026-10-08T00:50:44+03:00.
- `chicken_promotions`: [https://xn--61-vlcmaqngs4c.xn--p1ai/promotions](https://xn--61-vlcmaqngs4c.xn--p1ai/promotions) — official_html; проверено 2026-10-08T00:50:47+03:00.
- `chicken_loyalty`: [https://xn--61-vlcmaqngs4c.xn--p1ai/loyalty-program](https://xn--61-vlcmaqngs4c.xn--p1ai/loyalty-program) — official_html; проверено 2026-10-08T00:50:51+03:00.
- `mix_home`: [https://taplink.cc/_sushi_mix_](https://taplink.cc/_sushi_mix_) — official_link_hub_html; проверено 2026-10-08T00:49:19+03:00.
- `mix_menu`: [https://taplink.cc/_sushi_mix_/p/34c255/](https://taplink.cc/_sushi_mix_/p/34c255/) — official_link_hub_html; проверено 2026-10-08T00:50:54+03:00.
- `mix_cold`: [https://taplink.cc/_sushi_mix_/m/98949/](https://taplink.cc/_sushi_mix_/m/98949/) — official_catalog_html; проверено 2026-10-08T00:51:23+03:00.
- `mix_sets`: [https://taplink.cc/_sushi_mix_/m/986c1/](https://taplink.cc/_sushi_mix_/m/986c1/) — official_catalog_html; проверено 2026-10-08T00:51:24+03:00.
- `mix_maps`: [https://yandex.com/maps/org/sushi_mix/1687413327/](https://yandex.com/maps/org/sushi_mix/1687413327/) — maps_search_index; проверено 2026-10-08.
- `mamma_home`: [https://www.pizzadamamma.ru/](https://www.pizzadamamma.ru/) — official_html; проверено 2026-10-08T00:49:18+03:00.
- `mamma_catalog`: [https://store.tildaapi.com/api/getproductslist/?storepartuid=202588773792&recid=837391015&getparts=true&getoptions=true&flag_root=withroot](https://store.tildaapi.com/api/getproductslist/?storepartuid=202588773792&recid=837391015&getparts=true&getoptions=true&flag_root=withroot) — public_official_store_catalog; проверено 2026-10-08T00:53:26+03:00.
- `yobi_home_index`: [https://volgodonsk.yobidoyobi.ru/](https://volgodonsk.yobidoyobi.ru/) — official_search_index; проверено 2026-10-08.
- `yobi_detail_index`: [https://volgodonsk.yobidoyobi.ru/novinki/krevetkadorblyu](https://volgodonsk.yobidoyobi.ru/novinki/krevetkadorblyu) — official_search_index; проверено 2026-10-08.
- `yobi_promos_index`: [https://volgodonsk.yobidoyobi.ru/actions](https://volgodonsk.yobidoyobi.ru/actions) — official_search_index; проверено 2026-10-08.
- `yobi_eda_lead`: [https://vkus-eda-dostavka.ru/volgodonsk/yobidoyobi_osfkt/yabo](https://vkus-eda-dostavka.ru/volgodonsk/yobidoyobi_osfkt/yabo) — third_party_source_discovery; проверено 2026-10-08.
- `vesla_home`: [https://xn--80adjkr6adm9b.xn--p1ai/](https://xn--80adjkr6adm9b.xn--p1ai/) — official_html; проверено 2026-10-08T00:49:31+03:00.
- `vesla_telegram`: [https://t.me/s/sushivesla?after=4576](https://t.me/s/sushivesla?after=4576) — official_social_search_index; проверено 2026-10-08.
- `vesla_maps`: [https://yandex.com/maps/org/sushivesla/88818937392/reviews/](https://yandex.com/maps/org/sushivesla/88818937392/reviews/) — maps_search_index; проверено 2026-10-08.
- `vesla_app`: [https://play.google.com/store/apps/details?hl=ru&id=com.ub.sushivesla.app](https://play.google.com/store/apps/details?hl=ru&id=com.ub.sushivesla.app) — app_store_search_index; проверено 2026-10-08.
