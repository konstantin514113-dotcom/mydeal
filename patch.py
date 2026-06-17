import re
with open('main.py','r',encoding='utf-8') as f:
    c=f.read()
print("BOOKING_HTML_B64 найден:", 'BOOKING_HTML_B64' in c)
m=re.search(r'BOOKING_HTML_B64\s*=\s*"([A-Za-z0-9+/=]{20})', c)
if m: print("Начало:", m.group(1))
