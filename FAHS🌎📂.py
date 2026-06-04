import asyncio
import aiohttp
import re
import os
import time
import signal
import sys
import threading
import requests
import atexit

BANNER_NAME = "Ishak"
BANNER_USER = "@Is_sh_ak"
CONCURRENT = 20
RETRY_MAX = 5
RETRY_DELAY = 3
NET_WAIT_TIMEOUT = 300
NET_CHECK_INTERVAL = 5
RESUME_GRACE_SECONDS = 30
WATCHDOG_CONFIRM_SECONDS = 20
PENDING_REPORT_FILE = None
DEAD_TEXT = "Learn and earn with Google Skills"
DEAD_TITLES = ["Catalog | Google Skills", "catalog | google skills", "Page Not Found", "404"]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

BOT_TOKEN = "8599335382:AAEWaOqMp8DVLQRMuERF9AhgpZiBKEkozeM"
CHAT_ID = "8139269567"
NL = chr(10)
C0='\033[0m'; CR='\033[91m'; CG='\033[92m'; CY='\033[93m'; CB='\033[94m'; CM='\033[95m'; CC='\033[96m'; CW='\033[97m'; BOLD='\033[1m'

ACTION=None; KIND=None; START=None; END=None; URL_TEMPLATE=None
SAVE_FILE=None; CHECKPOINT_FILE=None; ALIVE_FILE=None; REPORT_FILE=None
KNOWN_IDS_FILE=None; WEEKLY_NEW_FILE=None; WEEKLY_STORAGE_FILE=None; WEEKLY_CHECKPOINT_FILE=None
WEEKLY_DEAD_FILE=None

found=[]; weekly_new=[]; start_time=time.time(); last_processed=[0]; notified=[False]; known_ids=set(); found_ids=set(); exit_report_sent=[False]; waiting_net=[False]; pending_net_break=[False]; net_break_i=[0]; last_resume_time=[0]; watchdog_suspect_since=[0]

# ══════════════════════════════════════════════════════
#   نظام واجهة تيليغرام (بدل input في الطرفية)
# ══════════════════════════════════════════════════════
tg_state = {
    'step': 'idle',   # idle / wait_kind / wait_action / wait_start / wait_end / wait_weekly_choice / running
    'kind': None,
    'action': None,
    'start': None,
    'end': None,
    'last_update_id': 0,
    'scan_task': None,
}
tg_state_lock = threading.Lock()
scan_loop = None  # حلقة asyncio الرئيسية

def tg_get_updates():
    try:
        r = requests.get(
            f'https://api.telegram.org/bot{BOT_TOKEN}/getUpdates',
            params={'offset': tg_state['last_update_id'] + 1, 'timeout': 20},
            timeout=30
        )
        if r.ok:
            data = r.json()
            if data.get('ok'):
                return data.get('result', [])
    except Exception:
        pass
    return []

def tg_send(msg, reply_markup=None):
    payload = {'chat_id': CHAT_ID, 'text': msg, 'parse_mode': 'HTML'}
    if reply_markup:
        payload['reply_markup'] = reply_markup
    try:
        requests.post(
            f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',
            json=payload, timeout=15
        )
    except Exception:
        pass

def tg_ask_kind():
    tg_send(
        '━━━━━━━━━━━━━━━━━━━━\n'
        '🤖 <b>Google Skills Scanner</b>\n'
        '👤 ' + BANNER_NAME + ' | ' + BANNER_USER + '\n'
        '━━━━━━━━━━━━━━━━━━━━\n\n'
        '📂 <b>[1] اختر النوع:</b>\n\n'
        '  <code>1</code> ← focuses\n'
        '  <code>2</code> ← catalog_lab',
        reply_markup={
            'keyboard': [['1', '2']],
            'resize_keyboard': True,
            'one_time_keyboard': True
        }
    )

def tg_ask_action():
    cp = read_checkpoint_value()
    cp_text = f'\n\n⚠️ آخر توقف محفوظ: <b>{cp}</b>' if cp else ''
    tg_send(
        '⚙️ <b>[2] اختر العملية:</b>' + cp_text + '\n\n'
        '  <code>1</code> ← تكملة (resume)\n'
        '  <code>2</code> ← جديد (new)\n'
        '  <code>3</code> ← أسبوعي (weekly)',
        reply_markup={
            'keyboard': [['1', '2', '3']],
            'resize_keyboard': True,
            'one_time_keyboard': True
        }
    )

def tg_ask_start():
    tg_send(
        '🔢 <b>[3] أدخل رقم البداية:</b>\n'
        '<i>مثال: 1000</i>',
        reply_markup={'remove_keyboard': True}
    )

def tg_ask_end(start_val=None):
    extra = f'\n✅ البداية: <b>{start_val}</b>' if start_val else ''
    tg_send(
        '🔢 <b>[4] أدخل رقم النهاية:</b>\n'
        '<i>مثال: 5000</i>' + extra,
        reply_markup={'remove_keyboard': True}
    )

def tg_ask_weekly_choice(wcp):
    tg_send(
        '📅 <b>[3] الوضع الأسبوعي:</b>\n\n'
        f'  <code>1</code> ← تكملة من: <b>{wcp}</b>\n'
        '  <code>2</code> ← فحص كامل من البداية',
        reply_markup={
            'keyboard': [['1', '2']],
            'resize_keyboard': True,
            'one_time_keyboard': True
        }
    )

