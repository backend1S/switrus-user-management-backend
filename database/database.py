
import pymssql
from dotenv import load_dotenv
import os

load_dotenv()

class Database:

    def __init__(self):
        self.conn = ""

    def db_connect(self):
        """
        Establishes a connection to the database and returns a cursor.
        """
        try:
            if os.getenv('DB_ENGINE', 'mssql').lower() == 'sqlite':
                # Local development / tests only (see database/sqlite_dev.py).
                from database import sqlite_dev
                self.conn = sqlite_dev.connect()
                return True, self.conn.cursor()
            # DB_* names are preferred: USER/PASSWORD collide with shell
            # variables on Linux. The old names still work.
            self.conn = pymssql.connect(
                server=os.getenv('DB_SERVER') or os.getenv('SERVER'),
                user=os.getenv('DB_USER') or os.getenv('USER'),
                password=os.getenv('DB_PASSWORD') or os.getenv('PASSWORD'),
                database=os.getenv('DB_NAME') or os.getenv('DATABASE'),
                login_timeout=int(os.getenv('DB_LOGIN_TIMEOUT', '10')),
            )
            cur = self.conn.cursor()
            return True, cur
        except Exception as e:
            return False, str(e)

    def db_query(self, sql_query, params=None):
        rows = ""
        cursor = None
    
        try:
            status, cursor = self.db_connect()
            if status:
                
                if params:
                    cursor.execute(sql_query, params) 
                else:
                    cursor.execute(sql_query)
                
                rows = cursor.fetchall()
                
                cursor.close()
                self.conn.close()
                return True, rows
            else:
                return status, cursor 
                
        except Exception as e:
            if cursor:
                cursor.close()
            if self.conn:
                self.conn.close()
            return False, str(e)

    def db_update(self, sql_query, params=None):
        """
        Executes an UPDATE/INSERT/DELETE query with optional parameters.
        Must also be simplified for positional parameters.
        """
        try:
            status, cursor = self.db_connect()
            if status:
                if params:
                    cursor.execute(sql_query, params)
                else:
                    cursor.execute(sql_query)
                self.conn.commit()
                cursor.close()
                self.conn.close()
                return True, "Query executed successfully"
            else:
                return status, cursor
        except Exception as e:
            cursor = locals().get('cursor')
            if cursor:
                cursor.close()
            if self.conn:
                self.conn.close()
            return False, str(e)

    def db_execute(self, sql_query, params=None):
        """
        Like db_update, but returns the number of affected rows instead of a
        message, so callers can implement conditional updates (locks).
        """
        cursor = None
        try:
            status, cursor = self.db_connect()
            if not status:
                return False, cursor
            if params:
                cursor.execute(sql_query, params)
            else:
                cursor.execute(sql_query)
            count = cursor.rowcount
            self.conn.commit()
            cursor.close()
            self.conn.close()
            return True, count
        except Exception as e:
            if cursor and not isinstance(cursor, str):
                cursor.close()
            if self.conn:
                self.conn.close()
            return False, str(e)
