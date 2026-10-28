import os
import uuid
from contextlib import contextmanager
from datetime import date
from functools import wraps

import click
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]


@contextmanager
def db():
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        yield conn


def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if session.get("role") != "admin":
            abort(403)
        return fn(*args, **kwargs)

    return wrapped


def expire(conn):
    # Lock rentals before changing related records so payment and expiry cannot race.
    rows = conn.execute(
        "SELECT id, car_id FROM rentals WHERE status='pending_payment' AND created_at < now() - interval '15 minutes' FOR UPDATE"
    ).fetchall()
    for row in rows:
        conn.execute("UPDATE rentals SET status='cancelled' WHERE id=%s", (row["id"],))
        conn.execute(
            "UPDATE payments SET status='failed' WHERE rental_id=%s AND status='pending'",
            (row["id"],),
        )
        conn.execute(
            "UPDATE cars SET status='free' WHERE id=%s AND status='reserved'",
            (row["car_id"],),
        )


@app.before_request
def before_request():
    if request.endpoint == "static":
        return
    with db() as conn:
        expire(conn)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        fields = [
            request.form.get(k, "").strip()
            for k in ("username", "email", "name", "phone", "license", "experience")
        ]
        password = request.form.get("password", "")
        try:
            if (
                not all(fields)
                or len(password) < 8
                or "@" not in fields[1]
                or int(fields[5]) < 0
            ):
                raise ValueError
            with db() as conn:
                driver = conn.execute(
                    "INSERT INTO drivers (name,phone,license,experience) VALUES (%s,%s,%s,%s) RETURNING id",
                    fields[2:],
                ).fetchone()
                conn.execute(
                    "INSERT INTO users (username,email,password_hash,role,driver_id) VALUES (%s,%s,%s,'renter',%s)",
                    (
                        fields[0],
                        fields[1].lower(),
                        generate_password_hash(password),
                        driver["id"],
                    ),
                )
            flash("Регистрация завершена. Войдите в систему.", "success")
            return redirect(url_for("login"))
        except (ValueError, psycopg.IntegrityError, psycopg.DataError):
            flash(
                "Проверьте поля: логин, почта и номер удостоверения должны быть уникальными; пароль — не менее 8 символов.",
                "error",
            )
    return render_template("auth.html", register=True)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        with db() as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE username=%s",
                (request.form.get("username", "").strip(),),
            ).fetchone()
        if user and check_password_hash(
            user["password_hash"], request.form.get("password", "")
        ):
            session.clear()
            session.update(
                user_id=user["id"], username=user["username"], role=user["role"]
            )
            return redirect(url_for("index"))
        flash("Неверный логин или пароль.", "error")
    return render_template("auth.html", register=False)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.get("/")
def index():
    with db() as conn:
        cars = conn.execute(
            "SELECT * FROM cars WHERE status='free' ORDER BY id"
        ).fetchall()
    return render_template("catalog.html", cars=cars)


@app.get("/cars/<int:car_id>")
def car_detail(car_id):
    with db() as conn:
        car = conn.execute("SELECT * FROM cars WHERE id=%s", (car_id,)).fetchone()
        open_rental = (
            conn.execute(
                "SELECT id FROM rentals WHERE renter_id=%s AND status IN ('pending_payment','active')",
                (session["user_id"],),
            ).fetchone()
            if session.get("role") == "renter"
            else None
        )
    if not car:
        abort(404)
    return render_template("car.html", car=car, open_rental=open_rental)


