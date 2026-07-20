import os, json, base64, urllib.request
SP=os.path.dirname(os.path.abspath(__file__))
UT=open(os.path.join(SP,"user_token.txt")).read().strip()
PROJ="gen-lang-client-0105958041"; DB="proeis"
URL=f"https://firestore.googleapis.com/v1/projects/{PROJ}/databases/{DB}/documents:runQuery"
REAL="captcha_tests/real"; os.makedirs(REAL, exist_ok=True)
LBL_PATH="captcha_tests/train/real_labels.json"
labels=json.load(open(LBL_PATH)) if os.path.exists(LBL_PATH) else {}
CHARS=set("0123456789ABCDEF")

def query(cursor=None):
    sq={"from":[{"collectionId":"captcha_dataset"}],
        "where":{"fieldFilter":{"field":{"fieldPath":"accepted"},"op":"EQUAL","value":{"booleanValue":True}}},
        "orderBy":[{"field":{"fieldPath":"__name__"}}],
        "limit":300}
    if cursor: sq["startAt"]={"values":[{"referenceValue":cursor}],"before":False}
    body=json.dumps({"structuredQuery":sq}).encode()
    req=urllib.request.Request(URL,data=body,headers={"Authorization":f"Bearer {UT}","Content-Type":"application/json"})
    return json.load(urllib.request.urlopen(req,timeout=120))

added=0; total=0; last=None; skipped=0
while True:
    rows=query(last)
    got=0
    for r in rows:
        d=r.get("document")
        if not d: continue
        got+=1; total+=1; last=d["name"]
        f=d.get("fields",{})
        ans=f.get("answer",{}).get("stringValue","").strip().upper()
        img=f.get("image_b64",{}).get("stringValue","")
        if len(ans)!=6 or any(c not in CHARS for c in ans) or not img:
            skipped+=1; continue
        docid=d["name"].split("/")[-1]
        fn=f"fv_{docid}.png"
        p=os.path.join(REAL,fn)
        if not os.path.exists(p):
            try: open(p,"wb").write(base64.b64decode(img))
            except Exception: skipped+=1; continue
        if fn not in labels:
            labels[fn]=ans; added+=1
    if got<300: break
json.dump(labels,open(LBL_PATH,"w"),indent=1)
print(f"docs verificados lidos: {total} | novos rotulos verificados adicionados: {added} | pulados: {skipped}")
print(f"total de rotulos agora: {len(labels)}")
