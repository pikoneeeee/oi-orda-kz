from flask import Flask, session, request, redirect, url_for, abort
from dotenv import load_dotenv
from flask_migrate import Migrate

from extensions import db, login_manager, babel
from config import Config


# ---------- CLI-команды ----------
def register_cli(app):
    @app.cli.command("seed-tests")
    @app.cli.command("seed_tests")
    def seed_tests():
        """Создаёт каталог тестов + по 3 демонстрационных вопроса (если их ещё нет)."""
        from models import Test, TestQuestion, TestOption

        data = [
            ("holland", "Опросник Холланда", "Исследуй тип личности и получи рекомендации по профессиям.", 6,
             "img/tests/holland.png", False),
            ("klimov", "ДДО (Климов)", "Тип профессий по классификации Е. А. Климова.", 5, "img/tests/klimov.png",
             False),
            ("kos2", "КОС-2", "Коммуникативные и организаторские склонности.", 5, "img/tests/kos2.png", False),
            (
            "interests", "Карта интересов", "Предпочтительные виды деятельности.", 4, "img/tests/interests.png", False),
            ("thinking", "Тип мышления", "Какой тип мышления у тебя преобладает.", 5, "img/tests/thinking.png", False),
            ("child_type", "Тип личности ребёнка", "Особенности школьника.", 5, "img/tests/child.png", False),
            ("bennett", "Тест Беннета", "Пространственное воображение и технические способности.", 6,
             "img/tests/bennett.png", False),
            ("mbti", "MBTI (укороченная версия)", "Склонности по типологии Майерс — Бриггс.", 7, "img/tests/mbti.png",
             False),
            ("cdi", "Опросник детской депрессии (CDI)", "Определение уровня депрессивных симптомов у 7–17 лет.", 7,
             "img/tests/cdi.png", True),
        ]

        created = 0
        for slug, title, short, mins, img, secret in data:
            t = Test.query.filter_by(slug=slug).first()
            if not t:
                t = Test(
                    slug=slug,
                    title=title,
                    short_desc=short,
                    long_desc=short,
                    duration_min=mins,
                    image=img,
                    confidential_student=secret,
                )
                db.session.add(t)
                db.session.flush()

                for i in range(1, 4):
                    q = TestQuestion(test_id=t.id, order=i, text=f"{title}: вопрос {i}", qtype="single")
                    db.session.add(q)
                    db.session.flush()
                    for j, txt in enumerate(["Совсем не про меня", "Похоже", "Очень про меня"], start=1):
                        db.session.add(TestOption(question_id=q.id, order=j, text=txt, value=str(j)))
                created += 1

        db.session.commit()
        print(f"Готово. Создано тестов: {created}")


def create_app():
    """Создаёт и конфигурирует Flask приложение"""

    load_dotenv()

    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.from_object(Config)

    # ---------- i18n: языки по умолчанию ----------
    app.config.setdefault("LANGUAGES", ["ru", "en", "kk"])
    app.config.setdefault("BABEL_DEFAULT_LOCALE", "ru")

    # ---------- Babel: селектор локали ----------
    def _select_locale():
        langs = app.config.get("LANGUAGES", ["ru", "en", "kk"])

        # 1) явный выбор пользователя из сессии
        lang = session.get("lang")
        if lang in langs:
            return lang

        # 2) язык из профиля пользователя (если поле есть)
        try:
            from flask_login import current_user
            if current_user.is_authenticated:
                u_lang = getattr(current_user, "locale", None)
                if u_lang in langs:
                    return u_lang
        except Exception:
            pass

        # 3) best-match из заголовков браузера
        return request.accept_languages.best_match(langs) or app.config.get("BABEL_DEFAULT_LOCALE", "ru")

    # get_locale для шаблонов
    try:
        from flask_babel import get_locale as _get_locale
    except Exception:
        def _get_locale():
            return session.get("lang") or "ru"

    # Инициализация Babel
    try:
        babel.init_app(app, locale_selector=_select_locale)
    except TypeError:
        babel.init_app(app)

    # ---------- Jinja: регистрировать функции перевода ----------
    try:
        from flask_babel import gettext as _gettext, ngettext as _ngettext
    except Exception:
        def _gettext(s: str, **kwargs):
            try:
                return s % kwargs if kwargs else s
            except Exception:
                return s

        def _ngettext(singular: str, plural: str, n: int, **kwargs):
            text = singular if int(n) == 1 else plural
            try:
                return text % kwargs if kwargs else text
            except Exception:
                return text

    app.jinja_env.globals.update(
        _=_gettext,
        gettext=_gettext,
        ngettext=_ngettext,
        get_locale=_get_locale,
    )

    # ---------- extensions ----------
    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Пожалуйста, войдите для доступа."
    login_manager.login_message_category = "warning"

    # ---------- Blueprints ----------
    from blueprints.public import bp as public_bp
    from blueprints.auth import bp as auth_bp
    from blueprints.student import bp as student_bp
    from blueprints.tests import bp as tests_bp

    # Регистрируем основные blueprints
    app.register_blueprint(public_bp)
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(student_bp, url_prefix="/student")
    app.register_blueprint(tests_bp)

    # Опциональные модули (психолог и админ)
    try:
        from blueprints.psych import bp as psych_bp
        app.register_blueprint(psych_bp, url_prefix="/psych")
    except ImportError:
        pass

    try:
        from blueprints.admin import admin_bp
        app.register_blueprint(admin_bp)
    except ImportError:
        pass

    # ---------- Переключение языка ----------
    @app.get("/set-lang/<lang>")
    def set_lang(lang: str):
        langs = app.config.get("LANGUAGES", ["ru", "en", "kk"])
        if lang not in langs:
            abort(404)
        session["lang"] = lang
        return redirect(request.referrer or url_for("public.index"))

    # ---------- Хелперы для шаблонов ----------
    @app.context_processor
    def utility_processor():
        from werkzeug.routing import BuildError
        from flask import url_for as _url_for

        def safe_url_for(endpoint, **values):
            try:
                return _url_for(endpoint, **values)
            except BuildError:
                return "#"

        def endpoint_exists(endpoint):
            try:
                _url_for(endpoint)
                return True
            except BuildError:
                return False

        lang_choices = [("ru", "Рус"), ("en", "Eng"), ("kk", "Qazaq")]

        return dict(
            safe_url_for=safe_url_for,
            endpoint_exists=endpoint_exists,
            site_name=app.config.get("SITE_NAME", "oi-orda"),
            LANG_CHOICES=lang_choices,
        )

    # ---------- Импорт моделей для Alembic ----------
    from models import (
        User, School, Subscription, Classroom,
        StudentProfile, PsychologistProfile, SchoolAdminProfile,
        Test, TestQuestion, TestOption, TestAttempt, TestAnswer,
    )

    # ---------- Миграции ----------
    Migrate(app, db)

    # ---------- CLI-команды ----------
    try:
        register_cli(app)
    except Exception:
        pass

    return app