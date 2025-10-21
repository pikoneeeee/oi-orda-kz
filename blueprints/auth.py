from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required
from urllib.parse import urlparse, urljoin
from extensions import db
from models import User, School, SchoolAdminProfile

bp = Blueprint("auth", __name__, template_folder="../templates")


def is_safe_url(target: str) -> bool:
    if not target:
        return False
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ("http", "https") and ref_url.netloc == test_url.netloc


@bp.get("/login")
def login():
    next_url = request.args.get("next", "")
    return render_template("auth/login.html", next_url=next_url)


@bp.post("/login")
def login_post():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    remember = bool(request.form.get("remember"))
    next_url = request.form.get("next") or ""

    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        flash("Неверный логин или пароль", "danger")
        return redirect(url_for("auth.login", next=next_url))

    login_user(user, remember=remember)

    # безопасный next -> иначе по роли
    if next_url and is_safe_url(next_url):
        return redirect(next_url)

    return redirect({
        "student": url_for("student.dashboard"),
        "psych":   url_for("psych.dashboard"),
        "admin":   url_for("admin.dashboard"),
    }.get(user.role, url_for("public.index")))


@bp.post("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("public.index"))


@bp.get("/register-school")
def register_school():
    return render_template("auth/register_school.html")


@bp.post("/register-school")
def register_school_post():
    school_name = (request.form.get("school_name") or "").strip()
    city = (request.form.get("city") or "").strip()
    admin_email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""

    if not school_name or not admin_email or not password:
        flash("Заполните все поля", "danger")
        return redirect(url_for("auth.register_school"))
    if User.query.filter_by(email=admin_email).first():
        flash("Такой email уже зарегистрирован", "danger")
        return redirect(url_for("auth.register_school"))

    school = School(name=school_name, city=city)
    user = User(email=admin_email, role="admin")
    user.set_password(password)
    db.session.add_all([school, user])
    db.session.flush()
    db.session.add(SchoolAdminProfile(user_id=user.id, school_id=school.id))
    db.session.commit()

    flash("Школа создана. Войдите под админом.", "success")
    return redirect(url_for("auth.login"))


# --- Забыли пароль? (заглушка) ---
@bp.get("/forgot")
def forgot():
    return render_template("auth/forgot.html")


@bp.post("/forgot")
def forgot_post():
    email = (request.form.get("email") or "").strip().lower()
    if not email:
        flash("Укажите email, чтобы восстановить доступ.", "danger")
        return redirect(url_for("auth.forgot"))
    # Здесь позже добавим отправку письма
    flash("Если такой аккаунт существует, мы отправим инструкцию на email.", "success")
    return redirect(url_for("auth.login"))
