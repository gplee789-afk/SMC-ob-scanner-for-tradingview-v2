"""抓取上市、上櫃的每日行情與三大法人買賣超，存成 data/YYYY-MM-DD.json，並更新 data/index.json。

用法：
    python scripts/fetch_daily.py                 # 台北時間今天
    python scripts/fetch_daily.py 2026-10-01 ...  # 指定日期（可多個，用來補資料）

只用標準函式庫。非交易日（證交所回傳無資料）時不寫檔、正常結束；
行情抓不到時以錯誤結束，讓 GitHub Actions 標示失敗。三大法人抓不到時只警告，該欄存 null。
"""
import datetime
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / 'data'
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; twstock-smc-scanner)'}
CODE = re.compile(r'^[1-9]\d{3}$')        # 四位數普通股，與 index.html 的篩選相同
TAIPEI = datetime.timezone(datetime.timedelta(hours=8))

QUOTE_FIELDS = ['code', 'name', 'amount', 'shares', 'close', 'high', 'low', 'change']
INST_FIELDS = ['code', 'net']


def fetch_json(url, form=None):
    body = urllib.parse.urlencode(form).encode() if form else None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=body, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode('utf-8'))
        except Exception as e:                  # 連線逾時、被暫時擋下等，稍等重試
            if attempt == 2:
                raise
            print(f'  重試（{e}）', file=sys.stderr)
            time.sleep(10 * (attempt + 1))


def num(s):
    """'1,234'、'+53.00'、'-0.04 '、'--'、'<p style=...>+</p>' → float 或 None"""
    s = re.sub(r'<[^>]+>', '', str(s)).replace(',', '').strip()
    try:
        return float(s)
    except ValueError:
        return None


def whole(v):
    return None if v is None else int(v)


def columns(fields, names):
    missing = [n for n in names if n not in fields]
    if missing:
        raise RuntimeError(f'欄位不見了：{missing}，格式可能改版')
    return [fields.index(n) for n in names]


def find_table(tables, *names):
    return [t for t in tables if all(n in (t.get('fields') or []) for n in names)]


# ── 證交所 ──

def twse_quote(d):
    j = fetch_json(f'https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={d:%Y%m%d}&type=ALLBUT0999&response=json')
    if j.get('stat') != 'OK':
        return None                              # 非交易日
    tables = find_table(j.get('tables', []), '證券代號', '成交金額')
    if not tables:
        raise RuntimeError('MI_INDEX 找不到個股行情表，格式可能改版')
    t = tables[0]
    ic, iname, ish, iamt, icl, ihi, ilo, isg, ich = columns(
        t['fields'], ['證券代號', '證券名稱', '成交股數', '成交金額', '收盤價', '最高價', '最低價', '漲跌(+/-)', '漲跌價差'])
    rows = []
    for r in t['data']:
        code = r[ic].strip()
        if not CODE.match(code):
            continue
        sign = re.sub(r'<[^>]+>', '', r[isg]).strip()
        change = num(r[ich])
        if change is not None:
            change = None if sign == 'X' else -change if sign == '-' else change   # X 為不比價
        rows.append([code, r[iname].strip(), whole(num(r[iamt])), whole(num(r[ish])),
                     num(r[icl]), num(r[ihi]), num(r[ilo]), change])
    return rows


def twse_inst(d):
    j = fetch_json(f'https://www.twse.com.tw/rwd/zh/fund/T86?date={d:%Y%m%d}&selectType=ALLBUT0999&response=json')
    if j.get('stat') != 'OK':
        return None
    ic, iv = columns(j['fields'], ['證券代號', '三大法人買賣超股數'])
    return [[r[ic].strip(), whole(num(r[iv]))] for r in j['data'] if CODE.match(r[ic].strip())]


# ── 櫃買中心 ──

