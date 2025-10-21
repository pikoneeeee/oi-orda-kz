from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from sqlalchemy import CheckConstraint, UniqueConstraint
from extensions import db, login_manager


# ---------- helpers ----------

def default_sub_end():
    """Дата окончания годовой подписки по умолчанию."""
    return datetime.utcnow() + timedelta(days=365)


class TimestampMixin:
    created_at = db.Column(db.DateTime, default=db.func.now(), nullable=False)
    updated_at = db.Column(
        db.DateTime, default=db.func.now(), onupdate=db.func.now(), nullable=False
    )


# ---------- школы / классы / подписки ----------

class School(TimestampMixin, db.Model):
    __tablename__ = "school"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    city = db.Column(db.String(64))

    # каскад: при удалении школы удаляются классы и подписки
    classes = db.relationship(
        "Classroom",
        backref="school",
        lazy=True,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    subscriptions = db.relationship(
        "Subscription",
        backref="school",
        lazy=True,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self):
        return f"<School {self.id} {self.name}>"


class Subscription(TimestampMixin, db.Model):
    __tablename__ = "subscription"

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(
        db.Integer, db.ForeignKey("school.id", ondelete="CASCADE"), nullable=False
    )
    plan = db.Column(db.String(32), default="yearly")  # пока один тариф
    start_date = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    end_date = db.Column(db.DateTime, default=default_sub_end, nullable=False)
    status = db.Column(db.String(16), index=True, default="active")  # active|expired|pending

    __table_args__ = (
        CheckConstraint("DATE(end_date) >= DATE(start_date)", name="ck_sub_dates"),
    )

    @property
    def is_active(self) -> bool:
        return (self.status == "active") and (self.end_date >= datetime.utcnow())

    def __repr__(self):
        return f"<Subscription school={self.school_id} {self.status}>"


class Classroom(TimestampMixin, db.Model):
    __tablename__ = "classroom"

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(
        db.Integer, db.ForeignKey("school.id", ondelete="CASCADE"), nullable=False
    )
    name = db.Column(db.String(32), nullable=False)   # "7А"
    grade = db.Column(db.Integer, nullable=False)     # 7..11

    __table_args__ = (
        UniqueConstraint("school_id", "name", name="uq_classroom_school_name"),
        CheckConstraint("grade BETWEEN 7 AND 11", name="ck_classroom_grade"),
    )

    def __repr__(self):
        return f"<Classroom {self.name} grade={self.grade} school={self.school_id}>"


# ---------- пользователи и профили ----------

class User(TimestampMixin, UserMixin, db.Model):
    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, index=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(
        db.Enum("student", "psych", "admin", name="user_role"),
        nullable=False,
        index=True,
    )

    # профили по ролям (один-к-одному)
    student = db.relationship("StudentProfile", backref="user", uselist=False, cascade="all, delete-orphan")
    psych = db.relationship("PsychologistProfile", backref="user", uselist=False, cascade="all, delete-orphan")
    admin = db.relationship("SchoolAdminProfile", backref="user", uselist=False, cascade="all, delete-orphan")

    # пароль
    def set_password(self, raw: str) -> None:
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw)

    def __repr__(self):
        return f"<User {self.email} role={self.role}>"



@login_manager.user_loader
def load_user(user_id):
    # современный способ для SQLAlchemy 2.x
    return db.session.get(User, int(user_id))


class StudentProfile(TimestampMixin, db.Model):
    __tablename__ = "student_profile"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    school_id = db.Column(
        db.Integer, db.ForeignKey("school.id", ondelete="RESTRICT"), nullable=False
    )
    classroom_id = db.Column(
        db.Integer, db.ForeignKey("classroom.id", ondelete="SET NULL")
    )
    grade = db.Column(db.Integer, nullable=False)

    school = db.relationship("School")
    classroom = db.relationship("Classroom")

    __table_args__ = (
        CheckConstraint("grade BETWEEN 7 AND 11", name="ck_student_grade"),
    )

    def __repr__(self):
        return f"<StudentProfile user={self.user_id} grade={self.grade}>"


class PsychologistProfile(TimestampMixin, db.Model):
    __tablename__ = "psychologist_profile"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    school_id = db.Column(
        db.Integer, db.ForeignKey("school.id", ondelete="RESTRICT"), nullable=False
    )

    school = db.relationship("School")

    def __repr__(self):
        return f"<PsychologistProfile user={self.user_id} school={self.school_id}>"


