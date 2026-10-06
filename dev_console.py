"""Вкладка «Разработка» в админке: задача текстом → Claude правит main.py → коммит в GitHub → Railway деплоит.
Сам этот файл агенту НЕ доступен (правится только main.py и виджеты внутри него)."""
import os, re, json, base64, ast, time, uuid, threading, fcntl, shutil, subprocess, tempfile
from datetime import datetime
import requests
from flask import request, jsonify, Response

REPO = os.environ.get("DEV_REPO", "konstantin514113-dotcom/mydeal")
BRANCH = os.environ.get("DEV_BRANCH", "main")
FILE = "main.py"
GH = "https://api.github.com"
DEV_PASS = os.environ.get("DEV_PASS", "210722")
MODEL = os.environ.get("DEV_MODEL", "claude-sonnet-4-6")
JOBS_DIR = os.path.join(tempfile.gettempdir(), "rj_dev_jobs")
os.makedirs(JOBS_DIR, exist_ok=True)

BLOBS = {"BOOKING_HTML_B64": "booking_widget.html", "STATS_HTML_B64": "stats_page.html"}
BOM = b"\xef\xbb\xbf"
HOOK = "import dev_console as _dev_console; _dev_console.register(app)  # вкладка «Разработка», не удалять"


def ensure_hook(main_text):
    if HOOK in main_text:
        return main_text
    return main_text.replace("app = Flask(__name__)\n", "app = Flask(__name__)\n" + HOOK + "\n", 1)


# ---------------- GitHub ----------------
def _h(raw=False):
    return {"Authorization": "Bearer " + os.environ.get("GITHUB_TOKEN", ""),
            "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json"}


def gh_get_file(ref=BRANCH):
    meta = requests.get(f"{GH}/repos/{REPO}/contents/{FILE}", params={"ref": ref}, headers=_h(), timeout=30)
    meta.raise_for_status()
    raw = requests.get(f"{GH}/repos/{REPO}/contents/{FILE}", params={"ref": ref}, headers=_h(raw=True), timeout=60)
    raw.raise_for_status()
    return raw.content.decode("utf-8"), meta.json()["sha"]


def gh_put_file(text, sha, message):
    body = {"message": message, "content": base64.b64encode(text.encode("utf-8")).decode(),
            "sha": sha, "branch": BRANCH,
            "committer": {"name": "Админка R&J (Разработка)", "email": "konstantin514113@gmail.com"}}
    r = requests.put(f"{GH}/repos/{REPO}/contents/{FILE}", headers=_h(), json=body, timeout=60)
    r.raise_for_status()
    return r.json()["commit"]["sha"]


def gh_history(n=30):
    r = requests.get(f"{GH}/repos/{REPO}/commits", params={"path": FILE, "sha": BRANCH, "per_page": n},
                     headers=_h(), timeout=30)
    r.raise_for_status()
    out = []
    for c in r.json():
        msg = c["commit"]["message"]
        out.append({"sha": c["sha"], "short": c["sha"][:7], "title": msg.split("\n")[0],
                    "body": "\n".join(msg.split("\n")[1:]).strip(),
                    "date": c["commit"]["committer"]["date"],
                    "who": c["commit"]["committer"]["name"]})
    return out


# ---------------- Виртуальные файлы ----------------
def split_workspace(main_text):
    files = {}
    bom = {}
    for var, fname in BLOBS.items():
        m = re.search(var + r'\s*=\s*"([^"]+)"', main_text)
        data = base64.b64decode(m.group(1))
        bom[fname] = data.startswith(BOM)
        files[fname] = data[3:].decode("utf-8") if bom[fname] else data.decode("utf-8")
        main_text = main_text[:m.start(1)] + f"__VIRTUAL_FILE:{fname}__" + main_text[m.end(1):]
    files["main.py"] = main_text
    return files, bom


def join_workspace(files, bom):
    main = files["main.py"]
    for var, fname in BLOBS.items():
        ph = f"__VIRTUAL_FILE:{fname}__"
        if main.count(ph) != 1:
            raise ValueError(f"Служебная метка {ph} в main.py повреждена")
        data = files[fname].encode("utf-8")
        if bom[fname]:
            data = BOM + data
        main = main.replace(ph, base64.b64encode(data).decode())
    return main