@app.route("/rent/new/<int:car_id>", methods=["GET", "POST"])
def rent_new(car_id):
    if session.get("role") != "renter":
        return redirect(url_for("login"))
    if request.method == "GET":
        with db() as conn:
            open_rental = conn.execute(
                "SELECT id FROM rentals WHERE renter_id=%s AND status IN ('pending_payment','active')",
                (session["user_id"],),
            ).fetchone()
        if open_rental:
            flash(
                "У вас уже есть открытая аренда. Завершите её перед оформлением новой.",
                "error",
            )
            return redirect(url_for("my_rentals"))
    if request.method == "POST":
        try:
            start = date.fromisoformat(request.form.get("start_date", ""))
            end = date.fromisoformat(request.form.get("end_date", ""))
            if start < date.today() or end <= start:
                raise ValueError("Укажите корректные даты.")
            with db() as conn:
                car = conn.execute(
                    "SELECT * FROM cars WHERE id=%s FOR UPDATE", (car_id,)
                ).fetchone()
                user = conn.execute(
                    "SELECT * FROM users WHERE id=%s FOR UPDATE", (session["user_id"],)
                ).fetchone()
                if not car or car["status"] != "free":
                    raise ValueError("Автомобиль уже недоступен.")
                if not user or not user["driver_id"]:
                    raise ValueError("Для аренды необходим профиль водителя.")
                if conn.execute(
                    "SELECT id FROM rentals WHERE renter_id=%s AND status IN ('pending_payment','active')",
                    (user["id"],),
                ).fetchone():
                    raise ValueError("У вас уже есть открытая аренда.")
                if conn.execute(
                    "SELECT id FROM car_driver WHERE driver_id=%s AND unassigned_date IS NULL",
                    (user["driver_id"],),
                ).fetchone():
                    raise ValueError("Водитель уже закреплён за автомобилем.")
                if conn.execute(
                    "SELECT id FROM rentals WHERE car_id=%s AND status IN ('pending_payment','active') AND start_date < %s AND end_date > %s",
                    (car_id, end, start),
                ).fetchone():
                    raise ValueError("Даты пересекаются с другой арендой.")
                rental = conn.execute(
                    "INSERT INTO rentals (car_id,renter_id,start_date,end_date,total_price,status) VALUES (%s,%s,%s,%s,%s,'pending_payment') RETURNING id,total_price",
                    (
                        car_id,
                        user["id"],
                        start,
                        end,
                        (end - start).days * car["daily_rate"],
                    ),
                ).fetchone()
                conn.execute("UPDATE cars SET status='reserved' WHERE id=%s", (car_id,))
                conn.execute(
                    "INSERT INTO payments (rental_id,amount,status,transaction_number) VALUES (%s,%s,'pending',%s)",
                    (rental["id"], rental["total_price"], uuid.uuid4().hex),
                )
            return redirect(url_for("rent_pay", rental_id=rental["id"]))
        except (ValueError, psycopg.IntegrityError) as e:
            flash(
                (
                    str(e)
                    if isinstance(e, ValueError)
                    else "Аренда уже оформляется. Обновите страницу."
                ),
                "error",
            )
    with db() as conn:
        car = conn.execute("SELECT * FROM cars WHERE id=%s", (car_id,)).fetchone()
    if not car:
        abort(404)
    return render_template("rent.html", car=car, today=date.today().isoformat())


