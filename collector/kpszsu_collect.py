#!/usr/bin/env python3
"""Сборщик и парсер канала t.me/kpszsu (Повітряні сили ЗСУ).

Запуск:  python3 kpszsu_collect.py [--days 30] [--out data]
Что делает:
  1. Догружает новые посты из публичной веб-версии https://t.me/s/kpszsu
     (кэш сырых постов: data/posts.json, хранится DAYS дней).
  2. Разбирает каждую строку поста в событие: тип цели, области, место, курс.
  3. Склеивает цепочки ответов (reply) в треки — одна цель, меняющая курс.
  4. Строит интервалы угрозы баллистики (☄️ … 📢 Відбій).
  5. Разбирает утренние/вечерние сводки (атаковано / сбито / попадания …).
  6. Пишет документы для базы дашборда: data/docs/*.json и список изменённых
     документов data/changed.json (их и надо залить в базу).
Только стандартная библиотека Python.
"""
import argparse, html, json, os, re, sys, time, urllib.request, hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")
CHANNEL = "kpszsu"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

# ----------------------------------------------------------------- загрузка

def fetch(before=None):
    url = f"https://t.me/s/{CHANNEL}" + (f"?before={before}" if before else "")
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            return urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
        except Exception as e:  # сеть/429 — подождать и повторить
            time.sleep(2 + attempt * 3)
    raise RuntimeError(f"не удалось загрузить {url}")


