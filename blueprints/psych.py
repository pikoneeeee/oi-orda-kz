# -*- coding: utf-8 -*-
import json
import re
from decimal import Decimal
from datetime import date, datetime, timedelta
from collections import defaultdict

from flask import Blueprint, render_template, request, redirect, url_for, abort, session
from flask_login import current_user, login_required
from sqlalchemy import func, desc, asc

from extensions import db
from models import (
    User, School, Classroom,
    StudentProfile, PsychologistProfile,
    Test, TestAttempt, TestAnswer, TestOption, TestQuestion
)

bp = Blueprint("psych", __name__, template_folder="../templates")

# ---- constants ----
_MBTI_LETTERS = {"E", "I", "S", "N", "T", "F", "J", "P"}

# --------- Guard: только психолог ----------
@bp.before_request
def _ensure_psych():
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login", next=request.path))
    if getattr(current_user, "role", None) not in ("psych", "psychologist"):
        return redirect(url_for("public.index"))

# --------- JSON helper (для графиков) ----------
def _json_default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    if callable(o):
        return str(o)
    raise TypeError(f"Unserializable: {type(o)}")

def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=_json_default)

# --------- helpers ----------
def _has_col(model, name):
    return hasattr(model, name)

def _psych_school_id():
    prof = PsychologistProfile.query.filter_by(user_id=current_user.id).first()
    return getattr(prof, "school_id", None)

def _classes_for_psych():
    school_id = _psych_school_id()
    if not school_id:
        return []
    return (Classroom.query
            .filter_by(school_id=school_id)
            .order_by(asc(Classroom.name))
            .all())

def _user_order_columns():
    """Безопасные колонки для сортировки пользователей."""
    cols = []
    if hasattr(User, "last_name"):
        cols.append(User.last_name)
    if hasattr(User, "first_name"):
        cols.append(User.first_name)
    elif hasattr(User, "name"):
        cols.append(User.name)
    elif hasattr(User, "email"):
        cols.append(User.email)
    else:
        cols.append(User.id)
    return cols

def _display_name(u):
    """Красивое имя с фолбэками: Фамилия Имя / name / email / #id."""
    parts = []
    for attr in ("last_name", "first_name"):
        if hasattr(u, attr) and getattr(u, attr):
            parts.append(getattr(u, attr))
    if not parts and hasattr(u, "name") and getattr(u, "name"):
        parts = [u.name]
    if not parts and hasattr(u, "email") and getattr(u, "email"):
        parts = [u.email]
    if not parts:
        parts = [f"#{getattr(u, 'id', '?')}"]
    return " ".join(parts)

@bp.app_template_filter("display_name")
def _display_name_filter(u):
    return _display_name(u)

def _students_in_class(class_id):
    q = (db.session.query(User)
         .join(StudentProfile, StudentProfile.user_id == User.id)
         .filter(StudentProfile.classroom_id == class_id))
    return q.order_by(*_user_order_columns()).all()

def _attempts_base_query():
    return (db.session.query(TestAttempt, Test, User)
            .join(Test, Test.id == TestAttempt.test_id)
            .join(User, User.id == TestAttempt.user_id))

def _finished_filter(q):
    # если есть finished_at — используем его; иначе «завершённость» = есть ответы
    fin = getattr(TestAttempt, "finished_at", None)
    if fin is not None:
        q = q.filter(fin.isnot(None))
    else:
        sub = (db.session.query(TestAnswer.attempt_id, func.count().label("acnt"))
               .group_by(TestAnswer.attempt_id)).subquery()
        q = q.join(sub, sub.c.attempt_id == TestAttempt.id).filter(sub.c.acnt > 0)
    return q

def _avg_score_expr():
    sc = getattr(TestAttempt, "score", None)
    return func.avg(sc) if sc is not None else None

def _safe_order_finished_desc():
    fin = getattr(TestAttempt, "finished_at", None)
    return desc(fin) if fin is not None else desc(TestAttempt.id)