@app.route("/rent/pay/<int:rental_id>", methods=["GET", "POST"])
def rent_pay(rental_id):
    if session.get("role") != "renter":
        abort(403)
    try:
        with db() as conn:
            rental = conn.execute(
                "SELECT r.*, c.brand,c.model,c.plate,u.driver_id, r.created_at < now() - interval '15 minutes' AS expired FROM rentals r JOIN cars c ON c.id=r.car_id JOIN users u ON u.id=r.renter_id WHERE r.id=%s AND r.renter_id=%s FOR UPDATE OF r",
                (rental_id, session["user_id"]),
            ).fetchone()
            if not rental:
                abort(404)
            if request.method == "POST":
                if rental["status"] != "pending_payment" or rental["expired"]:
                    raise ValueError(
                        "Оплата недоступна: аренда уже обработана или время истекло."
                    )
                if request.form.get("result") == "success":
                    conn.execute(
                        "INSERT INTO car_driver (car_id,driver_id,rental_id) VALUES (%s,%s,%s)",
                        (rental["car_id"], rental["driver_id"], rental_id),
                    )
                    conn.execute(
                        "UPDATE payments SET status='success',paid_at=now() WHERE rental_id=%s AND status='pending'",
                        (rental_id,),
                    )
                    conn.execute(
                        "UPDATE rentals SET status='active',paid_at=now() WHERE id=%s",
                        (rental_id,),
                    )
                    conn.execute(
                        "UPDATE cars SET status='rented' WHERE id=%s",
                        (rental["car_id"],),
                    )
                    flash("Оплата прошла успешно, аренда активна.", "success")
                elif request.form.get("result") == "failed":
                    conn.execute(
                        "UPDATE payments SET status='failed' WHERE rental_id=%s AND status='pending'",
                        (rental_id,),
                    )
                    conn.execute(
                        "UPDATE rentals SET status='cancelled' WHERE id=%s",
                        (rental_id,),
                    )
                    conn.execute(
                        "UPDATE cars SET status='free' WHERE id=%s", (rental["car_id"],)
                    )
                    flash("Оплата отклонена, резервирование отменено.", "error")
                else:
                    raise ValueError("Выберите результат оплаты.")
                return redirect(url_for("my_rentals"))
    except (ValueError, psycopg.IntegrityError) as e:
        flash(
            (
                str(e)
                if isinstance(e, ValueError)
                else "Водитель уже закреплён за автомобилем."
            ),
            "error",
        )
        return redirect(url_for("my_rentals"))
    return render_template("pay.html", rental=rental)


@app.get("/my_rentals")
def my_rentals():
    if session.get("role") != "renter":
        abort(403)
    with db() as conn:
        rows = conn.execute(
            "SELECT r.*,c.brand,c.model,c.plate FROM rentals r JOIN cars c ON c.id=r.car_id WHERE r.renter_id=%s ORDER BY r.id DESC",
            (session["user_id"],),
        ).fetchall()
    return render_template("rentals.html", rows=rows, admin=False)


@app.get("/my_payments")
def my_payments():
    if session.get("role") != "renter":
        abort(403)
    with db() as conn:
        rows = conn.execute(
            "SELECT p.*,r.renter_id FROM payments p JOIN rentals r ON r.id=p.rental_id WHERE r.renter_id=%s ORDER BY p.id DESC",
            (session["user_id"],),
        ).fetchall()
    return render_template("payments.html", rows=rows)


@app.post("/rent/<int:rental_id>/<action>")
def rent_finish(rental_id, action):
    if action not in ("complete", "cancel") or not session.get("user_id"):
        abort(403)
    if action == "cancel" and session.get("role") != "admin":
        abort(403)
    try:
        with db() as conn:
            r = conn.execute(
                "SELECT * FROM rentals WHERE id=%s FOR UPDATE", (rental_id,)
            ).fetchone()
            if not r:
                abort(404)
            if session.get("role") != "admin" and r["renter_id"] != session["user_id"]:
                abort(403)
            if (action == "complete" and r["status"] != "active") or (
                action == "cancel" and r["status"] not in ("active", "pending_payment")
            ):
                raise ValueError("Аренда уже завершена.")
            conn.execute(
                "UPDATE rentals SET status=%s WHERE id=%s",
                ("completed" if action == "complete" else "cancelled", rental_id),
            )
            conn.execute(
                "UPDATE payments SET status='failed' WHERE rental_id=%s AND status='pending'",
                (rental_id,),
            )
            conn.execute(
                "UPDATE car_driver SET unassigned_date=now() WHERE rental_id=%s AND unassigned_date IS NULL",
                (rental_id,),
            )
            conn.execute("UPDATE cars SET status='free' WHERE id=%s", (r["car_id"],))
        flash("Аренда обновлена.", "success")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(
        url_for("admin_rentals" if session.get("role") == "admin" else "my_rentals")
    )


