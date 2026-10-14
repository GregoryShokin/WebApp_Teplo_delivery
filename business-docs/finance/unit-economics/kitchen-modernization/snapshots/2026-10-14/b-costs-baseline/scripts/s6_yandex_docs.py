import asyncio, json, sys
from sqlalchemy import text
from app.db.session import AsyncSessionLocal, engine
async def main():
    async with AsyncSessionLocal() as s:
        await s.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        await s.execute(text("SET LOCAL statement_timeout='30000ms'"))
        r = await s.execute(text("""select i.number, i.invoice_date, i.service_period_start, i.service_period_end, i.amount, i.vat_total,
              i.source, i.doc_kind, i.line_items, e.amount acc_amount, e.status, e.article_id is null no_article,
              p.default_dds_article_id is null no_default
            from supplier_invoice i join counterparty c on c.id=i.counterparty_id
            left join supplier_expense_accrual e on e.invoice_id=i.id
            left join counterparty_payable_profile p on p.counterparty_id=c.id
            where c.name ilike '%ЯНДЕКС.ЕДА%' order by i.service_period_start"""))
        rows=[dict(x._mapping) for x in r]
        await s.rollback()
    await engine.dispose()
    json.dump(rows, sys.stdout, default=str, ensure_ascii=False)
asyncio.run(main())