def validate(files, full_main):
    if HOOK not in full_main:
        return "Удалена строка подключения вкладки «Разработка»"
    try:
        ast.parse(full_main)
    except SyntaxError as e:
        return f"Синтаксическая ошибка Python в main.py, строка {e.lineno}: {e.msg}"
    node = shutil.which("node")
    if node:
        for fname in BLOBS.values():
            js = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", files[fname], re.S))
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                f.write(js)
            p = subprocess.run([node, "--check", f.name], capture_output=True, text=True)
            os.unlink(f.name)
            if p.returncode != 0:
                return f"Ошибка JavaScript в {fname}: {p.stderr[:600]}"
    return None


# ---------------- Задачи (общие для всех воркеров через /tmp) ----------------
def _job_path(jid):
    return os.path.join(JOBS_DIR, re.sub(r"[^a-z0-9]", "", jid) + ".json")


def job_save(job):
    with open(_job_path(job["id"]), "w") as f:
        json.dump(job, f, ensure_ascii=False)


def job_load(jid):
    try:
        with open(_job_path(jid)) as f:
            return json.load(f)
    except Exception:
        return None


def job_log(job, text):
    job["log"].append(text)
    job_save(job)


# ---------------- Агент ----------------
SYSTEM = """Ты — разработчик сайта груминг-салона R&J Grooming (Таллин). Тебе пишет администратор салона простыми словами (часто голосом, бывают искажения: «Ксюша»=Ксения, «Таня»=Татьяна, «кликай»=кли-кай). Твоя задача — внести нужное изменение в код сайта и коротко по-русски объяснить, что сделано.

Файлы (виртуальные):
- main.py — Flask-сервер. Страницы /admin/* — это Python f-строки: фигурные скобки в CSS/JS внутри них пишутся двойными {{ }}.
- booking_widget.html — виджет онлайн-записи /app (RU/EN/ET). Цены и породы — массив `var DATA = [...]` (записи {"breed","services":{услуга:цена},"breed_en","breed_et"}; разные веса — отдельные записи). Какой мастер какие услуги/породы берёт — функция filterMasters(). Переводы услуг/описаний — объекты рядом.
- stats_page.html — страница /stats.

Мастера (русское имя — ключ календаря, НЕ переименовывать): Татьяна, Александра, Ксения, Анна, Алиса, Кристина.
Услуги: Базовый уход, Гигиенический уход, Комплексный уход, Экспресс-линька, Тримминг, Вычес/Вычёс (кошки), Вся программа (щенки).

Правила:
1. Сначала найди место (grep/view), потом правь точечно (replace). Не переписывай большие куски без нужды.
2. При добавлении породы всегда заполняй breed_en и breed_et.
3. Не трогай пароли, ключи, адреса API, маршруты /admin/dev, служебные метки __VIRTUAL_FILE:...__.
4. Расписание мастеров и записи живут в Google Календаре, логика слотов — в Google Apps Script: это отсюда не меняется. Если просят такое — ничего не меняй и объясни, что делается в календаре.
5. Если задача неясна или опасна (удалить много данных, сломать запись) — не меняй ничего, задай уточняющий вопрос в finish.
6. В конце обязательно вызови finish: summary — 1–3 простых предложения для администратора, title — короткое описание изменения для истории (до 70 символов).
"""

TOOLS = [
    {"name": "grep", "description": "Поиск по регулярному выражению (Python re, без учёта регистра). Возвращает 'файл:строка: текст'.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"},
                                                        "file": {"type": "string", "description": "main.py | booking_widget.html | stats_page.html; пусто — все"}},
                      "required": ["pattern"]}},
    {"name": "view", "description": "Показать строки файла (до 300 строк за раз).",
     "input_schema": {"type": "object", "properties": {"file": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}},
                      "required": ["file", "start", "end"]}},
    {"name": "replace", "description": "Заменить точный фрагмент old на new. old должен встречаться в файле ровно один раз (иначе добавь контекст).",
     "input_schema": {"type": "object", "properties": {"file": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
                      "required": ["file", "old", "new"]}},
    {"name": "finish", "description": "Завершить. changed=true, если были правки.",
     "input_schema": {"type": "object", "properties": {"changed": {"type": "boolean"}, "title": {"type": "string"}, "summary": {"type": "string"}},
                      "required": ["changed", "summary"]}},
]


def _cut(s, n=400):
    return s if len(s) <= n else s[:n] + f"…(+{len(s)-n} симв.)"