@app.cli.command("create-user")
def create_user():
    username = click.prompt("Логин")
    email = click.prompt("Email")
    password = click.prompt("Пароль", hide_input=True, confirmation_prompt=True)
    if len(password) < 8:
        raise click.ClickException("Пароль должен содержать не менее 8 символов.")
    with db() as conn:
        conn.execute(
            "INSERT INTO users (username,email,password_hash,role) VALUES (%s,%s,%s,'admin')",
            (username, email.lower(), generate_password_hash(password)),
        )
    click.echo("Администратор создан.")


# Whitelisted columns keep SQL identifiers independent of request data.
ENTITIES = {
    "cars": {
        "title": "Автомобили",
        "fields": [
            ("brand", "Марка", "text"),
            ("model", "Модель", "text"),
            ("plate", "Госномер", "text"),
            ("year", "Год", "number"),
            ("vin", "VIN", "text"),
            ("daily_rate", "Цена за сутки", "number"),
        ],
    },
    "drivers": {
        "title": "Водители",
        "fields": [
            ("name", "ФИО", "text"),
            ("phone", "Телефон", "text"),
            ("license", "Удостоверение", "text"),
            ("experience", "Стаж", "number"),
            ("status", "Статус", "driver_status"),
        ],
    },
    "maintenance": {
        "title": "Обслуживание",
        "fields": [
            ("car_id", "Автомобиль", "car"),
            ("service_date", "Дата", "date"),
            ("type", "Тип", "service_type"),
            ("cost", "Стоимость", "number"),
            ("description", "Описание", "text"),
        ],
    },
    "fuel_records": {
        "title": "Заправки",
        "fields": [
            ("car_id", "Автомобиль", "car"),
            ("fuel_date", "Дата", "date"),
            ("liters", "Литры", "number"),
            ("cost", "Стоимость", "number"),
        ],
    },
}


@app.get("/admin")
@admin_required
def admin_dashboard():
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    try:
        start_date = date.fromisoformat(start) if start else date(1900, 1, 1)
        end_date = date.fromisoformat(end) if end else date(2100, 1, 1)
        if end_date < start_date:
            raise ValueError
    except ValueError:
        flash("Проверьте период отчёта.", "error")
        start_date, end_date = date(1900, 1, 1), date(2100, 1, 1)
    with db() as conn:
        counts = conn.execute(
            "SELECT count(*) FILTER (WHERE status='free') AS free, count(*) FILTER (WHERE status='rented') AS rented, count(*) FILTER (WHERE status='repair') AS repair, count(*) FILTER (WHERE status='reserved') AS reserved FROM cars"
        ).fetchone()
        active = conn.execute(
            "SELECT count(*) AS n FROM rentals WHERE status='active'"
        ).fetchone()["n"]
        revenue = conn.execute(
            "SELECT coalesce(sum(amount),0) AS n FROM payments WHERE status='success' AND paid_at::date BETWEEN %s AND %s",
            (start_date, end_date),
        ).fetchone()["n"]
    return render_template(
        "dashboard.html", counts=counts, active=active, revenue=revenue
    )


@app.get("/admin/<kind>")
@admin_required
def admin_list(kind):
    if kind not in ENTITIES:
        abort(404)
    with db() as conn:
        rows = conn.execute(
            sql.SQL("SELECT * FROM {} ORDER BY id DESC").format(sql.Identifier(kind))
        ).fetchall()
        cars = conn.execute(
            "SELECT id,brand,model,plate,status FROM cars ORDER BY id"
        ).fetchall()
    status = request.args.get("status", "").strip()
    car_filter = request.args.get("car_id", "").strip()
    date_from, date_to = request.args.get("from", ""), request.args.get("to", "")
    if status and kind in ("cars", "drivers"):
        rows = [r for r in rows if r["status"] == status]
    if car_filter and kind in ("maintenance", "fuel_records"):
        rows = [r for r in rows if str(r["car_id"]) == car_filter]
    date_field = "service_date" if kind == "maintenance" else "fuel_date"
    if kind in ("maintenance", "fuel_records"):
        rows = [
            r
            for r in rows
            if (not date_from or str(r[date_field]) >= date_from)
            and (not date_to or str(r[date_field]) <= date_to)
        ]
    return render_template(
        "admin_list.html", kind=kind, spec=ENTITIES[kind], rows=rows, cars=cars
    )