# ---------- CDI: риск по raw и расчёт по ученикам ----------
def _cdi_risk_from_raw(raw: int | None, lang: str = "ru"):
    """
    Возвращает словарь с уровнем риска по CDI (детская депрессия):
      {'level': 'high|moderate|low', 'label': 'строка', 'color': 'danger|warning|secondary', 'reason': 'строка'}
    Пороги: raw >= 19 — высокий; 13..18 — умеренный; ниже — низкий.
    """
    if raw is None:
        return None

    if lang not in ("ru", "en", "kk"):
        lang = "ru"

    if raw >= 19:
        label = {"ru": "высокий риск", "en": "high risk", "kk": "жоғары тәуекел"}[lang]
        reason = {"ru": f"суммарный балл {raw} (≥19)",
                  "en": f"total score {raw} (≥19)",
                  "kk": f"жиынтық ұпай {raw} (≥19)"}[lang]
        return {"level": "high", "label": label, "color": "danger", "reason": reason}

    if raw >= 13:
        label = {"ru": "умеренный риск", "en": "moderate risk", "kk": "орташа тәуекел"}[lang]
        reason = {"ru": f"суммарный балл {raw} (13–18)",
                  "en": f"total score {raw} (13–18)",
                  "kk": f"жиынтық ұпай {raw} (13–18)"}[lang]
        return {"level": "moderate", "label": label, "color": "warning", "reason": reason}

    label = {"ru": "низкий риск", "en": "low risk", "kk": "төмен тәуекел"}[lang]
    reason = {"ru": f"суммарный балл {raw}", "en": f"total score {raw}", "kk": f"жиынтық ұпай {raw}"}[lang]
    return {"level": "low", "label": label, "color": "secondary", "reason": reason}


def _cdi_risks(student_ids: list[int], lang: str = "ru"):
    """
    Возвращает dict[user_id] = {level,label,color,reason, dt, attempt_id, raw}
    Берём ПОСЛЕДНЮЮ завершённую попытку CDI (slug='cdi') на ученика.
    Если теста/попытки нет — ключа не будет.
    """
    if not student_ids:
        return {}

    cdi_test = Test.query.filter_by(slug="cdi").first()
    if not cdi_test:
        return {}

    # выберем «последнюю» через подзапрос max(id) на пользователя
    sq = (db.session.query(func.max(TestAttempt.id).label("aid"), TestAttempt.user_id)
          .filter(TestAttempt.user_id.in_(student_ids), TestAttempt.test_id == cdi_test.id)
          .group_by(TestAttempt.user_id)).subquery()

    last_attempts = (db.session.query(TestAttempt)
                     .join(sq, sq.c.aid == TestAttempt.id)).all()

    out = {}
    for a in last_attempts:
        # возьмём raw: если есть колонка raw_score — используем, иначе посчитаем
        raw = getattr(a, "raw_score", None)
        if raw is None:
            ans = {x.question_id: x for x in TestAnswer.query.filter_by(attempt_id=a.id).all()}
            per, raw_calc, _max, _ = _calc_scales_and_totals(a.test_id, ans)
            raw = raw_calc

        risk = _cdi_risk_from_raw(raw, lang)
        if not risk:
            continue

        dt = None
        for fld in ("finished_at", "started_at", "created_at"):
            if hasattr(a, fld) and getattr(a, fld):
                dt = getattr(a, fld)
                break

        risk.update({"dt": dt, "attempt_id": a.id, "raw": raw})
        out[a.user_id] = risk

    return out


