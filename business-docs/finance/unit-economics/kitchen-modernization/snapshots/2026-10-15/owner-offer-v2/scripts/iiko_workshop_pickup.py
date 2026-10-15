import asyncio,copy,datetime,json,sys
from sqlalchemy import text
from app.db.session import AsyncSessionLocal,engine
from app.services.iiko_sync import _load_source_credential_env,_load_export_employees_module,_logout_iiko_client
PRESET='73a25778-dafb-4065-a820-1e9c7da6fed6'
async def main():
 async with AsyncSessionLocal() as s:
  await s.execute(text('SET TRANSACTION READ ONLY'))
  await s.execute(text("SET LOCAL statement_timeout='30000ms'"))
  await _load_source_credential_env(s);await s.rollback()
 await engine.dispose()
 mod=_load_export_employees_module();mod.load_local_env();client=mod.IikoClient()
 out={'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'requests':[]}
 def call(name,path,**kw):
  status,raw=client.request(path,**kw)
  try:d=json.loads(raw)
  except (TypeError,json.JSONDecodeError):d={'nonjson':True}
  out[name]=d;out['requests'].append({'name':name,'endpoint':path,'method':kw.get('method','GET'),'status':status,'request':kw.get('json_body',kw.get('params'))})
  print(name,status,len(d.get('data',[])) if isinstance(d,dict) else len(d),file=sys.stderr,flush=True)
  return d
 try:
  cols=call('columns','/v2/reports/olap/columns',params={'reportType':'SALES'})
  for name,start,end,rows,aggs in [
   ('daily_channels_2026','2026-01-01','2026-10-15',['OpenDate.Typed','OrderType'],['UniqOrderId','DishDiscountSumInt','DishSumInt']),
   ('aug_workshop_sku','2026-08-01','2026-09-01',['CookingPlace','DishId','DishName','SoldWithDish.Id'],['DishAmountInt','DishSumInt','DishDiscountSumInt','ProductCostBase.ProductCost'])]:
   filters={'DeletedWithWriteoff':{'filterType':'IncludeValues','values':['NOT_DELETED']},'OrderDeleted':{'filterType':'IncludeValues','values':['NOT_DELETED']},'Department.Code':{'filterType':'IncludeValues','values':['2']},'OpenDate.Typed':{'filterType':'DateRange','from':start+'T00:00:00','to':end+'T00:00:00','includeLow':True,'includeHigh':False,'periodType':'CUSTOM'}}
   body={'reportType':'SALES','buildSummary':False,'groupByColFields':[],'filters':filters,'groupByRowFields':rows,'aggregateFields':aggs}
   call(name,'/v2/reports/olap',method='POST',json_body=body)
  out['columns']={k:v for k,v in cols.items() if 'Discount' in k or k in ['DishSumInt','DishDiscountSumInt','OrderType','UniqOrderId']}
 finally:_logout_iiko_client(client);out['logout_done']=True
 out['finished_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
 json.dump(out,sys.stdout,ensure_ascii=False,default=str)
asyncio.run(main())
