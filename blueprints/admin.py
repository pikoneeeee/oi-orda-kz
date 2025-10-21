"""
Admin blueprint для управления пользователями и загрузки Excel
"""
from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash, current_app
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
import os
from functools import wraps
from extensions import db
from models import User, School, StudentProfile, PsychologistProfile, Classroom
from services.excel_import import parse_excel_file, create_users_from_excel, ExcelImportError

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

# Разрешенные расширения файлов
ALLOWED_EXTENSIONS = {'xlsx', 'xls'}


def admin_required(f):
    """Декоратор для проверки что это админ школы"""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('auth.login'))

        if current_user.role != 'admin':
            flash('У вас нет доступа к админ-панели', 'error')
            return redirect(url_for('public.index'))

        return f(*args, **kwargs)

    return decorated_function


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@admin_bp.route('/dashboard')
@login_required
@admin_required
def dashboard():
    """Админ-панель"""
    admin_profile = current_user.admin
    if not admin_profile:
        flash('Профиль администратора не найден', 'error')
        return redirect(url_for('public.index'))

    school = admin_profile.school

    # Статистика
    total_students = StudentProfile.query.filter_by(school_id=school.id).count()
    total_classrooms = Classroom.query.filter_by(school_id=school.id).count()
    total_psychologists = PsychologistProfile.query.filter_by(school_id=school.id).count()

    return render_template('admin/dashboard.html',
                           school=school,
                           total_students=total_students,
                           total_classrooms=total_classrooms,
                           total_psychologists=total_psychologists)


@admin_bp.route('/import-excel', methods=['GET', 'POST'])
@login_required
@admin_required
def import_excel():
    """Загрузка и импорт Excel файла"""
    admin_profile = current_user.admin
    if not admin_profile:
        flash('Профиль администратора не найден', 'error')
        return redirect(url_for('admin.dashboard'))

    school = admin_profile.school
    result = None
    errors = []

    if request.method == 'POST':
        # Проверяем наличие файла
        if 'file' not in request.files:
            flash('Файл не выбран', 'error')
            return redirect(request.url)

        file = request.files['file']

        if file.filename == '':
            flash('Файл не выбран', 'error')
            return redirect(request.url)

        if not allowed_file(file.filename):
            flash('Допустимы только .xlsx и .xls файлы', 'error')
            return redirect(request.url)

        try:
            # Парсим файл
            students_data, psychologist_data = parse_excel_file(file.stream)

            # Создаем пользователей
            result, errors = create_users_from_excel(
                school,
                students_data,
                psychologist_data
            )

            if not errors:
                flash(f'✓ Успешно импортировано: {len(result["students"])} учеников', 'success')
                if result['psychologist']:
                    flash('✓ Психолог создан успешно', 'success')
            else:
                for error in errors:
                    flash(f'⚠ {error}', 'warning')

        except ExcelImportError as e:
            flash(f'Ошибка в файле: {str(e)}', 'error')
        except Exception as e:
            flash(f'Ошибка при импорте: {str(e)}', 'error')

    return render_template('admin/import_excel.html',
                           school=school,
                           result=result,
                           errors=errors)


@admin_bp.route('/users')
@login_required
@admin_required
def users():
    """Список пользователей школы"""
    admin_profile = current_user.admin
    if not admin_profile:
        return redirect(url_for('admin.dashboard'))

    school = admin_profile.school

    # Ученики
    students = StudentProfile.query.filter_by(school_id=school.id).all()

    # Психологи
    psychologists = PsychologistProfile.query.filter_by(school_id=school.id).all()

    return render_template('admin/users.html',
                           school=school,
                           students=students,
                           psychologists=psychologists)


@admin_bp.route('/classrooms')
@login_required
@admin_required
def classrooms():
    """Список классов"""
    admin_profile = current_user.admin
    if not admin_profile:
        return redirect(url_for('admin.dashboard'))

    school = admin_profile.school
    classrooms_list = Classroom.query.filter_by(school_id=school.id).order_by(Classroom.grade, Classroom.name).all()

    return render_template('admin/classrooms.html',
                           school=school,
                           classrooms=classrooms_list)


@admin_bp.route('/api/user/<int:user_id>/reset-password', methods=['POST'])
@login_required
@admin_required
def reset_user_password(user_id):
    """API для сброса пароля пользователя"""
    admin_profile = current_user.admin
    if not admin_profile:
        return jsonify({'error': 'Forbidden'}), 403

    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    # Проверяем что пользователь принадлежит этой школе
    if user.role == 'student' and user.student.school_id != admin_profile.school_id:
        return jsonify({'error': 'Forbidden'}), 403
    elif user.role == 'psych' and user.psych.school_id != admin_profile.school_id:
        return jsonify({'error': 'Forbidden'}), 403

    # Генерируем новый пароль
    from services.excel_import import generate_temp_password
    new_password = generate_temp_password()
    user.set_password(new_password)
    db.session.commit()

    return jsonify({
        'success': True,
        'new_password': new_password,
        'message': 'Пароль успешно изменен'
    })


@admin_bp.route('/api/user/<int:user_id>/delete', methods=['DELETE'])
@login_required
@admin_required
def delete_user(user_id):
    """API для удаления пользователя"""
    admin_profile = current_user.admin
    if not admin_profile:
        return jsonify({'error': 'Forbidden'}), 403

    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    # Проверяем что пользователь принадлежит этой школе
    if user.role == 'student' and user.student.school_id != admin_profile.school_id:
        return jsonify({'error': 'Forbidden'}), 403
    elif user.role == 'psych' and user.psych.school_id != admin_profile.school_id:
        return jsonify({'error': 'Forbidden'}), 403

    db.session.delete(user)
    db.session.commit()

    return jsonify({
        'success': True,
        'message': 'Пользователь удален'
    })