@app.route("/admin/<kind>/edit", defaults={"record_id": None}, methods=["GET", "POST"])
@app.route("/admin/<kind>/edit/<int:record_id>", methods=["GET", "POST"])
@admin_required
def admin_edit(kind, record_id):
    if kind not in ENTITIES:
        abort(404)
    spec = ENTITIES[kind]
    if request.method == "POST":
        values = [request.form.get(key, "").strip() for key, _, _ in spec["fields"]]
        try:
            if any(
                not v
                for (key, _, _), v in zip(spec["fields"], values)
                if key != "description"
            ):
                raise ValueError("Заполните все обязательные поля.")
            with db() as conn:
                old = (
                    conn.execute(
                        sql.SQL("SELECT * FROM {} WHERE id=%s FOR UPDATE").format(
                            sql.Identifier(kind)
                        ),
                        (record_id,),
                    ).fetchone()
                    if record_id
                    else None
                )
                if record_id and not old:
                    abort(404)
                if kind == "maintenance":
                    car_id = int(values[0])
                    car = conn.execute(
                        "SELECT * FROM cars WHERE id=%s FOR UPDATE", (car_id,)
                    ).fetchone()
                    if not car:
                        raise ValueError("Автомобиль не найден.")
                    if old and old["is_completed"]:
                        raise ValueError("Выполненную запись нельзя изменить.")
                    if car["status"] in ("reserved", "rented"):
                        raise ValueError("Автомобиль занят арендой.")
                    if car["status"] == "repair" and (
                        not old or old["car_id"] != car_id
                    ):
                        raise ValueError("Автомобиль уже на обслуживании.")
                    if old and old["car_id"] != car_id:
                        conn.execute(
                            "UPDATE cars SET status='free' WHERE id=%s AND status='repair'",
                            (old["car_id"],),
                        )
                    conn.execute(
                        "UPDATE cars SET status='repair' WHERE id=%s", (car_id,)
                    )
                columns = [field[0] for field in spec["fields"]]
                if record_id:
                    query = sql.SQL("UPDATE {} SET {} WHERE id=%s").format(
                        sql.Identifier(kind),
                        sql.SQL(",").join(
                            sql.SQL("{}=%s").format(sql.Identifier(c)) for c in columns
                        ),
                    )
                    conn.execute(query, values + [record_id])
                else:
                    query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                        sql.Identifier(kind),
                        sql.SQL(",").join(map(sql.Identifier, columns)),
                        sql.SQL(",").join(sql.Placeholder() for _ in columns),
                    )
                    conn.execute(query, values)
            flash("Запись сохранена.", "success")
            return redirect(url_for("admin_list", kind=kind))
        except (ValueError, psycopg.IntegrityError, psycopg.DataError) as e:
            flash(
                (
                    str(e)
                    if isinstance(e, ValueError)
                    else "Проверьте значения и уникальность полей."
                ),
                "error",
            )
    with db() as conn:
        row = (
            conn.execute(
                sql.SQL("SELECT * FROM {} WHERE id=%s").format(sql.Identifier(kind)),
                (record_id,),
            ).fetchone()
            if record_id
            else None
        )
        cars = conn.execute("SELECT * FROM cars ORDER BY id").fetchall()
    if record_id and not row:
        abort(404)
    return render_template("edit.html", kind=kind, spec=spec, row=row, cars=cars)


