from flask import (
    Blueprint, render_template, current_app,
    request, redirect, url_for, flash
)
from extensions import db
from models import ContactMessage

bp = Blueprint("public", __name__)


# Глобальные переменные для шаблонов (site_name и т.п.)
@bp.app_context_processor
def inject_globals():
    return {
        "site_name": current_app.config.get("SITE_NAME", "oi-orda"),
    }


@bp.get("/")
def index():
    return render_template("public/index.html")


@bp.get("/pricing")
def pricing():
    return render_template("public/pricing.html")


# Приём формы "Контакты" (см. секцию #contacts на главной)
@bp.post("/contact")
def contact():
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    phone = (request.form.get("phone") or "").strip()
    school = (request.form.get("school") or "").strip()
    message = (request.form.get("message") or "").strip()

    if not name or not email or not message:
        flash("Пожалуйста, заполните имя, email и сообщение.", "danger")
        return redirect(url_for("public.index") + "#contacts")

    db.session.add(ContactMessage(
        name=name, email=email, phone=phone, school=school, message=message
    ))
    db.session.commit()

    flash("Спасибо! Мы свяжемся с вами в ближайшее время.", "success")
    return redirect(url_for("public.index") + "#contacts")
