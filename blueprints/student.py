# blueprints/student.py
from datetime import datetime, timedelta
import os
import re
from textwrap import dedent

from flask import Blueprint, render_template, render_template_string, redirect, url_for, request, abort, flash, jsonify, current_app, session
from flask_login import login_required, current_user
from sqlalchemy import asc, desc, func, nullslast, or_

# данные (если ещё не добавлял эти файлы — закомментируй импорт ниже)
from data.universities import UNIVERSITIES
from data.professions import PROFESSIONS  # title, slug?, short, full, tags, links, image

from extensions import db
from models import Test, TestQuestion, TestOption, TestAttempt, TestAnswer, AIThread, AIMessage

from openai import OpenAI

bp = Blueprint("student", __name__, template_folder="../templates")


# ====================== helpers: время старта/слуг/локаль ======================
def _get_start_dt(attempt):
    """Возвращает время старта попытки из того поля, которое есть в модели."""
    return (getattr(attempt, "started_at", None)
            or getattr(attempt, "created_at", None)
            or getattr(attempt, "created", None))


def _set_start_dt_if_possible(attempt, dt=None):
    """Устанавливает время старта, если в модели есть соответствующее поле."""
    dt = dt or datetime.utcnow()
    for field in ("started_at", "created_at", "created"):
        if hasattr(attempt, field):
            setattr(attempt, field, dt)
            return True
    return False