def clean(fragment):
    fragment = re.sub(r"<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    return html.unescape(fragment).replace("\xa0", " ").strip()


def parse_page(page):
    out = []
    for chunk in page.split("tgme_widget_message_wrap")[1:]:
        m = re.search(r'data-post="%s/(\d+)"' % CHANNEL, chunk)
        d = re.search(r'datetime="([^"]+)"', chunk)
        if not (m and d):
            continue
        reply = re.search(r'class="tgme_widget_message_reply([^"]*)"\s+href="https://t\.me/%s/(\d+)"(.*?)</a>' % CHANNEL, chunk, re.S)
        rid = quote = None
        body = chunk
        if reply:
            rid = int(reply.group(2))
            if "reply_quote" in reply.group(1):
                q = re.search(r'tgme_widget_message_text js-message_reply_text[^>]*>(.*?)</div>', reply.group(3), re.S)
                quote = clean(q.group(1)) if q else None
            body = chunk[reply.end():]
        t = re.search(r'class="tgme_widget_message_text js-message_text[^"]*"[^>]*>(.*?)</div>', body, re.S)
        out.append({
            "id": int(m.group(1)),
            "t": datetime.fromisoformat(d.group(1)).astimezone(timezone.utc).isoformat(),
            "text": clean(t.group(1)) if t else "",
            "reply": rid,
            "quote": quote,
        })
    return out


def update_cache(cache, days):
    """Догружает посты новее последнего известного и до глубины days."""
    horizon = datetime.now(timezone.utc) - timedelta(days=days)
    known = set(cache)
    last = max(known) if known else 0
    oldest = min(known) if known else None
    before, fetched, pages = None, 0, 0
    # 1) от свежих к старым, пока не дойдём до известного или горизонта
    while True:
        posts = parse_page(fetch(before))
        pages += 1
        if not posts:
            break
        stop = False
        for p in posts:
            if p["id"] not in cache:
                fetched += 1
            cache[p["id"]] = p  # перезаписываем — ловим правки текста
            if datetime.fromisoformat(p["t"]) < horizon:
                stop = True
        before = min(p["id"] for p in posts)
        if stop or (last and before <= last - 20):
            break
        time.sleep(0.4)
    # 2) если кэш не доходит до горизонта (первый запуск с меньшей глубиной) — добрать
    if cache:
        oldest = min(cache)
        while datetime.fromisoformat(cache[oldest]["t"]) > horizon:
            posts = parse_page(fetch(oldest))
            pages += 1
            if not posts:
                break
            for p in posts:
                if p["id"] not in cache:
                    fetched += 1
                cache[p["id"]] = p
            new_oldest = min(p["id"] for p in posts)
            if new_oldest >= oldest:
                break
            oldest = new_oldest
            time.sleep(0.4)
    # обрезать старое (держим days + 1 сутки запаса)
    cut = horizon - timedelta(days=1)
    for k in [k for k, p in cache.items() if datetime.fromisoformat(p["t"]) < cut]:
        del cache[k]
    return fetched, pages

# ----------------------------------------------------------------- словари

# Области: код → (название, регэксп основы). Порядок важен: область раньше города-центра.
OBLASTS = [
    ("kyivobl", "Київщина", r"київщин|київськ\w* обл|київськ\w* водосховищ|чорнобил|зон\w* відчуження"),
    ("kyiv", "Київ", r"\bкиє?в(?:а|і|ом|у)?\b|\bкиїв\b|\bкиєва\b|\bкиєві\b|\bстолиц"),
    ("vin", "Вінниччина", r"вінниц|вінниччин|вінничин"),
    ("vol", "Волинь", r"волин|луцьк"),
    ("dnipro", "Дніпропетровщина", r"дніпропетровщ|дніпропетровськ|\bдніпр(?:о|а|і|ом|у)\b|дніпровщ"),
    ("don", "Донеччина", r"донеччин|донецьк"),
    ("zhy", "Житомирщина", r"житомир"),
    ("zak", "Закарпаття", r"закарпат|ужгород|мукачев"),
    ("zap", "Запоріжжя", r"запоріж|запорізьк"),
    ("if", "Прикарпаття", r"івано-франків|прикарпат|франківщ"),
    ("kir", "Кіровоградщина", r"кіровоградщ|кіровоградськ|кропивницьк"),
    ("lug", "Луганщина", r"луганщ|луганськ"),
    ("lviv", "Львівщина", r"льв[іо]в"),
    ("myk", "Миколаївщина", r"микола[їє]в"),
    ("od", "Одещина", r"одещ|одес|чорного мор|чорне мор|акваторі"),
    ("pol", "Полтавщина", r"полтав"),
    ("riv", "Рівненщина", r"рівненщ|рівненськ|\bрівн(?:е|ого|ому)\b"),
    ("sum", "Сумщина", r"сумщ|сумськ|\bсум(?:и|ах|ам)?\b"),
    ("ter", "Тернопільщина", r"тернопіл|тернопол"),
    ("kh", "Харківщина", r"харк[іо]в"),
    ("khe", "Херсонщина", r"херсон"),
    ("khm", "Хмельниччина", r"хмельниц|хмельниччин"),
    ("che", "Черкащина", r"черкас|черкащ"),
    ("chn", "Чернігівщина", r"черніг[іо]в"),
    ("chv", "Буковина", r"чернівец|чернівц|буковин"),
    ("crimea", "Крим", r"крим|\bсевастопол"),
    ("ru", "рф", r"\bрф\b|брянськ|курськ|бєлгород|орел|воронеж|ростов|міллерово|приморсько-ахтарськ|шаталово"),
]
OBL_NAME = {c: n for c, n, _ in OBLASTS}
OBL_RE = [(c, re.compile(r, re.I)) for c, _, r in OBLASTS]

# Населённые пункты/районы → область. Ключ — основа слова (нижний регистр).
PLACES = {
    "kyiv": "чоколівк караваєві шулявк академмістечк жулян печерськ солом'янськ відрадн нивки воскресенк деміївк голосіїв мишоловк феофані святошин нивк пріорк мінськ лісов дарниц рембаз поділ труханів виноградар куренівк лук'янівк лукʼянівк центр русанівк троєщин оболон сирець берковець позняк осокорк борщагівк теремк",
    "kyivobl": "чабан славутич іванків красятич боярк вишгород бровар вишнев васильків українк борисп обухів фастів димер вишгород гостомел ірпін буч нові петрівц велик димерк згурівк біла церкв білу церкв білої церкв петропавлівська-борщагівк кагарлик яготин переяслав макарів бородянк",
    "vin": "вапнярк вороновиц гайсин хмільник жмеринк бершад оратов оратів теплик погребищ ладижин могилів-подільськ козятин калинівк",
    "dnipro": "петриківк нікопол кривий ріг кривого рог кам'янськ камʼянськ синельников павлоград вільногірськ верхівцев божедарівк марганц покров жовті вод п'ятихатк новомосковськ самар межов васильківк",
    "zhy": "коростен лугин бердичів новоград звягел овруч малин",
    "kir": "олександрі знам'янк знамʼянк помічн новоархангельськ бобринец компаніївк велик виск гайворон олександрівк інгулецьк новоукраїнк долинськ світловодськ",
    "kh": "ізюм балакл лозов богодухів старий салтів чугуїв куп'янськ купʼянськ златопіл вовчанськ первомайськ красноград",
    "sum": "охтирк шостк конотоп ромн глухів тростянец лебедин",
    "chn": "сновськ корюківк ніжин батурин борзн козелец прилук бахмач парафіївк куликівк десн мена новгород-сіверськ",
    "pol": "кременчук лубн миргород пирятин чутов кобиляк нові санжар чорнух лохвиц гадяч горішні плавн",
    "myk": "очаков очаков південноукраїнськ березнегуват арбузинк вознесенськ первомайськ очаків баштанк",
    "od": "вилков ширяєв ізмаїл подільськ балт білгород-дністровськ чорноморськ рені",
    "che": "умань сміл канів монастирищ золотоноші золотонош звенигородк корсунь",
    "chv": "кельменц",
    "khm": "шепетівк кам'янець-подільськ староконстянтинів старокостянтинів",
    "zap": "комишувах малокатеринівк оріхів",
    "khe": "",
    "don": "краматорськ слов'янськ словʼянськ дружківк костянтинівк покровськ",
    "riv": "сарн костопіл",
    "vol": "ковел",
}
PLACE_RE = []
for obl, stems in PLACES.items():
    # многословные основы, разделённые двумя пробелами нет — берём фразы из списка вручную
    pass
_place_list = []
for obl, stems in PLACES.items():
    words = stems.split()
    i = 0
    # склеиваем фразы из двух слов, если первое — прилагательное-маркер
    while i < len(words):
        w = words[i]
        if w in ("біла", "білу", "білої", "нові", "велик", "старий", "кривий", "кривого", "жовті", "горішні") and i + 1 < len(words):
            w = w + " " + words[i + 1]
            i += 1
        _place_list.append((obl, w))
        i += 1
_place_list.sort(key=lambda x: -len(x[1]))
PLACE_RE = [(obl, stem, re.compile(r"(?<![а-яіїєґ'ʼ])" + re.escape(stem), re.I)) for obl, stem in _place_list]

EXCLUDE = {"kab", "aviation"}  # не входят в анализ

TYPE_NAMES = {
    "jet": "Реактивний БпЛА",
    "shahed": "БпЛА (Shahed/Гербера)",
    "banderol": "«Бандероль»",
    "ballistic": "Балістика",
    "missile": "Ракета",
    "fast": "Швидкісна ціль",
}

DIRS = [
    ("північно-західн", "ПнЗ"), ("північно-східн", "ПнС"), ("південно-західн", "ПдЗ"), ("південно-східн", "ПдС"),
    ("північ", "Пн"), ("півден", "Пд"), ("південь", "Пд"), ("схід", "С"), ("східн", "С"), ("захід", "З"), ("західн", "З"),
]
FROM_DIRS = [("з півночі", "Пн"), ("з півдня", "Пд"), ("зі сходу", "С"), ("із заходу", "З"), ("з заходу", "З"), ("з сходу", "С")]


def classify(line):
    l = line.lower()
    if re.search(r"збито/подавлено|протягом дня .*противник атакував|у ніч на .*противник атакував", l):
        return "report"
    if "відбій" in l and "балістич" in l:
        return "clear"
    if line.lstrip().startswith("☄") or ("загроза" in l and "балістич" in l):
        return "threat"
    if "тактичної авіації" in l or "тактична авіація" in l or "авіаційних засобів ураження" in l:
        return "aviation"
    if "каб" in l or "керованих авіаційних бомб" in l or "кар" == l[:3]:
        return "kab"
    if "бандерол" in l or re.search(r"\bбб\b", l) or "баражуюч" in l:
        return "banderol"
    if "балістик" in l or "балістичн" in l:
        return "ballistic"
    if re.search(r"ракет|крилат|циркон|калібр|\bх-\d", l):
        return "missile"
    if "швидкісн" in l:
        return "fast"
    if "реактивн" in l or line.lstrip().startswith("🏍"):
        return "jet"
    if "бпла" in l or "шахед" in l or "shahed" in l or line.lstrip().startswith("🛵"):
        return "shahed"
    return None


def geo(line):
    """→ (список кодов областей по порядку упоминания, место, курс, откуда)."""
    l = line.lower()
    hits = []
    for c, rx in OBL_RE:
        for m in rx.finditer(l):
            hits.append((m.start(), c))
    place = None
    for obl, stem, rx in PLACE_RE:
        m = rx.search(l)
        if m:
            if place is None:
                place = line[m.start(): m.start() + len(stem)].strip()
                # дописать окончание слова до границы
                tail = re.match(r"[\wʼ'-]*", line[m.start() + len(stem):])
                place += tail.group(0) if tail else ""
            hits.append((m.start(), obl))
    # Київщина и Київ одновременно по одной позиции: оставить область
    hits.sort()
    obls = []
    for _, c in hits:
        if c not in obls:
            obls.append(c)
    if "kyivobl" in obls and "kyiv" in obls and not re.search(r"\bкиє?в(а|і|ом|у)?\b|\bкиїв\b", l.replace("київщин", "")):
        obls.remove("kyiv")
    course = None
    m = re.search(r"(курс\w*|напрямк\w*|вектор\w*)[^.]*", l)
    seg = m.group(0) if m else ""
    for stem, code in DIRS:
        if stem in seg:
            course = code
            break
    frm = None
    for stem, code in FROM_DIRS:
        if stem in l:
            frm = code
            break
    return obls, place, course, frm


WORDNUM = {"одній": 1, "одна": 1, "двох": 2, "дві": 2, "два": 2, "двома": 2, "трьох": 3, "три": 3, "трьома": 3, "чотирьох": 4,
           "чотири": 4, "п'яти": 5, "пʼяти": 5, "шести": 6, "семи": 7, "восьми": 8, "дев'яти": 9, "девʼяти": 9, "десяти": 10, "декількох": None}


def num(s):
    if s is None:
        return None
    s = s.strip().lower()
    if s.isdigit():
        return int(s)
    return WORDNUM.get(s)


def parse_report(text, t):
    l = re.sub(r"S-?8000", "SBAN", text.replace("\n", " "))
    r = {"kind": "night" if re.search(r"у ніч на", l, re.I) else "day"}
    m = re.search(r"ЗБИТО/ПОДАВЛЕНО\s+(\d+)", l)
    r["downedTotal"] = int(m.group(1)) if m else None
    att = re.search(r"(?:атакував|завдав)(.*?)(?:із напрямк|Повітряний напад|💥|$)", l, re.S)
    a = att.group(1) if att else ""
    m = re.search(r"(\d+)\s+ударн\w*\s+БпЛА", a)
    r["uav"] = int(m.group(1)) if m else None
    m = re.search(r"ударн\w*\s+БпЛА[^()]*\((?:в т\.\s?ч\.\s*|із них\s*[-–—]*\s*)?(\d+)\s+(?:із них\s*[-–—]*\s*)?реактивн", a)
    r["uavJet"] = int(m.group(1)) if m else None
    missiles = []
    for mm in re.finditer(r"(\d+|\w+)\s+(балістичн\w*|крилат\w*|керован\w* авіаційн\w*|протикорабельн\w*|зенітн\w* керован\w*)\s+(?:\([^)]*\)\s+)?ракет\w*([^,;]*)", a):
        missiles.append({"n": num(mm.group(1)), "kind": mm.group(2), "detail": mm.group(3).strip()[:80]})
    if not missiles and re.search(r"ракет", a):
        for mm in re.finditer(r"(балістичн\w*|крилат\w*|протикорабельн\w*)\s+ракет\w*\s*([^,]*)", a):
            missiles.append({"n": None, "kind": mm.group(1), "detail": mm.group(2).strip()[:80]})
    r["missiles"] = missiles
    r["banderol"] = bool(re.search(r"бандерол", a, re.I))
    m = re.search(r"(?<![\w-])(\d+)(?:-\w{1,3})?\s+(?:SBAN\s+)?Бандерол", a)
    r["banderolN"] = int(m.group(1)) if m else None
    ds = re.search(r"збито/подавлено(.*?)(?:Зафіксовано|В повітряному|❗|✊|$)", l, re.S | re.I)
    d = ds.group(1) if ds else ""
    m = re.search(r"(\d+)\s+(?:ворож\w+\s+)?БпЛА", d)
    r["uavDown"] = int(m.group(1)) if m else None
    m = re.search(r"БпЛА[^()]*\((?:в т\.\s?ч\.\s*|із них\s*[-–—]*\s*)?(\d+)\s+(?:із них\s*[-–—]*\s*)?реактивн", d)
    r["uavJetDown"] = int(m.group(1)) if m else None
    m = re.search(r"(?<![\w-])(\d+|\w+)(?:-\w{1,3})?\s+(?:SBAN\s+)?Бандерол", d)
    r["banderolDown"] = num(m.group(1)) if m else None
    m = re.search(r"(\d+)\s+(?:ракет|балістичн|крилат)", d)
    r["missileDown"] = int(m.group(1)) if m else None
    m = re.search(r"влучання[^.]*?на\s+(\d+|\w+)\s+локац", l)
    r["hits"] = num(m.group(1)) if m else None
    m = re.search(r"уламки\)?\s+на\s+(\d+|\w+)\s+локац", l)
    r["debris"] = num(m.group(1)) if m else None
    m = re.search(r"із напрямк\w*:\s*(.*?)\.\s", l + " ")
    dirs = []
    if m:
        raw = re.sub(r"\s*[-–—]\s*(рф|ТОТ)\b", ",", m.group(1))
        for part in re.split(r",|\s[-–—]\s", raw):
            part = re.sub(r"\b(рф|ТОТ|АР)\b", "", part).strip(" –-—")
            if part and len(part) < 40:
                dirs.append(part)
    r["launchFrom"] = dirs
    m = re.search(r"станом на (\d{1,2}[:.]\d{2})", l)
    r["asOf"] = m.group(1) if m else None
    # итог сбитых: из заголовка, иначе сумма
    if r["downedTotal"] is None:
        parts = [r["uavDown"], r["banderolDown"], r["missileDown"]]
        r["downedTotal"] = sum(p for p in parts if p) or None
    attacked = (r["uav"] or 0) + (r["banderolN"] or 0) + sum(x["n"] or 0 for x in missiles)
    r["attackedTotal"] = attacked or None
    return r

# ----------------------------------------------------------------- разбор

def to_kyiv(iso):
    return datetime.fromisoformat(iso).astimezone(KYIV)


def build(cache):
    posts = sorted(cache.values(), key=lambda p: p["id"])
    byid = {p["id"]: p for p in posts}
    events, reports, unparsed, threats = [], [], [], []
    line_events = {}   # post id → список событий (по строкам)
    prev = None        # последний пост с типом (для «Деміївка.» без reply)
    for p in posts:
        text = p["text"]
        if not text:
            continue
        kind = classify(text)
        if kind == "report":
            rep = parse_report(text, p["t"])
            rep.update({"id": p["id"], "t": p["t"], "text": text})
            reports.append(rep)
            continue
        if kind == "threat":
            threats.append(("start", p))
            continue
        if kind == "clear":
            threats.append(("end", p))
            continue
        lines = [ln.strip() for ln in text.split("\n") if ln.strip() and not re.fullmatch(r"[➖\s]+", ln.strip())]
        evs = []
        skipped_kind = None
        parent_ev = None
        if p["reply"] and p["reply"] in line_events and line_events[p["reply"]]:
            cands = line_events[p["reply"]]
            parent_ev = cands[0]
            if len(cands) > 1:
                key = (p.get("quote") or "").lower()
                best = None
                if key:
                    for ev in cands:
                        if ev["x"].lower()[:30] in key or key[:30] in ev["x"].lower():
                            best = ev
                if best is None:
                    o, _, _, _ = geo(text)
                    for ev in cands:
                        if set(o) & set(ev["o"]):
                            best = ev
                            break
                parent_ev = best or cands[0]
        for ln in lines:
            if re.fullmatch(r"[⚠️❗\s]*(Увага!?)?", ln) or ln.rstrip().endswith(":"):
                continue
            typ = classify(ln)
            inherited = False
            if typ in (None, "report", "threat", "clear"):
                if parent_ev:
                    typ, inherited = parent_ev["k"], True
                elif prev and (datetime.fromisoformat(p["t"]) - datetime.fromisoformat(prev["t"])).total_seconds() < 600 and len(ln) < 60:
                    typ, inherited = prev["k"], True
                else:
                    unparsed.append({"id": p["id"], "t": p["t"], "x": ln})
                    continue
            if typ in EXCLUDE:  # КАБы и предупреждения о тактической авиации не анализируем
                skipped_kind = typ
                continue
            o, place, course, frm = geo(ln)
            if typ == "ballistic" and not o and ln.endswith("!"):
                place = ln.strip("🚀! ")
            if not o and inherited and parent_ev:
                # «Святошин.» в треке, где район не распознан — область берём у родителя
                o = parent_ev["o"][-1:]
            if not o and not place and typ in ("jet", "shahed", "kab", "banderol", "missile", "fast"):
                unparsed.append({"id": p["id"], "t": p["t"], "x": ln, "why": "нет географии"})
            ev = {
                "id": p["id"], "t": p["t"], "k": typ, "o": o, "p": place, "c": course, "f": frm,
                "pl": bool(re.search(r"реактивні|бпла\b.*\bта\b|бандеролі|кабы|каби|декілька|групи", ln.lower())),
                "x": ln, "tr": None, "par": parent_ev["uid"] if parent_ev else None,
            }
            ev["uid"] = f"{p['id']}.{len(evs)}"
            evs.append(ev)
        if evs:
            line_events[p["id"]] = evs
            events.extend(evs)
            prev = {"t": p["t"], "k": evs[-1]["k"]}
        elif skipped_kind:
            prev = {"t": p["t"], "k": skipped_kind}  # продолжения КАБ-поста тоже отбрасываются
        # пост-ответ на угрозу баллистики («Балістика через Сумщину!») уже типизирован выше
    # --- треки: union по ссылке на родителя
    uid_ev = {e["uid"]: e for e in events}
    for e in events:
        root = e
        seen = 0
        while root["par"] and root["par"] in uid_ev and seen < 50:
            root = uid_ev[root["par"]]
            seen += 1
        e["tr"] = root["uid"]
    # цепочки ответов на пост угрозы баллистики: такие посты — корни треков балістики
    tracks = {}
    for e in events:
        tracks.setdefault(e["tr"], []).append(e)
    track_list = []
    for root, evs in tracks.items():
        if len(evs) < 2:
            continue
        evs.sort(key=lambda e: e["t"])
        route = []
        for e in evs:
            for c in e["o"]:
                if c != "ru" and (not route or route[-1] != c):
                    route.append(c)
        t0, t1 = to_kyiv(evs[0]["t"]), to_kyiv(evs[-1]["t"])
        track_list.append({
            "id": root, "k": evs[0]["k"], "t0": evs[0]["t"], "t1": evs[-1]["t"],
            "min": round((t1 - t0).total_seconds() / 60),
            "route": route, "n": len(evs),
            "pts": [{"t": e["t"], "p": e["p"], "o": e["o"][-1] if e["o"] else None, "c": e["c"], "x": e["x"]} for e in evs],
        })
    # --- интервалы баллистической угрозы
    intervals, cur = [], None
    for kind, p in sorted(threats, key=lambda x: x[1]["id"]):
        t = datetime.fromisoformat(p["t"])
        if kind == "start":
            if cur and (t - cur["last"]).total_seconds() < 45 * 60:
                cur["last"] = t
                cur["n"] += 1
            else:
                if cur:
                    cur["end"] = cur["last"] + timedelta(minutes=15)
                    cur["cleared"] = False
                    intervals.append(cur)
                frm = re.search(r"з (півночі|півдня|сходу|заходу|[А-ЯІЇЄ][\w-]+)", p["text"])
                cur = {"start": t, "last": t, "n": 1, "from": frm.group(1) if frm else None, "id": p["id"]}
        else:
            if cur:
                cur["end"], cur["cleared"] = t, True
                intervals.append(cur)
                cur = None
    if cur:
        cur["end"], cur["cleared"] = None, False  # ещё активна
        intervals.append(cur)
    ballistic = [{
        "id": i["id"], "start": i["start"].isoformat(), "end": i["end"].isoformat() if i["end"] else None,
        "cleared": i["cleared"], "from": i["from"], "n": i["n"],
    } for i in intervals]
    # треки балістики, привязанные reply к посту ☄️, в events не попадают как корень — это ок
    return events, track_list, reports, ballistic, unparsed

# ----------------------------------------------------------------- документы базы

def day_key(iso):
    return to_kyiv(iso).strftime("%Y-%m-%d")


def make_docs(events, tracks, reports, ballistic, unparsed, fetched):
    docs = {}
    days = {}
    for e in events:
        d = days.setdefault(day_key(e["t"]), {"events": [], "tracks": [], "unparsed": []})
        d["events"].append({k: e[k] for k in ("id", "t", "k", "o", "p", "c", "f", "pl", "x", "tr")})
    for tr in tracks:
        days.setdefault(day_key(tr["t0"]), {"events": [], "tracks": [], "unparsed": []})["tracks"].append(tr)
    for u in unparsed:
        days.setdefault(day_key(u["t"]), {"events": [], "tracks": [], "unparsed": []})["unparsed"].append(u)
    for k, d in days.items():
        d["date"] = k
        docs[f"days/{k}"] = d
    for r in reports:
        docs[f"reports/{r['id']}"] = r
    now = datetime.now(timezone.utc)
    docs["meta/ballistic"] = {"intervals": ballistic}
    docs["meta/state"] = {
        "updatedAt": now.isoformat(),
        "lastPostId": max((e["id"] for e in events), default=None),
        "lastPostAt": max((e["t"] for e in events), default=None),
        "events": len(events), "tracks": len(tracks), "reports": len(reports),
        "unparsed": len(unparsed), "newPosts": fetched,
        "from": min((e["t"] for e in events), default=None),
        "oblasts": OBL_NAME, "types": TYPE_NAMES,
    }
    return docs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
    ap.add_argument("--offline", action="store_true", help="не ходить в сеть, только пересобрать")
    a = ap.parse_args()
    os.makedirs(os.path.join(a.out, "docs"), exist_ok=True)
    cache_path = os.path.join(a.out, "posts.json")
    cache = {}
    if os.path.exists(cache_path):
        cache = {int(k): v for k, v in json.load(open(cache_path, encoding="utf-8")).items()}
    fetched = pages = 0
    if not a.offline:
        fetched, pages = update_cache(cache, a.days)
        json.dump(cache, open(cache_path, "w", encoding="utf-8"), ensure_ascii=False)
    events, tracks, reports, ballistic, unparsed = build(cache)
    docs = make_docs(events, tracks, reports, ballistic, unparsed, fetched)
    # какие документы изменились с прошлого запуска
    hash_path = os.path.join(a.out, "hashes.json")
    old = json.load(open(hash_path)) if os.path.exists(hash_path) else {}
    new, changed = {}, []
    for path, doc in docs.items():
        body = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        h = hashlib.sha1(body.encode()).hexdigest()
        new[path] = h
        fname = path.replace("/", "__") + ".json"
        with open(os.path.join(a.out, "docs", fname), "w", encoding="utf-8") as f:
            f.write(body)
        if old.get(path) != h and path != "meta/state":
            changed.append(path)
    if changed or not old:
        changed.append("meta/state")
    removed = [p for p in old if p not in new]
    json.dump(new, open(hash_path, "w"))
    json.dump({"changed": changed, "removed": removed}, open(os.path.join(a.out, "changed.json"), "w"), ensure_ascii=False)
    print(json.dumps({"posts": len(cache), "new": fetched, "pages": pages, "events": len(events), "tracks": len(tracks),
                      "reports": len(reports), "ballistic": len(ballistic), "unparsed": len(unparsed),
                      "changed": len(changed), "removed": len(removed)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
