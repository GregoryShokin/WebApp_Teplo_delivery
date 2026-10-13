"""Read-only Starter feedback snapshot; credentials/cookies never written to output."""
import argparse, ast, collections, hashlib, http.cookiejar, json, os, re, time
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = 'https://crm.starterapp.ru/fmteplo'
ENV_DEFAULT = '/Users/grigorii/Developer/Projects/Teplo-all-for-business/.env'
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None
class Client:
    def __init__(self, env):
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect())
        values = {}
        for line in Path(env).read_text().splitlines():
            m = re.match(r'^(?:export\s+)?(STARTER_[A-Z0-9_]+)\s*=\s*(.*)$', line)
            if m:
                v = m.group(2).strip()
                values[m.group(1)] = ast.literal_eval(v) if v[:1] in ('"', "'") else v
        if values.get('STARTER_BASE_URL') != BASE: raise RuntimeError('Unexpected destination')
        req = urllib.request.Request(BASE+'/api/v1/frontend/login', data=json.dumps({'username':values['STARTER_USERNAME'],'password':values['STARTER_PASSWORD']}).encode(), method='POST', headers={'Content-Type':'application/json'})
        values.clear()
        try:
            with self.opener.open(req, timeout=45) as r: code = r.status
        except urllib.error.HTTPError as e: code = e.code
        if code != 303 or not any(c.name == 'access_token' for c in jar): raise RuntimeError(f'Authentication not verified: HTTP {code}')
    def get(self, route, params=None):
        if route not in ('feedbacks','feedback/total','feedback/filters','feedback/tags'): raise ValueError('Unsupported read route')
        url = BASE+'/api/v1/frontend/'+route
        if params: url += '?'+urllib.parse.urlencode(params, doseq=True)
        for attempt in range(3):
            try:
                with self.opener.open(urllib.request.Request(url,headers={'Accept':'application/json'}),timeout=60) as r:
                    if r.status != 200 or 'application/json' not in r.headers.get('Content-Type',''): raise RuntimeError('Unexpected HTTP response')
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code not in (429,502,503,504) or attempt == 2: raise RuntimeError(f'Read {route}: HTTP {e.code}') from None
                time.sleep(2*(attempt+1))
        raise RuntimeError('Read failed')