def _slugify_title(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", "-", s)
    return s


def _current_lang() -> str:
    """ru / en / kk — из Babel, сессии или дефолт ru."""
    # 1) если flask-babel установлен
    try:
        from flask_babel import get_locale  # type: ignore
        loc = str(get_locale() or "").split("_")[0]
        if loc in current_app.config.get("LANGUAGES", ["ru", "en", "kk"]):
            return loc
    except Exception:
        pass
    # 2) из сессии (маршрут /set-lang/<lang> ты уже добавлял в app.py)
    lang = session.get("lang")
    if lang in current_app.config.get("LANGUAGES", ["ru", "en", "kk"]):
        return lang
    # 3) дефолт
    return current_app.config.get("BABEL_DEFAULT_LOCALE", "ru")


# ====================== Guard ======================
@bp.before_request
def ensure_student():
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login", next=request.path))
    if getattr(current_user, "role", None) != "student":
        return redirect(url_for("public.index"))


# ====================== Навигация ======================
@bp.get("/dashboard")
@login_required
def dashboard():
    return redirect(url_for(".tests"))


@bp.get("/tests")
@login_required
def tests():
    tests = Test.query.order_by(Test.title.asc()).all()
    return render_template("student/tests.html", active="tests", tests=tests)


@bp.get("/tests/<slug>")
@login_required
def test_detail(slug):
    test = Test.query.filter_by(slug=slug).first_or_404()
    return render_template("student/test_detail.html", active="tests", test=test)


# ====================== Профессии ======================
@bp.get("/professions")
@login_required
def professions():
    """Список профессий с поиском по заголовку и тегам."""
    q = (request.args.get("q") or "").strip().lower()

    items = []
    for p in PROFESSIONS:
        item = dict(p)
        item["slug"] = p.get("slug") or _slugify_title(p.get("title", ""))
        item["tags"] = p.get("tags", [])
        if q:
            hay = " ".join([item.get("title", ""), item.get("short", ""), " ".join(item["tags"])]).lower()
            if q not in hay:
                continue
        items.append(item)

    items.sort(key=lambda x: x.get("title", "").lower())

    return render_template("student/professions.html", active="professions", items=items, q=q)


@bp.get("/professions/<slug>")
@login_required
def profession_detail(slug):
    p = None
    for it in PROFESSIONS:
        s = it.get("slug") or _slugify_title(it.get("title", ""))
        if s == slug:
            p = dict(it)
            p["slug"] = s
            break
    if not p:
        abort(404)

    return render_template("student/profession_detail.html", active="professions", p=p)


# ====================== Университеты ======================
@bp.get("/universities")
@login_required
def universities():
    items = sorted(UNIVERSITIES, key=lambda u: (u["city"], u["name"]))
    q = (request.args.get("q") or "").strip().lower()
    city = (request.args.get("city") or "").strip().lower()

    def ok(u):
        txt = f'{u["name"]} {u["city"]} {" ".join(u.get("program_groups", []))}'.lower()
        if q and q not in txt:
            return False
        if city and city not in (u["city"] or "").lower():
            return False
        return True

    filtered = [u for u in items if ok(u)]
    cities = sorted({u["city"] for u in items})

    return render_template("student/universities.html",
                           active="universities", items=filtered, cities=cities, q=q, city=city)


@bp.get("/universities/<slug>")
@login_required
def university_detail(slug):
    uni = next((u for u in UNIVERSITIES if u["slug"] == slug), None)
    if not uni:
        abort(404)
    return render_template("student/university_detail.html", active="universities", u=uni)


# ====================== Прохождение теста ======================
@bp.post("/tests/<slug>/start")
@login_required
def test_start(slug):
    test = Test.query.filter_by(slug=slug).first_or_404()
    attempt = TestAttempt(test_id=test.id, user_id=current_user.id)
    db.session.add(attempt)
    db.session.flush()
    _set_start_dt_if_possible(attempt)
    db.session.commit()
    return redirect(url_for(".attempt_question", attempt_id=attempt.id, order=1))


@bp.route("/attempt/<int:attempt_id>/q/<int:order>", methods=["GET", "POST"])
@login_required
def attempt_question(attempt_id, order):
    attempt = TestAttempt.query.get_or_404(attempt_id)
    if attempt.user_id != current_user.id:
        abort(403)

    test = Test.query.get_or_404(attempt.test_id)
    questions = (TestQuestion.query
                 .filter_by(test_id=test.id)
                 .order_by(asc(TestQuestion.order)).all())
    total = len(questions)
    if total == 0:
        flash("В этом тесте пока нет вопросов.", "warning")
        return redirect(url_for(".test_detail", slug=test.slug))

    if getattr(attempt, "finished_at", None):
        return redirect(url_for(".attempt_result", attempt_id=attempt.id))

    if order < 1:
        order = 1
    if order > total:
        return redirect(url_for(".attempt_finish", attempt_id=attempt.id))

    q = questions[order - 1]
    options = (TestOption.query
               .filter_by(question_id=q.id)
               .order_by(asc(TestOption.order))
               .all())

    if request.method == "POST":
        opt_id = request.form.get("option_id")
        if not opt_id:
            flash("Пожалуйста, выберите вариант.", "warning")
        else:
            ans = TestAnswer.query.filter_by(attempt_id=attempt.id, question_id=q.id).first()
            if ans is None:
                ans = TestAnswer(attempt_id=attempt.id, question_id=q.id)
                db.session.add(ans)
            ans.option_id = int(opt_id)
            db.session.commit()
            if order >= total:
                return redirect(url_for(".attempt_finish", attempt_id=attempt.id))
            return redirect(url_for(".attempt_question", attempt_id=attempt.id, order=order + 1))

    answered_ids = {a.question_id for a in TestAnswer.query.filter_by(attempt_id=attempt.id).all()}
    progress = int(100 * len(answered_ids) / total) if total else 0

    start_dt = _get_start_dt(attempt) or datetime.utcnow()
    ends_at = None
    if getattr(test, "duration_min", 0):
        ends_at = start_dt + timedelta(minutes=test.duration_min)

    prev = TestAnswer.query.filter_by(attempt_id=attempt.id, question_id=q.id).first()
    chosen_id = prev.option_id if prev else None

    return render_template("student/take_question.html",
                           active="tests",
                           test=test,
                           attempt=attempt,
                           q=q,
                           options=options,
                           order=order,
                           total=total,
                           progress=progress,
                           chosen_id=chosen_id,
                           ends_at=ends_at)


@bp.get("/attempt/<int:attempt_id>/finish")
@login_required
def attempt_finish(attempt_id):
    attempt = TestAttempt.query.get_or_404(attempt_id)
    if attempt.user_id != current_user.id:
        abort(403)

    if getattr(attempt, "finished_at", None):
        return redirect(url_for(".attempt_result", attempt_id=attempt.id))

    test = Test.query.get_or_404(attempt.test_id)
    questions = TestQuestion.query.filter_by(test_id=test.id).all()
    answers = TestAnswer.query.filter_by(attempt_id=attempt.id).all()

    raw, max_score = _calc_raw_total(questions, answers)

    attempt.finished_at = datetime.utcnow()

    total_q = len(questions)
    answered = len(answers)
    if hasattr(attempt, "score"):
        if max_score and total_q:
            attempt.score = round(100 * raw / max_score)
        else:
            attempt.score = round(100 * answered / total_q) if total_q else 0

    start_dt = _get_start_dt(attempt)
    if hasattr(attempt, "duration_sec") and start_dt:
        attempt.duration_sec = int((attempt.finished_at - start_dt).total_seconds())  # type: ignore

    if hasattr(attempt, "raw_score"):
        attempt.raw_score = raw
    if hasattr(attempt, "max_score"):
        attempt.max_score = max_score

    db.session.commit()
    return redirect(url_for(".attempt_result", attempt_id=attempt.id))


@bp.get("/attempt/<int:attempt_id>/result")
@login_required
def attempt_result(attempt_id):
    attempt = TestAttempt.query.get_or_404(attempt_id)
    if attempt.user_id != current_user.id:
        abort(403)

    test = Test.query.get_or_404(attempt.test_id)
    hide_details_for_student = bool(getattr(test, "confidential_student", False))

    questions = (TestQuestion.query
                 .filter_by(test_id=test.id)
                 .order_by(asc(TestQuestion.order)).all())
    answers = {a.question_id: a for a in TestAnswer.query.filter_by(attempt_id=attempt.id).all()}
    per_scale, raw, max_score = _calc_scales_and_totals(questions, answers)

    summary = _interpret(test.slug, per_scale, raw, max_score)

    rows = []
    if not hide_details_for_student:
        for q in questions:
            opts = TestOption.query.filter_by(question_id=q.id).order_by(asc(TestOption.order)).all()
            ans = answers.get(q.id)
            chosen = next((o for o in opts if ans and o.id == ans.option_id), None)
            rows.append({"q": q, "options": opts, "chosen": chosen})

    return render_template("student/attempt_result.html",
                           active="analysis",
                           test=test,
                           attempt=attempt,
                           rows=rows,
                           total=len(questions),
                           hide_details=hide_details_for_student,
                           summary=summary,
                           per_scale=per_scale)


@bp.get("/results")
@login_required
def my_results():
    # Подсчёт кол-ва ответов на попытку (чтобы показывать даже без finished_at/score)
    ans_count_sq = (
        db.session.query(TestAnswer.attempt_id, func.count().label("acnt"))
        .group_by(TestAnswer.attempt_id)
        .subquery()
    )

    fin_col   = getattr(TestAttempt, "finished_at", None)
    score_col = getattr(TestAttempt, "score", None)

    q = (
        db.session.query(
            TestAttempt,
            Test,
            func.coalesce(ans_count_sq.c.acnt, 0).label("acnt")
        )
        .join(Test, Test.id == TestAttempt.test_id)
        .outerjoin(ans_count_sq, ans_count_sq.c.attempt_id == TestAttempt.id)
        .filter(TestAttempt.user_id == current_user.id)
        .filter(Test.confidential_student == False)  # noqa: E712
    )

    conds = [func.coalesce(ans_count_sq.c.acnt, 0) > 0]
    if fin_col is not None:
        conds.append(fin_col.isnot(None))
    if score_col is not None:
        conds.append(score_col.isnot(None))
    q = q.filter(or_(*conds))

    order_by = [TestAttempt.id.desc()]
    if fin_col is not None:
        order_by.insert(0, nullslast(fin_col.desc()))
    rows = q.order_by(*order_by).all()

    attempts = [r[0] for r in rows]

    return render_template("student/results.html",
                           active="analysis",
                           attempts=attempts,
                           items=rows)


@bp.get("/analytics")
@login_required
def analytics_alias():
    return redirect(url_for(".my_results"))


# ====================== СЛУЖЕБНЫЕ: парсинг/подсчёт/интерпретации ======================
def _parse_value_to_scales(val: str):
    if val is None:
        return {}
    s = str(val).strip()
    if not s:
        return {}
    if s.upper() in {"E", "I", "S", "N", "T", "F", "J", "P"}:
        return {s.upper(): 1}
    if re.fullmatch(r"-?\d+", s):
        return {"TOTAL": int(s)}
    result = {}
    for token in re.split(r"[;,]", s):
        token = token.strip()
        if not token:
            continue
        m = re.match(r"([A-Za-zА-Яа-я_]+)\s*=?\s*([+-]?\d+)", token)
        if m:
            code = m.group(1).upper()
            pts = int(m.group(2))
            result[code] = result.get(code, 0) + pts
    return result


def _calc_raw_total(questions, answers):
    raw, max_score = 0, 0
    for q in questions:
        opts = TestOption.query.filter_by(question_id=q.id).order_by(asc(TestOption.order)).all()
        max_q = 0
        for o in opts:
            parsed = _parse_value_to_scales(o.value)
            max_q = max(max_q, parsed.get("TOTAL", 0))
        max_score += max_q

        ans = next((a for a in answers if a.question_id == q.id), None)
        if ans:
            chosen = next((o for o in opts if o.id == ans.option_id), None)
            if chosen is not None:
                parsed = _parse_value_to_scales(chosen.value)
                raw += parsed.get("TOTAL", 0)
    return raw, max_score


def _calc_scales_and_totals(questions, answers_by_qid):
    per = {}
    raw, max_score = 0, 0
    for q in questions:
        opts = TestOption.query.filter_by(question_id=q.id).order_by(asc(TestOption.order)).all()
        max_q_total = 0
        for o in opts:
            parsed = _parse_value_to_scales(o.value)
            max_q_total = max(max_q_total, parsed.get("TOTAL", 0))
        max_score += max_q_total

        ans = answers_by_qid.get(q.id)
        if not ans:
            continue
        chosen = next((o for o in opts if o.id == ans.option_id), None)
        if not chosen:
            continue
        parsed = _parse_value_to_scales(chosen.value)
        for code, pts in parsed.items():
            per[code] = per.get(code, 0) + pts
        raw += parsed.get("TOTAL", 0)
    return per, raw, max_score


def _interpret(slug: str, per, raw, max_score):
    slug = (slug or "").lower()

    if slug == "mbti":
        labels = {
            "E": "Экстраверсия", "I": "Интроверсия",
            "S": "Ощущение",     "N": "Интуиция",
            "T": "Мышление",     "F": "Чувство",
            "J": "Суждение",     "P": "Восприятие",
        }
        pairs = [("E", "I"), ("S", "N"), ("T", "F"), ("J", "P")]
        code, explain = "", []
        for a, b in pairs:
            va, vb = per.get(a, 0), per.get(b, 0)
            pick = a if va >= vb else b
            code += pick
            trend = "≈" if va == vb else ">"
            explain.append(f"{labels[a]}/{labels[b]} — {a}:{va} {trend} {b}:{vb} → <b>{pick}</b>")
        bullets = [
            f"Ваш тип: <b>{code}</b> (укороченная версия).",
            "Это описание предпочтений, а не диагноз.",
            "Используйте тип как ориентир для стиля учёбы, коммуникации и распределения ролей в проекте.",
        ] + explain
        return {"title": f"Тип по MBTI: {code}", "bullets": bullets, "code": code}

    if slug == "holland":
        order = sorted([(k, v) for k, v in per.items() if k in "RIASEC"], key=lambda x: -x[1])
        top3 = "".join([k for k, _ in order[:3]]) or "—"
        bullets = [
            f"Рейтинг кодов RIASEC: <b>{' > '.join([f'{k}:{v}' for k, v in order]) or 'н/д'}</b>.",
            f"Ваш профиль интересов (топ-3): <b>{top3}</b>.",
            "Ориентируйтесь на направления и факультеты, где ведущие коды раскрываются лучше всего."
        ]
        return {"title": f"RIASEC-профиль: {top3}", "bullets": bullets, "code": top3}

    if slug == "klimov":
        names = {"H": "Ч-Ч", "T": "Ч-Т", "N": "Ч-Пр", "S": "Ч-Зн", "A": "Ч-ХО"}
        order = sorted([(names.get(k, k), v) for k, v in per.items() if k in names], key=lambda x: -x[1])
        lead = order[0][0] if order else "—"
        bullets = [
            f"Рейтинг типов: <b>{' > '.join([f'{k}:{v}' for k, v in order]) or 'н/д'}</b>.",
            f"Ведущий тип: <b>{lead}</b>. Подберите профиль и учебные активности под него."
        ]
        return {"title": f"Климов: ведущий тип — {lead}", "bullets": bullets, "code": lead}

    if slug == "kos2":
        comm, org = per.get("COMM", 0), per.get("ORG", 0)

        def lvl(x: int) -> str:
            return "высокий" if x >= 8 else ("средний" if x >= 4 else "низкий")

        bullets = [
            f"Коммуникативные склонности: <b>{comm}</b> — {lvl(comm)}.",
            f"Организаторские склонности: <b>{org}</b> — {lvl(org)}.",
            "Развивайте soft-skills через дебат-клубы, проектную работу и тьюторство младших."
        ]
        return {"title": "КОС-2: профиль навыков", "bullets": bullets}

    if slug in ("interests", "thinking", "child_type", "bennett"):
        ordered = sorted(per.items(), key=lambda x: -x[1]) if per else []
        top = ", ".join([f"{k}:{v}" for k, v in ordered[:3]]) or "—"
        bullets = [f"Ведущие шкалы: <b>{top}</b>."]
        if slug == "bennett" and max_score:
            pct = int(100 * raw / max_score) if max_score else 0
            cat = "высокий" if pct >= 75 else ("средний" if pct >= 40 else "низкий")
            bullets.append(f"Пространственное мышление: ≈{pct}% — <b>{cat}</b> уровень.")
        return {"title": "Итоги по шкалам", "bullets": bullets}

    if slug == "cdi":
        level = "низкая"
        if raw >= 19:
            level = "высокая"
        elif raw >= 13:
            level = "умеренная"
        return {
            "title": "CDI: сводка",
            "bullets": [
                f"Суммарный показатель: <b>{raw}</b> (уровень: {level}).",
                "Подробная интерпретация и дальнейшие шаги доступны школьному психологу."
            ],
        }

    return {"title": "Итоги теста",
            "bullets": ["Результаты сохранены. Подробная интерпретация будет доступна после настройки шкал."]}


# ================== AI-Orda (чат с сохранением в БД) ==================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

def _current_lang():
    # 1) Babel, если есть
    try:
        from flask_babel import get_locale
        return str(get_locale())[:2]
    except Exception:
        pass
    # 2) сохранённый выбор
    lang = session.get("lang")
    if lang in ("ru", "en", "kk"):
        return lang
    return "ru"

def _student_profile_text(user_id: int, lang: str) -> str:
    """
    Короткая сводка по последним попыткам пользователя — чтобы ассистент отвечал осмысленно.
    """
    attempts = (
        TestAttempt.query
        .filter_by(user_id=user_id)
        .filter(TestAttempt.finished_at.isnot(None))
        .order_by(TestAttempt.finished_at.desc())
        .limit(4)
        .all()
    )
    if not attempts:
        return ""

    lines = []
    for a in attempts:
        t = Test.query.get(a.test_id)
        if not t:
            continue
        # пересчитываем краткую сводку по тесту
        qs = TestQuestion.query.filter_by(test_id=t.id).order_by(asc(TestQuestion.order)).all()
        answers_by_qid = {x.question_id: x for x in TestAnswer.query.filter_by(attempt_id=a.id).all()}
        per, raw, max_score = _calc_scales_and_totals(qs, answers_by_qid)
        s = _interpret(t.slug, per, raw, max_score)
        code = s.get("code")
        title = t.title
        if code:
            lines.append(f"{title}: {code}")
        else:
            lines.append(f"{title}: {s.get('title')}")
    if lang == "kk":
        intro = "Соңғы нәтижелер:"
    elif lang == "en":
        intro = "Recent results:"
    else:
        intro = "Последние результаты:"
    return intro + " " + "; ".join(lines)

def _ai_system_prompt(lang: str, profile_text: str) -> str:
    if lang == "kk":
        return (
            "Сен мектеп оқушысына арналған қамқор кеңесші-психологсың. "
            "Түсінікті, қысқа және жылы жауап бер. "
            "Медициналық диагноз қойма, қауіпті нұсқаулар берме. "
            "Егер сұрақ тест нәтижелеріне қатысты болса, төмендегі мәліметтерді ескер:\n"
            f"{profile_text}\n"
            "Қажет болса, келесі қадамдарға арналған нақты ұсыныстар бер."
        )
    if lang == "en":
        return (
            "You are a caring school counselor for a student. "
            "Answer clearly, briefly, and kindly. "
            "Do not give medical diagnoses or unsafe instructions. "
            "If the question relates to test results, consider this context:\n"
            f"{profile_text}\n"
            "Offer concrete next steps when helpful."
        )
    # ru
    return (
        "Ты заботливый школьный психолог-консультант. "
        "Отвечай ясно, кратко и по-доброму. "
        "Не давай медицинских диагнозов и небезопасных инструкций. "
        "Если вопрос про результаты тестов — используй этот контекст:\n"
        f"{profile_text}\n"
        "Когда уместно — предложи понятные следующие шаги."
    )

def _ai_generate(messages: list[dict], lang: str) -> str:
    """
    Вызов OpenAI. Если ключа нет — офлайн-заглушка.
    """
    if not OPENAI_API_KEY:
        # офлайн-режим — чтобы интерфейс не ломался на dev-машинах
        if lang == "kk":
            return "Сәлем! Қазір офлайн режимдемін, бірақ сұрағыңды түсіндім. Келесі қадамды бірге ойластырайық."
        if lang == "en":
            return "Hi! I'm offline now, but I got your question. Let's think of a next step together."
        return "Привет! Сейчас я офлайн, но вопрос понял. Давай подумаем над следующими шагами."
    client = OpenAI(api_key=OPENAI_API_KEY)
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.2,
        max_tokens=600,
    )
    return resp.choices[0].message.content.strip()