class SchoolAdminProfile(TimestampMixin, db.Model):
    __tablename__ = "school_admin_profile"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    school_id = db.Column(
        db.Integer, db.ForeignKey("school.id", ondelete="RESTRICT"), nullable=False
    )

    school = db.relationship("School")

    def __repr__(self):
        return f"<SchoolAdminProfile user={self.user_id} school={self.school_id}>"


# ---------- контакты (заявки с формы) ----------

class ContactMessage(db.Model):
    __tablename__ = "contact_message"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    email = db.Column(db.String(255), nullable=False, index=True)
    phone = db.Column(db.String(64))
    school = db.Column(db.String(160))
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default="new", index=True)  # new|seen|done
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<ContactMessage {self.email} {self.created_at:%Y-%m-%d}>"

# --- ТЕСТЫ / ВОПРОСЫ / ОТВЕТЫ ---

class Test(db.Model):
    __tablename__ = "test"
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(64), unique=True, index=True, nullable=False)     # 'holland', 'cdi' ...
    title = db.Column(db.String(200), nullable=False)
    short_desc = db.Column(db.String(400))
    long_desc = db.Column(db.Text)                                               # «Подробнее»
    duration_min = db.Column(db.Integer, default=5)                              # время на прохождение
    image = db.Column(db.String(200))                                            # static/img/tests/<slug>.png
    confidential_student = db.Column(db.Boolean, default=False)                  # если True – ученик НЕ видит свои результаты
    active = db.Column(db.Boolean, default=True)

    questions = db.relationship("TestQuestion", backref="test", lazy=True, order_by="TestQuestion.order")
    attempts = db.relationship("TestAttempt", backref="test", lazy=True)

class TestQuestion(db.Model):
    __tablename__ = "test_question"
    id = db.Column(db.Integer, primary_key=True)
    test_id = db.Column(db.Integer, db.ForeignKey("test.id"), nullable=False)
    order = db.Column(db.Integer, default=1, index=True)
    text = db.Column(db.Text, nullable=False)
    qtype = db.Column(db.Enum("single", "multi", "scale", "bool", name="qtype"), default="single")
    required = db.Column(db.Boolean, default=True)

    options = db.relationship("TestOption", backref="question", lazy=True, order_by="TestOption.order")

class TestOption(db.Model):
    __tablename__ = "test_option"
    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(db.Integer, db.ForeignKey("test_question.id"), nullable=False)
    order = db.Column(db.Integer, default=1)
    text = db.Column(db.String(300), nullable=False)
    value = db.Column(db.String(50))   # при желании можно хранить балл/код

class TestAttempt(db.Model):
    __tablename__ = "test_attempt"
    id = db.Column(db.Integer, primary_key=True)
    test_id = db.Column(db.Integer, db.ForeignKey("test.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)    # ученик (User.id)
    status = db.Column(db.Enum("in_progress", "submitted", "expired", name="attempt_status"), default="in_progress", index=True)
    started_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    finished_at = db.Column(db.DateTime)

    user = db.relationship("User")
    answers = db.relationship("TestAnswer", backref="attempt", lazy=True, cascade="all, delete-orphan")

    @property
    def deadline(self):
        return self.started_at + timedelta(minutes=self.test.duration_min)

class TestAnswer(db.Model):
    __tablename__ = "test_answer"
    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey("test_attempt.id"), nullable=False)
    question_id = db.Column(db.Integer, db.ForeignKey("test_question.id"), nullable=False)
    option_id = db.Column(db.Integer, db.ForeignKey("test_option.id"))  # для single/multi
    value_text = db.Column(db.String(200))                               # для scale/bool/свободного ввода

    question = db.relationship("TestQuestion")
    option = db.relationship("TestOption")

class AIThread(db.Model):
    __tablename__ = "ai_threads"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    title = db.Column(db.String(200))
    lang = db.Column(db.String(5), default="ru", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    messages = db.relationship(
        "AIMessage",
        backref="thread",
        lazy="dynamic",
        cascade="all, delete-orphan",
        order_by="AIMessage.created_at.asc()",
    )


class AIMessage(db.Model):
    __tablename__ = "ai_messages"
    id = db.Column(db.Integer, primary_key=True)
    thread_id = db.Column(db.Integer, db.ForeignKey("ai_threads.id"), nullable=False, index=True)
    role = db.Column(db.String(20), nullable=False)  # 'user' | 'assistant' | 'system'
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)