# ============ 1) Общешкольный дашборд психолога ============
@bp.get("/dashboard")
@login_required
def dashboard():
    classes = _classes_for_psych()
    class_ids = [c.id for c in classes]
    if not class_ids:
        return render_template(
            "psych/dashboard.html",
            classes=[],
            totals={"classes": 0, "students": 0, "attempts_30d": 0, "tests_total": Test.query.count()},
            tests_agg=[],
            tests_chart_json=_dumps({"labels": [], "values": []}),
            recent_attempts=[],
        )

    # все ученики школы (subquery с id)
    students_sq = (db.session.query(User.id)
                   .join(StudentProfile, StudentProfile.user_id == User.id)
                   .filter(StudentProfile.classroom_id.in_(class_ids))
                   ).subquery()

    # попытки за последние 30 дней (и только завершённые)
    since = datetime.utcnow() - timedelta(days=30)
    base_30d = db.session.query(TestAttempt).filter(
        TestAttempt.user_id.in_(db.session.query(students_sq.c.id))
    )
    if hasattr(TestAttempt, "started_at"):
        base_30d = base_30d.filter(TestAttempt.started_at >= since)
    elif hasattr(TestAttempt, "created_at"):
        base_30d = base_30d.filter(TestAttempt.created_at >= since)
    base_30d = _finished_filter(base_30d)

    # агрегаты по тестам за 30 дней
    selects = [Test.title, Test.slug, func.count(TestAttempt.id).label("cnt")]
    avg_sc = _avg_score_expr()
    if avg_sc is not None:
        selects.append(avg_sc.label("avg"))

    tests_agg = (base_30d
                 .join(Test, Test.id == TestAttempt.test_id)
                 .with_entities(*selects)
                 .group_by(Test.id, Test.title, Test.slug)
                 .order_by(desc("cnt"))
                 ).all()

    tests_chart = {
        "labels": [t.title for t in tests_agg][:10],
        "values": [int(getattr(t, "cnt", 0)) for t in tests_agg][:10],
    }

    totals = {
        "classes": len(classes),
        "students": db.session.query(User.id)
                              .filter(User.id.in_(db.session.query(students_sq.c.id))).count(),
        "attempts_30d": base_30d.count(),
        "tests_total": Test.query.count(),
    }

    # последние 12 попыток (за всё время)
    recent = (_attempts_base_query()
              .filter(TestAttempt.user_id.in_(db.session.query(students_sq.c.id)))
              .order_by(_safe_order_finished_desc())
              .limit(12)
              .all())

    return render_template(
        "psych/dashboard.html",
        classes=classes,
        totals=totals,
        tests_agg=tests_agg,
        tests_chart_json=_dumps(tests_chart),
        recent_attempts=recent,
    )

# ============ 2) Дашборд класса ============
@bp.get("/class/<int:class_id>")
@login_required
def class_dashboard(class_id):
    # доступ только к своим классам
    if class_id not in [c.id for c in _classes_for_psych()]:
        abort(403)

    classroom = Classroom.query.get_or_404(class_id)
    students = _students_in_class(class_id)
    student_ids = [s.id for s in students] or [-1]

    # агрегаты по тестам (всё время)
    selects = [Test.id, Test.title, Test.slug, func.count(TestAttempt.id).label("cnt")]
    avg_sc = _avg_score_expr()
    if avg_sc is not None:
        selects.append(avg_sc.label("avg"))

    tests_agg = (db.session.query(TestAttempt)
                 .filter(TestAttempt.user_id.in_(student_ids))
                 .join(Test, Test.id == TestAttempt.test_id)
                 .with_entities(*selects)
                 .group_by(Test.id, Test.title, Test.slug)
                 .order_by(desc("cnt"))
                 ).all()

    # MBTI: распределение по 8 буквам
    mbti_letters = "E I S N T F J P".split()
    mbti_counts = defaultdict(int)

    mbti_test = Test.query.filter_by(slug="mbti").first()
    if mbti_test:
        rows = (db.session.query(TestOption.value)
                .join(TestAnswer, TestAnswer.option_id == TestOption.id)
                .join(TestAttempt, TestAttempt.id == TestAnswer.attempt_id)
                .filter(TestAttempt.user_id.in_(student_ids))
                .filter(TestAttempt.test_id == mbti_test.id)
                .all())
        for (v,) in rows:
            vv = (v or "").strip().upper()
            if vv in mbti_letters:
                mbti_counts[vv] += 1

    mbti_chart = {
        "labels": mbti_letters,
        "values": [int(mbti_counts[k]) for k in mbti_letters],
    }

    # CDI: риски по ученикам + счётчики
    lang = _current_lang()
    cdi_risks = _cdi_risks(student_ids, lang)
    cdi_counts = {"high": 0, "moderate": 0, "low": 0, "none": 0}
    for s in students:
        r = cdi_risks.get(s.id)
        if not r:
            cdi_counts["none"] += 1
        else:
            cdi_counts[r["level"]] = cdi_counts.get(r["level"], 0) + 1

    # последние попытки класса
    recent = (_attempts_base_query()
              .filter(TestAttempt.user_id.in_(student_ids))
              .order_by(_safe_order_finished_desc())
              .limit(20)
              .all())

    return render_template(
        "psych/class_dashboard.html",
        classroom=classroom,
        students=students,
        tests_agg=tests_agg,
        mbti_chart_json=_dumps(mbti_chart),
        recent_attempts=recent,
        # >>> добавлено для CDI
        cdi_risks=cdi_risks,
        cdi_counts=cdi_counts,
    )

