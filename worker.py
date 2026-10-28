import time
import psycopg
from app import db, expire

while True:
    try:
        with db() as conn:
            expire(conn)
    except psycopg.Error as error:
        print(f"Ошибка проверки резервирований: {error}", flush=True)
    time.sleep(15)
