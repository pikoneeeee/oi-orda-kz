# services/analysis.py
from __future__ import annotations
from typing import Dict, Tuple, List
from sqlalchemy import asc
from extensions import db
from models import Test, TestQuestion, TestOption, TestAttempt, TestAnswer

# --- парсинг значения опции в шкалы ---
def _parse_value_to_scales(val: str) -> Dict[str, int]:
    if val is None:
        return {}
    s = str(val).strip()
    if not s:
        return {}
    # MBTI одиночная буква
    up = s.upper()
    if up in {"E","I","S","N","T","F","J","P"}:
        return {up: 1}
    # простое число
    if s.lstrip("-").isdigit():
        return {"TOTAL": int(s)}
    # составные CODE=±N через ; или ,
    result: Dict[str, int] = {}
    for token in [t.strip() for t in s.replace(",", ";").split(";")]:
        if not token:
            continue
        if "=" in token:
            code, num = token.split("=", 1)
            code = code.strip().upper()
            try:
                pts = int(num.strip())
            except Exception:
                continue
            result[code] = result.get(code, 0) + pts
    return result

# --- расчёт «сырых» чисел и сумм по шкалам (универсально) ---
def _calc_scales_and_totals(test_id: int, attempt_id: int) -> Tuple[Dict[str,int], int, int]:
    per: Dict[str, int] = {}
    raw = 0
    max_score = 0

    qs = (TestQuestion.query
          .filter_by(test_id=test_id)
          .order_by(asc(TestQuestion.order)).all())
    ans_by_q = {a.question_id: a for a in TestAnswer.query.filter_by(attempt_id=attempt_id).all()}

    for q in qs:
        opts = TestOption.query.filter_by(question_id=q.id).order_by(asc(TestOption.order)).all()
        # максимум TOTAL по вопросу (для %)
        max_q_total = 0
        for o in opts:
            parsed = _parse_value_to_scales(o.value)
            max_q_total = max(max_q_total, parsed.get("TOTAL", 0))
        max_score += max_q_total

        ans = ans_by_q.get(q.id)
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

# --- MBTI специальный подсчёт кода ---
_MBTI_PAIRS = [("E","I"), ("S","N"), ("T","F"), ("J","P")]
def _mbti_code_from_per(per: Dict[str,int]) -> Tuple[str, List[str]]:
    labels = {
        "E":"Экстраверсия","I":"Интроверсия",
        "S":"Сенсорика","N":"Интуиция",
        "T":"Мышление","F":"Чувство",
        "J":"Суждение","P":"Восприятие",
    }
    code = ""
    commentary = []
    for a,b in _MBTI_PAIRS:
        va, vb = per.get(a,0), per.get(b,0)
        pick = a if va >= vb else b
        code += pick
        trend = "≈" if va == vb else ">"
        commentary.append(f"{labels[a]}/{labels[b]} — {a}:{va} {trend} {b}:{vb} → {pick}")
    return code, commentary

# --- человекочитаемые выжимки по тестам ---
def _humanize(slug: str, per: Dict[str,int], raw: int, max_score: int) -> str:
    s = slug.lower()

    if s == "mbti":
        code, lines = _mbti_code_from_per(per)
        return ("MBTI: ваш тип — {code}. "
                "Это описание предпочтений, не диагноз. "
                "Баланс по дихотомиям: {pairs}.").format(
                    code=code,
                    pairs="; ".join(lines)
                )

    if s == "klimov":
        names = {"H":"Человек-человек","T":"Человек-техника","N":"Человек-природа","S":"Человек-знаковая система","A":"Человек-художественный образ"}
        ordered = sorted([(k, v) for k, v in per.items() if k in names], key=lambda kv: -kv[1])
        lead = names.get(ordered[0][0], "—") if ordered else "—"
        top = " > ".join([f"{names.get(k,k)}:{v}" for k,v in ordered]) or "н/д"
        return f"Климов: ведущий тип — {lead}. Рейтинг: {top}."

    if s == "holland":
        ordered = sorted([(k, v) for k, v in per.items() if k in "RIASEC"], key=lambda kv: -kv[1])
        top3 = "".join([k for k,_ in ordered[:3]]) or "—"
        return f"RIASEC (Холланд): топ-3 профиль — {top3}. Рейтинг: " + \
               (" > ".join([f"{k}:{v}" for k,v in ordered]) or "н/д")

    if s == "kos2":
        comm, org = per.get("COMM",0), per.get("ORG",0)
        def lvl(x: int) -> str:
            return "высокий" if x >= 8 else ("средний" if x >= 4 else "низкий")
        return f"КОС-2: коммуникативные {comm} ({lvl(comm)}), организаторские {org} ({lvl(org)})."

    if s == "bennett":
        pct = int(100*raw/max_score) if max_score else 0
        band = "высокий" if pct>=75 else ("средний" if pct>=40 else "низкий")
        return f"Беннет: пространственное мышление ≈{pct}% — {band}."

    if s in ("interests","thinking","child_type"):
        ordered = sorted(per.items(), key=lambda kv: -kv[1]) if per else []
        top = ", ".join([f"{k}:{v}" for k,v in ordered[:3]]) or "—"
        title = {"interests":"Карта интересов","thinking":"Тип мышления","child_type":"Тип личности ребёнка"}[s]
        return f"{title}: ведущие шкалы — {top}."

    if s == "cdi":
        # конфиденциальный — здесь не детализируем
        return "CDI (детская шкала): результаты доступны только школьному психологу."

    return "Итоги сохранены."

def build_user_results_summary(user_id: int, limit: int = 6) -> str:
    """
    Возвращает компактный текст со сводкой последних попыток пользователя,
    безопасный для передачи в LLM. Конфиденциальные тесты не раскрываются.
    """
    # последние завершённые попытки
    atts = (TestAttempt.query
            .filter_by(user_id=user_id)
            .filter(TestAttempt.finished_at.isnot(None))
            .order_by(TestAttempt.finished_at.desc(), TestAttempt.id.desc())
            .limit(limit)
            .all())

    if not atts:
        return "У ученика пока нет завершённых тестов."

    lines: List[str] = []
    for a in atts:
        t: Test | None = Test.query.get(a.test_id)
        if not t:
            continue

        if getattr(t, "confidential_student", False):
            # не раскрываем детали
            lines.append(f"• {t.title}: конфиденциальная сводка без подробностей.")
            continue

        per, raw, max_score = _calc_scales_and_totals(t.id, a.id)
        lines.append(f"• {t.title}: " + _humanize(t.slug, per, raw, max_score))

    return "Краткая сводка результатов ученика:\n" + "\n".join(lines)