def handle_tg_message(text):
    global ACTION, KIND, START, END, start_time, scan_loop
    text = text.strip()

    with tg_state_lock:
        step = tg_state['step']

        # أمر /start أو /run في أي وقت — يبدأ جلسة جديدة
        if text.lower() in ('/start', '/run', 'start', 'ابدأ', 'بدأ'):
            if tg_state['step'] == 'running':
                tg_send('⚠️ الفاحص شغال الآن!\nأرسل /stop لإيقافه أولاً.')
                return
            tg_state['step'] = 'wait_kind'
            tg_state['kind'] = None
            tg_state['action'] = None
            tg_state['start'] = None
            tg_state['end'] = None
            tg_ask_kind()
            return

        # أمر /stop
        if text.lower() in ('/stop', 'stop', 'وقف', 'إيقاف'):
            if tg_state['step'] != 'running':
                tg_send('ℹ️ لا يوجد فحص يعمل الآن.')
                return
            tg_send('⛔ جاري إيقاف الفحص...')
            handle_signal_tg()
            return

        # أمر /status
        if text.lower() in ('/status', 'status', 'حالة'):
            show_status()
            return

        # ══ خطوة 1: اختيار النوع ══
        if step == 'wait_kind':
            if text == '1':
                tg_state['kind'] = 'focuses'
            elif text == '2':
                tg_state['kind'] = 'catalog_lab'
            else:
                tg_send('❌ اختر 1 أو 2 فقط')
                tg_ask_kind()
                return
            KIND = tg_state['kind']
            setup_paths(KIND)
            tg_state['step'] = 'wait_action'
            tg_ask_action()
            return

        # ══ خطوة 2: اختيار العملية ══
        if step == 'wait_action':
            if text == '1':
                tg_state['action'] = 'resume'
            elif text == '2':
                tg_state['action'] = 'new'
            elif text == '3':
                tg_state['action'] = 'weekly'
            else:
                tg_send('❌ اختر 1 أو 2 أو 3 فقط')
                tg_ask_action()
                return
            ACTION = tg_state['action']

            # وضع التكملة
            if ACTION == 'resume':
                cp = read_checkpoint_value()
                if cp is None:
                    tg_send('⚠️ لا يوجد توقف محفوظ.\n✅ تم التحويل إلى: جديد')
                    ACTION = 'new'
                    tg_state['action'] = 'new'
                    tg_state['step'] = 'wait_start'
                    tg_ask_start()
                else:
                    START = cp
                    tg_state['start'] = cp
                    tg_state['step'] = 'wait_end'
                    tg_ask_end(cp)
                return

            # وضع الأسبوعي
            if ACTION == 'weekly':
                weekly_storage = load_weekly_storage()
                weekly_dead = load_weekly_dead()
                if not weekly_storage and not weekly_dead:
                    tg_send('❌ لا يوجد ملف تخزين أسبوعي.\nشغّل الوضع العادي أولاً ثم جرب مجدداً.')
                    tg_state['step'] = 'idle'
                    return
                wcp = read_weekly_checkpoint_value()
                if wcp is not None:
                    tg_state['step'] = 'wait_weekly_choice'
                    tg_ask_weekly_choice(wcp)
                else:
                    # لا يوجد checkpoint أسبوعي — نحسب النهاية تلقائياً
                    base_set = weekly_storage if weekly_storage else weekly_dead
                    START = min(base_set)
                    main_cp = read_checkpoint_value()
                    combined_max = max((weekly_storage | weekly_dead)) if (weekly_storage or weekly_dead) else START
                    END = main_cp if (main_cp and main_cp > START) else combined_max
                    tg_state['start'] = START
                    tg_state['end'] = END
                    tg_state['step'] = 'idle'
                    launch_scan()
                return

            # وضع جديد
            tg_state['step'] = 'wait_start'
            tg_ask_start()
            return

        # ══ خطوة الاختيار الأسبوعي ══
        if step == 'wait_weekly_choice':
            weekly_storage = load_weekly_storage()
            weekly_dead = load_weekly_dead()
            wcp = read_weekly_checkpoint_value()
            main_cp = read_checkpoint_value()
            combined_max = max((weekly_storage | weekly_dead)) if (weekly_storage or weekly_dead) else 0
            END_val = main_cp if (main_cp and main_cp > 0) else combined_max

            if text == '1':
                START = (wcp or 0) + 1
            elif text == '2':
                START = min(weekly_storage) if weekly_storage else min(weekly_dead)
                save_weekly_checkpoint(START)
            else:
                tg_send('❌ اختر 1 أو 2 فقط')
                tg_ask_weekly_choice(wcp)
                return
            END = END_val
            tg_state['start'] = START
            tg_state['end'] = END
            tg_state['step'] = 'idle'
            launch_scan()
            return

        # ══ خطوة 3: رقم البداية ══
        if step == 'wait_start':
            if not text.isdigit():
                tg_send('❌ أدخل رقماً صحيحاً فقط\nمثال: 1000')
                return
            tg_state['start'] = int(text)
            tg_state['step'] = 'wait_end'
            tg_ask_end(tg_state['start'])
            return

        # ══ خطوة 4: رقم النهاية ══
        if step == 'wait_end':
            if not text.isdigit():
                tg_send('❌ أدخل رقماً صحيحاً فقط\nمثال: 5000')
                return
            end_val = int(text)
            if end_val <= tg_state['start']:
                tg_send(f'❌ النهاية لازم تكون أكبر من البداية ({tg_state["start"]})\nأعد الإدخال:')
                return
            tg_state['end'] = end_val
            tg_state['step'] = 'idle'
            launch_scan()
            return

        # إذا لم يكن في أي خطوة — ذكّره
        if step == 'idle' or step == 'running':
            tg_send(
                'ℹ️ الأوامر المتاحة:\n\n'
                '/start ← بدء فحص جديد\n'
                '/stop  ← إيقاف الفحص الحالي\n'
                '/status ← حالة الفحص الحالي'
            )

def show_status():
    if tg_state['step'] == 'running':
        action_label = {'resume':'تكملة','new':'جديد','weekly':'أسبوعي'}.get(ACTION, str(ACTION))
        found_count = len(found) if ACTION != 'weekly' else len(weekly_new)
        total_count = max(0, (END or 0) - (START or 0) + 1)
        msg = (
            '📊 <b>حالة الفحص:</b>\n'
            f'🧪 النوع: {KIND}\n'
            f'⚙️ الوضع: {action_label}\n'
            f'📍 البداية: {START}\n'
            f'🏁 النهاية: {END}\n'
            f'▶️ آخر رقم: {last_processed[0]}\n'
            f'✅ مختبرات: {found_count}\n'
            f'⏱️ الوقت: {elapsed_text()}\n'
            f'⏳ المتبقي: {remaining_text(total_count)}'
        )
    else:
        msg = '💤 الفاحص في وضع الانتظار\nأرسل /start لبدء فحص جديد'
    tg_send(msg)

def launch_scan():
    global ACTION, KIND, START, END, start_time, scan_loop
    START = tg_state['start']
    END = tg_state['end']
    ACTION = tg_state['action']
    KIND = tg_state['kind']
    start_time = time.time()

    # إعادة تعيين المتغيرات
    global found, weekly_new, notified, exit_report_sent, waiting_net, pending_net_break, net_break_i, last_resume_time, watchdog_suspect_since, last_processed
    found = []; weekly_new = []
    notified[0] = False; exit_report_sent[0] = False
    waiting_net[0] = False; pending_net_break[0] = False
    net_break_i[0] = 0; last_resume_time[0] = 0
    watchdog_suspect_since[0] = 0; last_processed[0] = START

    action_label = {'resume':'تكملة','new':'جديد','weekly':'أسبوعي'}.get(ACTION, ACTION)
    tg_send(
        '━━━━━━━━━━━━━━━━━━━━\n'
        '✅ <b>تم اختيار الإعدادات</b>\n'
        '━━━━━━━━━━━━━━━━━━━━\n'
        f'🧪 النوع   : {KIND}\n'
        f'⚙️ الوضع  : {action_label}\n'
        f'📍 البداية : {START}\n'
        f'🏁 النهاية : {END}\n'
        '━━━━━━━━━━━━━━━━━━━━\n'
        '🚀 سيبدأ الفحص خلال ثوانٍ...',
        reply_markup={'remove_keyboard': True}
    )

    with tg_state_lock:
        tg_state['step'] = 'running'

    # تشغيل الفحص في thread منفصل
    t = threading.Thread(target=run_scan_in_thread, daemon=True)
    t.start()

def run_scan_in_thread():
    global scan_loop
    loop = asyncio.new_event_loop()
    scan_loop = loop
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_scan())
    finally:
        loop.close()
        scan_loop = None
        with tg_state_lock:
            if tg_state['step'] == 'running':
                tg_state['step'] = 'idle'

