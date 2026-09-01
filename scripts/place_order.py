"""One-off: tempatkan order beli limit LIVE di Indodax (SKR/IDR), pakai klien V2 di ~/idx_v2_client.py."""
import os, sys, json
sys.path.insert(0, os.path.expanduser('~'))
from idx_v2_client import signed_request

symbol = sys.argv[1] if len(sys.argv) > 1 else 'SKRIDR'
side = sys.argv[2] if len(sys.argv) > 2 else 'BUY'
price = sys.argv[3] if len(sys.argv) > 3 else '475'
qty = sys.argv[4] if len(sys.argv) > 4 else '101'

res = signed_request('POST', '/api/v2/order', {
    'symbol': symbol, 'side': side, 'type': 'LIMIT',
    'price': price, 'quantity': qty})
print(json.dumps({'status': 'success', 'response': res}, default=str))