def _ensure_thread(user_id: int, lang: str) -> AIThread:
    th = (
        AIThread.query
        .filter_by(user_id=user_id)
        .order_by(AIThread.updated_at.desc())
        .first()
    )
    if th:
        return th
    # создаём «первый диалог»
    title = {"ru": "Новый диалог", "en": "New chat", "kk": "Жаңа диалог"}.get(lang, "Chat")
    th = AIThread(user_id=user_id, lang=lang, title=title)
    db.session.add(th)
    db.session.commit()
    return th

def _thread_messages_for_llm(thread: AIThread, lang: str) -> list[dict]:
    profile_text = _student_profile_text(current_user.id, lang)
    system = _ai_system_prompt(lang, profile_text)
    msgs = [{"role": "system", "content": system}]
    for m in thread.messages.order_by(AIMessage.created_at.asc()).all():
        msgs.append({"role": m.role, "content": m.content})
    return msgs

@bp.get("/ai-orda")
@login_required
def ai_orda():
    lang = _current_lang()
    threads = (
        AIThread.query
        .filter_by(user_id=current_user.id)
        .order_by(AIThread.updated_at.desc())
        .all()
    )
    thread_id = request.args.get("thread_id", type=int)
    if thread_id:
        thread = AIThread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()
    else:
        thread = _ensure_thread(current_user.id, lang)

    messages = thread.messages.order_by(AIMessage.created_at.asc()).all()
    return render_template(
        "student/ai_orda.html",
        active="ai-orda",
        threads=threads,
        thread=thread,
        messages=messages,
        lang=lang,
    )