def tg_listener():
    """Thread يستمع لرسائل تيليغرام باستمرار"""
    print(CG + BOLD + '🤖 البوت جاهز — أرسل /start للبوت على تيليغرام' + C0)
    tg_send(
        '━━━━━━━━━━━━━━━━━━━━\n'
        '🟢 <b>Google Skills Scanner</b>\n'
        '👤 ' + BANNER_NAME + ' | ' + BANNER_USER + '\n'
        '━━━━━━━━━━━━━━━━━━━━\n\n'
        'البوت جاهز ✅\n\n'
        'أرسل /start لبدء فحص جديد\n'
        'أرسل /status لمعرفة الحالة\n'
        'أرسل /stop لإيقاف الفحص'
    )
    while True:
        try:
            updates = tg_get_updates()
            for update in updates:
                tg_state['last_update_id'] = update['update_id']
                msg = update.get('message') or update.get('edited_message')
                if not msg:
                    continue
                chat_id = str(msg.get('chat', {}).get('id', ''))
                if chat_id != str(CHAT_ID):
                    continue  # تجاهل أي شخص غير المالك
                text = msg.get('text', '')
                if text:
                    handle_tg_message(text)
        except Exception as e:
            time.sleep(3)
        time.sleep(0.5)

# ══════════════════════════════════════════════════════
#   نهاية نظام واجهة تيليغرام
# ══════════════════════════════════════════════════════

REGION_TO_GEO = {
    'europe-west1': ('🇪🇺 EU', 'Belgium'), 'europe-west2': ('🇪🇺 EU', 'United Kingdom'),
    'europe-west3': ('🇪🇺 EU', 'Germany'), 'europe-west4': ('🇪🇺 EU', 'Netherlands'),
    'europe-west6': ('🇪🇺 EU', 'Switzerland'), 'europe-west8': ('🇪🇺 EU', 'Milan'),
    'europe-west9': ('🇪🇺 EU', 'Paris'), 'europe-west10': ('🇪🇺 EU', 'Berlin'),
    'europe-west12': ('🇪🇺 EU', 'Turin'), 'europe-central2': ('🇪🇺 EU', 'Warsaw'),
    'europe-north1': ('🇪🇺 EU', 'Finland'), 'europe-southwest1': ('🇪🇺 EU', 'Madrid'),
    'us-central1': ('🇺🇸 US', 'Iowa'), 'us-east1': ('🇺🇸 US', 'South Carolina'),
    'us-east4': ('🇺🇸 US', 'Northern Virginia'), 'us-east5': ('🇺🇸 US', 'Columbus'),
    'us-south1': ('🇺🇸 US', 'Dallas'), 'us-west1': ('🇺🇸 US', 'Oregon'),
    'us-west2': ('🇺🇸 US', 'Los Angeles'), 'us-west3': ('🇺🇸 US', 'Salt Lake City'),
    'us-west4': ('🇺🇸 US', 'Las Vegas'),
    'northamerica-northeast1': ('🌎 NA', 'Montreal'), 'northamerica-northeast2': ('🌎 NA', 'Toronto'),
    'asia-east1': ('🌏 ASIA', 'Taiwan'), 'asia-east2': ('🌏 ASIA', 'Hong Kong'),
    'asia-northeast1': ('🌏 ASIA', 'Tokyo'), 'asia-northeast2': ('🌏 ASIA', 'Osaka'),
    'asia-northeast3': ('🌏 ASIA', 'Seoul'), 'asia-south1': ('🌏 ASIA', 'Mumbai'),
    'asia-south2': ('🌏 ASIA', 'Delhi'), 'asia-southeast1': ('🌏 ASIA', 'Singapore'),
    'asia-southeast2': ('🌏 ASIA', 'Jakarta'),
    'australia-southeast1': ('🌏 APAC', 'Sydney'), 'australia-southeast2': ('🌏 APAC', 'Melbourne'),
    'me-central1': ('🌍 ME', 'Doha'), 'me-central2': ('🌍 ME', 'Dammam'),
    'me-west1': ('🌍 ME', 'Tel Aviv'),
    'southamerica-east1': ('🌎 SA', 'Sao Paulo'), 'southamerica-west1': ('🌎 SA', 'Santiago'),
}

REGION_KEYS = sorted(REGION_TO_GEO.keys(), key=len, reverse=True)
GLOBAL_HINTS = ['cross-region', 'cross region', 'multi-region', 'multi region']
DYNAMIC_REGION_HINTS = ['default_region', '| region', '--region', ' region=', 'location=', 'locations/', '/locations/', 'choose region', 'select region', 'compute/regions/']
DYNAMIC_ZONE_HINTS = ['default_zone', '| zone', '--zone', ' zone=', 'choose zone', 'select zone', 'compute/zones/']

GEO_KEYWORDS_EU = [
    'netherlands','belgium','frankfurt','london','finland','warsaw','milan','paris',
    'berlin','madrid','zurich','turin','united kingdom','england','scotland',
    'europe','european','eu region','eu-west','eu-central','eu-north',
    'amsterdam','brussels','cologne','stockholm','oslo','copenhagen',
]

GEO_KEYWORDS_US = [
    'iowa','virginia','oregon','carolina','texas','ohio','columbus','dallas',
    'las vegas','los angeles','salt lake','montreal','toronto','canada',
    'us region','us-central','us-east','us-west','us-south',
    'united states','america','north america','new york','chicago',
]

def simple_server_label(region_label, evidence, secs):
    ev = (evidence or '').lower()
    rl = region_label.lower()
    has_eu = 'europe-' in ev or '🇪🇺' in region_label
    has_us = 'us-' in ev or '🇺🇸' in region_label
    if not has_eu:
        has_eu = any(kw in ev or kw in rl for kw in GEO_KEYWORDS_EU)
    if not has_us:
        has_us = any(kw in ev or kw in rl for kw in GEO_KEYWORDS_US)
    if has_eu and has_us: return '🇺🇸 أمريكا + 🇪🇺 أوروبا'
    if has_eu: return '🇪🇺 أوروبا'
    if has_us: return '🇺🇸 أمريكا'
    if secs >= 10800: return '🌐 عالمي طويل'
    return None

def clear_screen():
    try: os.system('clear')
    except: print('\033c', end='')

