import json,urllib.request,urllib.error,urllib.parse,http.cookiejar,datetime,hashlib
from pathlib import Path
import os

def env_read():
 out={}
 path=Path(os.environ.get('TEPLO_ENV_FILE', '/Users/grigorii/Developer/Projects/Teplo-all-for-business/.env'))
 for line in path.read_text().splitlines():
  if '=' not in line or line.lstrip().startswith('#'):continue
  k,v=line.split('=',1)
  if k.strip() in ['STARTER_BASE_URL','STARTER_USERNAME','STARTER_PASSWORD']:out[k.strip()]=v.strip().strip(chr(34)).strip(chr(39))
 return out
OUT=Path(os.environ.get('TEPLO_DISCOUNT_OUTPUT_DIR','/tmp/teplo-discount-audit'))
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*a,**k):return None

def connect():
 e=env_read();base=e['STARTER_BASE_URL'].rstrip('/')
 jar=http.cookiejar.CookieJar();op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar),NoRedirect())
 req=urllib.request.Request(base+'/api/v1/frontend/login',data=json.dumps({'username':e['STARTER_USERNAME'],'password':e['STARTER_PASSWORD']}).encode(),headers={'Content-Type':'application/json'},method='POST')
 try:
  with op.open(req,timeout=40) as r:status=r.status
 except urllib.error.HTTPError as ex:
  status=ex.code
  if status!=303:raise RuntimeError('Login HTTP '+str(status)) from None
 if not any(c.name=='access_token' for c in jar):raise RuntimeError('Login did not establish expected session')
 print('Login confirmed',status,flush=True)
 return base,op

def get(base,op,path,params=None):
 url=base+path+('?' + urllib.parse.urlencode(params,doseq=True) if params else '')
 with op.open(urllib.request.Request(url,headers={'Accept':'application/json'}),timeout=50) as r:
  if 'application/json' not in r.headers.get('Content-Type',''):raise ValueError('Non-JSON at '+path)
  return json.load(r)

LIST_KEYS=['id','posId','starterId','status','source','price','totalPrice','discountPrice','sumToPay','bonuses','mainBonusProgramAccountSpent','serviceFeePrice','deliveryTime','createdAt','doneAt','paymentMethod','discounts','shop','isDelivery']
DETAIL_KEYS=['id','posId','starterId','status','source','price','totalPrice','discountPrice','sumToPay','bonuses','mainBonusProgramAccountSpent','bonusProgramAccountsSpent','serviceFeePrice','deliveryTime','createdAt','doneAt','paymentMethod','discounts','orderItems','comboGroups','serviceFees','shop']
def project(d,detail=False):
 out={k:d[k] for k in (DETAIL_KEYS if detail else LIST_KEYS) if k in d}
 if isinstance(out.get('shop'),dict):out['shop']={k:out['shop'].get(k) for k in ['id','title','name'] if k in out['shop']}
 # Allowlist item structure; omit arbitrary descriptions/comments, customer/contact/payment credentials.
 if 'orderItems' in out:
  out['orderItems']=[{k:x.get(k) for k in ['code','price','discountPrice','totalPrice','quantity','comboGroupId','comboName','comboDiscount','comboItemPrice'] if k in x} | {'menuItem':{k:x.get('menuItem',{}).get(k) for k in ['code','name']}} for x in out['orderItems']]
 if 'discounts' in out:
  out['discounts']=[{k:x.get(k) for k in ['id','name','sum','type','meal','code'] if k in x} for x in out['discounts']]
 out.pop('comboGroups',None);out.pop('bonusProgramAccountsSpent',None);out.pop('serviceFees',None)
 return out
