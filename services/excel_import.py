"""
Сервис для импорта учеников и психолога из Excel файла
"""
import io
import secrets
from typing import Tuple, List, Dict
import pandas as pd
from flask import current_app
from extensions import db
from models import User, StudentProfile, PsychologistProfile, School, Classroom


class ExcelImportError(Exception):
    """Ошибка при импорте Excel"""
    pass


def generate_temp_password() -> str:
    """Генерирует временный пароль"""
    return secrets.token_urlsafe(8)


def parse_excel_file(file_stream) -> Tuple[List[Dict], Dict]:
    """
    Парсит Excel файл и возвращает данные учеников и психолога

    Returns:
        (students_data, psychologist_data)
    """
    try:
        # Читаем оба листа
        excel_file = pd.ExcelFile(file_stream)

        if 'Ученики' not in excel_file.sheet_names:
            raise ExcelImportError("Не найден лист 'Ученики'")

        # Парсим учеников
        students_df = pd.read_excel(excel_file, sheet_name='Ученики')
        required_cols_students = ['Фамилия', 'Имя', 'Email', 'Класс', 'Класс номер']

        missing = [col for col in required_cols_students if col not in students_df.columns]
        if missing:
            raise ExcelImportError(f"Отсутствуют колонки: {', '.join(missing)}")

        # Парсим психолога (опционально)
        psychologist_data = None
        if 'Психолог' in excel_file.sheet_names:
            psych_df = pd.read_excel(excel_file, sheet_name='Психолог')
            required_cols_psych = ['Фамилия', 'Имя', 'Email']

            missing = [col for col in required_cols_psych if col not in psych_df.columns]
            if missing:
                raise ExcelImportError(f"Психолог: отсутствуют колонки {', '.join(missing)}")

            if len(psych_df) > 0:
                row = psych_df.iloc[0]
                psychologist_data = {
                    'last_name': str(row['Фамилия']).strip(),
                    'first_name': str(row['Имя']).strip(),
                    'middle_name': str(row.get('Отчество', '')).strip() or '',
                    'email': str(row['Email']).strip().lower(),
                }

        # Преобразуем учеников в список словарей
        students_data = []
        for idx, row in students_df.iterrows():
            try:
                student = {
                    'last_name': str(row['Фамилия']).strip(),
                    'first_name': str(row['Имя']).strip(),
                    'middle_name': str(row.get('Отчество', '')).strip() or '',
                    'email': str(row['Email']).strip().lower(),
                    'class_name': str(row['Класс']).strip(),
                    'grade': int(row['Класс номер']),
                    'gender': str(row.get('Пол', '')).strip() or None,
                }

                # Валидация
                if not student['email'] or '@' not in student['email']:
                    raise ExcelImportError(f"Строка {idx + 2}: некорректный email")
                if student['grade'] < 7 or student['grade'] > 11:
                    raise ExcelImportError(f"Строка {idx + 2}: класс должен быть от 7 до 11")

                students_data.append(student)
            except ValueError as e:
                raise ExcelImportError(f"Строка {idx + 2}: ошибка парсинга данных - {str(e)}")

        if not students_data:
            raise ExcelImportError("В листе 'Ученики' нет данных")

        return students_data, psychologist_data

    except pd.errors.ParserError as e:
        raise ExcelImportError(f"Ошибка чтения файла: {str(e)}")
    except Exception as e:
        if isinstance(e, ExcelImportError):
            raise
        raise ExcelImportError(f"Неожиданная ошибка: {str(e)}")


def create_users_from_excel(
        school: School,
        students_data: List[Dict],
        psychologist_data: Dict = None
) -> Tuple[Dict, List[str]]:
    """
    Создает пользователей из данных Excel

    Returns:
        (result_dict, errors_list)
        result_dict содержит информацию о созданных пользователях и пароли
    """
    result = {
        'students': [],
        'psychologist': None,
        'new_classrooms': []
    }
    errors = []

    try:
        # Создаем классы которых нет
        existing_classrooms = {
            (c.name, c.grade): c
            for c in Classroom.query.filter_by(school_id=school.id).all()
        }

        classrooms_to_create = set()
        for student in students_data:
            key = (student['class_name'], student['grade'])
            if key not in existing_classrooms:
                classrooms_to_create.add(key)

        # Создаем новые классы
        for class_name, grade in classrooms_to_create:
            try:
                new_classroom = Classroom(
                    school_id=school.id,
                    name=class_name,
                    grade=grade
                )
                db.session.add(new_classroom)
                existing_classrooms[(class_name, grade)] = new_classroom
                result['new_classrooms'].append(class_name)
            except Exception as e:
                errors.append(f"Ошибка при создании класса {class_name}: {str(e)}")

        db.session.flush()

        # Создаем учеников
        for student_data in students_data:
            try:
                # Проверяем существует ли уже пользователь
                existing_user = User.query.filter_by(email=student_data['email']).first()
                if existing_user:
                    errors.append(f"Email {student_data['email']} уже зарегистрирован")
                    continue

                # Генерируем пароль
                password = generate_temp_password()

                # Создаем пользователя
                user = User(
                    email=student_data['email'],
                    role='student'
                )
                user.set_password(password)
                db.session.add(user)
                db.session.flush()

                # Получаем класс
                classroom_key = (student_data['class_name'], student_data['grade'])
                classroom = existing_classrooms[classroom_key]

                # Создаем профиль ученика
                student_profile = StudentProfile(
                    user_id=user.id,
                    school_id=school.id,
                    classroom_id=classroom.id,
                    grade=student_data['grade']
                )
                db.session.add(student_profile)

                result['students'].append({
                    'email': student_data['email'],
                    'password': password,
                    'class': student_data['class_name'],
                    'full_name': f"{student_data['last_name']} {student_data['first_name']}"
                })

            except Exception as e:
                errors.append(f"Ошибка при создании ученика {student_data['email']}: {str(e)}")

        # Создаем психолога если данные есть
        if psychologist_data:
            try:
                existing_psych = User.query.filter_by(email=psychologist_data['email']).first()
                if existing_psych:
                    errors.append(f"Email психолога {psychologist_data['email']} уже зарегистрирован")
                else:
                    password = generate_temp_password()

                    psych_user = User(
                        email=psychologist_data['email'],
                        role='psych'
                    )
                    psych_user.set_password(password)
                    db.session.add(psych_user)
                    db.session.flush()

                    psych_profile = PsychologistProfile(
                        user_id=psych_user.id,
                        school_id=school.id
                    )
                    db.session.add(psych_profile)

                    result['psychologist'] = {
                        'email': psychologist_data['email'],
                        'password': password,
                        'full_name': f"{psychologist_data['last_name']} {psychologist_data['first_name']}"
                    }
            except Exception as e:
                errors.append(f"Ошибка при создании психолога: {str(e)}")

        # Коммитим все изменения
        db.session.commit()

    except Exception as e:
        db.session.rollback()
        errors.append(f"Критическая ошибка при сохранении: {str(e)}")

    return result, errors