# ============ 3) Дашборд ученика ============
@bp.get("/student/<int:user_id>")
@login_required
def student_dashboard(user_id):
    # доступ только к ученику из «своей» школы
    class_ids = [c.id for c in _classes_for_psych()]
    ok = (db.session.query(StudentProfile)
          .filter(StudentProfile.user_id == user_id,
                  StudentProfile.classroom_id.in_(class_ids))
          .first())
    if not ok:
        abort(403)

    student = User.query.get_or_404(user_id)

    attempts = (_attempts_base_query()
                .filter(TestAttempt.user_id == user_id)
                .order_by(_safe_order_finished_desc())
                .all())

    # средняя успеваемость по тестам ученика (если есть score)
    avg_sc = _avg_score_expr()
    perf_chart = {"labels": [], "values": []}
    if avg_sc is not None:
        rows = (db.session.query(Test.title, func.avg(getattr(TestAttempt, "score")).label("avg"))
                .select_from(TestAttempt)
                .join(Test, Test.id == TestAttempt.test_id)
                .filter(TestAttempt.user_id == user_id)
                .group_by(Test.id, Test.title)
                .order_by(desc("avg"))).all()
        perf_chart = {
            "labels": [r.title for r in rows][:8],
            "values": [round(float(r.avg or 0), 1) for r in rows][:8]
        }

    # CDI: риск по ученику (для алерта вверху)
    lang = _current_lang()
    cdi_map = _cdi_risks([user_id], lang)
    cdi = cdi_map.get(user_id)

    return render_template(
        "psych/student_dashboard.html",
        student=student,
        attempts=attempts,
        perf_chart_json=_dumps(perf_chart),
        # >>> добавлено для CDI
        cdi=cdi,
    )

# ---------- УТИЛИТЫ АНАЛИЗА ----------
def _current_lang():
    """Берём из сессии или query (?lang=ru|en|kk), дефолт — ru"""
    return (request.args.get("lang")
            or session.get("lang")
            or "ru")

def _parse_value_to_scales(val):
    """Парсит значение опции вопроса в словарь шкал: {'TOTAL': 2, 'RIA':1, ...}."""
    if val is None:
        return {}
    s = str(val).strip()
    if not s:
        return {}
    u = s.upper()
    if u in _MBTI_LETTERS:
        return {u: 1}
    if re.fullmatch(r"-?\d+", s):
        return {"TOTAL": int(s)}
    res = {}
    for token in re.split(r"[;,]", s):
        token = token.strip()
        if not token:
            continue
        m = re.match(r"([A-Za-zА-Яа-я_]+)\s*=?\s*([+-]?\d+)", token)
        if m:
            code = m.group(1).upper()
            pts = int(m.group(2))
            res[code] = res.get(code, 0) + pts
    return res

