from flask import Blueprint, render_template
from flask_login import login_required
from auth_utils import role_required

bp = Blueprint("admin", __name__, template_folder="../templates")

@bp.get("/")
@login_required
@role_required("admin")
def dashboard():
    return render_template("dash/admin.html")