@bp.post("/ai-orda/new")
@login_required
def ai_orda_new():
    lang = _current_lang()
    title = (request.form.get("title") or "").strip()
    if not title:
        title = {"ru": "Новый диалог", "en": "New chat", "kk": "Жаңа диалог"}.get(lang, "Chat")
    th = AIThread(user_id=current_user.id, lang=lang, title=title)
    db.session.add(th); db.session.commit()
    return redirect(url_for(".ai_orda", thread_id=th.id))

@bp.post("/ai-orda/send")
@login_required
def ai_orda_send():
    lang = _current_lang()
    thread_id = request.form.get("thread_id", type=int)
    q = (request.form.get("q") or "").strip()
    thread = AIThread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()
    if not q:
        return redirect(url_for(".ai_orda", thread_id=thread.id))

    # сохраняем сообщение пользователя
    um = AIMessage(thread_id=thread.id, role="user", content=q)
    db.session.add(um); db.session.flush()

    # инференс
    msgs = _thread_messages_for_llm(thread, lang) + [{"role": "user", "content": q}]
    try:
        answer = _ai_generate(msgs, lang)
    except Exception:
        answer = {
            "ru": "Извини, сервис недоступен. Попробуй ещё раз чуть позже.",
            "en": "Sorry, the service is unavailable. Please try again later.",
            "kk": "Кешіріңіз, қызмет қолжетімсіз. Кейінірек қайталап көріңіз."
        }.get(lang, "Temporary issue.")

    am = AIMessage(thread_id=thread.id, role="assistant", content=answer)
    thread.updated_at = datetime.utcnow()  # type: ignore
    if not thread.title or thread.title.strip().lower() in ("новый диалог", "new chat", "жаңа диалог"):
        # первые 30 символов вопроса — как название
        thread.title = (q[:30] + "…") if len(q) > 30 else q

    db.session.add(am); db.session.commit()
    return redirect(url_for(".ai_orda", thread_id=thread.id))

# JSON (AJAX) — опционально, если захочешь отправлять без перезагрузки
@bp.post("/api/ai-orda/send")
@login_required
def ai_orda_send_api():
    data = request.get_json(silent=True) or {}
    lang = _current_lang()
    thread_id = data.get("thread_id")
    q = (data.get("q") or "").strip()
    if not thread_id or not q:
        return jsonify({"error": "bad_request"}), 400
    thread = AIThread.query.filter_by(id=int(thread_id), user_id=current_user.id).first_or_404()

    db.session.add(AIMessage(thread_id=thread.id, role="user", content=q)); db.session.flush()
    msgs = _thread_messages_for_llm(thread, lang) + [{"role": "user", "content": q}]
    try:
        answer = _ai_generate(msgs, lang)
    except Exception:
        answer = {
            "ru": "Временная ошибка. Попробуйте позже.",
            "en": "Temporary error. Try again later.",
            "kk": "Уақытша қате. Кейінірек қайталап көріңіз."
        }.get(lang, "Temporary error.")

    db.session.add(AIMessage(thread_id=thread.id, role="assistant", content=answer))
    thread.updated_at = datetime.utcnow()  # type: ignore
    db.session.commit()
    return jsonify({"answer": answer})
