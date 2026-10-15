from starter_read import *
import concurrent.futures,time
OUT.mkdir(parents=True,exist_ok=True)
base,op=connect()
p={'statuses':'done','date_min':'01.08.2026','date_max':'31.08.2026','offset':0,'limit':100,'order_by':'-delivery_time'}
started=datetime.datetime.now(datetime.timezone.utc).isoformat()
total=get(base,op,'/api/v1/frontend/order/total',p)
records=[]
for offset in range(0,total['total']+1,100):
 data=get(base,op,'/api/v1/frontend/orders',p|{'offset':offset})
 records.extend(project(x) for x in data)
 print('List page',offset,'rows',len(data),flush=True)
 if len(data)<100:break
assert len({x['id'] for x in records})==len(records)==total['total']
(OUT/'orders-aug-list-private.json').write_text(json.dumps(records,ensure_ascii=False))
print('List complete',len(records),'date range',min(x['deliveryTime'] for x in records),max(x['deliveryTime'] for x in records),flush=True)
# One-month complete details, only required financial fields retained. Four read workers.
errors=[];details=[]
def detail(row):
 for attempt in range(2):
  try:return project(get(base,op,'/api/v1/frontend/order/'+str(row['id'])),True)
  except (urllib.error.HTTPError,urllib.error.URLError,TimeoutError) as e:
   code=getattr(e,'code',None)
   if code in [401,403]:raise RuntimeError('Authorization expired; stop') from None
   if attempt==0:time.sleep(1)
   else:return {'id':row['id'],'read_error':str(code or type(e).__name__)}
with (OUT/'orders-aug-detail-private.jsonl').open('w') as f,concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
 for i,d in enumerate(pool.map(detail,records),1):
  if 'read_error' in d:errors.append(d)
  else:details.append(d)
  f.write(json.dumps(d,ensure_ascii=False)+'\n');f.flush()
  if i%100==0:print('Details',i,'/',len(records),'errors',len(errors),flush=True)
end_total=get(base,op,'/api/v1/frontend/order/total',p)
manifest={'started_at':started,'finished_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'method':'authenticated read-only Starter API','query':p,'total_before':total,'total_after':end_total,'list_rows':len(records),'detail_rows':len(details),'errors':errors,'date_format':'DD.MM.YYYY verified; ISO variants returned HTTP500','coverage':'done orders; August deliveryTime; full accessible tenant, observed shop ids checked separately','private_projection':'no customer, phone, email, address, payment credentials or free-text note retained','files_sha256':{n:hashlib.sha256((OUT/n).read_bytes()).hexdigest() for n in ['orders-aug-list-private.json','orders-aug-detail-private.jsonl']}}
(OUT/'starter-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
print('COMPLETE',len(details),'expected',total['total'],'errors',len(errors),flush=True)