def tpex_post(path, form, d):
    j = fetch_json('https://www.tpex.org.tw' + path, {**form, 'date': f'{d:%Y/%m/%d}', 'id': '', 'response': 'json'})
    if str(j.get('stat', '')).lower() != 'ok' or j.get('date') != f'{d:%Y%m%d}':
        return None
    return j.get('tables', [])


def tpex_quote(d):
    tables = tpex_post('/www/zh-tw/afterTrading/dailyQuotes', {}, d)
    if tables is None:
        return None
    tables = find_table(tables, '代號', '成交金額(元)')       # 一般股票與管理股票兩張表
    if not tables:
        raise RuntimeError('上櫃行情找不到個股表，格式可能改版')
    rows = []
    for t in tables:
        ic, iname, icl, ich, ihi, ilo, ish, iamt = columns(
            t['fields'], ['代號', '名稱', '收盤', '漲跌', '最高', '最低', '成交股數', '成交金額(元)'])
        for r in t.get('data') or []:
            code = r[ic].strip()
            if CODE.match(code):
                rows.append([code, r[iname].strip(), whole(num(r[iamt])), whole(num(r[ish])),
                             num(r[icl]), num(r[ihi]), num(r[ilo]), num(r[ich])])
    return rows


def tpex_inst(d):
    tables = tpex_post('/www/zh-tw/insti/dailyTrade', {'type': 'Daily', 'sect': 'EW'}, d)
    if tables is None:
        return None
    tables = find_table(tables, '代號', '三大法人買賣超股數合計')
    if not tables:
        return None
    ic, iv = columns(tables[0]['fields'], ['代號', '三大法人買賣超股數合計'])
    return [[r[ic].strip(), whole(num(r[iv]))] for r in tables[0]['data'] if CODE.match(r[ic].strip())]


# ── 主流程 ──

def optional(label, fn, d):
    try:
        rows = fn(d)
    except Exception as e:
        print(f'  警告：{label}抓取失敗（{e}），先存成 null', file=sys.stderr)
        return None
    if rows is None:
        print(f'  警告：{label}尚未公布，先存成 null', file=sys.stderr)
    return rows


def fetch_day(d):
    print(f'{d:%Y-%m-%d}')
    tq = twse_quote(d)
    if tq is None:
        print('  證交所沒有這天的資料（非交易日），略過')
        return False
    time.sleep(3)                                # 證交所要求放慢請求頻率
    ti = optional('上市三大法人', twse_inst, d)
    tp = tpex_quote(d)
    if tp is None:
        raise RuntimeError('證交所有資料，但櫃買沒有這天的行情')
    pi = optional('上櫃三大法人', tpex_inst, d)

    day = {
        'date': f'{d:%Y-%m-%d}',
        'fields': {'quote': QUOTE_FIELDS, 'inst': INST_FIELDS},
        'TWSE': {'quote': tq, 'inst': ti},
        'TPEX': {'quote': tp, 'inst': pi},
    }
    DATA.mkdir(exist_ok=True)
    (DATA / f'{d:%Y-%m-%d}.json').write_text(
        json.dumps(day, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print(f'  上市 {len(tq)} 檔、法人 {len(ti) if ti else "無"}；上櫃 {len(tp)} 檔、法人 {len(pi) if pi else "無"}')
    return True


def write_index():
    dates = sorted((p.stem for p in DATA.glob('*.json') if re.fullmatch(r'\d{4}-\d{2}-\d{2}', p.stem)), reverse=True)
    (DATA / 'index.json').write_text(json.dumps({'dates': dates}, separators=(',', ':')), encoding='utf-8')


def main(args):
    if args:
        days = []
        for a in args:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', a):
                sys.exit(f'日期格式要是 YYYY-MM-DD：{a}')
            days.append(datetime.date.fromisoformat(a))
    else:
        days = [datetime.datetime.now(TAIPEI).date()]
    for i, d in enumerate(days):
        if i:
            time.sleep(5)
        fetch_day(d)
    if DATA.exists():
        write_index()


if __name__ == '__main__':
    main(sys.argv[1:])