@app.post("/admin/<kind>/delete/<int:record_id>")
@admin_required
def admin_delete(kind, record_id):
    if kind not in ENTITIES:
        abort(404)
    try:
        with db() as conn:
            row = conn.execute(
                sql.SQL("SELECT * FROM {} WHERE id=%s FOR UPDATE").format(
                    sql.Identifier(kind)
                ),
                (record_id,),
            ).fetchone()
            if not row:
                abort(404)
            if kind == "cars" and row["status"] != "free":
                raise ValueError("Удалить можно только свободный автомобиль.")
            if kind == "maintenance" and not row["is_completed"]:
                conn.execute(
                    "UPDATE cars SET status='free' WHERE id=%s AND status='repair'",
                    (row["car_id"],),
                )
            conn.execute(
                sql.SQL("DELETE FROM {} WHERE id=%s").format(sql.Identifier(kind)),
                (record_id,),
            )
        flash("Запись удалена.", "success")
    except (ValueError, psycopg.IntegrityError) as e:
        flash(
            (
                str(e)
                if isinstance(e, ValueError)
                else "Запись связана с другими данными и не может быть удалена."
            ),
            "error",
        )
    return redirect(url_for("admin_list", kind=kind))


@app.post("/admin/maintenance/<int:record_id>/complete")
@admin_required
def maintenance_complete(record_id):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM maintenance WHERE id=%s FOR UPDATE", (record_id,)
        ).fetchone()
        if not row:
            abort(404)
        if not row["is_completed"]:
            conn.execute(
                "UPDATE maintenance SET is_completed=true WHERE id=%s", (record_id,)
            )
            conn.execute(
                "UPDATE cars SET status='free' WHERE id=%s AND status='repair'",
                (row["car_id"],),
            )
    flash("Обслуживание завершено.", "success")
    return redirect(url_for("admin_list", kind="maintenance"))


@app.get("/admin/rentals")
@admin_required
def admin_rentals():
    with db() as conn:
        rows = conn.execute(
            "SELECT r.*,c.brand,c.model,c.plate,u.username FROM rentals r JOIN cars c ON c.id=r.car_id JOIN users u ON u.id=r.renter_id ORDER BY r.id DESC"
        ).fetchall()
    return render_template("rentals.html", rows=rows, admin=True)


@app.get("/admin/payments")
@admin_required
def admin_payments():
    with db() as conn:
        rows = conn.execute(
            "SELECT p.*,u.username FROM payments p JOIN rentals r ON r.id=p.rental_id JOIN users u ON u.id=r.renter_id ORDER BY p.id DESC"
        ).fetchall()
    status, username, start, end = (
        request.args.get(k, "").strip() for k in ("status", "username", "from", "to")
    )
    rows = [
        r
        for r in rows
        if (not status or r["status"] == status)
        and (not username or username.lower() in r["username"].lower())
        and (not start or str(r["created_at"].date()) >= start)
        and (not end or str(r["created_at"].date()) <= end)
    ]
    return render_template("payments.html", rows=rows, admin=True)


@app.get("/admin/users")
@admin_required
def admin_users():
    with db() as conn:
        rows = conn.execute(
            "SELECT u.id,u.username,u.email,u.role,u.created_at,d.name FROM users u LEFT JOIN drivers d ON d.id=u.driver_id ORDER BY u.id"
        ).fetchall()
    return render_template("users.html", rows=rows)


@app.post("/admin/users/<int:user_id>/delete")
@admin_required
def delete_user(user_id):
    if user_id == session["user_id"]:
        flash("Нельзя удалить свою учётную запись.", "error")
    else:
        try:
            with db() as conn:
                conn.execute("DELETE FROM users WHERE id=%s", (user_id,))
            flash("Пользователь удалён.", "success")
        except psycopg.IntegrityError:
            flash("Нельзя удалить пользователя с историей аренд.", "error")
    return redirect(url_for("admin_users"))
