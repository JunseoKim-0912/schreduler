import sqlite3

conn = sqlite3.connect("schreduler.db")
tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
print("테이블 목록:", tables)

users = conn.execute("SELECT * FROM users").fetchall()
print("users 테이블:", users)
