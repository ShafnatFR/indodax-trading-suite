"""Indodax TAPI v2 client — read-only verification.
Signing per docs resmi: HMAC-SHA256(secret, query_string) di header Sign, X-APIKEY, timestamp ms + recvWindow.
Pakai IPv4 (Warp off), User-Agent browser, dan CookieJar utk lolos Cloudflare __cf_bm.
"""
import os, json, time, hmac, hashlib, urllib.parse, urllib.request, http.cookiejar

BASE = 'https://api.indodax.com'
KEY = os.environ['IDX_KEY']
SECRET = os.environ['IDX_SECRET']
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0 Safari/537.36')

# CookieJar + opener agar cookie Cloudflare (__cf_bm) diterima & dipantulkan
_cj = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cj))


def _request(url, data=None, method='GET', sign_sig=None):
    headers = {
        'Accept': 'application/json',
        'User-Agent': UA,
    }
    if sign_sig is not None:
        headers.update({
            'X-APIKEY': KEY,
            'Sign': sign_sig,
            'Content-Type': 'application/x-www-form-urlencoded',
        })
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    return _opener.open(req, timeout=30)


def signed_request(method, path, params=None):
    params = dict(params or {})
    params['timestamp'] = int(time.time() * 1000)
    params.setdefault('recvWindow', 5000)
    query = urllib.parse.urlencode(sorted(params.items()))
    sig = hmac.new(SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f'{BASE}{path}'
    data = None
    if method == 'GET':
        url += '?' + query
    else:
        data = query.encode()
    with _request(url, data=data, method=method, sign_sig=sig) as r:
        return json.loads(r.read().decode())


def public_request(path, params=None):
    q = urllib.parse.urlencode(params or {})
    url = f'{BASE}{path}' + (('?' + q) if q else '')
    with _request(url) as r:
        return json.loads(r.read().decode())


if __name__ == '__main__':
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else 'account'
    try:
        if mode == 'account':
            res = signed_request('GET', '/api/v2/account', {'omitZeroBalances': True})
            print(json.dumps({'status': 'success', 'response': res}, default=str))
        elif mode == 'server_time':
            res = public_request('/api/v2/time')
            print(json.dumps({'status': 'success', 'response': res}, default=str))
        else:
            print('unknown mode', mode)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors='ignore')
        print(json.dumps({'status': 'error', 'http': e.code, 'is_html': body.lstrip().startswith('<!'), 'body': body[:300]}, default=str))
    except Exception as e:
        print(json.dumps({'status': 'exception', 'type': type(e).__name__, 'msg': str(e)[:300]}, default=str))