def setup_paths(kind):
    global SAVE_FILE, CHECKPOINT_FILE, ALIVE_FILE, REPORT_FILE, KNOWN_IDS_FILE, WEEKLY_NEW_FILE, URL_TEMPLATE, WEEKLY_STORAGE_FILE, WEEKLY_CHECKPOINT_FILE, WEEKLY_DEAD_FILE, PENDING_REPORT_FILE
    try:
        base = '/storage/emulated/0'
        open(base + '/.probe', 'w').write('ok')
        os.remove(base + '/.probe')
    except:
        base = os.path.expanduser('~')
    prefix = 'focuses' if kind == 'focuses' else 'catalog_lab'
    SAVE_FILE = base + f'/{prefix}_labs.txt'
    CHECKPOINT_FILE = base + f'/{prefix}_checkpoint.txt'
    ALIVE_FILE = base + f'/{prefix}_alive.txt'
    REPORT_FILE = base + f'/{prefix}_report.txt'
    KNOWN_IDS_FILE = base + f'/{prefix}_known_ids.txt'
    WEEKLY_NEW_FILE = base + f'/{prefix}_weekly_new.txt'
    WEEKLY_STORAGE_FILE = base + f'/{prefix}_weekly_storage.txt'
    WEEKLY_CHECKPOINT_FILE = base + f'/{prefix}_weekly_checkpoint.txt'
    WEEKLY_DEAD_FILE = base + f'/{prefix}_weekly_dead.txt'
    PENDING_REPORT_FILE = base + f'/{prefix}_pending_report.txt'
    URL_TEMPLATE = 'https://www.skills.google/focuses/{id}?parent=catalog' if kind == 'focuses' else 'https://www.skills.google/catalog_lab/{id}'

def get_pending_report_file(prefix):
    for base_path in ['/storage/emulated/0', '/sdcard', os.path.expanduser('~')]:
        pfile = base_path + f'/{prefix}_pending_report.txt'
        if os.path.exists(pfile):
            return pfile
    return os.path.expanduser('~') + '/' + prefix + '_pending_report.txt'

