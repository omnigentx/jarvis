"""
Migration script: Multi-user → Single-user.

Removes the `users` table and `user_id` foreign keys from `books` and `story_progress`.
Preserves all existing data (books, story progress, TTS cache, etc).

Usage:
    python migrate_single_user.py                      # Migrate data/jarvis.db
    python migrate_single_user.py --db-path /tmp/test.db
    python migrate_single_user.py --dry-run             # Preview only, no changes
"""

import argparse
import os
import shutil
import sqlite3
import sys
import time


def backup_db(db_path: str) -> str:
    """Create a timestamped backup of the database."""
    ts = int(time.time())
    backup_path = f"{db_path}.bak.{ts}"
    shutil.copy2(db_path, backup_path)
    return backup_path


def get_table_info(conn: sqlite3.Connection, table: str) -> list[dict]:
    """Get column info for a table."""
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return [
        {"cid": row[0], "name": row[1], "type": row[2], "notnull": row[3], "pk": row[5]}
        for row in cursor.fetchall()
    ]


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Check if a table exists."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return cursor.fetchone() is not None


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    """Count rows in a table."""
    cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")
    return cursor.fetchone()[0]


def migrate(db_path: str, dry_run: bool = False):
    """Run the migration."""
    print(f"{'[DRY RUN] ' if dry_run else ''}Migration: Multi-user → Single-user")
    print(f"Database: {db_path}")
    print()

    if not os.path.exists(db_path):
        print(f"ERROR: Database not found at {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")

    # --- Pre-migration info ---
    print("=== Pre-migration state ===")
    
    has_users = table_exists(conn, "users")
    has_books = table_exists(conn, "books")
    has_story_progress = table_exists(conn, "story_progress")

    if has_users:
        print(f"  users: {count_rows(conn, 'users')} rows")
    else:
        print("  users: TABLE NOT FOUND (may already be migrated)")

    if has_books:
        book_count = count_rows(conn, "books")
        cols = [c["name"] for c in get_table_info(conn, "books")]
        has_user_id_books = "user_id" in cols
        print(f"  books: {book_count} rows, has user_id: {has_user_id_books}")
    else:
        print("  books: TABLE NOT FOUND")
        has_user_id_books = False

    if has_story_progress:
        sp_count = count_rows(conn, "story_progress")
        cols = [c["name"] for c in get_table_info(conn, "story_progress")]
        has_user_id_sp = "user_id" in cols
        print(f"  story_progress: {sp_count} rows, has user_id: {has_user_id_sp}")
    else:
        print("  story_progress: TABLE NOT FOUND")
        has_user_id_sp = False

    # Check if migration is needed
    if not has_users and not has_user_id_books and not has_user_id_sp:
        print("\n✅ Database already migrated. Nothing to do.")
        conn.close()
        return

    print()

    # --- Backup ---
    if not dry_run:
        backup_path = backup_db(db_path)
        print(f"📦 Backup created: {backup_path}")
    else:
        print("📦 Backup: SKIPPED (dry run)")

    print()

    # --- Migrate books table ---
    if has_books and has_user_id_books:
        print("🔧 Migrating 'books' table (removing user_id)...")
        
        if not dry_run:
            # SQLite lacks ALTER TABLE DROP COLUMN (pre-3.35), so recreate
            conn.execute("""
                CREATE TABLE books_new (
                    id VARCHAR(100) PRIMARY KEY,
                    title VARCHAR(255) NOT NULL,
                    chapter VARCHAR(255),
                    url VARCHAR(500),
                    file_path VARCHAR(500),
                    duration FLOAT DEFAULT 0.0,
                    current_time FLOAT DEFAULT 0.0,
                    status VARCHAR(50) DEFAULT 'generating',
                    created_at FLOAT,
                    last_played_at FLOAT
                )
            """)
            
            conn.execute("""
                INSERT INTO books_new (id, title, chapter, url, file_path, duration, current_time, status, created_at, last_played_at)
                SELECT id, title, chapter, url, file_path, duration, current_time, status, created_at, last_played_at
                FROM books
            """)
            
            new_count = count_rows(conn, "books_new")
            assert new_count == book_count, f"Row count mismatch: {new_count} != {book_count}"
            
            conn.execute("DROP TABLE books")
            conn.execute("ALTER TABLE books_new RENAME TO books")
            print(f"   ✅ books: {new_count} rows preserved, user_id removed")
        else:
            print(f"   Would recreate 'books' without user_id ({book_count} rows)")

    # --- Migrate story_progress table ---
    if has_story_progress and has_user_id_sp:
        print("🔧 Migrating 'story_progress' table (removing user_id from PK)...")
        
        if not dry_run:
            conn.execute("""
                CREATE TABLE story_progress_new (
                    story_title VARCHAR(255) PRIMARY KEY,
                    last_chapter_num INTEGER DEFAULT 0,
                    last_chapter_file VARCHAR(255),
                    last_played_at FLOAT
                )
            """)
            
            # Keep only the latest progress per story (in case multiple users had progress)
            conn.execute("""
                INSERT OR REPLACE INTO story_progress_new (story_title, last_chapter_num, last_chapter_file, last_played_at)
                SELECT story_title, last_chapter_num, last_chapter_file, MAX(last_played_at)
                FROM story_progress
                GROUP BY story_title
            """)
            
            new_count = count_rows(conn, "story_progress_new")
            conn.execute("DROP TABLE story_progress")
            conn.execute("ALTER TABLE story_progress_new RENAME TO story_progress")
            print(f"   ✅ story_progress: {new_count} rows preserved (deduplicated by story_title)")
        else:
            print(f"   Would recreate 'story_progress' without user_id ({sp_count} rows)")

    # --- Drop users table ---
    if has_users:
        print("🔧 Dropping 'users' table...")
        if not dry_run:
            conn.execute("DROP TABLE users")
            print("   ✅ users table dropped")
        else:
            print("   Would drop 'users' table")

    # --- Commit ---
    if not dry_run:
        conn.commit()
        print()
        
        # --- Verify ---
        print("=== Post-migration verification ===")
        
        assert not table_exists(conn, "users"), "users table still exists!"
        print("  ✅ 'users' table removed")
        
        if has_books:
            cols = [c["name"] for c in get_table_info(conn, "books")]
            assert "user_id" not in cols, "user_id still in books!"
            final_count = count_rows(conn, "books")
            print(f"  ✅ books: {final_count} rows, no user_id")
        
        if has_story_progress:
            cols = [c["name"] for c in get_table_info(conn, "story_progress")]
            assert "user_id" not in cols, "user_id still in story_progress!"
            final_count = count_rows(conn, "story_progress")
            print(f"  ✅ story_progress: {final_count} rows, no user_id")
        
        # Integrity check
        result = conn.execute("PRAGMA integrity_check").fetchone()
        assert result[0] == "ok", f"Integrity check failed: {result[0]}"
        print("  ✅ Integrity check: OK")
    
    conn.close()
    print()
    print("✅ Migration complete!" if not dry_run else "✅ Dry run complete. No changes made.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate Jarvis DB from multi-user to single-user")
    parser.add_argument(
        "--db-path",
        default=os.path.join("data", "jarvis.db"),
        help="Path to jarvis.db (default: data/jarvis.db)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying the database",
    )
    args = parser.parse_args()
    migrate(args.db_path, args.dry_run)
