# extensions.py
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager

db = SQLAlchemy()
login_manager = LoginManager()

# ---- Flask-Babel делаем опциональным ----
try:
    from flask_babel import Babel  # type: ignore
except Exception:
    Babel = None  # пакета нет — работаем без i18n

    class _DummyBabel:
        def init_app(self, *args, **kwargs):
            """Ничего не делаем, чтобы приложение работало без Flask-Babel."""
            return None

    babel = _DummyBabel()
else:
    babel = Babel()

__all__ = ["db", "login_manager", "babel", "Babel"]
