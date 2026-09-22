import json
import random
import re
import sqlite3
import ssl
from datetime import datetime
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

import certifi
from vkbottle import Keyboard, KeyboardButtonColor, Text
from vkbottle.bot import Bot, Message


# ============================================================
# НАСТРОЙКИ
# ============================================================

TOKEN = "vk1.a.7usV5r-QjE0Z0U9O728ldpPXNtvXv0iE7CmYzJ0G4O2VZ1m1Op2e3PFyx6rkLFaxn1kOtYPY7b8uGiD073aCRh3V8OX_b-HEbo8i1gy4rYhLblXwbYHHdBIuK5e-MH7TukOA3MG_tDZvROPBzgwl55rYn6TO0mElQPA5ga99r1hUNITQVNe0naTVzhhliJSc1a_EzA3xRg9QAjyFF2iASA"
GROUP_ID = 241570122
API_VERSION = "5.199"
DB_FILE = "lost_found.db"

# ID пользователей ВК, у которых есть доступ к админ-панели.
# Узнать свой user_id можно так: напишите боту что угодно —
# в консоли, где запущен bot.py, появится строка вида
# "📩 user_id=123456789, ...". Впишите это число сюда.
ADMIN_IDS = {775393018, 733863718}

# Простой пароль для получения админ-доступа.
# Для изменения пароля просто поменяйте значение в кавычках.
ADMIN_PASSWORD = "123456"

# Текст, который получает человек, когда его заявку на находку одобрили.
# Вещи выдаются очно, без переписки с нашедшим — поменяйте текст под
# свою реальную точку выдачи (вахта, стол находок и т.п.).
PICKUP_INSTRUCTIONS = (
    "Заберите вещь на вахте и покажите это уведомление."
)


# ============================================================
# БАЗА ДАННЫХ
# ============================================================

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS ads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            ad_type TEXT NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            place TEXT NOT NULL,
            date_text TEXT NOT NULL,
            photo_attachment TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            peer_id INTEGER PRIMARY KEY,
            state TEXT NOT NULL,
            data TEXT NOT NULL
        )
    """)

    # ------------------------------------------------------------
    # ЗАЯВКИ на найденные вещи: человек, увидевший в каталоге свою
    # потерянную вещь среди "найдено", подаёт заявку и доказывает,
    # что вещь его. Заявка уходит всем админам, они одобряют/отклоняют.
    # ------------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ad_id INTEGER NOT NULL,
            claimant_id INTEGER NOT NULL,
            proof_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            resolved_by INTEGER,
            resolved_at TEXT
        )
    """)

    # Миграция для баз, где таблица claims уже была создана раньше,
    # без поля под фото-доказательство.
    existing_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(claims)").fetchall()
    }

    if "photo_attachment" not in existing_columns:
        conn.execute("ALTER TABLE claims ADD COLUMN photo_attachment TEXT")

    conn.commit()

    # ------------------------------------------------------------
    # МИГРАЦИЯ: отдельный "видимый" номер объявления (number),
    # который не совпадает с внутренним id базы. Это позволяет
    # перенумеровывать объявления, ничего не удаляя и не ломая
    # (фото, автор, id в базе остаются прежними).
    # ------------------------------------------------------------
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(ads)").fetchall()]

    if "number" not in cols:
        conn.execute("ALTER TABLE ads ADD COLUMN number INTEGER")
        conn.commit()

    rows_without_number = conn.execute(
        "SELECT id FROM ads WHERE number IS NULL ORDER BY id ASC"
    ).fetchall()

    if rows_without_number:
        current_max = conn.execute(
            "SELECT COALESCE(MAX(number), 0) AS m FROM ads"
        ).fetchone()["m"]

        next_number = current_max + 1

        for row in rows_without_number:
            conn.execute(
                "UPDATE ads SET number = ? WHERE id = ?",
                (next_number, row["id"])
            )
            next_number += 1

        conn.commit()

    conn.close()


def create_ad(user_id, ad_type, category, title, description,
              place, date_text, photo_attachment):
    conn = get_db()

    next_number = conn.execute(
        "SELECT COALESCE(MAX(number), 0) AS m FROM ads"
    ).fetchone()["m"] + 1

    cur = conn.execute("""
        INSERT INTO ads (
            user_id, ad_type, category, title, description,
            place, date_text, photo_attachment, status, created_at, number
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
    """, (
        user_id,
        ad_type,
        category,
        title,
        description,
        place,
        date_text,
        photo_attachment,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        next_number
    ))

    ad_id = cur.lastrowid
    conn.commit()
    conn.close()

    return ad_id


def get_ad(ad_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM ads WHERE id = ?",
        (ad_id,)
    ).fetchone()
    conn.close()
    return row


def get_ad_by_number(number):
    """
    Ищет объявление по видимому номеру (#12), а не по внутреннему id.
    """
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM ads WHERE number = ?",
        (number,)
    ).fetchone()
    conn.close()
    return row