def _calc_scales_and_totals(test_id: int, answers_by_qid: dict[int, TestAnswer]):
    """Суммы по шкалам + сырые баллы для числовых тестов."""
    per, raw, max_score = {}, 0, 0
    qs = (TestQuestion.query
          .filter_by(test_id=test_id)
          .order_by(asc(TestQuestion.order))
          .all())
    for q in qs:
        opts = (TestOption.query
                .filter_by(question_id=q.id)
                .order_by(asc(TestOption.order)).all())
        # максимум TOTAL по вопросу
        max_q = 0
        for o in opts:
            parsed = _parse_value_to_scales(o.value)
            max_q = max(max_q, parsed.get("TOTAL", 0))
        max_score += max_q

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
    return per, raw, max_score, qs

def _interpretation(slug: str, lang: str, per: dict, raw: int, max_score: int):
    """Возвращает {title, bullets, code?} — короткую выжимку по тесту на 3 языках."""
    slug = (slug or "").lower()
    t = {
        "ru": {"your_type": "Ваш тип", "tips": "Советы", "saved": "Результаты сохранены."},
        "en": {"your_type": "Your type", "tips": "Tips", "saved": "Results saved."},
        "kk": {"your_type": "Сіздің типіңіз", "tips": "Кеңестер", "saved": "Нәтиже сақталды."},
    }[lang]

    # ---- MBTI ----
    if slug == "mbti":
        labels = {
            "ru": dict(E="Экстраверсия", I="Интроверсия", S="Ощущение", N="Интуиция",
                       T="Мышление", F="Чувство", J="Суждение", P="Восприятие"),
            "en": dict(E="Extraversion", I="Introversion", S="Sensing", N="Intuition",
                       T="Thinking", F="Feeling", J="Judging", P="Perceiving"),
            "kk": dict(E="Экстраверсия", I="Интроверсия", S="Сезіну", N="Интуиция",
                       T="Ойлау", F="Сезім", J="Бағалау", P="Қабылдау"),
        }[lang]
        pairs = [("E", "I"), ("S", "N"), ("T", "F"), ("J", "P")]
        code = ""
        bullets = []
        for a, b in pairs:
            va, vb = per.get(a, 0), per.get(b, 0)
            pick = a if va >= vb else b
            code += pick
            bullets.append(f"{labels[a]}/{labels[b]} — {a}:{va} {'≈' if va==vb else '>'} {b}:{vb} → <b>{pick}</b>")
        bullets[:0] = [
            f"{t['your_type']}: <b>{code}</b>.",
            "Это описание предпочтений, а не диагноз." if lang == "ru" else
            ("Бұл бағалау емес, қалаулардың сипаттамасы." if lang == "kk"
             else "This is a description of preferences, not a diagnosis."),
        ]
        return {"title": f"MBTI • {code}", "bullets": bullets, "code": code}

    # ---- Holland (RIASEC) ----
    if slug == "holland":
        order = sorted([(k, v) for k, v in per.items() if k in set("RIASEC")], key=lambda x: -x[1])
        top3 = "".join([k for k, _ in order[:3]]) or "—"
        bullets = [
            ("Топ-3 коды интересов: " if lang == "ru" else
             ("Үздік 3 код: " if lang == "kk" else "Top-3 codes: ")) + f"<b>{top3}</b>",
            ("Подбирайте профили и практики под ведущие коды."
             if lang == "ru" else
             ("Жетекші кодтарға сай пәндер таңдау." if lang == "kk"
              else "Align studies with the leading codes."))
        ]
        return {"title": f"RIASEC • {top3}", "bullets": bullets, "code": top3}

    # ---- КОС-2 ----
    if slug == "kos2":
        comm, org = per.get("COMM", 0), per.get("ORG", 0)
        def lvl(x):
            if x >= 8:  return ("высокий", "high", "жоғары")[["ru", "en", "kk"].index(lang)]
            if x >= 4:  return ("средний", "medium", "орта")[["ru", "en", "kk"].index(lang)]
            return ("низкий", "low", "төмен")[["ru", "en", "kk"].index(lang)]
        bullets = [
            (f"Коммуникативные: <b>{comm}</b> — {lvl(comm)}."
             if lang == "ru" else
             f"Communication: <b>{comm}</b> — {lvl(comm)}." if lang == "en"
             else f"Коммуникативтік: <b>{comm}</b> — {lvl(comm)}."),
            (f"Организаторские: <b>{org}</b> — {lvl(org)}."
             if lang == "ru" else
             f"Leadership/organization: <b>{org}</b> — {lvl(org)}." if lang == "en"
             else f"Ұйымдастыру: <b>{org}</b> — {lvl(org)}."),
        ]
        return {"title": "КОС-2", "bullets": bullets}

    # ---- CDI / Interests / Thinking / Bennett etc. ----
    if slug in {"cdi", "interests", "thinking", "child_type", "bennett"}:
        bullets = []
        if slug == "bennett" and max_score:
            pct = int(100 * raw / max_score)
            cat = ("высокий" if pct >= 75 else "средний" if pct >= 40 else "низкий") if lang == "ru" else \
                  ("high" if pct >= 75 else "medium" if pct >= 40 else "low") if lang == "en" else \
                  ("жоғары" if pct >= 75 else "орта" if pct >= 40 else "төмен")
            bullets.append((f"Пространственное мышление: ≈{pct}% — <b>{cat}</b>."
                            if lang == "ru" else
                            f"Spatial ability: ≈{pct}% — <b>{cat}</b>." if lang == "en"
                            else f"Кеңістіктік ойлау: ≈{pct}% — <b>{cat}</b>."))

        if not bullets:
            bullets = [t["saved"]]
        return {"title": "Итоги", "bullets": bullets}

    return {"title": "Итоги", "bullets": [t["saved"]]}

