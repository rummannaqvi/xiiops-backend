import os
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()

DB_URL = os.getenv("DATABASE_URL")

def get_db_connection():
    """Establish and return a connection to the PostgreSQL database."""
    try:
        conn = psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)
        return conn
    except Exception as e:
        print(f"Error connecting to the database: {e}")
        return None

def init_db():
    """Initialize the Context Graph table with JSONB support."""
    if not DB_URL:
        print("⚠️  DATABASE_URL not set. Skipping DB init. Chat history will be in-memory only.")
        return

    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS context_graph (
                        resource_id VARCHAR(255) PRIMARY KEY,
                        project_name VARCHAR(255) NOT NULL,
                        provider VARCHAR(50) NOT NULL,
                        resource_type VARCHAR(100) NOT NULL,
                        state_data JSONB NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS chat_history (
                        id SERIAL PRIMARY KEY,
                        session_id VARCHAR(255) NOT NULL,
                        message JSONB NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)
            conn.commit()
            print("✅ Database initialized: tables are ready.")
        except Exception as e:
            print(f"Error initializing tables: {e}")
        finally:
            conn.close()

if __name__ == "__main__":
    init_db()
