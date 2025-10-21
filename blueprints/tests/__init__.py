# blueprints/tests/__init__.py
from datetime import datetime, timedelta, timezone
from typing import Dict, Tuple

from flask import Blueprint, render_template, request, redirect, url_for, abort, flash
from flask_login import current_user
from sqlalchemy import func

from extensions import db
from utils.auth import role_required
from models import Test, TestQuestion, TestOption, TestAttempt, TestAnswer

bp = Blueprint("tests", __name__, url_prefix="/student/tests")


# ---------------- helpers ----------------
def _get_test_or_404(slug: str) -> Test:
    return Test.query.filter_by(slug=slug).first_or_404()


def _get_attempt_start_dt(attempt) -> datetime | None:
    """Берём время старта из любого доступного поля модели."""
    return (
        getattr(attempt, "started_at", None)
        or getattr(attempt, "created_at", None)
        or getattr(attempt, "created", None)
    )


def _set_attempt_start_dt_if_possible(attempt, dt: datetime | None = None) -> None:
    """Ставит время старта в первое подходящее поле (если оно есть в модели)."""
    dt = dt or datetime.utcnow()
    for field in ("started_at", "created_at", "created"):
        if hasattr(attempt, field):
            setattr(attempt, field, dt)
            return


def _get_or_create_attempt(test: Test) -> TestAttempt:
    """Ищем незавершённую попытку, иначе создаём новую (надёжно по полям)."""
    fin_col = getattr(TestAttempt, "finished_at", None)

    q = TestAttempt.query.filter_by(user_id=current_user.id, test_id=test.id)
    if fin_col is not None:
        q = q.filter(fin_col.is_(None))

    start_col = getattr(TestAttempt, "started_at", None) or getattr(TestAttempt, "created_at", None)
    if start_col is not None:
        q = q.order_by(start_col.desc())
    else:
        q = q.order_by(TestAttempt.id.desc())

    attempt = q.first()
    if attempt:
        return attempt

    attempt = TestAttempt(user_id=current_user.id, test_id=test.id)
    db.session.add(attempt)
    db.session.flush()
    _set_attempt_start_dt_if_possible(attempt, datetime.now(timezone.utc))
    db.session.commit()
    return attempt


def _get_question(test: Test, order: int) -> TestQuestion:
    q = TestQuestion.query.filter_by(test_id=test.id, order=order).first()
    if not q:
        abort(404)
    return q


def _save_answer(attempt_id: int, question_id: int, option: TestOption | None) -> None:
    """
    Сохраняем выбранную опцию.
    Букву MBTI в TestAnswer НЕ пишем (в модели нет поля) — буква берётся из TestOption.value.
    """
    ans = TestAnswer.query.filter_by(attempt_id=attempt_id, question_id=question_id).first()
    if not ans:
        ans = TestAnswer(attempt_id=attempt_id, question_id=question_id)
        db.session.add(ans)
    ans.option_id = option.id if option else None
    db.session.commit()


# ---------------- MBTI подсчёт ----------------
MBTI_PAIRS = [("E", "I"), ("S", "N"), ("T", "F"), ("J", "P")]


def _compute_mbti(test: Test, attempt: TestAttempt) -> Tuple[Dict[str, int], Dict[str, int], Dict[str, int], str]:
    """
    Возвращает:
      counts — фактические ответы по буквам,
      totals — теоретическая представленность букв в вопросах,
      pct    — проценты в каждой паре,
      type_code — финальный тип (например, INTJ).
    """
    # 1) Фактические ответы: берём букву из TestOption.value
    rows = (
        db.session.query(TestOption.value)
        .join(TestAnswer, TestAnswer.option_id == TestOption.id)
        .filter(TestAnswer.attempt_id == attempt.id)
        .all()
    )
    counts: Dict[str, int] = {k: 0 for k in "E I S N T F J P".split()}
    for (letter,) in rows:
        v = (letter or "").strip().upper()
        if v in counts:
            counts[v] += 1

    # 2) Теоретические total'ы по буквам (на случай неполных ответов)
    totals: Dict[str, int] = {k: 0 for k in "E I S N T F J P".split()}
    all_qs = TestQuestion.query.filter_by(test_id=test.id).all()
    for q in all_qs:
        vals = [(o.value or "").strip().upper() for o in TestOption.query.filter_by(question_id=q.id).all()]
        for a, b in MBTI_PAIRS:
            if a in vals or b in vals:
                if a in vals:
                    totals[a] += 1
                if b in vals:
                    totals[b] += 1
                break

    # 3) Проценты в парах
    pct: Dict[str, int] = {}
    for a, b in MBTI_PAIRS:
        pair_total = counts[a] + counts[b]
        if pair_total == 0:
            pct[a] = pct[b] = 0
        else:
            pct[a] = round(100 * counts[a] / pair_total)
            pct[b] = 100 - pct[a]

    # 4) Тип
    pick = lambda a, b: a if counts.get(a, 0) >= counts.get(b, 0) else b
    type_code = pick("E", "I") + pick("S", "N") + pick("T", "F") + pick("J", "P")
    return counts, totals, pct, type_code


# ---------------- каталоги/лендинги ----------------
@bp.get("/")
@role_required("student")
def index():
    tests = Test.query.order_by(Test.title.asc()).all()
    return render_template("student/tests.html", tests=tests)


@bp.get("/<slug>")
@role_required("student")
def detail(slug):
    test = _get_test_or_404(slug)
    return render_template("student/test_detail.html", test=test)


