import asyncio, json, sys
from sqlalchemy import text
from app.db.session import AsyncSessionLocal, engine
async def main():
    async with AsyncSessionLocal() as s:
        await s.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        await s.execute(text("SET LOCAL statement_timeout='30000ms'"))
        r = await s.execute(text("""select a.code, a.name, a.activity_type, a.movement_type,
              (select count(*) from cashflow_transactions t where t.article_id=a.id and t.operation_date>='2026-07-01' and t.quality_status<>'excluded') n,
              (select sum(amount) from cashflow_transactions t where t.article_id=a.id and t.operation_date>='2026-07-01' and t.quality_status<>'excluded') s
            from dds_articles a where a.activity_type ilike '%invest%' or a.name ilike '%оборудован%' or a.name ilike '%ОС%'"""))
        rows=[dict(x._mapping) for x in r]
        await s.rollback()
    await engine.dispose()
    json.dump(rows, sys.stdout, default=str, ensure_ascii=False)
asyncio.run(main())
