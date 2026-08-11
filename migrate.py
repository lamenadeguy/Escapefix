import pymysql
import json
import time

# --- подключения ---
old_db = pymysql.connect(host='127.0.0.1', user='escapekuzzyuser',
                          password='ПАРОЛЬ_К_escapekuzzy', database='escapekuzzy',
                          charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)
new_db = pymysql.connect(host='127.0.0.1', user='vichanuser',
                          password='ПАРОЛЬ_К_vichan', database='vichan_test',
                          charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)

old_cur = old_db.cursor()
new_cur = new_db.cursor()

# --- 1. борды ---
old_cur.execute("SELECT id, name FROM boards WHERE deleted=0")
boards = old_cur.fetchall()

for b in boards:
    uri = b['id']
    title = b['name']
    new_cur.execute(
        "INSERT IGNORE INTO boards (uri, title, subtitle) VALUES (%s, %s, %s)",
        (uri, title, None)
    )
    # таблица постов под борду — реальная схема Sharty (templates/posts.sql из вашего архива)
    new_cur.execute(f"""
        CREATE TABLE IF NOT EXISTS `posts_{uri}` (
           `id` int(11) unsigned NOT NULL AUTO_INCREMENT,
           `thread` int(11) DEFAULT NULL,
           `subject` varchar(100) DEFAULT NULL,
           `email` varchar(30) DEFAULT NULL,
           `name` varchar(35) DEFAULT NULL,
           `trip` varchar(15) DEFAULT NULL,
           `capcode` varchar(50) DEFAULT NULL,
           `body` text NOT NULL,
           `body_nomarkup` text,
           `time` int(11) NOT NULL,
           `bump` int(11) DEFAULT NULL,
           `files` text DEFAULT NULL,
           `num_files` int(11) DEFAULT 0,
           `filehash` text CHARACTER SET ascii,
           `password` varchar(20) DEFAULT NULL,
           `ip` varchar(39) CHARACTER SET ascii NOT NULL,
           `sticky` int(1) NOT NULL DEFAULT 0,
           `locked` int(1) NOT NULL DEFAULT 0,
           `cycle` int(1) NOT NULL DEFAULT 0,
           `sage` int(1) NOT NULL DEFAULT 0,
           `embed` text,
           `slug` varchar(256) DEFAULT NULL,
           UNIQUE KEY `id` (`id`),
           KEY `thread_id` (`thread`, `id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
    """)
new_db.commit()

# --- 2. посты, по бордам ---
# ВАЖНО: реальные колонки escapechan.posts — timestamp (unix int, не created_at),
# comment / comment_parsed (не title), subject (есть отдельно), sticky, closed,
# files varchar(1800) (формат зависит от вашей реализации — см. convert_files).

def convert_files(files_raw):
    """
    Поле files в escapechan.posts — varchar(1800). Пробуем распарсить как JSON,
    если не выходит — считаем, что реального файла нет (null / пусто).
    ПЕРЕД боевым прогоном: возьмите один реальный пост с файлом
    (SELECT files FROM posts WHERE files IS NOT NULL AND files!='null' LIMIT 1;)
    и пришлите мне пример — допишу маппинг под точный формат.
    """
    if not files_raw or files_raw == 'null':
        return None, 0
    try:
        files = json.loads(files_raw)
        if not isinstance(files, list):
            files = [files]
    except Exception:
        return None, 0

    out = []
    for f in files:
        out.append({
            "file": (f.get("path") or "").split("/")[-1],
            "thumb": (f.get("thumbnail") or "").split("/")[-1],
            "file_original": f.get("fullname") or f.get("displayname") or "",
            "width": f.get("width", 0),
            "height": f.get("height", 0),
            "size": f.get("size", 0),
        })
    return (json.dumps(out) if out else None), len(out)

for b in boards:
    uri = b['id']
    old_cur.execute("SELECT * FROM posts WHERE board=%s AND deleted=0 ORDER BY id ASC", (uri,))
    posts = old_cur.fetchall()

    num_to_new_id = {}
    op_posts = [p for p in posts if p['parent'] == 0]
    reply_posts = [p for p in posts if p['parent'] != 0]

    for p in op_posts:
        files_json, num_files = convert_files(p.get('files'))
        new_cur.execute(f"""
            INSERT INTO `posts_{uri}`
            (thread, subject, name, trip, body, body_nomarkup, time,
             bump, files, num_files, ip, sticky, locked)
            VALUES (NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            p.get('subject') or None,
            p.get('name') or 'Аноним',
            p.get('trip') or None,
            p.get('comment') or '',
            p.get('comment_parsed') or p.get('comment') or '',
            int(p.get('timestamp') or time.time()),
            int(p.get('timestamp') or time.time()),
            files_json,
            num_files,
            p.get('ip') or '127.0.0.1',
            1 if p.get('sticky') else 0,
            1 if p.get('closed') else 0,
        ))
        new_id = new_cur.lastrowid
        num_to_new_id[p['num']] = new_id

    new_db.commit()

    for p in reply_posts:
        parent_new_id = num_to_new_id.get(p['parent'])
        if not parent_new_id:
            continue
        files_json, num_files = convert_files(p.get('files'))
        new_cur.execute(f"""
            INSERT INTO `posts_{uri}`
            (thread, name, trip, body, body_nomarkup, time, files, num_files, ip)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            parent_new_id,
            p.get('name') or 'Аноним',
            p.get('trip') or None,
            p.get('comment') or '',
            p.get('comment_parsed') or p.get('comment') or '',
            int(p.get('timestamp') or time.time()),
            files_json,
            num_files,
            p.get('ip') or '127.0.0.1',
        ))
        num_to_new_id[p['num']] = new_cur.lastrowid

    new_db.commit()
    print(f"/{uri}/: перенесено {len(op_posts)} тредов, {len(reply_posts)} ответов")

# --- 3. баны ---
# Реальная escapechan.bans: ip_subnet (не ip), board, reason, timestamp, end, canceled.
# Реальная vichan(sharty).bans: ipstart/ipend varbinary(16), created, expires, board, creator, reason, seen.
import socket

def ip_to_varbinary(ip_str):
    ip_str = (ip_str or '').split('/')[0]  # обрезать /24 и т.п. если есть в ip_subnet
    try:
        return socket.inet_pton(socket.AF_INET, ip_str)
    except OSError:
        try:
            return socket.inet_pton(socket.AF_INET6, ip_str)
        except OSError:
            return None

old_cur.execute("SELECT * FROM bans WHERE canceled=0")
for ban in old_cur.fetchall():
    ip_bin = ip_to_varbinary(ban.get('ip_subnet', ''))
    if not ip_bin:
        continue
    expires = ban.get('end') or None
    new_cur.execute("""
        INSERT INTO bans (ipstart, ipend, created, expires, board, creator, reason, seen)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 0)
    """, (
        ip_bin, ip_bin,
        int(ban.get('timestamp') or time.time()),
        expires,
        ban.get('board') or None,
        1,
        ban.get('reason') or 'imported',
    ))
new_db.commit()

print("Готово. Проверьте данные вручную перед переключением nginx.")

old_db.close()
new_db.close()