def run_tool(files, name, a):
    f = a.get("file") or ""
    if name == "grep":
        try:
            rx = re.compile(a["pattern"], re.I)
        except re.error as e:
            return f"Ошибка regex: {e}"
        out = []
        for fn, txt in files.items():
            if f and fn != f:
                continue
            for i, line in enumerate(txt.split("\n"), 1):
                if rx.search(line):
                    if len(line) > 400:
                        m = rx.search(line)
                        s = max(0, m.start() - 150)
                        line = "…" + line[s:s + 400] + "…"
                    out.append(f"{fn}:{i}: {line}")
                    if len(out) >= 60:
                        return "\n".join(out) + "\n(показаны первые 60)"
        return "\n".join(out) or "Ничего не найдено"
    if f not in files:
        return f"Нет файла {f}. Есть: {', '.join(files)}"
    if name == "view":
        lines = files[f].split("\n")
        s, e = max(1, int(a["start"])), min(len(lines), int(a["end"]), int(a["start"]) + 299)
        return "\n".join(f"{i}: {_cut(lines[i-1], 600)}" for i in range(s, e + 1)) or "Пусто"
    if name == "replace":
        old, new = a["old"], a["new"]
        n = files[f].count(old)
        if n != 1:
            return f"Фрагмент найден {n} раз(а), нужно ровно 1 — уточни."
        if "__VIRTUAL_FILE:" in old and old.count("__VIRTUAL_FILE:") != new.count("__VIRTUAL_FILE:"):
            return "Нельзя трогать служебные метки."
        files[f] = files[f].replace(old, new)
        return "Готово."
    return "Неизвестный инструмент"


def run_job(jid, task):
    job = job_load(jid)
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        job_log(job, "Загружаю текущую версию сайта…")
        text, sha = gh_get_file()
        files, bom = split_workspace(text)
        try:
            recent = "\n".join("- " + h["title"] for h in gh_history(8))
        except Exception:
            recent = ""
        msgs = [{"role": "user", "content": f"Последние изменения сайта:\n{recent}\n\nЗадача администратора:\n{task}"}]
        result = None
        for step in range(40):
            resp = client.messages.create(model=MODEL, max_tokens=8000, system=SYSTEM, tools=TOOLS, messages=msgs)
            msgs.append({"role": "assistant", "content": resp.content})
            calls = [b for b in resp.content if b.type == "tool_use"]
            if not calls:
                txt = "".join(b.text for b in resp.content if b.type == "text")
                result = {"changed": False, "summary": txt or "Без изменений."}
                break
            outs = []
            for c in calls:
                if c.name == "finish":
                    result = c.input
                    outs.append({"type": "tool_result", "tool_use_id": c.id, "content": "ok"})
                    continue
                label = {"grep": "Ищу", "view": "Смотрю", "replace": "Правлю"}.get(c.name, c.name)
                job_log(job, f"{label}: {c.input.get('file') or ''} {_cut(c.input.get('pattern',''), 60)}".strip())
                outs.append({"type": "tool_result", "tool_use_id": c.id, "content": run_tool(files, c.name, c.input)})
            msgs.append({"role": "user", "content": outs})
            if result:
                break
        if not result:
            raise RuntimeError("Не уложился в 40 шагов — переформулируйте задачу проще.")

        orig_files, _ = split_workspace(text)
        really_changed = any(files[k] != orig_files[k] for k in files)
        if result.get("changed") and really_changed:
            job_log(job, "Проверяю код…")
            full = join_workspace(files, bom)
            err = validate(files, full)
            if err:
                raise RuntimeError("Изменение не опубликовано, проверка не пройдена: " + err)
            job_log(job, "Публикую…")
            title = (result.get("title") or result["summary"])[:70]
            commit = gh_put_file(full, sha, f"[Разработка] {title}\n\nЗапрос: {task}\n\nИтог: {result['summary']}")
            job.update(status="done", summary=result["summary"], commit=commit[:7], published=True)
        else:
            job.update(status="done", summary=result.get("summary", ""), published=False)
    except Exception as e:
        job.update(status="error", summary=str(e)[:1500])
    job_save(job)


# ---------------- Маршруты ----------------
def _ok():
    ok = request.headers.get("X-Dev-Pass", "") == DEV_PASS
    if not ok:
        time.sleep(1)
    return ok


