import pymysql, configparser, re
cfg = configparser.ConfigParser()
cfg.read('config.ini', encoding='utf-8')
d = cfg['database']
conn = pymysql.connect(host=d['host'], port=int(d['port']), user=d['user'],
                       password=d['password'], database=d['database'], charset=d['charset'],
                       cursorclass=pymysql.cursors.DictCursor)
cur = conn.cursor()
cur.execute(
    "SELECT id, title, published_at, detail "
    "FROM scraping_infos WHERE title LIKE %s LIMIT 1",
    ('%龙江银行%贷款线上化%',)
)
row = cur.fetchone()
conn.close()

if not row:
    print('未找到记录')
else:
    content = row['detail'] or ''
    print('published_at:', row['published_at'])
    print()
    # 找所有日期格式出现的位置
    for m in re.finditer(r'\d{4}[-/年]\d{1,2}[-/月]\d{1,2}', content):
        start = max(0, m.start() - 30)
        end = min(len(content), m.end() + 30)
        print(f'[pos {m.start()}] ...{repr(content[start:end])}...')
    print()
    print('--- 正文全文 ---')
    print(content)
