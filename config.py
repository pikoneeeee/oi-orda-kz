# config.py
import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(DB_DIR, exist_ok=True)


def _db_uri_from_env() -> str:
    uri = os.getenv("DATABASE_URL") or f"sqlite:///{os.path.join(DB_DIR, 'app.db')}"
    # Heroku-style: postgres:// -> postgresql://
    if uri.startswith("postgres://"):
        uri = uri.replace("postgres://", "postgresql://", 1)
    return uri


class Config:
    # ---- Flask / Core ----
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")
    SITE_NAME = os.getenv("SITE_NAME", "oi-orda")

    # ---- SQLAlchemy ----
    SQLALCHEMY_DATABASE_URI = _db_uri_from_env()
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # ---- Templates / Static cache ----
    TEMPLATES_AUTO_RELOAD = os.getenv("TEMPLATES_AUTO_RELOAD", "True").lower() == "true"
    SEND_FILE_MAX_AGE_DEFAULT = int(os.getenv("SEND_FILE_MAX_AGE_DEFAULT", "0"))

    # ---- i18n (Flask-Babel) ----
    # Список языков можно переопределить переменной LANGUAGES="ru,en,kk"
    LANGUAGES = (os.getenv("LANGUAGES", "ru,en,kk")).split(",")
    BABEL_DEFAULT_LOCALE = os.getenv("BABEL_DEFAULT_LOCALE", "ru")
    # Можно указать абсолютный путь или оставить дефолт в каталоге проекта
    BABEL_TRANSLATION_DIRECTORIES = os.getenv(
        "BABEL_TRANSLATION_DIRECTORIES",
        os.path.join(BASE_DIR, "translations"),
    )

    # ---- AI-Orda / OpenAI ----
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")  # например: gpt-4o, gpt-4o-mini
    AI_ORDA_ENABLED = os.getenv("AI_ORDA_ENABLED", "True").lower() == "true"
    AI_ORDA_SYSTEM_PROMPT = os.getenv(
        "AI_ORDA_SYSTEM_PROMPT",
        "Ты — школьный психолог-помощник. Отвечай понятно и доброжелательно.",
    )
    AI_ORDA_MAX_TOKENS = int(os.getenv("AI_ORDA_MAX_TOKENS", "800"))

    # ---- Admin Panel / File Upload ----
    # Папка для загружаемых файлов (Excel, отчёты и т.д.)
    UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)

    # Максимальный размер загружаемого файла (50 MB)
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", "50")) * 1024 * 1024

    # Допустимые расширения для загрузки
    ALLOWED_EXTENSIONS = {"xlsx", "xls", "csv"}