# ---------- Полноценный отчёт попытки для психолога ----------
@bp.get("/attempt/<int:attempt_id>", endpoint="result_view")
@login_required
def psych_attempt_view(attempt_id):
    # доступ только к ученикам своей школы
    class_ids = [c.id for c in _classes_for_psych()]
    a = TestAttempt.query.get_or_404(attempt_id)
    stud_prof = StudentProfile.query.filter_by(user_id=a.user_id).first()
    if not stud_prof or stud_prof.classroom_id not in class_ids:
        abort(403)

    test   = Test.query.get_or_404(a.test_id)
    user   = User.query.get_or_404(a.user_id)
    answers = {x.question_id: x for x in TestAnswer.query.filter_by(attempt_id=a.id).all()}

    per, raw, max_score, questions = _calc_scales_and_totals(test.id, answers)
    lang = _current_lang()
    summary = _interpretation(test.slug, lang, per, raw, max_score)

    rows = []
    for q in questions:
        opts = (TestOption.query
                .filter_by(question_id=q.id)
                .order_by(asc(TestOption.order)).all())
        chosen = None
        ans = answers.get(q.id)
        if ans:
            chosen = next((o for o in opts if o.id == ans.option_id), None)
        rows.append({"q": q, "opts": opts, "chosen": chosen})

    score_pct = None
    if max_score:
        score_pct = int(100 * raw / max_score)

    return render_template(
        "psych/result_view.html",
        attempt=a,
        test=test,
        user=user,
        rows=rows,
        per=per,
        raw=raw,
        max_score=max_score,
        score_pct=score_pct,
        summary=summary,
        lang=lang
    )