def send_pending_report_if_exists(prefix=None):
    targets = [prefix] if prefix else ['focuses', 'catalog_lab']
    sent_any = False
    for px in targets:
        pfile = get_pending_report_file(px)
        if not os.path.exists(pfile):
            continue
        try:
            with open(pfile, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            if not content:
                try: os.remove(pfile)
                except: pass
                continue
            lines = content.splitlines()
            session_file = None
            msg_lines = lines
            if lines and lines[0].startswith('SESSION_FILE='):
                session_file = lines[0].split('=', 1)[1].strip() or None
                msg_lines = lines[1:]
            msg = NL.join(msg_lines).strip()
            if msg:
                send_tg(msg)
            has_file = False
            if session_file and os.path.exists(session_file):
                try:
                    if os.path.getsize(session_file) > 0:
                        send_tg_file(session_file, f'📁 نتائج الجلسة السابقة ({px})')
                        has_file = True
                except:
                    pass
            try: os.remove(pfile)
            except: pass
            if session_file:
                try: os.remove(session_file)
                except: pass
            sent_any = True
        except Exception as e:
            pass
    return sent_any

def save_pending_report(reason='manual'):
    try:
        if KIND not in ('focuses', 'catalog_lab'):
            return
        pfile = get_pending_report_file(KIND)
        action_label = {'resume':'تكملة', 'new':'جديد', 'weekly':'أسبوعي'}.get(ACTION, ACTION)
        start_from = last_processed[0] if last_processed[0] else START
        found_count = len(found) if ACTION != 'weekly' else len(weekly_new)
        stop_reason = ('انقطاع النت' if reason == 'network' else 'انقطاع النت / إغلاق التطبيق' if reason in ('manual', 'crash') else str(reason))
        timestamp = time.strftime('%d/%m/%Y %H:%M')
        stop_msg = (
            NL + '📋 ━━━━━━━━━━━━━━━━━━━━' + NL +
            '🔴 تقرير جلسة منتهية' + NL +
            '📋 ━━━━━━━━━━━━━━━━━━━━' + NL +
            '👤 ' + BANNER_NAME + ' | ' + BANNER_USER + NL +
            '🧪 النوع: ' + str(KIND) + NL +
            '⚙️ الوضع: ' + str(action_label) + NL +
            '📍 البداية: ' + str(START) + NL +
            '▶️ توقف عند: ' + str(start_from) + NL +
            '🏁 النهاية المحددة: ' + str(END) + NL +
            '📊 مختبرات وجدها: ' + str(found_count) + NL +
            '⏱️ مدة الجلسة: ' + elapsed_text() + NL +
            '⏳ المتبقي التقريبي: ' + remaining_text(max(0, END - START + 1) if START is not None and END is not None else 0) + NL +
            '❓ سبب التوقف: ' + stop_reason + NL +
            '📅 وقت التوقف: ' + timestamp + NL +
            '📤 تم إرسال هذا التقرير عند عودة الاتصال' + NL +
            '━━━━━━━━━━━━━━━━━━━━'
        )
        session_file = None
        data = found if ACTION != 'weekly' else weekly_new
        if found_count > 0 and data:
            try:
                base_dir = os.path.dirname(pfile) or '.'
                session_file = os.path.join(base_dir, f'{KIND}_pending_session.txt')
                sorted_found = sorted(data, key=lambda x: x['secs'], reverse=True)
                with open(session_file, 'w', encoding='utf-8') as f:
                    for lab in sorted_found:
                        f.write(str(lab['id']) + ' | ' + lab['duration'] + ' | ' + lab['title'] + ' | ' + (lab.get('server') or '') + ' | ' + lab['url'] + NL)
            except:
                session_file = None
        with open(pfile, 'w', encoding='utf-8') as f:
            if session_file:
                f.write('SESSION_FILE=' + session_file + NL)
            f.write(stop_msg)
    except:
        pass

def elapsed_text():
    elapsed = int(time.time() - start_time)
    h = elapsed // 3600; m = (elapsed % 3600) // 60
    return str(h) + 'h ' + str(m) + 'm'

def remaining_text(total_count):
    try:
        done = max(0, last_processed[0] - START + 1)
        elapsed = max(1, int(time.time() - start_time))
        left = max(0, total_count - done)
        if elapsed >= 10 and done > 0:
            rate = done / elapsed
        else:
            rate = 4.0
        if rate <= 0:
            return 'غير معروف'
        secs = int(left / rate)
        h = secs // 3600
        m = (secs % 3600) // 60
        return str(h) + 'h ' + str(m) + 'm'
    except:
        return 'غير معروف'

def is_network_error(e):
    s = str(e).lower()
    keys = [
        'cannot connect to host', 'temporary failure', 'name or service not known',
        'nodename nor servname', 'timeout', 'timed out', 'clientconnectorerror',
        'serverdisconnectederror', 'connection reset', 'network is unreachable'
    ]
    return any(k in s for k in keys)

def internet_timeout_stop(i=None):
    try:
        save_pending_report('انقطاع النت')
    except Exception:
        pass
    if not notified[0]:
        notified[0] = True
        exit_report_sent[0] = True

_last_pending_save = [0]

def internet_pause_notice(i=None):
    waiting_net[0] = True
    pending_net_break[0] = True
    if i is not None:
        net_break_i[0] = i

def internet_resume_notice(i=None):
    if not waiting_net[0] and not pending_net_break[0]:
        return
    stopped_i = net_break_i[0] if net_break_i[0] else i
    resumed_i = i if i is not None else stopped_i
    if pending_net_break[0]:
        send_tg('⚠️ كان الإنترنت مقطوعًا' + NL + '▶️ توقف عند الرقم: ' + str(stopped_i))
        send_tg('🔄 عاد الإنترنت وتم الاستئناف' + NL + '▶️ استأنف من الرقم: ' + str(resumed_i))
    waiting_net[0] = False
    pending_net_break[0] = False
    last_resume_time[0] = time.time()
    watchdog_suspect_since[0] = 0
    write_heartbeat()

def write_heartbeat():
    try: open(ALIVE_FILE, 'w').write(str(int(time.time())))
    except: pass
    _now = time.time()
    if _now - _last_pending_save[0] >= 30:
        _last_pending_save[0] = _now
        save_pending_report()

def watchdog():
    time.sleep(15)
    while True:
        time.sleep(10)
        if tg_state['step'] != 'running':
            continue
        try:
            now = int(time.time())
            if waiting_net[0]:
                watchdog_suspect_since[0] = 0
                continue
            if last_resume_time[0] and (time.time() - last_resume_time[0] < RESUME_GRACE_SECONDS):
                watchdog_suspect_since[0] = 0
                continue
            last = int(open(ALIVE_FILE).read().strip())
            if now - last > 8:
                if not watchdog_suspect_since[0]:
                    watchdog_suspect_since[0] = time.time()
                    continue
                if time.time() - watchdog_suspect_since[0] < WATCHDOG_CONFIRM_SECONDS:
                    continue
                if not notified[0]:
                    notified[0] = True
                    save_pending_report('crash')
                    send_stop_report('crash')
                break
            else:
                watchdog_suspect_since[0] = 0
        except:
            pass

def save_checkpoint(i):
    last_processed[0] = i
    try: open(CHECKPOINT_FILE, 'w').write(str(i))
    except: pass

def read_checkpoint_value():
    try:
        if CHECKPOINT_FILE and os.path.exists(CHECKPOINT_FILE):
            val = open(CHECKPOINT_FILE).read().strip()
            if val and val.isdigit(): return int(val)
    except: pass
    return None

def read_weekly_checkpoint_value():
    try:
        if WEEKLY_CHECKPOINT_FILE and os.path.exists(WEEKLY_CHECKPOINT_FILE):
            val = open(WEEKLY_CHECKPOINT_FILE).read().strip()
            if val and val.isdigit(): return int(val)
    except: pass
    return None

def load_weekly_storage():
    s = set()
    try:
        if WEEKLY_STORAGE_FILE and os.path.exists(WEEKLY_STORAGE_FILE):
            with open(WEEKLY_STORAGE_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line.isdigit(): s.add(int(line))
    except: pass
    return s

def append_to_weekly_storage(i):
    try:
        with open(WEEKLY_STORAGE_FILE, 'a', encoding='utf-8') as f:
            f.write(str(i) + NL)
    except: pass

def load_weekly_dead():
    s = set()
    try:
        if WEEKLY_DEAD_FILE and os.path.exists(WEEKLY_DEAD_FILE):
            with open(WEEKLY_DEAD_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line.isdigit(): s.add(int(line))
    except: pass
    return s

def save_weekly_dead(ids_set):
    try:
        with open(WEEKLY_DEAD_FILE, 'w', encoding='utf-8') as f:
            for x in sorted(ids_set):
                f.write(str(x) + NL)
    except: pass

def add_weekly_dead(i):
    d = load_weekly_dead()
    if i in d: return
    d.add(i)
    save_weekly_dead(d)

def remove_weekly_dead(i):
    d = load_weekly_dead()
    if i not in d: return
    d.discard(i)
    save_weekly_dead(d)

def save_weekly_checkpoint(i):
    try: open(WEEKLY_CHECKPOINT_FILE, 'w').write(str(i))
    except: pass

def load_known_ids():
    s = set()
    try:
        if KNOWN_IDS_FILE and os.path.exists(KNOWN_IDS_FILE):
            with open(KNOWN_IDS_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line.isdigit(): s.add(int(line))
    except: pass
    return s

def save_known_ids(ids_set):
    try:
        with open(KNOWN_IDS_FILE, 'w', encoding='utf-8') as f:
            for x in sorted(ids_set): f.write(str(x) + NL)
    except: pass

def reset_weekly_new_file():
    try: open(WEEKLY_NEW_FILE, 'w', encoding='utf-8').close()
    except: pass

def save_weekly_new_txt(lab):
    try:
        with open(WEEKLY_NEW_FILE, 'a', encoding='utf-8') as f:
            f.write(str(lab['id']) + ' | ' + lab['duration'] + ' | ' + lab['title'] + ' | ' + lab['url'] + NL)
    except: pass

def save_lab_txt(lab):
    try:
        with open(SAVE_FILE, 'a', encoding='utf-8') as f:
            f.write(str(lab['id']) + ' | ' + lab['duration'] + ' | ' + lab['title'] + ' | ' + lab['url'] + NL)
    except: pass

TG_SESSION = requests.Session()

def send_tg(msg, tries=7):
    for attempt in range(tries):
        try:
            r = requests.post(
                'https://api.telegram.org/bot' + BOT_TOKEN + '/sendMessage',
                json={'chat_id': CHAT_ID, 'text': msg},
                timeout=15
            )
            if r.ok:
                return
        except Exception:
            pass
        time.sleep(min(3 * (attempt + 1), 15))

def send_tg_file(filepath, caption='', tries=7):
    for attempt in range(tries):
        try:
            with open(filepath, 'rb') as f:
                r = requests.post(
                    'https://api.telegram.org/bot' + BOT_TOKEN + '/sendDocument',
                    data={'chat_id': CHAT_ID, 'caption': caption},
                    files={'document': f},
                    timeout=45
                )
            if r.ok:
                return True
        except Exception:
            pass
        time.sleep(min(3 * (attempt + 1), 15))
    return False

def get_duration(text):
    m = re.search(r"data-lab-duration=['\"](\\d+)['\"]", text, re.IGNORECASE)
    if m:
        secs = int(m.group(1))
        if 60 <= secs <= 8 * 3600:
            h = secs // 3600; mn = (secs % 3600) // 60
            if h and mn: return f'{h}h {mn}m', secs
            if h: return f'{h}h', secs
            return f'{mn}m', secs
    m = re.search(r"chips='(\\[.*?\\])'", text)
    if not m: m = re.search(r'chips="(\\[.*?\\])"', text)
    if m:
        import html as html_mod
        chips_raw = html_mod.unescape(m.group(1))
        mc = re.search(r'(\\d+)\\s*hours?\\s*(\\d+)?\\s*minutes?', chips_raw, re.IGNORECASE)
        if not mc: mc = re.search(r'(\\d+)\\s*hours?', chips_raw, re.IGNORECASE)
        if mc:
            h = int(mc.group(1)); mn = int(mc.group(2)) if mc.lastindex and mc.lastindex >= 2 and mc.group(2) else 0
            secs = h * 3600 + mn * 60
            if 60 <= secs <= 8 * 3600:
                if h and mn: return f'{h}h {mn}m', secs
                return f'{h}h', secs
        mc = re.search(r'(\\d+)\\s*minutes?', chips_raw, re.IGNORECASE)
        if mc:
            mn = int(mc.group(1)); secs = mn * 60
            if 60 <= secs <= 8 * 3600: return f'{mn}m', secs
    m = re.search(r'Lab\\s+(\\d+)\\s*minutes?', text, re.IGNORECASE)
    if m:
        mn = int(m.group(1)); return f'{mn}m', mn * 60
    m = re.search(r'"duration"\\s*:\\s*"PT(?:(\\d+)H)?(?:(\\d+)M)?"', text, re.IGNORECASE)
    if m:
        h = int(m.group(1) or 0); mn = int(m.group(2) or 0); secs = h * 3600 + mn * 60
        if h and mn: return f'{h}h {mn}m', secs
        if h: return f'{h}h', secs
        return f'{mn}m', secs
    m = re.search(r'"totalDurationSeconds"\\s*:\\s*(\\d+)', text, re.IGNORECASE)
    if m:
        secs = int(m.group(1))
        if 60 <= secs <= 8 * 3600:
            h = secs // 3600; mn = (secs % 3600) // 60
            if h and mn: return f'{h}h {mn}m', secs
            if h: return f'{h}h', secs
            return f'{mn}m', secs
    m = re.search(r'"secondsRemaining"\\s*:\\s*(\\d+)', text, re.IGNORECASE)
    if m:
        secs = int(m.group(1))
        if 60 <= secs <= 8 * 3600:
            h = secs // 3600; mn = (secs % 3600) // 60
            if h and mn: return f'{h}h {mn}m', secs
            if h: return f'{h}h', secs
            return f'{mn}m', secs
    m = re.search(r'(\\d+)\\s*hours?\\s*(\\d+)?\\s*minutes?', text, re.IGNORECASE)
    if m:
        h = int(m.group(1) or 0); mn = int(m.group(2) or 0); secs = h * 3600 + mn * 60
        if 60 <= secs <= 8 * 3600:
            if h and mn: return f'{h}h {mn}m', secs
            if h: return f'{h}h', secs
    m = re.search(r'"durationMinutes"\\s*:\\s*(\\d+)', text, re.IGNORECASE)
    if m:
        mn = int(m.group(1)); secs = mn * 60
        if 60 <= secs <= 8 * 3600:
            h = secs // 3600; rem = (secs % 3600) // 60
            if h and rem: return f'{h}h {rem}m', secs
            if h: return f'{h}h', secs
            return f'{mn}m', secs
    return 'unknown', 0

def get_internal_times(text):
    clean = re.sub(r"data-lab-duration=[\x22\x27][^\x22\x27]+[\x22\x27]", '', text)
    clean = re.sub(r"chips='[^']+'", '', clean)
    clean = re.sub(r'chips="[^"]+"', '', clean)
    clean = re.sub(r'"totalDurationSeconds"\s*:\s*\d+', '', clean)
    clean = re.sub(r'"secondsRemaining"\s*:\s*\d+', '', clean)
    keywords = [
        r'takes?\s+about\s+(\d+)\s*hours?\s*(\d+)?\s*minutes?',
        r'takes?\s+about\s+(\d+)\s*minutes?',
        r'takes?\s+about\s+(\d+)\s*hours?',
        r'approximately\s+(\d+)\s*hours?\s*(\d+)?\s*minutes?',
        r'approximately\s+(\d+)\s*minutes?',
        r'takes?\s+(\d+)\s*[-–]\s*\d+\s*minutes?',
        r'takes?\s+(\d+)\s*minutes?',
        r'(\d+)\s*minutes?\s+to\s+(?:complete|finish|deploy|import|create|train)',
        r'(?:deploy|import|create|train|build).*?(\d+)\s*minutes?',
        r'(?:deploy|import|create|train|build).*?(\d+)\s*hours?',
        r'about\s+(\d+)\s*hours?',
        r'about\s+(\d+)\s*minutes?',
    ]
    total_secs = 0
    details = []
    seen_spans = []
    for pattern in keywords:
        for m in re.finditer(pattern, clean, re.IGNORECASE):
            span = m.span()
            overlap = any(s[0] <= span[0] <= s[1] or s[0] <= span[1] <= s[1] for s in seen_spans)
            if overlap:
                continue
            seen_spans.append(span)
            groups = m.groups()
            h = int(groups[0]) if groups[0] else 0
            mn = int(groups[1]) if len(groups) > 1 and groups[1] else 0
            if 'hours?' not in pattern.split('minutes?')[0].split('(')[0] or h < 10:
                if 'minutes?' in pattern and 'hours?' not in pattern:
                    mn = h; h = 0
            secs = h * 3600 + mn * 60
            if secs < 60 or secs > 8 * 3600:
                continue
            raw = m.group(0)[:60].strip()
            if h and mn:
                time_str = f'{h}h {mn}m'
            elif h:
                time_str = f'{h}h'
            else:
                time_str = f'{mn}m'
            total_secs += secs
            details.append((time_str, raw))
    return total_secs, details

def detect_region_ultra(text):
    t = text.lower()
    matches = []
    for key in REGION_KEYS:
        if re.search(r'(?<![a-z0-9\-])' + re.escape(key) + r'(?![a-z0-9\-])', t):
            matches.append(key)
    if matches:
        best = matches[0]
        flag, city = REGION_TO_GEO[best]
        return f'{flag} {city}', best
    for hint in GLOBAL_HINTS:
        if hint in t:
            return '🌐 Global', hint
    for hint in DYNAMIC_REGION_HINTS:
        if hint in t:
            return '🌐 Dynamic Region', hint
    for hint in DYNAMIC_ZONE_HINTS:
        if hint in t:
            return '🌐 Dynamic Zone', hint
    return '❓ Unknown', ''

def get_title(text):
    m = re.search(r'<title>(.*?)</title>', text, re.IGNORECASE | re.DOTALL)
    if m:
        t = m.group(1).strip()
        for bad in DEAD_TITLES:
            if bad.lower() in t.lower(): return None
        return t[:120]
    return None

def build_final_summary():
    data = found if ACTION != 'weekly' else weekly_new
    action_label = {'resume':'تكملة','new':'جديد','weekly':'أسبوعي'}.get(ACTION, ACTION)
    if not data:
        return ('✅ انتهى' + NL +
            '👤 ' + BANNER_NAME + ' | ' + BANNER_USER + NL +
            '🧪 النوع: ' + KIND + NL +
            '⚙️ الوضع: ' + action_label + NL +
            '📍 البداية: ' + str(START) + NL +
            '🏁 النهاية: ' + str(END) + NL +
            '📊 لا يوجد مختبرات جديدة' + NL +
            '⏱️ الوقت: ' + elapsed_text())
    secs_list = [x['secs'] for x in data if x['secs'] > 0]
    shortest = min(data, key=lambda x: x['secs'] if x['secs'] > 0 else 999999)
    longest = max(data, key=lambda x: x['secs'])
    dist = ''
    for label, cond in [('< 1h', lambda s: s < 3600), ('1h - 2h', lambda s: 3600 <= s < 7200), ('2h - 3h', lambda s: 7200 <= s < 10800), ('> 3h', lambda s: s >= 10800)]:
        count = sum(1 for s in secs_list if cond(s))
        if count > 0: dist += ' ' + label + ' -> ' + str(count) + NL
    return ('✅ انتهى' + NL +
        '👤 ' + BANNER_NAME + ' | ' + BANNER_USER + NL +
        '🧪 النوع: ' + KIND + NL +
        '⚙️ الوضع: ' + action_label + NL +
        '📍 البداية: ' + str(START) + NL +
        '🏁 النهاية: ' + str(END) + NL +
        '📊 العدد: ' + str(len(data)) + NL +
        '⚡ الأقصر: ' + shortest['duration'] + NL + '🏆 الأطول: ' + longest['duration'] + NL +
        '⏱️ الوقت: ' + elapsed_text() + NL + dist.strip())

def send_stop_report(reason='stop'):
    if exit_report_sent[0] and reason != 'end':
        return
    exit_report_sent[0] = True
    action_label = {'resume':'تكملة','new':'جديد','weekly':'أسبوعي'}.get(ACTION, ACTION)
    total_count = max(0, END - START + 1) if START is not None and END is not None else 0
    start_from = last_processed[0] if last_processed[0] else START
    found_count = len(found) if ACTION != 'weekly' else len(weekly_new)
    stop_msg = ('⛔ تم التوقف' + NL +
        '👤 ' + BANNER_NAME + ' | ' + BANNER_USER + NL +
        '🧪 النوع: ' + str(KIND) + NL +
        '⚙️ الوضع: ' + str(action_label) + NL +
        '▶️ يبدأ من: ' + str(start_from) + NL +
        '🏁 النهاية: ' + str(END) + NL +
        '📊 المختبرات الموجودة: ' + str(found_count) + NL +
        '⏳ المتبقي التقريبي: ' + remaining_text(total_count) + NL +
        '⏱️ الوقت الحالي: ' + elapsed_text())
    if reason == 'end':
        send_tg(build_final_summary())
    elif reason == 'crash':
        send_tg(stop_msg + NL + '⚠️ السبب: watchdog confirmed crash after grace period')
    else:
        send_tg(stop_msg)
    if ACTION == 'weekly':
        if weekly_new and os.path.exists(WEEKLY_NEW_FILE):
            send_tg_file(WEEKLY_NEW_FILE, '📁 مختبرات جديدة الأسبوع')
    else:
        if found:
            try:
                tmp = SAVE_FILE.replace('.txt', '_session.txt')
                sorted_found = sorted(found, key=lambda x: x['secs'], reverse=True)
                with open(tmp, 'w', encoding='utf-8') as f:
                    for lab in sorted_found:
                        f.write(str(lab['id']) + ' | ' + lab['duration'] + ' | ' + lab['title'] + ' | ' + (lab.get('server') or '') + ' | ' + lab['url'] + NL)
                sent = send_tg_file(tmp, '📁 نتائج الجلسة (' + str(START) + ' → ' + str(END) + ')')
                if sent:
                    try: os.remove(tmp)
                    except: pass
            except Exception:
                pass

def send_start_report():
    action_label = {'resume':'تكملة','new':'جديد','weekly':'أسبوعي'}.get(ACTION, ACTION)
    total_count = max(0, END - START + 1)
    msg = ('🚀 بدأ الفحص' + NL +
        '👤 ' + BANNER_NAME + ' | ' + BANNER_USER + NL +
        '🧪 النوع: ' + KIND + NL +
        '⚙️ الوضع: ' + action_label + NL +
        '📍 البداية: ' + str(START) + NL +
        '🏁 النهاية: ' + str(END) + NL +
        '🔢 العدد التقريبي: ' + str(total_count) + NL +
        '⏳ التقدير الأولي: ' + remaining_text(total_count))
    send_tg(msg)

def build_url(i):
    return URL_TEMPLATE.format(id=i)

async def fetch_with_retry(session, i, sem):
    url = build_url(i)
    async with sem:
        for attempt in range(1, RETRY_MAX + 1):
            try:
                async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20), ssl=False, allow_redirects=True) as r:
                    write_heartbeat()
                    if r.status == 404:
                        print(CR + 'dead ' + str(i) + C0)
                        save_checkpoint(i)
                        add_weekly_dead(i)
                        return
                    text = await r.text()
                    save_checkpoint(i)
                    if DEAD_TEXT in text and 'data-lab-duration' not in text and 'Lab' not in text:
                        add_weekly_dead(i)
                        print(CR + 'dead ' + str(i) + C0); return
                    title = get_title(text)
                    if not title:
                        add_weekly_dead(i)
                        print(CR + 'dead ' + str(i) + C0); return
                    duration, secs = get_duration(text)
                    if duration == 'unknown' or secs == 0:
                        add_weekly_dead(i)
                        print(CY + 'skip ' + str(i) + ' (no duration)' + C0); return
                    region_label, evidence = detect_region_ultra(text)
                    server = simple_server_label(region_label, evidence, secs)
                    if server is None:
                        add_weekly_dead(i)
                        print(CY + 'skip ' + str(i) + ' (filtered)' + C0); return
                    lab = {'id': i, 'title': title, 'duration': duration, 'secs': secs, 'server': server, 'url': url}
                    found.append(lab)
                    append_to_weekly_storage(i)
                    remove_weekly_dead(i)
                    save_lab_txt(lab)
                    int_secs, int_details = get_internal_times(text)
                    int_line = ''
                    if int_secs > 0 and int_details:
                        int_line = NL + '🔍 داخلي: ' + ' + '.join([d[0] for d in int_details[:4]])
                        estimated = secs + int_secs
                        eh = estimated // 3600; em = (estimated % 3600) // 60
                        if eh and em: int_line += NL + '📊 تقدير كلي: ~' + str(eh) + 'h ' + str(em) + 'm'
                        elif eh: int_line += NL + '📊 تقدير كلي: ~' + str(eh) + 'h'
                    msg = ('✅ مختبر جديد ' + str(i) + NL +
                        '🌍 السيرفر: ' + server + NL +
                        '📚 العنوان: ' + title + NL +
                        '⏱️ المدة: ' + duration +
                        int_line + NL +
                        '🔗 الرابط:' + NL + url)
                    send_tg(msg)
                    print(CG + '✅ ' + str(i) + ' | ' + duration + ' | ' + server + C0)
                    return
            except Exception as e:
                if is_network_error(e):
                    internet_pause_notice(i)
                    wait_started = time.time()
                    while True:
                        if time.time() - wait_started >= NET_WAIT_TIMEOUT:
                            internet_timeout_stop(i)
                            raise SystemExit(0)
                        await asyncio.sleep(NET_CHECK_INTERVAL)
                        try:
                            async with session.get('https://www.google.com/generate_204', timeout=aiohttp.ClientTimeout(total=10), ssl=False) as rr:
                                if rr.status in (200, 204):
                                    internet_resume_notice(i)
                                    break
                        except Exception:
                            pass
                    continue
                if attempt == RETRY_MAX:
                    print(CR + 'err ' + str(i) + ' | ' + str(e)[:40] + C0)
                    last_processed[0] = i
                    save_checkpoint(i)
                else:
                    await asyncio.sleep(RETRY_DELAY)

async def fetch_weekly(session, i, sem, weekly_storage):
    url = build_url(i)
    async with sem:
        for attempt in range(1, RETRY_MAX + 1):
            try:
                async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20), ssl=False, allow_redirects=True) as r:
                    write_heartbeat()
                    save_weekly_checkpoint(i)
                    last_processed[0] = i
                    if r.status == 404:
                        add_weekly_dead(i)
                        print(CR + 'dead ' + str(i) + C0); return
                    text = await r.text()
                    if DEAD_TEXT in text and 'data-lab-duration' not in text and 'Lab' not in text:
                        add_weekly_dead(i)
                        print(CR + 'dead ' + str(i) + C0); return
                    title = get_title(text)
                    if not title:
                        add_weekly_dead(i)
                        print(CR + 'dead ' + str(i) + C0); return
                    duration, secs = get_duration(text)
                    if duration == 'unknown' or secs == 0:
                        add_weekly_dead(i)
                        print(CY + 'skip ' + str(i) + ' (no duration)' + C0); return
                    region_label, evidence = detect_region_ultra(text)
                    server = simple_server_label(region_label, evidence, secs)
                    if server is None:
                        add_weekly_dead(i)
                        print(CY + 'skip ' + str(i) + ' (filtered)' + C0); return
                    lab = {'id': i, 'title': title, 'duration': duration, 'secs': secs, 'server': server, 'url': url}
                    weekly_new.append(lab)
                    append_to_weekly_storage(i)
                    remove_weekly_dead(i)
                    save_weekly_new_txt(lab)
                    int_secs, int_details = get_internal_times(text)
                    int_line = ''
                    if int_secs > 0 and int_details:
                        int_line = NL + '🔍 داخلي: ' + ' + '.join([d[0] for d in int_details[:4]])
                        estimated = secs + int_secs
                        eh = estimated // 3600; em = (estimated % 3600) // 60
                        if eh and em: int_line += NL + '📊 تقدير كلي: ~' + str(eh) + 'h ' + str(em) + 'm'
                        elif eh: int_line += NL + '📊 تقدير كلي: ~' + str(eh) + 'h'
                    msg = ('🆕 مختبر جديد ' + str(i) + NL +
                        '🌍 السيرفر: ' + server + NL +
                        '📚 العنوان: ' + title + NL +
                        '⏱️ المدة: ' + duration +
                        int_line + NL +
                        '🔗 الرابط:' + NL + url)
                    send_tg(msg)
                    print(CG + '🆕 ' + str(i) + ' | ' + duration + ' | ' + server + C0)
                    return
            except Exception as e:
                if is_network_error(e):
                    internet_pause_notice(i)
                    wait_started = time.time()
                    while True:
                        if time.time() - wait_started >= NET_WAIT_TIMEOUT:
                            internet_timeout_stop(i)
                            raise SystemExit(0)
                        await asyncio.sleep(NET_CHECK_INTERVAL)
                        try:
                            async with session.get('https://www.google.com/generate_204', timeout=aiohttp.ClientTimeout(total=10), ssl=False) as rr:
                                if rr.status in (200, 204):
                                    internet_resume_notice(i)
                                    break
                        except Exception:
                            pass
                    continue
                if attempt == RETRY_MAX:
                    print(CR + 'err ' + str(i) + ' | ' + str(e)[:40] + C0)
                    last_processed[0] = i
                else:
                    await asyncio.sleep(RETRY_DELAY)