def register(app):
    @app.route("/admin/dev")
    def dev_page():
        return Response(PAGE, mimetype="text/html", headers={"Cache-Control": "no-store"})

    @app.route("/admin/dev/run", methods=["POST"])
    def dev_run():
        if not _ok():
            return jsonify(error="Нет доступа"), 403
        task = ((request.get_json(silent=True) or {}).get("task") or "").strip()
        if not task:
            return jsonify(error="Пустая задача"), 400
        for fn in os.listdir(JOBS_DIR):
            j = job_load(fn[:-5])
            if j and j["status"] == "running" and time.time() - j["started"] < 900:
                return jsonify(error="Уже выполняется другая задача, подождите."), 409
        jid = uuid.uuid4().hex[:12]
        job_save({"id": jid, "task": task, "status": "running", "log": [], "started": time.time()})
        threading.Thread(target=run_job, args=(jid, task), daemon=True).start()
        return jsonify(id=jid)

    @app.route("/admin/dev/job/<jid>")
    def dev_job(jid):
        if not _ok():
            return jsonify(error="Нет доступа"), 403
        j = job_load(jid)
        return (jsonify(j), 200) if j else (jsonify(error="not_found"), 404)

    @app.route("/admin/dev/history")
    def dev_history():
        if not _ok():
            return jsonify(error="Нет доступа"), 403
        try:
            return jsonify(items=gh_history(30))
        except Exception as e:
            return jsonify(error=str(e)), 500

    @app.route("/admin/dev/rollback", methods=["POST"])
    def dev_rollback():
        if not _ok():
            return jsonify(error="Нет доступа"), 403
        target = (request.get_json(silent=True) or {}).get("sha", "")
        if not re.fullmatch(r"[0-9a-f]{40}", target):
            return jsonify(error="Неверная версия"), 400
        try:
            old_text, _ = gh_get_file(ref=target)
            _, cur_sha = gh_get_file()
            info = next((h for h in gh_history(60) if h["sha"] == target), None)
            title = info["title"] if info else target[:7]
            old_text = ensure_hook(old_text)
            c = gh_put_file(old_text, cur_sha, f"[Откат] вернули версию {target[:7]}: {title[:60]}")
            return jsonify(ok=True, commit=c[:7])
        except Exception as e:
            return jsonify(error=str(e)), 500


