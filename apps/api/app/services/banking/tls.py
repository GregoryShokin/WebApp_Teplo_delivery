"""TLS-доверие для банковских API на сертификатах НУЦ Минцифры.

23.09.2026 между 16:00 и 17:00 МСК Т-Банк перевёл ``business.tbank.ru`` на сертификат, выпущенный
``Russian Trusted Sub CA`` (корень — ``Russian Trusted Root CA`` Минцифры). Этого корня нет
в ``certifi``, по которому httpx проверяет сервер по умолчанию, поэтому каждый запрос падал
``CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain``: выписка Т-Банка
перестала приходить, а «Отправить в банк» отвечал «Банк временно недоступен».

Контекст = стандартный набор ``certifi`` + корень Минцифры: сервер проходит проверку и на
новой цепочке, и если банк вернётся к сертификату международного УЦ. Корень добавляем только
клиентам банков, которые на нём живут, а не глобально через ``SSL_CERT_FILE`` — чтобы не
расширять доверие для остального исходящего трафика (iiko, СБИС, Telegram, почта). Сбер
держит свой бандл отдельно (``sber_api_ca_bundle_path``), там этот корень уже есть.
"""

from __future__ import annotations

import ssl
from functools import lru_cache
from pathlib import Path

import certifi

RUSSIAN_TRUSTED_ROOT_CA = Path(__file__).with_name("certs") / "russian_trusted_root_ca.pem"


@lru_cache(maxsize=1)
def russian_trusted_ssl_context() -> ssl.SSLContext:
    """SSL-контекст ``certifi`` + ``Russian Trusted Root CA`` (строится один раз на процесс)."""
    context = ssl.create_default_context(cafile=certifi.where())
    context.load_verify_locations(cafile=str(RUSSIAN_TRUSTED_ROOT_CA))
    return context