async def run_scan():
    global found, weekly_new
    # فحص تقارير الجلسات المؤجلة
    send_pending_report_if_exists(KIND)
    send_start_report()
    threading.Thread(target=watchdog, daemon=True).start()
    sem = asyncio.Semaphore(CONCURRENT)
    connector = aiohttp.TCPConnector(ssl=False, limit=CONCURRENT)
    async with aiohttp.ClientSession(connector=connector) as session:
        if ACTION == 'weekly':
            weekly_storage = load_weekly_storage()
            weekly_dead = load_weekly_dead()
            reset_weekly_new_file()
            ids_gap = [i for i in range(START, END + 1) if i not in weekly_storage]
            ids_dead = [i for i in range(START, END + 1) if i in weekly_dead]
            ids_to_check = sorted(set(ids_gap) | set(ids_dead))
            total = len(ids_to_check)
            print(CY + f'أرقام أسبوعية للفحص: {total} | dead: {len(ids_dead)} | ساقطة: {len(ids_gap)}' + C0)
            if total == 0:
                send_tg('✅ الأسبوعي انتهى - لا يوجد جديد ولا dead لإعادة الفحص' + NL + '⏱️ الوقت: ' + elapsed_text())
                with tg_state_lock:
                    tg_state['step'] = 'idle'
                return
            tasks = [fetch_weekly(session, i, sem, weekly_storage) for i in ids_to_check]
            await asyncio.gather(*tasks, return_exceptions=True)
        else:
            tasks = [fetch_with_retry(session, i, sem) for i in range(START, END + 1)]
            await asyncio.gather(*tasks, return_exceptions=True)
    notified[0] = True
    send_stop_report('end')
    try:
        pfile = get_pending_report_file(KIND)
        if pfile and os.path.exists(pfile): os.remove(pfile)
    except: pass
    print(CG + BOLD + NL + '✅ انتهى البحث' + C0)
    with tg_state_lock:
        tg_state['step'] = 'idle'
    tg_send('━━━━━━━━━━━━━━━━━━━━\n✅ انتهى الفحص!\nأرسل /start لبدء جلسة جديدة\n━━━━━━━━━━━━━━━━━━━━')