# ---------------- запуск/прохождение/ответы ----------------
@bp.get("/<slug>/run")
@role_required("student")
def run(slug):
    test = _get_test_or_404(slug)
    attempt = _get_or_create_attempt(test)
    return redirect(url_for("tests.take", slug=slug, order=1, attempt_id=attempt.id))


@bp.post("/<slug>/start")
@role_required("student")
def start(slug):
    test = _get_test_or_404(slug)
    attempt = _get_or_create_attempt(test)
    return redirect(url_for("tests.take", slug=slug, order=1, attempt_id=attempt.id))


@bp.get("/<slug>/take")
@role_required("student")
def take(slug):
    test = _get_test_or_404(slug)
    attempt_id = request.args.get("attempt_id", type=int)
    order = request.args.get("order", 1, type=int)

    attempt = TestAttempt.query.get(attempt_id) if attempt_id else _get_or_create_attempt(test)
    fin_col = getattr(TestAttempt, "finished_at", None)

    if (not attempt) or attempt.user_id != current_user.id or (fin_col is not None and getattr(attempt, "finished_at")):
        flash("Нельзя продолжить эту попытку.", "warning")
        return redirect(url_for("tests.detail", slug=slug))

    q = _get_question(test, order)
    options = TestOption.query.filter_by(question_id=q.id).order_by(TestOption.order).all()
    total = TestQuestion.query.filter_by(test_id=test.id).count()

    # ранее в шаблоне могли использоваться имена q/chosen_id — пробрасываем обе пары имён
    prev = TestAnswer.query.filter_by(attempt_id=attempt.id, question_id=q.id).first()
    chosen_id = prev.option_id if prev else None

    # прогресс + таймер (если задано duration_min)
    answered_cnt = TestAnswer.query.filter_by(attempt_id=attempt.id).count()
    progress = int(100 * answered_cnt / total) if total else 0

    ends_at = None
    if getattr(test, "duration_min", 0):
        start_dt = _get_attempt_start_dt(attempt) or datetime.utcnow()
        ends_at = start_dt + timedelta(minutes=test.duration_min)

    return render_template(
        "student/take_question.html",
        test=test,
        attempt=attempt,
        # совместимость с разными шаблонами:
        q=q,
        question=q,
        options=options,
        order=order,
        total=total,
        chosen_id=chosen_id,
        selected_option_id=chosen_id,
        progress=progress,
        ends_at=ends_at,
    )


@bp.post("/<slug>/answer")
@role_required("student")
def answer(slug):
    test = _get_test_or_404(slug)
    attempt_id = request.form.get("attempt_id", type=int)
    question_id = request.form.get("question_id", type=int)
    option_id = request.form.get("option_id", type=int)
    nav = (request.form.get("nav") or "next").lower()
    order = request.form.get("order", type=int) or 1

    attempt = TestAttempt.query.get_or_404(attempt_id)
    if attempt.user_id != current_user.id or getattr(attempt, "finished_at", None):
        flash("Попытка недоступна.", "warning")
        return redirect(url_for("tests.detail", slug=slug))

    opt = TestOption.query.get(option_id) if option_id else None
    _save_answer(attempt.id, question_id, opt)

    total = TestQuestion.query.filter_by(test_id=test.id).count()
    if nav == "prev":
        return redirect(url_for("tests.take", slug=slug, order=max(1, order - 1), attempt_id=attempt.id))
    elif nav == "finish" or order >= total:
        return redirect(url_for("tests.finish", slug=slug, attempt_id=attempt.id))
    else:
        return redirect(url_for("tests.take", slug=slug, order=min(total, order + 1), attempt_id=attempt.id))


@bp.get("/<slug>/finish")
@role_required("student")
def finish(slug):
    test = _get_test_or_404(slug)
    attempt_id = request.args.get("attempt_id", type=int)
    attempt = TestAttempt.query.get_or_404(attempt_id)
    if attempt.user_id != current_user.id:
        abort(403)

    # MBTI — считаем и показываем спец. шаблон
    if test.slug == "mbti":
        counts, totals, pct, type_code = _compute_mbti(test, attempt)

        # Добавим простой «процент заполнения» как score
        total_q = TestQuestion.query.filter_by(test_id=test.id).count()
        answered = TestAnswer.query.filter_by(attempt_id=attempt.id).count()
        score = round(100 * answered / total_q) if total_q else 0

        if not getattr(attempt, "finished_at", None):
            setattr(attempt, "finished_at", datetime.now(timezone.utc))
        if hasattr(attempt, "score"):
            attempt.score = score
        db.session.commit()

        return render_template(
            "student/test_result_mbti.html",
            test=test,
            attempt=attempt,
            counts=counts,
            totals=totals,
            pct=pct,
            type_code=type_code,
        )

    # Остальные тесты — помечаем завершённой и отправляем на общий экран результата попытки
    if not getattr(attempt, "finished_at", None):
        setattr(attempt, "finished_at", datetime.now(timezone.utc))

    # На всякий случай — примитивный score как % отвеченных
    if hasattr(attempt, "score"):
        total_q = TestQuestion.query.filter_by(test_id=test.id).count()
        answered = TestAnswer.query.filter_by(attempt_id=attempt.id).count()
        attempt.score = round(100 * answered / total_q) if total_q else 0

    db.session.commit()
    return redirect(url_for("student.attempt_result", attempt_id=attempt.id))