def get_active_ads(limit=100):
    conn = get_db()
    rows = conn.execute("""
        SELECT *
        FROM ads
        WHERE status = 'active'
        ORDER BY id DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return rows


def get_user_ads(user_id):
    conn = get_db()
    rows = conn.execute("""
        SELECT *
        FROM ads
        WHERE user_id = ?
        ORDER BY id DESC
    """, (user_id,)).fetchall()
    conn.close()
    return rows


def search_ads(keyword, category=None, ad_type=None):
    conn = get_db()

    conditions = ["status = 'active'"]
    params = []

    if category and category != "Все категории":
        conditions.append("category = ?")
        params.append(category)

    if ad_type and ad_type != "all":
        conditions.append("ad_type = ?")
        params.append(ad_type)

    if keyword and keyword.lower() != "все":
        like = f"%{keyword.lower()}%"
        conditions.append("""
            (
                LOWER(title) LIKE ?
                OR LOWER(description) LIKE ?
                OR LOWER(place) LIKE ?
                OR LOWER(category) LIKE ?
            )
        """)
        params.extend([like, like, like, like])

    query = f"""
        SELECT *
        FROM ads
        WHERE {' AND '.join(conditions)}
        ORDER BY id DESC
        LIMIT 100
    """

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def delete_ad(ad_id, user_id):
    conn = get_db()
    cur = conn.execute(
        "DELETE FROM ads WHERE id = ? AND user_id = ?",
        (ad_id, user_id)
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


def resolve_ad(ad_id, user_id):
    conn = get_db()
    cur = conn.execute(
        """
        UPDATE ads
        SET status = 'resolved'
        WHERE id = ? AND user_id = ?
        """,
        (ad_id, user_id)
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


# ============================================================
# ЗАЯВКИ НА НАЙДЕННЫЕ ВЕЩИ
# ============================================================

def create_claim(ad_id, claimant_id, proof_text, photo_attachment=None):
    conn = get_db()
    cur = conn.execute("""
        INSERT INTO claims (
            ad_id, claimant_id, proof_text, photo_attachment,
            status, created_at
        )
        VALUES (?, ?, ?, ?, 'pending', ?)
    """, (
        ad_id,
        claimant_id,
        proof_text,
        photo_attachment,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ))

    claim_id = cur.lastrowid
    conn.commit()
    conn.close()

    return claim_id


def get_claim(claim_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM claims WHERE id = ?",
        (claim_id,)
    ).fetchone()
    conn.close()
    return row


def get_pending_claims(limit=100):
    conn = get_db()
    rows = conn.execute("""
        SELECT *
        FROM claims
        WHERE status = 'pending'
        ORDER BY id ASC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return rows


def set_claim_status(claim_id, status, admin_id):
    conn = get_db()
    cur = conn.execute("""
        UPDATE claims
        SET status = ?, resolved_by = ?, resolved_at = ?
        WHERE id = ?
    """, (
        status,
        admin_id,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        claim_id
    ))
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


# ============================================================
# АДМИН-ФУНКЦИИ
# ============================================================

def is_admin(user_id):
    return int(user_id) in ADMIN_IDS


def admin_get_all_ads(limit=300):
    """
    Все объявления (и активные, и закрытые), для админ-панели.
    Активные — сверху, дальше по номеру.
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT *
        FROM ads
        ORDER BY (status = 'active') DESC, number ASC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return rows


def admin_delete_ad(ad_id):
    """
    Удаление объявления админом — без проверки автора.
    """
    conn = get_db()
    cur = conn.execute("DELETE FROM ads WHERE id = ?", (ad_id,))
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


def admin_delete_ads_by_numbers(numbers):
    """
    Удаляет сразу несколько объявлений по их видимым номерам (#N).
    Возвращает (список удалённых номеров, список номеров, которые
    не нашлись в базе).
    """
    conn = get_db()
    deleted = []
    not_found = []

    for number in numbers:
        row = conn.execute(
            "SELECT id FROM ads WHERE number = ?",
            (number,)
        ).fetchone()

        if row:
            conn.execute("DELETE FROM ads WHERE id = ?", (row["id"],))
            deleted.append(number)
        else:
            not_found.append(number)

    conn.commit()
    conn.close()

    return deleted, not_found


def admin_delete_all_ads():
    """
    Удаляет ВСЕ объявления без исключения (активные и закрытые).
    Только из админ-панели, с обязательным подтверждением.
    Возвращает количество удалённых объявлений.
    """
    conn = get_db()
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM ads"
    ).fetchone()["c"]
    conn.execute("DELETE FROM ads")
    conn.commit()
    conn.close()
    return count


def admin_resolve_ad(ad_id):
    """
    Закрывает объявление (статус 'resolved') без проверки автора.
    Используется, когда заявка на находку одобрена — вещь считается
    возвращённой, объявление больше не показывается в каталоге.
    """
    conn = get_db()
    cur = conn.execute(
        "UPDATE ads SET status = 'resolved' WHERE id = ?",
        (ad_id,)
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


def renumber_ads():
    """
    Перенумеровывает объявления: активные получают номера #1, #2, #3...
    подряд, в порядке их создания. Ничего не удаляется — ни объявления,
    ни фото, ни описания, ни авторы. Меняются только видимые номера.

    Закрытые объявления получают номера следом за активными
    (чтобы номера не повторялись), но пользователь их всё равно
    не видит в каталоге.

    Возвращает количество активных объявлений после перенумерации.
    """
    conn = get_db()

    active_ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM ads WHERE status = 'active' ORDER BY id ASC"
        ).fetchall()
    ]

    other_ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM ads WHERE status != 'active' ORDER BY id ASC"
        ).fetchall()
    ]

    number = 1

    for ad_id in active_ids:
        conn.execute("UPDATE ads SET number = ? WHERE id = ?", (number, ad_id))
        number += 1

    active_count = number - 1

    for ad_id in other_ids:
        conn.execute("UPDATE ads SET number = ? WHERE id = ?", (number, ad_id))
        number += 1

    conn.commit()
    conn.close()

    return active_count


# ============================================================
# СЕССИИ
# ============================================================

def get_session(peer_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM sessions WHERE peer_id = ?",
        (peer_id,)
    ).fetchone()
    conn.close()

    if not row:
        return None

    return {
        "state": row["state"],
        "data": json.loads(row["data"])
    }


def save_session(peer_id, state, data):
    conn = get_db()
    conn.execute("""
        INSERT INTO sessions (peer_id, state, data)
        VALUES (?, ?, ?)
        ON CONFLICT(peer_id)
        DO UPDATE SET state=excluded.state, data=excluded.data
    """, (
        peer_id,
        state,
        json.dumps(data, ensure_ascii=False)
    ))
    conn.commit()
    conn.close()


def clear_session(peer_id):
    conn = get_db()
    conn.execute(
        "DELETE FROM sessions WHERE peer_id = ?",
        (peer_id,)
    )
    conn.commit()
    conn.close()


# ============================================================
# КЛАВИАТУРЫ
# ============================================================

def main_keyboard(admin=False):
    kb = Keyboard(one_time=False, inline=False)

    if admin:
        kb.add(
            Text("📦 Разместить находку"),
            color=KeyboardButtonColor.POSITIVE
        ).row()

    kb.add(
        Text("📚 Объявления"),
        color=KeyboardButtonColor.PRIMARY
    ).add(
        Text("📢 Я потерял вещь"),
        color=KeyboardButtonColor.POSITIVE
    ).row().add(
        Text("📋 Мои объявления"),
        color=KeyboardButtonColor.SECONDARY
    ).row().add(
        Text("ℹ️ Помощь"),
        color=KeyboardButtonColor.SECONDARY
    )

    if admin:
        kb = kb.row().add(
            Text("⚙️ Админ-панель"),
            color=KeyboardButtonColor.SECONDARY
        )

    return kb.get_json()


def admin_keyboard():
    return (
        Keyboard(one_time=False, inline=False)
        .add(
            Text("📋 Все объявления"),
            color=KeyboardButtonColor.PRIMARY
        )
        .row()
        .add(
            Text("📝 Заявки на вещи"),
            color=KeyboardButtonColor.PRIMARY
        )
        .row()
        .add(
            Text("🔄 Перенумеровать объявления"),
            color=KeyboardButtonColor.PRIMARY
        )
        .row()
        .add(
            Text("🗑 Удалить объявления"),
            color=KeyboardButtonColor.NEGATIVE
        )
        .row()
        .add(
            Text("🏠 Главное меню"),
            color=KeyboardButtonColor.SECONDARY
        )
        .get_json()
    )


def admin_delete_menu_keyboard():
    return (
        Keyboard(one_time=False, inline=False)
        .add(
            Text("1️⃣ Одно"),
            color=KeyboardButtonColor.NEGATIVE
        )
        .add(
            Text("🔢 Несколько"),
            color=KeyboardButtonColor.NEGATIVE
        )
        .row()
        .add(
            Text("💥 Все"),
            color=KeyboardButtonColor.NEGATIVE
        )
        .row()
        .add(
            Text("🔙 Назад в админ-панель"),
            color=KeyboardButtonColor.SECONDARY
        )
        .get_json()
    )


def admin_confirm_keyboard():
    return (
        Keyboard(one_time=False, inline=False)
        .add(
            Text("✅ Подтвердить"),
            color=KeyboardButtonColor.POSITIVE
        )
        .add(
            Text("❌ Отмена"),
            color=KeyboardButtonColor.NEGATIVE
        )
        .get_json()
    )


def cancel_keyboard():
    return (
        Keyboard(one_time=False, inline=False)
        .add(
            Text("❌ Отмена"),
            color=KeyboardButtonColor.NEGATIVE
        )
        .get_json()
    )


def help_keyboard(admin=False):
    kb = Keyboard(one_time=False, inline=False)

    if not admin:
        kb.add(
            Text("🔐 Доступ сотрудника"),
            color=KeyboardButtonColor.SECONDARY
        ).row()

    kb.add(
        Text("🏠 Главное меню"),
        color=KeyboardButtonColor.SECONDARY
    )

    return kb.get_json()


def category_keyboard():
    return (
        Keyboard(one_time=False, inline=False)
        .add(
            Text("🎒 Одежда и аксессуары"),
            color=KeyboardButtonColor.PRIMARY
        )
        .add(
            Text("🎓 Школьная форма"),
            color=KeyboardButtonColor.PRIMARY
        )
        .row()
        .add(
            Text("📱 Телефоны и техника"),
            color=KeyboardButtonColor.PRIMARY
        )
        .add(
            Text("📚 Учебные вещи"),
            color=KeyboardButtonColor.PRIMARY
        )
        .row()
        .add(
            Text("🔑 Ключи и документы"),
            color=KeyboardButtonColor.PRIMARY
        )
        .add(
            Text("🧸 Другое"),
            color=KeyboardButtonColor.PRIMARY
        )
        .row()
        .add(
            Text("❌ Отмена"),
            color=KeyboardButtonColor.NEGATIVE
        )
        .get_json()
    )


def search_type_keyboard():
    return (
        Keyboard(one_time=False, inline=False)
        .add(
            Text("📦 Найденные вещи"),
            color=KeyboardButtonColor.PRIMARY
        )
        .row()
        .add(
            Text("📢 Розыск"),
            color=KeyboardButtonColor.PRIMARY
        )
        .row()
        .add(
            Text("❌ Отмена"),
            color=KeyboardButtonColor.NEGATIVE
        )
        .get_json()
    )


def search_category_keyboard():
    return (
        Keyboard(one_time=False, inline=False)
        .add(
            Text("🎒 Одежда и аксессуары"),
            color=KeyboardButtonColor.PRIMARY
        )
        .add(
            Text("🎓 Школьная форма"),
            color=KeyboardButtonColor.PRIMARY
        )
        .row()
        .add(
            Text("📱 Телефоны и техника"),
            color=KeyboardButtonColor.PRIMARY
        )
        .add(
            Text("📚 Учебные вещи"),
            color=KeyboardButtonColor.PRIMARY
        )
        .row()
        .add(
            Text("🔑 Ключи и документы"),
            color=KeyboardButtonColor.PRIMARY
        )
        .add(
            Text("🧸 Другое"),
            color=KeyboardButtonColor.PRIMARY
        )
        .row()
        .add(
            Text("🔍 Поиск по слову"),
            color=KeyboardButtonColor.POSITIVE
        )
        .row()
        .add(
            Text("📖 Все объявления"),
            color=KeyboardButtonColor.POSITIVE
        )
        .row()
        .add(
            Text("❌ Отмена"),
            color=KeyboardButtonColor.NEGATIVE
        )
        .get_json()
    )


def photo_keyboard():
    return (
        Keyboard(one_time=False, inline=False)
        .add(
            Text("⏭ Пропустить фото"),
            color=KeyboardButtonColor.SECONDARY
        )
        .row()
        .add(
            Text("❌ Отмена"),
            color=KeyboardButtonColor.NEGATIVE
        )
        .get_json()
    )


def confirm_keyboard():
    return (
        Keyboard(one_time=False, inline=False)
        .add(
            Text("✅ Опубликовать"),
            color=KeyboardButtonColor.POSITIVE
        )
        .add(
            Text("✏️ Изменить"),
            color=KeyboardButtonColor.PRIMARY
        )
        .row()
        .add(
            Text("❌ Отмена"),
            color=KeyboardButtonColor.NEGATIVE
        )
        .get_json()
    )


def back_keyboard():
    return (
        Keyboard(one_time=False, inline=False)
        .add(
            Text("🏠 Главное меню"),
            color=KeyboardButtonColor.SECONDARY
        )
        .get_json()
    )


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

CATEGORY_MAP = {
    "🎒 одежда и аксессуары": "Одежда и аксессуары",
    "🎓 школьная форма": "Школьная форма",
    "📱 телефоны и техника": "Телефоны и техника",
    "📚 учебные вещи": "Учебные вещи",
    "🔑 ключи и документы": "Ключи и документы",
    "🧸 другое": "Другое",
}

# Подписи для заголовков списков, когда показываем вещи только
# одного типа (найденные или те, что кто-то разыскивает).
TYPE_LABELS = {
    "found": "НАЙДЕНО",
    "lost": "В РОЗЫСКЕ",
}


def normalize(text):
    return (text or "").strip().lower()


def short_text(text, size=60):
    text = " ".join((text or "").split())
    if len(text) <= size:
        return text
    return text[:size - 1] + "…"


def user_id_from_message(message):
    return int(getattr(message, "from_id", 0) or 0)


def extract_photo_attachment(message):
    """
    Сохраняет attachment вида photoOWNER_ID_PHOTO_ID[_ACCESS_KEY].
    Это позволяет отправлять фото обратно при просмотре объявления.
    """
    attachments = getattr(message, "attachments", None) or []

    for item in attachments:
        try:
            item_type = getattr(item, "type", None)

            if item_type != "photo":
                continue

            photo = getattr(item, "photo", None)

            if photo is None:
                continue

            owner_id = getattr(photo, "owner_id", None)
            photo_id = getattr(photo, "id", None)
            access_key = getattr(photo, "access_key", None)

            if owner_id is None or photo_id is None:
                continue

            value = f"photo{owner_id}_{photo_id}"

            if access_key:
                value += f"_{access_key}"

            return value

        except Exception as exc:
            print(f"Ошибка разбора фото: {exc}")

    return None


def combine_attachments(*attachments):
    """
    Склеивает несколько attachment-строк в одну через запятую —
    так VK API вкладывает сразу несколько фото в одно сообщение.
    Пустые/None-значения пропускаются.
    """
    return ",".join(a for a in attachments if a)


def format_ad(ad, show_author=False):
    if ad["ad_type"] == "lost":
        kind = "📢 ПОТЕРЯНО"
    else:
        kind = "📦 НАЙДЕНО"

    status = "🟢 Активно" if ad["status"] == "active" else "✅ Закрыто"

    lines = [
        kind,
        f"#{ad['number']} — {ad['title']}",
        "",
        f"📂 Категория: {ad['category']}",
        f"📍 Место: {ad['place']}",
        f"📅 Дата: {ad['date_text']}",
        f"📝 Описание: {ad['description']}",
    ]

    if show_author:
        lines.append("")
        lines.append(f"👤 Автор: [id{ad['user_id']}|профиль]")

    lines.append("")
    lines.append(status)

    return "\n".join(lines)


def catalog_text(rows, title="📚 КАТАЛОГ ВЕЩЕЙ", empty_text=None):
    """
    Каталог: одна строка на вещь — номер + тип + краткое описание.
    """
    if not rows:
        return (
            f"{title}\n\n"
            f"{empty_text or 'Сейчас активных объявлений нет.'}"
        )

    lines = [
        title,
        "",
        "Активные объявления:",
        ""
    ]

    for ad in rows:
        icon = "📢" if ad["ad_type"] == "lost" else "📦"

        lines.append(
            f"#{ad['number']} {icon} {short_text(ad['title'], 40)}"
        )
        lines.append(
            f"   {short_text(ad['description'], 75)}"
        )
        lines.append(
            f"   📍 {short_text(ad['place'], 45)}"
        )
        lines.append("")

    lines.append(
        "Чтобы открыть полную карточку, отправьте номер, "
        "например: #12"
    )

    return "\n".join(lines)


def admin_catalog_text(rows):
    """
    Список ВСЕХ объявлений (активных и закрытых) для админ-панели —
    с номером, статусом и автором.
    """
    if not rows:
        return "📋 Объявлений пока нет."

    lines = ["📋 ВСЕ ОБЪЯВЛЕНИЯ", ""]

    for ad in rows:
        icon = "📢" if ad["ad_type"] == "lost" else "📦"
        status = "🟢 активно" if ad["status"] == "active" else "✅ закрыто"

        lines.append(
            f"#{ad['number']} {icon} {short_text(ad['title'], 40)} — {status}"
        )
        lines.append(f"   автор: [id{ad['user_id']}|профиль]")
        lines.append("")

    lines.append(
        "Чтобы удалить объявление, откройте «🗑 Удалить объявления» "
        "→ «1️⃣ Одно» и пришлите его номер."
    )

    return "\n".join(lines)


def format_claim_for_admin(claim, ad):
    if ad:
        ad_line = f"#{ad['number']} — {ad['title']}"
        finder_line = f"📦 Нашёл вещь: [id{ad['user_id']}|профиль]"
    else:
        ad_line = "(объявление удалено)"
        finder_line = ""

    status_map = {
        "pending": "🕓 на рассмотрении",
        "approved": "✅ одобрена",
        "rejected": "❌ отклонена",
    }

    lines = [
        f"📝 ЗАЯВКА №{claim['id']}",
        "",
        f"Объявление: {ad_line}",
    ]

    if finder_line:
        lines.append(finder_line)

    lines.extend([
        "",
        f"Заявитель: [id{claim['claimant_id']}|профиль]",
        "",
        "📄 Доказательства:",
        claim["proof_text"],
    ])

    if claim["photo_attachment"]:
        lines.append("")
        lines.append(
            "📷 Заявитель приложил своё фото вещи (доп. доказательство) "
            "— прикреплено к этому сообщению."
        )

    lines.extend([
        "",
        f"Статус: {status_map.get(claim['status'], claim['status'])}",
    ])

    return "\n".join(lines)


def admin_claims_text(rows):
    if not rows:
        return "📝 Заявок на рассмотрении нет."

    lines = ["📝 ЗАЯВКИ НА РАССМОТРЕНИИ", ""]

    for c in rows:
        ad = get_ad(c["ad_id"])
        ad_label = f"#{ad['number']}" if ad else "объявление удалено"
        photo_mark = " 📷" if c["photo_attachment"] else ""

        lines.append(f"№{c['id']} — по {ad_label}{photo_mark}")
        lines.append(f"   заявитель: id{c['claimant_id']}")
        lines.append("")

    lines.append("Чтобы открыть заявку, отправьте: заявка 5")

    return "\n".join(lines)


def search_result_text(rows, title="🔎 РЕЗУЛЬТАТЫ ПОИСКА"):
    if not rows:
        return "🔎 Ничего не найдено."

    lines = [title, ""]

    for ad in rows:
        icon = "📢" if ad["ad_type"] == "lost" else "📦"

        lines.append(
            f"#{ad['number']} {icon} {short_text(ad['title'], 45)}"
        )
        lines.append(
            f"   {short_text(ad['description'], 70)}"
        )
        lines.append(
            f"   📍 {short_text(ad['place'], 45)}"
        )
        lines.append("")

    lines.append("Откройте объявление, отправив его номер: #12")

    return "\n".join(lines)


def owner_keyboard():
    return (
        Keyboard(one_time=False, inline=False)
        .add(
            Text("✅ Вещь найдена"),
            color=KeyboardButtonColor.POSITIVE
        )
        .add(
            Text("🗑 Удалить объявление"),
            color=KeyboardButtonColor.NEGATIVE
        )
        .row()
        .add(
            Text("🏠 Главное меню"),
            color=KeyboardButtonColor.SECONDARY
        )
        .get_json()
    )


def visitor_keyboard():
    """
    Клавиатура для чужого объявления о ПОТЕРЕ: можно написать автору
    напрямую (это применимо, если вещь потеряли — с находкой всё
    решается через заявку, см. visitor_found_keyboard).
    """
    return (
        Keyboard(one_time=False, inline=False)
        .add(
            Text("✉️ Написать автору"),
            color=KeyboardButtonColor.PRIMARY
        )
        .row()
        .add(
            Text("🏠 Главное меню"),
            color=KeyboardButtonColor.SECONDARY
        )
        .get_json()
    )


def visitor_found_keyboard():
    """
    Клавиатура для чужого объявления о НАХОДКЕ: можно написать автору
    напрямую или подать заявку админам, доказав, что вещь — своя.
    """
    return (
        Keyboard(one_time=False, inline=False)
        .add(
            Text("✉️ Написать автору"),
            color=KeyboardButtonColor.PRIMARY
        )
        .row()
        .add(
            Text("📝 Это моя вещь"),
            color=KeyboardButtonColor.POSITIVE
        )
        .row()
        .add(
            Text("🏠 Главное меню"),
            color=KeyboardButtonColor.SECONDARY
        )
        .get_json()
    )


def claim_only_keyboard():
    """
    Как visitor_found_keyboard, но без прямой связи с автором
    (используется, если автор объявления — админ).
    """
    return (
        Keyboard(one_time=False, inline=False)
        .add(
            Text("📝 Это моя вещь"),
            color=KeyboardButtonColor.POSITIVE
        )
        .row()
        .add(
            Text("🏠 Главное меню"),
            color=KeyboardButtonColor.SECONDARY
        )
        .get_json()
    )


def admin_lost_ad_keyboard():
    """
    Клавиатура для админа на чужом объявлении о ПОТЕРЕ: можно
    написать автору или отметить вещь найденной, если админ
    физически нашёл её (например, лежит на вахте/в коридоре).
    """
    return (
        Keyboard(one_time=False, inline=False)
        .add(
            Text("✉️ Написать автору"),
            color=KeyboardButtonColor.PRIMARY
        )
        .row()
        .add(
            Text("🎯 Эта вещь нашлась"),
            color=KeyboardButtonColor.POSITIVE
        )
        .row()
        .add(
            Text("🏠 Главное меню"),
            color=KeyboardButtonColor.SECONDARY
        )
        .get_json()
    )


def claim_admin_keyboard():
    return (
        Keyboard(one_time=False, inline=False)
        .add(
            Text("✅ Одобрить"),
            color=KeyboardButtonColor.POSITIVE
        )
        .add(
            Text("❌ Отклонить"),
            color=KeyboardButtonColor.NEGATIVE
        )
        .row()
        .add(
            Text("🏠 Главное меню"),
            color=KeyboardButtonColor.SECONDARY
        )
        .get_json()
    )


# ============================================================
# VK API — АВТОМАТИЧЕСКО ВКЛЮЧАЕМ LONG POLL
# ============================================================

SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def vk_api(method, params):
    payload = dict(params)
    payload["access_token"] = TOKEN
    payload["v"] = API_VERSION

    url = (
        f"https://api.vk.com/method/{method}?"
        f"{urlencode(payload)}"
    )

    with urlopen(
        url,
        timeout=15,
        context=SSL_CONTEXT
    ) as response:
        return json.loads(response.read().decode("utf-8"))


def notify_admins(text, attachment=None):
    """
    Рассылает текст личным сообщением каждому админу из ADMIN_IDS.
    Так все админы видят каждую новую заявку, без настройки
    отдельного чата.
    """
    for admin_id in ADMIN_IDS:
        try:
            params = {
                "peer_id": admin_id,
                "message": text,
                "random_id": random.randint(1, 2_147_483_647),
            }

            if attachment:
                params["attachment"] = attachment

            vk_api("messages.send", params)
        except Exception as exc:
            print(f"⚠️ Не удалось уведомить админа {admin_id}: {exc}")


def notify_user(user_id, text):
    try:
        vk_api("messages.send", {
            "peer_id": user_id,
            "message": text,
            "random_id": random.randint(1, 2_147_483_647),
        })
    except Exception as exc:
        print(f"⚠️ Не удалось отправить сообщение {user_id}: {exc}")


def diagnose_connection_error(exc):
    print("\n❌ НЕ УДАЁТСЯ ПОДКЛЮЧИТЬСЯ К VK (api.vk.com)\n")
    print(f"Техническая причина: {exc}\n")
    print(
        "Это не ошибка в коде бота — компьютер не смог физически "
        "достучаться до серверов VK. Обычно причина одна из:\n\n"
        "1) VK заблокирован в этой сети. Школьный/рабочий Wi-Fi часто "
        "блокирует соцсети. Проверка: включите на телефоне режим "
        "модема, подключите к нему компьютер и запустите бота ещё "
        "раз — если заработает, дело в сети.\n\n"
        "2) В Windows включён прокси-сервер, который сейчас не "
        "отвечает. Параметры → Сеть и Интернет → Прокси-сервер — "
        "отключите «Использовать прокси-сервер» и «Автоматическое "
        "определение параметров», если они включены.\n\n"
        "3) Антивирус или файрвол блокирует Python (Kaspersky, ESET, "
        "Windows Defender и т.п.). Временно отключите проверку "
        "HTTPS-трафика/файрвол и попробуйте снова.\n\n"
        "4) Файл hosts подменяет адрес api.vk.com (иногда так делают "
        "блокировщики рекламы/родительский контроль). Откройте от "
        "имени администратора "
        "C:\\Windows\\System32\\drivers\\etc\\hosts и проверьте, нет "
        "ли там строки с vk.com.\n\n"
        "Быстрая проверка: откройте в браузере на этом же компьютере\n"
        "https://api.vk.com/method/utils.getServerTime\n"
        "Если страница тоже не открывается — проблема точно в сети "
        "или компьютере, а не в самом боте."
    )


def setup_longpoll():
    print("Проверяем настройки VK...")

    result = vk_api(
        "groups.setLongPollSettings",
        {
            "group_id": GROUP_ID,
            "enabled": 1,
            "api_version": API_VERSION,
            "message_new": 1
        }
    )

    if "error" in result:
        print("❌ Ошибка VK:")
        print(result["error"])
        return False

    print("✅ Long Poll включён")
    print("✅ message_new включён")
    return True


# ============================================================
# BOT
# ============================================================

bot = Bot(TOKEN)


async def send_claim_card(message, peer_id, claim_id):
    """
    Показывает админу карточку заявки №claim_id и переводит его
    в состояние admin_view_claim (одобрить/отклонить).
    """
    claim = get_claim(claim_id)

    if not claim:
        await message.answer(
            "❌ Заявка с таким номером не найдена.",
            keyboard=admin_keyboard()
        )
        return

    ad = get_ad(claim["ad_id"])

    save_session(
        peer_id,
        "admin_view_claim",
        {"claim_id": claim["id"]}
    )

    card_text = format_claim_for_admin(claim, ad)

    attachments = combine_attachments(
        ad["photo_attachment"] if ad else None,
        claim["photo_attachment"]
    )

    if attachments:
        await message.answer(
            card_text,
            attachment=attachments,
            keyboard=claim_admin_keyboard()
        )
    else:
        await message.answer(
            card_text,
            keyboard=claim_admin_keyboard()
        )


def pick_viewer_keyboard(ad, user_id):
    """
    Выбирает клавиатуру для карточки объявления в зависимости от
    того, кто смотрит: сам автор, админ (в т.ч. на чужой «потере» —
    там доступна кнопка «нашлась»), обычный человек и т.д.
    """
    is_owner = ad["user_id"] == user_id
    contact_hidden = is_admin(ad["user_id"]) and not is_owner
    is_found = ad["ad_type"] == "found"
    is_lost = ad["ad_type"] == "lost"

    if is_owner:
        return owner_keyboard()

    if is_admin(user_id) and is_lost and ad["status"] == "active":
        return admin_lost_ad_keyboard()

    if contact_hidden:
        return claim_only_keyboard() if is_found else back_keyboard()

    if is_found:
        return visitor_found_keyboard()

    return visitor_keyboard()


async def guard_found_only_admin(message, peer_id, session, user_id):
    """
    Проверка на каждом шаге создания объявления: если это объявление
    о НАХОДКЕ, а пользователь не админ — обрывает сценарий. Нужна как
    подстраховка на случай «зависшей» сессии (например, человек начал
    создавать объявление, а потом его лишили прав администратора, или
    сессия осталась с прошлой версии бота). Объявления о ПОТЕРЕ
    может создавать кто угодно — их эта проверка не трогает.
    """
    if session["data"].get("ad_type") == "found" and not is_admin(user_id):
        clear_session(peer_id)
        await message.answer(
            "📦 Добавлять находки может только администратор.",
            keyboard=main_keyboard(False)
        )
        return True

    return False


async def open_ad_card(message, peer_id, user_id, number):
    """
    Открывает карточку объявления по видимому номеру (#N) для user_id
    и переводит диалог в состояние view_ad. Используется и из общего
    сценария, и из админ-панели (например, сразу после «Все
    объявления»), чтобы номер открывался в любом месте.
    """
    ad = get_ad_by_number(number)

    if not ad:
        await message.answer(
            "❌ Такое объявление не найдено.",
            keyboard=main_keyboard(is_admin(user_id))
        )
        return

    viewer_keyboard = pick_viewer_keyboard(ad, user_id)

    card_text = format_ad(ad, show_author=is_admin(user_id))

    if ad["photo_attachment"]:
        await message.answer(
            card_text,
            attachment=ad["photo_attachment"],
            keyboard=viewer_keyboard
        )
    else:
        await message.answer(
            card_text,
            keyboard=viewer_keyboard
        )

    save_session(peer_id, "view_ad", {"ad_id": ad["id"]})


@bot.on.message()
async def handler(message: Message):
    peer_id = int(message.peer_id)
    user_id = user_id_from_message(message)

    raw_text = message.text or ""
    text = normalize(raw_text)

    print(
        f"📩 user_id={user_id}, peer_id={peer_id}, "
        f"text={raw_text!r}"
    )

    # --------------------------------------------------------
    # ОТМЕНА
    # --------------------------------------------------------

    if text in {"отмена", "❌ отмена"}:
        clear_session(peer_id)

        await message.answer(
            "❌ Действие отменено.",
            keyboard=main_keyboard(is_admin(user_id))
        )
        return

    # --------------------------------------------------------
    # ГЛАВНОЕ МЕНЮ
    # --------------------------------------------------------

    if text in {
        "начать",
        "start",
        "/start",
        "привет",
        "🏠 главное меню"
    }:
        clear_session(peer_id)

        await message.answer(
            "🔎 БЮРО НАХОДОК\n\n"
            "Здесь можно посмотреть каталог потерянных вещей, "
            "найти нужное объявление или добавить свою вещь.\n\n"
            "Выберите раздел:",
            keyboard=main_keyboard(is_admin(user_id))
        )
        return

    # --------------------------------------------------------
    # ПОЛУЧЕНИЕ АДМИН-ДОСТУПА ПО ПАРОЛЮ
    # --------------------------------------------------------

    if text == "🔐 доступ сотрудника":
        if is_admin(user_id):
            await message.answer(
                "✅ У вас уже есть доступ администратора.",
                keyboard=main_keyboard(True)
            )
            return

        save_session(peer_id, "admin_password", {})
        await message.answer(
            "🔐 ДОСТУП СОТРУДНИКА\n\n"
            "Введите пароль для получения доступа администратора.",
            keyboard=cancel_keyboard()
        )
        return

    session = get_session(peer_id)

    if session and session["state"] == "admin_password":
        entered_password = raw_text.strip()

        if entered_password == ADMIN_PASSWORD:
            ADMIN_IDS.add(user_id)
            clear_session(peer_id)

            await message.answer(
                "✅ Пароль верный!\n\n"
                "Вам предоставлен доступ администратора.\n"
                "Теперь в главном меню появилась админ-панель.",
                keyboard=main_keyboard(True)
            )
            return

        await message.answer(
            "❌ Неверный пароль.\n\n"
            "Попробуйте ещё раз или нажмите «❌ Отмена».",
            keyboard=cancel_keyboard()
        )
        return

    # --------------------------------------------------------
    # ПОИСК
    # --------------------------------------------------------

    if text == "📚 объявления":
        save_session(peer_id, "search_type", {})

        await message.answer(
            "📚 ОБЪЯВЛЕНИЯ\n\n"
            "Что вы хотите посмотреть?\n\n"
            "📦 Найденные вещи — то, что уже нашли и ждут владельца.\n"
            "📢 Розыск — объявления тех, кто ищет свою потерянную вещь.",
            keyboard=search_type_keyboard()
        )
        return

    if text in {"📦 найденные вещи", "📢 розыск"}:
        session = get_session(peer_id)

        if session and session["state"] == "search_type":
            ad_type = "found" if "найденные" in text else "lost"

            save_session(
                peer_id,
                "search_category",
                {"ad_type": ad_type}
            )

            type_label = TYPE_LABELS[ad_type]

            await message.answer(
                f"🔎 {type_label}\n\n"
                "Выберите категорию — сразу покажу все вещи в ней.\n"
                "Или воспользуйтесь «🔍 Поиск по слову», чтобы найти "
                "конкретную вещь по названию/описанию среди всех "
                "категорий.",
                keyboard=search_category_keyboard()
            )
            return

    # --------------------------------------------------------
    # КАТАЛОГ ПО ВЫБРАННОМУ ТИПУ (кнопка внутри «📚 Объявления»)
    # --------------------------------------------------------

    if text == "📖 все объявления":
        session = get_session(peer_id)
        ad_type = None

        if session and session.get("data"):
            ad_type = session["data"].get("ad_type")

        clear_session(peer_id)

        rows = search_ads(keyword="", category=None, ad_type=ad_type)

        type_label = TYPE_LABELS.get(ad_type)
        title = f"📚 ВСЕ: {type_label}" if type_label else "📚 КАТАЛОГ ВЕЩЕЙ"

        await message.answer(
            catalog_text(rows, title=title),
            keyboard=back_keyboard()
        )
        return

    if text in CATEGORY_MAP and get_session(peer_id) and \
            get_session(peer_id)["state"] == "search_category":

        category = CATEGORY_MAP[text]
        session = get_session(peer_id)
        ad_type = session["data"].get("ad_type")
        clear_session(peer_id)

        rows = search_ads(keyword="", category=category, ad_type=ad_type)

        type_label = TYPE_LABELS.get(ad_type)
        title = f"📂 {category.upper()}"

        if type_label:
            title += f" — {type_label}"

        await message.answer(
            search_result_text(rows, title=title),
            keyboard=back_keyboard()
        )
        return

    if text == "🔍 поиск по слову":
        session = get_session(peer_id)

        if session and session["state"] == "search_category":
            ad_type = session["data"].get("ad_type")

            save_session(
                peer_id,
                "search_keyword",
                {"category": "Все категории", "ad_type": ad_type}
            )

            await message.answer(
                "🔍 ПОИСК ПО СЛОВУ\n\n"
                "Введите слово — поищу среди всех объявлений и "
                "категорий (по названию, описанию, месту находки).\n\n"
                "Например: рюкзак, телефон, ключи.",
                keyboard=cancel_keyboard()
            )
            return

    session = get_session(peer_id)

    # --------------------------------------------------------
    # ПОИСК ПО СЛОВУ
    # --------------------------------------------------------

    if session and session["state"] == "search_keyword":
        keyword = raw_text.strip()

        category = session["data"].get(
            "category",
            "Все категории"
        )
        ad_type = session["data"].get("ad_type")

        rows = search_ads(
            keyword=keyword,
            category=category,
            ad_type=ad_type
        )

        clear_session(peer_id)

        await message.answer(
            search_result_text(rows),
            keyboard=back_keyboard()
        )
        return

    # --------------------------------------------------------
    # СОЗДАНИЕ ОБЪЯВЛЕНИЯ — КАТЕГОРИЯ
    # --------------------------------------------------------

    if session and session["state"] == "create_category":
        if await guard_found_only_admin(message, peer_id, session, user_id):
            return

        if text not in CATEGORY_MAP:
            await message.answer(
                "Выберите категорию кнопкой:",
                keyboard=category_keyboard()
            )
            return

        data = session["data"]
        data["category"] = CATEGORY_MAP[text]

        save_session(
            peer_id,
            "create_title",
            data
        )

        await message.answer(
            "✏️ Напишите название вещи.\n\n"
            "Например: «Чёрный рюкзак Nike».",
            keyboard=cancel_keyboard()
        )
        return

    # --------------------------------------------------------
    # СОЗДАНИЕ ОБЪЯВЛЕНИЯ — НАЗВАНИЕ
    # --------------------------------------------------------

    if session and session["state"] == "create_title":
        if await guard_found_only_admin(message, peer_id, session, user_id):
            return

        title = raw_text.strip()

        if len(title) < 3:
            await message.answer(
                "Название слишком короткое."
            )
            return

        if len(title) > 120:
            await message.answer(
                "Название должно быть не длиннее 120 символов."
            )
            return

        data = session["data"]
        data["title"] = title

        save_session(
            peer_id,
            "create_description",
            data
        )

        await message.answer(
            "📝 Теперь опишите вещь.\n\n"
            "Например: цвет, марка, особые приметы.",
            keyboard=cancel_keyboard()
        )
        return

    # --------------------------------------------------------
    # СОЗДАНИЕ ОБЪЯВЛЕНИЯ — ОПИСАНИЕ
    # --------------------------------------------------------

    if session and session["state"] == "create_description":
        if await guard_found_only_admin(message, peer_id, session, user_id):
            return

        description = raw_text.strip()

        if len(description) < 5:
            await message.answer(
                "Описание слишком короткое."
            )
            return

        if len(description) > 1500:
            await message.answer(
                "Описание должно быть не длиннее 1500 символов."
            )
            return

        data = session["data"]
        data["description"] = description

        save_session(
            peer_id,
            "create_place",
            data
        )

        await message.answer(
            "📍 Где вещь была потеряна или найдена?\n\n"
            "Например: «2 этаж, кабинет 214».",
            keyboard=cancel_keyboard()
        )
        return

    # --------------------------------------------------------
    # СОЗДАНИЕ ОБЪЯВЛЕНИЯ — МЕСТО
    # --------------------------------------------------------

    if session and session["state"] == "create_place":
        if await guard_found_only_admin(message, peer_id, session, user_id):
            return

        place = raw_text.strip()

        if len(place) < 2:
            await message.answer("Укажите место.")
            return

        if len(place) > 200:
            await message.answer(
                "Место должно быть не длиннее 200 символов."
            )
            return

        data = session["data"]
        data["place"] = place

        save_session(
            peer_id,
            "create_date",
            data
        )

        await message.answer(
            "📅 Когда вещь была потеряна или найдена?\n\n"
            "Можно написать примерно: «17 сентября после 5 урока».",
            keyboard=cancel_keyboard()
        )
        return

    # --------------------------------------------------------
    # СОЗДАНИЕ ОБЪЯВЛЕНИЯ — ДАТА
    # --------------------------------------------------------

    if session and session["state"] == "create_date":
        if await guard_found_only_admin(message, peer_id, session, user_id):
            return

        date_text = raw_text.strip()

        if len(date_text) < 2:
            await message.answer("Укажите дату.")
            return

        if len(date_text) > 100:
            await message.answer(
                "Дата/время слишком длинные."
            )
            return

        data = session["data"]
        data["date_text"] = date_text

        save_session(
            peer_id,
            "create_photo",
            data
        )

        await message.answer(
            "📷 Пришлите фотографию вещи.\n\n"
            "Фото можно пропустить кнопкой ниже.",
            keyboard=photo_keyboard()
        )
        return

    # --------------------------------------------------------
    # СОЗДАНИЕ ОБЪЯВЛЕНИЯ — ФОТО
    # --------------------------------------------------------

    if session and session["state"] == "create_photo":
        if await guard_found_only_admin(message, peer_id, session, user_id):
            return

        data = session["data"]

        if text in {"пропустить фото", "⏭ пропустить фото"}:
            photo_attachment = None
        else:
            photo_attachment = extract_photo_attachment(message)

            if not photo_attachment:
                await message.answer(
                    "📷 Я не вижу фото.\n\n"
                    "Пришлите именно фотографию сообщением "
                    "или нажмите «Пропустить фото».",
                    keyboard=photo_keyboard()
                )
                return

        data["photo_attachment"] = photo_attachment

        save_session(
            peer_id,
            "create_confirm",
            data
        )

        kind = (
            "📢 ПОТЕРЯНО"
            if data["ad_type"] == "lost"
            else "📦 НАЙДЕНО"
        )

        photo_text = (
            "📷 Фото добавлено"
            if photo_attachment
            else "📷 Без фото"
        )

        await message.answer(
            "📋 ПРОВЕРЬТЕ ОБЪЯВЛЕНИЕ\n\n"
            f"{kind}\n"
            f"📂 {data['category']}\n"
            f"🏷 {data['title']}\n"
            f"📝 {data['description']}\n"
            f"📍 {data['place']}\n"
            f"📅 {data['date_text']}\n"
            f"{photo_text}\n\n"
            "Опубликовать?",
            keyboard=confirm_keyboard()
        )
        return

    # --------------------------------------------------------
    # СОЗДАНИЕ ОБЪЯВЛЕНИЯ — ПУБЛИКАЦИЯ
    # --------------------------------------------------------

    if session and session["state"] == "create_confirm":
        if await guard_found_only_admin(message, peer_id, session, user_id):
            return

        if text == "✅ опубликовать":
            data = session["data"]

            ad_id = create_ad(
                user_id=user_id,
                ad_type=data["ad_type"],
                category=data["category"],
                title=data["title"],
                description=data["description"],
                place=data["place"],
                date_text=data["date_text"],
                photo_attachment=data.get("photo_attachment")
            )

            ad = get_ad(ad_id)
            clear_session(peer_id)

            if ad["photo_attachment"]:
                await message.answer(
                    format_ad(ad),
                    attachment=ad["photo_attachment"],
                    keyboard=owner_keyboard()
                )
            else:
                await message.answer(
                    format_ad(ad),
                    keyboard=owner_keyboard()
                )

            await message.answer(
                f"✅ Объявление #{ad['number']} опубликовано.\n\n"
                "Оно уже появилось в каталоге вещей.",
                keyboard=main_keyboard(is_admin(user_id))
            )
            return

        if text == "✏️ изменить":
            data = session["data"]

            save_session(
                peer_id,
                "create_category",
                data
            )

            await message.answer(
                "Выберите категорию заново:",
                keyboard=category_keyboard()
            )
            return

        await message.answer(
            "Нажмите «Опубликовать», «Изменить» или «Отмена».",
            keyboard=confirm_keyboard()
        )
        return

    # --------------------------------------------------------
    # НАЧАЛО СОЗДАНИЯ ПОТЕРЯННОЙ ВЕЩИ
    # --------------------------------------------------------

    if text == "📢 я потерял вещь":
        save_session(
            peer_id,
            "create_category",
            {"ad_type": "lost"}
        )

        await message.answer(
            "📢 СОЗДАНИЕ ОБЪЯВЛЕНИЯ О ПОТЕРЕ\n\n"
            "Выберите категорию:",
            keyboard=category_keyboard()
        )
        return

    # --------------------------------------------------------
    # НАЧАЛО СОЗДАНИЯ НАЙДЕННОЙ ВЕЩИ
    # --------------------------------------------------------

    if text == "📦 разместить находку":
        if not is_admin(user_id):
            await message.answer(
                "📦 Добавлять находки может только администратор.\n\n"
                "Если вы нашли чужую вещь, передайте её на вахту — "
                "администратор сам разместит объявление.",
                keyboard=main_keyboard(False)
            )
            return

        save_session(
            peer_id,
            "create_category",
            {"ad_type": "found"}
        )

        await message.answer(
            "📦 СОЗДАНИЕ ОБЪЯВЛЕНИЯ О НАХОДКЕ\n\n"
            "Выберите категорию:",
            keyboard=category_keyboard()
        )
        return

    # --------------------------------------------------------
    # МОИ ОБЪЯВЛЕНИЯ
    # --------------------------------------------------------

    if text == "📋 мои объявления":
        rows = get_user_ads(user_id)

        if not rows:
            await message.answer(
                "📋 У вас пока нет объявлений.",
                keyboard=main_keyboard(is_admin(user_id))
            )
            return

        lines = ["📋 МОИ ОБЪЯВЛЕНИЯ", ""]

        for ad in rows:
            icon = "📢" if ad["ad_type"] == "lost" else "📦"
            status = "🟢 активно" if ad["status"] == "active" else "✅ закрыто"

            lines.append(
                f"#{ad['id']} {icon} {short_text(ad['title'], 45)} — {status}"
            )

        lines.append("")
        lines.append("Откройте объявление: #12")

        await message.answer(
            "\n".join(lines),
            keyboard=back_keyboard()
        )
        return

    # --------------------------------------------------------
    # АДМИН-ПАНЕЛЬ — ВХОД
    # --------------------------------------------------------

    if text == "⚙️ админ-панель":
        if not is_admin(user_id):
            await message.answer(
                "🤔 Не понял команду.\n\nОткройте главное меню:",
                keyboard=main_keyboard(False)
            )
            return

        save_session(peer_id, "admin_menu", {})

        await message.answer(
            "⚙️ АДМИН-ПАНЕЛЬ\n\n"
            "📦 Разместить находку теперь находится прямо в главном меню.\n"
            "📋 Все объявления — список всех объявлений (включая закрытые)\n"
            "📝 Заявки на вещи — заявки от людей, доказывающих, что "
            "найденная вещь их (открыть заявку: «заявка 5»)\n"
            "🔄 Перенумеровать объявления — сбросить нумерацию с #1\n"
            "🗑 Удалить объявления — удалить одно, несколько или все",
            keyboard=admin_keyboard()
        )
        return

    # --------------------------------------------------------
    # АДМИН-ПАНЕЛЬ — МЕНЮ
    # --------------------------------------------------------

    session = get_session(peer_id)

    if session and session["state"] == "admin_menu":
        if not is_admin(user_id):
            clear_session(peer_id)
            await message.answer(
                "🏠 Главное меню",
                keyboard=main_keyboard(False)
            )
            return

        if text == "📋 все объявления":
            rows = admin_get_all_ads()
            await message.answer(
                admin_catalog_text(rows),
                keyboard=admin_keyboard()
            )
            return

        if text == "📝 заявки на вещи":
            rows = get_pending_claims()
            await message.answer(
                admin_claims_text(rows),
                keyboard=admin_keyboard()
            )
            return

        claim_match = re.match(r"^заявка\s*№?\s*(\d+)$", text)

        if claim_match:
            await send_claim_card(
                message, peer_id, int(claim_match.group(1))
            )
            return

        ad_stripped = raw_text.strip()

        if ad_stripped.startswith("#") and ad_stripped[1:].strip().isdigit():
            await open_ad_card(
                message, peer_id, user_id, int(ad_stripped[1:].strip())
            )
            return

        if text == "🔄 перенумеровать объявления":
            save_session(peer_id, "admin_renumber_confirm", {})
            await message.answer(
                "🔄 Перенумеровать все объявления?\n\n"
                "Активные объявления получат номера #1, #2, #3... "
                "по порядку создания.\n"
                "Сами объявления, фото, описания и авторы не изменятся "
                "и не удалятся — поменяются только видимые номера.",
                keyboard=admin_confirm_keyboard()
            )
            return

        if text == "🗑 удалить объявления":
            save_session(peer_id, "admin_delete_menu", {})
            await message.answer(
                "🗑 УДАЛЕНИЕ ОБЪЯВЛЕНИЙ\n\n"
                "1️⃣ Одно — удалить одно объявление по номеру\n"
                "🔢 Несколько — удалить сразу несколько по номерам\n"
                "💥 Все — удалить вообще все объявления",
                keyboard=admin_delete_menu_keyboard()
            )
            return

        if text in {"🏠 главное меню", "назад"}:
            clear_session(peer_id)
            await message.answer(
                "🏠 Главное меню",
                keyboard=main_keyboard(True)
            )
            return

        await message.answer(
            "Выберите действие в админ-панели:",
            keyboard=admin_keyboard()
        )
        return

    # --------------------------------------------------------
    # АДМИН-ПАНЕЛЬ — ПОДМЕНЮ УДАЛЕНИЯ
    # --------------------------------------------------------

    if session and session["state"] == "admin_delete_menu":
        if not is_admin(user_id):
            clear_session(peer_id)
            await message.answer(
                "🏠 Главное меню",
                keyboard=main_keyboard(False)
            )
            return

        if text == "1️⃣ одно":
            save_session(peer_id, "admin_delete_wait_number", {})
            await message.answer(
                "🗑 Пришлите номер объявления для удаления, например: #12",
                keyboard=cancel_keyboard()
            )
            return

        if text == "🔢 несколько":
            save_session(peer_id, "admin_delete_multi_wait_numbers", {})
            await message.answer(
                "🗑 Пришлите номера объявлений для удаления через "
                "запятую или пробел, например:\n"
                "#3, #7, #12\n"
                "или просто: 3 7 12",
                keyboard=cancel_keyboard()
            )
            return

        if text == "💥 все":
            total = len(admin_get_all_ads(limit=100000))
            save_session(peer_id, "admin_delete_all_confirm", {})
            await message.answer(
                "🗑 Удалить ВСЕ объявления?\n\n"
                f"Сейчас в базе {total} объявлений (активных и "
                "закрытых). Это действие необратимо — все они "
                "будут удалены полностью, включая фото и описания.",
                keyboard=admin_confirm_keyboard()
            )
            return

        if text == "🔙 назад в админ-панель":
            save_session(peer_id, "admin_menu", {})
            await message.answer(
                "⚙️ Админ-панель",
                keyboard=admin_keyboard()
            )
            return

        await message.answer(
            "Выберите действие или «🔙 Назад в админ-панель».",
            keyboard=admin_delete_menu_keyboard()
        )
        return

    # --------------------------------------------------------
    # АДМИН-ПАНЕЛЬ — ПОДТВЕРЖДЕНИЕ ПЕРЕНУМЕРАЦИИ
    # --------------------------------------------------------

    if session and session["state"] == "admin_renumber_confirm":
        if not is_admin(user_id):
            clear_session(peer_id)
            await message.answer(
                "🏠 Главное меню",
                keyboard=main_keyboard(False)
            )
            return

        if text == "✅ подтвердить":
            active_count = renumber_ads()
            save_session(peer_id, "admin_menu", {})

            await message.answer(
                "✅ Перенумерация выполнена.\n\n"
                f"Активных объявлений: {active_count} "
                f"(от #1 до #{active_count}).",
                keyboard=admin_keyboard()
            )
            return

        await message.answer(
            "Нажмите «✅ Подтвердить» или «❌ Отмена».",
            keyboard=admin_confirm_keyboard()
        )
        return

    # --------------------------------------------------------
    # АДМИН-ПАНЕЛЬ — УДАЛЕНИЕ ПО НОМЕРУ
    # --------------------------------------------------------

    if session and session["state"] == "admin_delete_wait_number":
        if not is_admin(user_id):
            clear_session(peer_id)
            await message.answer(
                "🏠 Главное меню",
                keyboard=main_keyboard(False)
            )
            return

        admin_stripped = raw_text.strip()
        number_part = (
            admin_stripped[1:].strip()
            if admin_stripped.startswith("#")
            else admin_stripped
        )

        if not number_part.isdigit():
            await message.answer(
                "Пришлите номер объявления, например: #12",
                keyboard=cancel_keyboard()
            )
            return

        target_ad = get_ad_by_number(int(number_part))

        if not target_ad:
            await message.answer(
                "❌ Объявление с таким номером не найдено.\n\n"
                "Проверьте номер и попробуйте ещё раз, или нажмите «Отмена».",
                keyboard=cancel_keyboard()
            )
            return

        admin_delete_ad(target_ad["id"])
        save_session(peer_id, "admin_menu", {})

        await message.answer(
            f"🗑 Объявление #{target_ad['number']} удалено.",
            keyboard=admin_keyboard()
        )
        return

    # --------------------------------------------------------
    # АДМИН-ПАНЕЛЬ — УДАЛЕНИЕ НЕСКОЛЬКИХ ПО НОМЕРАМ
    # --------------------------------------------------------

    if session and session["state"] == "admin_delete_multi_wait_numbers":
        if not is_admin(user_id):
            clear_session(peer_id)
            await message.answer(
                "🏠 Главное меню",
                keyboard=main_keyboard(False)
            )
            return

        raw_numbers = re.findall(r"\d+", raw_text)

        if not raw_numbers:
            await message.answer(
                "Не вижу номеров. Пришлите их через запятую или "
                "пробел, например: #3, #7, #12",
                keyboard=cancel_keyboard()
            )
            return

        numbers = sorted({int(n) for n in raw_numbers})
        deleted, not_found = admin_delete_ads_by_numbers(numbers)

        save_session(peer_id, "admin_menu", {})

        lines = []

        if deleted:
            deleted_list = ", ".join(f"#{n}" for n in deleted)
            lines.append(f"🗑 Удалено ({len(deleted)}): {deleted_list}")

        if not_found:
            not_found_list = ", ".join(f"#{n}" for n in not_found)
            lines.append(
                f"⚠️ Не найдено ({len(not_found)}): {not_found_list}"
            )

        if not lines:
            lines.append("Ничего не удалено.")

        await message.answer(
            "\n".join(lines),
            keyboard=admin_keyboard()
        )
        return

    # --------------------------------------------------------
    # АДМИН-ПАНЕЛЬ — ПОДТВЕРЖДЕНИЕ УДАЛЕНИЯ ВСЕХ ОБЪЯВЛЕНИЙ
    # --------------------------------------------------------

    if session and session["state"] == "admin_delete_all_confirm":
        if not is_admin(user_id):
            clear_session(peer_id)
            await message.answer(
                "🏠 Главное меню",
                keyboard=main_keyboard(False)
            )
            return

        if text == "✅ подтвердить":
            count = admin_delete_all_ads()
            save_session(peer_id, "admin_menu", {})

            await message.answer(
                f"🗑 Удалены все объявления ({count} шт.).\n\n"
                "База объявлений теперь пуста.",
                keyboard=admin_keyboard()
            )
            return

        await message.answer(
            "Нажмите «✅ Подтвердить», чтобы удалить ВСЕ объявления, "
            "или «❌ Отмена».",
            keyboard=admin_confirm_keyboard()
        )
        return

    # --------------------------------------------------------
    # АДМИН-ПАНЕЛЬ — ПРОСМОТР ЗАЯВКИ (ОДОБРИТЬ / ОТКЛОНИТЬ)
    # --------------------------------------------------------

    if session and session["state"] == "admin_view_claim":
        if not is_admin(user_id):
            clear_session(peer_id)
            await message.answer(
                "🏠 Главное меню",
                keyboard=main_keyboard(False)
            )
            return

        claim_id = int(session["data"]["claim_id"])
        claim = get_claim(claim_id)

        if not claim:
            clear_session(peer_id)
            await message.answer(
                "❌ Заявка не найдена.",
                keyboard=admin_keyboard()
            )
            return

        if claim["status"] != "pending":
            clear_session(peer_id)
            await message.answer(
                "Эта заявка уже рассмотрена.",
                keyboard=admin_keyboard()
            )
            return

        if text == "✅ одобрить":
            set_claim_status(claim_id, "approved", user_id)
            ad = get_ad(claim["ad_id"])

            approved_at = datetime.now().strftime("%d.%m.%Y в %H:%M")

            if ad:
                item_line = f"Вещь: {ad['title']} (#{ad['number']})\n"
            else:
                item_line = ""

            claimant_message = (
                "✅ ЗАЯВКА ОДОБРЕНА — ПРОПУСК НА ВЫДАЧУ ВЕЩИ\n\n"
                f"{item_line}"
                f"Заявка №{claim_id}\n"
                f"Одобрено: {approved_at}\n\n"
                f"📍 {PICKUP_INSTRUCTIONS}"
            )

            if ad:
                admin_resolve_ad(ad["id"])

            notify_user(claim["claimant_id"], claimant_message)
            save_session(peer_id, "admin_menu", {})

            await message.answer(
                f"✅ Заявка №{claim_id} одобрена.\n\n"
                "Заявитель уведомлён, объявление о находке закрыто.",
                keyboard=admin_keyboard()
            )
            return

        if text == "❌ отклонить":
            set_claim_status(claim_id, "rejected", user_id)

            notify_user(
                claim["claimant_id"],
                f"❌ Ваша заявка №{claim_id} отклонена администратором.\n\n"
                "Если вы уверены, что это ваша вещь, напишите "
                "администраторам сообщества напрямую."
            )

            save_session(peer_id, "admin_menu", {})

            await message.answer(
                f"❌ Заявка №{claim_id} отклонена. Заявитель уведомлён.",
                keyboard=admin_keyboard()
            )
            return

        if text in {"🏠 главное меню", "назад"}:
            clear_session(peer_id)
            await message.answer(
                "🏠 Главное меню",
                keyboard=main_keyboard(True)
            )
            return

        await message.answer(
            "Нажмите «✅ Одобрить» или «❌ Отклонить».",
            keyboard=claim_admin_keyboard()
        )
        return

    # --------------------------------------------------------
    # АДМИН — ОТКРЫТИЕ ЗАЯВКИ ПО НОМЕРУ ("заявка 5") ИЗ ЛЮБОГО МЕСТА
    # --------------------------------------------------------

    if is_admin(user_id):
        claim_match = re.match(r"^заявка\s*№?\s*(\d+)$", text)

        if claim_match:
            await send_claim_card(
                message, peer_id, int(claim_match.group(1))
            )
            return

    # --------------------------------------------------------
    # ОТКРЫТИЕ ОБЪЯВЛЕНИЯ ПО #ID
    # --------------------------------------------------------

    stripped = raw_text.strip()

    if stripped.startswith("#"):
        number = stripped[1:].strip()

        if number.isdigit():
            await open_ad_card(message, peer_id, user_id, int(number))
            return

    # --------------------------------------------------------
    # ДЕЙСТВИЯ С ОТКРЫТЫМ ОБЪЯВЛЕНИЕМ
    # --------------------------------------------------------

    session = get_session(peer_id)

    if session and session["state"] == "view_ad":
        ad_id = int(session["data"]["ad_id"])
        ad = get_ad(ad_id)

        if not ad:
            clear_session(peer_id)

            await message.answer(
                "❌ Объявление не найдено.",
                keyboard=main_keyboard(is_admin(user_id))
            )
            return

        if text == "✅ вещь найдена":
            if resolve_ad(ad_id, user_id):
                clear_session(peer_id)

                await message.answer(
                    f"✅ Объявление #{ad['number']} закрыто.\n\n"
                    "Теперь оно не показывается в активном каталоге.",
                    keyboard=main_keyboard(is_admin(user_id))
                )
            else:
                await message.answer(
                    "❌ Закрыть объявление может только его автор."
                )
            return

        if text == "🗑 удалить объявление":
            if delete_ad(ad_id, user_id):
                clear_session(peer_id)

                await message.answer(
                    f"🗑 Объявление #{ad['number']} удалено.",
                    keyboard=main_keyboard(is_admin(user_id))
                )
            else:
                await message.answer(
                    "❌ Удалить объявление может только его автор."
                )
            return

        if text == "✉️ написать автору":
            if ad["user_id"] == user_id:
                await message.answer(
                    "Это ваше собственное объявление 🙂"
                )
                return

            if is_admin(ad["user_id"]):
                await message.answer(
                    "✉️ Для этого объявления связь с автором через бота "
                    "недоступна.",
                    keyboard=pick_viewer_keyboard(ad, user_id)
                )
                return

            await message.answer(
                "✉️ Автор объявления: "
                f"[id{ad['user_id']}|написать сообщение]\n\n"
                "Нажмите на ссылку, чтобы открыть диалог с автором "
                f"и договориться о возврате вещи (#{ad['number']}).",
                keyboard=pick_viewer_keyboard(ad, user_id)
            )
            return

        if text == "📝 это моя вещь":
            if ad["ad_type"] != "found":
                await message.answer(
                    "Заявку можно подать только на объявление о находке "
                    "(📦 НАЙДЕНО)."
                )
                return

            if ad["user_id"] == user_id:
                await message.answer(
                    "Это ваше собственное объявление 🙂"
                )
                return

            save_session(
                peer_id,
                "claim_wait_proof",
                {"ad_id": ad_id}
            )

            await message.answer(
                "📝 ПОДАЧА ЗАЯВКИ\n\n"
                f"Опишите, почему вы считаете, что вещь из объявления "
                f"#{ad['number']} — ваша.\n\n"
                "Укажите приметы, отличия, обстоятельства потери — "
                "всё, что поможет администратору убедиться, что вещь "
                "действительно ваша.\n\n"
                "После этого можно будет приложить фото как "
                "дополнительное доказательство.",
                keyboard=cancel_keyboard()
            )
            return

        if text == "🎯 эта вещь нашлась":
            if not is_admin(user_id):
                await message.answer(
                    "Отмечать вещи найденными может только администратор."
                )
                return

            if ad["ad_type"] != "lost":
                await message.answer(
                    "Эта кнопка только для объявлений о розыске "
                    "(📢 ПОТЕРЯНО)."
                )
                return

            if ad["user_id"] == user_id:
                await message.answer(
                    "Это ваше собственное объявление 🙂"
                )
                return

            if ad["status"] != "active":
                await message.answer(
                    "Это объявление уже закрыто."
                )
                return

            save_session(
                peer_id,
                "admin_confirm_found",
                {"ad_id": ad_id}
            )

            await message.answer(
                f"🎯 Отметить вещь из объявления #{ad['number']} "
                f"(«{ad['title']}») как найденную?\n\n"
                "Автору придёт уведомление, что его вещь нашлась, "
                "а объявление закроется.",
                keyboard=admin_confirm_keyboard()
            )
            return

        if text in {"🏠 главное меню", "назад"}:
            clear_session(peer_id)

            await message.answer(
                "🏠 Главное меню",
                keyboard=main_keyboard(is_admin(user_id))
            )
            return

    # --------------------------------------------------------
    # АДМИН — ПОДТВЕРЖДЕНИЕ «ВЕЩЬ НАШЛАСЬ»
    # --------------------------------------------------------

    if session and session["state"] == "admin_confirm_found":
        if not is_admin(user_id):
            clear_session(peer_id)
            await message.answer(
                "🏠 Главное меню",
                keyboard=main_keyboard(False)
            )
            return

        found_ad_id = int(session["data"]["ad_id"])
        found_ad = get_ad(found_ad_id)

        if not found_ad:
            clear_session(peer_id)
            await message.answer(
                "❌ Объявление не найдено.",
                keyboard=main_keyboard(True)
            )
            return

        if text == "✅ подтвердить":
            admin_resolve_ad(found_ad_id)
            clear_session(peer_id)

            notify_user(
                found_ad["user_id"],
                "🎉 ВАШУ ВЕЩЬ НАШЛИ!\n\n"
                f"Вещь: {found_ad['title']} (#{found_ad['number']})\n\n"
                f"📍 {PICKUP_INSTRUCTIONS}"
            )

            await message.answer(
                f"🎯 Объявление #{found_ad['number']} закрыто, "
                "автору отправлено уведомление о находке.",
                keyboard=main_keyboard(True)
            )
            return

        await message.answer(
            "Нажмите «✅ Подтвердить» или «❌ Отмена».",
            keyboard=admin_confirm_keyboard()
        )
        return

    # --------------------------------------------------------
    # ПОДАЧА ЗАЯВКИ — ТЕКСТ-ДОКАЗАТЕЛЬСТВО
    # --------------------------------------------------------

    if session and session["state"] == "claim_wait_proof":
        proof_text = raw_text.strip()

        if len(proof_text) < 10:
            await message.answer(
                "Опишите подробнее (не менее 10 символов) — это поможет "
                "администратору принять решение.",
                keyboard=cancel_keyboard()
            )
            return

        if len(proof_text) > 1500:
            await message.answer(
                "Слишком длинно, сократите описание до 1500 символов.",
                keyboard=cancel_keyboard()
            )
            return

        claim_ad_id = int(session["data"]["ad_id"])
        claim_ad = get_ad(claim_ad_id)

        if not claim_ad or claim_ad["status"] != "active":
            clear_session(peer_id)
            await message.answer(
                "❌ Это объявление уже недоступно для подачи заявки.",
                keyboard=main_keyboard(is_admin(user_id))
            )
            return

        save_session(
            peer_id,
            "claim_wait_photo",
            {"ad_id": claim_ad_id, "proof_text": proof_text}
        )

        await message.answer(
            "📷 Если есть старое фото этой вещи (из галереи, до "
            "потери) — пришлите его как дополнительное доказательство.\n\n"
            "Это необязательно, можно пропустить.",
            keyboard=photo_keyboard()
        )
        return

    # --------------------------------------------------------
    # ПОДАЧА ЗАЯВКИ — ФОТО-ДОКАЗАТЕЛЬСТВО (НЕОБЯЗАТЕЛЬНО)
    # --------------------------------------------------------

    if session and session["state"] == "claim_wait_photo":
        if text in {"пропустить фото", "⏭ пропустить фото"}:
            claim_photo = None
        else:
            claim_photo = extract_photo_attachment(message)

            if not claim_photo:
                await message.answer(
                    "📷 Я не вижу фото.\n\n"
                    "Пришлите именно фотографию сообщением или нажмите "
                    "«Пропустить фото».",
                    keyboard=photo_keyboard()
                )
                return

        claim_ad_id = int(session["data"]["ad_id"])
        proof_text = session["data"]["proof_text"]
        claim_ad = get_ad(claim_ad_id)

        if not claim_ad or claim_ad["status"] != "active":
            clear_session(peer_id)
            await message.answer(
                "❌ Это объявление уже недоступно для подачи заявки.",
                keyboard=main_keyboard(is_admin(user_id))
            )
            return

        claim_id = create_claim(
            claim_ad_id, user_id, proof_text, claim_photo
        )
        clear_session(peer_id)

        notify_admins(
            f"🆕 НОВАЯ ЗАЯВКА №{claim_id}\n\n"
            f"По объявлению #{claim_ad['number']} — {claim_ad['title']}\n"
            f"📦 Нашёл вещь: [id{claim_ad['user_id']}|профиль]\n\n"
            f"Заявитель: [id{user_id}|профиль]\n\n"
            f"📄 Доказательства:\n{proof_text}\n\n" +
            (
                "📷 Заявитель приложил своё фото вещи — прикреплено к "
                "этому сообщению.\n\n"
                if claim_photo else ""
            ) +
            "Чтобы открыть заявку, отправьте боту:\n"
            f"заявка {claim_id}",
            attachment=combine_attachments(
                claim_ad["photo_attachment"], claim_photo
            )
        )

        photo_note = (
            " и приложенным фото" if claim_photo else ""
        )

        await message.answer(
            f"✅ Заявка №{claim_id} с описанием{photo_note} отправлена "
            "администраторам.\n\n"
            "Как только её рассмотрят, вы получите сообщение с решением.",
            keyboard=main_keyboard(is_admin(user_id))
        )
        return

    # --------------------------------------------------------
    # ПОМОЩЬ
    # --------------------------------------------------------

    if text == "ℹ️ помощь":
        clear_session(peer_id)

        admin_lines = (
            "📦 Разместить находку — кнопка находится прямо в главном меню "
            "и видна только администраторам.\n\n"
            "🎯 Эта вещь нашлась — на чужом объявлении о розыске: "
            "отметить, что вещь нашлась (например, лежит на вахте), "
            "автору придёт уведомление. Доступно только "
            "администраторам.\n\n"
            if is_admin(user_id) else ""
        )

        await message.answer(
            "ℹ️ ПОМОЩЬ\n\n"
            "📚 Объявления — сначала выберите, что смотреть: "
            "«📦 Найденные вещи» (уже найдены, ждут владельца) или "
            "«📢 Розыск» (объявления тех, кто ищет свою вещь). Дальше "
            "нажмите категорию, чтобы сразу увидеть все вещи в ней "
            "(есть и «🎓 Школьная форма» отдельно от «🎒 Одежда и "
            "аксессуары»). Кнопка «🔍 Поиск по слову» ищет по "
            "названию и описанию, а «📖 Все объявления» показывает "
            "весь список выбранного типа.\n\n"
            "📢 Я потерял вещь — создать объявление о том, что вы "
            "ищете свою вещь (доступно всем).\n\n"
            "🔐 Доступ сотрудника — введите пароль, чтобы получить "
            "доступ администратора.\n\n"
            f"{admin_lines}"
            "📋 Мои объявления — ваши объявления.\n\n"
            "✉️ Написать автору — появляется у чужого объявления, "
            "открывает диалог с человеком, который его разместил.\n\n"
            "📝 Это моя вещь — на объявлении о находке: подать заявку "
            "администраторам и доказать, что вещь ваша.\n\n"
            "🎒 Как забрать вещь, если заявку одобрили:\n"
            f"{PICKUP_INSTRUCTIONS}\n\n"
            "Бот пришлёт сообщение-«пропуск» с номером заявки и датой "
            "одобрения — покажите его на месте выдачи (можно прямо с "
            "экрана телефона). Если сообщения под рукой нет — просто "
            "опишите приметы вещи, ответственный сверится с "
            "объявлением и заявкой.\n\n"
            "Чтобы открыть конкретную вещь, отправьте её номер, "
            "например #12.",
            keyboard=help_keyboard(is_admin(user_id))
        )
        return

    # --------------------------------------------------------
    # НЕИЗВЕСТНОЕ СООБЩЕНИЕ
    # --------------------------------------------------------

    await message.answer(
        "🤔 Не понял команду.\n\n"
        "Откройте главное меню:",
        keyboard=main_keyboard(is_admin(user_id))
    )


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":
    init_db()

    if TOKEN.startswith("ВСТАВЬ_"):
        raise SystemExit(
            "❌ Сначала вставьте новый ключ сообщества "
            "в переменную TOKEN."
        )

    print("🤖 Запускаем «Бюро находок»...")

    try:
        setup_longpoll()
        bot.run()
    except (URLError, OSError) as exc:
        diagnose_connection_error(exc)
        raise SystemExit(1)