def handle_signal_tg():
    if not notified[0]:
        notified[0] = True
        save_pending_report('manual')
        send_stop_report('manual')
    with tg_state_lock:
        tg_state['step'] = 'idle'

def handle_signal(sig, frame):
    print(CM + NL + '⛔ تم الإيقاف' + C0)
    handle_signal_tg()
    raise SystemExit(0)

def _atexit_report():
    try:
        if not exit_report_sent[0] and ACTION is not None and START is not None and END is not None:
            t = threading.Thread(target=lambda: (save_pending_report('manual'), send_stop_report('manual')), daemon=False)
            t.start()
            t.join(timeout=20)
    except:
        pass

atexit.register(_atexit_report)
signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)

if __name__ == '__main__':
    print(CM + BOLD + '=' * 48 + C0)
    print(CC + BOLD + ' Google Skills Scanner - Ultra Strict' + C0)
    print(CG + BOLD + f' {BANNER_NAME} | {BANNER_USER}' + C0)
    print(CM + BOLD + '=' * 48 + C0)
    print()
    # تشغيل مستمع تيليغرام في الخلفية
    tg_thread = threading.Thread(target=tg_listener, daemon=True)
    tg_thread.start()
    # إبقاء البرنامج شغالاً
    while True:
        time.sleep(1)
