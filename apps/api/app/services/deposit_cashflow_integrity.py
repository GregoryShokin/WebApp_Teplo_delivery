"""Deposit cashflows keep the recipient and balance in the deposit domain.

A generic DDS label cannot create a deposit payout or undo its employee ledger.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CashflowTransaction, DdsArticle, DepositBankDraft

DEPOSIT_PAYOUT_ARTICLE_CODE = "vydacha_depozita_sotrudniku"
DEPOSIT_PAYOUT_SOURCE_KINDS = frozenset(
    {"production_deposit_payout", "production_deposit_payout_draft"}
)
DEPOSIT_CLASSIFICATION_REFUSAL = (
    "Выдача депозита обязательно связана с сотрудником. "
    "Оформите или скорректируйте её в разделе «Зарплата → Депозиты»; "
    "отдельный разбор или исключение в ДДС не изменяет депозит сотрудника."
)


def deposit_cashflow_reclassification_reason(
    txn: CashflowTransaction, *, article_code: str | None = None
) -> str | None:
    if (
        txn.source_kind in DEPOSIT_PAYOUT_SOURCE_KINDS
        or article_code == DEPOSIT_PAYOUT_ARTICLE_CODE
    ):
        return DEPOSIT_CLASSIFICATION_REFUSAL
    return None


def ensure_generic_deposit_article_allowed(article: DdsArticle | None) -> None:
    if article is not None and article.code == DEPOSIT_PAYOUT_ARTICLE_CODE:
        raise ValueError(DEPOSIT_CLASSIFICATION_REFUSAL)


async def linked_deposit_cashflow_reclassification_reason(
    session: AsyncSession, txn: CashflowTransaction, *, article_code: str | None = None
) -> str | None:
    reason = deposit_cashflow_reclassification_reason(txn, article_code=article_code)
    if reason is not None:
        return reason
    if txn.source_kind in {"safe_payout", "kassa_target_payout"} and txn.source_id is not None:
        # Старая переразметка могла уже снять депозитную статью. Связь с депозитным
        # резервом всё равно защищает источник: новый ярлык не отменил выдачу сотруднику.
        draft_id = await session.scalar(
            select(DepositBankDraft.id).where(DepositBankDraft.safe_allocation_id == txn.source_id)
        )
        if draft_id is not None:
            return DEPOSIT_CLASSIFICATION_REFUSAL
    return None