PAGE = r"""<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Разработка — R&J Grooming</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@600&family=Montserrat:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0a09;color:#f2ede2;font-family:'Montserrat',sans-serif;padding:36px 16px 80px}
.wrap{max-width:720px;margin:0 auto}
a{color:#c9a05a}
.back{font-size:.8rem;text-decoration:none}
h1{font-family:'Playfair Display',serif;font-size:2rem;margin:14px 0 4px}
.sub{font-size:.8rem;color:rgba(242,237,226,.55);margin-bottom:24px;line-height:1.5}
.lbl{font-size:.66rem;letter-spacing:.2em;text-transform:uppercase;color:#c9a05a;margin:28px 0 12px}
textarea{width:100%;min-height:120px;background:#141310;color:#f2ede2;border:1px solid rgba(201,160,90,.25);border-radius:12px;padding:14px;font:inherit;font-size:.95rem;resize:vertical}
button{background:#c9a05a;color:#0a0a09;border:0;border-radius:10px;padding:12px 20px;font:inherit;font-weight:600;cursor:pointer}
button:disabled{opacity:.5}
.btn2{background:transparent;color:#c9a05a;border:1px solid rgba(201,160,90,.4);padding:7px 12px;font-size:.75rem}
.box{background:#141310;border:1px solid rgba(201,160,90,.18);border-radius:12px;padding:14px 16px;margin-top:14px;font-size:.88rem;line-height:1.5}
.log{font-size:.75rem;color:rgba(242,237,226,.55);white-space:pre-wrap;margin-top:8px}
.ok{border-color:#6fae7a}.err{border-color:#e0824a}
.item{display:flex;gap:12px;align-items:flex-start;padding:12px 0;border-bottom:1px solid rgba(242,237,226,.08)}
.item .t{flex:1;font-size:.85rem}.item .m{font-size:.7rem;color:rgba(242,237,226,.45);margin-top:3px}
.dev{color:#c9a05a}
</style></head><body><div class="wrap">
<a class="back" href="/admin?pass=anza1985">← Админ-панель</a>
<h1>Разработка</h1>
<div class="sub">Опишите простыми словами, что изменить на сайте: цены, породы, услуги мастеров, тексты. Изменение появится на сайте через 1–2 минуты. Любую версию можно вернуть в истории ниже.</div>
<div id="login"><div class="sub">Введите пароль разработки.</div>
<input id="pw" type="password" inputmode="numeric" autocomplete="off" style="width:100%;background:#141310;color:#f2ede2;border:1px solid rgba(201,160,90,.25);border-radius:12px;padding:14px;font:inherit;font-size:1rem">
<div style="margin-top:12px"><button id="enter">Войти</button></div><div id="lerr" class="log" style="color:#e0824a"></div></div>
<div id="app" style="display:none">
<textarea id="task" placeholder="Например: подними цены на Шпиц на 5 € по всем услугам"></textarea>
<div style="margin-top:12px"><button id="go">Выполнить</button></div>
<div id="out"></div>
<div class="lbl">История изменений</div>
<div id="hist">Загрузка…</div>
</div></div>
<script>
var P='';
function q(u){return u;}
var _f=window.fetch.bind(window);
function fetch(u,o){o=o||{};o.headers=Object.assign({},o.headers||{},{'X-Dev-Pass':P});return _f(u,o);}
document.getElementById('enter').onclick=function(){
  P=document.getElementById('pw').value.trim();
  fetch('/admin/dev/history').then(function(r){
    if(r.status===403){document.getElementById('lerr').textContent='Неверный пароль';return;}
    document.getElementById('login').style.display='none';document.getElementById('app').style.display='';loadHist();
  });
};
document.getElementById('pw').onkeydown=function(e){if(e.key==='Enter')document.getElementById('enter').click();};
function esc(s){return String(s||'').replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
var out=document.getElementById('out'),go=document.getElementById('go');
go.onclick=function(){
  var t=document.getElementById('task').value.trim(); if(!t) return;
  go.disabled=true; out.innerHTML='<div class="box">Работаю…<div class="log" id="lg"></div></div>';
  fetch(q('/admin/dev/run'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task:t})})
   .then(function(r){return r.json();}).then(function(d){
     if(d.error){out.innerHTML='<div class="box err">'+esc(d.error)+'</div>';go.disabled=false;return;}
     poll(d.id,0);
   }).catch(function(e){out.innerHTML='<div class="box err">Ошибка сети</div>';go.disabled=false;});
};
function poll(id,miss){
  fetch(q('/admin/dev/job/'+id)).then(function(r){return r.json();}).then(function(j){
    if(j.error){ if(miss<5){setTimeout(function(){poll(id,miss+1);},3000);} else {out.innerHTML='<div class="box">Статус потерян (сайт обновлялся). Проверьте историю ниже.</div>';go.disabled=false;loadHist();} return; }
    if(j.status==='running'){var lg=document.getElementById('lg'); if(lg) lg.textContent=j.log.join('\n'); setTimeout(function(){poll(id,0);},2000); return;}
    var cls=j.status==='done'?'ok':'err';
    var extra=j.published?'<div class="log">Опубликовано (версия '+esc(j.commit)+'). Сайт обновится через 1–2 минуты.</div>':(j.status==='done'?'<div class="log">Изменений на сайте нет.</div>':'');
    out.innerHTML='<div class="box '+cls+'">'+esc(j.summary).replace(/\n/g,'<br>')+extra+'</div>';
    go.disabled=false; if(j.published){document.getElementById('task').value='';} loadHist();
  }).catch(function(){ if(miss<5) setTimeout(function(){poll(id,miss+1);},3000); });
}
function loadHist(){
  fetch(q('/admin/dev/history')).then(function(r){return r.json();}).then(function(d){
    if(d.error){document.getElementById('hist').textContent='Не удалось загрузить: '+d.error;return;}
    document.getElementById('hist').innerHTML=d.items.map(function(h,i){
      var dt=new Date(h.date).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
      var isDev=h.title.indexOf('[Разработка]')===0||h.title.indexOf('[Откат]')===0;
      return '<div class="item"><div class="t"><div class="'+(isDev?'dev':'')+'">'+esc(h.title)+'</div><div class="m">'+dt+' · '+esc(h.who)+' · '+h.short+(i===0?' · текущая':'')+'</div></div>'+
        (i===0?'':'<button class="btn2" onclick="rb(\''+h.sha+'\',\''+h.short+'\')">Вернуть</button>')+'</div>';
    }).join('');
  });
}
function rb(sha,short){
  if(!confirm('Вернуть сайт к версии '+short+'? Все изменения, сделанные после неё, будут отменены.')) return;
  fetch(q('/admin/dev/rollback'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sha:sha})})
   .then(function(r){return r.json();}).then(function(d){
     alert(d.error?('Ошибка: '+d.error):'Готово. Сайт вернётся к этой версии через 1–2 минуты.'); loadHist();
   });
}
</script></body></html>"""
