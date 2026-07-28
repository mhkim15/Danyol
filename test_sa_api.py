"""SA API 403 디버그 테스트"""
import base64
import hashlib
import hmac
import time
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests

ak = os.environ.get('NAVER_AD_API_KEY', '')
sk = os.environ.get('NAVER_AD_SECRET_KEY', '')
cid = os.environ.get('NAVER_AD_CUSTOMER_ID', '')

print(f"ak length: {len(ak)}")
print(f"sk: {sk}")
print(f"cid: {cid}")

ts = str(int(time.time() * 1000))
uri = '/keywordstool'
method = 'GET'
message = f'{ts}.{method}.{uri}'.encode('utf-8')
print(f"message: {message}")

raw_secret = base64.b64decode(sk)
print(f"raw_secret length: {len(raw_secret)}")

sig = base64.b64encode(hmac.new(raw_secret, message, hashlib.sha256).digest()).decode('utf-8')
print(f"signature: {sig}")

headers = {
    'X-Timestamp': ts,
    'X-API-KEY': ak,
    'X-Customer': str(cid),
    'X-Signature': sig,
    'Content-Type': 'application/json',
}
params = {'hintKeywords': '주방용품', 'showDetail': '1'}

print("\n--- Request ---")
print(f"URL: https://api.searchad.naver.com{uri}")
print(f"Params: {params}")
print(f"Headers: {headers}")

resp = requests.get('https://api.searchad.naver.com' + uri, headers=headers, params=params, timeout=15)
print(f"\n--- Response ---")
print(f"Status: {resp.status_code}")
print(f"Body: {resp.text[:1000]}")