def save(path, obj):
    with open(path,'x',encoding='utf-8') as f: json.dump(obj,f,ensure_ascii=False,indent=2);f.write('\n')
    path.chmod(0o600)
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--env',default=ENV_DEFAULT);ap.add_argument('--out',required=True);ap.add_argument('--limit',type=int,default=500);args=ap.parse_args()
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True);out.chmod(0o700)
    raw=out/'starter-feedback-private.jsonl'
    if raw.exists(): raise SystemExit('Use a new snapshot directory; existing data is not overwritten')
    os.umask(0o077)
    c=Client(args.env);started=datetime.now(timezone.utc).isoformat()
    total_before=c.get('feedback/total');filters=c.get('feedback/filters');tags=c.get('feedback/tags')
    save(out/'starter-filters.json',filters);save(out/'starter-tags.json',tags)
    ids=set();pages=[];metadata=[];dates=[];rating_counts=collections.Counter();tag_counts=collections.Counter();shops=collections.Counter();status_counts=collections.Counter();shop_addresses=collections.Counter();texts=0;tagged=0;offset=0;last_id=None
    with raw.open('x',encoding='utf-8') as f:
        while True:
            page=c.get('feedbacks',{'offset':offset,'limit':args.limit,'order_by':'id'})
            if not isinstance(page,list):raise RuntimeError('Expected feedback array')
            if not page:break
            for x in page:
                ident=x['id']
                if ident in ids or (last_id is not None and ident<=last_id):raise RuntimeError('Duplicate ID or unstable ascending order')
                ids.add(ident);last_id=ident
                order=x.get('order') or {};cust=x.get('customer') or {};shop=order.get('shop') or {}
                tagids=sorted(set(t['id'] for t in x.get('tags',[])))
                comment=x.get('comment') or ''
                # Original feedback text retained privately; customer profile/contact fields intentionally omitted.
                private={k:x.get(k) for k in ('id','createdAt','status','rating','comment','answer','media','pointsAwarded')}
                private.update({'customer_id':cust.get('id'),'order':{k:order.get(k) for k in ('id','starterId','posId','number','deliveryTime','shop')},'tags':x.get('tags',[])})
                f.write(json.dumps(private,ensure_ascii=False)+'\n')
                safe={'feedback_id':ident,'created_at':x['createdAt'],'rating':x['rating'],'status':x['status'],'shop_id':shop.get('id'),'shop_city':shop.get('city'),'shop_street':shop.get('street'),'has_text':bool(comment.strip()),'comment_chars':len(comment),'comment_sha256':hashlib.sha256(comment.encode()).hexdigest(),'tag_ids':tagids,'has_answer':bool(x.get('answer')),'media_count':len(x.get('media',[]))}
                metadata.append(safe);dates.append(x['createdAt']);rating_counts[x['rating']]+=1;tag_counts.update(tagids);shops[str(shop.get('id'))]+=1;shop_addresses[(shop.get('city'),shop.get('street'))]+=1;status_counts[x['status']]+=1;texts+=safe['has_text'];tagged+=bool(tagids)
            pages.append({'offset':offset,'returned':len(page),'first_id':page[0]['id'],'last_id':page[-1]['id']});offset+=len(page);f.flush()
            if len(pages)%5==0:print(json.dumps({'read':offset,'expected_start':total_before['total']}),flush=True)
            if offset>=total_before['total']:break
            time.sleep(.15)
    total_after=c.get('feedback/total');tail=c.get('feedbacks',{'offset':offset,'limit':1,'order_by':'id'})
    shop_totals={str(sid):c.get('feedback/total',{'shops':[sid]}) for sid in (3,105)}
    manifest={'source':'Starter feedback API','base_url':BASE,'started_at_utc':started,'finished_at_utc':datetime.now(timezone.utc).isoformat(),'scope':'all accessible tenant feedback; no date/rating/status/shop filters','method':'GET /api/v1/frontend/feedbacks?offset=N&limit=500&order_by=id','total_before':total_before,'total_after':total_after,'rows':len(metadata),'unique_feedback_ids':len(ids),'pages':pages,'tail_rows':len(tail),'complete':len(metadata)==total_before['total']==total_after['total'] and not tail,'date_min':min(dates) if dates else None,'date_max':max(dates) if dates else None,'rating_counts':dict(sorted(rating_counts.items())),'with_text':texts,'without_text':len(metadata)-texts,'tagged_rows':tagged,'tag_counts':{str(k):v for k,v in sorted(tag_counts.items())},'shop_id_counts_from_payload':dict(shops),'shop_address_counts':[{'city':k[0],'street':k[1],'count':v} for k,v in shop_addresses.items()],'shop_id_caveat':'Observed order.shop has city/street only, no id; use address and verify API shop filters separately.','shop_totals':shop_totals,'status_counts':dict(status_counts),'raw_sha256':hashlib.sha256(raw.read_bytes()).hexdigest(),'pii_rule':'Customer profile/contact fields omitted; original text/answers may contain personal data; keep private JSONL outside Git. Metadata has no text/customer/order identifiers.','interpretation':'A rating or native tag is not a manually verified complaint; tag assignment provenance not verified; accepted/processed are workflow statuses, not sentiment.'}

    with (out/'starter-feedback-metadata.jsonl').open('x',encoding='utf-8') as f:
        for row in metadata:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    save(out/'starter-manifest.json',manifest)
    print(json.dumps(manifest,ensure_ascii=False),flush=True)
    if not manifest['complete']:raise SystemExit('Snapshot incomplete or source changed; do not report full coverage')
if __name__=='__main__':main()
